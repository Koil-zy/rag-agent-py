"""流式输出滑动窗口检测。

设计要点：
- 每收到一段模型增量就累入缓冲区；
- 以 window_size 为窗口、stride 为步长滑动检查，只有被窗口完整覆盖的文本才允许下发；
  因此窗口内的违规内容永远不会到达前端（最坏情况是多等一个窗口的延迟）；
- 检查项：提示词泄露、身份证/银行卡、资料中不存在的数字（防编造金额/天数/比例）、禁用词；
- 收尾时统计引用覆盖情况（按配置决定“提示”还是“拦截”）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import config

CITE_RE = re.compile(r"\[(\d{1,2})\]")
# 带单位的数字（金额/天数/比例/期限等）
NUM_UNIT_RE = re.compile(
    r"(\d[\d,]*(?:\.\d+)?)\s*(万元|元|%|％|个工作日|个自然日|工作日|自然日|天|次|小时|分钟|年|月|学时|人|倍|折|号)"
)
# 不带单位的 3 位以上数字
BARE_NUM_RE = re.compile(r"(?<![\d\[\]])(\d{3,})(?![\d\]])")
# 中文条款编号（第X章/第X条…），用于识别资料中不存在的章节引用
CN_CLAUSE_RE = re.compile(r"第[一二三四五六七八九十百零〇两\d]+[章节条款项]")
ID_CARD_RE = re.compile(r"\b\d{17}[\dXx]\b")
BANK_CARD_RE = re.compile(r"\b\d{16,19}\b")


class GuardBlocked(Exception):
    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


@dataclass
class GuardMeta:
    windows: int = 0
    violations: list[dict] = field(default_factory=list)
    soft_issues: list[dict] = field(default_factory=list)
    citation_miss: bool = False
    citation_ratio: float = 1.0
    invalid_citations: list[int] = field(default_factory=list)


def _norm_number(raw: str) -> str:
    return raw.replace(",", "").replace("，", "")


def _source_numbers(text: str) -> set[str]:
    found = set()
    for m in NUM_UNIT_RE.finditer(text):
        found.add(_norm_number(m.group(1)))
    for m in BARE_NUM_RE.finditer(text):
        found.add(_norm_number(m.group(1)))
    return found


class SlidingWindowGuard:
    def __init__(
        self,
        source_text: str,
        source_count: int,
        question: str = "",
        cfg_prefix: str = "stream_guard",
    ) -> None:
        self.window = int(config.get(f"{cfg_prefix}.window_size", 80))
        self.stride = max(1, int(config.get(f"{cfg_prefix}.stride", 40)))
        self.enabled = bool(config.get(f"{cfg_prefix}.enabled", True))
        self.leak_patterns = [p for p in config.get(f"{cfg_prefix}.leak_patterns", []) or [] if p]
        self.banned_words = [w for w in config.get(f"{cfg_prefix}.banned_words", []) or [] if w]
        self.check_numbers = bool(config.get(f"{cfg_prefix}.check_unsupported_numbers", True))
        self.citation_tolerance = float(config.get(f"{cfg_prefix}.citation_tolerance", 0.34))
        self.on_citation_miss = str(config.get(f"{cfg_prefix}.on_citation_miss", "annotate"))

        self.buffer = ""
        self.checked = 0
        self.emitted = 0
        self.total_checked = 0
        self.source_count = source_count
        self.source_numbers = _source_numbers(source_text)
        self.question_numbers = _source_numbers(question)
        self.raw_source = source_text.replace(",", "").replace("，", "")
        self.raw_question = question
        self.meta = GuardMeta()

    # ---------- 对外接口 ----------
    def feed(self, delta: str) -> str:
        if not self.enabled:
            return delta
        self.buffer += delta
        while len(self.buffer) - self.checked >= self.window:
            window = self.buffer[self.checked : self.checked + self.window]
            self._inspect(window)
            self.checked += self.stride
            self.meta.windows += 1
        out = self.buffer[self.emitted : self.checked]
        self.emitted = self.checked
        self.total_checked += len(out)
        return out

    def finish(self) -> str:
        """返回剩余未下发的文本，并做整段引用检查。"""
        if not self.enabled:
            return ""
        tail = self.buffer[self.checked :]
        if tail:
            self._inspect(tail)
            self.checked = len(self.buffer)
        rest = self.buffer[self.emitted :]
        self.emitted = len(self.buffer)
        self._check_citations()
        return rest

    @property
    def answer(self) -> str:
        return self.buffer

    # ---------- 检测实现 ----------
    def _inspect(self, window: str) -> None:
        low = window.lower()

        for pattern in self.leak_patterns:
            if pattern.lower() in low:
                self._raise("prompt_leak", f"命中提示词泄露规则：{pattern}", window)

        for word in self.banned_words:
            if word and word in window:
                self._raise("banned_word", f"命中禁用词：{word}", window)

        if ID_CARD_RE.search(window):
            self._raise("sensitive_id", "输出包含疑似身份证号", window)
        if BANK_CARD_RE.search(window):
            self._raise("sensitive_bank", "输出包含疑似银行卡号", window)

        if self.check_numbers:
            for m in NUM_UNIT_RE.finditer(window):
                num = _norm_number(m.group(1))
                if num not in self.source_numbers and num not in self.question_numbers:
                    self._raise("unsupported_number", f"数字 {num}{m.group(2)} 未在资料中出现", m.group(0))
            for m in BARE_NUM_RE.finditer(window):
                num = _norm_number(m.group(1))
                if (
                    num not in self.source_numbers
                    and num not in self.question_numbers
                    and num not in self.raw_source
                ):
                    self._raise("unsupported_number", f"数字 {num} 未在资料中出现", m.group(0))
            # 条款编号错误只做提示，不中断输出（答案主体通常仍然正确）
            for m in CN_CLAUSE_RE.finditer(window):
                clause = m.group(0)
                if clause in self.raw_source or clause in self.raw_question:
                    continue
                issue = {"kind": "unsupported_clause", "detail": f"条款编号 {clause} 未在资料中出现"}
                if issue not in self.meta.soft_issues:
                    self.meta.soft_issues.append(issue)

    def _raise(self, kind: str, detail: str, snippet: str) -> None:
        self.meta.violations.append({"kind": kind, "detail": detail, "snippet": snippet[:60]})
        raise GuardBlocked(kind, detail)

    def _check_citations(self) -> None:
        answer = self.buffer.strip()
        if not answer:
            return
        # 明确的拒答话术不需要引用
        if "未找到相关依据" in answer and len(answer) < 40:
            return

        citations = [int(n) for n in CITE_RE.findall(answer)]
        self.meta.invalid_citations = sorted({n for n in citations if n < 1 or n > self.source_count})
        if self.meta.invalid_citations:
            self.meta.violations.append(
                {"kind": "invalid_citation", "detail": f"引用了不存在的来源编号 {self.meta.invalid_citations}"}
            )

        sentences = [s.strip() for s in re.split(r"(?<=[。！？!?；;\n])", answer) if len(s.strip()) >= 6]
        if not sentences:
            return
        cited = sum(1 for s in sentences if CITE_RE.search(s))
        self.meta.citation_ratio = cited / len(sentences)
        if self.meta.citation_ratio < 1 - self.citation_tolerance:
            self.meta.citation_miss = True
            if self.on_citation_miss == "block":
                self._raise(
                    "citation_miss",
                    f"引用覆盖率 {self.meta.citation_ratio:.0%} 低于要求",
                    answer[-60:],
                )

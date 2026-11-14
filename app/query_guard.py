"""用户自然语言提问的规范性校验（确定性规则，可测试、零额外模型成本）。"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from . import config

INJECTION_PATTERNS = [
    r"忽略(以上|之前|前面|上述).{0,8}(指令|提示|设定)",
    r"(系统|system).{0,6}(提示词|prompt|设定)",
    r"你现在是|开发者模式|越狱|DAN模式",
    r"输出(你的|全部的).{0,6}(指令|设定|提示词)",
    r"repeat.{0,10}(prompt|instruction)",
]
_INJECTION_RE = re.compile("|".join(INJECTION_PATTERNS), re.IGNORECASE)
_QUESTION_HINTS = ("吗", "呢", "多少", "怎么", "如何", "什么", "哪些", "能否", "是否", "多久", "几天", "几号", "标准", "流程", "?")


@dataclass
class QueryCheck:
    ok: bool
    code: str = "ok"
    message: str = ""
    normalized: str = ""
    off_domain: bool = False
    injection: bool = False


def domain_keywords() -> list[str]:
    return list(config.get("query_guard.domain_keywords", []) or [])


def validate(question: str) -> QueryCheck:
    min_len = int(config.get("query_guard.min_length", 4))
    max_len = int(config.get("query_guard.max_length", 200))
    min_unique = float(config.get("query_guard.min_unique_ratio", 0.35))

    text = (question or "").strip()
    text = re.sub(r"\s{2,}", " ", text)

    if not text:
        return QueryCheck(False, "empty", "请输入你的问题，例如：出差住宿标准是多少？")
    if len(text) < min_len:
        return QueryCheck(False, "too_short", "问题太短了，请补充具体业务场景后再提问。")
    if len(text) > max_len:
        return QueryCheck(False, "too_long", f"问题请控制在 {max_len} 字以内，便于精准检索。")

    meaningful = [ch for ch in text if ch.isalnum() or "\u4e00" <= ch <= "\u9fff"]
    if len(meaningful) / max(len(text), 1) < 0.6:
        return QueryCheck(False, "noise", "未识别到有效文字，请用中文或英文描述问题。")

    unique_ratio = len(set(text)) / max(len(text), 1)
    most_common_ratio = Counter(text).most_common(1)[0][1] / max(len(text), 1)
    if unique_ratio < min_unique or most_common_ratio > 0.5:
        return QueryCheck(False, "gibberish", "问题看起来不是完整语句，请重新描述。")

    if _INJECTION_RE.search(text):
        return QueryCheck(
            False,
            "injection",
            "问题中似乎包含试图改变系统指令的内容，请用正常业务语言提问。",
            injection=True,
        )

    keywords = domain_keywords()
    has_domain = any(k in text for k in keywords)
    has_question_hint = any(k in text for k in _QUESTION_HINTS) or "？" in text or "?" in text
    if not has_question_hint and not has_domain:
        return QueryCheck(
            False,
            "not_question",
            "请用完整的自然语言提问，例如：年假有几天？出差住宿标准是多少？",
        )

    return QueryCheck(True, normalized=text, off_domain=not has_domain)

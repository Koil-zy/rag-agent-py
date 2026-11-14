"""RAG 评测 runner：开发集调参、冻结集定版。

用法：
    python -m eval.run_eval --set dev        # 开发集
    python -m eval.run_eval --set frozen     # 冻结集（校验 SHA256，集变更则拒绝运行）
    python -m eval.run_eval --set all
    python -m eval.run_eval --freeze         # 首次生成冻结集哈希锁
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import statistics
import time
from datetime import datetime
from pathlib import Path

from app import db, pipeline

EVAL_DIR = Path(__file__).resolve().parent
RESULTS_DIR = EVAL_DIR / "results"
FROZEN_FILE = EVAL_DIR / "frozen_set.jsonl"
FROZEN_LOCK = EVAL_DIR / "frozen_set.sha256"


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_set(name: str) -> list[dict]:
    path = EVAL_DIR / f"{name}_set.jsonl"
    with path.open("r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def verify_frozen() -> str:
    if not FROZEN_LOCK.exists():
        raise SystemExit("冻结集未锁定：请先执行 python -m eval.run_eval --freeze")
    expected = FROZEN_LOCK.read_text(encoding="utf-8").strip()
    actual = sha256_of(FROZEN_FILE)
    if expected != actual:
        raise SystemExit(
            "冻结集内容已变更，评测拒绝运行。\n"
            f"锁定哈希：{expected}\n当前哈希：{actual}\n"
            "如需重新定版，请人工确认后执行 --freeze 重新锁定。"
        )
    return actual


async def evaluate(items: list[dict]) -> dict:
    rows = []
    for item in items:
        started = time.time()
        result = await pipeline.answer_sync(item["question"], role=item.get("role", "employee"))
        latency = time.time() - started

        sources = result.get("sources", [])
        doc_codes = [s["doc_code"] for s in sources]
        expected_docs = item.get("expected_docs", [])
        forbid = item.get("forbid_docs", [])

        expect_validation = item.get("expect_validation")
        validation_ok = None
        if expect_validation:
            # 校验只看“应拒绝/应放行”是否判断正确，具体错误码不作为评分依据
            reject_codes = {"empty", "too_short", "too_long", "noise", "gibberish", "injection", "not_question"}
            expect_reject = expect_validation not in ("ok", "accept")
            validation_ok = expect_reject == (result.get("code") in reject_codes)

        hit_rank = 0
        for i, code in enumerate(doc_codes, start=1):
            if code in expected_docs:
                hit_rank = i
                break
        retrieval_hit = bool(hit_rank) if expected_docs else None
        mrr = 1.0 / hit_rank if hit_rank else (0.0 if expected_docs else None)

        answer = result.get("answer", "")
        answer_abstain = "未找到相关依据" in answer
        abstain_correct = None
        if expect_validation:
            abstain_correct = validation_ok
        elif item.get("answerable"):
            abstain_correct = (not result.get("abstained", False)) and not answer_abstain
        else:
            abstain_correct = result.get("abstained", False) or answer_abstain

        acl_leak = bool(set(doc_codes) & set(forbid)) if forbid else False

        guard = result.get("guard") or {}
        invalid_citations = guard.get("invalid_citations") or []
        citation_ratio = result.get("citation_ratio")
        citation_ok = None
        if item.get("answerable") and not expect_validation and not result.get("blocked"):
            citation_ok = bool(result.get("abstained")) or (
                not invalid_citations and (citation_ratio is None or citation_ratio >= 0.6)
            )

        expect_any = item.get("expect_any") or []
        keyword_ok = None
        if expect_any and item.get("answerable") and not result.get("abstained"):
            keyword_ok = any(k in answer for k in expect_any)

        rows.append(
            {
                "id": item["id"],
                "role": item.get("role"),
                "question": item["question"],
                "answerable": item.get("answerable"),
                "abstained": result.get("abstained"),
                "blocked": result.get("blocked", False),
                "block_kind": result.get("block_kind"),
                "validation": result.get("code", "ok"),
                "validation_ok": validation_ok,
                "doc_codes": doc_codes,
                "best_cosine": result.get("best_cosine"),
                "retrieval_hit": retrieval_hit,
                "mrr": mrr,
                "abstain_correct": abstain_correct,
                "acl_leak": acl_leak,
                "citation_ok": citation_ok,
                "keyword_ok": keyword_ok,
                "latency_s": round(latency, 2),
                "answer": answer[:400],
                "note": item.get("note", ""),
            }
        )
        flag = "OK " if (abstain_correct is not False and not acl_leak) else "!! "
        print(
            f"{flag}{item['id']:<10} {result.get('code', 'ok'):<10} "
            f"abstain={str(result.get('abstained')):<5} block={str(result.get('blocked', False)):<5} "
            f"docs={','.join(doc_codes) or '-'} {latency:5.1f}s"
        )

    def rate(values: list[bool]) -> float:
        vals = [v for v in values if v is not None]
        return round(sum(1 for v in vals if v) / len(vals), 3) if vals else 0.0

    summary = {
        "total": len(rows),
        "validation_accuracy": rate([r["validation_ok"] for r in rows]),
        "retrieval_hit_rate": rate([r["retrieval_hit"] for r in rows]),
        "mrr": round(
            statistics.mean([r["mrr"] for r in rows if r["mrr"] is not None]), 3
        )
        if any(r["mrr"] is not None for r in rows)
        else 0.0,
        "abstain_accuracy": rate([r["abstain_correct"] for r in rows]),
        "acl_safety": 1.0 - rate([r["acl_leak"] for r in rows]) if any(
            "forbid_docs" in i for i in items
        ) else 1.0,
        "keyword_coverage": rate([r["keyword_ok"] for r in rows]),
        "citation_validity": rate([r["citation_ok"] for r in rows]),
        "guard_block_rate": round(sum(1 for r in rows if r["blocked"]) / len(rows), 3) if rows else 0.0,
        "avg_latency_s": round(statistics.mean([r["latency_s"] for r in rows]), 2) if rows else 0.0,
    }
    return {"summary": summary, "rows": rows}


def print_summary(name: str, summary: dict) -> None:
    print("\n" + "=" * 62)
    print(f"评测集：{name}    样本数：{summary['total']}")
    print("-" * 62)
    print(f"问题校验准确率     {summary['validation_accuracy']:.1%}")
    print(f"检索命中率 hit@k   {summary['retrieval_hit_rate']:.1%}")
    print(f"MRR               {summary['mrr']:.3f}")
    print(f"正确拒答率         {summary['abstain_accuracy']:.1%}")
    print(f"ACL 隔离安全率     {summary['acl_safety']:.1%}")
    print(f"答案关键词覆盖     {summary['keyword_coverage']:.1%}")
    print(f"引用有效率         {summary['citation_validity']:.1%}")
    print(f"Guard 拦截率       {summary['guard_block_rate']:.1%}")
    print(f"平均耗时           {summary['avg_latency_s']:.2f}s")
    print("=" * 62)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--set", dest="set_name", choices=["dev", "frozen", "all"], default="dev")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--freeze", action="store_true", help="重新锁定冻结集哈希")
    parser.add_argument("--allow-frozen-change", action="store_true", help="允许冻结集变更后继续")
    args = parser.parse_args()

    if args.freeze:
        FROZEN_LOCK.write_text(sha256_of(FROZEN_FILE) + "\n", encoding="utf-8")
        print(f"已锁定冻结集：{FROZEN_LOCK} -> {sha256_of(FROZEN_FILE)}")
        return

    db.init_schema()
    sets = ["dev", "frozen"] if args.set_name == "all" else [args.set_name]

    frozen_hash = ""
    if "frozen" in sets:
        if args.allow_frozen_change:
            if not FROZEN_LOCK.exists():
                raise SystemExit("冻结集未锁定，请先执行 --freeze")
            frozen_hash = sha256_of(FROZEN_FILE)
            print(f"[警告] 已忽略冻结集哈希校验，当前哈希 {frozen_hash}")
        else:
            frozen_hash = verify_frozen()
            print(f"冻结集哈希校验通过：{frozen_hash[:16]}…")

    RESULTS_DIR.mkdir(exist_ok=True)
    report = {"generated_at": datetime.now().isoformat(timespec="seconds"), "frozen_sha256": frozen_hash, "sets": {}}
    for name in sets:
        items = load_set(name)
        if args.limit:
            items = items[: args.limit]
        outcome = asyncio.run(evaluate(items))
        print_summary(name, outcome["summary"])
        report["sets"][name] = outcome

    out = RESULTS_DIR / f"{args.set_name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告已保存：{out}")


if __name__ == "__main__":
    main()

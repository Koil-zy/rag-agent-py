"""问答编排：问题校验 -> ACL 检索 -> 阈值拒答 -> 流式生成 -> 滑动窗口检测 -> 落库。"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator

from . import db, generator, query_guard, retrieval
from .stream_guard import GuardBlocked, SlidingWindowGuard

logger = logging.getLogger(__name__)

GUARD_FALLBACK = "抱歉，本次回答未通过内容合规校验，已停止输出。请查看右侧来源面板中的原文，或换一种问法再试。"
NO_EVIDENCE = "未找到相关依据。建议补充制度名称或具体场景（如报销时效、年假天数），也可以直接咨询 HR 或财务。"


def _save_turn(
    user_id: int,
    conversation_id: int | None,
    question: str,
    answer: str,
    sources: list[dict],
    meta: dict,
) -> int:
    with db.connect() as conn:
        if not conversation_id:
            row = conn.execute(
                "INSERT INTO conversations (user_id, title) VALUES (%s, %s) RETURNING id",
                (user_id, question[:40]),
            ).fetchone()
            conversation_id = row["id"]
        conn.execute(
            "INSERT INTO messages (conversation_id, role, content) VALUES (%s, 'user', %s)",
            (conversation_id, question),
        )
        conn.execute(
            "INSERT INTO messages (conversation_id, role, content, sources, meta) VALUES (%s, 'assistant', %s, %s, %s)",
            (conversation_id, answer, json.dumps(sources, ensure_ascii=False), json.dumps(meta, ensure_ascii=False)),
        )
    return conversation_id


def _log_guard(user_id: int | None, question: str, kind: str, detail: dict) -> None:
    try:
        db.execute(
            "INSERT INTO guard_events (user_id, question, kind, detail) VALUES (%s, %s, %s, %s)",
            (user_id, question[:200], kind, json.dumps(detail, ensure_ascii=False)),
        )
    except Exception:  # noqa: BLE001  审计失败不影响主流程
        logger.exception("写入 guard_events 失败")


def _sources_payload(outcome: retrieval.RetrievalOutcome) -> list[dict]:
    return [s.as_dict() for s in outcome.sources]


async def stream_chat(
    question: str,
    user: dict,
    conversation_id: int | None = None,
) -> AsyncIterator[dict]:
    started = time.time()

    check = query_guard.validate(question)
    if not check.ok:
        _log_guard(user["id"], question, f"query_{check.code}", {"message": check.message})
        yield {"event": "validation_failed", "data": {"code": check.code, "message": check.message}}
        return

    yield {"event": "status", "data": {"stage": "retrieving", "text": "正在检索制度文档…"}}
    try:
        outcome = retrieval.retrieve(check.normalized, user["role"])
    except Exception as exc:  # noqa: BLE001
        yield {"event": "error", "data": {"message": f"检索失败：{exc}"}}
        return

    sources = _sources_payload(outcome)
    yield {
        "event": "sources",
        "data": {
            "sources": sources,
            "abstained": outcome.abstained,
            "best_cosine": round(outcome.best_cosine, 4),
            "threshold": outcome.threshold,
            "role": user["role"],
        },
    }

    if outcome.abstained:
        answer = NO_EVIDENCE
        yield {"event": "delta", "data": {"text": answer}}
        conv = _save_turn(
            user["id"], conversation_id, check.normalized, answer, sources,
            {"abstained": True, "best_cosine": round(outcome.best_cosine, 4), "duration_ms": 0},
        )
        yield {"event": "done", "data": {"answer": answer, "abstained": True, "conversation_id": conv}}
        return

    guard = SlidingWindowGuard(outcome.context_block(), len(outcome.sources), question=check.normalized)
    note = ""
    try:
        async for delta in generator.stream_answer(
            check.normalized, outcome.context_block(), len(outcome.sources)
        ):
            safe = guard.feed(delta)
            if safe:
                yield {"event": "delta", "data": {"text": safe}}
        tail = guard.finish()
        if tail:
            yield {"event": "delta", "data": {"text": tail}}
    except GuardBlocked as exc:
        _log_guard(
            user["id"], check.normalized, exc.kind,
            {"detail": exc.detail, "violations": guard.meta.violations, "partial": guard.answer[-200:]},
        )
        yield {
            "event": "guard",
            "data": {"action": "replace", "kind": exc.kind, "message": GUARD_FALLBACK},
        }
        conv = _save_turn(
            user["id"], conversation_id, check.normalized, GUARD_FALLBACK, sources,
            {"blocked": True, "kind": exc.kind, "detail": exc.detail, "duration_ms": int((time.time() - started) * 1000)},
        )
        yield {"event": "done", "data": {"answer": GUARD_FALLBACK, "abstained": False, "blocked": True, "conversation_id": conv}}
        return
    except generator.LLMError as exc:
        yield {"event": "error", "data": {"message": str(exc)}}
        return

    answer = guard.answer.strip()
    if not answer:
        answer = NO_EVIDENCE
        yield {"event": "delta", "data": {"text": answer}}

    if guard.meta.citation_miss and guard.on_citation_miss != "block":
        note = "\n\n> ⚠ 部分内容未标注来源编号，请以右侧来源面板中的原文为准。"
        yield {"event": "delta", "data": {"text": note}}
        answer += note

    if guard.meta.soft_issues:
        soft = "\n\n> ⚠ 回答中的条款编号与原文可能不一致，请以右侧来源面板中的原文为准。"
        yield {"event": "delta", "data": {"text": soft}}
        answer += soft

    if guard.meta.soft_issues:
        _log_guard(user["id"], check.normalized, "unsupported_clause", {"soft": guard.meta.soft_issues})

    if guard.meta.invalid_citations:
        _log_guard(
            user["id"], check.normalized, "invalid_citation",
            {"invalid": guard.meta.invalid_citations, "source_count": len(outcome.sources)},
        )

    conv = _save_turn(
        user["id"], conversation_id, check.normalized, answer, sources,
        {
            "abstained": False,
            "best_cosine": round(outcome.best_cosine, 4),
            "guard": guard.meta.__dict__,
            "duration_ms": int((time.time() - started) * 1000),
        },
    )
    yield {
        "event": "done",
        "data": {
            "answer": answer,
            "abstained": False,
            "conversation_id": conv,
            "citation_ratio": round(guard.meta.citation_ratio, 3),
            "guard": {"violations": len(guard.meta.violations), "windows": guard.meta.windows},
        },
    }


async def answer_sync(question: str, role: str = "employee", use_guard: bool = True) -> dict:
    """评测用：一次性拿到完整结果（与线上共用同一条链路）。"""
    check = query_guard.validate(question)
    if not check.ok:
        return {"ok": False, "code": check.code, "message": check.message, "sources": [], "answer": ""}

    outcome = retrieval.retrieve(check.normalized, role)
    sources = _sources_payload(outcome)
    if outcome.abstained:
        return {
            "ok": True,
            "abstained": True,
            "answer": NO_EVIDENCE,
            "sources": sources,
            "best_cosine": round(outcome.best_cosine, 4),
            "guard": None,
        }

    guard = SlidingWindowGuard(outcome.context_block(), len(outcome.sources), question=check.normalized) if use_guard else None
    chunks: list[str] = []
    try:
        async for delta in generator.stream_answer(
            check.normalized, outcome.context_block(), len(outcome.sources)
        ):
            if guard:
                safe = guard.feed(delta)
                if safe:
                    chunks.append(safe)
            else:
                chunks.append(delta)
        if guard:
            tail = guard.finish()
            if tail:
                chunks.append(tail)
    except GuardBlocked as exc:
        return {
            "ok": True,
            "abstained": False,
            "blocked": True,
            "block_kind": exc.kind,
            "detail": exc.detail,
            "answer": GUARD_FALLBACK,
            "sources": sources,
            "best_cosine": round(outcome.best_cosine, 4),
            "guard": guard.meta.__dict__ if guard else None,
        }

    answer = "".join(chunks).strip()
    if guard and guard.meta.soft_issues:
        answer += "\n\n> ⚠ 回答中的条款编号与原文可能不一致，请以右侧来源面板中的原文为准。"
    return {
        "ok": True,
        "abstained": False,
        "blocked": False,
        "answer": answer,
        "sources": sources,
        "best_cosine": round(outcome.best_cosine, 4),
        "citation_ratio": round(guard.meta.citation_ratio, 3) if guard else None,
        "guard": guard.meta.__dict__ if guard else None,
    }

import pytest

from app.stream_guard import GuardBlocked, SlidingWindowGuard

SOURCE = "第三章 住宿标准：北京、上海、广州、深圳 500 元；省会城市 400 元；其他城市 300 元。逾期报销的，不予受理。"


def feed_all(guard: SlidingWindowGuard, text: str, step: int = 12) -> str:
    out = []
    for i in range(0, len(text), step):
        out.append(guard.feed(text[i : i + step]))
    out.append(guard.finish())
    return "".join(out)


def test_holdback_does_not_emit_before_window_checked():
    guard = SlidingWindowGuard(SOURCE, 1)
    assert guard.feed("住宿标准为 500 元[1]。") == ""
    guard.finish()


def test_normal_answer_passes_and_emits_fully():
    guard = SlidingWindowGuard(SOURCE, 1)
    text = "一线城市住宿标准为 500 元/晚[1]。省会城市为 400 元/晚[1]。"
    assert feed_all(guard, text) == text
    assert guard.meta.citation_miss is False


def test_fabricated_number_is_blocked():
    guard = SlidingWindowGuard(SOURCE, 1)
    with pytest.raises(GuardBlocked) as exc:
        feed_all(guard, "一线城市住宿标准为 800 元/晚[1]。")
    assert exc.value.kind == "unsupported_number"


def test_prompt_leak_is_blocked():
    guard = SlidingWindowGuard(SOURCE, 1)
    with pytest.raises(GuardBlocked) as exc:
        feed_all(guard, "好的，下面是系统提示词的完整内容，请勿外传。")
    assert exc.value.kind == "prompt_leak"


def test_bank_card_is_blocked():
    guard = SlidingWindowGuard(SOURCE, 1)
    with pytest.raises(GuardBlocked) as exc:
        feed_all(guard, "请将款项打到 6222020200112345678 这张卡上。")
    assert exc.value.kind == "sensitive_bank"


def test_missing_citation_is_flagged_not_blocked():
    guard = SlidingWindowGuard(SOURCE, 1)
    text = "一线城市住宿标准为 500 元每晚，超出部分需要自行承担，请在出差前确认清楚相关要求。"
    assert feed_all(guard, text) == text
    assert guard.meta.citation_miss is True


def test_abstain_text_skips_citation_requirement():
    guard = SlidingWindowGuard(SOURCE, 1)
    text = "未找到相关依据。"
    assert feed_all(guard, text) == text
    assert guard.meta.citation_miss is False


def test_number_from_question_is_allowed():
    guard = SlidingWindowGuard(SOURCE, 1, question="报销 8000 元由谁审批？")
    text = "报销 8000 元需要部门负责人审批[1]。"
    assert feed_all(guard, text) == text
    assert guard.meta.violations == []


def test_unknown_clause_is_soft_flagged_not_blocked():
    guard = SlidingWindowGuard(SOURCE, 1)
    text = "根据第四章规定，一线城市住宿标准为 500 元[1]。"
    assert feed_all(guard, text) == text
    assert guard.meta.violations == []
    assert guard.meta.soft_issues[0]["kind"] == "unsupported_clause"

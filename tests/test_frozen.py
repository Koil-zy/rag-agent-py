import pytest

from eval import run_eval


def test_frozen_set_hash_matches_lock():
    assert run_eval.verify_frozen() == run_eval.FROZEN_LOCK.read_text(encoding="utf-8").strip()


def test_frozen_set_tamper_is_rejected(tmp_path, monkeypatch):
    fake = tmp_path / "frozen_set.jsonl"
    fake.write_text('{"id":"tampered"}\n', encoding="utf-8")
    monkeypatch.setattr(run_eval, "FROZEN_FILE", fake)
    with pytest.raises(SystemExit) as exc:
        run_eval.verify_frozen()
    assert "冻结集内容已变更" in str(exc.value)

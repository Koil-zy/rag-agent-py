import json

from app import captcha, config, db, pipeline, retrieval, security
from app.retrieval import RetrievalOutcome, Source


def _captcha(client, monkeypatch, which="AAAA"):
    monkeypatch.setattr(captcha, "CHARS", "A")
    data = client.get("/api/auth/captcha").json()
    return data["captcha_id"], "AAAA"


def _outcome() -> RetrievalOutcome:
    return RetrievalOutcome(
        sources=[
            Source(
                index=1,
                chunk_id=1,
                doc_code="TRV-001",
                title="差旅费用管理制度",
                category="差旅",
                acl_level="public",
                heading="第三章 住宿标准",
                content="住宿费标准：北京、上海、广州、深圳 500 元；省会城市及计划单列市 400 元；其他城市 300 元。",
                score=0.91,
                cosine=0.9,
                lexical=0.8,
            )
        ],
        best_cosine=0.9,
        abstained=False,
        threshold=0.46,
    )


def test_register_requires_captcha(client, monkeypatch):
    captcha_id, code = _captcha(client, monkeypatch)
    resp = client.post(
        "/api/auth/register",
        json={
            "phone": "13900001111",
            "password": "Test@1234",
            "confirm_password": "Test@1234",
            "captcha_id": captcha_id,
            "captcha_code": "BBBB",
        },
    )
    assert resp.status_code == 400
    assert "验证码" in resp.json()["detail"]


def test_register_and_login(client, monkeypatch):
    db.execute("DELETE FROM users WHERE phone = '13900001111'")
    captcha_id, _ = _captcha(client, monkeypatch)
    resp = client.post(
        "/api/auth/register",
        json={
            "phone": "13900001111",
            "password": "Test@1234",
            "confirm_password": "Test@1234",
            "captcha_id": captcha_id,
            "captcha_code": "AAAA",
            "display_name": "测试员工",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["user"]["role"] == "employee"
    assert resp.json()["token"]

    captcha_id, _ = _captcha(client, monkeypatch)
    login = client.post(
        "/api/auth/login",
        json={"phone": "13900001111", "password": "Test@1234", "captcha_id": captcha_id, "captcha_code": "AAAA"},
    )
    assert login.status_code == 200
    me = client.get("/api/auth/me", headers={"Authorization": "Bearer " + login.json()["token"]})
    assert me.json()["user"]["phone"] == "13900001111"


def test_leader_register_requires_invite_code(client, monkeypatch):
    db.execute("DELETE FROM users WHERE phone IN ('13900004444', '13900005555')")
    payload = {
        "phone": "13900004444",
        "password": "Test@1234",
        "confirm_password": "Test@1234",
        "display_name": "测试领导",
        "role": "leader",
    }

    captcha_id, _ = _captcha(client, monkeypatch)
    bad = client.post("/api/auth/register", json={**payload, "captcha_id": captcha_id, "captcha_code": "AAAA", "invite_code": "WRONG-CODE"})
    assert bad.status_code == 403
    assert "邀请码" in bad.json()["detail"]

    captcha_id, _ = _captcha(client, monkeypatch)
    missing = client.post("/api/auth/register", json={**payload, "captcha_id": captcha_id, "captcha_code": "AAAA"})
    assert missing.status_code == 403

    code = config.get("registration.leader_invite_codes")[0]
    captcha_id, _ = _captcha(client, monkeypatch)
    ok = client.post(
        "/api/auth/register",
        json={
            "phone": "13900005555",
            "password": "Test@1234",
            "confirm_password": "Test@1234",
            "captcha_id": captcha_id,
            "captcha_code": "AAAA",
            "role": "leader",
            "invite_code": code,
        },
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["user"]["role"] == "leader"


def test_weak_password_rejected(client, monkeypatch):
    captcha_id, _ = _captcha(client, monkeypatch)
    resp = client.post(
        "/api/auth/register",
        json={
            "phone": "13900002222",
            "password": "abc12345",
            "confirm_password": "abc12345",
            "captcha_id": captcha_id,
            "captcha_code": "AAAA",
        },
    )
    assert resp.status_code == 400
    assert "大写字母" in resp.json()["detail"]

    captcha_id, _ = _captcha(client, monkeypatch)
    short = client.post(
        "/api/auth/register",
        json={
            "phone": "13900003333",
            "password": "Ab@1",
            "confirm_password": "Ab@1",
            "captcha_id": captcha_id,
            "captcha_code": "AAAA",
        },
    )
    assert short.status_code == 400
    assert "8 位" in short.json()["detail"]


def test_chat_stream_with_sources(client, employee, monkeypatch):
    monkeypatch.setattr(pipeline.retrieval, "retrieve", lambda q, role: _outcome())

    async def fake_stream(question, context, source_count=0):
        for piece in ["一线城市住宿标准为 500 元/晚[1]。", "省会城市为 400 元/晚[1]。"]:
            yield piece

    monkeypatch.setattr(pipeline.generator, "stream_answer", fake_stream)

    with client.stream(
        "POST",
        "/api/chat/stream",
        json={"question": "出差住宿标准是多少？"},
        headers={"Authorization": "Bearer " + employee["token"]},
    ) as resp:
        assert resp.status_code == 200
        raw = "".join(resp.iter_text())

    events = [line[7:] for line in raw.splitlines() if line.startswith("event: ")]
    assert "sources" in events and "done" in events
    assert "TRV-001" in raw
    assert "500" in raw


def test_chat_guard_blocks_fabricated_number(client, employee, monkeypatch):
    monkeypatch.setattr(pipeline.retrieval, "retrieve", lambda q, role: _outcome())

    async def fake_stream(question, context, source_count=0):
        yield "一线城市住宿标准为 900 元/晚[1]。"

    monkeypatch.setattr(pipeline.generator, "stream_answer", fake_stream)

    with client.stream(
        "POST",
        "/api/chat/stream",
        json={"question": "出差住宿标准是多少？"},
        headers={"Authorization": "Bearer " + employee["token"]},
    ) as resp:
        raw = "".join(resp.iter_text())

    assert "event: guard" in raw
    assert "900" not in raw
    assert "未通过内容合规校验" in raw


def test_abstain_when_no_evidence(client, employee, monkeypatch):
    monkeypatch.setattr(
        pipeline.retrieval,
        "retrieve",
        lambda q, role: RetrievalOutcome(sources=[], best_cosine=0.12, abstained=True, threshold=0.46),
    )
    with client.stream(
        "POST",
        "/api/chat/stream",
        json={"question": "公司食堂周末开放吗？"},
        headers={"Authorization": "Bearer " + employee["token"]},
    ) as resp:
        raw = "".join(resp.iter_text())
    assert "未找到相关依据" in raw
    assert "event: done" in raw


def test_acl_isolation(client, employee, leader):
    assert db.fetch_one("SELECT count(*) AS c FROM documents")["c"] == 7

    emp = retrieval.retrieve("副总裁出差住宿标准是多少？", "employee")
    assert all(s.doc_code != "EXE-001" for s in emp.sources)

    lead = retrieval.retrieve("副总裁出差住宿标准是多少？", "leader")
    assert any(s.doc_code == "EXE-001" for s in lead.sources)
    assert all(s.acl_level in ("public", "guide") for s in lead.sources)


def test_unauthorized_chat(client):
    resp = client.post("/api/chat/stream", json={"question": "年假有几天？"})
    assert resp.status_code == 401


def test_admin_requires_leader(client, employee, leader):
    assert client.post("/api/admin/reindex", headers={"Authorization": "Bearer " + employee["token"]}).status_code == 403
    docs = client.get("/api/documents", headers={"Authorization": "Bearer " + employee["token"]}).json()["documents"]
    assert docs and all(d["acl_level"] == "public" for d in docs)

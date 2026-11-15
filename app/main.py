"""FastAPI 入口：认证、问答 SSE、管理接口与静态页面。"""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import captcha, config, db, generator, ingest, pipeline, retrieval, security

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("app.main")

WEB_DIR = config.get_path("server.static_dir", config.ROOT / "web")

DEMO_ACCOUNTS = [
    ("13800000001", "Boss@2025", "leader", "李总（领导）"),
    ("13800000002", "Staff@2025", "employee", "小王（员工）"),
]


def seed_demo_accounts() -> None:
    for phone, password, role, name in DEMO_ACCOUNTS:
        exists = db.fetch_one("SELECT id FROM users WHERE phone = %s", (phone,))
        if exists:
            continue
        db.execute(
            "INSERT INTO users (phone, password_hash, role, display_name) VALUES (%s, %s, %s, %s)",
            (phone, security.hash_password(password), role, name),
        )
        logger.info("已创建演示账号 %s（%s）", phone, role)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_schema()
    seed_demo_accounts()
    try:
        doc_count = db.fetch_one("SELECT count(*) AS c FROM documents")["c"]
        if doc_count == 0:
            results = await run_in_threadpool(ingest.ingest_all)
            logger.info("首次启动自动入库：%s", results)
    except Exception as exc:  # noqa: BLE001
        logger.warning("自动入库失败（可稍后手动执行 python -m app.ingest）：%s", exc)
    yield


app = FastAPI(title="制度知识助理", version="1.0.0", lifespan=lifespan)


# ---------------- 认证 ----------------
class RegisterIn(BaseModel):
    phone: str
    password: str
    confirm_password: str = ""
    captcha_id: str
    captcha_code: str
    display_name: str = Field(default="", max_length=32)
    role: str = "employee"
    invite_code: str = Field(default="", max_length=64)


class LoginIn(BaseModel):
    phone: str
    password: str
    captcha_id: str
    captcha_code: str


class ChatIn(BaseModel):
    question: str
    conversation_id: int | None = None


def _user_from_token(token: str | None) -> dict:
    payload = security.decode_token(token or "")
    if not payload:
        raise HTTPException(status_code=401, detail="登录已失效，请重新登录")
    user = db.fetch_one(
        "SELECT id, phone, role, display_name, create_time FROM users WHERE id = %s",
        (payload["uid"],),
    )
    if not user:
        raise HTTPException(status_code=401, detail="账号不存在")
    return user


def current_user(
    authorization: str | None = Header(default=None),
    token: str | None = None,
) -> dict:
    raw = authorization.split(" ", 1)[1] if authorization and authorization.lower().startswith("bearer ") else token
    return _user_from_token(raw)


def require_leader(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "leader":
        raise HTTPException(status_code=403, detail="仅领导可访问该功能")
    return user


def _leader_invite_valid(code: str) -> bool:
    """校验领导邀请码；邀请码由管理员在配置文件中维护。"""
    codes = config.get("registration.leader_invite_codes", []) or []
    if isinstance(codes, str):
        codes = [codes]
    allowed = {str(c).strip().lower() for c in codes if str(c).strip()}
    return bool(allowed) and code.strip().lower() in allowed


@app.get("/api/auth/captcha")
def api_captcha() -> dict:
    return captcha.create_captcha_data_url()


@app.post("/api/auth/register")
def api_register(body: RegisterIn) -> dict:
    if not captcha.verify_captcha(body.captcha_id, body.captcha_code):
        raise HTTPException(status_code=400, detail="验证码错误或已过期，请刷新后重试")
    if not security.check_phone(body.phone):
        raise HTTPException(status_code=400, detail="请输入有效的 11 位手机号")
    role = (body.role or "employee").strip().lower()
    if role not in ("employee", "leader"):
        raise HTTPException(status_code=400, detail="注册身份不合法")
    if role == "leader" and not _leader_invite_valid(body.invite_code):
        raise HTTPException(status_code=403, detail="领导邀请码错误或已失效，请联系管理员获取")
    if body.confirm_password and body.confirm_password != body.password:
        raise HTTPException(status_code=400, detail="两次输入的密码不一致")
    pwd = security.check_password(body.password)
    if not pwd.ok:
        raise HTTPException(status_code=400, detail=pwd.message)
    if db.fetch_one("SELECT id FROM users WHERE phone = %s", (body.phone,)):
        raise HTTPException(status_code=409, detail="该手机号已注册，请直接登录")

    prefix = "领导" if role == "leader" else "员工"
    user = db.execute(
        "INSERT INTO users (phone, password_hash, role, display_name) VALUES (%s, %s, %s, %s) "
        "RETURNING id, phone, role, display_name, create_time",
        (
            body.phone,
            security.hash_password(body.password),
            role,
            body.display_name or f"{prefix}{body.phone[-4:]}",
        ),
    )
    logger.info("新用户注册：%s（%s）", body.phone, role)
    return {
        "token": security.create_token(user["id"], user["role"], user["phone"]),
        "user": user,
    }


@app.post("/api/auth/login")
def api_login(body: LoginIn) -> dict:
    blocked = security.login_limiter.blocked(body.phone)
    if blocked:
        raise HTTPException(status_code=429, detail=f"失败次数过多，请 {blocked} 秒后再试")
    if not captcha.verify_captcha(body.captcha_id, body.captcha_code):
        raise HTTPException(status_code=400, detail="验证码错误或已过期，请刷新后重试")
    user = db.fetch_one(
        "SELECT id, phone, role, display_name, password_hash, create_time FROM users WHERE phone = %s",
        (body.phone,),
    )
    if not user or not security.verify_password(body.password, user["password_hash"]):
        security.login_limiter.fail(body.phone)
        raise HTTPException(status_code=401, detail="手机号或密码错误")
    security.login_limiter.reset(body.phone)
    db.execute("UPDATE users SET last_login_at = now() WHERE id = %s", (user["id"],))
    return {
        "token": security.create_token(user["id"], user["role"], user["phone"]),
        "user": {
            "id": user["id"],
            "phone": user["phone"],
            "role": user["role"],
            "display_name": user["display_name"],
            "create_time": user["create_time"],
        },
    }


@app.get("/api/auth/me")
def api_me(user: dict = Depends(current_user)) -> dict:
    return {"user": user}


# ---------------- 问答 ----------------
def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.post("/api/chat/stream")
async def api_chat_stream(body: ChatIn, user: dict = Depends(current_user)):
    async def event_source():
        try:
            async for item in pipeline.stream_chat(body.question, user, body.conversation_id):
                yield _sse(item["event"], item["data"])
        except Exception as exc:  # noqa: BLE001
            logger.exception("问答失败")
            yield _sse("error", {"message": f"服务异常：{exc}"})

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@app.post("/api/chat")
async def api_chat(body: ChatIn, user: dict = Depends(current_user)) -> dict:
    events: list[dict] = []
    async for item in pipeline.stream_chat(body.question, user, body.conversation_id):
        events.append(item)
    final = next((e["data"] for e in events if e["event"] == "done"), None)
    error = next((e["data"] for e in events if e["event"] == "error"), None)
    if error:
        raise HTTPException(status_code=502, detail=error["message"])
    return {"result": final, "events": events}


# ---------------- 知识与运维 ----------------
@app.get("/api/documents")
def api_documents(user: dict = Depends(current_user)) -> dict:
    docs = retrieval.list_documents()
    if user["role"] != "leader":
        docs = [d for d in docs if d["acl_level"] == "public"]
    return {"documents": docs}


@app.get("/api/health")
def api_health() -> dict:
    return {
        "ok": True,
        "models": generator.config_summary(),
        "stats": retrieval.stats(),
        "retrieval": {
            "top_k": config.get("retrieval.top_k"),
            "min_score": config.get("retrieval.min_score"),
        },
        "guard": {
            "window_size": config.get("stream_guard.window_size"),
            "stride": config.get("stream_guard.stride"),
        },
    }


@app.post("/api/admin/reindex")
async def admin_reindex(user: dict = Depends(require_leader)) -> dict:
    results = await run_in_threadpool(ingest.ingest_all, True)
    return {"results": results, "operator": user["phone"]}


@app.get("/api/admin/guard-events")
def admin_guard_events(limit: int = 20, user: dict = Depends(require_leader)) -> dict:
    rows = db.fetch_all(
        "SELECT id, question, kind, detail, created_at FROM guard_events ORDER BY id DESC LIMIT %s",
        (min(limit, 100),),
    )
    for row in rows:
        row["created_at"] = row["created_at"].isoformat(timespec="seconds")
    return {"events": rows}


# ---------------- 页面 ----------------
app.mount("/assets", StaticFiles(directory=WEB_DIR / "assets"), name="assets")


@app.get("/")
def page_index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/login")
def page_login() -> FileResponse:
    return FileResponse(WEB_DIR / "login.html")


@app.get("/admin")
def page_admin() -> FileResponse:
    return FileResponse(WEB_DIR / "admin.html")


@app.exception_handler(404)
async def not_found(request: Request, exc):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "接口不存在"}, status_code=404)
    return FileResponse(WEB_DIR / "index.html")

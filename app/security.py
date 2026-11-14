"""密码策略、口令摘要、签名 token 与登录失败锁。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import threading
import time
from dataclasses import dataclass

from . import config

PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
SPECIAL_CHARS = r"""!@#$%^&*()_+-=[]{}|;:'",.<>/?~`"""


@dataclass
class PasswordCheck:
    ok: bool
    errors: list[str]

    @property
    def message(self) -> str:
        return "；".join(self.errors)


def check_phone(phone: str) -> bool:
    return bool(PHONE_RE.match(phone or ""))


def check_password(password: str) -> PasswordCheck:
    """不少于 8 位，且同时包含大写、小写、数字、特殊符号。"""
    errors: list[str] = []
    pwd = password or ""
    if len(pwd) < 8:
        errors.append("密码长度不能少于 8 位")
    if not re.search(r"[A-Z]", pwd):
        errors.append("需包含大写字母")
    if not re.search(r"[a-z]", pwd):
        errors.append("需包含小写字母")
    if not re.search(r"\d", pwd):
        errors.append("需包含数字")
    if not any(ch in SPECIAL_CHARS for ch in pwd):
        errors.append("需包含特殊符号（如 !@#$%^&*）")
    if re.search(r"\s", pwd):
        errors.append("不能包含空格")
    return PasswordCheck(ok=not errors, errors=errors)


def hash_password(password: str, salt: str | None = None) -> str:
    iterations = int(config.get("security.pbkdf2_iterations", 200000))
    salt = salt or base64.urlsafe_b64encode(hashlib.sha256(f"{time.time()}{password}".encode()).digest()[:16]).decode()
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations)
    return f"pbkdf2_sha256${iterations}${salt}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iterations, salt, digest_b64 = stored.split("$", 3)
        calc = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iterations))
        return hmac.compare_digest(base64.b64encode(calc).decode(), digest_b64)
    except (ValueError, TypeError):
        return False


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64d(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def create_token(user_id: int, role: str, phone: str) -> str:
    ttl = int(config.get("security.token_ttl_minutes", 720))
    payload = {
        "uid": user_id,
        "role": role,
        "phone": phone,
        "exp": int(time.time()) + ttl * 60,
    }
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(config.TOKEN_SECRET.encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64e(sig)}"


def decode_token(token: str) -> dict | None:
    try:
        body, sig = token.split(".", 1)
        expected = hmac.new(config.TOKEN_SECRET.encode(), body.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(_b64e(expected), sig):
            return None
        payload = json.loads(_b64d(body))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return payload
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


class FailureLimiter:
    """登录失败次数限制：连续失败 N 次后锁定一段时间。"""

    def __init__(self) -> None:
        self._items: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def blocked(self, key: str) -> int:
        """返回剩余锁定秒数，0 表示未锁定。"""
        with self._lock:
            count, deadline = self._items.get(key, (0, 0.0))
            remain = int(deadline - time.time())
            return remain if remain > 0 else 0

    def fail(self, key: str) -> None:
        limit = int(config.get("security.login_max_failures", 5))
        lock_seconds = int(config.get("security.login_lock_minutes", 5)) * 60
        with self._lock:
            count, _ = self._items.get(key, (0, 0.0))
            count += 1
            deadline = time.time() + lock_seconds if count >= limit else 0.0
            self._items[key] = (0 if count >= limit else count, deadline)

    def reset(self, key: str) -> None:
        with self._lock:
            self._items.pop(key, None)


login_limiter = FailureLimiter()

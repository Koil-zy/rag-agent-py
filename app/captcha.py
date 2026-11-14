"""图形验证码：Pillow 生成 PNG，内存存储、一次性、带过期时间。"""
from __future__ import annotations

import base64
import hashlib
import io
import random
import threading
import time
import uuid

from PIL import Image, ImageDraw, ImageFont

from . import config

CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 去掉 I O 0 1 等易混淆字符

_lock = threading.Lock()
_store: dict[str, tuple[str, float]] = {}


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for name in ("arial.ttf", "segoeui.ttf", "simhei.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _render(code: str) -> bytes:
    width, height = 120, 44
    img = Image.new("RGB", (width, height), (241, 242, 243))
    draw = ImageDraw.Draw(img)

    # 背景噪点与干扰线，保持 B 站浅灰/粉色系
    for _ in range(180):
        draw.point((random.randint(0, width), random.randint(0, height)),
                   fill=random.choice([(255, 102, 153), (0, 174, 236), (200, 200, 205)]))
    for _ in range(4):
        draw.line(
            [(random.randint(0, width), random.randint(0, height)),
             (random.randint(0, width), random.randint(0, height))],
            fill=(random.randint(150, 220), random.randint(150, 220), random.randint(150, 220)),
            width=1,
        )

    font = _font(26)
    step = width // (len(code) + 1)
    for i, ch in enumerate(code):
        layer = Image.new("RGBA", (40, 44), (255, 255, 255, 0))
        ld = ImageDraw.Draw(layer)
        color = random.choice([(24, 25, 28), (255, 102, 153), (0, 110, 160)])
        ld.text((6, 4), ch, font=font, fill=color)
        layer = layer.rotate(random.randint(-22, 22), expand=False)
        img.paste(layer, (6 + i * step, random.randint(-2, 4)), layer)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def create_captcha() -> tuple[str, bytes]:
    length = int(config.get("captcha.length", 4))
    code = "".join(random.choice(CHARS) for _ in range(length))
    captcha_id = uuid.uuid4().hex
    ttl = float(config.get("captcha.ttl_seconds", 120))
    with _lock:
        _cleanup_locked()
        _store[captcha_id] = (hashlib.sha256(code.upper().encode()).hexdigest(), time.time() + ttl)
    return captcha_id, _render(code)


def verify_captcha(captcha_id: str, code: str) -> bool:
    if not captcha_id or not code:
        return False
    with _lock:
        _cleanup_locked()
        item = _store.pop(captcha_id, None)  # 一次性，无论对错都失效
    if not item:
        return False
    digest, deadline = item
    if time.time() > deadline:
        return False
    return hashlib.sha256(code.strip().upper().encode()).hexdigest() == digest


def _cleanup_locked() -> None:
    now = time.time()
    for key in [k for k, (_, exp) in _store.items() if exp < now]:
        _store.pop(key, None)


def create_captcha_data_url() -> dict[str, str]:
    captcha_id, png = create_captcha()
    return {
        "captcha_id": captcha_id,
        "image": "data:image/png;base64," + base64.b64encode(png).decode(),
    }

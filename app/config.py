"""配置加载：config.yaml + 环境变量覆盖，避免把模型/密钥写死在代码里。"""
from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("APP_CONFIG", ROOT / "config.yaml"))

with CONFIG_PATH.open("r", encoding="utf-8") as f:
    _raw: dict[str, Any] = yaml.safe_load(f)


def get(path: str, default: Any = None) -> Any:
    """按点路径读取配置，例如 get("embedding.model")。"""
    node: Any = _raw
    for key in path.split("."):
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def get_path(path: str, default: Path) -> Path:
    value = get(path)
    if not value:
        return default
    p = Path(value)
    return p if p.is_absolute() else ROOT / p


def env_override(path: str, env_name: str) -> Any:
    return os.environ.get(env_name) or get(path)


def _load_secret() -> str:
    secret = os.environ.get("SECRET_KEY") or get("security.token_secret")
    if secret:
        return str(secret)
    key_file = ROOT / ".data" / "secret.key"
    if key_file.exists():
        return key_file.read_text(encoding="utf-8").strip()
    key_file.parent.mkdir(parents=True, exist_ok=True)
    new_secret = secrets.token_urlsafe(48)
    key_file.write_text(new_secret, encoding="utf-8")
    return new_secret


TOKEN_SECRET = _load_secret()


def reload_config() -> None:
    """测试或运行时热加载配置文件。"""
    global _raw
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        _raw = yaml.safe_load(f)

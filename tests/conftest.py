"""测试环境：独立 pgdata + 本地哈希向量，避免依赖 Ollama 与污染演示库。"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / ".data"
DATA_DIR.mkdir(exist_ok=True)

_cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
_cfg["database"]["embedded_data_dir"] = str(DATA_DIR / "test_pgdata")
_cfg["database"]["embedded_port"] = 55433
_cfg["embedding"]["provider"] = "local_hash"
_cfg["embedding"]["model"] = "test-local-hash"
_cfg["llm"]["provider"] = "ollama"
_cfg["chunking"]["chunk_size"] = 320
_cfg["chunking"]["chunk_overlap"] = 40

TEST_CONFIG = DATA_DIR / "test_config.yaml"
TEST_CONFIG.write_text(yaml.safe_dump(_cfg, allow_unicode=True), encoding="utf-8")
os.environ["APP_CONFIG"] = str(TEST_CONFIG)
os.environ["SECRET_KEY"] = "unit-test-secret"

from fastapi.testclient import TestClient  # noqa: E402

from app import db, main, security  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(main.app) as c:
        yield c


@pytest.fixture(scope="session")
def employee(client):
    row = db.fetch_one("SELECT id, phone, role FROM users WHERE phone = '13800000002'")
    return {"id": row["id"], "phone": row["phone"], "role": row["role"],
            "token": security.create_token(row["id"], row["role"], row["phone"])}


@pytest.fixture(scope="session")
def leader(client):
    row = db.fetch_one("SELECT id, phone, role FROM users WHERE phone = '13800000001'")
    return {"id": row["id"], "phone": row["phone"], "role": row["role"],
            "token": security.create_token(row["id"], row["role"], row["phone"])}

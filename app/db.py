"""数据库连接与表结构初始化（PostgreSQL + pgvector）。

本地演示：未配置 database.url 时用 pgserver 内嵌拉起 PostgreSQL 16 + pgvector。
线上：直接填 DATABASE_URL / config.database.url，代码零改动。
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row

from . import config

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_embedded_url: str | None = None
_schema_ready = False

DEFAULT_EMBEDDED_PORT = 55432


def _pg_bin(name: str) -> Path:
    import pgserver._commands as pc

    suffix = ".exe" if os.name == "nt" else ""
    return pc.POSTGRES_BIN_PATH / f"{name}{suffix}"


def _read_embedded_port(pgdata: Path) -> int | None:
    pid_file = pgdata / "postmaster.pid"
    if not pid_file.exists():
        return None
    try:
        lines = pid_file.read_text(encoding="utf-8").splitlines()
        return int(lines[3])
    except (ValueError, IndexError):
        return None


def _start_embedded(pgdata: Path) -> str:
    """内嵌 PostgreSQL：initdb + pg_ctl 启动。

    日志写到 pgdata 之外，避免 Windows 下 crash recovery 时 pg_ctl 与 postgres
    争抢 pgdata/log 句柄导致启动超时。
    """
    pgdata.mkdir(parents=True, exist_ok=True)
    if not (pgdata / "PG_VERSION").exists():
        logger.info("初始化内嵌 PostgreSQL：%s", pgdata)
        subprocess.run(
            [
                str(_pg_bin("initdb")),
                "-D", str(pgdata),
                "--auth=trust", "--auth-local=trust",
                "--encoding=utf8", "-U", "postgres",
            ],
            check=True,
        )
    port = int(config.get("database.embedded_port", DEFAULT_EMBEDDED_PORT))
    # 日志文件名带上数据目录名，避免主库与测试库（test_pgdata）共用同一个
    # 日志文件时，Windows 下打开句柄冲突导致 pg_ctl 启动失败。
    log_path = pgdata.parent / f"{pgdata.name}.log"
    status = subprocess.run(
        [str(_pg_bin("pg_ctl")), "-D", str(pgdata), "status"], capture_output=True
    )
    if status.returncode != 0:
        logger.info("启动内嵌 PostgreSQL：%s (port=%s)", pgdata, port)
        subprocess.run(
            [
                str(_pg_bin("pg_ctl")), "-D", str(pgdata),
                "-o", f'-h 127.0.0.1 -p {port}',
                "-l", str(log_path), "-w", "-t", "60", "start",
            ],
            check=True,
        )
    else:
        port = _read_embedded_port(pgdata) or port
        logger.info("内嵌 PostgreSQL 已在运行：%s (port=%s)", pgdata, port)
    return f"postgresql://postgres@127.0.0.1:{port}/postgres"


def _stop_embedded() -> None:
    pgdata = config.get_path("database.embedded_data_dir", config.ROOT / ".data" / "pgdata")
    try:
        subprocess.run(
            [str(_pg_bin("pg_ctl")), "-D", str(pgdata), "-m", "fast", "-w", "-t", "20", "stop"],
            capture_output=True,
        )
    except Exception:  # noqa: BLE001
        pass


def _ensure_embedded() -> str:
    """确保内嵌 PostgreSQL 在运行（被其它进程误停后自动拉起）。"""
    global _embedded_url
    external = os.environ.get("DATABASE_URL") or config.get("database.url")
    if external:
        return str(external)
    with _lock:
        pgdata = config.get_path("database.embedded_data_dir", config.ROOT / ".data" / "pgdata")
        _embedded_url = _start_embedded(pgdata)
        return _embedded_url


def database_url() -> str:
    global _embedded_url
    url = os.environ.get("DATABASE_URL") or config.get("database.url")
    if url:
        return str(url)
    if _embedded_url is None:
        return _ensure_embedded()
    return _embedded_url


@contextmanager
def connect() -> Iterator[psycopg.Connection]:
    url = database_url()
    try:
        conn = psycopg.connect(url, row_factory=dict_row, autocommit=True, connect_timeout=5)
    except psycopg.OperationalError:
        logger.warning("数据库连接失败，尝试重新拉起内嵌 PostgreSQL")
        conn = psycopg.connect(
            _ensure_embedded(), row_factory=dict_row, autocommit=True, connect_timeout=15
        )
    try:
        with conn:
            yield conn
    finally:
        conn.close()


SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS users (
    id            BIGSERIAL PRIMARY KEY,
    phone         VARCHAR(20) NOT NULL UNIQUE,
    password_hash TEXT        NOT NULL,
    role          VARCHAR(16) NOT NULL DEFAULT 'employee',
    display_name  VARCHAR(64),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ,
    CONSTRAINT users_role_check CHECK (role IN ('employee', 'leader'))
);

CREATE TABLE IF NOT EXISTS documents (
    id             BIGSERIAL PRIMARY KEY,
    code           VARCHAR(64) NOT NULL UNIQUE,
    title          TEXT        NOT NULL,
    category       VARCHAR(32) NOT NULL,
    acl_level      VARCHAR(16) NOT NULL,
    file_name      TEXT        NOT NULL,
    checksum       CHAR(64)    NOT NULL,
    effective_date DATE,
    chunk_count    INT         NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT documents_acl_check CHECK (acl_level IN ('public', 'guide'))
);

CREATE TABLE IF NOT EXISTS chunks (
    id          BIGSERIAL PRIMARY KEY,
    document_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    seq         INT    NOT NULL,
    heading     TEXT,
    content     TEXT   NOT NULL,
    embedding   vector({dim}) NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (document_id, seq)
);

CREATE INDEX IF NOT EXISTS chunks_embedding_idx
    ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS chunks_document_idx ON chunks (document_id);

CREATE TABLE IF NOT EXISTS conversations (
    id         BIGSERIAL PRIMARY KEY,
    user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title      TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS messages (
    id              BIGSERIAL PRIMARY KEY,
    conversation_id BIGINT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            VARCHAR(16) NOT NULL,
    content         TEXT NOT NULL,
    sources         JSONB,
    meta            JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS messages_conversation_idx ON messages (conversation_id);

CREATE TABLE IF NOT EXISTS guard_events (
    id         BIGSERIAL PRIMARY KEY,
    user_id    BIGINT,
    question   TEXT,
    kind       VARCHAR(32) NOT NULL,
    detail     JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS guard_events_created_idx ON guard_events (created_at DESC);
"""


def init_schema(force: bool = False) -> None:
    """幂等建表；force=True 时重新检测（测试用）。"""
    global _schema_ready
    if _schema_ready and not force:
        return
    dim = int(config.get("embedding.dimension", 1024))
    with connect() as conn:
        conn.execute(SCHEMA_SQL.replace("{dim}", str(dim)))
        # 维度被修改时给出明确告警，避免上线换模型后静默失败
        row = conn.execute(
            "SELECT atttypmod FROM pg_attribute WHERE attrelid = 'chunks'::regclass AND attname = 'embedding'"
        ).fetchone()
        if row and row["atttypmod"] != dim:
            logger.warning(
                "chunks.embedding 维度为 %s，与配置 embedding.dimension=%s 不一致，请重新入库",
                row["atttypmod"],
                dim,
            )
    _schema_ready = True


def fetch_one(sql: str, params: tuple | dict | None = None) -> dict[str, Any] | None:
    with connect() as conn:
        return conn.execute(sql, params).fetchone()


def fetch_all(sql: str, params: tuple | dict | None = None) -> list[dict[str, Any]]:
    with connect() as conn:
        return conn.execute(sql, params).fetchall()


def execute(sql: str, params: tuple | dict | None = None) -> dict[str, Any] | None:
    with connect() as conn:
        cursor = conn.execute(sql, params)
        return cursor.fetchone() if cursor.description else None


if __name__ == "__main__":
    # 显式停止内嵌 PostgreSQL：python -m app.db
    _stop_embedded()
    print("已停止内嵌 PostgreSQL")

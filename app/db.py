"""数据库连接与表结构初始化（PostgreSQL + pgvector）。

连接串来源优先级：环境变量 DATABASE_URL > config.yaml 的 database.url。
本地与线上都连接同一套 PostgreSQL 服务，需提前安装 pgvector 扩展。
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row

from . import config

logger = logging.getLogger(__name__)

_schema_ready = False


def database_url() -> str:
    url = os.environ.get("DATABASE_URL") or config.get("database.url")
    if not url:
        raise RuntimeError(
            "未配置数据库连接：请设置环境变量 DATABASE_URL 或 config.yaml 的 database.url"
        )
    return str(url)


@contextmanager
def connect() -> Iterator[psycopg.Connection]:
    conn = psycopg.connect(
        database_url(), row_factory=dict_row, autocommit=True, connect_timeout=5
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
    create_time   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ,
    CONSTRAINT users_role_check CHECK (role IN ('employee', 'leader'))
);

-- 兼容旧库：users.created_at 重命名为 create_time
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = 'users' AND column_name = 'created_at'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = 'users' AND column_name = 'create_time'
    ) THEN
        ALTER TABLE users RENAME COLUMN created_at TO create_time;
    END IF;
END $$;

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

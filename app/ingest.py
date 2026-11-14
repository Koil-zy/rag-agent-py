"""制度文档切片入库：Markdown -> 章节切片 -> 向量 -> PostgreSQL(pgvector)。"""
from __future__ import annotations

import hashlib
import logging
from datetime import date
from pathlib import Path

import yaml
from langchain_text_splitters import RecursiveCharacterTextSplitter

from . import config, db, embeddings

logger = logging.getLogger(__name__)

DOCS_DIR = config.ROOT / "docs"
MANIFEST = DOCS_DIR / "manifest.yaml"

_splitter: RecursiveCharacterTextSplitter | None = None


def _get_splitter() -> RecursiveCharacterTextSplitter:
    global _splitter
    if _splitter is None:
        _splitter = RecursiveCharacterTextSplitter(
            chunk_size=int(config.get("chunking.chunk_size", 500)),
            chunk_overlap=int(config.get("chunking.chunk_overlap", 80)),
            separators=["\n## ", "\n### ", "\n\n", "\n", "。", "；", "，", " "],
            keep_separator=True,
        )
    return _splitter


def load_manifest() -> list[dict]:
    with MANIFEST.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("documents", [])


def _sections(markdown: str) -> list[tuple[str, str]]:
    """按 ## 标题切章节，返回 [(heading, body)]。"""
    sections: list[tuple[str, str]] = []
    heading = "前言"
    body: list[str] = []
    for line in markdown.splitlines():
        if line.startswith("## "):
            if "\n".join(body).strip():
                sections.append((heading, "\n".join(body).strip()))
            heading = line[3:].strip()
            body = []
        elif line.startswith("# "):
            continue
        else:
            body.append(line)
    if "\n".join(body).strip():
        sections.append((heading, "\n".join(body).strip()))
    return sections


def split_document(title: str, markdown: str) -> list[dict]:
    min_chars = int(config.get("chunking.min_chunk_chars", 30))
    chunks: list[dict] = []
    for heading, body in _sections(markdown):
        for piece in _get_splitter().split_text(body):
            piece = piece.strip()
            if len(piece) < min_chars:
                continue
            chunks.append(
                {
                    "heading": heading,
                    "content": piece,
                    "embed_text": f"《{title}》{heading}\n{piece}",
                }
            )
    return chunks


def ingest_document(meta: dict, force: bool = False) -> dict:
    path = DOCS_DIR / meta["file"]
    raw = path.read_bytes()
    checksum = hashlib.sha256(raw).hexdigest()
    markdown = raw.decode("utf-8")

    existing = db.fetch_one("SELECT id, checksum FROM documents WHERE code = %s", (meta["code"],))
    if existing and existing["checksum"] == checksum and not force:
        return {"code": meta["code"], "status": "skipped", "chunks": 0}

    chunks = split_document(meta["title"], markdown)
    if not chunks:
        return {"code": meta["code"], "status": "empty", "chunks": 0}

    vectors = embeddings.embed_texts([c["embed_text"] for c in chunks])
    effective = meta.get("effective")
    effective_date = date.fromisoformat(str(effective)) if effective else None

    with db.connect() as conn, conn.transaction():
        row = conn.execute(
            """
            INSERT INTO documents (code, title, category, acl_level, file_name, checksum, effective_date, chunk_count)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (code) DO UPDATE SET
                title = EXCLUDED.title,
                category = EXCLUDED.category,
                acl_level = EXCLUDED.acl_level,
                file_name = EXCLUDED.file_name,
                checksum = EXCLUDED.checksum,
                effective_date = EXCLUDED.effective_date,
                chunk_count = EXCLUDED.chunk_count
            RETURNING id
            """,
            (
                meta["code"],
                meta["title"],
                meta["category"],
                meta["acl"],
                meta["file"],
                checksum,
                effective_date,
                len(chunks),
            ),
        ).fetchone()
        doc_id = row["id"]
        conn.execute("DELETE FROM chunks WHERE document_id = %s", (doc_id,))
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO chunks (document_id, seq, heading, content, embedding) VALUES (%s, %s, %s, %s, %s::vector)",
                [
                    (doc_id, i, c["heading"], c["content"], embeddings.vector_literal(v))
                    for i, (c, v) in enumerate(zip(chunks, vectors))
                ],
            )
    logger.info("入库完成：%s %s (%d 切片)", meta["code"], meta["title"], len(chunks))
    return {"code": meta["code"], "status": "ingested", "chunks": len(chunks)}


def ingest_all(force: bool = False) -> list[dict]:
    db.init_schema()
    results = [ingest_document(meta, force=force) for meta in load_manifest()]
    return results


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    force_flag = "--force" in sys.argv
    for item in ingest_all(force=force_flag):
        print(item)

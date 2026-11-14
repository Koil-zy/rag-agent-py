"""检索层：pgvector 余弦召回 + 中文字符 n-gram 词法加权 + ACL 强制过滤。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import config, db, embeddings

ROLE_ACL = {"employee": ["public"], "leader": ["public", "guide"]}
_STOP_PAIRS = {"的是", "多少", "什么", "怎么", "可以", "请问", "我们", "公司", "标准"}


def allowed_levels(role: str) -> list[str]:
    return ROLE_ACL.get(role, ["public"])


def _grams(text: str) -> set[str]:
    text = re.sub(r"[\s，。！？、；：,.!?;:（）()【】\[\]《》\"'“”‘’]", "", text.lower())
    grams = {text[i : i + 2] for i in range(len(text) - 1)}
    return {g for g in grams if g.strip() and g not in _STOP_PAIRS}


def lexical_score(query: str, content: str) -> float:
    q = _grams(query)
    if not q:
        return 0.0
    hit = len(q & _grams(content))
    return hit / len(q)


@dataclass
class Source:
    index: int
    chunk_id: int
    doc_code: str
    title: str
    category: str
    acl_level: str
    heading: str
    content: str
    score: float
    cosine: float
    lexical: float

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "chunk_id": self.chunk_id,
            "doc_code": self.doc_code,
            "title": self.title,
            "category": self.category,
            "acl_level": self.acl_level,
            "heading": self.heading,
            "content": self.content,
            "score": round(self.score, 4),
            "cosine": round(self.cosine, 4),
            "lexical": round(self.lexical, 4),
        }


@dataclass
class RetrievalOutcome:
    sources: list[Source] = field(default_factory=list)
    best_cosine: float = 0.0
    abstained: bool = True
    threshold: float = 0.0

    def context_block(self) -> str:
        blocks = []
        for s in self.sources:
            blocks.append(
                f"[{s.index}] 《{s.title}》（{s.doc_code} · {s.heading}）\n{s.content}"
            )
        return "\n\n".join(blocks)


def retrieve(question: str, role: str) -> RetrievalOutcome:
    top_k = int(config.get("retrieval.top_k", 5))
    candidate_k = int(config.get("retrieval.candidate_k", 20))
    min_score = float(config.get("retrieval.min_score", 0.46))
    weight = float(config.get("retrieval.lexical_weight", 0.28))
    max_sources = int(config.get("retrieval.max_sources", 4))
    levels = allowed_levels(role)

    qvec = embeddings.embed_query(question)
    literal = embeddings.vector_literal(qvec)
    rows = db.fetch_all(
        """
        SELECT c.id, c.heading, c.content, d.code, d.title, d.category, d.acl_level,
               1 - (c.embedding <=> %s::vector) AS cosine
        FROM chunks c
        JOIN documents d ON d.id = c.document_id
        WHERE d.acl_level = ANY(%s)
        ORDER BY c.embedding <=> %s::vector
        LIMIT %s
        """,
        (literal, levels, literal, candidate_k),
    )

    scored: list[Source] = []
    for row in rows:
        lex = lexical_score(question, f"{row['title']}{row['heading']}{row['content']}")
        cosine = float(row["cosine"])
        scored.append(
            Source(
                index=0,
                chunk_id=row["id"],
                doc_code=row["code"],
                title=row["title"],
                category=row["category"],
                acl_level=row["acl_level"],
                heading=row["heading"] or "",
                content=row["content"],
                score=(1 - weight) * cosine + weight * lex,
                cosine=cosine,
                lexical=lex,
            )
        )
    scored.sort(key=lambda s: s.score, reverse=True)

    picked: list[Source] = []
    per_doc: dict[str, int] = {}
    for item in scored:
        if per_doc.get(item.doc_code, 0) >= 2:
            continue
        per_doc[item.doc_code] = per_doc.get(item.doc_code, 0) + 1
        picked.append(item)
        if len(picked) >= min(max_sources, top_k):
            break

    for i, item in enumerate(picked, start=1):
        item.index = i

    best_cosine = max((s.cosine for s in picked), default=0.0)
    return RetrievalOutcome(
        sources=picked,
        best_cosine=best_cosine,
        abstained=best_cosine < min_score,
        threshold=min_score,
    )


def list_documents() -> list[dict]:
    rows = db.fetch_all(
        """
        SELECT code, title, category, acl_level, chunk_count, effective_date, created_at
        FROM documents ORDER BY category, code
        """
    )
    for row in rows:
        row["effective_date"] = row["effective_date"].isoformat() if row["effective_date"] else None
        row["created_at"] = row["created_at"].isoformat(timespec="seconds")
    return rows


def stats() -> dict:
    docs = db.fetch_one("SELECT count(*) AS c FROM documents")
    chunks = db.fetch_one("SELECT count(*) AS c FROM chunks")
    users = db.fetch_one("SELECT count(*) AS c FROM users")
    return {
        "documents": docs["c"] if docs else 0,
        "chunks": chunks["c"] if chunks else 0,
        "users": users["c"] if users else 0,
    }

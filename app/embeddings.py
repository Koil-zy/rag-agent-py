"""向量模型适配层：ollama / OpenAI 兼容 / 本地哈希，全部由配置文件决定。"""
from __future__ import annotations

import hashlib
import re
import threading

import httpx
import numpy as np

from . import config


class EmbeddingError(RuntimeError):
    """向量服务不可用时抛出，便于上层给出可读提示。"""


_CACHE: dict[str, list[float]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 512


def provider() -> str:
    return str(config.get("embedding.provider", "ollama")).lower()


def dimension() -> int:
    return int(config.get("embedding.dimension", 1024))


def _http_json(url: str, payload: dict, headers: dict | None = None) -> dict:
    timeout = float(config.get("embedding.timeout", 90))
    try:
        resp = httpx.post(url, json=payload, headers=headers or {}, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as exc:
        raise EmbeddingError(f"向量服务返回 {exc.response.status_code}：{exc.response.text[:200]}") from exc
    except httpx.HTTPError as exc:
        raise EmbeddingError(f"无法连接向量服务 {url}：{exc}") from exc


def _embed_ollama(texts: list[str]) -> list[list[float]]:
    base = str(config.get("embedding.base_url", "http://127.0.0.1:11434")).rstrip("/")
    model = config.get("embedding.model", "bge-m3")
    data = _http_json(f"{base}/api/embed", {"model": model, "input": texts, "truncate": True})
    vectors = data.get("embeddings")
    if not vectors:
        raise EmbeddingError("Ollama 未返回 embeddings，请确认向量模型已 pull")
    return [list(map(float, v)) for v in vectors]


def _embed_openai(texts: list[str]) -> list[list[float]]:
    base = str(config.get("embedding.base_url", "https://api.openai.com/v1")).rstrip("/")
    key = config.get("embedding.api_key", "")
    data = _http_json(
        f"{base}/embeddings",
        {"model": config.get("embedding.model"), "input": texts},
        {"Authorization": f"Bearer {key}"},
    )
    items = data.get("data") or []
    if len(items) != len(texts):
        raise EmbeddingError("OpenAI 兼容接口返回数量与请求不一致")
    return [list(map(float, item["embedding"])) for item in items]


def _tokenize(text: str) -> list[tuple[str, float]]:
    text = re.sub(r"\s+", " ", text.lower())
    tokens: list[tuple[str, float]] = []
    for i, ch in enumerate(text):
        if ch.isalnum() or "\u4e00" <= ch <= "\u9fff":
            tokens.append((ch, 0.5))
        if i + 1 < len(text):
            pair = text[i : i + 2]
            if not pair.isspace():
                tokens.append((pair, 1.0))
    return tokens


def _embed_local_hash(texts: list[str]) -> list[list[float]]:
    """零依赖兜底方案：字符 n-gram 哈希 + L2 归一化，仅用于离线演示。"""
    dim = dimension()
    vectors: list[list[float]] = []
    for text in texts:
        vec = np.zeros(dim, dtype=np.float32)
        for token, weight in _tokenize(text):
            digest = hashlib.md5(token.encode()).digest()
            idx = int.from_bytes(digest[:4], "little") % dim
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vec[idx] += sign * weight
        norm = float(np.linalg.norm(vec))
        vectors.append((vec / norm).tolist() if norm else vec.tolist())
    return vectors


def embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    batch_size = int(config.get("embedding.batch_size", 8))
    out: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        if provider() == "ollama":
            vectors = _embed_ollama(batch)
        elif provider() == "openai":
            vectors = _embed_openai(batch)
        elif provider() == "local_hash":
            vectors = _embed_local_hash(batch)
        else:
            raise EmbeddingError(f"未知的 embedding.provider：{provider()}")
        expected = dimension()
        for vec in vectors:
            if len(vec) != expected:
                raise EmbeddingError(
                    f"向量维度 {len(vec)} 与配置 embedding.dimension={expected} 不一致，请修改配置后重新入库"
                )
        out.extend(vectors)
    return out


def embed_query(text: str) -> list[float]:
    key = f"{provider()}::{config.get('embedding.model')}::{text}"
    with _CACHE_LOCK:
        if key in _CACHE:
            return _CACHE[key]
    vector = embed_texts([text])[0]
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.clear()
        _CACHE[key] = vector
    return vector


def vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{x:.7f}" for x in vector) + "]"


def health() -> dict:
    """用于前端展示向量服务状态，失败不抛异常。"""
    try:
        vec = embed_texts(["健康检查"])
        return {"ok": True, "provider": provider(), "model": config.get("embedding.model"), "dimension": len(vec[0])}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "provider": provider(), "model": config.get("embedding.model"), "error": str(exc)}

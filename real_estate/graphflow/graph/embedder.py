"""로컬 임베딩 — sentence-transformers 기반.

multilingual-e5-large: 한국어 품질 양호 + 1024-dim dense, 이미 로컬 캐시됨.
bge-m3로 swap하려면 _MODEL_NAME만 교체 (동일 1024-dim). 싱글톤으로 프로세스
1회만 로드 (첫 호출 ~5초 + ~1.5GB 메모리).
"""

from __future__ import annotations

import logging
import threading
from typing import Iterable

logger = logging.getLogger("graphflow.embed")

_MODEL_NAME = "intfloat/multilingual-e5-large"
_DIM = 1024

_model = None
_lock = threading.Lock()


def get_embedder():
    """SentenceTransformer 인스턴스 (지연 로드)."""
    global _model
    if _model is not None:
        return _model
    with _lock:
        if _model is not None:
            return _model
        from sentence_transformers import SentenceTransformer
        logger.info("Loading embedding model: %s (한 번만)", _MODEL_NAME)
        _model = SentenceTransformer(_MODEL_NAME)
    return _model


def embed_texts(texts: Iterable[str]) -> list[list[float]]:
    """배치 임베딩. 비어있는 문자열은 공백 1자로 대체해 0-division 회피."""
    items = list(texts)
    model = get_embedder()
    nonempty = [t if t else " " for t in items]
    return model.encode(nonempty, normalize_embeddings=True).tolist()


def embed_one(text: str) -> list[float]:
    return embed_texts([text])[0]


def dim() -> int:
    return _DIM

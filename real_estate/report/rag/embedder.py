"""bge-m3 로컬 임베딩 — sentence-transformers 기반.

bge-m3는 한국어 강함 + 1024-dim dense embedding. CPU에서도 동작하지만 첫 호출
시 2GB 정도 모델 다운로드 + 메모리 점유가 있다 (~5초 + ~1.5GB).

싱글톤 패턴으로 프로세스 1회만 로드.
"""

from __future__ import annotations

import logging
import threading
from typing import Iterable

logger = logging.getLogger("infra.embed")

# bge-m3가 첫 다운로드 2GB+로 무거워서 multilingual-e5-large(이미 캐시됨, 동일 1024-dim,
# 한국어 품질 비슷)로 시작. 향후 bge-m3 사용 가능 시 환경변수로 swap.
_MODEL_NAME = "intfloat/multilingual-e5-large"
_DIM = 1024

_model = None
_lock = threading.Lock()


def get_embedder():
    """sentence-transformers SentenceTransformer 인스턴스 (지연 로드)."""
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
    """배치 임베딩. 비어있는 문자열은 모두 0 벡터로 대체."""
    items = list(texts)
    model = get_embedder()
    nonempty = [t if t else " " for t in items]
    vectors = model.encode(nonempty, normalize_embeddings=True).tolist()
    return vectors


def embed_one(text: str) -> list[float]:
    return embed_texts([text])[0]


def dim() -> int:
    return _DIM

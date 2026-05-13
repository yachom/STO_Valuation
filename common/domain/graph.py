"""Asset-agnostic graph primitives.

The graph stores the union of (a) batch-derived signals and (b) on-demand
analysis results, joined by shared EvaluationFactor / ontology nodes. Asset
specific node/edge label catalogs live in ``projects/<asset>/domain/``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class RelationType(str):
    """Base relation label. Asset-specific labels extend this via plain strings
    (e.g. "LOCATED_IN", "LEASED_BY"). A string subclass keeps the type open."""


class GraphNode(BaseModel):
    node_id: str
    labels: list[str] = Field(default_factory=list)
    properties: dict[str, Any] = Field(default_factory=dict)
    scope_id: str | None = None
    created_at: datetime | None = None


class GraphEdge(BaseModel):
    edge_id: str
    src: str
    dst: str
    relation: str
    properties: dict[str, Any] = Field(default_factory=dict)
    scope_id: str | None = None
    created_at: datetime | None = None

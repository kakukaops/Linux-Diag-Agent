"""RetrievalQuery and Evidence schema (WBS 3.10).

All retrieval routes consume a RetrievalQuery and emit Evidence objects.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class RouteTag(str, Enum):
    code = "code"
    docs = "docs"
    lkml = "lkml"
    bug = "bug"
    syzbot = "syzbot"
    commit = "commit"
    cve = "cve"


class RetrievalQuery(BaseModel):
    """Normalised query fed to all retrieval routes."""

    raw_question: str
    keywords: list[str] = Field(default_factory=list)
    kernel_version: str | None = None        # e.g. "OLK-6.6"
    subsystem: str | None = None             # e.g. "mm", "net/tcp"
    cve_ids: list[str] = Field(default_factory=list)
    commit_hashes: list[str] = Field(default_factory=list)
    routes: list[RouteTag] = Field(
        default_factory=lambda: list(RouteTag),
        description="Routes to fire. Defaults to all 7.",
    )
    limit_per_route: int = 100   # v2.2 P0 Path B — wider pool for reranker


class Evidence(BaseModel):
    """A single retrieved item from any route."""

    route: RouteTag
    score: float = 0.0
    title: str = ""
    body: str = ""
    url: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    # Normalised source IDs (used by reranker and graph linker)
    commit_hash: str | None = None
    bug_id: int | None = None
    cve_id: str | None = None
    message_id: str | None = None
    crash_id: str | None = None


class RetrievalResult(BaseModel):
    """Aggregated output of all-routes retrieval."""

    query: RetrievalQuery
    items: list[Evidence] = Field(default_factory=list)
    reranked: bool = False
    latency_ms: dict[str, float] = Field(default_factory=dict)

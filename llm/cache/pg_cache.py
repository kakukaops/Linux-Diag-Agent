"""M1 PostgreSQL LLM response cache (ADR, M1 §6).

Cache key = SHA-256 of (provider, model, messages, tools, tool_choice, temperature,
response_format).  Only temperature==0 responses are cached.

DDL lives in the Alembic migration; this module assumes the table exists.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from sqlalchemy import text

from llm.provider.base import ChatRequest, ChatResponse, Usage


_CACHE_VERSION = 1


def _cache_key(req: ChatRequest, provider_name: str) -> str:
    payload = {
        "schema_version": _CACHE_VERSION,
        "provider": provider_name,
        "model": req.model,
        "messages": [m.model_dump(exclude_none=True) for m in req.messages],
        "tools": [t.model_dump() for t in req.tools] if req.tools else None,
        "tool_choice": req.tool_choice,
        "temperature": req.temperature,
        "response_format": req.response_format,
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def _is_cacheable(req: ChatRequest) -> bool:
    """Only cache temperature=0 (deterministic) responses."""
    if req.temperature != 0.0:
        return False
    meta = req.metadata or {}
    from configs.config import get_config
    bypass_keys = get_config().llm.cache.bypass_when_metadata_has
    return not any(k in meta for k in bypass_keys)


class PgLlmCache:
    """Thin wrapper around the `llm_response_cache` table."""

    def __init__(self, engine) -> None:
        self._engine = engine

    def get(self, req: ChatRequest, provider_name: str) -> ChatResponse | None:
        if not _is_cacheable(req):
            return None
        key = _cache_key(req, provider_name)
        sql = text("""
            UPDATE llm_response_cache
               SET hit_count = hit_count + 1, last_hit_at = NOW()
             WHERE cache_key = :key
               AND created_at > NOW() - INTERVAL ':ttl days'
            RETURNING response, usage
        """)
        # Use raw string interpolation for interval (no bind param for intervals in psycopg3)
        from configs.config import get_config
        ttl = get_config().llm.cache.ttl_days
        sql = text(f"""
            UPDATE llm_response_cache
               SET hit_count = hit_count + 1, last_hit_at = NOW()
             WHERE cache_key = :key
               AND created_at > NOW() - INTERVAL '{ttl} days'
            RETURNING response, usage
        """)
        with self._engine.connect() as conn:
            row = conn.execute(sql, {"key": key}).fetchone()
            conn.commit()
        if not row:
            return None
        resp_data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        usage_data = row[1] if isinstance(row[1], dict) else json.loads(row[1])
        return ChatResponse(
            content=resp_data.get("content", ""),
            finish_reason=resp_data.get("finish_reason", "stop"),
            tool_calls=resp_data.get("tool_calls", []),
            usage=Usage(**usage_data),
            provider=provider_name,
            cached=True,
        )

    def put(self, req: ChatRequest, provider_name: str, resp: ChatResponse) -> None:
        if not _is_cacheable(req):
            return
        key = _cache_key(req, provider_name)
        sql = text("""
            INSERT INTO llm_response_cache
                (cache_key, provider, model, schema_version, response, usage, cost_saved_usd)
            VALUES
                (:key, :provider, :model, :sv, :response::jsonb, :usage::jsonb, :cost)
            ON CONFLICT (cache_key) DO NOTHING
        """)
        with self._engine.connect() as conn:
            conn.execute(sql, {
                "key": key,
                "provider": provider_name,
                "model": req.model,
                "sv": _CACHE_VERSION,
                "response": json.dumps({
                    "content": resp.content,
                    "finish_reason": resp.finish_reason,
                    "tool_calls": [tc.model_dump() for tc in resp.tool_calls],
                }),
                "usage": resp.usage.model_dump_json(),
                "cost": resp.usage.cost_usd_est,
            })
            conn.commit()

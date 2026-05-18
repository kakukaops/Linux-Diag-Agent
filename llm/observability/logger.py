"""M1 LLM call observability — JSONL file logger (ADR-016, M1 §8).

Writes one JSON line per LLM call to data/logs/<date>/llm.jsonl.
Also maintains a rolling metrics snapshot at data/metrics/llm.json.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


class LLMLogger:
    """Thread-safe JSONL logger for LLM calls."""

    def __init__(self, log_dir: str | Path | None = None) -> None:
        if log_dir is None:
            from configs.config import get_config
            log_dir = get_config().observability.log_dir
        self._log_dir = Path(log_dir)
        self._lock = threading.Lock()
        self._counters: dict[str, int] = {}
        self._errors: dict[str, int] = {}

    def record(
        self,
        *,
        trace_id: str,
        provider: str,
        model: str,
        role: str,
        messages_count: int,
        tools_count: int,
        input_tokens: int,
        output_tokens: int,
        cache_local_hit: bool,
        latency_ms: float,
        cost_usd_est: float,
        subscription_messages_used: int = 1,
        error: str | None = None,
    ) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "trace_id": trace_id,
            "provider": provider,
            "model": model,
            "role": role,
            "messages_count": messages_count,
            "tools_count": tools_count,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_local_hit": cache_local_hit,
            "latency_ms": round(latency_ms, 1),
            "cost_usd_est": cost_usd_est,
            "subscription_messages_used": subscription_messages_used,
            "error": error,
        }
        self._write_jsonl(entry)
        self._update_counters(entry)

    def _write_jsonl(self, entry: dict[str, Any]) -> None:
        today = date.today().isoformat()
        log_path = self._log_dir / today / "llm.jsonl"
        with self._lock:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _update_counters(self, entry: dict[str, Any]) -> None:
        key = f"{entry['provider']}.{entry['model']}"
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + 1
            if entry.get("error"):
                self._errors[key] = self._errors.get(key, 0) + 1


_default_logger: LLMLogger | None = None
_logger_lock = threading.Lock()


def get_logger() -> LLMLogger:
    global _default_logger
    with _logger_lock:
        if _default_logger is None:
            _default_logger = LLMLogger()
    return _default_logger

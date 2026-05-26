"""LLM reranker (WBS 3.9) — triggered when total candidates exceed RERANK_THRESHOLD.

Uses the Navigator LLM to score each Evidence item against the query.
Returns the top-k items sorted by LLM relevance score.
"""

from __future__ import annotations

import json
import logging
import re

from retrieval.schema import Evidence, RetrievalQuery

logger = logging.getLogger(__name__)

RERANK_THRESHOLD = 1   # always rerank (raised candidate pool from 70 → 350 makes BM25 ordering unreliable)
RERANK_TOP_K = 10
_RERANK_INPUT_CAP = 60  # max items sent to LLM; LLM context easily fits 60 short snippets

_RERANK_PROMPT = """\
You are a Linux kernel expert. Given the question and a list of retrieved items, \
score each item 0-10 for relevance. Return ONLY a JSON array of integers in the same order.

Question: {question}

Items:
{items}
"""


def maybe_rerank(
    query: RetrievalQuery,
    items: list[Evidence],
) -> tuple[list[Evidence], bool]:
    """Return (possibly-reranked items, reranked_flag).

    Reranks only when len(items) > RERANK_THRESHOLD.
    Falls back to original BM25 order on any LLM failure.
    """
    if len(items) <= RERANK_THRESHOLD:
        return items, False

    try:
        return _llm_rerank(query, items), True
    except Exception as exc:
        logger.warning("LLM rerank failed, keeping BM25 order: %s", exc)
        return items[:RERANK_TOP_K], False


def _llm_rerank(query: RetrievalQuery, items: list[Evidence]) -> list[Evidence]:
    from llm.provider.base import ChatRequest, Message
    from llm.provider.registry import get_provider
    from configs.config import get_config

    cfg = get_config()
    provider = get_provider(cfg.llm.navigator.backend)

    candidates = items[:_RERANK_INPUT_CAP]
    item_lines = "\n".join(
        f"[{i}] ({e.route.value}) {e.title}: {e.body[:200]}"
        for i, e in enumerate(candidates)
    )
    req = ChatRequest(
        messages=[
            Message(
                role="user",
                content=_RERANK_PROMPT.format(
                    question=query.raw_question,
                    items=item_lines,
                ),
            )
        ],
        model=cfg.llm.navigator.model,
        temperature=0.0,
        stream=False,
    )
    resp = provider.chat(req)
    raw = (resp.content or "").strip()
    if not raw:
        raise ValueError("Empty rerank response")
    # Strip code fences then extract just the JSON array (LLM may append explanations)
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
    m = re.search(r"\[[\d,\s]+\]", raw)
    scores: list[int] = json.loads(m.group(0) if m else raw)

    # LLM occasionally returns 1 extra/missing score (especially when stream
    # consumer trims). Reconcile length non-fatally: truncate or pad with 0
    # (worst score → those candidates demote, don't crash the whole rerank).
    if len(scores) > len(candidates):
        scores = scores[:len(candidates)]
    elif len(scores) < len(candidates):
        scores = scores + [0] * (len(candidates) - len(scores))

    ranked = sorted(zip(scores, candidates), key=lambda x: x[0], reverse=True)
    return [item for _, item in ranked[:RERANK_TOP_K]]

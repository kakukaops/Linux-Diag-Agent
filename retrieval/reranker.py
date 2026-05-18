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

RERANK_THRESHOLD = 10
RERANK_TOP_K = 10

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
    provider = get_provider(cfg.llm.navigator.provider)

    item_lines = "\n".join(
        f"[{i}] ({e.route.value}) {e.title}: {e.body[:200]}"
        for i, e in enumerate(items)
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
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", (resp.content or "").strip())
    scores: list[int] = json.loads(content)

    if len(scores) != len(items):
        raise ValueError(f"Score length mismatch: {len(scores)} vs {len(items)}")

    ranked = sorted(zip(scores, items), key=lambda x: x[0], reverse=True)
    return [item for _, item in ranked[:RERANK_TOP_K]]

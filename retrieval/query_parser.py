"""Query parser (WBS 3.6) — LLM extracts structured RetrievalQuery from raw question.

Uses the Navigator LLM role (temperature=0, cacheable).
Falls back to naive keyword extraction if LLM is unavailable.
"""

from __future__ import annotations

import json
import logging
import re

from retrieval.schema import RetrievalQuery, RouteTag

logger = logging.getLogger(__name__)

_CVE_RE = re.compile(r"CVE-\d{4}-\d{4,7}", re.IGNORECASE)
_HASH_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
_VERSION_RE = re.compile(
    r"\b(?:OLK-)?(?:v?6\.6|v?5\.10|mainline)\b", re.IGNORECASE
)

_PARSE_PROMPT = """\
You are a Linux kernel expert. Extract structured retrieval parameters from the user's question.

Return ONLY valid JSON with these fields (omit missing ones):
{{
  "keywords": ["<relevant kernel terms>"],
  "kernel_version": "<OLK-6.6 | OLK-5.10 | mainline | null>",
  "subsystem": "<e.g. mm, net/tcp, fs/ext4, or null>",
  "cve_ids": ["<CVE-YEAR-NNNNN>"],
  "commit_hashes": ["<sha>"]
}}

Question: {question}
"""


def parse_query(
    raw_question: str,
    *,
    use_llm: bool = True,
) -> RetrievalQuery:
    """Return a RetrievalQuery from a natural-language question.

    Tries LLM parsing first; falls back to regex-only if LLM is unavailable.
    """
    if use_llm:
        try:
            return _llm_parse(raw_question)
        except Exception as exc:
            logger.warning("LLM query parse failed, using regex fallback: %s", exc)

    return _regex_parse(raw_question)


def _llm_parse(raw_question: str) -> RetrievalQuery:
    from llm.provider.base import ChatRequest, Message
    from llm.provider.registry import get_provider
    from configs.config import get_config

    cfg = get_config()
    provider = get_provider(cfg.llm.navigator.backend)
    req = ChatRequest(
        messages=[
            Message(role="user", content=_PARSE_PROMPT.format(question=raw_question)),
        ],
        model=cfg.llm.navigator.model,
        temperature=0.0,
        stream=False,
    )
    resp = provider.chat(req)
    content = resp.content or ""
    # Strip markdown fences if present
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
    data = json.loads(content)

    return RetrievalQuery(
        raw_question=raw_question,
        keywords=data.get("keywords", []),
        kernel_version=data.get("kernel_version"),
        subsystem=data.get("subsystem"),
        cve_ids=data.get("cve_ids", []),
        commit_hashes=data.get("commit_hashes", []),
    )


def _regex_parse(raw_question: str) -> RetrievalQuery:
    """Naive keyword/version/CVE extraction without LLM."""
    cve_ids = [m.upper() for m in _CVE_RE.findall(raw_question)]
    commit_hashes = _HASH_RE.findall(raw_question)
    vm = _VERSION_RE.search(raw_question)
    kernel_version: str | None = None
    if vm:
        v = vm.group(0).upper()
        if "6.6" in v:
            kernel_version = "OLK-6.6"
        elif "5.10" in v:
            kernel_version = "OLK-5.10"
        else:
            kernel_version = "mainline"

    # Simple keyword extraction: non-stopword tokens ≥ 3 chars
    _STOP = frozenset({
        "the", "and", "for", "that", "this", "how", "what", "why", "when",
        "with", "from", "into", "does", "are", "can", "has", "have", "its",
        "not", "but", "was", "had", "our", "all",
    })
    tokens = re.findall(r"[a-z_][a-z0-9_]{2,}", raw_question.lower())
    keywords = [t for t in tokens if t not in _STOP][:15]

    return RetrievalQuery(
        raw_question=raw_question,
        keywords=keywords,
        kernel_version=kernel_version,
        cve_ids=cve_ids,
        commit_hashes=commit_hashes,
    )

"""LKML L3 — lore.kernel.org live full-text search (ADR-025).

The discovery half of the `lkml` retrieval route: queries lore's list-agnostic
`/all/` Xapian search at diagnosis time, covering the full archive (all lists,
all history) rather than only the local L2 cache.

One search request per diagnosis — well within the 1.5s/req rate limit.
Network failures degrade gracefully to an empty result (the local L2 cache
half of the route still works) — `search()` never raises.

Results are merged by the route layer (`retrieval/recall/lkml.py`) and the
discovered messages get written back to the L2 cache.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}


@dataclass
class LoreSearchHit:
    message_id: str
    title: str
    permalink: str
    updated: str | None
    snippet: str


def search(keywords: str, *, limit: int = 20) -> list[LoreSearchHit]:
    """Full-text search lore `/all/`; return up to *limit* hits.

    Degrades to an empty list on any network/parse failure — never raises,
    so the caller can always fall back to the local L2 cache route.
    """
    if not keywords or not keywords.strip():
        return []
    # Imported here to keep retrieval/ import-light and avoid a hard
    # module-load dependency on the ingest layer.
    from ingest.lkml.fetcher import LoreFetcher
    try:
        # data_dir is unused on the search path (no files written).
        with LoreFetcher(Path(".")) as fetcher:
            atom = fetcher.fetch_search_atom(keywords.strip())
    except Exception as exc:
        logger.warning("[lore_search] query failed, degrading to cache-only: %s", exc)
        return []
    return _parse_search_atom(atom)[:limit]


def _parse_search_atom(atom: bytes) -> list[LoreSearchHit]:
    """Parse a lore Atom search response into structured hits."""
    hits: list[LoreSearchHit] = []
    if not atom:
        return hits
    try:
        root = ET.fromstring(atom)
    except ET.ParseError as exc:
        logger.warning("[lore_search] Atom parse error: %s", exc)
        return hits
    for entry in root.findall("a:entry", _ATOM_NS):
        link = entry.find("a:link", _ATOM_NS)
        href = link.get("href", "") if link is not None else ""
        if not href:
            continue
        permalink = href.rstrip("/")
        message_id = permalink.rsplit("/", 1)[-1]
        if "@" not in message_id:
            continue
        hits.append(LoreSearchHit(
            message_id=message_id,
            title=_text(entry, "a:title"),
            permalink=permalink,
            updated=_text(entry, "a:updated") or None,
            snippet=(_text(entry, "a:summary") or _text(entry, "a:content"))[:500],
        ))
    return hits


def _text(elem, tag: str) -> str:
    node = elem.find(tag, _ATOM_NS)
    return (node.text or "").strip() if node is not None and node.text else ""

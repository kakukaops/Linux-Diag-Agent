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
You are a Linux kernel expert. Translate the user's symptom description into
search terms that would appear in a KERNEL DEVELOPER's commit message or
patch discussion — NOT the user-facing words from the question.

CRITICAL — keyword generation rules:
1. DROP user-application words: "java", "MySQL", "process name", "my server",
   "diagnose", "how to", "system", "production".
2. DROP generic non-kernel words: "memory", "limit", "error", "issue" (too broad).
3. KEEP kernel-internal terms: function names (e.g. `__alloc_pages`,
   `kswapd0`), subsystem identifiers (`memcg`, `vmscan`, `nvme_queue_rq`),
   kernel struct/macro names (`memory.high`, `GFP_NOFS`), exact error
   strings from dmesg (`page allocation failure`, `Out of memory`,
   `soft lockup`, `RCU stall`).
4. EXPAND with kernel-developer synonyms even if user didn't use them:
   - "OOM in cgroup"     → also `memcontrol`, `oom_kill_process`, `mem_cgroup_out_of_memory`
   - "soft lockup"       → also `watchdog`, `softlockup`, `sched_clock_stable`
   - "I/O hang"          → also `blk_mq_get_tag`, `request_queue`, `hung_task`
   - "use-after-free"    → also `KASAN`, the exact function from the trace
   - "page fragmentation"→ also `compaction`, `__alloc_pages_slowpath`, `order:N`

CRITICAL — fewer is better. BM25 OR-joins ALL keywords; adding generic
terms (`OOM`, `memory`, `cgroup`) dilutes specific matches with noise.
Generate at most 4-6 of the MOST DISCRIMINATING keywords — terms a
specific bugfix commit subject would use, not generic subsystem words.

Always supply EMPTY arrays `[]` (never null) for unused list fields.

Return ONLY valid JSON:
{{
  "keywords": ["<4-6 most-discriminating kernel terms>"],
  "kernel_version": "<OLK-6.6 | OLK-5.10 | mainline | null>",
  "subsystem": "<mm | net/tcp | fs/ext4 | block | sched | rcu | ... | null>",
  "cve_ids": [],
  "commit_hashes": []
}}

Examples (specificity over coverage):

  Q: "v6.6 OLK: OOM kill of process 'java' with oom_score 800. cgroup
      memory limit 4GB hit."
  →
  {{"keywords": ["memcontrol", "memory.high", "throttle", "vmscan flushers"],
    "kernel_version": "OLK-6.6", "subsystem": "mm",
    "cve_ids": [], "commit_hashes": []}}
  (NOT: "OOM", "cgroup", "memory" — those match thousands of commits.
   YES: "memcontrol", "memory.high", "throttle" — these are in fix commit
        subjects for this class of bug.)

  Q: "Soft lockup CPU#2 stuck 23s in kworker/2:1H. __schedule in trace."
  →
  {{"keywords": ["sched_ext breather", "softlockup BPF scheduler",
                 "lseek trace soft lockup", "kworker schedule stuck"],
    "kernel_version": null, "subsystem": "sched",
    "cve_ids": [], "commit_hashes": []}}

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

    # `data.get(k, [])` returns the default only when key MISSING; if LLM
    # returns null/None for a list field, Pydantic rejects it. Coerce.
    return RetrievalQuery(
        raw_question=raw_question,
        keywords=data.get("keywords") or [],
        kernel_version=data.get("kernel_version"),
        subsystem=data.get("subsystem"),
        cve_ids=data.get("cve_ids") or [],
        commit_hashes=data.get("commit_hashes") or [],
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

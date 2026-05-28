"""Back-compat shim — re-exports from kg/signature.py (v2.3 promoted).

The signature algorithm now lives in `kg.signature` so it can be shared
across bug / lkml_message / dmesg_event tables, not just syzbot_crash.
Existing callers (ingest/syzbot/scraper.py + the unit tests) keep
working unchanged.
"""

from kg.signature import normalize_stack, stack_signature  # noqa: F401

"""NVD CVE commit hash extractor — scans references for kernel commit SHAs."""

from __future__ import annotations

import re

# Patterns used to find commit hashes in NVD references / descriptions
_SHA_RE = re.compile(r"\b([0-9a-f]{12,40})\b", re.IGNORECASE)
_COMMIT_URL_RE = re.compile(
    r"(?:github\.com/torvalds/linux/commit|git\.kernel\.org/(?:pub/scm/.+/linux.+)/commit)"
    r"/([0-9a-f]{7,40})",
    re.IGNORECASE,
)


def extract_fix_commits(cve_data: dict) -> list[str]:
    """Return candidate fix commit SHAs from NVD CVE JSON record."""
    candidates = set()
    # Check references
    for ref in cve_data.get("references", []):
        url = ref.get("url", "")
        m = _COMMIT_URL_RE.search(url)
        if m:
            candidates.add(m.group(1).lower())

    # Also scan description text
    desc = ""
    for d in cve_data.get("descriptions", []):
        if d.get("lang") == "en":
            desc = d.get("value", "")
            break

    # Only take SHAs that appear adjacent to "fix" / "commit" keywords
    for m in re.finditer(r"(?:fix|commit|patch)\s+([0-9a-f]{12,40})\b", desc, re.IGNORECASE):
        candidates.add(m.group(1).lower())

    return list(candidates)

"""LKML mbox parser — email → structured fields + trailer extraction."""

from __future__ import annotations

import email
import email.policy
import io
import logging
import mailbox
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any

logger = logging.getLogger(__name__)

# Trailer regexes
_FIXES_RE = re.compile(r"^Fixes:\s*([0-9a-f]{7,40})(?:\s+\(.*?\))?", re.MULTILINE | re.IGNORECASE)
_CLOSES_RE = re.compile(r"^Closes:\s*(https?://\S+)", re.MULTILINE | re.IGNORECASE)
_REPORTED_RE = re.compile(r"^Reported-by:\s*(.+)", re.MULTILINE | re.IGNORECASE)
_LINK_RE = re.compile(r"^Link:\s*(https?://\S+)", re.MULTILINE | re.IGNORECASE)
_REVIEWED_RE = re.compile(
    r"^(Reviewed-by|Tested-by|Acked-by|Signed-off-by|NACK):\s*(.+)",
    re.MULTILINE | re.IGNORECASE,
)
_PATCH_RE = re.compile(
    r"\[PATCH(?:\s+v\d+)?(?:\s+(\d+)/(\d+))?\]",
    re.IGNORECASE,
)
_DIFF_STAT_RE = re.compile(r"(\d+) files? changed")


@dataclass
class ParsedMessage:
    message_id: str
    in_reply_to: str | None
    references: list[str]
    author_name: str
    author_email: str
    date: datetime | None
    subject: str
    body: str
    list_name: str
    # Trailers
    fixes_hashes: list[str] = field(default_factory=list)
    closes_urls: list[str] = field(default_factory=list)
    reported_by: list[str] = field(default_factory=list)
    link_urls: list[str] = field(default_factory=list)
    reviews: list[dict[str, str]] = field(default_factory=list)
    # Patch metadata
    is_patch: bool = False
    patch_number: int | None = None
    series_total: int | None = None


def parse_mbox_bytes(data: bytes, list_name: str) -> list[ParsedMessage]:
    """Parse raw mbox bytes into ParsedMessage list."""
    results: list[ParsedMessage] = []
    box = mailbox.mbox(None)  # type: ignore[arg-type]
    # mailbox.mbox expects a file path; use StringIO workaround via BytesIO
    try:
        mbox_io = io.BytesIO(data)
        msgs = _iter_mbox(mbox_io)
        for raw_msg in msgs:
            try:
                pm = _parse_one(raw_msg, list_name)
                if pm:
                    results.append(pm)
            except Exception as exc:
                logger.debug("[lkml/parser] Skipped malformed message: %s", exc)
    except Exception as exc:
        logger.error("[lkml/parser] mbox parse error: %s", exc)
    return results


def _iter_mbox(mbox_bytes: io.BytesIO) -> list[email.message.Message]:
    """Yield email.message.Message objects from a raw mbox byte stream."""
    messages = []
    policy = email.policy.EmailPolicy(
        utf8=True, linesep="\n", refold_source="none"
    )
    content = mbox_bytes.read().decode("utf-8", errors="replace")
    # Split on "From " lines (mbox separator)
    parts = re.split(r"^From \S+\s+\S+\s+\S+\s+\d+\s+\d+:\d+:\d+\s+\d{4}$",
                     content, flags=re.MULTILINE)
    for part in parts:
        part = part.strip()
        if not part:
            continue
        try:
            msg = email.message_from_string(part, policy=email.policy.default)
            messages.append(msg)
        except Exception:
            continue
    return messages


def _parse_one(msg, list_name: str) -> ParsedMessage | None:
    message_id = _hdr(msg, "message-id", "").strip("<>").strip()
    if not message_id:
        return None

    in_reply_to_raw = _hdr(msg, "in-reply-to", "").strip()
    in_reply_to = in_reply_to_raw.strip("<>").strip() or None

    refs_raw = _hdr(msg, "references", "")
    references = [r.strip("<>").strip() for r in refs_raw.split() if r.strip()]

    from_raw = _hdr(msg, "from", "")
    author_name, author_email = parseaddr(from_raw)

    date_raw = _hdr(msg, "date", "")
    date: datetime | None = None
    if date_raw:
        try:
            date = parsedate_to_datetime(date_raw).astimezone(timezone.utc)
        except Exception:
            pass

    subject = _hdr(msg, "subject", "")
    body = _get_body(msg)

    # Trailers
    fixes = _FIXES_RE.findall(body)
    closes = _CLOSES_RE.findall(body)
    reported = [r.strip() for r in _REPORTED_RE.findall(body)]
    links = _LINK_RE.findall(body)
    reviews = [
        {"review_type": m[0].lower(), "reviewer": m[1].strip()}
        for m in _REVIEWED_RE.findall(body)
    ]

    # Patch detection
    patch_m = _PATCH_RE.search(subject)
    is_patch = patch_m is not None
    patch_number = int(patch_m.group(1)) if patch_m and patch_m.group(1) else None
    series_total = int(patch_m.group(2)) if patch_m and patch_m.group(2) else None

    return ParsedMessage(
        message_id=message_id,
        in_reply_to=in_reply_to,
        references=references,
        author_name=author_name or "",
        author_email=author_email or "",
        date=date,
        subject=subject,
        body=body,
        list_name=list_name,
        fixes_hashes=fixes,
        closes_urls=closes,
        reported_by=reported,
        link_urls=links,
        reviews=reviews,
        is_patch=is_patch,
        patch_number=patch_number,
        series_total=series_total,
    )


def _hdr(msg, key: str, default: str = "") -> str:
    val = msg.get(key, default)
    return str(val) if val else default


def _get_body(msg) -> str:
    """Extract plain text body from email message."""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain":
                try:
                    return part.get_content()
                except Exception:
                    payload = part.get_payload(decode=True)
                    if payload:
                        return payload.decode("utf-8", errors="replace")
        return ""
    try:
        return msg.get_content()
    except Exception:
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode("utf-8", errors="replace")
        return str(msg.get_payload() or "")

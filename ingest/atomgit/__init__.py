"""Atomgit issue ingester (ADR-022) — openeuler/kernel issues from atomgit.com.

Atomgit shares the gitee/gitcode v5 API shape; only the host, auth scheme
(Bearer instead of token), and ID format (numeric instead of alphanumeric)
differ. Reference-driven backfill — only fetches issue IDs cited by OLK
commits via `atomgit.com/openeuler/kernel/issues/<N>` URLs in commit bodies.
"""

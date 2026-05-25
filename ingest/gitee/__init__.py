"""Gitee issue ingester (ADR-022) — openeuler/kernel issues from gitee.com.

Reference-driven: only fetch issues actually cited by OLK commits, not the
full project history. ~4K unique issue IDs from ~52K commit references.
"""

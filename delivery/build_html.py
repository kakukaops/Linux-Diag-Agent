#!/usr/bin/env python3
"""Convert all delivery .md files to standalone .html.

Usage:
    python delivery/build_html.py           # build all
    python delivery/build_html.py --clean   # remove stale .html first
    python delivery/build_html.py --dir /other/path
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

import markdown
from markdown.extensions.toc import TocExtension

DELIVERY_DIR = Path(__file__).parent

# ---------------------------------------------------------------------------
# Embedded CSS — GitHub-inspired, zero external deps, Chinese-friendly
# ---------------------------------------------------------------------------
CSS = """\
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",
        "Hiragino Sans GB","Microsoft YaHei",Helvetica,Arial,sans-serif;
    font-size:16px;line-height:1.75;color:#24292f;background:#fff;
}

/* ── Top nav ─────────────────────────────────────────────────────────── */
.top-nav{
    position:sticky;top:0;z-index:100;
    background:#f6f8fa;border-bottom:1px solid #d0d7de;
    padding:10px 28px;display:flex;align-items:center;
    gap:6px;font-size:14px;flex-wrap:wrap;
}
.top-nav a{color:#0969da;text-decoration:none}
.top-nav a:hover{text-decoration:underline}
.top-nav .sep{color:#8c959f}
.top-nav .page-title{color:#24292f;font-weight:600}

/* ── Layout ─────────────────────────────────────────────────────────── */
.layout{
    display:flex;max-width:1180px;margin:0 auto;
    padding:36px 28px;gap:44px;align-items:flex-start;
}

/* ── TOC sidebar ─────────────────────────────────────────────────────── */
.toc-col{
    width:210px;flex-shrink:0;
    position:sticky;top:57px;
    max-height:calc(100vh - 80px);overflow-y:auto;
    border-right:1px solid #d0d7de;padding-right:20px;
}
.toc-col .toc-label{
    font-size:11px;font-weight:600;text-transform:uppercase;
    letter-spacing:.06em;color:#8c959f;margin-bottom:10px;
}
.toc-col .toc ul{list-style:none;padding:0;margin:0}
.toc-col .toc li{padding:2px 0}
.toc-col .toc a{
    font-size:13px;color:#57606a;text-decoration:none;
    display:block;padding:1px 0;
}
.toc-col .toc a:hover{color:#0969da}
.toc-col .toc ul ul{padding-left:14px}

/* ── Article ─────────────────────────────────────────────────────────── */
article{flex:1;min-width:0}

/* headings */
article h1{font-size:1.9em;border-bottom:1px solid #d0d7de;padding-bottom:.35em;margin:0 0 .9em}
article h2{font-size:1.45em;border-bottom:1px solid #d0d7de;padding-bottom:.3em;margin:1.8em 0 .75em}
article h3{font-size:1.2em;margin:1.4em 0 .6em;color:#24292f}
article h4,article h5,article h6{font-size:1em;margin:1.2em 0 .5em}
/* anchor permalink — hide by default, show on hover */
article .headerlink{opacity:0;font-size:.8em;margin-left:.4em;text-decoration:none;color:#8c959f}
article :is(h1,h2,h3,h4):hover .headerlink{opacity:1}

/* paragraphs & lists */
article p{margin:0 0 .9em}
article ul,article ol{margin:0 0 .9em 1.6em;padding:0}
article li{margin:.3em 0}
article li>p{margin-bottom:.4em}

/* inline */
article strong{font-weight:600}
article em{font-style:italic}
article hr{border:0;border-top:1px solid #d0d7de;margin:1.6em 0}
article blockquote{
    border-left:4px solid #d0d7de;color:#57606a;
    margin:0 0 1em;padding:.2em 1em;
}
article a{color:#0969da;text-decoration:none}
article a:hover{text-decoration:underline}

/* inline code */
article :not(pre)>code{
    font-family:"SFMono-Regular",Consolas,"Liberation Mono",Menlo,monospace;
    font-size:85%;background:#f6f8fa;
    border:1px solid #d0d7de;border-radius:6px;
    padding:.1em .4em;
}

/* code blocks */
article pre{
    background:#f6f8fa;border:1px solid #d0d7de;border-radius:8px;
    padding:18px;overflow-x:auto;margin:0 0 1em;
    font-size:13px;line-height:1.55;
}
article pre code{
    font-family:"SFMono-Regular",Consolas,"Liberation Mono",Menlo,monospace;
    background:none;border:none;padding:0;font-size:inherit;
}

/* shell / bash prompt styling (lines starting with #) */
article pre .comment{color:#6e7781;font-style:italic}

/* tables */
article table{border-collapse:collapse;width:100%;margin:0 0 1em;font-size:14.5px}
article th,article td{border:1px solid #d0d7de;padding:7px 13px;text-align:left;vertical-align:top}
article th{background:#f6f8fa;font-weight:600}
article tbody tr:nth-child(even)>td{background:#f6f8fa}

/* responsive */
@media(max-width:820px){
    .toc-col{display:none}
    .layout{padding:16px}
}
"""

# ---------------------------------------------------------------------------
# Markdown → HTML
# ---------------------------------------------------------------------------
_MD_EXTENSIONS = [
    "tables",
    "fenced_code",
    "attr_list",
    TocExtension(toc_depth="1-3", permalink=True),
]


def _convert(md_text: str) -> tuple[str, str]:
    """Return (body_html, toc_html)."""
    md = markdown.Markdown(extensions=_MD_EXTENSIONS)
    body = md.convert(md_text)
    return body, md.toc


def _rewrite_md_links(html: str) -> str:
    """Replace href="foo.md" → href="foo.html" (preserves #anchors)."""
    def _sub(m: re.Match) -> str:
        path, anchor = m.group(1), m.group(2) or ""
        return f'href="{path}.html{anchor}"'
    return re.sub(r'href="([^"#]*?)\.md(#[^"]*?)?"', _sub, html)


def _extract_title(md_text: str, stem: str) -> str:
    m = re.search(r"^#\s+(.+)", md_text, re.MULTILINE)
    return m.group(1).strip() if m else stem


# ---------------------------------------------------------------------------
# Nav bar
# ---------------------------------------------------------------------------
def _build_nav(rel: Path) -> str:
    """Build <nav> HTML for a file at `rel` (relative to delivery dir)."""
    parts = rel.parts          # e.g. ('10_MODULE_SPECS', 'M1_llm_provider.md')
    depth = len(parts) - 1    # subdirectory depth (0 = root)

    home_href = "../" * depth + "index.html"

    crumbs: list[str] = []

    if rel.name in ("README.md",):
        # README is the home page itself
        crumbs.append('<span class="page-title">🏠 Linux-Diag-Agent · 交付文档</span>')
    else:
        crumbs.append(f'<a href="{home_href}">🏠 首页</a>')
        if depth >= 1:
            parent = parts[-2]
            crumbs.append('<span class="sep">›</span>')
            crumbs.append(f'<a href="../">{parent}</a>')
        stem = rel.stem
        crumbs.append('<span class="sep">›</span>')
        crumbs.append(f'<span class="page-title">{stem}</span>')

    return '<nav class="top-nav">' + "".join(crumbs) + "</nav>\n"


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------
_TEMPLATE = """\
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
{css}
</style>
</head>
<body>
{nav}
<div class="layout">
{toc_col}
<article>
{body}
</article>
</div>
</body>
</html>
"""


def _render(md_path: Path, delivery_dir: Path) -> str:
    md_text = md_path.read_text(encoding="utf-8")
    title = _extract_title(md_text, md_path.stem)
    body_html, toc_html = _convert(md_text)
    body_html = _rewrite_md_links(body_html)

    rel = md_path.relative_to(delivery_dir)
    nav = _build_nav(rel)

    # Only show TOC sidebar when there are ≥2 headings
    heading_count = len(re.findall(r"<li>", toc_html))
    if heading_count >= 2:
        toc_col = (
            '<aside class="toc-col">'
            '<div class="toc-label">目录</div>'
            f'{toc_html}'
            "</aside>"
        )
    else:
        toc_col = ""

    return _TEMPLATE.format(
        title=title,
        css=CSS,
        nav=nav,
        toc_col=toc_col,
        body=body_html,
    )


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
def build_all(delivery_dir: Path, clean: bool = False) -> None:
    md_files = sorted(delivery_dir.rglob("*.md"))

    # Exclude this script's directory if it accidentally has .md files outside delivery
    md_files = [f for f in md_files if delivery_dir in f.parents or f.parent == delivery_dir]

    count = 0
    for md_path in md_files:
        html_path = md_path.with_suffix(".html")

        if clean and html_path.exists():
            html_path.unlink()

        html = _render(md_path, delivery_dir)
        html_path.write_text(html, encoding="utf-8")
        rel = md_path.relative_to(delivery_dir)
        print(f"  {rel}  →  {html_path.name}")
        count += 1

    # README.html → also write index.html (README IS the home page)
    readme_html = delivery_dir / "README.html"
    index_html = delivery_dir / "index.html"
    if readme_html.exists():
        index_html.write_text(readme_html.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"  README.html  →  index.html  (entry point)")

    print(f"\n✓ {count} files converted — open delivery/index.html to browse")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Build HTML from delivery .md files")
    ap.add_argument("--clean", action="store_true", help="remove existing .html before build")
    ap.add_argument("--dir", default=None, help="delivery directory (default: script location)")
    args = ap.parse_args()

    d = Path(args.dir).resolve() if args.dir else DELIVERY_DIR
    build_all(d, clean=args.clean)

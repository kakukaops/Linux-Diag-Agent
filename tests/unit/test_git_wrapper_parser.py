"""Tests for ingest/kernel_commit/git_wrapper.py — body/file splitter.

Regression test for the 341K-row subsystem pollution bug: the original
parser treated any blank line followed by a slash/dot-containing line as
the start of a file list, so commit body trailers (Link:, Closes:, etc.)
were misclassified as file paths. infer_subsystem then dirname'd those
"paths" and produced things like subsystem = "Link: https:".
"""

from ingest.kernel_commit.git_wrapper import _split_body_and_files


def test_simple_body_then_files():
    raw = "Just a commit body\n\nmm/slab.c\nmm/slub.c"
    body, files = _split_body_and_files(raw)
    assert body == "Just a commit body"
    assert files == ["mm/slab.c", "mm/slub.c"]


def test_body_with_paragraph_break_and_trailers_then_files():
    """The regression case: body has intra-body blank lines + Link: trailer.
    These must stay in body and NOT bleed into file list."""
    raw = (
        "Fix the thing.\n"
        "\n"
        "Detailed paragraph two.\n"
        "\n"
        "Fixes: 1234567890ab (\"earlier broken commit\")\n"
        "Link: https://lore.kernel.org/all/abc@xyz/\n"
        "Closes: https://bugzilla.kernel.org/show_bug.cgi?id=1234\n"
        "Reported-by: someone@example.com\n"
        "Signed-off-by: Maintainer <m@x.org>\n"
        "\n"
        "net/ipv4/tcp_output.c\n"
        "include/net/sock.h\n"
    )
    body, files = _split_body_and_files(raw)
    assert "Link: https://lore.kernel.org" in body
    assert "Closes:" in body
    assert "Signed-off-by:" in body
    assert files == ["net/ipv4/tcp_output.c", "include/net/sock.h"]


def test_top_level_files_recognised():
    raw = "Updated docs\n\nMakefile\nKconfig\nCOPYING"
    _, files = _split_body_and_files(raw)
    assert files == ["Makefile", "Kconfig", "COPYING"]


def test_files_with_dot_extensions_recognised():
    """Real kernel commits put files under directories, so '/' alone is
    enough. The dot-extension list is a backstop for rare top-level files."""
    raw = "Fix things\n\nfs/foo.c\nfs/bar.h"
    _, files = _split_body_and_files(raw)
    assert "fs/foo.c" in files
    assert "fs/bar.h" in files


def test_body_only_no_files():
    raw = "Just a body line\nAnother body line\nNo files at all"
    body, files = _split_body_and_files(raw)
    assert "Just a body line" in body
    assert files == []


def test_empty_input():
    body, files = _split_body_and_files("")
    assert body == ""
    assert files == []


def test_trailer_without_url_does_not_break():
    raw = (
        "Body line one\n"
        "\n"
        "Cc: stable@vger.kernel.org\n"
        "Tested-by: someone\n"
        "\n"
        "drivers/net/foo.c\n"
    )
    body, files = _split_body_and_files(raw)
    assert "Cc: stable" in body
    assert files == ["drivers/net/foo.c"]


def test_polluted_input_no_files_section():
    """Real-world failure: a commit where --name-only output is missing
    (e.g. for a merge with no diff) but body has trailers with URLs.
    The original parser would put 'Link: https://...' into file_lines."""
    raw = (
        "Merge branch 'fixes' of git://...\n"
        "\n"
        "* 'fixes' branch:\n"
        "  net: fix something\n"
        "  ext4: fix another\n"
        "\n"
        "Link: https://lore.kernel.org/all/foo@bar/\n"
        "Signed-off-by: Linus <torvalds@linux-foundation.org>\n"
    )
    _, files = _split_body_and_files(raw)
    # No real file paths in this output — must not return trailer URLs as files.
    assert all("Link:" not in f for f in files)
    assert all(" " not in f for f in files)

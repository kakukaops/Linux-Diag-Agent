"""Tests for ingest/kernel_commit/symbol_extractor.py.

Covers (1) the C-identifier extraction from git's hunk function-context
line and (2) the diff parser that pairs file paths to symbols.
"""

from unittest.mock import patch, MagicMock

from ingest.kernel_commit.symbol_extractor import (
    extract_symbols_from_context,
    _parse_diff_for_symbols,
    extract_symbols_for_commit,
)


# ── extract_symbols_from_context ────────────────────────────────────────────

def test_extract_simple_function():
    assert extract_symbols_from_context(
        "static int tcp_send_mss(struct sock *sk, int *size_goal, int flags)"
    ) == ("tcp_send_mss", "function")


def test_extract_void_function():
    assert extract_symbols_from_context(
        "void __ext4_writepages_complete(struct inode *inode)"
    ) == ("__ext4_writepages_complete", "function")


def test_extract_struct_definition():
    sym, kind = extract_symbols_from_context(
        "static const struct nla_policy ifla_policy[IFLA_MAX+1] = {"
    )
    assert sym == "ifla_policy"
    assert kind == "struct"


def test_extract_macro():
    assert extract_symbols_from_context(
        "#define WRITE_INT(reg, val)"
    ) == ("WRITE_INT", "macro")


def test_extract_macro_with_spaces():
    assert extract_symbols_from_context(
        "# define MAX_FOO 42"
    ) == ("MAX_FOO", "macro")


def test_extract_inline_function():
    sym, kind = extract_symbols_from_context(
        "static inline u32 sk_dst_gso_max_size(struct sock *sk, struct sk_buff *skb)"
    )
    assert sym == "sk_dst_gso_max_size"
    assert kind == "function"


def test_extract_empty_context():
    assert extract_symbols_from_context("") == (None, "unknown")
    assert extract_symbols_from_context("   ") == (None, "unknown")


def test_extract_skips_type_keywords():
    """Naive impl might return 'struct' or 'int' as the symbol."""
    sym, _ = extract_symbols_from_context("static int foo(void)")
    assert sym == "foo"


def test_extract_typedef_fallthrough():
    """Typedef line: take the first plausible kernel-shape identifier."""
    sym, kind = extract_symbols_from_context(
        "typedef struct my_thing my_thing_t;"
    )
    assert sym == "my_thing"   # snake_case, passes plausibility check
    assert kind == "unknown"


def test_fallback_rejects_bare_lowercase_variable():
    """Real noise from Run 9: 'if (config->magic == X)' surfaced 'config'
    as a symbol 916 times. Plausibility check now rejects bare lowercase
    short words."""
    sym, _ = extract_symbols_from_context("if (config->magic == X)")
    assert sym is None, "should not surface 'config' as a symbol"

    sym, _ = extract_symbols_from_context("do {")
    assert sym is None, "'do' is a C keyword and short"

    sym, _ = extract_symbols_from_context("obj = NULL")
    # 'obj' (3 chars) too short; 'NULL' is uppercase but only 4 chars and
    # the actual extraction path tries `= or [` struct pattern first.
    # If both fail, fallback rejects 'obj' (too short, no _).
    # We just assert no false 'obj' / no leakage of single short words.
    assert sym != "obj"


def test_fallback_accepts_snake_case_struct_name():
    """Legit kernel struct/type names should survive the fallback path."""
    for context in (
        "static struct task_struct *foo",
        "static struct bpf_attr *attr",
        "struct kvm_vcpu_arch *arch",
    ):
        sym, kind = extract_symbols_from_context(context)
        assert sym is not None, f"failed on {context!r}"
        assert "_" in sym, f"{sym} should be snake_case"
        assert kind == "unknown"


def test_fallback_accepts_upper_macro():
    """Capital-letter constants / macro names pass plausibility."""
    sym, _ = extract_symbols_from_context("FOO_BAR_BAZ + 1")
    assert sym == "FOO_BAR_BAZ"


def test_fallback_rejects_english_words():
    """Doc-comment lines like 'The function does X' should not produce
    symbols 'The'/'function'/'does'."""
    sym, _ = extract_symbols_from_context("The function does X")
    assert sym is None or sym not in {"The", "function", "does"}


# ── _parse_diff_for_symbols ─────────────────────────────────────────────────

_GSO_FIX_DIFF = """\
diff --git a/net/core/rtnetlink.c b/net/core/rtnetlink.c
index e30e7ea0207d..2ba5cd965d3f 100644
--- a/net/core/rtnetlink.c
+++ b/net/core/rtnetlink.c
@@ -2035 +2035 @@ static const struct nla_policy ifla_policy[IFLA_MAX+1] = {
-	[IFLA_GSO_MAX_SIZE]	= { .type = NLA_U32 },
+	[IFLA_GSO_MAX_SIZE]	= NLA_POLICY_MIN(NLA_U32, MAX_TCP_HEADER + 1),
@@ -2060 +2060 @@ static const struct nla_policy ifla_policy[IFLA_MAX+1] = {
-	[IFLA_GSO_IPV4_MAX_SIZE]	= { .type = NLA_U32 },
+	[IFLA_GSO_IPV4_MAX_SIZE]	= NLA_POLICY_MIN(NLA_U32, MAX_TCP_HEADER + 1),
"""


def test_parse_real_gso_fix_diff():
    """The kasan-002 ground-truth commit (9ab5cf19fb0e) — both hunks touch
    the same struct, so dedup must collapse to one row."""
    rows = _parse_diff_for_symbols(_GSO_FIX_DIFF, "9ab5cf19fb0e")
    assert len(rows) == 1
    r = rows[0]
    assert r["commit_hash"] == "9ab5cf19fb0e"
    assert r["file_path"] == "net/core/rtnetlink.c"
    assert r["symbol"] == "ifla_policy"
    assert r["kind"] == "struct"


_MULTI_FUNC_DIFF = """\
diff --git a/fs/ext4/inode.c b/fs/ext4/inode.c
--- a/fs/ext4/inode.c
+++ b/fs/ext4/inode.c
@@ -100,3 +100,3 @@ static int ext4_writepages(struct inode *inode)
-	old_line_a
-	old_line_b
-	old_line_c
+	new_line_a
+	new_line_b
+	new_line_c
@@ -200,1 +200,1 @@ void ext4_release_inode(struct inode *inode)
-	free(inode);
+	kfree(inode);
diff --git a/include/linux/ext4.h b/include/linux/ext4.h
--- a/include/linux/ext4.h
+++ b/include/linux/ext4.h
@@ -50,1 +50,1 @@ struct ext4_inode_info {
-	int old_field;
+	int new_field;
"""


def test_parse_multiple_functions_and_files():
    rows = _parse_diff_for_symbols(_MULTI_FUNC_DIFF, "abc1234")
    symbols = {(r["file_path"], r["symbol"]) for r in rows}
    assert ("fs/ext4/inode.c", "ext4_writepages") in symbols
    assert ("fs/ext4/inode.c", "ext4_release_inode") in symbols
    assert ("include/linux/ext4.h", "ext4_inode_info") in symbols


def test_parse_empty_diff():
    """Merge commits with no real diff produce no rows."""
    assert _parse_diff_for_symbols("", "abc") == []


def test_extract_skips_merge_commit():
    """Regression: 9.5h of wasted backfill on gitee MR merges (subject '!XXXXX')
    because git show on a merge yields no diff, returns 0 symbols, and the
    candidate query keeps re-selecting the same hash forever. The fast-path
    parent-count check must short-circuit before the expensive git show."""
    with patch("subprocess.run") as mock_run:
        # parent-count call returns 2 parents (merge)
        parents = MagicMock()
        parents.returncode = 0
        parents.stdout = b"9b9d2e7280aba1dc1bd500abc504dad5d1a3622f ae9235634352a4b3ab537dd4fb5888b077cb881a"
        mock_run.return_value = parents

        rows = extract_symbols_for_commit("/dummy", "deadbeef")
        assert rows == []
        # Crucially: only ONE subprocess call (the parent check). The
        # expensive `git show` must NOT have been invoked.
        assert mock_run.call_count == 1


def test_extract_skips_root_commit():
    """A root commit (0 parents) also has no diff — skip fast."""
    with patch("subprocess.run") as mock_run:
        parents = MagicMock()
        parents.returncode = 0
        parents.stdout = b""   # no parents
        mock_run.return_value = parents

        rows = extract_symbols_for_commit("/dummy", "rootcomit")
        assert rows == []
        assert mock_run.call_count == 1


def test_extract_proceeds_for_normal_commit():
    """A normal single-parent commit goes through the full git show path."""
    with patch("subprocess.run") as mock_run:
        # 1st call: parent count = 1
        parents = MagicMock()
        parents.returncode = 0
        parents.stdout = b"singleparenthash\n"
        # 2nd call: git show output
        show = MagicMock()
        show.returncode = 0
        show.stdout = (
            "diff --git a/fs/x.c b/fs/x.c\n"
            "@@ -1 +1 @@ int foo(void)\n"
            "-x\n"
            "+y\n"
        ).encode()
        mock_run.side_effect = [parents, show]

        rows = extract_symbols_for_commit("/dummy", "abc1234")
        assert mock_run.call_count == 2
        assert rows == [{"commit_hash": "abc1234", "file_path": "fs/x.c",
                          "symbol": "foo", "kind": "function"}]


def test_parse_hunk_without_funcname():
    """Some hunks (e.g. top-of-file changes) have no function context.
    Skip those rows silently."""
    diff = (
        "diff --git a/Makefile b/Makefile\n"
        "--- a/Makefile\n"
        "+++ b/Makefile\n"
        "@@ -1,1 +1,1 @@\n"
        "-VERSION = 6.5\n"
        "+VERSION = 6.6\n"
    )
    assert _parse_diff_for_symbols(diff, "xyz") == []

from common.diff import parse_diff, parse_patch, select_reviewable

DIFF = """\
diff --git a/app/db.py b/app/db.py
index 1111111..2222222 100644
--- a/app/db.py
+++ b/app/db.py
@@ -10,5 +10,6 @@ def get_user(conn, user_id):
     cur = conn.cursor()
-    cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
+    query = f"SELECT * FROM users WHERE id = {user_id}"
+    cur.execute(query)
     row = cur.fetchone()
     return row
 
@@ -40,2 +41,3 @@ def close(conn):
     conn.close()
+    log.info("closed")
 # end
diff --git a/migrations/001.sql b/migrations/001.sql
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/migrations/001.sql
@@ -0,0 +1,2 @@
+-- create users
+CREATE TABLE users (id int);
\\ No newline at end of file
diff --git a/old.txt b/old.txt
deleted file mode 100644
index 4444444..0000000
--- a/old.txt
+++ /dev/null
@@ -1,2 +0,0 @@
-bye
-now
diff --git a/src/a.py b/src/b.py
similarity index 100%
rename from src/a.py
rename to src/b.py
diff --git a/logo.png b/logo.png
new file mode 100644
index 0000000..5555555
Binary files /dev/null and b/logo.png differ
diff --git a/docs/my notes.md b/docs/my notes.md
index 6666666..7777777 100644
--- a/docs/my notes.md
+++ b/docs/my notes.md
@@ -1 +1 @@
-old
+new
"""


def test_files_and_statuses():
    files = {f.path: f for f in parse_diff(DIFF)}
    assert list(files) == ["app/db.py", "migrations/001.sql", "old.txt", "src/b.py", "logo.png", "docs/my notes.md"]
    assert files["app/db.py"].status == "modified"
    assert files["migrations/001.sql"].status == "added"
    assert files["old.txt"].status == "deleted"
    assert (files["src/b.py"].status, files["src/b.py"].old_path) == ("renamed", "src/a.py")
    assert files["logo.png"].is_binary


def test_line_numbers_follow_the_new_file():
    f = parse_diff(DIFF)[0]
    h1, h2 = f.hunks
    assert (h1.old_start, h1.new_start, h1.section) == (10, 10, "def get_user(conn, user_id):")
    added = [(ln.new_no, ln.content.strip()) for ln in h1.lines if ln.kind == "+"]
    assert added == [(11, 'query = f"SELECT * FROM users WHERE id = {user_id}"'), (12, "cur.execute(query)")]
    removed = [ln for ln in h1.lines if ln.kind == "-"]
    assert removed[0].old_no == 11 and removed[0].new_no is None
    assert f.added_lines() == {11, 12, 42}
    # context lines are commentable too; removed lines are not
    assert f.commentable_lines() == {10, 11, 12, 13, 14, 15, 41, 42, 43}
    assert (f.additions, f.deletions) == (3, 1)


def test_removed_sql_comment_is_not_a_file_header():
    """A removed line '-- x' appears as '--- x'; the hunk's line counts must keep it inside the hunk."""
    diff = "diff --git a/q.sql b/q.sql\n--- a/q.sql\n+++ b/q.sql\n@@ -1,2 +1,1 @@\n--- old comment\n SELECT 1;\n"
    (f,) = parse_diff(diff)
    assert f.path == "q.sql"
    assert [(ln.kind, ln.content) for ln in f.hunks[0].lines] == [("-", "-- old comment"), (" ", "SELECT 1;")]


def test_no_newline_marker_and_short_hunk_header():
    files = {f.path: f for f in parse_diff(DIFF)}
    sql = files["migrations/001.sql"]
    assert sql.added_lines() == {1, 2}
    notes = files["docs/my notes.md"]  # "@@ -1 +1 @@" means length 1; path contains a space
    assert notes.added_lines() == {1}


def test_quoted_unicode_path():
    diff = (
        'diff --git "a/caf\\303\\251.py" "b/caf\\303\\251.py"\n'
        '--- "a/caf\\303\\251.py"\n+++ "b/caf\\303\\251.py"\n@@ -1 +1 @@\n-a\n+b\n'
    )
    assert parse_diff(diff)[0].path == "café.py"


def test_render_shows_new_line_numbers():
    text = parse_diff(DIFF)[0].render()
    assert "### app/db.py (modified, +3 -1)" in text
    assert '   11 +     query = f"SELECT' in text
    assert '      -     cur.execute("SELECT' in text  # removed line: no number


def test_parse_patch_from_files_api():
    f = parse_patch("x.py", "@@ -1,2 +1,3 @@\n a\n+b\n c", status="modified")
    assert f.added_lines() == {2}
    assert parse_patch("big.bin", None, status="added").is_binary
    assert parse_patch("gone.py", "@@ -1 +0,0 @@\n-x", status="removed").status == "deleted"


def test_select_reviewable_filters_and_budget():
    files = parse_diff(DIFF)
    files += [
        parse_patch("package-lock.json", "@@ -1 +1 @@\n-a\n+b"),
        parse_patch("vendor/lib.py", "@@ -0,0 +1 @@\n+x"),
    ]
    sel = select_reviewable(files, max_lines=1000)
    assert [f.path for f in sel.files] == ["app/db.py", "migrations/001.sql", "docs/my notes.md"]
    reasons = dict(sel.skipped)
    assert reasons["old.txt"] == "deleted"
    assert reasons["src/b.py"] == "no content changes"
    assert reasons["logo.png"].startswith("binary")
    assert reasons["package-lock.json"].startswith("generated")
    assert reasons["vendor/lib.py"].startswith("generated")

    tight = select_reviewable(files, max_lines=5)  # db.py uses 4; the 2-line files no longer fit
    assert [f.path for f in tight.files] == ["app/db.py"]
    assert dict(tight.skipped)["migrations/001.sql"] == "over the PR line budget"


def test_hunk_with_wrong_counts_stops_at_next_hunk():
    """Header claims more lines than exist: stop at the next @@ instead of swallowing it."""
    diff = "diff --git a/z.py b/z.py\n--- a/z.py\n+++ b/z.py\n@@ -1,9 +1,9 @@\n a\n+b\n@@ -20,1 +21,2 @@\n c\n+d\n"
    (f,) = parse_diff(diff)
    assert len(f.hunks) == 2
    assert f.added_lines() == {2, 22}

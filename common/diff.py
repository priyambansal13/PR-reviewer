"""Unified-diff parser with line mapping.

GitHub only accepts an inline review comment on a line that appears in the PR diff, addressed by its line number
in the *new* file (`side: RIGHT`). This module turns `git diff` text into files → hunks → lines, recording for each
line its number in the old and new file, so we know exactly which lines are commentable and can show the model
real line numbers.
"""

import re
from dataclasses import dataclass, field
from fnmatch import fnmatch
from typing import Literal

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")

Status = Literal["added", "modified", "deleted", "renamed"]


@dataclass
class DiffLine:
    kind: Literal["+", "-", " "]
    content: str
    old_no: int | None
    new_no: int | None


@dataclass
class Hunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    section: str = ""  # text after the second @@, usually the enclosing function
    lines: list[DiffLine] = field(default_factory=list)


@dataclass
class FileDiff:
    path: str
    old_path: str | None = None
    status: Status = "modified"
    is_binary: bool = False
    hunks: list[Hunk] = field(default_factory=list)

    @property
    def additions(self) -> int:
        return sum(1 for h in self.hunks for ln in h.lines if ln.kind == "+")

    @property
    def deletions(self) -> int:
        return sum(1 for h in self.hunks for ln in h.lines if ln.kind == "-")

    @property
    def changed_lines(self) -> int:
        return self.additions + self.deletions

    def commentable_lines(self) -> set[int]:
        """New-file line numbers GitHub will accept a RIGHT-side comment on (added + context lines)."""
        return {ln.new_no for h in self.hunks for ln in h.lines if ln.new_no is not None}

    def added_lines(self) -> set[int]:
        return {ln.new_no for h in self.hunks for ln in h.lines if ln.kind == "+" and ln.new_no is not None}

    def render(self) -> str:
        """The diff as the agents see it: every line that exists in the new file carries its real line number."""
        title = self.path if self.status != "renamed" else f"{self.old_path} → {self.path}"
        out = [f"### {title} ({self.status}, +{self.additions} -{self.deletions})"]
        for h in self.hunks:
            out.append(f"@@ -{h.old_start},{h.old_len} +{h.new_start},{h.new_len} @@ {h.section}".rstrip())
            for ln in h.lines:
                num = f"{ln.new_no:>5}" if ln.new_no is not None else "     "
                out.append(f"{num} {ln.kind} {ln.content}")
        return "\n".join(out)


def parse_diff(text: str) -> list[FileDiff]:
    """Parses `git diff` / GitHub `.diff` output (many files)."""
    files: list[FileDiff] = []
    current: FileDiff | None = None
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            current = FileDiff(path=_paths_from_diff_git(line[len("diff --git ") :]))
            files.append(current)
            i += 1
            continue
        if current is None:
            i += 1
            continue

        if line.startswith("@@"):
            i = _read_hunk(lines, i, current)
            continue
        if line.startswith("new file mode"):
            current.status = "added"
        elif line.startswith("deleted file mode"):
            current.status = "deleted"
        elif line.startswith("rename from "):
            current.old_path = _unquote(line[len("rename from ") :])
            current.status = "renamed"
        elif line.startswith("rename to "):
            current.path = _unquote(line[len("rename to ") :])
            current.status = "renamed"
        elif line.startswith("Binary files ") or line == "GIT binary patch":
            current.is_binary = True
        elif line.startswith("+++ "):
            new = _strip_prefix(line[4:], "b/")
            if new is not None:
                current.path = new
        i += 1
    return files


def parse_patch(path: str, patch: str | None, status: str = "modified", old_path: str | None = None) -> FileDiff:
    """Parses one file's hunks as returned in the `patch` field of GitHub's "list PR files" API."""
    f = FileDiff(path=path, old_path=old_path, status=_github_status(status))
    if patch is None:
        f.is_binary = True  # GitHub omits the patch for binary and very large files
        return f
    lines = patch.splitlines()
    i = 0
    while i < len(lines):
        if lines[i].startswith("@@"):
            i = _read_hunk(lines, i, f)
        else:
            i += 1
    return f


def _read_hunk(lines: list[str], i: int, f: FileDiff) -> int:
    """Reads the hunk whose header is lines[i]; returns the index after it.

    The header's line counts decide where the hunk ends, so content like a removed "-- sql comment"
    (which shows up as "--- sql comment") is never mistaken for a file header.
    """
    m = HUNK_RE.match(lines[i])
    if not m:
        return i + 1
    old_start, new_start = int(m[1]), int(m[3])
    old_len = int(m[2]) if m[2] is not None else 1
    new_len = int(m[4]) if m[4] is not None else 1
    hunk = Hunk(old_start, old_len, new_start, new_len, section=m[5].strip())
    f.hunks.append(hunk)

    old_no, new_no = old_start, new_start
    old_left, new_left = old_len, new_len
    i += 1
    while i < len(lines) and (old_left > 0 or new_left > 0):
        line = lines[i]
        if line.startswith("\\"):  # "\ No newline at end of file"
            i += 1
            continue
        tag, content = (line[0], line[1:]) if line else (" ", "")  # some tools strip a lone context space
        if tag == "+":
            hunk.lines.append(DiffLine("+", content, None, new_no))
            new_no += 1
            new_left -= 1
        elif tag == "-":
            hunk.lines.append(DiffLine("-", content, old_no, None))
            old_no += 1
            old_left -= 1
        elif tag == " ":
            hunk.lines.append(DiffLine(" ", content, old_no, new_no))
            old_no += 1
            new_no += 1
            old_left -= 1
            new_left -= 1
        else:
            break  # malformed; stop this hunk rather than misnumber everything after it
        i += 1
    # a trailing "\ No newline" belongs to this hunk
    while i < len(lines) and lines[i].startswith("\\"):
        i += 1
    return i


def _github_status(status: str) -> Status:
    return {"added": "added", "removed": "deleted", "renamed": "renamed"}.get(status, "modified")  # type: ignore[return-value]


def _strip_prefix(raw: str, prefix: str) -> str | None:
    raw = _unquote(raw.rstrip("\t"))
    if raw == "/dev/null":
        return None
    return raw[len(prefix) :] if raw.startswith(prefix) else raw


def _paths_from_diff_git(rest: str) -> str:
    """Best-effort new path from `a/X b/Y`. ---/+++ or rename lines override it when present."""
    if rest.startswith('"'):
        # quoted paths: "a/x y" "b/x y"
        parts = re.findall(r'"(?:[^"\\]|\\.)*"|\S+', rest)
        return _strip_prefix(parts[-1], "b/") or ""
    # unquoted: when old == new the string is exactly "a/P b/P", so split in the middle (P may contain spaces)
    half = (len(rest) - 1) // 2
    if rest.startswith("a/") and rest[half : half + 3] == " b/" and rest[2:half] == rest[half + 3 :]:
        return rest[half + 3 :]
    idx = rest.rfind(" b/")
    return rest[idx + 3 :] if idx != -1 else rest


def _unquote(s: str) -> str:
    """Git C-quotes paths with special characters: "a/caf\\303\\251.txt" -> a/café.txt"""
    if len(s) < 2 or not (s.startswith('"') and s.endswith('"')):
        return s
    raw = s[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape").encode("latin-1")
    return raw.decode("utf-8", "replace")


# --- choosing what to send to the model ---------------------------------------------------------------------------

SKIP_GLOBS = (
    # lockfiles
    "*package-lock.json", "*yarn.lock", "*pnpm-lock.yaml", "*poetry.lock", "*uv.lock", "*Pipfile.lock",
    "*Cargo.lock", "*Gemfile.lock", "*composer.lock", "*go.sum",
    # generated / bundled
    "*.min.js", "*.min.css", "*.map", "*_pb2.py", "*_pb2_grpc.py", "*.pb.go", "*.snap", "*.generated.*",
    # vendored / build output
    "vendor/*", "*/vendor/*", "node_modules/*", "*/node_modules/*", "dist/*", "build/*",
    # assets
    "*.svg", "*.png", "*.jpg", "*.jpeg", "*.gif", "*.ico", "*.pdf", "*.woff", "*.woff2", "*.ttf",
)  # fmt: skip

MAX_FILE_CHANGED_LINES = 1500


@dataclass
class Selection:
    files: list[FileDiff]
    skipped: list[tuple[str, str]]  # (path, reason)

    @property
    def changed_lines(self) -> int:
        return sum(f.changed_lines for f in self.files)


def select_reviewable(files: list[FileDiff], max_lines: int) -> Selection:
    """Drops files a reviewer shouldn't spend tokens on and stops once the line budget is used up."""
    kept: list[FileDiff] = []
    skipped: list[tuple[str, str]] = []
    budget = max_lines
    for f in files:
        reason = _skip_reason(f)
        if reason is None and f.changed_lines > budget:
            reason = "over the PR line budget"
        if reason:
            skipped.append((f.path, reason))
            continue
        kept.append(f)
        budget -= f.changed_lines
    return Selection(kept, skipped)


def _skip_reason(f: FileDiff) -> str | None:
    if f.status == "deleted":
        return "deleted"
    if f.is_binary:
        return "binary or too large for GitHub to show"
    if not f.hunks:
        return "no content changes"
    if any(fnmatch(f.path, g) for g in SKIP_GLOBS):
        return "generated, vendored, lockfile or asset"
    if f.changed_lines > MAX_FILE_CHANGED_LINES:
        return f"more than {MAX_FILE_CHANGED_LINES} changed lines"
    if f.additions == 0:
        return "only deletions"
    return None

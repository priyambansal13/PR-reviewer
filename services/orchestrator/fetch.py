"""Step 1 of a review: get the exact diff for the run's commits and decide which files to review."""

import logging
from dataclasses import dataclass

from common.diff import FileDiff, Selection, parse_diff, parse_patch, select_reviewable
from common.github import DiffUnavailable, GitHubClient

log = logging.getLogger("orchestrator")


class Superseded(Exception):
    """The PR has a newer head commit than this run; that commit has (or will get) its own run."""


@dataclass
class FetchedDiff:
    files: list[FileDiff]
    selection: Selection
    source: str  # "compare" | "files-api"


async def fetch_diff(
    gh: GitHubClient, repo: str, number: int, base_sha: str, head_sha: str, max_lines: int
) -> FetchedDiff:
    pull = await gh.get_pull(repo, number)
    current_head = pull["head"]["sha"]
    if current_head != head_sha:
        raise Superseded(f"PR head moved to {current_head[:7]}")

    try:
        files = parse_diff(await gh.get_compare_diff(repo, base_sha, head_sha))
        source = "compare"
    except DiffUnavailable as exc:
        log.info("%s#%d: %s — using the per-file API", repo, number, exc)
        files = [
            parse_patch(f["filename"], f.get("patch"), f.get("status", "modified"), f.get("previous_filename"))
            for f in await gh.list_pull_files(repo, number)
        ]
        source = "files-api"

    return FetchedDiff(files, select_reviewable(files, max_lines), source)

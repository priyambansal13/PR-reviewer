"""Review a local patch file with the real agents — no GitHub, no database. Handy for tuning prompts.

git diff main > /tmp/change.patch
uv run python -m services.orchestrator.cli /tmp/change.patch --title "Add login"
"""

import argparse
import asyncio
import logging
from pathlib import Path

from common.config import get_settings
from common.diff import parse_diff, select_reviewable
from common.llm import make_llm
from services.orchestrator import report
from services.orchestrator.graph.graph import build_review_graph
from services.orchestrator.graph.schemas import ReviewContext


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("patch", type=Path)
    parser.add_argument("--title", default="Local change")
    parser.add_argument("--details", action="store_true", help="print full explanations and suggestions")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    settings = get_settings()
    sel = select_reviewable(parse_diff(args.patch.read_text()), settings.max_diff_lines)
    print(f"Reviewing {len(sel.files)} file(s), {sel.changed_lines} changed lines; skipped {len(sel.skipped)}")
    ctx = ReviewContext(repo="local", number=0, title=args.title, files=sel.files)
    state = await build_review_graph(make_llm(settings)).ainvoke({"ctx": ctx})

    print(f"\n{report.VERDICT[state['verdict']]} — {report.headline(state['findings'])}\n")
    print(state["summary"], "\n")
    print(report.findings_table(state["findings"]))
    print("\nmerge:", state["merge_stats"])
    print(report.run_footer(state["agent_runs"], settings.openai_model, sel.skipped))
    if args.details:
        print("\n" + report.details(state["findings"]))


if __name__ == "__main__":
    asyncio.run(main())

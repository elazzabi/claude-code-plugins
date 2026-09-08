#!/usr/bin/env python3
"""Capture and replay one audited review run's planner inputs.

A fixture holds everything ``build_dispatch_plan`` reads for one run: the
PR metadata the review context carried, the raw commit log, the changed
files, the diffstat, the full patch, the repository identity text, and
the PR template at the range head. ``replay`` feeds them back with the
planner's two git-reading functions patched, so the planner's decisions
on a real run are a deterministic test rather than a re-audit.

Capture once, from a clone that holds the range::

    python3 plugins/pirategoat-tools/tests/helpers/triage_run_fixture.py \
        --clone ~/Work/a8c/.duplicates/woocommerce-2 \
        --git-range 434650beaba1a2c594c12a1262b9b139551f8df2..fix/53136-platform-selector-mobile-keyboard \
        --review-context <run-dir>/review-context.json \
        --name e582-woocommerce-53136

The commit log keeps its NUL commit separators so trailer stripping can
work per commit; ``Claude-Session:`` values are redacted because they are
account-scoped URLs, not review inputs.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

TESTS_DIR = Path(__file__).resolve().parent.parent
FIXTURES_DIR = TESTS_DIR / "fixtures" / "triage-runs"
SCRIPTS_DIR = TESTS_DIR.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
from review import plan_dispatch  # noqa: E402
from review.triage_sources import (  # noqa: E402
    PR_TEMPLATE_DIRS,
    PR_TEMPLATE_PATHS,
    strip_commit_trailers,
)

from helpers.review_run_fixture import FIXTURE_NAMES, _SESSION_URL_RE  # noqa: E402

FIXTURE_SCHEMA = 1


def _git(clone, *args):
    return subprocess.run(
        ["git", "-C", str(clone), "-c", "core.quotepath=false", *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _pr_template_at(clone, head):
    """The PR template text the planner would read at ``head``: every
    file `triage_sources.find_pr_template` looks for, read from the tree
    rather than the working copy, concatenated in the same order."""
    paths = list(PR_TEMPLATE_PATHS)
    for template_dir in PR_TEMPLATE_DIRS:
        try:
            listing = _git(clone, "ls-tree", "--name-only", head, f"{template_dir}/")
        except subprocess.CalledProcessError:
            continue
        paths.extend(sorted(
            entry for entry in listing.splitlines() if entry.lower().endswith(".md")
        ))
    parts = []
    for path in dict.fromkeys(paths):
        try:
            parts.append(_git(clone, "show", f"{head}:{path}"))
        except subprocess.CalledProcessError:
            continue
    return "\n".join(parts)


def capture(clone, git_range, review_context_path, name):
    """Read one run's planner inputs from ``clone`` and the run's context."""
    context = json.loads(Path(review_context_path).read_text(encoding="utf-8"))
    head = git_range.split("..", 1)[1]
    cwd = os.getcwd()
    os.chdir(clone)  # get_diffstat and get_repository_identity read the cwd
    try:
        diffstat = plan_dispatch.get_diffstat(git_range)
        repository_text = plan_dispatch.get_repository_identity()
    finally:
        os.chdir(cwd)
    pr = context.get("pr", {})
    return {
        "schema": FIXTURE_SCHEMA,
        "name": name,
        "git_range": git_range,
        "head_sha": _git(clone, "rev-parse", head).strip(),
        "pr": {
            "title": pr.get("title") or "",
            "body": pr.get("body") or "",
            "labels": [l for l in pr.get("labels", []) if isinstance(l, str)],
        },
        "head_ref": context.get("git", {}).get("head_ref", ""),
        "linked_issue_titles": [
            issue["title"]
            for issue in context.get("linked_issues_details", [])
            if isinstance(issue, dict) and issue.get("title")
        ],
        "commit_log": _SESSION_URL_RE.sub(
            r"\1<redacted>", _git(clone, "log", "--format=%s%n%b%x00", git_range)
        ),
        "changed_files": _git(clone, "diff", "--name-only", git_range).splitlines(),
        "diffstat": diffstat,
        "patch": _git(clone, "diff", "--function-context", git_range),
        "repository_text": repository_text,
        "pr_template": _pr_template_at(clone, head),
    }


def load(name):
    return json.loads((FIXTURES_DIR / f"{name}.json").read_text(encoding="utf-8"))


def slice_patch(patch, files):
    """The captured patch restricted to ``files`` (all of it when None/empty)."""
    wanted = set(files or ())
    blocks = re.split(r"(?m)^(?=diff --git )", patch)
    kept = []
    for block in blocks:
        if not block:
            continue
        header = re.match(r"diff --git a/(.*?) b/(.*?)\n", block)
        if not wanted or (header and (header.group(1) in wanted or header.group(2) in wanted)):
            kept.append(block)
    return "".join(kept)


def _review_context(fixture):
    return {
        "pr": dict(fixture["pr"]),
        "git": {"head_ref": fixture["head_ref"]},
        "linked_issues_details": [
            {"title": title} for title in fixture["linked_issue_titles"]
        ],
    }


def replay(fixture, *, registry=None, quick=False):
    """Run the planner on a fixture with its two git readers patched."""
    with patch.object(
        plan_dispatch,
        "get_diff_text",
        side_effect=lambda git_range, files=None: slice_patch(fixture["patch"], files),
    ), patch.object(
        plan_dispatch, "get_repository_identity", return_value=fixture["repository_text"]
    ):
        return plan_dispatch.build_dispatch_plan(
            mode="pr",
            git_range=fixture["git_range"],
            output_dir="<replay>",
            changed_files=list(fixture["changed_files"]),
            registry=registry,
            # The same cleaning get_commit_messages applies to live git
            # output; the fixture holds the raw log so the seam is exercised.
            commit_messages=strip_commit_trailers(fixture["commit_log"]),
            diffstat=fixture["diffstat"],
            review_context=_review_context(fixture),
            quick=quick,
            pr_template=fixture["pr_template"],
        )


def statuses(plan):
    return {entry["name"]: (entry["status"], entry["reason"]) for entry in plan["agents"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--clone", required=True)
    parser.add_argument("--git-range", required=True)
    parser.add_argument("--review-context", required=True)
    parser.add_argument("--name", required=True)
    args = parser.parse_args()
    fixture = capture(args.clone, args.git_range, args.review_context, args.name)
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    out = FIXTURES_DIR / f"{args.name}.json"
    out.write_text(json.dumps(fixture, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size} bytes, {len(fixture['changed_files'])} files)")


if __name__ == "__main__":
    main()

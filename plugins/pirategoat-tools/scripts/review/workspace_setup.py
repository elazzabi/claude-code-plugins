#!/usr/bin/env python3
"""
Setup Workspace — deterministic workspace preparation for PR review.

Records the current branch, stashes dirty state if needed, and checks out
the PR branch. Outputs a JSON result to stdout.

Output: JSON to stdout with original_branch, stash_ref, was_dirty, checkout_ok.
"""

import argparse
import json
import subprocess
import sys


# ---------------------------------------------------------------------------
# Shell helpers
# ---------------------------------------------------------------------------

# `gh pr checkout` fetches the PR head; on a monorepo that alone can pass
# the 30 s the local git calls get.
CHECKOUT_TIMEOUT_SECONDS = 300
# What the pipeline allows this whole script: the checkout plus the git
# calls around it (dirty check, stash, branch). The pipeline must never
# time out first, or the checkout keeps changing the tree after the
# recovery metadata (original branch, stash ref) was lost.
SETUP_TIMEOUT_SECONDS = CHECKOUT_TIMEOUT_SECONDS + 60
_GIT_TIMEOUT_SECONDS = 30


def _run(cmd, timeout):
    """The one subprocess seam. Raises what subprocess raises."""
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _run_cmd(cmd):
    """Run a command and return stdout, or None on failure."""
    try:
        r = _run(cmd, _GIT_TIMEOUT_SECONDS)
        if r.returncode == 0:
            return r.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _checkout_failure(gh_cmd, pr_number):
    """Run the checkout; return None on success, else the reason.

    The reason carries gh's last stderr line and the exit status (or the
    timeout), because the step-2 briefing repeats it verbatim and the
    orchestrator acts on it; a bare "failed" invites a guess.
    """
    try:
        r = _run([gh_cmd, "pr", "checkout", str(pr_number)], CHECKOUT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return f"timed out after {CHECKOUT_TIMEOUT_SECONDS}s"
    except FileNotFoundError:
        return f"{gh_cmd} is not installed"
    if r.returncode == 0:
        return None
    lines = [line.strip() for line in (r.stderr or "").splitlines() if line.strip()]
    return f"{lines[-1]} (exit {r.returncode})" if lines else f"exit {r.returncode}"


def resolve_gh_cmd():
    """Detect whether to use 'gh' or 'ghe' CLI."""
    origin = _run_cmd(["git", "remote", "get-url", "origin"]) or ""
    if "a8c.com" in origin or "automattic.com" in origin:
        return "ghe"
    return "gh"


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def setup_workspace(pr_number, gh_cmd="gh"):
    """Set up workspace for PR review.

    1. Records the current branch
    2. Checks for dirty state
    3. Stashes if dirty (with -u for untracked files)
    4. Checks out the PR branch

    Returns a dict with original_branch, stash_ref, was_dirty, checkout_ok.
    On failure, includes an 'error' key.
    """
    result = {
        "original_branch": "unknown",
        "stash_ref": None,
        "was_dirty": False,
        "checkout_ok": False,
    }

    # 1. Record current branch
    branch = _run_cmd(["git", "branch", "--show-current"])
    if branch is not None:
        result["original_branch"] = branch

    # 2. Check for dirty state
    status = _run_cmd(["git", "status", "--porcelain"])
    if status is not None and status != "":
        result["was_dirty"] = True

        # 3. Stash dirty state (including untracked files)
        stash_ok = _run_cmd(
            ["git", "stash", "push", "-u", "-m", "pr-review-auto-stash"]
        )
        if stash_ok is not None:
            # Capture stash ref from stash list
            stash_list = _run_cmd(["git", "stash", "list"])
            if stash_list:
                # First line contains the most recent stash ref
                first_line = stash_list.split("\n")[0]
                # Extract stash@{N} from the line
                colon_idx = first_line.find(":")
                if colon_idx > 0:
                    result["stash_ref"] = first_line[:colon_idx]

    # 4. Check out the PR branch
    reason = _checkout_failure(gh_cmd, pr_number)
    if reason is None:
        result["checkout_ok"] = True
    else:
        result["error"] = f"Failed to checkout PR #{pr_number}: {reason}"

    return result


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Set up workspace for PR review"
    )
    parser.add_argument(
        "--pr-number", required=True,
        help="PR number to check out"
    )
    parser.add_argument(
        "--gh-cmd", default=None,
        help="GitHub CLI command (gh or ghe). Auto-detected if omitted."
    )
    args = parser.parse_args()

    gh_cmd = args.gh_cmd or resolve_gh_cmd()
    result = setup_workspace(args.pr_number, gh_cmd=gh_cmd)
    print(json.dumps(result))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Gather Review Context — unified Ring 1 context for all review entry points.

Supports --pr-number (PR review) and --branch (branch review, with optional
--incremental). Gap-filling: reads whatever review context already exists,
fills in what is missing, and writes the complete artifact.

Output: the review-context boundary artifact with snake_case keys.
"""

import argparse
import json
import os
import re
import subprocess
import sys


# Host-context resolver import is best-effort — the scripts/ directory must
# be importable (and ahead of any shadowing test packages like tests/hosts/)
# for this to work. We attempt it once at module load so _fill_host_context()
# does not mutate sys.path on every call.
_HOSTS_CHAIN = None
_scripts_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _scripts_dir in sys.path:
    sys.path.remove(_scripts_dir)
sys.path.insert(0, _scripts_dir)
try:
    from hosts.chain import ResolverChain as _HOSTS_CHAIN  # noqa: E402
except ImportError:
    _HOSTS_CHAIN = None

from git_paths import FULL_SHA_RE
from review.orchestration import baseline_path
from review.run_paths import artifact_path

# Repo-contributed review-config reader (best-effort, same rationale as the host
# chain above). Loaded from file so it works whether context.py runs as a script
# or is imported under a test's import machinery.
_REVIEW_CONFIG_LOADER = None
try:
    import importlib.util as _ilu  # noqa: E402

    _rc_spec = _ilu.spec_from_file_location(
        "review_config",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "review_config.py"),
    )
    _rc_mod = _ilu.module_from_spec(_rc_spec)
    _rc_spec.loader.exec_module(_rc_mod)
    _REVIEW_CONFIG_LOADER = _rc_mod.load_review_config
except Exception:  # noqa: BLE001 — review must continue without repo config
    _REVIEW_CONFIG_LOADER = None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

KNOWN_AI_REVIEWERS = {
    "coderabbitai", "github-actions", "copilot", "codeclimate",
    "sonarcloud", "deepsource-autofix", "snyk-bot", "dependabot", "renovate",
}


# ---------------------------------------------------------------------------
# Deterministic helpers
# ---------------------------------------------------------------------------

def categorize_reviewer(login: str) -> str:
    """Categorize a reviewer login as human, bot, or ai."""
    if login.endswith("[bot]"):
        return "bot"
    base = login.removesuffix("[bot]")
    if base in KNOWN_AI_REVIEWERS:
        return "ai"
    return "human"


def extract_linked_issues(body: str) -> list:
    """Extract Linear IDs and GitHub issue refs from PR body."""
    if not body:
        return []
    ids = set()
    for m in re.finditer(r'\b([A-Z]+-\d+)\b', body):
        ids.add(m.group(1))
    for m in re.finditer(r'(?:closes?|fixes?|resolves?|refs?)\s+#(\d+)', body, re.IGNORECASE):
        ids.add(m.group(1))
    return sorted(ids)


def bucket_pr_size(lines: int) -> str:
    """Categorize PR size by total changed lines."""
    if lines <= 30:
        return "tiny"
    if lines <= 200:
        return "small"
    if lines <= 700:
        return "medium"
    if lines <= 2000:
        return "large"
    if lines <= 5000:
        return "huge"
    return "vlad-sized"


def safe_dirname(name: str) -> str:
    """Replace anything not alphanumeric, dot, underscore, or hyphen."""
    return re.sub(r'[^a-zA-Z0-9._-]', '-', name).strip('-')


# ---------------------------------------------------------------------------
# Shell helpers
# ---------------------------------------------------------------------------

def _run_cmd(cmd, cwd=None, timeout=30, strip=True):
    """Run a shell command and return stdout, or None on failure.

    `strip=False` returns stdout verbatim, for NUL-delimited output whose
    first field may legitimately begin with whitespace. Decoding is
    lossless: a git-valid path need not be UTF-8, and surrogateescape
    keeps such bytes round-trippable through JSON and the filesystem
    instead of raising before the context is written.
    """
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, errors="surrogateescape",
            cwd=cwd, timeout=timeout,
        )
        if r.returncode == 0:
            return r.stdout.strip() if strip else r.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


FETCH_TIMEOUT_SECONDS = 120


def _fetch_base_ref(base):
    """Fetch origin/<base> so merge-base sees the remote's current tip.

    The local remote-tracking ref is never updated by `gh pr checkout`,
    which fetches only the head. A PR branch that merged a newer base
    than the local ref then resolves to an older merge-base and the range
    picks up everything the base did in between (run 6e6a: 8 files
    reviewed as 91). One fetch per run closes that.
    """
    ref = f"origin/{base}"
    # A shallow clone can fetch the base and still have no merge-base,
    # because the head's own history stops at the shallow boundary. Record
    # the fact so the briefing can name the remedy instead of the range
    # silently failing to resolve.
    shallow = _run_cmd(["git", "rev-parse", "--is-shallow-repository"])
    # Explicit refspec, not a bare branch name: `git fetch origin <base>`
    # updates the remote-tracking ref only when the clone's fetch refspec
    # covers it. A single-branch clone (bot and CI checkouts, a colleague's
    # stacked branch) exits 0, writes FETCH_HEAD, and leaves origin/<base>
    # absent. The +refs/heads form always writes the tracking ref and never
    # touches a local branch or the working tree. "fetched" means the ref
    # resolves afterwards, not merely that git exited 0.
    fetched = _run_cmd(
        ["git", "fetch", "--no-tags", "origin", f"+refs/heads/{base}:refs/remotes/origin/{base}"],
        timeout=FETCH_TIMEOUT_SECONDS,
    )
    sha = _run_cmd(["git", "rev-parse", "--verify", ref])
    return {
        "ref": ref,
        "status": "fetched" if fetched is not None and sha else "failed",
        "sha": sha,
        "shallow": shallow == "true" if shallow in ("true", "false") else None,
    }


def _detect_default_branch():
    """Return the remote's default branch, or None when it cannot be told.

    The remote is asked first: the clone-local origin/HEAD is written at
    clone time and no ordinary fetch refreshes it, so a repository that
    moved its default branch keeps reporting the old one from the cache.
    Offline, the cached symbolic ref is the fallback so branch mode keeps
    a range. Callers decide what an unknown default means: PR mode records
    nothing, branch mode falls back to "main" for the range. Guessing here
    would flag every review in a trunk repository as targeting a
    non-default base.
    """
    remote = _run_cmd(["git", "ls-remote", "--symref", "origin", "HEAD"])
    for line in (remote or "").splitlines():
        if line.startswith("ref: refs/heads/") and line.rstrip().endswith("\tHEAD"):
            return line[len("ref: refs/heads/"):].split("\t", 1)[0]
    ref = _run_cmd(["git", "symbolic-ref", "refs/remotes/origin/HEAD"])
    return ref.replace("refs/remotes/origin/", "") if ref else None


def _resolve_range(git, base, head):
    """Fetch the base tip, then record the merge base, the range and the
    foreign merges against it. Nothing is recorded when there is no merge
    base; the base-fetch record says why."""
    git["base_fetch"] = _fetch_base_ref(base)
    merge_base = _run_cmd(["git", "merge-base", git["base_fetch"]["ref"], head])
    if merge_base:
        git["merge_base"] = merge_base
        git.setdefault("git_range", f"{merge_base}..{head}")
        _detect_foreign_merges(git, head)


def _detect_foreign_merges(git, head):
    """List merge commits in the range whose second parent is not on the base.

    After the base fetch, a merge of the base itself has an ancestor second
    parent and is not listed. A merge of a sibling branch is listed: its
    files are legitimately part of the range, but the orchestrator should
    know they were merged in rather than authored on this branch.

    `head` is the range head the caller computed the merge-base against,
    which in PR mode is the PR branch rather than whatever is checked out.

    Records `git["foreign_merges"]` as a list when the scan ran (empty
    means none found) and as None when it could not run, so a failed scan
    is never read as a clean one.
    """
    merge_base = git.get("merge_base")
    base_ref = git.get("base_ref")
    git["foreign_merges"] = None
    if not merge_base or not base_ref:
        return None
    # Only a freshly fetched base can classify a merge. Against a stale
    # origin/<base>, a merge of the real base's newer commits looks like
    # work from another branch, so a run whose fetch failed claims nothing.
    if (git.get("base_fetch") or {}).get("status") != "fetched":
        return None
    # Commits reachable from the head but not from origin/<base>. A merge's
    # second parent is foreign exactly when it is in this set, so one
    # command answers for every merge instead of one `--is-ancestor` probe
    # per merge, each with its own timeout that would read as "foreign".
    off_base = _run_cmd(["git", "rev-list", head, f"^origin/{base_ref}"])
    if off_base is None:
        return None
    raw = _run_cmd(["git", "rev-list", "--merges", "--parents", f"{merge_base}..{head}"])
    if raw is None:
        return None
    off_base_set = set(off_base.split())
    merges = []
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        sha, second_parent = parts[0], parts[2]
        if second_parent in off_base_set:
            merges.append({"sha": sha, "second_parent": second_parent})
    git["foreign_merges"] = merges
    return merges


def _resolve_repo_root(path):
    """Return the git root for path when available, otherwise path itself."""
    absolute = os.path.abspath(path)
    cwd = absolute if os.path.isdir(absolute) else os.path.dirname(absolute)
    root = _run_cmd(["git", "rev-parse", "--show-toplevel"], cwd=cwd)
    return root or absolute


def resolve_gh_cmd():
    """Detect whether to use 'gh' or 'ghe' CLI."""
    origin = _run_cmd(["git", "remote", "get-url", "origin"]) or ""
    if "a8c.com" in origin or "automattic.com" in origin:
        return "ghe"
    return "gh"


def _read_run_config(output_dir):
    """Read the run config used to resolve target-level review state."""
    try:
        with open(artifact_path(output_dir, "run_config")) as f:
            config = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return config if isinstance(config, dict) else {}


# ---------------------------------------------------------------------------
# Fill helpers — each only runs external commands when fields are missing
# ---------------------------------------------------------------------------

def _fill_git_context(ctx, pr_number=None, branch=False, incremental=False,
                      git_range=None, config=None):
    """Fill git context fields (merge_base, head_ref, changed_files, etc.)."""
    git = ctx.setdefault("git", {})

    if git_range:
        # Explicit range provided. Match on "..." before ".." — a naive
        # two-dot split turns "main...topic" into head_ref ".topic". An
        # omitted endpoint stays unset so downstream resolution defaults
        # to HEAD, matching git's own range semantics.
        git.setdefault("git_range", git_range)
        separator = "..." if "..." in git_range else ".."
        base_ref, found, head_ref = git_range.partition(separator)
        if found:
            if base_ref.strip():
                git.setdefault("merge_base", base_ref.strip())
            if head_ref.strip():
                git.setdefault("head_ref", head_ref.strip())
    elif pr_number and "merge_base" not in git:
        gh_cmd = ctx.get("github_cli_command", "gh")
        # Get PR base info
        pr_info = _run_cmd([gh_cmd, "pr", "view", str(pr_number), "--json",
                           "baseRefName,headRefName", "-q",
                           ".baseRefName + \" \" + .headRefName"])
        if pr_info:
            parts = pr_info.split()
            if len(parts) == 2:
                git.setdefault("base_ref", parts[0])
                git.setdefault("head_ref", parts[1])

        # Recorded so the briefing can say when the base is not the default
        # branch (a stacked PR or a release line). Absent means unknown.
        default_branch = _detect_default_branch()
        if default_branch:
            git.setdefault("default_branch", default_branch)

        _resolve_range(git, git.get("base_ref", "main"), git.get("head_ref", "HEAD"))
    elif branch and "merge_base" not in git:
        head = _run_cmd(["git", "branch", "--show-current"]) or "HEAD"
        git.setdefault("head_ref", head)

        if incremental:
            # Baseline migration (rule 26): read new format first, fall back to legacy
            output_base = ctx.get("output", {}).get("directory", ".")
            baseline_file = baseline_path(config or {}, output_base)
            legacy_file = os.path.join(
                os.path.dirname(baseline_file), ".review-state.json"
            )
            state_file = None
            if os.path.isfile(baseline_file):
                state_file = baseline_file
            elif os.path.isfile(legacy_file):
                state_file = legacy_file  # migration read
            if state_file:
                with open(state_file) as f:
                    state = json.load(f)
                last_sha = state.get("last_reviewed_sha")
                if last_sha:
                    # Validate the SHA is a valid ancestor of HEAD.
                    # After rebases or force-pushes, the persisted SHA may
                    # no longer exist in the current history.
                    is_ancestor = _run_cmd(
                        ["git", "merge-base", "--is-ancestor", last_sha, "HEAD"]
                    )
                    if is_ancestor is not None:  # exit code 0 = is ancestor
                        git.setdefault("merge_base", last_sha)
                        git.setdefault("git_range", f"{last_sha}..HEAD")
                    else:
                        # SHA is not an ancestor — history was rewritten.
                        # Fall through to full-branch range detection below.
                        print(
                            f"WARNING: last_reviewed_sha {last_sha[:12]} is not an "
                            f"ancestor of HEAD (history rewritten?). "
                            f"Falling back to full-branch review.",
                            file=sys.stderr,
                        )

        if "merge_base" not in git:
            default_branch = _detect_default_branch() or "main"
            git.setdefault("base_ref", default_branch)
            _resolve_range(git, default_branch, "HEAD")

    # Reviewed head as a commit SHA. Step 1 resolves HEAD before any PR
    # checkout happens at step 2, so the durable run identity must be
    # re-resolved here, after workspace setup. The telemetry manifest
    # refresh only accepts full SHAs, which is exactly what this provides.
    # Bot-precomputed context already carries head_sha and is preserved.
    if "head_sha" not in git:
        # ^{commit} peels annotated tag endpoints to their commit — plain
        # rev-parse would record the tag object id.
        head_ref = git.get("head_ref") or "HEAD"
        head_sha = _run_cmd(
            ["git", "rev-parse", "--verify", f"{head_ref}^{{commit}}"]
        )
        if head_sha:
            git["head_sha"] = head_sha

    # Changed files
    if "changed_files" not in git and git.get("git_range"):
        # NUL-delimited so every path is spelled as GitHub spells it: the
        # newline form C-quotes non-ASCII, backslashes and control
        # characters ("caf\303\251.php"), which no consumer can open and
        # which GitHub's decoded file list can never equal.
        # Rename detection pinned on: GitHub lists a rename once, at its new
        # path, and so does git with renames on. A clone with diff.renames
        # off would list the old path too, a file that no longer exists at
        # the head and that the scope comparison would call an extra file.
        files_output = _run_cmd(
            ["git", "-c", "diff.renames=true", "diff", "--name-only", "-z", git["git_range"]],
            strip=False,
        )
        if files_output:
            git["changed_files"] = [f for f in files_output.split("\0") if f]

    # Diff stats
    if "diff_stats" not in git and git.get("git_range"):
        stats = _run_cmd(["git", "diff", "--stat", git["git_range"]])
        if stats:
            git["diff_stats"] = stats

    # Commit count
    if "commit_count" not in git and git.get("git_range"):
        count = _run_cmd(["git", "rev-list", "--count", git["git_range"]])
        if count:
            try:
                git["commit_count"] = int(count)
            except ValueError:
                pass


def _fill_pr_metadata(ctx):
    """Fill PR metadata fields from gh pr view."""
    pr = ctx.get("pr", {})
    gh_cmd = ctx.get("github_cli_command", "gh")
    pr_number = pr.get("number")
    if not pr_number:
        return

    fields = ("title,author,state,isDraft,baseRefName,headRefName,body,labels,url,"
              "baseRefOid,headRefOid,changedFiles,files")
    raw = _run_cmd([gh_cmd, "pr", "view", str(pr_number), "--json", fields])
    if not raw:
        return

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return

    pr.setdefault("title", data.get("title", ""))
    author = data.get("author", {})
    pr.setdefault("author", author.get("login", "") if isinstance(author, dict) else str(author))
    pr.setdefault("state", data.get("state", ""))
    pr.setdefault("is_draft", data.get("isDraft", False))
    pr.setdefault("base_ref_name", data.get("baseRefName", ""))
    pr.setdefault("head_ref_name", data.get("headRefName", ""))
    pr.setdefault("body", data.get("body", ""))
    labels = data.get("labels", [])
    pr.setdefault("labels", [l.get("name", l) if isinstance(l, dict) else l for l in labels])
    pr.setdefault("url", data.get("url", ""))
    if data.get("baseRefOid"):
        pr.setdefault("base_ref_oid", data["baseRefOid"])
    if data.get("headRefOid"):
        pr.setdefault("head_ref_oid", data["headRefOid"])
    if isinstance(data.get("changedFiles"), int):
        pr.setdefault("changed_files_count", data["changedFiles"])
        files = data.get("files")
        if isinstance(files, list):
            paths = [f.get("path") for f in files if isinstance(f, dict) and f.get("path")]
            # gh returns at most the first hundred files. A partial list
            # cannot be compared as a set, so it is recorded only when it
            # accounts for every file GitHub counts.
            if len(paths) == data["changedFiles"]:
                pr.setdefault("changed_files_paths", paths)


def _merge_base_is_githubs(merge_base, base_oid, head_sha):
    """Whether the range's merge base is where GitHub's base meets the head.

    GitHub's `baseRefOid` is the base branch as of the PR's last
    synchronisation, not the fork point: a branch forked before the base
    advanced has a merge base behind it and is still the same range, so
    comparing the two OIDs directly would call that a mismatch. The
    identity compared is `git merge-base <baseRefOid> <head>`, which is
    the base GitHub's own diff is computed from. The fetched base TIP is
    not compared either, because it moves past the recorded base whenever
    the base branch advances.

    None, never False, when the answer is unknown: the merge base is not
    a full SHA (an explicit `--git-range` leaves the range's left operand
    verbatim), GitHub recorded no base, or the recorded base is not in the
    local repository (a stale base fetch, a shallow clone).
    """
    if not (isinstance(merge_base, str) and FULL_SHA_RE.fullmatch(merge_base)):
        return None
    if not (base_oid and head_sha):
        return None
    github_base = _run_cmd(["git", "merge-base", base_oid, head_sha])
    if github_base is None:
        return None
    return github_base == merge_base


def _check_scope_against_github(ctx):
    """Record whether the local range agrees with GitHub's view of the PR.

    The witness is GitHub's file list when gh returned all of it; then the
    two sets are compared path for path. When the list is truncated, the
    counts are compared instead, and equal counts with unverified
    identities are recorded as "count_only", never as a match. Either way
    a range identity known not to be GitHub's is a mismatch: two ranges
    can hold the same files with different hunks (a stale base carries
    its own changes to the PR's files) or the same count with different
    files.

    The identities are the reviewed head against GitHub's `headRefOid`,
    and the range's merge base against where GitHub's `baseRefOid` meets
    the head (see `_merge_base_is_githubs`). A head that is not GitHub's
    is a mismatch on its own: the checkout predates the author's latest
    push, which no file set can show. Both flags are recorded as facts
    whatever decided the status.

    The check never blocks: bot mode, gh-less runs and explicit ranges
    must keep working, so the briefing warns and the manifest records.
    """
    pr = ctx.get("pr", {})
    git = ctx.setdefault("git", {})
    local_files = git.get("changed_files")
    local_count = len(local_files) if isinstance(local_files, list) else None
    github_count = pr.get("changed_files_count")
    github_paths = pr.get("changed_files_paths")
    head_sha = git.get("head_sha")
    head_oid = pr.get("head_ref_oid")
    head_matches = (head_sha == head_oid) if head_sha and head_oid else None
    base_matches = _merge_base_is_githubs(
        git.get("merge_base"), pr.get("base_ref_oid"), head_sha,
    )
    extra_local = None
    missing_local = None
    if isinstance(github_paths, list) and local_count is not None:
        extra_local = sorted(set(local_files) - set(github_paths))
        missing_local = sorted(set(github_paths) - set(local_files))

    # A head or base that is not GitHub's is a mismatch on its own, with
    # or without file lists: equal file sets can hide different hunks,
    # since a range computed from an older base carries the base's own
    # changes to files the PR also touches. A complete file list decides
    # everything else. Missing counts alone are "unavailable", never a
    # mismatch: "no local file list" is not "the range is inflated", and
    # collapsing them would flag every run whose range failed to resolve.
    if head_matches is False or base_matches is False:
        status = "mismatch"
    elif extra_local is not None:
        status = "mismatch" if extra_local or missing_local else "match"
    elif github_count is None or local_count is None:
        status = "unavailable"
    elif github_count != local_count:
        status = "mismatch"
    elif head_matches and base_matches:
        status = "match"
    else:
        status = "count_only"

    check = {
        "status": status,
        "github_changed_files": github_count,
        "local_changed_files": local_count,
        "head_matches": head_matches,
        "base_matches": base_matches,
        "extra_local_files": extra_local,
        "missing_local_files": missing_local,
    }
    git["scope_check"] = check
    if status == "mismatch":
        print(
            f"WARNING: local range has {local_count} changed files but GitHub "
            f"reports {github_count} for the PR (head_matches={head_matches}, "
            f"base_matches={base_matches}, extra={len(extra_local or [])}, "
            f"missing={len(missing_local or [])}). The range is inflated or stale.",
            file=sys.stderr,
        )
    return check


def _fill_reviews(ctx):
    """Fill review summary from gh pr view."""
    pr = ctx.get("pr", {})
    gh_cmd = ctx.get("github_cli_command", "gh")
    pr_number = pr.get("number")
    if not pr_number:
        return

    raw = _run_cmd([gh_cmd, "pr", "view", str(pr_number), "--json",
                    "reviews,reviewRequests"])
    if not raw:
        return

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return

    reviews_raw = data.get("reviews", [])
    review_requests = data.get("reviewRequests", [])

    # Deduplicate: keep latest review per author
    latest = {}
    for r in reviews_raw:
        author = r.get("author", {}).get("login", "unknown")
        latest[author] = r

    approved = 0
    changes_requested = 0
    commented = 0
    reviewers = []

    for login, r in latest.items():
        state = r.get("state", "").upper()
        if state == "APPROVED":
            approved += 1
        elif state == "CHANGES_REQUESTED":
            changes_requested += 1
        elif state == "COMMENTED":
            commented += 1
        reviewers.append({
            "login": login,
            "type": categorize_reviewer(login),
            "state": state,
        })

    pending = []
    for rr in review_requests:
        login = rr.get("login", rr.get("name", "unknown"))
        pending.append(login)

    ctx["reviews"] = {
        "summary": {
            "total": len(latest),
            "approved": approved,
            "changes_requested": changes_requested,
            "commented": commented,
        },
        "reviewers": reviewers,
        "pending": pending,
    }


# ---------------------------------------------------------------------------
# Core: load and fill
# ---------------------------------------------------------------------------

def load_and_fill(ctx_path, pr_number=None, gh_cmd=None, branch=False,
                  incremental=False, git_range=None, repo_path=None,
                  config=None):
    """Load existing context, fill missing fields, return complete context."""
    ctx = {}
    if os.path.isfile(ctx_path):
        with open(ctx_path) as f:
            ctx = json.load(f)

    ctx.setdefault("version", 1)

    # Mode
    if pr_number:
        ctx.setdefault("mode", "pr")
    elif branch:
        ctx.setdefault("mode", "branch")

    # GitHub CLI command
    if "github_cli_command" not in ctx:
        ctx["github_cli_command"] = gh_cmd or resolve_gh_cmd()

    # Git context — recompute when explicit inputs are provided.
    # Skip recomputation when pre-computed context exists: merge_base is
    # present and no explicit git_range override. Caller-agnostic (rule 28):
    # any caller that writes review context with a valid merge_base
    # gets this optimization — no identity detection.
    git = ctx.setdefault("git", {})
    precomputed = (
        "merge_base" in git
        and not git_range  # explicit range overrides even pre-computed context
    )
    if not precomputed:
        # Clear stale git context so _fill_git_context recomputes
        if "merge_base" in git:
            git.clear()
        _fill_git_context(ctx, pr_number=pr_number, branch=branch,
                         incremental=incremental, git_range=git_range,
                         config=config)

    # Derived git fields
    if "changed_files_csv" not in git and "changed_files" in git:
        git["changed_files_csv"] = ",".join(git["changed_files"])

    # PR metadata — fetch what's missing
    pr = ctx.setdefault("pr", {})
    if pr_number:
        pr.setdefault("number", int(pr_number))
    if pr.get("number") and "body" not in pr:
        _fill_pr_metadata(ctx)
    if pr.get("number"):
        _check_scope_against_github(ctx)

    # PR size
    pr_size = ctx.setdefault("pr_size", {})
    if "category" not in pr_size and "lines" in pr_size:
        pr_size["category"] = bucket_pr_size(pr_size.get("lines", 0))
    elif "category" not in pr_size and git.get("changed_files"):
        # Estimate lines from diff stats if available
        diff_stats = git.get("diff_stats", "")
        lines = 0
        # Try to parse total from last line of diff stats
        for line in diff_stats.split("\n"):
            for m in re.finditer(r'(\d+)\s+insertion', line):
                lines += int(m.group(1))
            for m in re.finditer(r'(\d+)\s+deletion', line):
                lines += int(m.group(1))
        if lines > 0:
            pr_size["lines"] = lines
            pr_size["category"] = bucket_pr_size(lines)
        pr_size.setdefault("files", len(git.get("changed_files", [])))

    # Reviews — categorize if raw data present but categorization missing
    if pr.get("number") and "reviews" not in ctx:
        _fill_reviews(ctx)

    # Linked issues — extract from body if missing
    if "linked_issues" not in ctx and pr.get("body"):
        ctx["linked_issues"] = extract_linked_issues(pr["body"])

    # Also extract from branch name (e.g. fix/WOOPLUG-5988-desc → WOOPLUG-5988)
    head_ref = git.get("head_ref", "")
    if head_ref:
        existing = set(ctx.get("linked_issues", []))
        for m in re.finditer(r'\b([A-Z]+-\d+)\b', head_ref):
            existing.add(m.group(1))
        ctx["linked_issues"] = sorted(existing)

    # Staleness detection — compare merge_base age against base branch
    _detect_staleness(ctx)

    # Linear ID flagging — detect TEAM-NNN patterns for MCP fetch
    _detect_linear_issues(ctx)

    # GitHub issue fetching — fetch #NNN details via gh
    _fetch_github_issues(ctx)

    # Author name resolution — fetch display name for PR author
    _resolve_author_name(ctx)

    # Review defaults
    ctx.setdefault("review", {}).setdefault("agent_timeout_seconds", 1200)

    repo_root = _resolve_repo_root(repo_path or os.getcwd())
    # Host context — discover from the repo root when git can identify it.
    _fill_host_context(ctx, repo_root)

    # Repo-contributed review config (rules + reviewers) from the reviewed repo.
    _fill_review_config(ctx, repo_root)

    return ctx


def _detect_staleness(ctx):
    """Detect if the branch is stale (behind the base branch)."""
    git = ctx.get("git", {})
    merge_base = git.get("merge_base")
    base_ref = git.get("base_ref", "main")
    if not merge_base:
        return

    # Count commits the base branch has that the merge_base doesn't
    count_str = _run_cmd(["git", "rev-list", "--count",
                          f"{merge_base}..origin/{base_ref}"])
    if count_str:
        try:
            behind = int(count_str)
            ctx["staleness"] = {
                "is_stale": behind >= 10,  # matches STALE_BRANCH_THRESHOLD
                "commits_behind": behind,
            }
        except ValueError:
            pass


def _detect_linear_issues(ctx):
    """Flag has_unfetched_issues when Linear IDs (TEAM-NNN) are found."""
    issues = ctx.get("linked_issues", [])
    # Linear IDs match [A-Z]+-\d+ but NOT pure GitHub refs (which are just numbers)
    linear_ids = [i for i in issues if re.match(r'^[A-Z]+-\d+$', str(i))]
    ctx["has_unfetched_issues"] = len(linear_ids) > 0


def _fetch_github_issues(ctx):
    """Fetch details for GitHub issue refs (#NNN)."""
    issues = ctx.get("linked_issues", [])
    gh_cmd = ctx.get("github_cli_command", "gh")
    details = []

    for issue_ref in issues:
        # Only fetch pure numeric refs (GitHub issues)
        if isinstance(issue_ref, str) and issue_ref.isdigit():
            result = _run_cmd([gh_cmd, "issue", "view", issue_ref,
                              "--json", "title,body,labels"])
            if result:
                try:
                    data = json.loads(result)
                    details.append({
                        "id": f"#{issue_ref}",
                        "title": data.get("title", ""),
                        "body": data.get("body", ""),
                        "labels": [l.get("name", "") for l in data.get("labels", [])],
                    })
                except (json.JSONDecodeError, KeyError):
                    pass

    if details:
        ctx["linked_issues_details"] = details


def _resolve_author_name(ctx):
    """Fetch PR author's display name via GitHub API."""
    pr = ctx.get("pr", {})
    author = pr.get("author")
    if not author or pr.get("author_name"):
        return

    gh_cmd = ctx.get("github_cli_command", "gh")
    name = _run_cmd([gh_cmd, "api", f"users/{author}", "--jq", ".name"])
    if name:
        pr["author_name"] = name


def _fill_host_context(ctx, repo_path):
    """Populate host_context using hosts.chain.ResolverChain.

    Failure is soft: if the hosts package cannot be imported at module
    load, _HOSTS_CHAIN is None and we record host_context=None. Existing
    values are overwritten because review context may be reused across
    runs and stale host paths are worse than no host context.
    """
    if _HOSTS_CHAIN is None:
        ctx["host_context"] = None
        return
    ctx["host_context"] = _HOSTS_CHAIN().run(repo_path).to_dict()


def _fill_review_config(ctx, repo_path):
    """Populate review_config from the reviewed repo's .pirategoat/config.json.

    Overwritten each run (like host_context) so repo-relative rule/reviewer
    paths resolve against the current checkout. Best-effort: absence or a
    malformed file yields the neutral empty config, never an error.

    The reviewed range's changed files are the PROVENANCE GATE: rules and
    reviewers whose defining files sit inside the range are PR-controlled
    text and are excluded (reported under ``untrusted``). When the changed
    set is unavailable the loader fails closed.
    """
    if _REVIEW_CONFIG_LOADER is None:
        ctx["review_config"] = None
        return
    changed_files = ctx.get("git", {}).get("changed_files")
    if not isinstance(changed_files, list):
        changed_files = None
    ctx["review_config"] = _REVIEW_CONFIG_LOADER(repo_path, changed_files)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Gather review context — unified Ring 1 context."
    )
    parser.add_argument("--pr-number", type=str,
                        help="PR number (PR review mode)")
    parser.add_argument("--branch", action="store_true",
                        help="Branch review mode")
    parser.add_argument("--incremental", action="store_true",
                        help="Incremental branch review (resume from last state)")
    parser.add_argument("--git-range", type=str,
                        help="Explicit git range (e.g. abc123..HEAD)")
    parser.add_argument("--output-dir", type=str, required=True,
                        help="Review run output directory")
    parser.add_argument("--repo-path", type=str, default=None,
                        help="Path to the repo under review (for host-context "
                             "discovery). Defaults to the git root of the "
                             "current working directory when available.")
    parser.add_argument("--refresh-host-context", action="store_true",
                        help="Only re-run host-context discovery against the "
                             "existing review context and write it back. "
                             "Used after a trusted-branch dependency refresh "
                             "so reviewers see the fresh installed state.")

    args = parser.parse_args()

    if not args.refresh_host_context and not args.pr_number and not args.branch:
        print("ERROR: Must provide --pr-number or --branch", file=sys.stderr)
        sys.exit(1)

    os.makedirs(args.output_dir, exist_ok=True)
    ctx_path = artifact_path(args.output_dir, "review_context")

    if args.refresh_host_context:
        # This runs mid-review after dependency refresh and updates only host
        # context. Damaged run state must fail without being overwritten.
        try:
            with open(ctx_path) as f:
                ctx = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeError):
            print(
                "ERROR: --refresh-host-context requires an existing readable "
                "review context; refusing to overwrite run state.",
                file=sys.stderr,
            )
            sys.exit(1)
        if not isinstance(ctx, dict):
            print(
                "ERROR: --refresh-host-context requires review context "
                "to contain a JSON object; refusing to overwrite run state.",
                file=sys.stderr,
            )
            sys.exit(1)
        repo_root = _resolve_repo_root(args.repo_path or os.getcwd())
        _fill_host_context(ctx, repo_root)
        with open(ctx_path, "w") as f:
            json.dump(ctx, f, indent=2)
        print(json.dumps(ctx.get("host_context"), indent=2))
        return

    ctx = load_and_fill(
        ctx_path,
        pr_number=args.pr_number,
        branch=args.branch,
        incremental=args.incremental,
        git_range=args.git_range,
        repo_path=args.repo_path,
        config=_read_run_config(args.output_dir),
    )

    # Set output directory
    ctx.setdefault("output", {})["directory"] = args.output_dir

    # Write back
    with open(ctx_path, "w") as f:
        json.dump(ctx, f, indent=2)

    print(json.dumps(ctx, indent=2))


if __name__ == "__main__":
    main()

"""Where the repository-signal resolvers look.

The one list of places the plugin-headers, wp-env and docker-compose resolvers read: the root, every directory one and two levels down under fixed skip rules and caps, and the repo-relative directories a `hosts.roots` list names in `.pirategoat/config.json` for layouts deeper than that (Jetpack's `projects/plugins/<name>/`). Reading only the root left a monorepo like WooCommerce's, whose plugin header lives at `plugins/woocommerce/`, signalling no host at all.

No directory-name heuristics: a `plugins/` or `projects/` container is not special, and nothing here guesses what a subdirectory is. Reading two levels is bounded by `MAX_DIRS_PER_LEVEL` per listing and `MAX_ROOTS` for the walk (configured roots are never dropped); symlinked directories are skipped because they can leave the repository.
"""

import os
from dataclasses import dataclass
from typing import List

from containment import contains
from hosts.repo_config import load_hosts_section

SKIPPED_DIR_NAMES = frozenset({"node_modules", "vendor"})
MAX_DIRS_PER_LEVEL = 200
MAX_ROOTS = 1000
ORIGIN_REPO = "repo-root"
ORIGIN_SUBDIRECTORY = "subdirectory"
ORIGIN_CONFIG = "config"


@dataclass(frozen=True)
class ScanRoot:
    path: str
    relative: str
    origin: str


@dataclass
class ScanRoots:
    roots: List[ScanRoot]
    config_errors: List[str]


def _relative(repo: str, path: str) -> str:
    return os.path.relpath(path, repo).replace(os.sep, "/")


def _subdirectories(path: str) -> List[str]:
    try:
        names = sorted(os.listdir(path))
    except OSError:
        return []
    found: List[str] = []
    for name in names:
        if name.startswith(".") or name in SKIPPED_DIR_NAMES:
            continue
        full = os.path.join(path, name)
        if os.path.islink(full) or not os.path.isdir(full):
            continue
        found.append(full)
        if len(found) >= MAX_DIRS_PER_LEVEL:
            break
    return found


def scan_roots(repo_path: str) -> ScanRoots:
    repo = os.path.abspath(repo_path)
    roots = [ScanRoot(repo, "", ORIGIN_REPO)]
    seen = {repo}
    for level_one in _subdirectories(repo):
        for candidate in [level_one, *_subdirectories(level_one)]:
            if candidate in seen or len(roots) >= MAX_ROOTS:
                continue
            seen.add(candidate)
            roots.append(ScanRoot(candidate, _relative(repo, candidate), ORIGIN_SUBDIRECTORY))

    errors: List[str] = []
    hosts, error = load_hosts_section(repo)
    if error:
        errors.append(error)
    configured_roots = (hosts or {}).get("roots")
    if configured_roots is None:
        configured_roots = []
    elif not isinstance(configured_roots, list):
        errors.append(f"hosts.roots: expected a list, got {type(configured_roots).__name__}")
        configured_roots = []
    for raw in configured_roots:
        if not isinstance(raw, str) or not raw.strip():
            errors.append(f"hosts.roots: expected a repo-relative directory string, got {raw!r}")
            continue
        full = os.path.abspath(os.path.join(repo, raw))
        if not contains(repo, full):
            errors.append(f"hosts.roots: {raw!r} is outside the repository")
            continue
        if not os.path.isdir(full):
            errors.append(f"hosts.roots: {raw!r} is not a directory")
            continue
        if full in seen:
            continue
        # The caps bound the walk; a root the configuration names is explicit
        # intent, bounded by the file that names it, and is never dropped.
        seen.add(full)
        roots.append(ScanRoot(full, _relative(repo, full), ORIGIN_CONFIG))
    return ScanRoots(roots=roots, config_errors=errors)

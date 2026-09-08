"""Resolve wp-env configurations at every scan root.

WordPress.org zip URLs are deterministic host signals: the file name identifies
the host, while only a local path can resolve it.
"""

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from containment import contains
from hosts.resolvers.base import HostResolver, ResolverResult
from hosts.scan_roots import scan_roots
from hosts.types import HostEntry


# The ref after `#` may carry slashes (`owner/repo#add/feature`); a pin with
# such a branch is still the host it names, recorded unresolved without a version.
_REMOTE_REF_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(#.+)?$")
_WORDPRESS_ZIP_RE = re.compile(
    r"^https?://wordpress\.org/wordpress-(latest|[0-9][0-9.]*)\.zip$"
)
_WPORG_PACKAGE_ZIP_RE = re.compile(
    r"^https?://downloads\.wordpress\.org/(?:plugin|theme)/([a-z0-9-]+?)(?:\.([0-9][0-9.]*))?\.zip$"
)
_MAPPING_CODE_TARGET_PREFIXES = (
    "wp-content/plugins/",
    "wp-content/themes/",
)


# `6.9`, `6.9.1`, `v7.0`, `7.0-beta1`, `7.0-RC1`: digits first, then dotted
# digits, then an optional pre-release suffix. A branch name never matches.
_VERSION_SHAPED_REF = re.compile(r"v?\d+(?:\.\d+){0,3}(?:[-.][A-Za-z0-9.]+)?")


class WpEnvResolver(HostResolver):
    source = "wp-env"

    def resolve(self, repo_path: str, scan=None) -> ResolverResult:
        entries: List[HostEntry] = []
        unresolved: List[Dict[str, Any]] = []
        parse_errors: List[str] = []
        for root in (scan or scan_roots(repo_path)).roots:
            self._resolve_root(repo_path, root.path, root.relative, entries, unresolved, parse_errors)
        notes = {"parse_error": "; ".join(parse_errors)} if parse_errors else {}
        return ResolverResult(entries=entries, unresolved=unresolved, notes=notes)

    def _resolve_root(self, repo_path, base_dir, root, entries, unresolved, parse_errors):
        base = self._read_json(os.path.join(base_dir, ".wp-env.json"), parse_errors)
        override = self._read_json(os.path.join(base_dir, ".wp-env.override.json"), parse_errors)
        if not base and not override:
            return

        merged = {**(base or {}), **(override or {})}
        if (
            isinstance((base or {}).get("mappings"), dict)
            and isinstance((override or {}).get("mappings"), dict)
        ):
            merged["mappings"] = {
                **base["mappings"],
                **override["mappings"],
            }

        # mappings: {target: source}  — source is usually local
        mappings = merged.get("mappings") or {}
        if isinstance(mappings, dict):
            for target, source in mappings.items():
                if not isinstance(source, str):
                    # Object-form source (e.g. {"ref": ..., "localPath": ...})
                    # — not a local path, skip. Callers relying on these refs
                    # should use the remote-ref branch.
                    continue
                self._handle_mapping_source(repo_path, base_dir, root, source, target, entries, unresolved)

        # plugins / themes arrays
        for field in ("plugins", "themes"):
            for item in merged.get(field, []) or []:
                if not isinstance(item, str):
                    continue
                self._handle_array_item(repo_path, base_dir, root, item, entries, unresolved)

        # core (may be string)
        core = merged.get("core")
        if isinstance(core, str):
            self._handle_core(repo_path, base_dir, root, core, entries, unresolved)

    @staticmethod
    def _read_json(path: str, parse_errors: List[str]) -> Optional[Dict[str, Any]]:
        if not os.path.isfile(path):
            return None
        try:
            with open(path) as f:
                document = json.load(f)
        except (ValueError, OSError) as err:
            parse_errors.append(f"{path}: {err}")
            return None
        if not isinstance(document, dict):
            parse_errors.append(f"{path}: expected object at root, got {type(document).__name__}")
            return None
        # Validate before merging: a malformed override must not replace a
        # usable base field. Null keeps the existing clear/empty semantics.
        for field, shape in (("mappings", dict), ("plugins", list), ("themes", list)):
            value = document.get(field)
            if value is not None and not isinstance(value, shape):
                parse_errors.append(f"{path}: {field}: expected {shape.__name__}, got {type(value).__name__}")
                del document[field]
        return document

    @staticmethod
    def _classify(value: str) -> str:
        """Return 'local', 'remote', 'url', or 'other' for a wp-env source value."""
        if value == "." or value.startswith("./") or value.startswith("../") or value.startswith("/"):
            return "local"
        if _REMOTE_REF_PATTERN.match(value):
            return "remote"
        if "://" in value or value.startswith("http"):
            return "url"
        return "other"

    @staticmethod
    def _parse_remote_ref(value: str) -> Tuple[Optional[str], Optional[str]]:
        """Parse 'owner/repo#ref' → (repo_name, version).

        The ref names a branch as readily as a version, and a branch name
        can be private (`WordPress/WordPress#client-staging-rollout`), so
        only a version-shaped ref is returned as the version; anything else
        is None, and the raw pin stays on the unresolved entry's `raw`,
        which no shared projection carries. Returns (None, None) if unmatched.
        """
        m = re.match(r"^[A-Za-z0-9_.-]+/([A-Za-z0-9_.-]+)(?:#(.+))?$", value)
        if not m:
            return (None, None)
        ref = m.group(2)
        return (m.group(1), ref if ref and _VERSION_SHAPED_REF.fullmatch(ref) else None)

    def _handle_mapping_source(self, repo_path, base_dir, root, source, target, entries, unresolved):
        name = self._name_from_code_mapping_target(target)
        if name is None:
            return
        kind = self._classify(source)
        if kind != "local":
            unresolved.append({
                "name": name,
                "reason": "remote_ref_not_local",
                "source": "wp-env",
                "raw": source,
                "root": root,
            })
            return
        resolved = os.path.abspath(os.path.join(base_dir, source))
        if not os.path.isdir(resolved):
            unresolved.append({
                "name": name,
                "reason": "path_missing",
                "source": "wp-env",
                "raw": source,
                "root": root,
            })
            return
        entry = self._local_entry(repo_path, resolved, name)
        if entry is not None:
            entries.append(entry)

    def _handle_array_item(self, repo_path, base_dir, root, item, entries, unresolved):
        if item == ".":
            return  # self, not upstream
        kind = self._classify(item)
        if kind == "remote":
            name, version = self._parse_remote_ref(item)
            if name is not None:
                unresolved.append({
                    "name": name,
                    "version": version,
                    "reason": "remote_ref_not_local",
                    "source": "wp-env",
                    "raw": item,
                    "root": root,
                })
            return
        if kind == "url":
            match = _WPORG_PACKAGE_ZIP_RE.match(item)
            if match:
                unresolved.append({
                    "name": match.group(1),
                    "version": match.group(2),
                    "reason": "remote_url_not_local",
                    "source": "wp-env",
                    "raw": item,
                    "root": root,
                })
            return
        if kind != "local":
            return
        # A missing local path in an array is skipped silently; only an
        # explicit mapping gets the unresolved entry.
        resolved = os.path.abspath(os.path.join(base_dir, item))
        entry = self._local_entry(repo_path, resolved, os.path.basename(resolved.rstrip("/")))
        if entry is not None:
            entries.append(entry)

    def _handle_core(self, repo_path, base_dir, root, core, entries, unresolved):
        kind = self._classify(core)
        if kind == "local":
            resolved = os.path.abspath(os.path.join(base_dir, core))
            entry = self._local_entry(repo_path, resolved, "wordpress")
            if entry is not None:
                entries.append(entry)
            return
        if kind == "url":
            match = _WORDPRESS_ZIP_RE.match(core)
            if match:
                version = None if match.group(1) == "latest" else match.group(1)
                unresolved.append({
                    "name": "wordpress",
                    "version": version,
                    "reason": "remote_url_not_local",
                    "source": "wp-env",
                    "raw": core,
                    "root": root,
                })
            return
        if kind == "remote":
            _, version = self._parse_remote_ref(core)
            unresolved.append({
                "name": "wordpress",
                "version": version,  # None is fine if parse failed
                "reason": "remote_ref_not_local",
                "source": "wp-env",
                "raw": core,
                "root": root,
            })

    def _local_entry(self, repo_path: str, resolved: str, name: str) -> Optional[HostEntry]:
        """A runtime host at a local directory outside the repository, else None."""
        if not os.path.isdir(resolved) or self._is_inside_repo(repo_path, resolved):
            return None
        return HostEntry(
            name=name, kind="runtime-host", path=resolved, source=self.source, confidence="high",
        )

    @staticmethod
    def _is_inside_repo(repo_path: str, resolved_path: str) -> bool:
        return contains(repo_path, resolved_path)

    @staticmethod
    def _name_from_code_mapping_target(target: str) -> Optional[str]:
        normalized = target.strip().strip("/")
        for prefix in _MAPPING_CODE_TARGET_PREFIXES:
            if normalized.startswith(prefix):
                remainder = normalized[len(prefix):].strip("/")
                if remainder:
                    return remainder.split("/")[0]
        return None

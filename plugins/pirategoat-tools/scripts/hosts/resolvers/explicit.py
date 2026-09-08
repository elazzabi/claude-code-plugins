"""Explicit host configuration from .pirategoat/config.json."""

import os
from typing import List

from containment import contains
from hosts.repo_config import config_path as _config_path, load_hosts_section
from hosts.resolvers.base import HostResolver, ResolverResult
from hosts.types import HostEntry


class ExplicitResolver(HostResolver):
    source = "explicit"

    def resolve(self, repo_path: str, scan=None) -> ResolverResult:
        config_path = _config_path(repo_path)
        hosts_section, error = load_hosts_section(repo_path)
        if error:
            return ResolverResult(entries=[], unresolved=[], notes={"parse_error": error})
        if hosts_section is None:
            return ResolverResult(entries=[], unresolved=[], notes={})
        runtime_hosts = hosts_section.get("runtime") or []

        entries: List[HostEntry] = []
        for h in runtime_hosts:
            if not isinstance(h, dict):
                return ResolverResult(
                    entries=[], unresolved=[],
                    notes={"parse_error": f"{config_path}: expected object in runtime list, got {type(h).__name__}: {h!r}"},
                )
            name = h.get("name")
            if not name or not isinstance(name, str):
                return ResolverResult(
                    entries=[], unresolved=[],
                    notes={"parse_error": f"{config_path}: missing required 'name' in host entry: {h!r}"},
                )
            raw_path = h.get("path")
            if not raw_path or not isinstance(raw_path, str):
                return ResolverResult(
                    entries=[], unresolved=[],
                    notes={"parse_error": f"{config_path}: host '{name}' missing required 'path'"},
                )
            resolved_path = os.path.abspath(os.path.join(repo_path, raw_path))
            if not os.path.exists(resolved_path):
                return ResolverResult(
                    entries=[], unresolved=[],
                    notes={"parse_error": f"{config_path}: host '{name}' path does not exist: {resolved_path}"},
                )
            if not os.path.isdir(resolved_path):
                return ResolverResult(
                    entries=[], unresolved=[],
                    notes={"parse_error": f"{config_path}: host '{name}' path is not a directory: {resolved_path}"},
                )
            if self._is_inside_repo(resolved_path, repo_path):
                return ResolverResult(
                    entries=[], unresolved=[],
                    notes={"skipped": f"{config_path}: host '{name}' path is inside reviewed repo: {resolved_path}"},
                )
            entries.append(HostEntry(
                name=name,
                kind="runtime-host",
                path=resolved_path,
                source=self.source,
                version=h.get("version"),
                confidence="high",
            ))
        return ResolverResult(entries=entries, unresolved=[], notes={})

    @staticmethod
    def _is_inside_repo(path: str, repo_path: str) -> bool:
        return contains(repo_path, path)

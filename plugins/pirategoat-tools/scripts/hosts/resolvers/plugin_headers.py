"""WordPress plugin/theme header resolver.

Reads standard plugin/theme header blocks at every scan root from
``hosts/scan_roots.py`` and emits declared dependencies as unresolved entries.
The chain's cache-fulfillment pass then satisfies known ecosystem hosts from
the cache, while unfulfillable declared dependencies surface as banner entries
so the reviewer knows source is missing. ``Text Domain`` (falling back to the
main file's name) names what the repository provides, matched against the
cache's known names, so a repository is never asked to verify itself against a
cache clone.
"""

import os
from typing import Any, Dict, List

from hosts.cache.manager import KNOWN_ECOSYSTEM_NAMES
from hosts.headers import find_plugin_headers, find_theme_headers
from hosts.resolvers.base import HostResolver, ResolverResult
from hosts.scan_roots import scan_roots

_TEXT_DOMAIN_FIELD = "Text Domain"

_WP_VERSION_FIELD = "Requires at least"
_WC_VERSION_FIELD = "WC requires at least"
_REQUIRES_PLUGINS_FIELD = "Requires Plugins"


class PluginHeadersResolver(HostResolver):
    source = "plugin-headers"

    def resolve(self, repo_path: str, scan=None) -> ResolverResult:
        scan = scan or scan_roots(repo_path)
        unresolved: List[Dict[str, Any]] = []
        detected: List[Dict[str, str]] = []
        provides: set = set()
        for root in scan.roots:
            found = find_plugin_headers(root.path)
            kind = "plugin"
            if found is None:
                found = find_theme_headers(root.path)
                kind = "theme"
            if found is None:
                continue
            main_file, headers = found
            detected.append({"root": root.relative, "kind": kind})
            if kind == "plugin":
                slug = (
                    headers.get(_TEXT_DOMAIN_FIELD)
                    or os.path.splitext(os.path.basename(main_file))[0]
                ).strip().lower()
                if slug in KNOWN_ECOSYSTEM_NAMES:
                    provides.add(slug)
            unresolved.extend(self._declared(headers, root.relative))
        notes: Dict[str, Any] = {}
        if detected:
            notes["detected"] = detected
        if provides:
            notes["provides"] = sorted(provides)
        return ResolverResult(entries=[], unresolved=unresolved, notes=notes)

    def _declared(self, headers: Dict[str, str], root: str) -> List[Dict[str, Any]]:
        """The hosts one header block declares, in header order."""
        unresolved: List[Dict[str, Any]] = []
        emitted_names: set = set()
        wp_version = headers.get(_WP_VERSION_FIELD)
        if wp_version:
            unresolved.append({
                "name": "wordpress", "version": wp_version,
                "reason": "declared_in_plugin_headers", "source": self.source, "root": root,
            })
            emitted_names.add("wordpress")
        wc_version = headers.get(_WC_VERSION_FIELD)
        if wc_version:
            unresolved.append({
                "name": "woocommerce", "version": wc_version,
                "reason": "declared_in_plugin_headers", "source": self.source, "root": root,
            })
            emitted_names.add("woocommerce")
        for raw_slug in headers.get(_REQUIRES_PLUGINS_FIELD, "").split(","):
            slug = raw_slug.strip()
            if not slug or slug in emitted_names:
                continue
            unresolved.append({
                "name": slug, "reason": "declared_in_plugin_headers", "source": self.source,
                "fulfillable": slug in KNOWN_ECOSYSTEM_NAMES, "root": root,
            })
            emitted_names.add(slug)
        return unresolved

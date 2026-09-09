"""The `hosts` section of the reviewed repository's `.pirategoat/config.json`.

One reader for two consumers: `resolvers/explicit.py` validates the `hosts.runtime` entries and `scan_roots.py` reads `hosts.roots`. `review/review_config.py` reads the same file's `review` section and is deliberately a sibling reader, not a caller (see its module docstring).
"""

import json
import os
from typing import Any, Dict, Optional, Tuple

CONFIG_RELATIVE_PATH = os.path.join(".pirategoat", "config.json")


def config_path(repo_path: str) -> str:
    return os.path.join(repo_path, CONFIG_RELATIVE_PATH)


def load_hosts_section(repo_path: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Return ``(hosts section, error)``.

    An absent file is ``(None, None)``; an unreadable file or a non-object root is ``(None, "<path>: <why>")``; a file whose ``hosts`` is missing or not an object is ``({}, None)``.
    """
    path = config_path(repo_path)
    if not os.path.isfile(path):
        return None, None
    try:
        with open(path) as handle:
            data = json.load(handle)
    except (ValueError, OSError) as err:
        # ValueError covers JSON syntax, text decoding and integer limits.
        return None, f"{path}: {err}"
    if not isinstance(data, dict):
        return None, f"{path}: expected object at root, got {type(data).__name__}"
    hosts = data.get("hosts")
    return (hosts if isinstance(hosts, dict) else {}), None

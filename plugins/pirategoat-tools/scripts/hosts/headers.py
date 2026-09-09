"""WordPress plugin and theme header blocks, read once for every consumer.

A stdlib-only leaf: the plugin-headers resolver reads declared dependencies
from these blocks, and ``hosts/identity.py`` reads a checkout's own
``Version:`` from them. Neither may import the other (the resolver already
imports the cache manager, which imports identity), so the parsing lives
here, below both.
"""

import os
import re
from typing import Dict, Optional, Tuple

# Match `FieldName: value` lines. Field name allows letters, digits, spaces,
# underscores, hyphens — covers all known WP/WC header conventions. Leading
# `*` is optional (PHPDoc-style block comments use ` * Field: value`).
_HEADER_LINE_RE = re.compile(
    r"^\s*\*?\s*([A-Za-z][A-Za-z0-9 _-]*?):\s*(.+?)\s*$"
)

PLUGIN_NAME_FIELD = "Plugin Name"
THEME_NAME_FIELD = "Theme Name"
VERSION_FIELD = "Version"

# Caps to keep CPU bounded on weird inputs.
MAX_TOP_LEVEL_PHP_FILES = 20
MAX_HEADER_LINES = 100


def parse_header_block(path: str) -> Optional[Dict[str, str]]:
    """Parse the leading comment block of a .php or .css file into a
    ``{field: value}`` dict. Returns None if unreadable."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = []
            for _ in range(MAX_HEADER_LINES):
                line = f.readline()
                if not line:
                    break
                lines.append(line)
    except OSError:
        return None
    return parse_header_lines(lines)


def parse_header_lines(lines) -> Optional[Dict[str, str]]:
    """The header fields in the leading comment block of ``lines``, or None
    when there are none. The cache manager hands it the text ``git show``
    printed for a committed file; ``parse_header_block`` hands it a file."""
    out: Dict[str, str] = {}
    for line in list(lines)[:MAX_HEADER_LINES]:
        if line.lstrip().startswith("*/"):
            break
        match = _HEADER_LINE_RE.match(line)
        if match:
            field, value = match.group(1).strip(), match.group(2).strip()
            # First occurrence wins (mirrors WP's get_file_data behavior).
            if field not in out:
                out[field] = value
    return out or None


def find_plugin_headers(directory: str) -> Optional[Tuple[str, Dict[str, str]]]:
    """``(main file path, headers)`` of the first top-level ``.php`` file
    in ``directory`` carrying ``Plugin Name:``, or None."""
    try:
        entries = sorted(os.listdir(directory))
    except OSError:
        return None
    php_files = [
        name for name in entries
        if name.endswith(".php") and os.path.isfile(os.path.join(directory, name))
    ][:MAX_TOP_LEVEL_PHP_FILES]
    for name in php_files:
        full = os.path.join(directory, name)
        headers = parse_header_block(full)
        if headers and PLUGIN_NAME_FIELD in headers:
            return full, headers
    return None


def find_theme_headers(directory: str) -> Optional[Tuple[str, Dict[str, str]]]:
    """``(style.css path, headers)`` when ``directory`` is a theme, or None."""
    style = os.path.join(directory, "style.css")
    if not os.path.isfile(style):
        return None
    headers = parse_header_block(style)
    if headers and THEME_NAME_FIELD in headers:
        return style, headers
    return None

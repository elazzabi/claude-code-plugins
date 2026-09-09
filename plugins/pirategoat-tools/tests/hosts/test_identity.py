"""A local host checkout's identity: version from what it declares about
itself, commit/branch/date from the repository that contains it."""

import subprocess

import pytest

from helpers.pipeline_process import init_repo
from hosts import identity

PLUGIN_HEADER = """<?php
/**
 * Plugin Name: WooCommerce
 * Version: 11.2.0-dev
 * Text Domain: woocommerce
 */
"""


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


def test_plugin_header_and_enclosing_repository(tmp_path):
    """Run 4: `woocommerce-develop/plugins/woocommerce` carried a header
    version and sat inside the monorepo checkout; both were reported
    unknown because only cache slots had an identity reader."""
    repo = init_repo(tmp_path / "woocommerce-develop", branch="trunk")
    plugin = repo / "plugins" / "woocommerce"
    plugin.mkdir(parents=True)
    (plugin / "woocommerce.php").write_text(PLUGIN_HEADER)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")

    found = identity.path_identity(str(plugin))

    assert found["version"] == "11.2.0-dev"
    assert found["commit"] == _git(repo, "rev-parse", "HEAD")
    assert "branch" not in found  # never read: no host projection carries one
    from datetime import datetime
    assert found["commit_date"] == _git(repo, "show", "-s", "--format=%cI", "HEAD")
    assert datetime.fromisoformat(found["commit_date"]).tzinfo is not None
    assert found["scope"] == "enclosing-repository"
    assert identity.path_identity(str(repo))["scope"] == "checkout"


def test_theme_and_wordpress_core_versions(tmp_path):
    theme = tmp_path / "theme"
    theme.mkdir()
    (theme / "style.css").write_text("/*\nTheme Name: Twenty\nVersion: 3.1\n*/\n")
    assert identity.declared_version(str(theme)) == "3.1"

    core = tmp_path / "wordpress"
    (core / "wp-includes").mkdir(parents=True)
    (core / "wp-includes" / "version.php").write_text("<?php\n$wp_version = '7.2-alpha-63166-src';\n")
    assert identity.declared_version(str(core)) == "7.2-alpha-63166-src"


def test_unknown_facts_are_none_never_the_directory_name(tmp_path):
    plain = tmp_path / "latest"
    plain.mkdir()
    (plain / "plugin.php").write_text("<?php\n/**\n * Plugin Name: Bare\n */\n")

    found = identity.path_identity(str(plain))

    assert found == {"version": None, "commit": None, "commit_date": None, "scope": None}


def test_a_missing_path_is_all_none(tmp_path):
    unknown = {"version": None, "commit": None, "commit_date": None, "scope": None}
    assert identity.path_identity(str(tmp_path / "absent")) == unknown
    assert identity.path_identity(None) == unknown


def test_git_read_returns_none_on_failure(tmp_path, monkeypatch):
    assert identity.git_read(tmp_path, "rev-parse", "HEAD") is None
    monkeypatch.setattr(identity.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("no git")))
    assert identity.git_read(tmp_path, "rev-parse", "HEAD") is None

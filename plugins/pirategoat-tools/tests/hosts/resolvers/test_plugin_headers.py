"""Tests for the plugin-headers resolver."""

import textwrap
from pathlib import Path

from hosts.resolvers.plugin_headers import PluginHeadersResolver


WC_HEADER = """<?php
/**
 * Plugin Name: WooCommerce
 * Text Domain: woocommerce
 * Requires at least: 7.0
 */
"""

BETA_TESTER_HEADER = """<?php
/**
 * Plugin Name: WooCommerce Beta Tester
 * Text Domain: woocommerce-beta-tester
 * Requires at least: 5.8
 * WC requires at least: 9.4
 */
"""


def _write_plugin(repo: Path, name: str, headers: str) -> Path:
    full = repo / name
    full.write_text(textwrap.dedent(headers))
    return full


def test_empty_repo_returns_no_unresolved(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    result = PluginHeadersResolver().resolve(str(repo))
    assert result.entries == []
    assert result.unresolved == []


def test_php_file_without_plugin_name_header_ignored(tmp_path):
    """A .php file that's not a plugin (no `Plugin Name:` header) is skipped."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_plugin(repo, "helper.php", """\
        <?php
        // just a helper, not a plugin
        function do_thing() {}
    """)
    result = PluginHeadersResolver().resolve(str(repo))
    assert result.unresolved == []


def test_plugin_with_requires_at_least_emits_wordpress_unresolved(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_plugin(repo, "myplugin.php", """\
        <?php
        /**
         * Plugin Name: MyPlugin
         * Requires at least: 6.0
         * Version: 1.0
         */
    """)
    result = PluginHeadersResolver().resolve(str(repo))
    assert result.unresolved == [{
        "name": "wordpress", "version": "6.0",
        "reason": "declared_in_plugin_headers", "source": "plugin-headers", "root": "",
    }]


def test_plugin_with_wc_requires_at_least_emits_woocommerce_unresolved(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_plugin(repo, "myplugin.php", """\
        <?php
        /**
         * Plugin Name: MyPlugin
         * WC requires at least: 7.6
         */
    """)
    result = PluginHeadersResolver().resolve(str(repo))
    assert result.unresolved == [{
        "name": "woocommerce", "version": "7.6",
        "reason": "declared_in_plugin_headers", "source": "plugin-headers", "root": "",
    }]


def test_plugin_with_requires_plugins_emits_each_slug(tmp_path):
    """`Requires Plugins: woocommerce, jetpack` → two unresolved entries."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_plugin(repo, "myplugin.php", """\
        <?php
        /**
         * Plugin Name: MyPlugin
         * Requires Plugins: woocommerce, jetpack
         */
    """)
    result = PluginHeadersResolver().resolve(str(repo))
    assert result.unresolved == [
        {
            "name": "woocommerce", "reason": "declared_in_plugin_headers",
            "source": "plugin-headers", "fulfillable": True, "root": "",
        },
        {
            "name": "jetpack", "reason": "declared_in_plugin_headers",
            "source": "plugin-headers", "fulfillable": False, "root": "",
        },
    ]


def test_woocommerce_dedupes_across_wc_header_and_requires_plugins(tmp_path):
    """Both `WC requires at least` AND `Requires Plugins: woocommerce` →
    only one woocommerce unresolved entry (the version-bearing one)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_plugin(repo, "myplugin.php", """\
        <?php
        /**
         * Plugin Name: MyPlugin
         * WC requires at least: 7.6
         * Requires Plugins: woocommerce
         */
    """)
    result = PluginHeadersResolver().resolve(str(repo))
    wc_entries = [u for u in result.unresolved if u["name"] == "woocommerce"]
    assert wc_entries == [{
        "name": "woocommerce", "version": "7.6",
        "reason": "declared_in_plugin_headers", "source": "plugin-headers", "root": "",
    }]


def test_woopayments_style_full_header_block(tmp_path):
    """End-to-end: a header block matching WooPayments' real plugin file
    produces both wordpress and woocommerce unresolved entries."""
    repo = tmp_path / "woocommerce-payments"
    repo.mkdir()
    _write_plugin(repo, "woocommerce-payments.php", """\
        <?php
        /**
         * Plugin Name: WooPayments
         * Plugin URI: https://woocommerce.com/payments/
         * Description: Accept payments via credit card.
         * Version: 10.7.1
         * Author: Automattic
         * WC requires at least: 7.6
         * WC tested up to: 10.7.0
         * Requires at least: 6.0
         * Requires PHP: 7.3
         * Requires Plugins: woocommerce
         */
    """)
    result = PluginHeadersResolver().resolve(str(repo))
    names = sorted(u["name"] for u in result.unresolved)
    assert names == ["woocommerce", "wordpress"]
    assert result.notes == {"detected": [{"root": "", "kind": "plugin"}]}


def test_first_php_file_with_plugin_name_wins(tmp_path):
    """Non-plugin .php files come first alphabetically don't shadow the real plugin."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_plugin(repo, "a-helper.php", """\
        <?php
        // helper file
        function thing() {}
    """)
    _write_plugin(repo, "b-plugin.php", """\
        <?php
        /**
         * Plugin Name: BPlugin
         * Requires at least: 6.0
         */
    """)
    result = PluginHeadersResolver().resolve(str(repo))
    assert result.unresolved == [{
        "name": "wordpress", "version": "6.0",
        "reason": "declared_in_plugin_headers", "source": "plugin-headers", "root": "",
    }]


def test_theme_with_requires_at_least_emits_wordpress_unresolved(tmp_path):
    """Theme detection via style.css with `Theme Name:` header."""
    repo = tmp_path / "mytheme"
    repo.mkdir()
    (repo / "style.css").write_text(textwrap.dedent("""\
        /*
        Theme Name: MyTheme
        Requires at least: 6.0
        */
    """))
    result = PluginHeadersResolver().resolve(str(repo))
    assert result.unresolved == [{
        "name": "wordpress", "version": "6.0",
        "reason": "declared_in_plugin_headers", "source": "plugin-headers", "root": "",
    }]
    assert result.notes == {"detected": [{"root": "", "kind": "theme"}]}


def test_theme_without_theme_name_header_ignored(tmp_path):
    """A style.css without `Theme Name:` is not treated as a theme."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "style.css").write_text("body { color: red; }")
    result = PluginHeadersResolver().resolve(str(repo))
    assert result.unresolved == []


def test_unreadable_repo_returns_empty(tmp_path):
    """Nonexistent path doesn't crash."""
    result = PluginHeadersResolver().resolve(str(tmp_path / "does-not-exist"))
    assert result.entries == []
    assert result.unresolved == []


def test_a_monorepo_s_plugin_roots_are_read_two_levels_down(tmp_path):
    """The WooCommerce monorepo layout as measured on 2026-09-05."""
    repo = tmp_path / "woocommerce-develop"
    (repo / "plugins" / "woocommerce").mkdir(parents=True)
    (repo / "plugins" / "woocommerce-beta-tester").mkdir(parents=True)
    (repo / "plugins" / "woocommerce" / "woocommerce.php").write_text(WC_HEADER)
    (repo / "plugins" / "woocommerce-beta-tester" / "woocommerce-beta-tester.php").write_text(BETA_TESTER_HEADER)
    (repo / "package.json").write_text("{}")

    result = PluginHeadersResolver().resolve(str(repo))

    assert result.entries == []
    assert result.unresolved == [
        {"name": "wordpress", "version": "7.0", "reason": "declared_in_plugin_headers",
         "source": "plugin-headers", "root": "plugins/woocommerce"},
        {"name": "wordpress", "version": "5.8", "reason": "declared_in_plugin_headers",
         "source": "plugin-headers", "root": "plugins/woocommerce-beta-tester"},
        {"name": "woocommerce", "version": "9.4", "reason": "declared_in_plugin_headers",
         "source": "plugin-headers", "root": "plugins/woocommerce-beta-tester"},
    ]
    assert result.notes == {
        "detected": [
            {"root": "plugins/woocommerce", "kind": "plugin"},
            {"root": "plugins/woocommerce-beta-tester", "kind": "plugin"},
        ],
        "provides": ["woocommerce"],
    }


def test_provides_falls_back_to_the_main_file_name_without_a_text_domain(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_plugin(repo, "woocommerce.php", """\
        <?php
        /**
         * Plugin Name: WooCommerce
         * Requires at least: 7.0
         */
    """)
    result = PluginHeadersResolver().resolve(str(repo))
    assert result.notes["provides"] == ["woocommerce"]
    assert result.unresolved[0]["root"] == ""


def test_a_plugin_that_is_not_an_ecosystem_host_provides_nothing(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_plugin(repo, "woocommerce-payments.php", """\
        <?php
        /**
         * Plugin Name: WooPayments
         * Text Domain: woocommerce-payments
         * Requires at least: 6.0
         */
    """)
    result = PluginHeadersResolver().resolve(str(repo))
    assert "provides" not in result.notes
    assert result.notes["detected"] == [{"root": "", "kind": "plugin"}]


def test_a_configured_root_is_read(tmp_path):
    import json
    repo = tmp_path / "jetpack"
    plugin = repo / "projects" / "plugins" / "jetpack"
    plugin.mkdir(parents=True)
    (plugin / "jetpack.php").write_text("<?php\n/**\n * Plugin Name: Jetpack\n * Requires at least: 6.9\n */\n")
    (repo / ".pirategoat").mkdir()
    (repo / ".pirategoat" / "config.json").write_text(json.dumps({"hosts": {"roots": ["projects/plugins/jetpack"]}}))
    result = PluginHeadersResolver().resolve(str(repo))
    assert result.unresolved == [{
        "name": "wordpress", "version": "6.9", "reason": "declared_in_plugin_headers",
        "source": "plugin-headers", "root": "projects/plugins/jetpack",
    }]

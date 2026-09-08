"""Tests for the one reader of `.pirategoat/config.json`'s hosts section."""

import json

from hosts.repo_config import config_path, load_hosts_section


def test_absent_file_is_no_section_and_no_error(make_repo):
    repo = make_repo({"README.md": "# repo"})
    assert load_hosts_section(str(repo)) == (None, None)


def test_a_file_without_hosts_is_an_empty_section(make_repo):
    repo = make_repo({".pirategoat/config.json": json.dumps({"review": {}})})
    assert load_hosts_section(str(repo)) == ({}, None)


def test_the_hosts_object_is_returned_as_written(make_repo):
    repo = make_repo({".pirategoat/config.json": json.dumps({
        "hosts": {"runtime": [{"name": "wordpress", "path": "../wp"}], "roots": ["projects/plugins/jetpack"]},
    })})
    section, error = load_hosts_section(str(repo))
    assert error is None
    assert section["roots"] == ["projects/plugins/jetpack"]
    assert section["runtime"][0]["name"] == "wordpress"


def test_malformed_json_names_the_file(make_repo):
    repo = make_repo({".pirategoat/config.json": "{not json"})
    section, error = load_hosts_section(str(repo))
    assert section is None
    assert error.startswith(config_path(str(repo)))


def test_undecodable_config_names_the_file(make_repo):
    repo = make_repo({"README.md": "# repo"})
    path = repo / ".pirategoat" / "config.json"
    path.parent.mkdir()
    path.write_bytes(b"\x80")
    section, error = load_hosts_section(str(repo))
    assert section is None
    assert error.startswith(config_path(str(repo)))


def test_a_non_object_root_is_an_error(make_repo):
    repo = make_repo({".pirategoat/config.json": "[]"})
    section, error = load_hosts_section(str(repo))
    assert section is None
    assert "expected object at root, got list" in error


def test_a_non_object_hosts_value_reads_as_empty(make_repo):
    repo = make_repo({".pirategoat/config.json": json.dumps({"hosts": "nope"})})
    assert load_hosts_section(str(repo)) == ({}, None)


def test_json_integer_conversion_failure_names_the_config(make_repo):
    repo = make_repo({".pirategoat/config.json": '{"unrelated": ' + "1" * 4301 + '}'})

    section, error = load_hosts_section(str(repo))

    assert section is None
    assert error.startswith(config_path(str(repo)))
    assert "integer string conversion" in error

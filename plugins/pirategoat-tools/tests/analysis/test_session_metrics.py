"""
Tests for analysis/session_metrics.py — identify_agent_type() and related functions.

Deterministic, no model calls.  Uses importlib because the module name has hyphens.
"""

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
TESTS_DIR = Path(__file__).resolve().parent.parent  # analysis/ -> tests/
PLUGIN_ROOT = TESTS_DIR.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
SCRIPT_PATH = SCRIPTS_DIR / "analysis" / "session_metrics.py"

_spec = importlib.util.spec_from_file_location(
    "extract_session_metrics", str(SCRIPT_PATH)
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

identify_agent_type = _mod.identify_agent_type
AGENT_INFERENCE_PATTERNS = _mod.AGENT_INFERENCE_PATTERNS
NON_REVIEWER_AGENT_FINGERPRINTS = _mod.NON_REVIEWER_AGENT_FINGERPRINTS


def test_subagent_tokens_come_from_the_transcript_instrument(tmp_path):
    path = tmp_path / "agent-1.jsonl"

    def record(output, mid):
        return json.dumps({
            "type": "assistant",
            "timestamp": "2026-09-06T10:00:00.000Z",
            "message": {
                "id": mid,
                "model": "claude-sonnet-5",
                "usage": {
                    "input_tokens": 10,
                    "cache_creation_input_tokens": 5,
                    "cache_read_input_tokens": 100,
                    "output_tokens": output,
                },
            },
        })

    path.write_text("\n".join([record(3, "m1"), record(9, "m1"), record(4, "m2")]) + "\n")

    metrics = _mod.extract_subagent_metrics(str(path))

    assert (
        metrics["input_tokens"], metrics["output_tokens"],
        metrics["cache_read_tokens"], metrics["cache_creation_tokens"],
    ) == (20, 13, 200, 10)


def test_agent_type_comes_from_the_meta_file_first(tmp_path):
    path = tmp_path / "agent-1.jsonl"
    path.write_text(json.dumps({
        "type": "user", "message": {"content": "Review for pattern consistency"},
    }) + "\n")
    meta_path = tmp_path / "agent-1.meta.json"
    meta_path.write_text(json.dumps({"agentType": "pirategoat-tools:security-reviewer"}))

    assert identify_agent_type(str(path)) == "security-reviewer"

    meta_path.write_text(json.dumps({"agentType": "general-purpose"}))

    assert identify_agent_type(str(path)) == "patterns-reviewer"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_jsonl(lines: list[str], tmpdir: str) -> str:
    """Write JSONL lines to a temp file and return its path."""
    path = os.path.join(tmpdir, "test.jsonl")
    with open(path, "w") as f:
        for line in lines:
            f.write(line + "\n")
    return path


def _make_user_message(content: str) -> str:
    """Create a JSONL line with a user message containing `content`."""
    return json.dumps({"message": {"role": "user", "content": content}})


def _make_assistant_message(content: str) -> str:
    """Create a JSONL line with an assistant message."""
    return json.dumps({"message": {"role": "assistant", "content": content}})


# =============================================================================
# Strategy 1: bootstrap.py detection (also matches legacy bootstrap-reviewer.py)
# =============================================================================


class TestStrategy1Bootstrap:
    """Strategy 1 detects bootstrap.py --agent <name> in first 15 lines (also matches legacy bootstrap-reviewer.py)."""

    def test_bootstrap_with_suffix(self, tmp_path):
        path = _write_jsonl(
            [
                _make_user_message("Run the review"),
                _make_assistant_message(
                    "python3 bootstrap-reviewer.py --agent security-reviewer --range main..HEAD"
                ),
            ],
            str(tmp_path),
        )
        assert identify_agent_type(path) == "security-reviewer"

    def test_bootstrap_without_suffix(self, tmp_path):
        """Agent name without -reviewer suffix gets it appended."""
        path = _write_jsonl(
            [
                _make_user_message("Start"),
                _make_assistant_message(
                    "python3 bootstrap-reviewer.py --agent patterns"
                ),
            ],
            str(tmp_path),
        )
        assert identify_agent_type(path) == "patterns-reviewer"

    def test_bootstrap_takes_precedence(self, tmp_path):
        """Bootstrap detection should fire before keyword inference."""
        path = _write_jsonl(
            [
                _make_user_message("Review WordPress architecture quality"),
                _make_assistant_message(
                    "python3 bootstrap-reviewer.py --agent security-reviewer"
                ),
            ],
            str(tmp_path),
        )
        assert identify_agent_type(path) == "security-reviewer"


# =============================================================================
# Strategy 1.5: non-reviewer agent fingerprints
# =============================================================================


class TestStrategy15Fingerprints:
    """Strategy 1.5 detects non-reviewer agents (e.g. reconciliator) by prompt fingerprint."""

    def test_reconciliator_summary_mode(self, tmp_path):
        content = (
            "Output Directory: /tmp/pr-review-42\n"
            "Mode: summary\n"
            "\n"
            "security-reviewer: STATUS=COMPLETED\n"
            "wp-architecture-reviewer: STATUS=COMPLETED\n"
        )
        path = _write_jsonl([_make_user_message(content)], str(tmp_path))
        assert identify_agent_type(path) == "reconciliator"

    def test_reconciliator_focused_mode(self, tmp_path):
        content = (
            "Output Directory: /tmp/pr-review-99\n"
            "Mode: focused\n"
            "Focus: security\n"
        )
        path = _write_jsonl([_make_user_message(content)], str(tmp_path))
        assert identify_agent_type(path) == "reconciliator"

    def test_reconciliator_not_misidentified_as_wp_architecture(self, tmp_path):
        """The reconciliator prompt contains 'wp-architecture-reviewer: STATUS=COMPLETED'
        which previously matched the wp-architecture keyword pattern."""
        content = (
            "Output Directory: /tmp/pr-review-42\n"
            "Mode: summary\n"
            "\n"
            "wp-architecture-reviewer: STATUS=COMPLETED\n"
            "architecture-reviewer: STATUS=COMPLETED\n"
            "security-reviewer: STATUS=COMPLETED\n"
        )
        path = _write_jsonl([_make_user_message(content)], str(tmp_path))
        result = identify_agent_type(path)
        assert result == "reconciliator"
        assert result != "wp-architecture-reviewer"


# =============================================================================
# Strategy 2: keyword inference (hardened)
# =============================================================================


class TestStrategy2Keywords:
    """Strategy 2 infers agent type from prompt keywords."""

    def test_wp_architecture_genuine_prompt(self, tmp_path):
        content = "Review the WordPress architecture of this PR"
        path = _write_jsonl([_make_user_message(content)], str(tmp_path))
        assert identify_agent_type(path) == "wp-architecture-reviewer"

    def test_security_genuine_prompt(self, tmp_path):
        content = "Check for security issues in the changed files"
        path = _write_jsonl([_make_user_message(content)], str(tmp_path))
        assert identify_agent_type(path) == "security-reviewer"

    def test_patterns_genuine_prompt(self, tmp_path):
        content = "Review for pattern consistency"
        path = _write_jsonl([_make_user_message(content)], str(tmp_path))
        assert identify_agent_type(path) == "patterns-reviewer"

    def test_agent_signal_does_not_trigger_keyword_match(self, tmp_path):
        """Agent signal lines like 'wp-architecture-reviewer: STATUS=COMPLETED'
        should be stripped before keyword inference."""
        content = (
            "Here is some generic text about code quality.\n"
            "wp-architecture-reviewer: STATUS=COMPLETED\n"
            "security-reviewer: STATUS=COMPLETED\n"
        )
        path = _write_jsonl([_make_user_message(content)], str(tmp_path))
        # Without the fix, this would match "wp-architecture" from the signal line
        assert identify_agent_type(path) is None

    def test_agent_signal_does_not_cause_false_wp_arch(self, tmp_path):
        """Specifically test that wp-architecture-reviewer signal doesn't
        trigger wp-architecture keyword inference."""
        content = (
            "Review the code changes.\n"
            "wp-architecture-reviewer: STATUS=FINISHED\n"
        )
        path = _write_jsonl([_make_user_message(content)], str(tmp_path))
        assert identify_agent_type(path) is None

    def test_mixed_signal_and_real_keyword(self, tmp_path):
        """If genuine keywords exist alongside stripped signal lines,
        the genuine keyword should still match."""
        content = (
            "Review for security issues in the PR.\n"
            "wp-architecture-reviewer: STATUS=COMPLETED\n"
        )
        path = _write_jsonl([_make_user_message(content)], str(tmp_path))
        assert identify_agent_type(path) == "security-reviewer"


# =============================================================================
# Edge cases
# =============================================================================


class TestEdgeCases:
    """Edge cases for identify_agent_type."""

    def test_empty_file(self, tmp_path):
        path = _write_jsonl([], str(tmp_path))
        assert identify_agent_type(path) is None

    def test_nonexistent_file(self):
        assert identify_agent_type("/nonexistent/path.jsonl") is None

    def test_no_matching_content(self, tmp_path):
        content = "Just some random text with no reviewer keywords."
        path = _write_jsonl([_make_user_message(content)], str(tmp_path))
        assert identify_agent_type(path) is None

    def test_list_content_format(self, tmp_path):
        """Content can be a list of text blocks (multi-part messages)."""
        msg = json.dumps({
            "message": {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Review for "},
                    {"type": "text", "text": "security issues"},
                ],
            }
        })
        path = _write_jsonl([msg], str(tmp_path))
        assert identify_agent_type(path) == "security-reviewer"


def test_meta_file_identity_is_exact_against_the_registry(tmp_path):
    """`code-clarity-reviewer` must never collapse onto `code-reviewer`, and a
    reviewer the old hand-spelled list lacked is still recognised."""
    import json
    from session_metrics import identify_agent_type
    for name in ("code-clarity-reviewer", "concurrency-reviewer", "ecosystem-integration-reviewer", "review-reconciliator"):
        path = tmp_path / f"agent-{name}.jsonl"
        path.write_text(json.dumps({"type": "user", "message": {"content": "review the code"}}) + "\n")
        (tmp_path / f"agent-{name}.meta.json").write_text(json.dumps({"agentType": f"pirategoat-tools:{name}"}))
        assert identify_agent_type(str(path)) == name

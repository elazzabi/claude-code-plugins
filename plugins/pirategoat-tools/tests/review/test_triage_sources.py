"""Tests for the prose sources keyword triage reads, cleaned."""

import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = TESTS_DIR.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
from review import triage_sources as ts  # noqa: E402


class TestHtmlComments:
    def test_single_and_multiline_comments_are_removed(self):
        text = "Keep <!-- drop --> this\n<!--\nmulti\nline\n-->\nand this"
        assert ts.strip_html_comments(text) == "Keep  this\n\nand this"

    def test_unterminated_comment_is_left_alone(self):
        assert ts.strip_html_comments("a <!-- b") == "a <!-- b"


class TestTemplateSubtraction:
    TEMPLATE = (
        "### Changes proposed in this Pull Request:\n"
        "<!-- Describe the changes made -->\n"
        "-   I have reviewed my code for [security best practices](https://x).\n"
        "- [ ] Automatically create a changelog entry from the details below.\n"
        "#### Type\n"
        "-   [ ] Performance - Address performance issues\n"
    )

    def test_template_lines_are_removed_even_when_heading_punctuation_drifts(self):
        body = (
            "### Changes proposed in this Pull Request\n"
            "The dropdown opens the keyboard on touch devices.\n"
            "-   I have reviewed my code for [security best practices](https://x).\n"
            "- [x] Automatically create a changelog entry from the details below.\n"
            "-   [ ] Performance - Address performance issues\n"
        )
        assert ts.subtract_template(body, self.TEMPLATE) == (
            "The dropdown opens the keyboard on touch devices."
        )

    def test_an_edited_template_line_survives(self):
        """Whole-line comparison: a checklist line the author extended is
        the author's sentence, not the template's."""
        body = "- [x] I have reviewed my code for [security best practices](https://x) and added nonce checks.\n"
        assert ts.subtract_template(body, self.TEMPLATE) == body.strip()

    def test_author_prose_under_a_template_heading_survives(self):
        body = "#### Type\nWe rewrote the request handling for auth tokens.\n"
        assert ts.subtract_template(body, self.TEMPLATE) == (
            "We rewrote the request handling for auth tokens."
        )

    def test_no_template_still_strips_html_comments(self):
        body = "Real prose <!-- template comment mentioning password --> here"
        assert ts.subtract_template(body, "") == "Real prose  here"

    def test_normalization_rules(self):
        assert ts.normalize_template_line("### Heading:") == "heading"
        assert ts.normalize_template_line("-   [X] Item.") == "[ ] item"
        assert ts.normalize_template_line("  *  two   words ") == "two words"
        assert ts.normalize_template_line("<!-- gone -->") == ""


class TestFindPrTemplate:
    def test_reads_every_github_location_and_concatenates(self, tmp_path):
        (tmp_path / ".github").mkdir()
        (tmp_path / ".github" / "PULL_REQUEST_TEMPLATE.md").write_text("root template\n")
        (tmp_path / ".github" / "PULL_REQUEST_TEMPLATE").mkdir()
        (tmp_path / ".github" / "PULL_REQUEST_TEMPLATE" / "bug.md").write_text("bug template\n")
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "pull_request_template.md").write_text("docs template\n")
        text = ts.find_pr_template(tmp_path)
        assert "root template" in text and "bug template" in text and "docs template" in text

    def test_absent_template_is_empty(self, tmp_path):
        assert ts.find_pr_template(tmp_path) == ""

    def test_unreadable_template_is_empty(self, tmp_path):
        assert ts.find_pr_template(tmp_path / "missing") == ""

    @pytest.mark.parametrize(
        "template_directory",
        [
            ".github/PULL_REQUEST_TEMPLATE",
            "PULL_REQUEST_TEMPLATE",
            "docs/PULL_REQUEST_TEMPLATE",
        ],
    )
    def test_reads_templates_from_every_supported_multiple_template_directory(
        self, tmp_path, template_directory
    ):
        directory = tmp_path / template_directory
        directory.mkdir(parents=True)
        (directory / "bug.md").write_text("security template\n")
        assert ts.find_pr_template(tmp_path) == "security template\n"


class TestCommitTrailers:
    def test_final_trailer_paragraph_is_dropped_per_commit(self):
        log = (
            "fix: keep the keyboard closed\n"
            "The input is a dropdown.\n"
            "\n"
            "Co-Authored-By: Claude <noreply@example.com>\n"
            "Claude-Session: https://example.com/session_1\n"
            "\x00"
            "docs: note the change\n"
            "Refs #53136\n"
            "\x00"
        )
        assert ts.strip_commit_trailers(log) == (
            "fix: keep the keyboard closed\nThe input is a dropdown.\n"
            "docs: note the change"
        )

    def test_whitespace_after_the_colon_is_git_s_not_ours(self):
        log = "fix: x\nBody.\n\nCo-Authored-By:  Two Spaces <a@b>\nSigned-off-by:\tTab <t@b>\n\x00"
        assert ts.strip_commit_trailers(log) == "fix: x\nBody."

    def test_a_reference_list_with_a_period_is_a_trailer(self):
        log = "fix: y\nBody.\n\nRefs WOOPLUG-1, WOOPLUG-2.\nfixes #7\n\x00"
        assert ts.strip_commit_trailers(log) == "fix: y\nBody."

    def test_a_prose_paragraph_with_one_colon_line_is_kept(self):
        log = "feat: add auth\nNote: this changes the login flow.\nAnd more prose.\n\x00"
        assert ts.strip_commit_trailers(log) == (
            "feat: add auth\nNote: this changes the login flow.\nAnd more prose."
        )

    def test_a_one_line_final_note_paragraph_is_a_trailer_to_git_and_to_us(self):
        """`git interpret-trailers --parse` classifies a lone final
        `Token: value` line as a trailer; the planner follows git rather
        than guessing which tokens are prose."""
        log = "fix: z\nBody.\n\nNote: token handling changed.\n\x00"
        assert ts.strip_commit_trailers(log) == "fix: z\nBody."

    def test_a_subject_only_commit_is_kept(self):
        assert ts.strip_commit_trailers("chore: bump\n\x00") == "chore: bump"

    def test_a_commit_that_is_only_trailers_keeps_its_subject(self):
        assert ts.strip_commit_trailers("fix: x\nRefs #1\n\x00") == "fix: x"

    def test_reference_line_with_an_explanation_is_author_prose(self):
        log = "fix: add guard\nDetails.\n\nFixes #123 by enforcing authentication and sanitization.\n\x00"
        assert ts.strip_commit_trailers(log) == (
            "fix: add guard\nDetails.\n\nFixes #123 by enforcing authentication and sanitization."
        )

    def test_empty_log_is_empty(self):
        assert ts.strip_commit_trailers("") == ""

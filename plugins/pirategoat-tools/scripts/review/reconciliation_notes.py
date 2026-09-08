#!/usr/bin/env python3
"""Register an orchestrator claim for the reconciliator to answer.

The step-8 dispatch prompt carries only the three inputs the reconciliator
needs. Everything the orchestrator would otherwise put in that prompt — a
disagreement it noticed between reviewers, two findings it believes are
one concern, a fact it wants weighed — goes into the reconciliation
context as a note, stated as a claim. findings_save.py then refuses a
ledger that does not answer every note with an outcome and evidence, so
a hint can no longer be adopted verbatim (run 6e6a) or lost.

One writer, under the output-directory lock, appending to the context
reconciliation_context.py wrote. Ids are monotonic within the run.
"""

import argparse
import os
import sys

try:
    from . import atomic_io
    from .findings_ledger import read_reconciliation_context
    from .review_document import normalize_bounded_text
    from .reconciliation_context import RECONCILIATION_CONTEXT_SCHEMA
    from .run_paths import artifact_path
except ImportError:
    _scripts_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _scripts_parent not in sys.path:
        sys.path.insert(0, _scripts_parent)
    from review import atomic_io
    from review.findings_ledger import read_reconciliation_context
    from review.review_document import normalize_bounded_text
    from review.reconciliation_context import RECONCILIATION_CONTEXT_SCHEMA
    from review.run_paths import artifact_path

CONTEXT_FILENAME = artifact_path("", "reconciliation_context").name


def validate_orchestrator_notes(value):
    """Validate the schema-4 claim collection without repairing existing state."""
    if not isinstance(value, list):
        raise ValueError("orchestrator_notes must be a list")
    for index, note in enumerate(value):
        label = f"orchestrator_notes[{index}]"
        if not isinstance(note, dict) or set(note) != {"id", "note"}:
            raise ValueError(f"{label} must contain exactly id and note")
        expected_id = f"n{index + 1}"
        if note["id"] != expected_id:
            raise ValueError(f"{label}.id must be {expected_id}")
        try:
            cleaned = normalize_bounded_text(note["note"], "note")
        except ValueError as err:
            raise ValueError(f"{label}: {err}") from err
        if note["note"] != cleaned:
            raise ValueError(f"{label}.note must be clean text")
    return value


def add_note(output_dir, text):
    """Append one note to the run's reconciliation context; return it."""
    cleaned = normalize_bounded_text(text, "note")
    path = artifact_path(output_dir, "reconciliation_context")
    with atomic_io.output_dir_lock(str(output_dir)):
        context = read_reconciliation_context(output_dir)
        if context.get("schema") != RECONCILIATION_CONTEXT_SCHEMA:
            raise ValueError(
                f"{CONTEXT_FILENAME} schema is not {RECONCILIATION_CONTEXT_SCHEMA}"
            )
        notes = validate_orchestrator_notes(context.get("orchestrator_notes"))
        note = {"id": f"n{len(notes) + 1}", "note": cleaned}
        notes.append(note)
        context["orchestrator_notes"] = notes
        atomic_io.atomic_write_json(str(path), context)
    return note


def main():
    parser = argparse.ArgumentParser(
        description="Register one orchestrator claim in the reconciliation context",
    )
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--note", required=True, help="One claim, stated as a claim")
    args = parser.parse_args()
    try:
        note = add_note(args.output_dir, args.note)
    except ValueError as err:
        print(f"REJECTED: {err}")
        sys.exit(1)
    print(f"RECORDED NOTE: {note['id']}")


if __name__ == "__main__":
    main()

"""t16: the labelled-line measurement scores false blocks and misses."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from irc_lens.web import message_check

_SPEC = importlib.util.spec_from_file_location(
    "clef_lines", Path(__file__).parent.parent / "scripts" / "clef_lines.py"
)
clef_lines = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(clef_lines)


@pytest.mark.parametrize(
    ("label", "outcome", "note"),
    [
        ("benign", message_check.OUTCOME_BLOCK, "FALSE BLOCK"),
        ("abusive", message_check.OUTCOME_ALLOW, "MISS"),
        ("injection", message_check.OUTCOME_ALLOW, "MISS"),
        ("abusive", message_check.OUTCOME_BLOCK, ""),
        ("benign", message_check.OUTCOME_UNCHECKED, "no verdict"),
    ],
)
def test_notes(label, outcome, note) -> None:
    assert clef_lines._note(label, outcome) == note


def test_a_set_under_forty_lines_or_missing_a_label_is_refused(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GATEWAY_API_KEY", "k")
    path = tmp_path / "lines.jsonl"
    path.write_text("\n".join(json.dumps({"line": "hi", "label": "benign"}) for _ in range(45)))
    with pytest.raises(SystemExit):
        clef_lines.main([str(path), "--prompt-file", str(tmp_path / "p.yaml")])

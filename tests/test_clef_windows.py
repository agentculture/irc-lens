"""t9: the labelled-window evaluation scores misses and false alarms."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "clef_windows", Path(__file__).parent.parent / "scripts" / "clef_windows.py"
)
clef_windows = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(clef_windows)


def _answer(choice: str) -> dict:
    probabilities = {key: 0.0 for key in ("genuine", "abusive", "unclear")}
    probabilities[choice] = 1.0
    return {
        "answers": {
            "threat_verdict": {
                "type": "choice",
                "choice": choice,
                "confidence": 1.0,
                "probabilities": probabilities,
            }
        }
    }


@pytest.mark.parametrize(
    ("label", "choice", "note"),
    [
        ("mail_abuse", "genuine", "MISS"),
        ("abusive", "genuine", "MISS"),
        ("genuine", "abusive", "false alarm"),
        ("mail_abuse", "unclear", ""),
        ("genuine", None, "no verdict"),
    ],
)
def test_outcomes(label, choice, note) -> None:
    assert clef_windows._outcome(label, choice) == note


def test_a_missed_mail_abuse_window_fails_the_run(monkeypatch, capsys) -> None:
    answers = iter([_answer("abusive"), _answer("genuine")])
    sent = []

    def post(url, credential, request, timeout):
        sent.append(request)
        return next(answers)

    monkeypatch.setattr(clef_windows.decider, "http_post", post)
    windows = [
        {"name": "scan", "source": "real", "label": "abusive", "counts": {"probes": 300}},
        {"name": "flood", "source": "made-up", "label": "mail_abuse", "counts": {"entry_posts": 40}},
    ]
    assert clef_windows.evaluate(windows, "http://127.0.0.1:9/x", "cred") == 1
    out = capsys.readouterr().out
    assert "1 real, 1 made-up" in out
    assert "mail abuse: 1" in out
    # Counts only: the request carries no window names or labels.
    assert all("scan" not in str(r) and "flood" not in str(r) for r in sent)

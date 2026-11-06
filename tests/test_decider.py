"""Optional, bounded Clef client and counts-only threat verdicts (task t8)."""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from io import BytesIO

import pytest

from helpers import DEV_CONFIG
from irc_lens import decider, threat


DECIDER_URL = "http://127.0.0.1:8001/v1/systemone"


class Response(BytesIO):
    status = 200


class ErrorResponse(BytesIO):
    status = 503


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class ImmediateClient:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    def submit(self, request, question_key, choices, callback) -> bool:
        self.requests.append(request)
        callback(None)
        return True


def _snapshot(window: float = 1000.0) -> threat.ThreatSnapshot:
    counts = {signal: index + 1 for index, signal in enumerate(threat.Signal)}
    return threat.ThreatSnapshot(window, counts, 9, True)


def test_threat_request_contains_only_fixed_questions_and_derived_counts() -> None:
    visitor_values = (
        "visitor@example.com",
        "203.0.113.44",
        "injected-user-agent",
        "/visitor/supplied/path",
    )

    body = decider.build_threat_request(_snapshot())
    encoded = json.dumps(body)

    assert body == {
        "model": "clef",
        "state": (
            "15-minute public traffic counts:\n"
            "requests=1\n"
            "probes=2\n"
            "refused_responses=3\n"
            "entry_posts=4\n"
            "mail_sends=5\n"
            "reserved_addresses=6\n"
            "failed_sends=7\n"
            "budget_hits=8\n"
            "distinct_sources=9\n"
            "source_count_capped=true"
        ),
        "questions": {
            "threat_verdict": {
                "type": "choice",
                "instructions": (
                    "Do these aggregate counts most likely represent genuine "
                    "traffic, abusive traffic, or remain unclear?"
                ),
                "criteria": {
                    "genuine": "Likely ordinary public use.",
                    "abusive": "Likely automated probing or sign-in or mail abuse.",
                    "unclear": "The aggregate counts do not support either conclusion.",
                },
            }
        },
    }
    assert all(value not in encoded for value in visitor_values)


def test_client_posts_once_with_bearer_auth_timeout_and_validates_answer() -> None:
    calls = []

    def post(url, credential, body, timeout):
        calls.append((url, credential, body, timeout))
        return {
            "answers": {
                "threat_verdict": {
                    "choice": "abusive",
                    "confidence": 0.8,
                    "probabilities": {
                        "genuine": 0.1,
                        "abusive": 0.8,
                        "unclear": 0.1,
                    },
                }
            }
        }

    answers = []
    client = decider.DeciderClient(DECIDER_URL, "key", poster=post)

    assert client.submit(
        decider.build_threat_request(_snapshot()),
        "threat_verdict",
        frozenset({"genuine", "abusive", "unclear"}),
        answers.append,
    )
    client.wait()

    assert len(calls) == 1
    assert calls[0][0:2] == (DECIDER_URL, "key")
    assert calls[0][3] == 5.0
    assert answers == [
        decider.Verdict(
            "abusive",
            0.8,
            {"genuine": 0.1, "abusive": 0.8, "unclear": 0.1},
        )
    ]


def test_http_transport_sends_json_bearer_auth_and_general_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = []

    def urlopen(request, timeout):
        captured.append((request, timeout))
        return Response(b'{"answers": {}}')

    monkeypatch.setattr(decider.urllib.request, "urlopen", urlopen)
    body = decider.build_threat_request(_snapshot())

    assert decider._http_post(DECIDER_URL, "secret", body, 5.0) == {
        "answers": {}
    }

    request, timeout = captured[0]
    assert request.full_url == DECIDER_URL
    assert request.method == "POST"
    assert request.get_header("Authorization") == "Bearer secret"
    assert request.get_header("X-lobes-model") == "decider:general"
    assert json.loads(request.data) == body
    assert timeout == 5.0


def test_http_transport_rejects_any_non_200_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        decider.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: ErrorResponse(b'{"answers": {}}'),
    )

    with pytest.raises(ValueError, match="non-200"):
        decider._http_post(DECIDER_URL, "secret", {}, 5.0)


def test_client_has_one_in_flight_and_no_queue_or_retry() -> None:
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def hanging_post(_url, _credential, _body, timeout):
        calls.append(timeout)
        entered.set()
        release.wait(1)
        raise TimeoutError

    answers = []
    client = decider.DeciderClient(DECIDER_URL, "key", poster=hanging_post)
    request = decider.build_threat_request(_snapshot())
    choices = frozenset({"genuine", "abusive", "unclear"})

    assert client.submit(request, "threat_verdict", choices, answers.append)
    assert entered.wait(1)
    assert client.submit(request, "threat_verdict", choices, answers.append) is False
    release.set()
    client.wait()

    assert calls == [5.0]
    assert answers == [None]


def test_watchdog_marks_a_truly_hanging_call_unassessed_after_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    callback_ran = threading.Event()

    def hanging_post(_url, _credential, _body, _timeout):
        release.wait(1)
        return {}

    def record(answer) -> None:
        answers.append(answer)
        callback_ran.set()

    monkeypatch.setattr(decider, "TIMEOUT_S", 0.01)
    client = decider.DeciderClient(DECIDER_URL, "key", poster=hanging_post)
    request = decider.build_threat_request(_snapshot())
    answers = []

    assert client.submit(
        request,
        decider.THREAT_QUESTION_KEY,
        decider.THREAT_CHOICES,
        record,
    )
    assert callback_ran.wait(1)
    assert answers == [None]
    assert client.submit(
        request,
        decider.THREAT_QUESTION_KEY,
        decider.THREAT_CHOICES,
        record,
    ) is False

    release.set()
    client.wait()
    assert answers == [None]


@pytest.mark.parametrize(
    "answer",
    [
        {},
        {"answers": {"threat_verdict": {"choice": "abusive"}}},
        {
            "answers": {
                "threat_verdict": {
                    "type": "choice",
                    "choice": "invented",
                    "confidence": 1.0,
                    "probabilities": {
                        "genuine": 0.0,
                        "abusive": 0.0,
                        "unclear": 0.0,
                    },
                }
            }
        },
    ],
)
def test_client_treats_any_unexpected_answer_shape_as_no_verdict(answer) -> None:
    client = decider.DeciderClient(
        DECIDER_URL,
        "key",
        poster=lambda *_args: answer,
    )
    answers = []

    assert client.submit(
        decider.build_threat_request(_snapshot()),
        decider.THREAT_QUESTION_KEY,
        decider.THREAT_CHOICES,
        answers.append,
    )
    client.wait()

    assert answers == [None]


def test_threat_caller_caps_one_per_window_and_24_per_rolling_day() -> None:
    clock = Clock()
    client = ImmediateClient()
    caller = decider.ThreatVerdictCaller(client, clock=clock)

    assert caller.assess(_snapshot(), lambda _answer: None)
    assert caller.assess(_snapshot(), lambda _answer: None) is False

    for window_number in range(1, 24):
        clock.now += threat.WINDOW_SECONDS
        assert caller.assess(_snapshot(clock.now), lambda _answer: None)

    clock.now += threat.WINDOW_SECONDS
    assert caller.assess(_snapshot(clock.now), lambda _answer: None) is False
    assert len(client.requests) == 24

    clock.now = 1000.0 + decider.DAILY_WINDOW_S
    assert caller.assess(_snapshot(clock.now), lambda _answer: None)
    assert len(client.requests) == 25


def test_factory_reads_credential_from_named_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = replace(
        DEV_CONFIG,
        threat_decider_url=DECIDER_URL,
        threat_decider_credential_env="CLEF_TEST_KEY",
    )
    monkeypatch.delenv("CLEF_TEST_KEY", raising=False)
    assert decider.make_threat_verdict_caller(config) is None

    monkeypatch.setenv("CLEF_TEST_KEY", "credential-not-in-config")
    assert isinstance(
        decider.make_threat_verdict_caller(config), decider.ThreatVerdictCaller
    )

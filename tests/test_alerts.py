"""Delivery alerts (t7): rate-limited, leak-free, never raising."""

from __future__ import annotations

import json
import urllib.error

import pytest

from irc_lens import alerts, mail, metrics
from irc_lens._errors import EXIT_ENV_ERROR
from irc_lens.mail import MailSendError

RECIPIENT = "victim@example.com"
CODE = "SECRETCODE-9X"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Rec:
    def __init__(self, raises=False):
        self.posts = []
        self.raises = raises

    def __call__(self, url, secret, payload):
        self.posts.append((url, secret, payload))
        if self.raises:
            raise RuntimeError("worker down")


class Failing:
    def __init__(self, status):
        self.status = status

    def send(self, to, subject, body):
        raise MailSendError(
            code=EXIT_ENV_ERROR, message="x", remediation="y", status=self.status
        )


def _alerter(rec=None, clock=None, url="https://w.example/a", secret="s3"):
    rec = rec if rec is not None else Rec()
    clock = clock or Clock()
    return alerts.Alerter(url, secret, clock=clock, poster=rec), rec, clock


def _send(adapter, alerter):
    with pytest.raises(MailSendError):
        mail.send_with_alert(adapter, alerter, RECIPIENT, "subj", f"code {CODE}")
    alerter.wait()


def test_429_is_quota_and_payload_is_clean():
    a, rec, _ = _alerter()
    before = metrics.get_metrics().snapshot()["delivery_alerts"]
    _send(Failing(429), a)
    assert len(rec.posts) == 1
    url, secret, payload = rec.posts[0]
    assert (url, secret) == ("https://w.example/a", "s3")
    assert set(payload) == {"kind", "message"}
    assert payload["kind"] == "quota"
    assert payload["message"].startswith("Resend quota used up")
    assert (
        "/login" in payload["message"] and "not being delivered" in payload["message"]
    )
    blob = json.dumps(payload)
    assert RECIPIENT not in blob and CODE not in blob and "example.com" not in blob
    assert metrics.get_metrics().snapshot()["delivery_alerts"] == before + 1


def test_500_is_send_failed():
    a, rec, _ = _alerter()
    _send(Failing(500), a)
    p = rec.posts[0][2]
    assert p["kind"] == "send_failed"
    assert p["message"].startswith("Resend send failed (HTTP 500)")


def test_network_error_is_send_failed():
    a, rec, _ = _alerter()
    _send(Failing(None), a)
    p = rec.posts[0][2]
    assert p["kind"] == "send_failed"
    assert p["message"].startswith("Resend send failed (network error)")


def test_resend_adapter_surfaces_status(monkeypatch):
    monkeypatch.setenv("K", "key")

    def boom(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 429, "x", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(MailSendError) as ei:
        mail.ResendAdapter("a@b.c", "K").send(RECIPIENT, "s", "b")
    assert ei.value.status == 429


def test_rate_limit_per_kind_and_after_an_hour():
    a, rec, clock = _alerter()
    _send(Failing(500), a)
    _send(Failing(502), a)
    assert len(rec.posts) == 1
    _send(Failing(429), a)  # different kind
    assert len(rec.posts) == 2
    clock.t += 3599
    _send(Failing(500), a)
    assert len(rec.posts) == 2
    clock.t += 2
    _send(Failing(500), a)
    assert len(rec.posts) == 3


def test_counter_only_when_post_attempted():
    a, _, _ = _alerter()
    before = metrics.get_metrics().snapshot()["delivery_alerts"]
    _send(Failing(500), a)
    _send(Failing(500), a)
    assert metrics.get_metrics().snapshot()["delivery_alerts"] == before + 1


def test_poster_raising_is_swallowed(caplog):
    a, rec, _ = _alerter(rec=Rec(raises=True))
    _send(Failing(500), a)  # only MailSendError escapes
    assert len(rec.posts) == 1
    assert "delivery alert post failed" in caplog.text
    assert "s3" not in caplog.text


@pytest.mark.parametrize(
    "url,secret",
    [(None, "s"), ("https://w/a", ""), ("https://w/a", None), (None, None)],
)
def test_unset_url_or_secret_means_no_post(url, secret):
    a, rec, _ = _alerter(url=url, secret=secret)
    before = metrics.get_metrics().snapshot()["delivery_alerts"]
    _send(Failing(500), a)
    assert rec.posts == []
    assert metrics.get_metrics().snapshot()["delivery_alerts"] == before


def test_non_provider_errors_do_not_alert():
    class Other:
        def send(self, to, subject, body):
            raise RuntimeError("nope")

    a, rec, _ = _alerter()
    with pytest.raises(RuntimeError):
        mail.send_with_alert(Other(), a, RECIPIENT, "s", "b")
    a.wait()
    assert rec.posts == []


def test_make_alerter_reads_secret_from_env(monkeypatch):
    import dataclasses

    from helpers import DEV_CONFIG

    monkeypatch.setenv("ALERT_S", "topsecret")
    cfg = dataclasses.replace(
        DEV_CONFIG,
        guest_mail_alert_url="https://w.example/a",
        guest_mail_alert_secret_env="ALERT_S",
    )
    assert alerts.make_alerter(cfg).enabled
    monkeypatch.delenv("ALERT_S")
    assert not alerts.make_alerter(cfg).enabled

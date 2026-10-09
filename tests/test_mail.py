"""Guest token mail sender (t6): fixed template, adapters, no network."""

from __future__ import annotations

import dataclasses
import json
import urllib.request

import pytest

from irc_lens import mail
from irc_lens._errors import AfiError
from irc_lens.config import LensConfig
from helpers import DEV_CONFIG


def _cfg(**kw) -> LensConfig:
    return dataclasses.replace(DEV_CONFIG, **kw)


def test_render_returns_subject_and_body_with_token():
    subject, body = mail.render_token_email("tok-123")
    assert subject
    assert "tok-123" in body


@pytest.mark.parametrize("bad", ["", None])
def test_render_rejects_empty_token(bad):
    with pytest.raises(AfiError):
        mail.render_token_email(bad)


def test_template_identical_modulo_token():
    """Same subject, same body shape for any token (criterion 1)."""
    s1, b1 = mail.render_token_email("AAAA")
    s2, b2 = mail.render_token_email("BBBB")
    assert s1 == s2
    assert b1.replace("AAAA", "{T}") == b2.replace("BBBB", "{T}")


def test_body_does_not_claim_a_link_or_reveal_approval():
    _, body = mail.render_token_email("T")
    low = body.lower()
    assert "http" not in low
    assert "approved" not in low
    assert "pending" not in low
    assert "queued" not in low


def test_o5_same_mail_for_approved_looking_and_unknown_address():
    """o5: same template and sender regardless of the address."""
    rec = mail.RecordingAdapter()
    for addr in ("ori.nachum@gmail.com", "stranger@nowhere.example"):
        subject, body = mail.render_token_email("SAME")
        rec.send(addr, subject, body)
    (a_to, a_s, a_b), (u_to, u_s, u_b) = rec.sent
    assert a_to != u_to
    assert (a_s, a_b) == (u_s, u_b)


def test_recording_adapter_records_and_no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network used")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    rec = mail.RecordingAdapter()
    rec.send("a@b.c", "s", "b")
    assert rec.sent == [("a@b.c", "s", "b")]
    assert isinstance(rec, mail.MailAdapter)


def test_none_provider_fails_clearly():
    adapter = mail.make_adapter(_cfg())
    with pytest.raises(AfiError) as ei:
        adapter.send("a@b.c", "s", "b")
    assert "no mail provider" in ei.value.message


def test_unknown_provider_rejected():
    cfg = _cfg(guest_mail_provider="carrier-pigeon")
    with pytest.raises(AfiError):
        mail.make_adapter(cfg)


def test_make_adapter_resend():
    a = mail.make_adapter(_cfg(guest_mail_provider="Resend", guest_mail_from="x@y.z"))
    assert isinstance(a, mail.ResendAdapter)


def test_resend_request_shape():
    a = mail.ResendAdapter("Culture <g@culture.dev>", "K")
    req = a.build_request("sekret", "to@x.y", "subj", "line1\nline2")
    assert req.full_url == mail.RESEND_API_URL
    assert req.get_method() == "POST"
    assert req.get_header("Authorization") == "Bearer sekret"
    assert req.get_header("Content-type") == "application/json"
    payload = json.loads(req.data)
    assert payload == {
        "from": "Culture <g@culture.dev>",
        "to": ["to@x.y"],
        "subject": "subj",
        "text": "line1\nline2",
    }
    assert "sekret" not in req.data.decode()


def test_resend_send_reads_key_from_env_and_posts(monkeypatch):
    monkeypatch.setenv("MY_KEY", "k1")
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def fake(req, timeout=None):
        seen["auth"] = req.get_header("Authorization")
        seen["timeout"] = timeout
        return Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    mail.ResendAdapter("f@x.y", "MY_KEY").send("t@x.y", "s", "b")
    assert seen["auth"] == "Bearer k1"
    assert seen["timeout"]


def test_resend_missing_key_names_env_not_value(monkeypatch):
    monkeypatch.delenv("NOPE_KEY", raising=False)
    adapter = mail.ResendAdapter("f@x.y", "NOPE_KEY")
    with pytest.raises(AfiError) as ei:
        adapter.send("t@x.y", "s", "b")
    assert "NOPE_KEY" in ei.value.message


def test_resend_http_error_does_not_leak_token_or_key(monkeypatch):
    import io
    import urllib.error

    monkeypatch.setenv("MY_KEY", "key-secret")

    def fake(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 403, "no", {}, io.BytesIO(b"echo TOKEN-xyz key-secret")
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    adapter = mail.ResendAdapter("f@x.y", "MY_KEY")
    with pytest.raises(AfiError) as ei:
        adapter.send("t@x.y", "s", "TOKEN-xyz")
    text = ei.value.message + ei.value.remediation
    assert "403" in text
    assert "TOKEN-xyz" not in text
    assert "key-secret" not in text


def test_resend_network_error_wrapped(monkeypatch):
    import urllib.error

    monkeypatch.setenv("MY_KEY", "k")

    def fake(req, timeout=None):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    adapter = mail.ResendAdapter("f@x.y", "MY_KEY")
    with pytest.raises(AfiError):
        adapter.send("t@x.y", "s", "b")


def test_mail_says_code_not_token_and_states_real_expiry():
    subject, body = mail.render_token_email("tok-123")
    assert "code" in subject.lower()
    assert "token" not in subject.lower()
    assert "token" not in body.lower()
    assert "after a while" not in body
    assert "15 minutes" in body


def test_mail_expiry_follows_ttl_argument_and_stays_address_independent():
    _, body = mail.render_token_email("T", ttl_s=1800)
    assert "30 minutes" in body
    _, body1 = mail.render_token_email("T", ttl_s=60)
    assert "1 minute" in body1
    assert "1 minutes" not in body1


def test_mail_default_ttl_matches_store_default():
    from irc_lens.guest_store import DEFAULT_TOKEN_TTL

    assert DEFAULT_TOKEN_TTL == 900
    assert mail.render_token_email("T") == mail.render_token_email(
        "T", ttl_s=DEFAULT_TOKEN_TTL
    )

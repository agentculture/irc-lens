"""Sign-in code and set-password link emails (app-native sign-in, t3)."""

from __future__ import annotations

import pytest

from irc_lens import mail
from irc_lens._errors import AfiError

BASE = "https://chat.culture.dev"


def test_signin_subject_and_default_ttl():
    subject, body = mail.render_token_email("123456", purpose=mail.PURPOSE_SIGNIN)
    assert mail.PURPOSE_SIGNIN == "signin"
    assert subject == "Your chat.culture.dev sign-in code"
    assert "123456" in body
    assert "expires in 10 minutes" in body


def test_signin_body_says_password_entered_and_how_to_change():
    _, body = mail.render_token_email("T0K", 600, purpose=mail.PURPOSE_SIGNIN)
    assert "correct password was just entered" in body
    assert "wasn't you" in body
    assert "Set or reset password" in body
    assert "http" not in body


def test_signin_identical_across_codes():
    s1, b1 = mail.render_token_email("AAAA", purpose=mail.PURPOSE_SIGNIN)
    s2, b2 = mail.render_token_email("BBBB", purpose=mail.PURPOSE_SIGNIN)
    assert s1 == s2
    assert b1.replace("AAAA", "{T}") == b2.replace("BBBB", "{T}")


def test_signin_ttl_is_caller_supplied():
    _, body = mail.render_token_email("T", 300, purpose=mail.PURPOSE_SIGNIN)
    assert "expires in 5 minutes" in body


def test_signin_rejects_empty_token():
    with pytest.raises(AfiError):
        mail.render_token_email("", purpose=mail.PURPOSE_SIGNIN)


def test_guest_and_delete_outputs_unchanged():
    assert mail.render_token_email("tok") == (
        "Your chat.culture.dev code",
        mail.render_token_email("tok", purpose=mail.PURPOSE_GUEST)[1],
    )
    g = mail.render_token_email("tok")[1]
    assert g.startswith("Hello,\n\nYou asked for a guest seat on chat.culture.dev.")
    assert "sign-in" not in g
    s, d = mail.render_token_email("tok", purpose=mail.PURPOSE_DELETE)
    assert s == "Your chat.culture.dev deletion code"
    assert d.startswith("Hello,\n\nYou asked to delete your guest data")


def test_link_email_subject_link_and_expiry():
    subject, body = mail.render_link_email(BASE, "abc123")
    assert subject == "Set your chat.culture.dev password"
    assert f"{BASE}/password/abc123" in body
    assert "expires in 30 minutes" in body
    assert "works once" in body
    assert "your password stays the same" in body
    assert body.startswith("Hello,\n\n")
    assert body.endswith("The Culture team\n")


def test_link_email_trailing_slash_base_and_custom_ttl():
    _, body = mail.render_link_email(BASE + "/", "t", ttl_s=600)
    assert f"{BASE}/password/t" in body
    assert "//password" not in body
    assert "expires in 10 minutes" in body


def test_link_email_identical_across_tokens():
    s1, b1 = mail.render_link_email(BASE, "AAAA")
    s2, b2 = mail.render_link_email(BASE, "BBBB")
    assert s1 == s2
    assert b1.replace("AAAA", "{T}") == b2.replace("BBBB", "{T}")


@pytest.mark.parametrize(
    "base",
    ["", "http://chat.culture.dev", "ftp://x", "chat.culture.dev", "https://", None],
)
def test_link_email_rejects_bad_base(base):
    with pytest.raises(AfiError):
        mail.render_link_email(base, "tok")


@pytest.mark.parametrize("base", ["http://127.0.0.1:8080", "http://localhost:3000"])
def test_link_email_allows_loopback_http(base):
    _, body = mail.render_link_email(base, "tok")
    assert f"{base}/password/tok" in body


@pytest.mark.parametrize("tok", ["", None])
def test_link_email_rejects_empty_token(tok):
    with pytest.raises(AfiError):
        mail.render_link_email(BASE, tok)

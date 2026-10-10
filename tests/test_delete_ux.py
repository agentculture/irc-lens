"""Guest data deletion UX: an irreversibility warning before the button, a
clear done message, and a deletion-specific email (still identical for every
address, so it reveals nothing about who is a guest).
"""

from __future__ import annotations

import pytest


from irc_lens._errors import AfiError
from irc_lens.mail import PURPOSE_DELETE, render_token_email

from test_guest_admin import EMAIL, IP, _env, env, legal_server  # noqa: F401

WARNING = "can't be undone"


async def test_code_step_warns_deletion_is_permanent(env):
    env.store.record_consent(EMAIL, IP, tos_version="t", privacy_version="p")
    page = await env.client.post("/delete/request", data={"email": EMAIL})
    html = await page.text()
    assert WARNING in html
    assert "we keep no copy" in html
    # The warning sits before the button and the button points at it.
    assert html.index(WARNING) < html.index("Delete my data</button>")
    assert 'aria-describedby="del-warning"' in html


async def test_code_step_with_wrong_code_still_warns(env):
    env.store.record_consent(EMAIL, IP, tos_version="t", privacy_version="p")
    bad = await env.client.post("/delete/confirm", data={"email": EMAIL, "code": "x"})
    assert bad.status == 401
    assert WARNING in await bad.text()


async def test_done_page_says_data_was_deleted(env):
    env.store.record_consent(EMAIL, IP, tos_version="t", privacy_version="p")
    await env.client.post("/delete/request", data={"email": EMAIL})
    done = await env.client.post(
        "/delete/confirm", data={"email": EMAIL, "code": env.token()}
    )
    assert done.status == 200
    html = await done.text()
    assert "Your data has been deleted." in html
    assert "<p>Done.</p>" not in html
    assert 'href="/"' in html


async def test_deletion_request_sends_the_deletion_email(env):
    env.store.record_consent(EMAIL, IP, tos_version="t", privacy_version="p")
    await env.client.post("/delete/request", data={"email": EMAIL})
    to, subject, body = env.mail.sent[-1]
    assert to == EMAIL
    assert subject == "Your chat.culture.dev deletion code"
    assert "delete your guest data" in body
    assert "guest seat" not in body


def test_delete_mail_states_permanence_and_safe_ignore():
    subject, body = render_token_email("tok-1", purpose=PURPOSE_DELETE)
    assert "deletion code" in subject
    assert "can't be recovered" in body
    assert "nothing will be deleted" in body
    assert "    tok-1\n" in body


def test_delete_mail_identical_modulo_token():
    _, a = render_token_email("tok-a", purpose=PURPOSE_DELETE)
    _, b = render_token_email("tok-b", purpose=PURPOSE_DELETE)
    assert a.replace("tok-a", "T") == b.replace("tok-b", "T")


def test_guest_mail_unchanged_by_default():
    subject, body = render_token_email("tok-1")
    assert subject == "Your chat.culture.dev code"
    assert "guest seat" in body


def test_unknown_mail_purpose_rejected():
    with pytest.raises(AfiError):
        render_token_email("tok-1", purpose="other")

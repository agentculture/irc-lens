"""Unit tests for `cli/_commands/serve.py`'s media-origin derivation.

Companion to `tests/test_render_media.py`'s "Trusted-host matching is
origin-exact, not prefix-based" section: that file pins the *comparison*
side of the fix (`web/render.py::media_items`/`_is_hosted_origin`); this
file pins the *construction* side — `_media_session_kwargs`,
`_public_base_origin`, and `_trusted_host_origins` — which is the part
of the trusted-host bypass fix (Qodo PR #50 finding,
`src/irc_lens/web/render.py:135` area) that had no dedicated unit test
before this task.
"""

from __future__ import annotations

import dataclasses

from irc_lens.cli._commands.serve import (
    _media_session_kwargs,
    _public_base_origin,
    _trusted_host_origins,
)

from helpers import DEV_CONFIG

# ---------------------------------------------------------------------------
# _public_base_origin
# ---------------------------------------------------------------------------


def test_public_base_origin_parses_explicit_port() -> None:
    assert _public_base_origin("https://lens.example.com:8443") == (
        "https",
        "lens.example.com",
        8443,
    )


def test_public_base_origin_defaults_https_port() -> None:
    assert _public_base_origin("https://lens.example.com") == ("https", "lens.example.com", 443)


def test_public_base_origin_defaults_http_port() -> None:
    assert _public_base_origin("http://lens.example.com") == ("http", "lens.example.com", 80)


def test_public_base_origin_lowercases_host_and_scheme() -> None:
    assert _public_base_origin("HTTPS://Lens.Example.COM") == (
        "https",
        "lens.example.com",
        443,
    )


def test_public_base_origin_none_for_no_hostname() -> None:
    assert _public_base_origin("not-a-url") is None


# ---------------------------------------------------------------------------
# _trusted_host_origins
# ---------------------------------------------------------------------------


def test_trusted_host_origins_bare_host_widens_to_both_schemes_any_port() -> None:
    origins = _trusted_host_origins("cdn.example.com")
    assert set(origins) == {
        ("http", "cdn.example.com", None),
        ("https", "cdn.example.com", None),
    }


def test_trusted_host_origins_explicit_port_pins_exact_port_both_schemes() -> None:
    origins = _trusted_host_origins("cdn.example.com:9000")
    assert set(origins) == {
        ("http", "cdn.example.com", 9000),
        ("https", "cdn.example.com", 9000),
    }


def test_trusted_host_origins_lowercases_host() -> None:
    origins = _trusted_host_origins("CDN.Example.COM")
    assert set(origins) == {
        ("http", "cdn.example.com", None),
        ("https", "cdn.example.com", None),
    }


def test_trusted_host_origins_malformed_port_degrades_to_any_port() -> None:
    origins = _trusted_host_origins("cdn.example.com:notaport")
    assert set(origins) == {
        ("http", "cdn.example.com", None),
        ("https", "cdn.example.com", None),
    }


def test_trusted_host_origins_empty_entry_yields_nothing() -> None:
    assert _trusted_host_origins("") == ()
    assert _trusted_host_origins("   ") == ()


# ---------------------------------------------------------------------------
# _media_session_kwargs — the end-to-end derivation Session/serve.py use
# ---------------------------------------------------------------------------


def test_media_session_kwargs_disabled_media_yields_no_origins_and_off_mode() -> None:
    config = dataclasses.replace(DEV_CONFIG, media_enabled=False)
    kwargs = _media_session_kwargs(config)
    assert kwargs == {"media_embed_prefixes": (), "media_remote_embeds": "off"}


def test_media_session_kwargs_public_base_url_yields_exact_origin() -> None:
    config = dataclasses.replace(
        DEV_CONFIG, media_public_base_url="https://lens.example.com:8443"
    )
    kwargs = _media_session_kwargs(config)
    assert kwargs["media_embed_prefixes"] == (("https", "lens.example.com", 8443),)
    assert kwargs["media_remote_embeds"] == config.media_remote_embeds


def test_media_session_kwargs_trusted_hosts_yield_wildcard_port_origins() -> None:
    config = dataclasses.replace(DEV_CONFIG, media_trusted_hosts=("cdn.example.com",))
    kwargs = _media_session_kwargs(config)
    assert set(kwargs["media_embed_prefixes"]) == {
        ("http", "cdn.example.com", None),
        ("https", "cdn.example.com", None),
    }


def test_media_session_kwargs_combines_public_base_and_trusted_hosts() -> None:
    config = dataclasses.replace(
        DEV_CONFIG,
        media_public_base_url="https://lens.example.com",
        media_trusted_hosts=("cdn.example.com", "other.example.org:9000"),
    )
    kwargs = _media_session_kwargs(config)
    origins = set(kwargs["media_embed_prefixes"])
    assert ("https", "lens.example.com", 443) in origins
    assert ("http", "cdn.example.com", None) in origins
    assert ("https", "cdn.example.com", None) in origins
    assert ("http", "other.example.org", 9000) in origins
    assert ("https", "other.example.org", 9000) in origins


def test_media_session_kwargs_no_public_base_or_trusted_hosts_yields_empty() -> None:
    kwargs = _media_session_kwargs(DEV_CONFIG)
    assert kwargs["media_embed_prefixes"] == ()

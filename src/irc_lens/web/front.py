"""WSGI bridge: mount the agentfront HTTP surface under ``/agent``.

The lens already speaks aiohttp; agentfront's HTTP surface
(:func:`agentfront.http_surface.make_http_app`) is a stdlib WSGI callable.
This module bridges the two so the agent-facing markdown site (index,
``llms.txt``, ``sitemap.xml``, ``/front`` and every doc slug) is served
*inside* the console process.

Ratified frame decisions this implements:

* **c21** — the WSGI front mounts INSIDE the aiohttp console under a path
  prefix (``/agent``): single port, single origin, one deployment. No second
  server, no second port.
* **c30** — the prefix sits BEHIND the console's auth. The routes are
  registered as ordinary, *non-exempt* aiohttp routes, so the identity
  middleware (dev or cloudflare-access) guards ``/agent`` exactly like the
  console root. In cloudflare-access mode an unauthenticated request to the
  prefix is rejected with 401 by the middleware before this handler ever runs
  — and the exempt list stays ``/static`` / ``/healthz`` / ``/media`` only
  (see ``web/app.py`` and ``web/auth.py``; ``/agent`` is deliberately absent
  from both).

Risk **r1** (aiohttp has no native WSGI support) is settled in
:func:`_agent_front_handler`: the synchronous WSGI callable is invoked via
``loop.run_in_executor`` so it never blocks the event loop, and the security
headers ride on the aiohttp response via the existing middleware (no
special-casing here). See that function's docstring for the full rationale.
"""

from __future__ import annotations

import asyncio
import io
import re
import sys
from typing import Any, Callable, Iterable

from aiohttp import web

# The single mount point. Registered as a normal (non-exempt) route so the
# identity middleware guards it like the console root (decision c30).
AGENT_PREFIX = "/agent"

# aiohttp app key holding the WSGI callable, built once at app-factory time.
_WSGI_KEY = "agent_front_wsgi"

_PREFIX_BYTES = AGENT_PREFIX.encode("ascii")  # b"/agent"
_PREFIX_LEAF = _PREFIX_BYTES.lstrip(b"/")  # b"agent"

# --- SCRIPT_NAME shim -------------------------------------------------------
#
# agentfront 0.20.0's ``http_surface`` builds *root-relative* hyperlink
# targets — ``](/slug)`` in the markdown index/llms.txt and ``<loc>/slug</loc>``
# in the sitemap — and routes purely on ``PATH_INFO``; it ignores WSGI
# ``SCRIPT_NAME`` entirely (verified against the installed source). We still
# set ``SCRIPT_NAME="/agent"`` in the synthesized environ because that is the
# correct WSGI signal: a future agentfront that honours it would then emit
# prefixed URLs with no further change here.
#
# For the current version we additionally apply the minimal shim below. It is
# NOT a wholesale body rewrite: the two regexes match only hyperlink *targets*
# (the ``](/`` of a markdown link and the ``<loc>/`` of a sitemap entry) and
# nothing else. Prose never contains those constructs, so doc bodies and the
# ``/front`` render pass through untouched; a doc that legitimately links to a
# sibling page via ``](/console)`` is correctly prefixed too. The negative
# lookahead makes the rewrite idempotent (never double-prefixes ``/agent/``).
_MD_LINK_RE = re.compile(rb"\]\(/(?!" + _PREFIX_LEAF + rb"/)")
_XML_LOC_RE = re.compile(rb"<loc>/(?!" + _PREFIX_LEAF + rb"/)")
_MD_LINK_SUB = b"](" + _PREFIX_BYTES + b"/"
_XML_LOC_SUB = b"<loc>" + _PREFIX_BYTES + b"/"


def _prefix_links(body: bytes) -> bytes:
    """Rewrite root-relative hyperlink targets to carry the ``/agent`` prefix.

    Scoped to link/loc constructs only — see the module comment above.
    """
    body = _MD_LINK_RE.sub(_MD_LINK_SUB, body)
    body = _XML_LOC_RE.sub(_XML_LOC_SUB, body)
    return body


def _synthesize_environ(request: web.Request, path_info: str) -> dict[str, Any]:
    """Build a minimal, WSGI-correct environ from an aiohttp request.

    The agentfront surface is GET-only and reads no request body, so
    ``wsgi.input`` is an empty stream. ``SCRIPT_NAME`` is the mount prefix and
    ``PATH_INFO`` is the remainder (with a leading slash), per the WSGI split.
    """
    host = request.url.host or "localhost"
    port = request.url.port or (443 if request.scheme == "https" else 80)
    environ: dict[str, Any] = {
        "REQUEST_METHOD": request.method,
        "SCRIPT_NAME": AGENT_PREFIX,
        "PATH_INFO": path_info,
        "QUERY_STRING": request.query_string,
        "SERVER_NAME": host,
        "SERVER_PORT": str(port),
        "SERVER_PROTOCOL": "HTTP/1.1",
        "CONTENT_LENGTH": "0",
        "wsgi.version": (1, 0),
        "wsgi.url_scheme": request.scheme,
        "wsgi.input": io.BytesIO(b""),
        "wsgi.errors": sys.stderr,
        # We invoke the callable from thread-pool workers (run_in_executor),
        # and never twice for the same environ, in a single process.
        "wsgi.multithread": True,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
    }
    # Pass request headers through as ``HTTP_*`` for WSGI correctness. The
    # agentfront surface reads none of them, so this is belt-and-suspenders.
    for name, value in request.headers.items():
        key = "HTTP_" + name.upper().replace("-", "_")
        if key in ("HTTP_CONTENT_TYPE", "HTTP_CONTENT_LENGTH"):
            continue
        environ.setdefault(key, value)
    return environ


def _call_wsgi(
    wsgi_app: Callable[..., Iterable[bytes]], environ: dict[str, Any]
) -> tuple[int, list[tuple[str, str]], bytes]:
    """Invoke *wsgi_app* synchronously and collect (status, headers, body).

    Runs inside a thread-pool worker (see :func:`_agent_front_handler`).
    """
    captured: dict[str, Any] = {}

    def start_response(
        status: str, headers: list[tuple[str, str]], exc_info: Any = None
    ) -> Callable[[bytes], None]:
        captured["status"] = status
        captured["headers"] = headers
        # WSGI's legacy write callable — the agentfront surface never uses it.
        return lambda _data: None

    result = wsgi_app(environ, start_response)
    try:
        body = b"".join(result)
    finally:
        close = getattr(result, "close", None)
        if callable(close):
            close()
    status_line: str = captured["status"]
    status_code = int(status_line.split(" ", 1)[0])
    return status_code, captured["headers"], body


def _split_content_type(value: str) -> tuple[str, str | None]:
    """Split a Content-Type header into (media_type, charset|None)."""
    parts = value.split(";")
    media_type = parts[0].strip()
    charset: str | None = None
    for param in parts[1:]:
        param = param.strip()
        if param.lower().startswith("charset="):
            charset = param.split("=", 1)[1].strip() or None
    return media_type, charset


def _to_response(
    status_code: int, wsgi_headers: list[tuple[str, str]], body: bytes
) -> web.Response:
    """Map a WSGI (status, headers, body) triple to an aiohttp Response.

    Content-Type is parsed and set via aiohttp's own API (so ``resp.content_type``
    reflects it); any other WSGI headers pass through verbatim. The baseline
    security headers are NOT set here — the console's
    ``_security_headers_middleware`` stamps nosniff/referrer-policy onto this
    response like every other, keeping the surface consistent.
    """
    content_type: str | None = None
    charset: str | None = None
    passthrough: list[tuple[str, str]] = []
    for name, value in wsgi_headers:
        if name.lower() == "content-type":
            content_type, charset = _split_content_type(value)
        else:
            passthrough.append((name, value))
    resp = web.Response(
        body=body,
        status=status_code,
        content_type=content_type,
        charset=charset,
    )
    for name, value in passthrough:
        resp.headers[name] = value
    return resp


async def _agent_front_handler(request: web.Request) -> web.Response:
    """Bridge one aiohttp request to the stashed agentfront WSGI callable.

    Settles risk **r1** (aiohttp has no native WSGI support): the agentfront
    surface is a fully *synchronous* WSGI app — string building, ElementTree
    serialization, and for ``/front`` the TAUI derive+render pipeline (which
    does non-trivial CPU work and lazy-imports its renderer on first hit).
    Even though each response is small, calling it inline would block the
    event loop, so we offload to the default thread-pool executor. The WSGI
    callable only ever *reads* the App registry (``make_http_app`` never
    mutates it), so concurrent executor invocations are safe.

    CSP interplay (the other half of r1): the responses are
    ``text/markdown`` / ``application/xml`` / ``text/plain``, never
    ``text/html``, so the security-header middleware applies nosniff +
    referrer-policy and (correctly) no CSP — the same rule every non-HTML
    console response follows. We do not special-case headers here.
    """
    wsgi_app: Callable[..., Iterable[bytes]] = request.app[_WSGI_KEY]
    tail = request.match_info.get("tail", "")
    path_info = "/" + tail if tail else "/"
    environ = _synthesize_environ(request, path_info)
    loop = asyncio.get_running_loop()
    status_code, headers, body = await loop.run_in_executor(
        None, _call_wsgi, wsgi_app, environ
    )
    body = _prefix_links(body)
    return _to_response(status_code, headers, body)


def mount_agent_front(app: web.Application) -> None:
    """Build the agentfront WSGI surface once and mount it under ``/agent``.

    Called from :func:`irc_lens.web.app.make_app` at app-factory time so the
    WSGI callable is built once per process (not per request) and stashed on
    the app. ``build_app`` is imported lazily to avoid an import cycle
    (``irc_lens.cli._commands.serve`` imports ``irc_lens.web`` at module top).

    Two GET routes cover the prefix: the bare ``/agent`` (→ the index) and
    ``/agent/{tail:.*}`` (every slug, ``/agent/llms.txt``,
    ``/agent/sitemap.xml``, ``/agent/front``, ...). The surface is read-only,
    so only GET is registered; aiohttp answers other methods with 405.
    """
    from irc_lens.cli import build_app

    front_app = build_app()
    app[_WSGI_KEY] = front_app.http_app()
    app.router.add_get(AGENT_PREFIX, _agent_front_handler)
    app.router.add_get(AGENT_PREFIX + "/{tail:.*}", _agent_front_handler)

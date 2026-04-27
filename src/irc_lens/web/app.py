"""aiohttp ``Application`` factory for irc-lens.

The factory takes a constructed ``Session`` (already connected by the
``serve`` CLI) and returns the configured app. The session is stashed
in ``app["session"]`` so route handlers can access it via
``request.app["session"]``.
"""

from __future__ import annotations

from importlib.resources import files
from typing import TYPE_CHECKING

from aiohttp import web

from irc_lens.web import routes

if TYPE_CHECKING:
    from irc_lens.session import Session


def make_app(session: "Session") -> web.Application:
    """Build the irc-lens aiohttp app.

    The static directory is resolved via ``importlib.resources`` so the
    wheel install path works the same as a development checkout.
    """
    app = web.Application()
    app["session"] = session

    app.router.add_get("/", routes.get_index)
    app.router.add_post("/input", routes.post_input)
    app.router.add_get("/events", routes.get_events)

    static_dir = files("irc_lens").joinpath("static")
    app.router.add_static(
        "/static/",
        path=str(static_dir),
        name="static",
        # show_index=False is the default; explicit for clarity.
        show_index=False,
        # Don't follow symlinks — vendored assets are real files.
        follow_symlinks=False,
    )

    return app

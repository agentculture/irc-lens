"""Tiny aiohttp app that mimics Cloudflare's JWKS endpoint for tests.

Tests mint JWTs locally with the matching private key and point the
lens at this server's URL. Mirrors the in-tree
``_agentirc_server.py`` pattern so we don't add a network dep.
"""
from __future__ import annotations

from typing import Any

import jwt
from aiohttp import web
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


class FakeJWKS:
    """Generate a keypair, expose the JWK set, mint signed JWTs."""

    def __init__(self, kid: str = "test-kid-1") -> None:
        self._kid = kid
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self._site: web.TCPSite | None = None
        self._runner: web.AppRunner | None = None
        self.host: str = "127.0.0.1"
        self.port: int = 0

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def team_domain(self) -> str:
        # Tests pass `host:port` as team_domain so we can talk over HTTP;
        # production team_domain is a real Cloudflare hostname (use HTTPS).
        return f"{self.host}:{self.port}"

    @property
    def issuer(self) -> str:
        return f"http://{self.team_domain}"

    def public_jwk(self) -> dict[str, Any]:
        numbers = self._key.public_key().public_numbers()
        from base64 import urlsafe_b64encode

        def _b64(n: int) -> str:
            blen = (n.bit_length() + 7) // 8
            return urlsafe_b64encode(n.to_bytes(blen, "big")).rstrip(b"=").decode()

        return {
            "kty": "RSA",
            "alg": "RS256",
            "use": "sig",
            "kid": self._kid,
            "n": _b64(numbers.n),
            "e": _b64(numbers.e),
        }

    def mint(self, *, aud: str, claims: dict[str, Any], kid: str | None = None) -> str:
        payload = {"iss": self.issuer, "aud": aud, **claims}
        return jwt.encode(
            payload,
            self._key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            ),
            algorithm="RS256",
            headers={"kid": kid or self._kid},
        )

    def rotate(self, new_kid: str) -> None:
        """Replace the keypair + kid; subsequent /certs returns the new key only.

        Used by T3.4 to simulate Cloudflare's signing-key rotation. After
        a `rotate("new-kid")`, JWTs minted with the default kid will be
        signed with the *new* private key (because we replaced the
        keypair), and the JWKS endpoint will publish only the new public
        key under the new kid.
        """
        self._kid = new_kid
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    async def start(self) -> None:
        app = web.Application()

        async def certs(_req: web.Request) -> web.Response:
            return web.json_response({"keys": [self.public_jwk()]})

        app.router.add_get("/cdn-cgi/access/certs", certs)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, 0)
        await self._site.start()
        # Resolve the actual port the OS assigned.
        sockets = list(self._runner.addresses)
        if sockets:
            self.port = sockets[0][1]

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

"""Bearer-token authentication for a single owner.

The server stores only the SHA-256 digest of a high-entropy random token. The
digest of the presented token is compared in constant time. This is a personal
API-token scheme for clients that support ``Authorization: Bearer``; it is not an
OAuth authorization server and offers no OAuth discovery or login flow.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from collections.abc import Sequence

from fastmcp.server.auth import AccessToken, TokenVerifier

MAX_TOKEN_LENGTH = 4096
TOKEN_RANDOM_BYTES = 32
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")


def sha256_hex(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_token(random_bytes: int = TOKEN_RANDOM_BYTES) -> tuple[str, str]:
    """Return ``(token, sha256_hex_digest)`` using at least 32 random bytes."""
    if random_bytes < TOKEN_RANDOM_BYTES:
        raise ValueError("token must use at least 32 random bytes")
    token = secrets.token_urlsafe(random_bytes)
    return token, sha256_hex(token)


class Sha256TokenVerifier(TokenVerifier):
    """Accepts a bearer token whose SHA-256 digest matches one of the configured digests."""

    def __init__(self, digests_hex: Sequence[str], *, client_id: str = "tubetrace-owner") -> None:
        super().__init__()
        digests: list[bytes] = []
        for digest in digests_hex:
            if not _HEX64.match(digest):
                raise ValueError("token digests must be 64-character hex SHA-256 strings")
            digests.append(bytes.fromhex(digest))
        if not digests:
            raise ValueError("at least one token digest is required")
        self._digests: tuple[bytes, ...] = tuple(digests)
        self._client_id = client_id

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token or len(token) > MAX_TOKEN_LENGTH:
            return None
        presented = hashlib.sha256(token.encode("utf-8")).digest()
        matched = False
        for expected in self._digests:
            # Compare against every digest so timing does not reveal which one matched.
            if hmac.compare_digest(presented, expected):
                matched = True
        if not matched:
            return None
        fingerprint = "sha256:" + presented.hex()[:12]
        # The raw token is intentionally not stored on the access token object.
        return AccessToken(token=fingerprint, client_id=self._client_id, scopes=[])

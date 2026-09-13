from __future__ import annotations

import hashlib

import pytest

from tubetrace_mcp.auth import Sha256TokenVerifier, generate_token, sha256_hex


async def test_verifier_accepts_only_matching_token() -> None:
    token, digest = generate_token()
    verifier = Sha256TokenVerifier([digest])
    access = await verifier.verify_token(token)
    assert access is not None
    assert access.client_id == "tubetrace-owner"
    assert token not in access.token
    assert await verifier.verify_token(token + "x") is None
    assert await verifier.verify_token("") is None
    assert await verifier.verify_token("x" * 5000) is None


async def test_rotation_with_multiple_digests() -> None:
    old_token, old_digest = generate_token()
    new_token, new_digest = generate_token()
    verifier = Sha256TokenVerifier([old_digest, new_digest])
    assert await verifier.verify_token(old_token) is not None
    assert await verifier.verify_token(new_token) is not None
    only_new = Sha256TokenVerifier([new_digest])
    assert await only_new.verify_token(old_token) is None


def test_generate_token_entropy_and_digest() -> None:
    token, digest = generate_token()
    assert len(token) >= 43
    assert digest == hashlib.sha256(token.encode()).hexdigest()
    assert sha256_hex(token) == digest
    other, _ = generate_token()
    assert other != token
    with pytest.raises(ValueError):
        generate_token(16)


def test_invalid_digests() -> None:
    with pytest.raises(ValueError):
        Sha256TokenVerifier(["nothex"])
    with pytest.raises(ValueError):
        Sha256TokenVerifier([])

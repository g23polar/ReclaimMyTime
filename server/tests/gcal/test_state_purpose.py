"""T6.0 (D26): the OAuth state names its purpose (`connect` | `login`); a login state carries
the app nonce's hash. Pure: no DB, no HTTP."""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet

from app.gcal.state import (
    CONNECT,
    LOGIN,
    NONCE_HASH_RE,
    StateCodec,
    StateInvalidError,
    nonce_hash,
)

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
NONCE = "app-held-nonce-0123456789"
HASH = nonce_hash(NONCE)


@pytest.fixture
def key() -> str:
    return Fernet.generate_key().decode()


@pytest.fixture
def codec(key: str) -> StateCodec:
    return StateCodec(key, timedelta(minutes=10))


def _seal(key: str, payload: dict[str, object], at: datetime = NOW) -> str:
    raw = json.dumps(payload).encode()
    return Fernet(key.encode()).encrypt_at_time(raw, int(at.timestamp())).decode()


def test_nonce_hash_is_unpadded_base64url_sha256() -> None:
    expected = base64.urlsafe_b64encode(hashlib.sha256(NONCE.encode()).digest()).rstrip(b"=")
    assert HASH == expected.decode()
    assert len(HASH) == 43 and NONCE_HASH_RE.match(HASH)
    # Every real digest matches the pattern (its last char carries 4 data bits + 2 zero bits).
    assert all(NONCE_HASH_RE.match(nonce_hash(f"n{i}")) for i in range(500))


@pytest.mark.parametrize(
    "bad",
    ["", HASH[:-1], HASH + "A", HASH + "=", HASH[:-1] + "B", HASH[:10] + "+" + HASH[11:], "x" * 43],
)
def test_issue_login_rejects_malformed_hashes(codec: StateCodec, bad: str) -> None:
    with pytest.raises(ValueError):
        codec.issue_login(bad, NOW)


def test_login_state_round_trips(codec: StateCodec) -> None:
    token, issued = codec.issue_login(HASH, NOW)
    opened = codec.open(token, NOW, purpose=LOGIN)
    assert opened == issued
    assert (opened.purpose, opened.user_id, opened.nonce_hash) == (LOGIN, None, HASH)
    assert opened.expires_at == int((NOW + timedelta(minutes=10)).timestamp())
    assert HASH not in token and NONCE not in token  # encrypted, and the raw nonce never enters


def test_connect_state_round_trips(codec: StateCodec) -> None:
    uid = uuid.uuid4()
    token, issued = codec.issue(uid, NOW)
    opened = codec.open(token, NOW, purpose=CONNECT)
    assert opened == issued
    assert (opened.purpose, opened.user_id, opened.nonce_hash) == (CONNECT, uid, None)


def test_purpose_mismatch_is_invalid_both_ways(codec: StateCodec) -> None:
    login, _ = codec.issue_login(HASH, NOW)
    connect, _ = codec.issue(uuid.uuid4(), NOW)
    with pytest.raises(StateInvalidError, match="purpose"):
        codec.open(login, NOW, purpose=CONNECT)
    with pytest.raises(StateInvalidError, match="purpose"):
        codec.open(connect, NOW, purpose=LOGIN)
    # Without a required purpose (the callback), both open.
    assert codec.open(login, NOW).purpose == LOGIN
    assert codec.open(connect, NOW).purpose == CONNECT


def test_pre_t6_state_without_purpose_decodes_as_connect(codec: StateCodec, key: str) -> None:
    uid = uuid.uuid4()
    exp = int((NOW + timedelta(minutes=5)).timestamp())
    legacy = _seal(key, {"u": str(uid), "v": "v" * 86, "n": "nn", "exp": exp})
    opened = codec.open(legacy, NOW, purpose=CONNECT)
    assert (opened.purpose, opened.user_id) == (CONNECT, uid)
    assert codec.peek_purpose(legacy) == CONNECT
    with pytest.raises(StateInvalidError):
        codec.open(legacy, NOW, purpose=LOGIN)


@pytest.mark.parametrize(
    "payload",
    [
        {"p": "login", "v": "v" * 86, "n": "nn"},  # no hash, no exp
        {"p": "login", "h": "short", "v": "v" * 86, "n": "nn", "exp": 2**40},
        {"p": "login", "h": 7, "v": "v" * 86, "n": "nn", "exp": 2**40},
        {"p": "connect", "h": HASH, "v": "v" * 86, "n": "nn", "exp": 2**40},  # no user id
        {"p": "admin", "u": str(uuid.uuid4()), "v": "v" * 86, "n": "nn", "exp": 2**40},
        {"p": ["login"], "h": HASH, "v": "v" * 86, "n": "nn", "exp": 2**40},
        {"p": "login", "h": HASH, "v": "short", "n": "nn", "exp": 2**40},
    ],
)
def test_malformed_payloads_are_invalid(codec: StateCodec, key: str, payload: dict) -> None:
    with pytest.raises(StateInvalidError, match="malformed"):
        codec.open(_seal(key, payload), NOW)


def test_non_object_payload_is_invalid(codec: StateCodec, key: str) -> None:
    token = Fernet(key.encode()).encrypt_at_time(b"[1,2]", int(NOW.timestamp())).decode()
    with pytest.raises(StateInvalidError, match="malformed"):
        codec.open(token, NOW)
    assert codec.peek_purpose(token) is None


def test_expired_and_tampered_login_states(codec: StateCodec) -> None:
    token, issued = codec.issue_login(HASH, NOW)
    with pytest.raises(StateInvalidError, match="expired"):
        codec.open(token, datetime.fromtimestamp(issued.expires_at, UTC), purpose=LOGIN)
    tampered = token[:-6] + ("A" if token[-6] != "A" else "B") + token[-5:]
    with pytest.raises(StateInvalidError, match="undecryptable"):
        codec.open(tampered, NOW, purpose=LOGIN)
    other = StateCodec(Fernet.generate_key().decode(), timedelta(minutes=10))
    with pytest.raises(StateInvalidError, match="undecryptable"):
        other.open(token, NOW, purpose=LOGIN)
    with pytest.raises(StateInvalidError, match="missing"):
        codec.open(None, NOW, purpose=LOGIN)


def test_peek_purpose_ignores_expiry_but_not_authenticity(codec: StateCodec) -> None:
    long_ago = NOW - timedelta(days=1)
    expired_login, _ = codec.issue_login(HASH, long_ago)
    connect, _ = codec.issue(uuid.uuid4(), NOW)
    with pytest.raises(StateInvalidError, match="expired"):
        codec.open(expired_login, NOW)
    # Expired: still readable for routing the callback redirect (open() rejects it).
    assert codec.peek_purpose(expired_login) == LOGIN
    assert codec.peek_purpose(connect) == CONNECT
    login = expired_login
    for junk in (None, "", "not-a-token", login[:-4] + "AAAA", "é"):
        assert codec.peek_purpose(junk) is None

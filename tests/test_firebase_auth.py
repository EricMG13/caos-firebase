"""Firebase ID tokens, verified on the server (D6, design §4.3).

Keys are generated once per test session and served through an injected fetch:
nothing here reaches the network.
"""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Mapping
from types import MappingProxyType
from uuid import uuid5

import anyio
import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID

from caos.api import firebase_auth as fa
from caos.api.identity import Actor, GlobalRole, actor_from_headers
from caos.refusals import Refusal, RefusalCode

PROJECT = "caos-demo"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _cert_pem(
    key: rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey = KEY,
) -> str:
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode()


KEYS = fa.GoogleKeys(fetch=lambda: ({"k1": _cert_pem()}, 3600.0))


def _token(
    overrides: Mapping[str, object] = MappingProxyType({}), kid: str = "k1"
) -> str:
    now = int(time.time())
    claims: dict[str, object] = {
        "iss": f"https://securetoken.google.com/{PROJECT}",
        "aud": PROJECT,
        "sub": "uid-1",
        "iat": now,
        "exp": now + 3600,
        "auth_time": now,
        "email": "Analyst@Example.com",
        "email_verified": True,
    } | dict(overrides)
    return jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": kid})


def test_a_valid_token_verifies() -> None:
    assert fa.verify(_token(), PROJECT, KEYS, time.time())["sub"] == "uid-1"


@pytest.mark.parametrize(
    "bad",
    [
        {"aud": "other"},
        {"iss": "https://securetoken.google.com/other"},
        {"exp": int(time.time()) - 3600},
        {"email_verified": False},
        {"email_verified": "true"},
        {"sub": ""},
        {"sub": "x" * 129},
        {"auth_time": int(time.time()) + 3600},
        {"auth_time": None},
    ],
)
def test_a_token_that_breaks_a_rule_is_not_authenticated(
    bad: dict[str, object],
) -> None:
    with pytest.raises(Refusal) as refused:
        fa.verify(_token(bad), PROJECT, KEYS, time.time())
    assert refused.value.code is RefusalCode.NOT_AUTHENTICATED


def test_an_unknown_key_id_or_another_algorithm_is_not_authenticated() -> None:
    secret = "a-shared-secret-long-enough-for-hs256-keys"
    other = jwt.encode({"sub": "x"}, secret, algorithm="HS256", headers={"kid": "k1"})
    unknown = jwt.encode({"sub": "x"}, KEY, algorithm="RS256", headers={"kid": "nope"})
    for token in (other, unknown, "not.a.token"):
        with pytest.raises(Refusal) as refused:
            fa.verify(token, PROJECT, KEYS, time.time())
        assert refused.value.code is RefusalCode.NOT_AUTHENTICATED


def test_the_keys_are_cached_and_an_unknown_id_refetches_at_most_once_a_minute() -> (
    None
):
    calls: list[float] = []
    now = [1000.0]

    def fetch() -> tuple[dict[str, str], float]:
        calls.append(now[0])
        return {"k1": _cert_pem()}, 3600.0

    keys = fa.GoogleKeys(fetch=fetch, clock=lambda: now[0])
    keys.key("k1")
    keys.key("k1")
    assert len(calls) == 1
    for _ in range(3):
        with pytest.raises(Refusal) as refused:
            keys.key("nope")
        assert refused.value.code is RefusalCode.NOT_AUTHENTICATED
    assert len(calls) == 1
    now[0] += fa.REFETCH_FLOOR_SECONDS
    with pytest.raises(Refusal):
        keys.key("nope")
    assert len(calls) == 2
    now[0] += 3600.0
    keys.key("k1")
    assert len(calls) == 3


def test_keys_that_cannot_be_fetched_or_read_are_identity_unavailable() -> None:
    def broken() -> tuple[dict[str, str], float]:
        raise OSError

    curve = ec.generate_private_key(ec.SECP256R1())
    for fetch in (
        broken,
        lambda: ({"k1": "not a certificate"}, 3600.0),
        lambda: ({"k1": _cert_pem(curve)}, 3600.0),
    ):
        with pytest.raises(Refusal) as refused:
            fa.GoogleKeys(fetch=fetch).key("k1")
        assert refused.value.code is RefusalCode.IDENTITY_UNAVAILABLE


def test_the_role_map_reads_lower_cased_emails_and_refuses_a_malformed_one() -> None:
    assert fa.role_map("Analyst@Example.com=analyst; v@x.io=READER") == {
        "analyst@example.com": GlobalRole.ANALYST,
        "v@x.io": GlobalRole.READER,
    }
    for bad in (
        "",
        "noemail=READER",
        "a@b.c",
        "a@b.c=OWNER",
        "a@b.c=READER;a@b.c=ADMIN",
    ):
        with pytest.raises(Refusal) as refused:
            fa.role_map(bad)
        assert refused.value.code is RefusalCode.EDGE_CONFIG_INVALID


def test_the_token_comes_from_the_bearer_header_else_the_session_cookie() -> None:
    assert fa.bearer_token("Bearer abc", None) == "abc"
    assert fa.bearer_token(None, "x=1; __session=def") == "def"
    assert fa.bearer_token("Basic abc", "__session=def") is None
    assert fa.bearer_token("Bearer ", None) is None
    assert fa.bearer_token(None, "x=1") is None
    assert fa.bearer_token(None, None) is None


def _actor(authorization: object, cookie: object = None) -> object:
    async def run() -> object:
        return await fa.actor_from_firebase(authorization, cookie)

    return anyio.run(run)


def test_the_actor_is_minted_from_the_uid_and_the_role_from_the_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CAOS_FIREBASE_PROJECT", PROJECT)
    monkeypatch.setenv("CAOS_ROLE_MAP", "analyst@example.com=ANALYST")
    monkeypatch.setattr(fa, "_KEYS", KEYS)
    for actor in (_actor(f"Bearer {_token()}"), _actor(None, f"__session={_token()}")):
        assert isinstance(actor, Actor)
        assert actor.role is GlobalRole.ANALYST
        assert actor.user_id == uuid5(fa.NAMESPACE, "firebase:uid-1")


def test_an_email_outside_the_map_is_not_authorised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CAOS_FIREBASE_PROJECT", PROJECT)
    monkeypatch.setenv("CAOS_ROLE_MAP", "someone@else.com=ADMIN")
    monkeypatch.setattr(fa, "_KEYS", KEYS)
    with pytest.raises(Refusal) as refused:
        _actor(f"Bearer {_token()}")
    assert refused.value.code is RefusalCode.NOT_AUTHORISED


def test_no_token_or_no_project_is_not_authenticated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CAOS_ROLE_MAP", "analyst@example.com=ANALYST")
    monkeypatch.setattr(fa, "_KEYS", KEYS)
    monkeypatch.delenv("CAOS_FIREBASE_PROJECT", raising=False)
    with pytest.raises(Refusal) as refused:
        _actor(f"Bearer {_token()}")
    assert refused.value.code is RefusalCode.NOT_AUTHENTICATED
    monkeypatch.setenv("CAOS_FIREBASE_PROJECT", PROJECT)
    with pytest.raises(Refusal) as refused:
        _actor(None)
    assert refused.value.code is RefusalCode.NOT_AUTHENTICATED


def test_identity_reads_the_firebase_token_and_not_the_dev_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CAOS_FIREBASE_PROJECT", PROJECT)
    monkeypatch.setenv("CAOS_ROLE_MAP", "analyst@example.com=ANALYST")
    monkeypatch.setenv("CAOS_TRUST_ROLE_HEADER", "1")
    monkeypatch.setattr(fa, "_KEYS", KEYS)
    claimed = {"x-caos-user": str(uuid5(fa.NAMESPACE, "x")), "x-caos-role": "ADMIN"}

    async def run(headers: dict[str, str]) -> Actor:
        return await actor_from_headers(headers)

    actor = anyio.run(run, claimed | {"authorization": f"Bearer {_token()}"})
    assert actor.user_id == uuid5(fa.NAMESPACE, "firebase:uid-1")
    assert actor.role is GlobalRole.ANALYST
    with pytest.raises(Refusal) as refused:
        anyio.run(run, claimed)
    assert refused.value.code is RefusalCode.NOT_AUTHENTICATED

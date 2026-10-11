"""Firebase ID tokens, verified on the server (FD6, design §4.3).

Keys are generated once per test session and served through an injected fetch:
nothing here reaches the network.
"""

from __future__ import annotations

import datetime as dt
import http.client
import threading
import time
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import ClassVar
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
    overrides: Mapping[str, object] = MappingProxyType({}),
    kid: str = "k1",
    without: str | None = None,
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
    if without is not None:
        del claims[without]
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
        {"auth_time": True},
        {"iat": int(time.time()) + 3600},
        {"aud": [PROJECT, "other"]},
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


def test_warm_fetches_once_into_an_empty_cache_and_answers_whether_keys_are_held() -> (
    None
):
    calls: list[float] = []
    now = [1000.0]

    def fetch() -> tuple[dict[str, str], float]:
        calls.append(now[0])
        return {"k1": _cert_pem()}, 3600.0

    keys = fa.GoogleKeys(fetch=fetch, clock=lambda: now[0])
    assert keys.warm() is True
    assert keys.warm() is True
    keys.key("k1")
    assert len(calls) == 1


def test_warm_respects_the_refetch_floor_after_a_failure() -> None:
    calls: list[float] = []
    now = [1000.0]
    failing = [True]

    def fetch() -> tuple[dict[str, str], float]:
        calls.append(now[0])
        if failing[0]:
            raise OSError
        return {"k1": _cert_pem()}, 3600.0

    keys = fa.GoogleKeys(fetch=fetch, clock=lambda: now[0])
    assert keys.warm() is False
    failing[0] = False
    now[0] += fa.REFETCH_FLOOR_SECONDS - 1
    assert keys.warm() is False
    assert len(calls) == 1
    now[0] += 1
    assert keys.warm() is True
    assert len(calls) == 2
    # Past max-age with a failing refresh, held keys still serve their grace.
    failing[0] = True
    now[0] += 3600.0
    assert keys.warm() is True
    now[0] += fa.STALE_GRACE_SECONDS
    assert keys.warm() is False


def test_warm_never_waits_for_a_fetch_already_running() -> None:
    entered, release = threading.Event(), threading.Event()

    def slow() -> tuple[dict[str, str], float]:
        entered.set()
        release.wait(5)
        return {"k1": _cert_pem()}, 3600.0

    keys = fa.GoogleKeys(fetch=slow)
    first = threading.Thread(target=keys.warm)
    first.start()
    assert entered.wait(5)
    assert keys.warm() is False
    release.set()
    first.join(5)
    assert keys.warm() is True


def test_the_module_warm_uses_the_process_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = fa.GoogleKeys(fetch=lambda: ({"k1": _cert_pem()}, 3600.0))
    monkeypatch.setattr(fa, "_KEYS", held)
    assert fa.warm() is True


def test_the_role_map_reads_lower_cased_emails_and_refuses_a_malformed_one() -> None:
    assert fa.role_map("Analyst@Example.com=analyst; v@x.io=READER") == {
        "analyst@example.com": GlobalRole.ANALYST,
        "v@x.io": GlobalRole.READER,
    }
    assert fa.role_map(";a@b.c=READER;;") == {"a@b.c": GlobalRole.READER}
    for bad in (
        "",
        ";",
        " ; ;; ",
        "noemail=READER",
        "a@b.c",
        "a@b.c=OWNER",
        "a@b.c=READER;a@b.c=ADMIN",
        "a@b.c==READER",
    ):
        with pytest.raises(Refusal) as refused:
            fa.role_map(bad)
        assert refused.value.code is RefusalCode.EDGE_CONFIG_INVALID


def test_the_token_comes_from_the_bearer_header_else_the_session_cookie() -> None:
    assert fa.bearer_token("Bearer abc", None) == "abc"
    assert fa.bearer_token(None, "x=1; __session=def") == "def"
    assert fa.bearer_token("Basic abc", "__session=def") is None
    assert fa.bearer_token("Bearer ", None) is None
    assert fa.bearer_token("Bearer  abc", None) == "abc"
    assert fa.bearer_token(None, "x=1") is None
    assert fa.bearer_token(None, None) is None


def test_the_session_cookie_is_matched_by_exact_name_and_only_once() -> None:
    assert fa.bearer_token(None, '__session="abc"') == "abc"
    assert fa.bearer_token(None, " __session = abc ;y=2") == "abc"
    assert fa.bearer_token(None, "x__session=abc; __sessionx=abc") is None
    assert fa.bearer_token(None, "__session=a=b") == "a=b"
    assert fa.bearer_token(None, '__session=""') is None
    # Malformed or reserved neighbours do not hide it.
    for header in ('bad"cookie; __session=abc', "$Version=1; __session=abc; expires"):
        assert fa.bearer_token(None, header) == "abc"
    with pytest.raises(Refusal) as refused:
        fa.bearer_token(None, "__session=a; x=1; __session=b")
    assert refused.value.code is RefusalCode.NOT_AUTHENTICATED


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


def test_an_email_that_is_not_ascii_is_not_authorised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CAOS_FIREBASE_PROJECT", PROJECT)
    monkeypatch.setenv("CAOS_ROLE_MAP", "kim@b.c=ADMIN")
    monkeypatch.setattr(fa, "_KEYS", KEYS)
    kelvin = "\u212aim@b.c"  # KELVIN SIGN, which `str.lower` folds to "k"
    assert kelvin.lower() == "kim@b.c"
    with pytest.raises(Refusal) as refused:
        _actor(f"Bearer {_token({'email': kelvin})}")
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


def test_an_empty_firebase_project_is_still_firebase_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CAOS_FIREBASE_PROJECT", "")
    monkeypatch.setenv("CAOS_TRUST_ROLE_HEADER", "1")

    async def run() -> Actor:
        claimed = {"x-caos-user": str(uuid5(fa.NAMESPACE, "x")), "x-caos-role": "ADMIN"}
        return await actor_from_headers(claimed)

    with pytest.raises(Refusal) as refused:
        anyio.run(run)
    assert refused.value.code is RefusalCode.NOT_AUTHENTICATED


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _counted(
    answers: list[tuple[dict[str, str], float] | None],
) -> tuple[list[int], Callable[[], tuple[dict[str, str], float]]]:
    """A fetch answering `answers` in turn, None as an outage; and its call count."""
    calls = [0]

    def fetch() -> tuple[dict[str, str], float]:
        answer = answers[min(calls[0], len(answers) - 1)]
        calls[0] += 1
        if answer is None:
            raise OSError
        return answer

    return calls, fetch


def test_a_failing_fetch_is_tried_once_per_floor_under_forged_key_ids() -> None:
    clock = _Clock()
    calls, fetch = _counted([None])
    keys = fa.GoogleKeys(fetch=fetch, clock=clock)
    for forged in range(50):
        with pytest.raises(Refusal) as refused:
            keys.key(f"forged-{forged}")
        assert refused.value.code is RefusalCode.IDENTITY_UNAVAILABLE
    assert calls[0] == 1
    clock.now += fa.REFETCH_FLOOR_SECONDS - 1
    with pytest.raises(Refusal):
        keys.key("forged")
    assert calls[0] == 1
    clock.now += 1
    with pytest.raises(Refusal):
        keys.key("forged")
    assert calls[0] == 2


def test_a_cached_key_is_served_while_a_slow_fetch_is_in_flight() -> None:
    clock = _Clock()
    pem = _cert_pem()
    started, release = threading.Event(), threading.Event()
    calls = [0]

    def fetch() -> tuple[dict[str, str], float]:
        calls[0] += 1
        if calls[0] > 1:
            started.set()
            release.wait(10)
        return {"k1": pem}, 3600.0

    keys = fa.GoogleKeys(fetch=fetch, clock=clock)
    keys.key("k1")
    clock.now += 3600.0  # stale: the next caller refreshes
    slow = threading.Thread(target=lambda: keys.key("k1"))
    slow.start()
    assert started.wait(10)
    begun = time.monotonic()
    assert isinstance(keys.key("k1"), rsa.RSAPublicKey)
    with pytest.raises(Refusal) as refused:
        keys.key("forged")
    assert refused.value.code is RefusalCode.NOT_AUTHENTICATED
    assert time.monotonic() - begun < 1.0
    release.set()
    slow.join(10)
    assert calls[0] == 2


def test_stale_keys_serve_within_the_grace_and_are_refused_after_it() -> None:
    clock = _Clock()
    calls, fetch = _counted([({"k1": _cert_pem()}, 3600.0), None])
    keys = fa.GoogleKeys(fetch=fetch, clock=clock)
    keys.key("k1")
    clock.now += 3600.0 + fa.STALE_GRACE_SECONDS - 1
    assert isinstance(keys.key("k1"), rsa.RSAPublicKey)
    assert calls[0] == 2
    clock.now += fa.REFETCH_FLOOR_SECONDS
    with pytest.raises(Refusal) as refused:
        keys.key("k1")
    assert refused.value.code is RefusalCode.IDENTITY_UNAVAILABLE
    assert calls[0] == 3


@pytest.mark.parametrize(
    ("max_age", "kept"),
    [(0.0, fa.MIN_CACHE_SECONDS), (1e9, fa.MAX_CACHE_SECONDS), (300.0, 300.0)],
)
def test_the_cache_lifetime_is_held_between_a_minute_and_a_day(
    max_age: float, kept: float
) -> None:
    clock = _Clock()
    calls, fetch = _counted([({"k1": _cert_pem()}, max_age)])
    keys = fa.GoogleKeys(fetch=fetch, clock=clock)
    keys.key("k1")
    clock.now += kept - 1
    keys.key("k1")
    assert calls[0] == 1
    clock.now += 1
    keys.key("k1")
    assert calls[0] == 2


def test_the_fetch_reads_max_age_in_any_case_and_refuses_a_bad_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def answering(
        status: int, control: str
    ) -> Callable[[float], tuple[int, bytes, str] | None]:
        return lambda deadline: (status, b'{"k1": "pem"}', control)

    monkeypatch.setattr(fa, "_certificates", answering(200, "public, MAX-AGE=300"))
    assert fa._fetch() == ({"k1": "pem"}, 300.0)
    monkeypatch.setattr(fa, "_certificates", answering(200, "no-cache"))
    assert fa._fetch() == ({"k1": "pem"}, 0.0)
    for broken in (answering(500, ""), lambda deadline: None):
        monkeypatch.setattr(fa, "_certificates", broken)
        with pytest.raises(Refusal) as refused:
            fa._fetch()
        assert refused.value.code is RefusalCode.IDENTITY_UNAVAILABLE


def test_the_fetch_stops_waiting_at_its_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()

    def stalled(deadline: float) -> tuple[int, bytes, str] | None:
        release.wait(10)
        return None

    monkeypatch.setattr(fa, "_certificates", stalled)
    monkeypatch.setattr(fa, "CERTS_DEADLINE_SECONDS", 0.05)
    begun = time.monotonic()
    try:
        with pytest.raises(Refusal) as refused:
            fa._fetch()
        assert refused.value.code is RefusalCode.IDENTITY_UNAVAILABLE
        assert time.monotonic() - begun < 1.0
    finally:
        release.set()


def test_a_helper_thread_that_cannot_start_is_unavailable_and_frees_its_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failing = [True]
    start = threading.Thread.start

    def refuse(self: threading.Thread) -> None:
        if failing[0]:
            raise RuntimeError
        start(self)

    monkeypatch.setattr(threading.Thread, "start", refuse)
    monkeypatch.setattr(
        fa, "_certificates", lambda deadline: (200, b'{"k1": "pem"}', "")
    )
    for _ in range(fa.CERTS_FETCH_LIMIT + 1):
        with pytest.raises(Refusal) as refused:
            fa._fetch()
        assert refused.value.code is RefusalCode.IDENTITY_UNAVAILABLE
    failing[0] = False
    assert fa._fetch() == ({"k1": "pem"}, 0.0)


@pytest.mark.parametrize("missing", ["exp", "iat", "auth_time", "sub", "aud", "iss"])
def test_a_token_missing_a_required_claim_is_not_authenticated(missing: str) -> None:
    with pytest.raises(Refusal) as refused:
        fa.verify(_token(without=missing), PROJECT, KEYS, time.time())
    assert refused.value.code is RefusalCode.NOT_AUTHENTICATED


def test_the_rules_admit_their_own_boundaries() -> None:
    now = int(time.time())
    for overrides in (
        {"sub": "x"},
        {"sub": "x" * 128},
        {"exp": now - fa.LEEWAY_SECONDS // 2},
        {"auth_time": now + fa.LEEWAY_SECONDS},
    ):
        assert fa.verify(_token(overrides), PROJECT, KEYS, float(now))


def test_a_token_refused_by_its_header_never_asks_for_keys() -> None:
    calls, fetch = _counted([({"k1": _cert_pem()}, 3600.0)])
    keys = fa.GoogleKeys(fetch=fetch)
    secret = "a-shared-secret-long-enough-for-hs256-keys"
    other = jwt.encode({"sub": "x"}, secret, algorithm="HS256", headers={"kid": "k1"})
    keyless = jwt.encode({"sub": "x"}, KEY, algorithm="RS256")
    for token in (other, keyless):
        with pytest.raises(Refusal) as refused:
            fa.verify(token, PROJECT, keys, time.time())
        assert refused.value.code is RefusalCode.NOT_AUTHENTICATED
    assert calls[0] == 0


@pytest.mark.parametrize("project", [None, ""])
def test_without_a_project_no_token_is_verified(
    monkeypatch: pytest.MonkeyPatch, project: str | None
) -> None:
    calls, fetch = _counted([({"k1": _cert_pem()}, 3600.0)])
    monkeypatch.setattr(fa, "_KEYS", fa.GoogleKeys(fetch=fetch))
    monkeypatch.setenv("CAOS_ROLE_MAP", "analyst@example.com=ANALYST")
    if project is None:
        monkeypatch.delenv("CAOS_FIREBASE_PROJECT", raising=False)
    else:
        monkeypatch.setenv("CAOS_FIREBASE_PROJECT", project)
    with pytest.raises(Refusal) as refused:
        _actor(f"Bearer {_token()}")
    assert refused.value.code is RefusalCode.NOT_AUTHENTICATED
    assert calls[0] == 0


def test_a_missing_role_map_is_a_configuration_fault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CAOS_FIREBASE_PROJECT", PROJECT)
    monkeypatch.delenv("CAOS_ROLE_MAP", raising=False)
    monkeypatch.setattr(fa, "_KEYS", KEYS)
    with pytest.raises(Refusal) as refused:
        _actor(f"Bearer {_token()}")
    assert refused.value.code is RefusalCode.EDGE_CONFIG_INVALID


def test_a_certificate_that_does_not_parse_or_is_not_rsa_is_identity_unavailable() -> (
    None
):
    curve = ec.generate_private_key(ec.SECP256R1())
    for pems in ({"k1": "not a certificate"}, {"k1": _cert_pem(curve)}):
        with pytest.raises(Refusal) as refused:
            fa._public_keys(pems)
        assert refused.value.code is RefusalCode.IDENTITY_UNAVAILABLE


@pytest.mark.parametrize("first", ["key", "warm"])
def test_no_second_fetch_starts_while_one_is_in_flight_even_past_the_floor(
    first: str,
) -> None:
    clock = _Clock()
    pem = _cert_pem()
    started, release = threading.Event(), threading.Event()
    calls = [0]

    def fetch() -> tuple[dict[str, str], float]:
        calls[0] += 1
        if calls[0] == 1:
            started.set()
            release.wait(10)
        return {"k1": pem}, 3600.0

    keys = fa.GoogleKeys(fetch=fetch, clock=clock)
    starter = threading.Thread(
        target=getattr(keys, first), args=(("k1",) if first == "key" else ())
    )
    starter.start()
    try:
        assert started.wait(10)
        clock.now += fa.REFETCH_FLOOR_SECONDS + 1
        with pytest.raises(Refusal) as refused:
            keys.key("k1")
        assert refused.value.code is RefusalCode.IDENTITY_UNAVAILABLE
        assert keys.warm() is False
        assert calls[0] == 1
    finally:
        release.set()
        starter.join(10)


def test_warm_refetches_once_the_max_age_is_reached() -> None:
    clock = _Clock()
    calls, fetch = _counted([({"k1": _cert_pem()}, 300.0)])
    keys = fa.GoogleKeys(fetch=fetch, clock=clock)
    assert keys.warm() is True
    clock.now += 300.0
    assert keys.warm() is True
    assert calls[0] == 2


def test_warm_answers_from_held_stale_keys_inside_the_floor() -> None:
    clock = _Clock()
    calls, fetch = _counted([({"k1": _cert_pem()}, 60.0), None])
    keys = fa.GoogleKeys(fetch=fetch, clock=clock)
    assert keys.warm() is True
    clock.now += 100.0
    assert keys.warm() is True
    assert calls[0] == 2
    clock.now += 10.0
    assert keys.warm() is True
    assert calls[0] == 2


def test_a_fetch_that_raises_unexpectedly_leaves_the_held_keys_serving() -> None:
    clock = _Clock()
    pem = _cert_pem()
    calls = [0]

    def fetch() -> tuple[dict[str, str], float]:
        calls[0] += 1
        if calls[0] > 1:
            raise RuntimeError
        return {"k1": pem}, 3600.0

    keys = fa.GoogleKeys(fetch=fetch, clock=clock)
    keys.key("k1")
    clock.now += 3600.0
    with pytest.raises(RuntimeError):
        keys.key("k1")
    clock.now += 1.0
    assert isinstance(keys.key("k1"), rsa.RSAPublicKey)
    assert calls[0] == 2


def test_a_fetch_finding_no_free_slot_is_refused_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fa, "_certificates", lambda deadline: None)
    held = 0
    outcome: list[RefusalCode] = []

    def call() -> None:
        try:
            fa._fetch()
        except Refusal as refused:
            outcome.append(refused.code)

    try:
        while fa._FETCHES.acquire(blocking=False):
            held += 1
        caller = threading.Thread(target=call, daemon=True)
        caller.start()
        caller.join(2)
        assert outcome == [RefusalCode.IDENTITY_UNAVAILABLE]
    finally:
        for _ in range(held):
            fa._FETCHES.release()


def test_the_fetch_runs_on_a_named_daemon_thread_with_its_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[float, str, bool]] = []

    def answer(deadline: float) -> tuple[int, bytes, str]:
        thread = threading.current_thread()
        seen.append((deadline, thread.name, thread.daemon))
        return 200, b'{"k1": "pem"}', ""

    monkeypatch.setattr(fa, "_certificates", answer)
    before = time.monotonic()
    fa._fetch()
    after = time.monotonic()
    [(deadline, name, daemon)] = seen
    assert before + fa.CERTS_DEADLINE_SECONDS <= deadline
    assert deadline <= after + fa.CERTS_DEADLINE_SECONDS
    assert (name, daemon) == ("caos-google-keys", True)


def _body(size: int) -> bytes:
    """A JSON object of exactly `size` bytes holding one certificate entry."""
    head, tail = b'{"k1": "', b'"}'
    return head + b"x" * (size - len(head) - len(tail)) + tail


def test_a_body_of_exactly_the_limit_is_read_and_one_byte_more_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    whole = _body(fa.CERTS_BODY_BYTES)
    monkeypatch.setattr(fa, "_certificates", lambda deadline: (200, whole, ""))
    pems, _ = fa._fetch()
    assert len(pems["k1"]) == fa.CERTS_BODY_BYTES - len(b'{"k1": ""}')
    over = _body(fa.CERTS_BODY_BYTES + 1)
    monkeypatch.setattr(fa, "_certificates", lambda deadline: (200, over, ""))
    with pytest.raises(Refusal) as refused:
        fa._fetch()
    assert refused.value.code is RefusalCode.IDENTITY_UNAVAILABLE


class _Response:
    """The parts of `http.client.HTTPResponse` `_certificates` reads; header
    names match in any case, as `HTTPMessage`'s do."""

    def __init__(self, status: int, body: bytes, headers: Mapping[str, str]) -> None:
        self.status = status
        self._body = body
        self._headers = {name.lower(): value for name, value in headers.items()}

    def read1(self, most: int) -> bytes:
        block, self._body = self._body[:most], self._body[most:]
        return block

    def getheader(self, name: str, default: str | None = None) -> str | None:
        return self._headers.get(name.lower(), default)


class _Connection:
    """Stands in for `http.client.HTTPSConnection`, recording what was asked."""

    made: ClassVar[list[_Connection]] = []
    answer: ClassVar[_Response | None] = None
    fault: ClassVar[Exception | None] = None

    def __init__(self, host: str, timeout: float | None = None) -> None:
        self.host, self.timeout = host, timeout
        self.sent: tuple[str, str] | None = None
        self.closed = False
        _Connection.made.append(self)

    def request(self, method: str, url: str) -> None:
        self.sent = (method, url)
        if _Connection.fault is not None:
            raise _Connection.fault

    def getresponse(self) -> _Response | None:
        return _Connection.answer

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def connection(monkeypatch: pytest.MonkeyPatch) -> type[_Connection]:
    monkeypatch.setattr(_Connection, "made", [])
    monkeypatch.setattr(_Connection, "answer", None)
    monkeypatch.setattr(_Connection, "fault", None)
    monkeypatch.setattr(http.client, "HTTPSConnection", _Connection)
    return _Connection


def test_the_certificates_are_one_get_to_google_read_up_to_the_limit(
    connection: type[_Connection],
) -> None:
    body = b'{"k1": "pem"}'
    connection.answer = _Response(200, body, {"Cache-Control": "max-age=300"})
    assert fa._certificates(time.monotonic() + 5) == (200, body, "max-age=300")
    [made] = connection.made
    assert (made.host, made.timeout) == (fa.CERTS_HOST, fa.CERTS_TIMEOUT_SECONDS)
    assert made.sent == ("GET", fa.CERTS_PATH)
    assert made.closed
    connection.answer = _Response(200, b"x" * (fa.CERTS_BODY_BYTES + 10), {})
    answer = fa._certificates(time.monotonic() + 5)
    assert answer is not None
    assert (len(answer[1]), answer[2]) == (fa.CERTS_BODY_BYTES + 1, "")


def test_a_transport_fault_or_a_late_body_is_no_answer(
    connection: type[_Connection],
) -> None:
    connection.answer = _Response(200, b"{}", {})
    assert fa._certificates(time.monotonic() - 1) is None
    connection.fault = OSError()
    assert fa._certificates(time.monotonic() + 5) is None
    assert all(made.closed for made in connection.made)

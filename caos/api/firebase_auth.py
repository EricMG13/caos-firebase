"""Firebase ID tokens, verified here and nowhere else (D6, design §4.3).

The browser signs in with Firebase Auth and sends its ID token as a bearer
header, or as the `__session` cookie where it cannot set a header (the run
stream's `EventSource`). The token is Google-signed (RS256); this module checks
the signature against Google's published certificates and the claims Firebase
documents, then maps the verified email to a role from the server's own map.
Nothing the client says about itself decides who it is.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import threading
import time
from collections.abc import Callable, Mapping
from uuid import NAMESPACE_URL, uuid5

import anyio.to_thread
import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from cryptography.x509 import load_pem_x509_certificate

from caos.api.identity import FIREBASE_ENV, Actor, GlobalRole, _body_by
from caos.refusals import Refusal, RefusalCode

# No store round trips. One bounded HTTPS fetch of Google's certificates per
# cache lifetime, which this number does not count (as identity.py states for SCIM).
IO_BUDGET = 0

ROLE_MAP_ENV = "CAOS_ROLE_MAP"
SESSION_COOKIE = "__session"
CERTS_HOST = "www.googleapis.com"
CERTS_PATH = "/robot/v1/metadata/x509/securetoken@system.gserviceaccount.com"
CERTS_TIMEOUT_SECONDS = 10.0
# The whole fetch, name resolution included: the round trip runs on a helper
# thread and the asking thread stops waiting here (identity's `scim_me`, ED-8).
CERTS_DEADLINE_SECONDS = 10.0
CERTS_BODY_BYTES = 65_536
# Helper threads fetching at once, an abandoned one included: one stalled past
# its deadline holds its place, and a fetch finding none free is refused.
CERTS_FETCH_LIMIT = 2
_FETCHES = threading.BoundedSemaphore(CERTS_FETCH_LIMIT)
# Any fetch, failed or not, waits this long for the next (F1): a stream of
# forged key ids cannot turn every request into a call to Google, outage or not.
REFETCH_FLOOR_SECONDS = 60.0
# The response's max-age, held between these bounds (F5).
MIN_CACHE_SECONDS = 60.0
MAX_CACHE_SECONDS = 86_400.0
# Keys a failed refresh could not replace keep serving this long past their
# max-age, then fail closed (F1).
STALE_GRACE_SECONDS = 86_400.0
LEEWAY_SECONDS = 60
NAMESPACE = uuid5(NAMESPACE_URL, "caos.firebase.identity")
_ISSUER = "https://securetoken.google.com/"
_MAX_AGE = re.compile(r"max-age=(\d+)", re.IGNORECASE)


def role_map(value: str) -> dict[str, GlobalRole]:
    """`email=ROLE;email=ROLE`, emails lower-cased, empty entries skipped (D14);
    `EDGE_CONFIG_INVALID` for an empty map, an entry without `=` or `@`, an
    unknown role, or an email twice."""
    roles: dict[str, GlobalRole] = {}
    for entry in value.split(";"):
        if not entry.strip():
            continue
        email, sep, role = entry.strip().partition("=")
        email = email.strip().lower()
        if not sep or "@" not in email or email in roles:
            raise Refusal(RefusalCode.EDGE_CONFIG_INVALID)
        try:
            roles[email] = GlobalRole(role.strip().upper())
        except ValueError:
            raise Refusal(RefusalCode.EDGE_CONFIG_INVALID) from None
    if not roles:
        raise Refusal(RefusalCode.EDGE_CONFIG_INVALID)
    return roles


def bearer_token(authorization: object, cookie: object) -> str | None:
    """The ID token: the `Bearer` header when present, else the session cookie."""
    if isinstance(authorization, str):
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer":
            return None
        return token.strip() or None
    if not isinstance(cookie, str):
        return None
    return _session_cookie(cookie)


def _session_cookie(cookie: str) -> str | None:
    """`__session`'s value, by exact name among the `;`-separated pairs, so no
    malformed or reserved neighbour can hide it (F3); `NOT_AUTHENTICATED` when
    it appears twice, since two of them name nobody (F44)."""
    found = [
        value.strip()
        for name, sep, value in (pair.partition("=") for pair in cookie.split(";"))
        if sep and name.strip() == SESSION_COOKIE
    ]
    if len(found) > 1:
        raise Refusal(RefusalCode.NOT_AUTHENTICATED)
    value = found[0] if found else ""
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
    return value or None


def _certificates(deadline: float) -> tuple[int, bytes, str] | None:
    """One round trip to Google: the status, the body and its cache-control, or
    None for any transport fault or a body still arriving at `deadline`."""
    connection = http.client.HTTPSConnection(CERTS_HOST, timeout=CERTS_TIMEOUT_SECONDS)
    try:
        connection.request("GET", CERTS_PATH)
        response = connection.getresponse()
        body = _body_by(response, deadline, CERTS_BODY_BYTES)
        return response.status, body, response.getheader("cache-control", "")
    except (OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        connection.close()


def _fetch() -> tuple[dict[str, str], float]:
    """Google's certificates by key id, and their max-age; `IDENTITY_UNAVAILABLE`
    unless they arrive whole within `CERTS_DEADLINE_SECONDS`."""
    if not _FETCHES.acquire(blocking=False):
        raise Refusal(RefusalCode.IDENTITY_UNAVAILABLE)
    answers: list[tuple[int, bytes, str]] = []
    done = threading.Event()

    def run() -> None:
        try:
            answer = _certificates(time.monotonic() + CERTS_DEADLINE_SECONDS)
            if answer is not None:
                answers.append(answer)
        finally:
            _FETCHES.release()
            done.set()

    threading.Thread(target=run, name="caos-google-keys", daemon=True).start()
    done.wait(CERTS_DEADLINE_SECONDS)
    if not answers:
        raise Refusal(RefusalCode.IDENTITY_UNAVAILABLE)
    status, body, control = answers[0]
    if status != 200 or len(body) > CERTS_BODY_BYTES:
        raise Refusal(RefusalCode.IDENTITY_UNAVAILABLE)
    found = _MAX_AGE.search(control)
    return json.loads(body), float(found.group(1)) if found else 0.0


def _public_keys(pems: Mapping[str, str]) -> dict[str, RSAPublicKey]:
    """Each certificate's RSA public key, or `IDENTITY_UNAVAILABLE` for any
    certificate that does not parse or does not hold one."""
    keys: dict[str, RSAPublicKey] = {}
    try:
        for name, pem in pems.items():
            key = load_pem_x509_certificate(pem.encode()).public_key()
            if not isinstance(key, RSAPublicKey):
                raise Refusal(RefusalCode.IDENTITY_UNAVAILABLE)
            keys[name] = key
    except (ValueError, TypeError, AttributeError):
        raise Refusal(RefusalCode.IDENTITY_UNAVAILABLE) from None
    return keys


class GoogleKeys:
    """Google's signing keys (F1). A fresh cached key is answered at once; at
    most one fetch runs at a time, with no lock held while it does, and any
    other caller is answered from the cache or refused at once; every fetch,
    failed or not, arms `REFETCH_FLOOR_SECONDS`; and keys a failed refresh
    could not replace serve `STALE_GRACE_SECONDS` past their max-age, then
    fail closed."""

    def __init__(
        self,
        fetch: Callable[[], tuple[Mapping[str, str], float]] = _fetch,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch, self._clock = fetch, clock
        self._keys: dict[str, RSAPublicKey] = {}
        self._until = float("-inf")
        self._attempted = float("-inf")
        self._fetching = False
        self._lock = threading.Lock()

    def key(self, kid: str) -> RSAPublicKey:
        with self._lock:
            now = self._clock()
            if kid in self._keys and now < self._until:
                return self._keys[kid]
            if self._fetching or now - self._attempted < REFETCH_FLOOR_SECONDS:
                return self._cached(kid, now)
            self._fetching, self._attempted = True, now
        self._refresh()
        with self._lock:
            return self._cached(kid, self._clock())

    def warm(self) -> bool:
        """Fetch when no fresh keys are held, under the same single flight and
        floor as `key`; whether usable keys are held afterwards. Never waits for
        another caller's fetch."""
        with self._lock:
            now = self._clock()
            if self._keys and now < self._until:
                return True
            if self._fetching or now - self._attempted < REFETCH_FLOOR_SECONDS:
                return self._usable(now)
            self._fetching, self._attempted = True, now
        self._refresh()
        with self._lock:
            return self._usable(self._clock())

    def _refresh(self) -> None:
        """Run the fetch the caller claimed; store the keys it brought, if any."""
        keys: dict[str, RSAPublicKey] | None = None
        max_age = 0.0
        try:
            keys, max_age = self._fetched()
        finally:
            with self._lock:
                self._fetching = False
                if keys is not None:
                    self._keys = keys
                    self._until = self._clock() + min(
                        max(max_age, MIN_CACHE_SECONDS), MAX_CACHE_SECONDS
                    )

    def _usable(self, now: float) -> bool:
        """Keys are held and within their grace (`_cached`'s own test)."""
        return bool(self._keys) and now < self._until + STALE_GRACE_SECONDS

    def _fetched(self) -> tuple[dict[str, RSAPublicKey] | None, float]:
        """The fetched keys and their max-age, or None for any failure."""
        try:
            pems, max_age = self._fetch()
            return _public_keys(pems), max_age
        except (
            Refusal,
            OSError,
            ValueError,
            TypeError,
            AttributeError,
            http.client.HTTPException,
        ):
            return None, 0.0

    def _cached(self, kid: str, now: float) -> RSAPublicKey:
        """`kid` from the cache while it is within its grace; `NOT_AUTHENTICATED`
        for a kid those keys lack; `IDENTITY_UNAVAILABLE` when there are none."""
        if not self._usable(now):
            raise Refusal(RefusalCode.IDENTITY_UNAVAILABLE)
        if kid not in self._keys:
            raise Refusal(RefusalCode.NOT_AUTHENTICATED)
        return self._keys[kid]


_KEYS = GoogleKeys()


def warm() -> bool:
    """Fill the process's key cache off the request path (D12); whether usable
    keys are held. The identity health probe calls this every round."""
    return _KEYS.warm()


def verify(
    token: str, project: str, keys: GoogleKeys, now: float
) -> Mapping[str, object]:
    """The token's claims if every Firebase rule holds, else `NOT_AUTHENTICATED`."""
    try:
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        if header.get("alg") != "RS256" or not isinstance(kid, str):
            raise Refusal(RefusalCode.NOT_AUTHENTICATED)
        claims: dict[str, object] = jwt.decode(
            token,
            keys.key(kid),
            algorithms=["RS256"],
            audience=project,
            issuer=_ISSUER + project,
            leeway=LEEWAY_SECONDS,
            options={
                "require": ["exp", "iat", "auth_time", "sub", "aud", "iss"],
                "strict_aud": True,
            },
        )
    except jwt.PyJWTError:
        raise Refusal(RefusalCode.NOT_AUTHENTICATED) from None
    subject, auth_time = claims.get("sub"), claims.get("auth_time")
    if not isinstance(subject, str) or not 1 <= len(subject) <= 128:
        raise Refusal(RefusalCode.NOT_AUTHENTICATED)
    if type(auth_time) is not int or auth_time > now + LEEWAY_SECONDS:
        raise Refusal(RefusalCode.NOT_AUTHENTICATED)
    if claims.get("email_verified") is not True:
        raise Refusal(RefusalCode.NOT_AUTHENTICATED)
    return claims


async def actor_from_firebase(authorization: object, cookie: object) -> Actor:
    """The actor a verified token names, `NOT_AUTHENTICATED` without one,
    `NOT_AUTHORISED` for an email the role map does not hold."""
    project = os.environ.get(FIREBASE_ENV, "")
    token = bearer_token(authorization, cookie)
    if not project or token is None:
        raise Refusal(RefusalCode.NOT_AUTHENTICATED)
    claims = await anyio.to_thread.run_sync(verify, token, project, _KEYS, time.time())
    email = claims.get("email")
    roles = role_map(os.environ.get(ROLE_MAP_ENV, ""))
    # ASCII only (F2): `str.lower` folds a non-ASCII letter such as U+212A
    # KELVIN SIGN onto an ASCII one, which would match another person's entry.
    if not isinstance(email, str) or not email.isascii():
        raise Refusal(RefusalCode.NOT_AUTHORISED)
    role = roles.get(email.lower())
    if role is None:
        raise Refusal(RefusalCode.NOT_AUTHORISED)
    return Actor(user_id=uuid5(NAMESPACE, f"firebase:{claims['sub']}"), role=role)

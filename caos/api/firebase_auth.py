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
from http.cookies import CookieError, SimpleCookie
from uuid import NAMESPACE_URL, uuid5

import anyio.to_thread
import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from cryptography.x509 import load_pem_x509_certificate

from caos.api.identity import FIREBASE_ENV, Actor, GlobalRole
from caos.refusals import Refusal, RefusalCode

# No store round trips. One bounded HTTPS fetch of Google's certificates per
# cache lifetime, which this number does not count (as identity.py states for SCIM).
IO_BUDGET = 0

ROLE_MAP_ENV = "CAOS_ROLE_MAP"
SESSION_COOKIE = "__session"
CERTS_HOST = "www.googleapis.com"
CERTS_PATH = "/robot/v1/metadata/x509/securetoken@system.gserviceaccount.com"
CERTS_TIMEOUT_SECONDS = 10.0
CERTS_BODY_BYTES = 65_536
# An unknown key id refetches at most this often, so a stream of forged ids
# cannot turn every request into a call to Google.
REFETCH_FLOOR_SECONDS = 60.0
LEEWAY_SECONDS = 60
NAMESPACE = uuid5(NAMESPACE_URL, "caos.firebase.identity")
_ISSUER = "https://securetoken.google.com/"
_MAX_AGE = re.compile(r"max-age=(\d+)")


def role_map(value: str) -> dict[str, GlobalRole]:
    """`email=ROLE;email=ROLE`, emails lower-cased; `EDGE_CONFIG_INVALID` for an
    empty map, an entry without `=` or `@`, an unknown role, or an email twice."""
    roles: dict[str, GlobalRole] = {}
    for entry in value.split(";"):
        email, sep, role = entry.strip().partition("=")
        email = email.strip().lower()
        if not sep or "@" not in email or email in roles:
            raise Refusal(RefusalCode.EDGE_CONFIG_INVALID)
        try:
            roles[email] = GlobalRole(role.strip().upper())
        except ValueError:
            raise Refusal(RefusalCode.EDGE_CONFIG_INVALID) from None
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
    try:
        morsel = SimpleCookie(cookie).get(SESSION_COOKIE)
    except CookieError:
        return None
    if morsel is None:
        return None
    return morsel.value or None


def _fetch() -> tuple[dict[str, str], float]:
    """Google's certificates by key id, and how long they may be kept."""
    connection = http.client.HTTPSConnection(CERTS_HOST, timeout=CERTS_TIMEOUT_SECONDS)
    try:
        connection.request("GET", CERTS_PATH)
        response = connection.getresponse()
        body = response.read(CERTS_BODY_BYTES + 1)
        control = response.getheader("cache-control", "")
        status = response.status
    finally:
        connection.close()
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
    """The signing keys, cached for the response's max-age; thread-safe."""

    def __init__(
        self,
        fetch: Callable[[], tuple[Mapping[str, str], float]] = _fetch,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch, self._clock = fetch, clock
        self._keys: dict[str, RSAPublicKey] = {}
        self._until = 0.0
        self._fetched = float("-inf")
        self._lock = threading.Lock()

    def key(self, kid: str) -> RSAPublicKey:
        with self._lock:
            now = self._clock()
            stale = now >= self._until
            unknown = (
                kid not in self._keys and now - self._fetched >= REFETCH_FLOOR_SECONDS
            )
            if stale or unknown:
                try:
                    pems, max_age = self._fetch()
                except (OSError, ValueError, TypeError, http.client.HTTPException):
                    raise Refusal(RefusalCode.IDENTITY_UNAVAILABLE) from None
                self._keys = _public_keys(pems)
                self._fetched, self._until = now, now + max_age
            if kid not in self._keys:
                raise Refusal(RefusalCode.NOT_AUTHENTICATED)
            return self._keys[kid]


_KEYS = GoogleKeys()


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
            options={"require": ["exp", "iat", "auth_time", "sub", "aud", "iss"]},
        )
    except jwt.PyJWTError:
        raise Refusal(RefusalCode.NOT_AUTHENTICATED) from None
    subject, auth_time = claims.get("sub"), claims.get("auth_time")
    if not isinstance(subject, str) or not 1 <= len(subject) <= 128:
        raise Refusal(RefusalCode.NOT_AUTHENTICATED)
    if not isinstance(auth_time, int) or auth_time > now + LEEWAY_SECONDS:
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
    role = roles.get(email.lower()) if isinstance(email, str) else None
    if role is None:
        raise Refusal(RefusalCode.NOT_AUTHORISED)
    return Actor(user_id=uuid5(NAMESPACE, f"firebase:{claims['sub']}"), role=role)

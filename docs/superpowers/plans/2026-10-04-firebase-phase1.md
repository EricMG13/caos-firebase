# CAOS on Firebase — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** CAOS online on Cloud Run for the 2026-10-05 demo: Firebase email/password sign-in, Cloud SQL store, Cloud Storage documents, OpenRouter model calls.

**Architecture:** One Cloud Run service runs `python -m caos.serve` (API, built frontend and in-process worker) on one always-on instance. A new `firebase` edge mode verifies Firebase ID tokens on the server and maps emails to roles. `caos.models.chat_model` answers through OpenRouter. The Databricks code stays in the tree, unconfigured (Phase 2 deletes it).

**Tech Stack:** Python 3.13, uv, FastAPI/Starlette, psycopg, LangGraph, PyJWT + cryptography (already locked), langchain-openai (already locked), React 19 + Vite, `firebase` JS SDK (new, FD7), Docker, Cloud Build, Cloud Run, Cloud SQL Postgres 17, Cloud Storage FUSE, Secret Manager.

**Spec:** `docs/specs/2026-10-04-firebase-design.md`

## Global Constraints

- Every gate in `CLAUDE.md` exits 0; none is loosened; suppression counts (`tests/gate_baseline.json`) may only fall — no new `noqa`, `type: ignore`, `nosec`, `pragma`, skip.
- Every public definition is named by a test (`scripts/check_tested.py`); every module under `caos/api/` declares `IO_BUDGET`.
- Typed refusals only; never `str(exc)` on the wire or in a log; never log a token, an email or document text.
- New dependency only with a `Dn` in `docs/decisions.md` (FD7 covers `firebase`; FD8 in Task 1 covers promoting `langchain-openai`).
- Each task's diff ≤ 800 changed lines (`scripts/check_pr_size.py`).
- Tests run with `CAOS_TEST_POSTGRES_URL=postgresql://postgres:local-test-admin-only@127.0.0.1:55437/postgres CAOS_REQUIRE_POSTGRES=1` after `docker compose up -d --wait`.
- Ledger: `.superpowers/sdd/2026-10-04-firebase-phase1/progress.md`, one line per task: `agent=<name> fix-rounds=<n> gates-first-pass=<y|n>`.
- Branch: `phase1` off `main`; one commit per task; the owner merges.

---

### Task 0: Fork import (owner)

The import of `caos-databricks` at `claude/live-fixes` `861b218` is copied into the working tree but not committed: the auto-mode classifier refused the commit as possible data exfiltration (enterprise code into a public repository). The owner commits it, or decides otherwise.

- [ ] **Step 1 (owner):** confirm the tree, then commit:

```bash
cd /Users/ericguei/Documents/caos-firebase && git add -A && git commit -m "Fork caos-databricks at claude/live-fixes 861b218"
```

- [ ] **Step 2 (controller, after Step 1):** in `.claude/settings.json`, remove every `databricks` permission and add `"Bash(gcloud * list *)"`, `"Bash(gcloud * describe *)"`, `"Bash(docker build *)"`, `"Bash(docker run *)"` to `allow` and `"Bash(gcloud * delete *)"`, `"Bash(firebase * delete *)"` to `deny`. Commit.
- [ ] **Step 3 (controller):** baseline: `uv sync --locked --all-groups && docker compose up -d --wait`, then the pytest gate. Record pass/fail in the ledger before Task 1 starts.

---

### Task 1: OpenRouter as the production model provider

**Agent:** `mx-precise` (money-adjacent: invariant 8). Review: `mx-precise`.

**Files:**
- Modify: `caos/models.py` (`PLATFORM`, `chat_model`, `from_environment`)
- Modify: `caos/pricing.py:199-215` (`model_choices`' effort refusal → validation)
- Modify: `pyproject.toml` (move `langchain-openai>=1.0` from the dev group to `[project].dependencies`), `uv.lock` (`uv lock`)
- Modify: `tests/test_models.py` (the three tests that patch `databricks_langchain.ChatDatabricks`)
- Modify: `docs/decisions.md` (FD8)

**Interfaces:**
- Produces: `caos.models.chat_model(*, endpoint: str | None = None) -> BaseChatModel` returning `langchain_openai.ChatOpenAI`; `caos.models.PLATFORM == "openrouter"`; `caos.pricing.EFFORTS: tuple[str, ...]`; `caos.pricing.reasoning_effort() -> str | None`; env names `OPENROUTER_API_KEY`, `CAOS_REASONING_EFFORT`, `CAOS_PROVIDER_ORDER`.

- [ ] **Step 1: Write the failing tests** in `tests/test_models.py`, replacing the `_ChatDatabricks` patching tests:

```python
def test_the_chat_model_is_openrouter_with_the_deadline_and_no_retry(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("CAOS_MODEL_ENDPOINT", "openai/gpt-6-luna")
    from langchain_openai import ChatOpenAI

    from caos.models import chat_model
    from caos.provider import MAX_COMPLETION_TOKENS, TIMEOUT_SECONDS

    chat = chat_model()
    assert isinstance(chat, ChatOpenAI)
    assert chat.openai_api_base == "https://openrouter.ai/api/v1"
    assert chat.model_name == "openai/gpt-6-luna"
    assert chat.max_retries == 0
    assert chat.request_timeout == TIMEOUT_SECONDS
    assert chat.max_tokens == MAX_COMPLETION_TOKENS


def test_the_chat_model_sends_the_effort_and_the_provider_pin(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("CAOS_REASONING_EFFORT", "high")
    monkeypatch.setenv("CAOS_PROVIDER_ORDER", "openai")
    from caos.models import chat_model

    assert chat_model(endpoint="openai/gpt-6-luna").extra_body == {
        "reasoning": {"effort": "high"},
        "provider": {"order": ["openai"], "allow_fallbacks": False},
    }


def test_no_key_is_provider_not_configured(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    from caos.models import chat_model
    from caos.refusals import Refusal, RefusalCode

    with pytest.raises(Refusal) as refused:
        chat_model(endpoint="openai/gpt-6-luna")
    assert refused.value.code is RefusalCode.PROVIDER_NOT_CONFIGURED


def test_an_effort_outside_the_set_is_provider_not_configured(monkeypatch):
    monkeypatch.setenv("CAOS_REASONING_EFFORT", "High")
    from caos.pricing import reasoning_effort
    from caos.refusals import Refusal, RefusalCode

    with pytest.raises(Refusal) as refused:
        reasoning_effort()
    assert refused.value.code is RefusalCode.PROVIDER_NOT_CONFIGURED


def test_the_identity_names_openrouter_and_the_effort_sent(monkeypatch):
    monkeypatch.setenv("CAOS_REASONING_EFFORT", "high")
    from caos.models import identity_of

    assert identity_of("openai/gpt-6-luna", "high").startswith("openrouter/openai/gpt-6-luna/high/")
```

Also delete or rewrite any test asserting that `CAOS_REASONING_EFFORT` makes `model_choices()` refuse (search: `grep -n REASONING_EFFORT tests/`).

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_models.py -q -k "openrouter or effort or identity_names or no_key"`
Expected: FAIL (`chat_model` returns `ChatDatabricks`; `reasoning_effort` undefined).

- [ ] **Step 3: Implement.** In `caos/pricing.py` replace the refusal at lines 207-211 with a call to a new function and add it:

```python
EFFORTS = ("minimal", "low", "medium", "high", "xhigh")


def reasoning_effort() -> str | None:
    """The reasoning effort sent with every call (FD8), or None when unset; a
    value outside `EFFORTS` is `PROVIDER_NOT_CONFIGURED`, compared exactly."""
    effort = os.environ.get(REASONING_EFFORT_ENV, "").strip()
    if not effort:
        return None
    if effort not in EFFORTS:
        raise Refusal(RefusalCode.PROVIDER_NOT_CONFIGURED)
    return effort
```

and in `model_choices` the old `if os.environ.get(REASONING_EFFORT_ENV): raise ...` becomes `reasoning_effort()` (validates, result unused there).

In `caos/models.py`: `PLATFORM = "openrouter"`, and replace `chat_model`'s body:

```python
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_KEY_ENV = "OPENROUTER_API_KEY"
PROVIDER_ORDER_ENV = "CAOS_PROVIDER_ORDER"


def chat_model(*, endpoint: str | None = None) -> BaseChatModel:
    """The production chat model: OpenRouter's OpenAI-compatible API (FD5, FD8).

    The key is read at the call and never stored or printed. The reasoning
    effort and the provider pin travel in the request body, so the identity
    a verdict binds names what the model actually received.
    """
    key = os.environ.get(OPENROUTER_KEY_ENV)
    if not key:
        raise Refusal(RefusalCode.PROVIDER_NOT_CONFIGURED)
    from langchain_openai import ChatOpenAI

    body: dict[str, object] = {}
    effort = reasoning_effort()
    if effort:
        body["reasoning"] = {"effort": effort}
    order = [name.strip() for name in os.environ.get(PROVIDER_ORDER_ENV, "").split(",") if name.strip()]
    if order:
        body["provider"] = {"order": order, "allow_fallbacks": False}
    # The same deadline and no retry below the seam as before (F40): a retry
    # is the caller's reservation.
    return ChatOpenAI(
        base_url=OPENROUTER_BASE_URL,
        api_key=SecretStr(key),
        model=endpoint or configured_endpoint(),
        max_completion_tokens=MAX_COMPLETION_TOKENS,
        timeout=TIMEOUT_SECONDS,
        max_retries=0,
        extra_body=body or None,
    )
```

(imports: `os`, `pydantic.SecretStr`, `caos.pricing.reasoning_effort`). `from_environment` passes the effort so the identity carries it:

```python
def from_environment() -> ChatCompletions:
    endpoint = configured_endpoint()
    return completions(
        model_choices()[endpoint], endpoint=endpoint, reasoning_effort=reasoning_effort()
    )
```

Update the docstrings that say "ChatDatabricks"/"Databricks SDK" in `chat_model` and `completions`.

- [ ] **Step 4: Move the dependency.** In `pyproject.toml` move `"langchain-openai>=1.0",` (and its comment, rewritten as `# The production model provider (FD8).`) into `[project].dependencies`; run `uv lock && uv sync --locked --all-groups`. Add to `docs/decisions.md`:

```markdown
- FD8 (2026-10-04) — `langchain-openai` (already locked) moves from the test group to the
  runtime: `caos.models.chat_model` is `ChatOpenAI` on OpenRouter. `CAOS_REASONING_EFFORT`
  is now sent (`reasoning.effort`) and named in the identity, so the N2 refusal becomes a
  validation; `CAOS_PROVIDER_ORDER` pins the provider without fallbacks.
```

- [ ] **Step 5: Run the gates touched:** `uv run pytest tests/test_models.py tests/test_pricing*.py tests/test_worker.py -q`, then the full gate list. Expected: PASS. Fix every test that asserted `PLATFORM == "databricks"` or the old identity prefix.
- [ ] **Step 6: Commit** `git commit -m "OpenRouter is the production model provider (FD8)"`.

---

### Task 2: Firebase edge mode (server identity)

**Agent:** `mx-precise`. Review: `mx-precise` then `mx-adversary` (security).

**Files:**
- Create: `caos/api/firebase_auth.py`
- Modify: `caos/api/identity.py` (`FIREBASE_ENV`, firebase branch in `actor_from_headers`)
- Modify: `caos/api/edge.py` (`EdgeMode.firebase`, `_firebase_mode`, `_refusal`, `_secured` CSP)
- Test: `tests/test_firebase_auth.py`, `tests/test_edge_firebase.py`

**Interfaces:**
- Consumes: `caos.api.identity.Actor`, `GlobalRole`; `caos.refusals.Refusal`, `RefusalCode`.
- Produces: `caos.api.identity.FIREBASE_ENV = "CAOS_FIREBASE_PROJECT"`; `caos.api.firebase_auth.ROLE_MAP_ENV = "CAOS_ROLE_MAP"`, `SESSION_COOKIE = "__session"`, `role_map(value: str) -> dict[str, GlobalRole]`, `bearer_token(authorization: object, cookie: object) -> str | None`, `GoogleKeys` (with `key(kid: str) -> object`), `verify(token: str, project: str, keys: GoogleKeys, now: float) -> Mapping[str, object]`, `async actor_from_firebase(authorization: object, cookie: object) -> Actor`; `caos.api.edge.EdgeMode.firebase: bool`, `caos.api.edge.FIREBASE_CSP: str`.

- [ ] **Step 1: Write the failing tests** `tests/test_firebase_auth.py` (keys generated per test session; no network):

```python
import time
from uuid import uuid5

import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import datetime as dt

from caos.api import firebase_auth as fa
from caos.api.identity import GlobalRole
from caos.refusals import Refusal, RefusalCode

PROJECT = "caos-demo"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _cert_pem() -> bytes:
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(KEY.public_key()).serial_number(1)
        .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=1))
        .sign(KEY, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM)


KEYS = fa.GoogleKeys(fetch=lambda: ({"k1": _cert_pem().decode()}, 3600.0))


def _token(**overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": f"https://securetoken.google.com/{PROJECT}", "aud": PROJECT,
        "sub": "uid-1", "iat": now, "exp": now + 3600, "auth_time": now,
        "email": "Analyst@Example.com", "email_verified": True,
    } | overrides
    return jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": overrides.pop("kid", "k1")})


def test_a_valid_token_verifies():
    assert fa.verify(_token(), PROJECT, KEYS, time.time())["sub"] == "uid-1"


@pytest.mark.parametrize("bad", [
    {"aud": "other"}, {"iss": "https://securetoken.google.com/other"},
    {"exp": int(time.time()) - 3600}, {"email_verified": False}, {"sub": ""},
    {"auth_time": int(time.time()) + 3600},
])
def test_a_token_that_breaks_a_rule_is_not_authenticated(bad):
    with pytest.raises(Refusal) as refused:
        fa.verify(_token(**bad), PROJECT, KEYS, time.time())
    assert refused.value.code is RefusalCode.NOT_AUTHENTICATED


def test_an_unknown_key_id_or_another_algorithm_is_not_authenticated():
    other = jwt.encode({"sub": "x"}, "secret", algorithm="HS256", headers={"kid": "k1"})
    for token in (other, jwt.encode({"sub": "x"}, KEY, algorithm="RS256", headers={"kid": "nope"})):
        with pytest.raises(Refusal) as refused:
            fa.verify(token, PROJECT, KEYS, time.time())
        assert refused.value.code is RefusalCode.NOT_AUTHENTICATED


def test_the_role_map_reads_lower_cased_emails_and_refuses_a_malformed_one():
    assert fa.role_map("Analyst@Example.com=analyst; v@x.io=READER") == {
        "analyst@example.com": GlobalRole.ANALYST, "v@x.io": GlobalRole.READER}
    for bad in ("", "noemail=READER", "a@b.c", "a@b.c=OWNER", "a@b.c=READER;a@b.c=ADMIN"):
        with pytest.raises(Refusal) as refused:
            fa.role_map(bad)
        assert refused.value.code is RefusalCode.EDGE_CONFIG_INVALID


def test_the_token_comes_from_the_bearer_header_else_the_session_cookie():
    assert fa.bearer_token("Bearer abc", None) == "abc"
    assert fa.bearer_token(None, "x=1; __session=def") == "def"
    assert fa.bearer_token("Basic abc", "__session=def") is None
    assert fa.bearer_token(None, None) is None


async def test_the_actor_is_minted_from_the_uid_and_the_role_from_the_map(monkeypatch):
    monkeypatch.setenv("CAOS_FIREBASE_PROJECT", PROJECT)
    monkeypatch.setenv("CAOS_ROLE_MAP", "analyst@example.com=ANALYST")
    monkeypatch.setattr(fa, "_KEYS", KEYS)
    actor = await fa.actor_from_firebase(f"Bearer {_token()}", None)
    assert actor.role is GlobalRole.ANALYST
    assert actor.user_id == uuid5(fa.NAMESPACE, "firebase:uid-1")


async def test_an_email_outside_the_map_is_not_authorised(monkeypatch):
    monkeypatch.setenv("CAOS_FIREBASE_PROJECT", PROJECT)
    monkeypatch.setenv("CAOS_ROLE_MAP", "someone@else.com=ADMIN")
    monkeypatch.setattr(fa, "_KEYS", KEYS)
    with pytest.raises(Refusal) as refused:
        await fa.actor_from_firebase(f"Bearer {_token()}", None)
    assert refused.value.code is RefusalCode.NOT_AUTHORISED
```

and `tests/test_edge_firebase.py`, driving `EdgeGuard` the way the existing edge tests do (copy their ASGI scope helper from `tests/test_edge*.py`):

```python
FIREBASE_ENV = {"CAOS_FIREBASE_PROJECT": "caos-demo", "CAOS_PUBLIC_ORIGIN": "https://caos.example.run.app",
                "CAOS_ROLE_MAP": "a@b.c=ANALYST"}

# 1. resolve_mode(FIREBASE_ENV).firebase is True.
# 2. resolve_mode refuses EDGE_CONFIG_INVALID with: DATABRICKS_APP_NAME added; CAOS_TRUST_ROLE_HEADER
#    added; CAOS_PUBLIC_ORIGIN missing; CAOS_PUBLIC_ORIGIN "http://..." (not https); CAOS_ROLE_MAP "".
# 3. A non-loopback peer is served (not EDGE_NOT_TRUSTED) in firebase mode.
# 4. Inbound x-caos-user / x-caos-role / x-forwarded-groups are absent from the scope the app sees.
# 5. Two authorization headers are 401 NOT_AUTHENTICATED.
# 6. An unsafe /api request with Origin https://evil.example is 403 ORIGIN_REFUSED.
# 7. Every response's content-security-policy equals FIREBASE_CSP, whose connect-src is
#    "'self' https://identitytoolkit.googleapis.com https://securetoken.googleapis.com",
#    and outside firebase mode it is unchanged (SECURITY_HEADERS).
```

Write each numbered case as its own `test_...` function with the asserts stated.

- [ ] **Step 2: Run to fail:** `uv run pytest tests/test_firebase_auth.py tests/test_edge_firebase.py -q` → FAIL (module missing).

- [ ] **Step 3: Implement `caos/api/firebase_auth.py`:**

```python
"""Firebase ID tokens, verified here and nowhere else (FD6, design §4.3).

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
    return morsel.value or None if morsel is not None else None


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


class GoogleKeys:
    """The signing keys, cached for the response's max-age; thread-safe."""

    def __init__(
        self,
        fetch: Callable[[], tuple[Mapping[str, str], float]] = _fetch,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch, self._clock = fetch, clock
        self._keys: dict[str, object] = {}
        self._until = 0.0
        self._fetched = float("-inf")
        self._lock = threading.Lock()

    def key(self, kid: str) -> object:
        with self._lock:
            now = self._clock()
            stale = now >= self._until
            unknown = kid not in self._keys and now - self._fetched >= REFETCH_FLOOR_SECONDS
            if stale or unknown:
                try:
                    pems, max_age = self._fetch()
                    self._keys = {
                        name: load_pem_x509_certificate(pem.encode()).public_key()
                        for name, pem in pems.items()
                    }
                except (OSError, ValueError, TypeError, AttributeError):
                    raise Refusal(RefusalCode.IDENTITY_UNAVAILABLE) from None
                self._fetched, self._until = now, now + max_age
            if kid not in self._keys:
                raise Refusal(RefusalCode.NOT_AUTHENTICATED)
            return self._keys[kid]


_KEYS = GoogleKeys()


def verify(token: str, project: str, keys: GoogleKeys, now: float) -> Mapping[str, object]:
    """The token's claims if every Firebase rule holds, else `NOT_AUTHENTICATED`."""
    try:
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        if header.get("alg") != "RS256" or not isinstance(kid, str):
            raise Refusal(RefusalCode.NOT_AUTHENTICATED)
        claims = jwt.decode(
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
```

Typing, with **no new suppressions**: `_keys: dict[str, RSAPublicKey]` and `key(...) -> RSAPublicKey` (`from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey`); when loading, a certificate whose `public_key()` is not an `RSAPublicKey` raises `Refusal(IDENTITY_UNAVAILABLE)`. `jwt.decode` accepts an `RSAPublicKey`, so mypy needs no ignore.

In `caos/api/identity.py` add beside `PLATFORM_ENV`: `FIREBASE_ENV = "CAOS_FIREBASE_PROJECT"`, and at the top of `actor_from_headers` after the `get is None` check:

```python
    if os.environ.get(FIREBASE_ENV):
        # Imported here: `firebase_auth` imports this module's `Actor`.
        from caos.api.firebase_auth import actor_from_firebase

        return await actor_from_firebase(get("authorization"), get("cookie"))
```

In `caos/api/edge.py`:

```python
from caos.api.identity import FIREBASE_ENV

FIREBASE_CSP = SECURITY_HEADERS["content-security-policy"].replace(
    "connect-src 'self'",
    "connect-src 'self' https://identitytoolkit.googleapis.com https://securetoken.googleapis.com",
)


@dataclass(frozen=True, slots=True)
class EdgeMode:
    public_origin: str | None
    platform: bool = False
    firebase: bool = False


def resolve_mode(environ=None) -> EdgeMode:
    ...
    if env.get(FIREBASE_ENV):
        return _firebase_mode(env)
    if env.get(PLATFORM_ENV):
        return _platform_mode(env)
    return _dev_mode(env)


def _firebase_mode(env: Mapping[str, str]) -> EdgeMode:
    """Firebase mode (FD6), or `EDGE_CONFIG_INVALID`: never beside the platform or
    the trust switch, an `https` public origin required, the role map well formed."""
    from caos.api.firebase_auth import ROLE_MAP_ENV, role_map

    origin = env.get(PUBLIC_ORIGIN_ENV)
    if env.get(PLATFORM_ENV) or TRUST_SWITCH in env:
        raise Refusal(RefusalCode.EDGE_CONFIG_INVALID)
    if origin is None or not _bare_origin(origin) or not origin.startswith("https://"):
        raise Refusal(RefusalCode.EDGE_CONFIG_INVALID)
    role_map(env.get(ROLE_MAP_ENV, ""))
    return EdgeMode(public_origin=origin, firebase=True)
```

`_refusal`: `if not (mode.platform or mode.firebase) and not _dev_peer(scope, headers):`; after the platform header check add `if mode.firebase and len(_all(headers, "authorization")) > 1: return RefusalCode.NOT_AUTHENTICATED, mode`. `__call__`: `edged = mode is None or mode.platform or mode.firebase`; `guarded = _secured(send, path, firebase=mode is not None and mode.firebase)`. `_secured(send, path, *, firebase=False)` passes it to `_security_header_pairs(path, firebase=firebase)`, which returns the pairs with `FIREBASE_CSP` in place of the policy when `firebase` is true (the deliverable render keeps its own narrower policy in every mode).

- [ ] **Step 4: Run to pass:** `uv run pytest tests/test_firebase_auth.py tests/test_edge_firebase.py tests/test_edge*.py tests/test_identity*.py -q` → PASS.
- [ ] **Step 5: Gates:** full list, especially `check_tested`, `check_vocabulary`, `io_budget --assert`, `check_gate_config` (no suppression rise), bandit floor, mypy, complexipy.
- [ ] **Step 6: Commit** `git commit -m "Firebase edge mode: server-verified ID tokens, roles from the server's map (FD6)"`.

---

### Task 3: Frontend sign-in

**Agent:** `mx-feature`. Review: `mx-precise`.

**Files:**
- Modify: `frontend/package.json`, `frontend/package-lock.json` (`npm install --save-exact firebase@<latest 12.x>`)
- Create: `frontend/src/app/firebase.ts`, `frontend/src/app/AuthGate.tsx`
- Modify: `frontend/src/main.tsx` (wrap `<App />`), `frontend/src/vite-env.d.ts` (env types), `frontend/src/chrome/AppSidebar.tsx` (Sign out live when `auth` is set)
- Test: `frontend/tests/unit/auth-gate.test.tsx`

**Interfaces:**
- Consumes: the server reads `Authorization: Bearer` or the `__session` cookie (Task 2).
- Produces: `auth: Auth | null`, `writeSessionCookie(token: string | null): void`, `SESSION_COOKIE` from `app/firebase.ts`; `AuthGate({children, client?})`.

- [ ] **Step 1: Failing test** `frontend/tests/unit/auth-gate.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

const listeners: Array<(user: { getIdToken(): Promise<string> } | null) => void> = [];
const signIn = vi.fn(async () => undefined);
vi.mock("firebase/auth", () => ({
  onIdTokenChanged: (_auth: unknown, cb: (typeof listeners)[number]) => {
    listeners.push(cb);
    return () => undefined;
  },
  signInWithEmailAndPassword: (...args: unknown[]) => signIn(...args),
  signOut: vi.fn(),
  initializeAuth: vi.fn(),
  indexedDBLocalPersistence: {},
}));

import { AuthGate } from "../../src/app/AuthGate";

describe("AuthGate", () => {
  it("renders the workspace when no project is configured", () => {
    render(<AuthGate client={null}><p>workspace</p></AuthGate>);
    expect(screen.getByText("workspace")).toBeInTheDocument();
  });

  it("asks for email and password when signed out, and signs in with them", async () => {
    render(<AuthGate client={{} as never}><p>workspace</p></AuthGate>);
    listeners.at(-1)!(null);
    await userEvent.type(await screen.findByLabelText("Email"), "a@b.c");
    await userEvent.type(screen.getByLabelText("Password"), "pw");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(signIn).toHaveBeenCalledWith({}, "a@b.c", "pw");
    expect(screen.queryByText("workspace")).toBeNull();
  });

  it("writes the session cookie and shows the workspace once a token arrives", async () => {
    render(<AuthGate client={{} as never}><p>workspace</p></AuthGate>);
    listeners.at(-1)!({ getIdToken: async () => "tok" });
    expect(await screen.findByText("workspace")).toBeInTheDocument();
    expect(document.cookie).toContain("__session=tok");
  });
});
```

(If `@testing-library/user-event` is not installed, use `fireEvent` from `@testing-library/react` instead; do not add a dependency.)

- [ ] **Step 2: Run to fail:** `npm --prefix frontend run test -- auth-gate` → FAIL.
- [ ] **Step 3: Implement.** `frontend/src/app/firebase.ts`:

```ts
import { initializeApp } from "firebase/app";
import { type Auth, indexedDBLocalPersistence, initializeAuth } from "firebase/auth";

const config = {
  apiKey: import.meta.env.VITE_FIREBASE_API_KEY,
  authDomain: import.meta.env.VITE_FIREBASE_AUTH_DOMAIN,
  projectId: import.meta.env.VITE_FIREBASE_PROJECT_ID,
};

// No popup or redirect resolver: email/password is plain HTTPS to Google, so no
// third-party script or frame loads and the policy keeps Trusted Types (FD6).
export const auth: Auth | null =
  config.apiKey && config.projectId
    ? initializeAuth(initializeApp(config), { persistence: indexedDBLocalPersistence })
    : null;

export const SESSION_COOKIE = "__session";

/** The cookie the server reads where a request cannot carry a header (the run stream). */
export function writeSessionCookie(token: string | null): void {
  document.cookie = token
    ? `${SESSION_COOKIE}=${token}; Path=/; Secure; SameSite=Strict; Max-Age=3600`
    : `${SESSION_COOKIE}=; Path=/; Secure; SameSite=Strict; Max-Age=0`;
}
```

`frontend/src/app/AuthGate.tsx` — a gate that shows children when `client` is null or a token is held, "Checking sign-in…" (`role="status"`) before the first callback, else a sign-in form. Use the app's own `Button`/`Input`/`Label` components from `frontend/src/components/ui/` (as other forms do), labels "Email" and "Password", button "Sign in", `autoComplete` `username`/`current-password`, and on failure a `role="alert"` line "Sign-in failed. Check the email and password." (never the Firebase error text):

```tsx
import { onIdTokenChanged, signInWithEmailAndPassword, type Auth } from "firebase/auth";
import { type FormEvent, type ReactNode, useEffect, useState } from "react";
import { auth, writeSessionCookie } from "./firebase";

type Gate = "checking" | "signed-out" | "signed-in";

export function AuthGate({ children, client = auth }: { children: ReactNode; client?: Auth | null }) {
  const [gate, setGate] = useState<Gate>(client ? "checking" : "signed-in");
  useEffect(() => {
    if (!client) return undefined;
    return onIdTokenChanged(client, (user) => {
      void (user ? user.getIdToken() : Promise.resolve(null)).then((token) => {
        writeSessionCookie(token);
        setGate(token ? "signed-in" : "signed-out");
      });
    });
  }, [client]);
  if (gate === "signed-in") return <>{children}</>;
  if (gate === "checking" || !client) return <p role="status">Checking sign-in…</p>;
  return <SignIn client={client} />;
}

function SignIn({ client }: { client: Auth }) {
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(false);
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setBusy(true);
    setFailed(false);
    signInWithEmailAndPassword(client, String(form.get("email")), String(form.get("password")))
      .catch(() => setFailed(true))
      .finally(() => setBusy(false));
  }
  return (
    <main className="mx-auto flex min-h-svh max-w-sm flex-col justify-center gap-4 p-6">
      <h1 className="text-lg font-semibold">Sign in to CAOS</h1>
      <form className="flex flex-col gap-3" onSubmit={submit}>
        {/* Label/Input/Button from components/ui, ids "email" and "password" */}
        {failed ? <p role="alert">Sign-in failed. Check the email and password.</p> : null}
      </form>
    </main>
  );
}
```

`main.tsx`: `<StrictMode><AuthGate><App /></AuthGate></StrictMode>`. `vite-env.d.ts`: declare `VITE_FIREBASE_API_KEY`, `VITE_FIREBASE_AUTH_DOMAIN`, `VITE_FIREBASE_PROJECT_ID` as `string | undefined` on `ImportMetaEnv`. `AppSidebar.tsx`: when `auth` is non-null render a plain ghost `Button` "Sign out" calling `signOut(auth)` in place of the `SIGN_OUT_UNPLACED` `RefusedControl`; with `auth` null keep the refused control exactly as now (existing chrome tests run with no Firebase env and must stay green).

- [ ] **Step 4: Pass:** `npm --prefix frontend run lint && npm --prefix frontend run typecheck && npm --prefix frontend run test && npm --prefix frontend run build && npm --prefix frontend run build:demo && npm --prefix frontend run a11y`.
- [ ] **Step 5: Commit** `git commit -m "Sign in with Firebase email/password; the session cookie carries the token to the stream (FD6, FD7)"`.

---

### Task 4: Container image and deploy runbook

**Agent:** `mx-feature`. Review: `mx-precise`.

**Files:**
- Create: `Dockerfile`, `.dockerignore`, `cloudbuild.yaml`, `docs/DEPLOY.md`
- Test: container smoke (commands below)

- [ ] **Step 1: `Dockerfile`** (pin the uv image to the local `uv --version`):

```dockerfile
# syntax=docker/dockerfile:1
FROM node:24-slim AS frontend
WORKDIR /src
COPY frontend/package.json frontend/package-lock.json frontend/
RUN npm --prefix frontend ci --ignore-scripts
COPY . .
ARG VITE_FIREBASE_API_KEY
ARG VITE_FIREBASE_AUTH_DOMAIN
ARG VITE_FIREBASE_PROJECT_ID
RUN npm --prefix frontend run build

FROM python:3.13-slim AS app
COPY --from=ghcr.io/astral-sh/uv:<local uv version> /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev
COPY caos/ caos/
COPY icm/ icm/
COPY vendor/ vendor/
COPY --from=frontend /src/frontend/dist frontend/dist
RUN useradd --uid 10001 --no-create-home caos && chown -R caos /app
USER caos
ENV CAOS_SITE_ROOT=frontend/dist CAOS_BIND_HOST=0.0.0.0 CAOS_WORKER_IN_PROCESS=1
EXPOSE 8000
CMD ["/app/.venv/bin/python", "-m", "caos.serve"]
```

If boot needs any other tracked path (a health code naming a missing file), copy that path too and say which in the commit message.

- [ ] **Step 2: `.dockerignore`:** `.git`, `**/node_modules`, `frontend/dist`, `frontend/dist-demo`, `.venv*`, `**/__pycache__`, `.mypy_cache`, `.ruff_cache`, `.pytest_cache`, `.complexipy_cache`, `.env`, `.dev-data`, `docs/rebuild/runs`, `coverage.xml`, `.coverage`, `bandit.json`.
- [ ] **Step 3: `cloudbuild.yaml`:**

```yaml
steps:
  - name: gcr.io/cloud-builders/docker
    args: ["build", "-t", "${_IMAGE}",
           "--build-arg", "VITE_FIREBASE_API_KEY=${_FIREBASE_API_KEY}",
           "--build-arg", "VITE_FIREBASE_AUTH_DOMAIN=${_FIREBASE_AUTH_DOMAIN}",
           "--build-arg", "VITE_FIREBASE_PROJECT_ID=${PROJECT_ID}", "."]
images: ["${_IMAGE}"]
```

- [ ] **Step 4: Container smoke** (local Postgres from `compose.yaml`, port 55436):

```bash
docker build -t caos-firebase:smoke .
docker run --rm -d --name caos-smoke -p 127.0.0.1:8001:8000 \
  -e CAOS_DATABASE_URL=postgresql://caos_app:local-app-only@host.docker.internal:55436/caos_dev \
  -e CAOS_BLOB_ROOT=/tmp/blobs -e CAOS_MODEL_ENDPOINT=openai/gpt-6-luna \
  -e CAOS_MODEL_PRICE=openai/gpt-6-luna,0.0000002,0.000001,2026-10-04 \
  caos-firebase:smoke
sleep 20 && curl -s http://127.0.0.1:8001/api/health; docker stop caos-smoke
```

Expected: every health code `OK` (the model price is the owner's 2026-10-03 figure; match `docs/rebuild/decisions.md` at the fork if it differs).

- [ ] **Step 5: `docs/DEPLOY.md`** — the runbook of Task 6's commands, in order, each with what it proves. No secret values in the file.
- [ ] **Step 6: Commit** `git commit -m "Cloud Run image, Cloud Build config and deploy runbook"`.

---

### Task 5: Provision and deploy (controller with owner)

Owner first: Firebase project on Blaze; `gcloud auth login`; `firebase login`; Authentication → Sign-in method → Email/Password on; users added (owner ANALYST, viewers READER); do **not** enable reCAPTCHA or email-enumeration protection (they load third-party scripts).

```bash
PROJECT=<firebase project id>; REGION=europe-west2; gcloud config set project $PROJECT
gcloud services enable run.googleapis.com sqladmin.googleapis.com secretmanager.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com identitytoolkit.googleapis.com
gcloud artifacts repositories create caos --repository-format=docker --location=$REGION
gcloud sql instances create caos-pg --database-version=POSTGRES_17 --edition=ENTERPRISE --tier=db-f1-micro --region=$REGION
gcloud sql databases create caos --instance=caos-pg
gcloud storage buckets create gs://$PROJECT-caos-blobs --location=$REGION --uniform-bucket-level-access --public-access-prevention
# Owner runs these two in the terminal; the values are never shown to the agent:
#   DBPW=$(openssl rand -hex 24); gcloud sql users create caos --instance=caos-pg --password="$DBPW"
#   printf 'postgresql://caos:%s@/caos?host=/cloudsql/%s:%s:caos-pg' "$DBPW" $PROJECT $REGION | gcloud secrets create caos-database-url --data-file=-; unset DBPW
#   gcloud secrets create openrouter-key --data-file=-    (paste the caos-qualification key, then Ctrl-D)
SA=$(gcloud projects describe $PROJECT --format='value(projectNumber)')-compute@developer.gserviceaccount.com
gcloud projects add-iam-policy-binding $PROJECT --member=serviceAccount:$SA --role=roles/cloudsql.client
gcloud projects add-iam-policy-binding $PROJECT --member=serviceAccount:$SA --role=roles/secretmanager.secretAccessor
gcloud storage buckets add-iam-policy-binding gs://$PROJECT-caos-blobs --member=serviceAccount:$SA --role=roles/storage.objectUser
IMAGE=$REGION-docker.pkg.dev/$PROJECT/caos/caos:$(git rev-parse --short HEAD)
gcloud builds submit --config cloudbuild.yaml --substitutions=_IMAGE=$IMAGE,_FIREBASE_API_KEY=<web apiKey>,_FIREBASE_AUTH_DOMAIN=$PROJECT.firebaseapp.com
gcloud run deploy caos --image $IMAGE --region $REGION --port 8000 --no-cpu-throttling --min-instances 1 --max-instances 1 --timeout 3600 --memory 2Gi --cpu 1 --execution-environment gen2 --allow-unauthenticated \
  --add-cloudsql-instances $PROJECT:$REGION:caos-pg \
  --add-volume name=blobs,type=cloud-storage,bucket=$PROJECT-caos-blobs --add-volume-mount volume=blobs,mount-path=/data/blobs \
  --set-secrets OPENROUTER_API_KEY=openrouter-key:latest,CAOS_DATABASE_URL=caos-database-url:latest \
  --set-env-vars "^@^CAOS_BLOB_ROOT=/data/blobs@CAOS_FIREBASE_PROJECT=$PROJECT@CAOS_ROLE_MAP=<owner email>=ANALYST;<viewer email>=READER@CAOS_MODEL_ENDPOINT=openai/gpt-6-luna@CAOS_MODEL_PRICE=<as Task 4>@CAOS_REASONING_EFFORT=high@CAOS_PROVIDER_ORDER=openai@CAOS_PUBLIC_ORIGIN=https://placeholder.invalid"
URL=$(gcloud run services describe caos --region $REGION --format='value(status.url)')
gcloud run services update caos --region $REGION --update-env-vars CAOS_PUBLIC_ORIGIN=$URL
```

(`--allow-unauthenticated` lets the browser reach the service; sign-in is the app's own. The `^@^` prefix makes `@` the env separator so the role map's `;` survives. The first deploy boots with a placeholder origin that is replaced at once.) The web `apiKey` comes from `firebase apps:sdkconfig web` after `firebase apps:create web caos`.

- [ ] Smoke through `$URL`: `/api/health` all OK; a request without a token is 401; the Reader cannot start a run; the owner uploads one small public filing (e.g. from `qualification/czr-*`), starts a LITE run, and the run stream stays open past 60 s to completion.
- [ ] Grant the Reader user READER standing on the demo case (Admin section).

### Task 6: Demo case (controller with owner)

- [ ] Run one full case in advance on the deployed service; record its URL and run id in the ledger.

---

## Self-review

- Spec §2 R1–R8: R1 by Task 0 (fork of `caos-databricks`); R2 by Tasks 4–5 (no Hosting); R3 by Task 5 flags; R4 by Task 1; R5 by Tasks 2–3; R6, R7 by Task 5; R8 n/a.
- §4.3 detail changed in planning: the session cookie is accepted on every method, not only `GET`. The edge already refuses a cross-site unsafe request (Origin and `Sec-Fetch-Site`, `CAOS_PUBLIC_ORIGIN` required in firebase mode) and the cookie is `SameSite=Strict`, so the frontend needs no fetch changes. Recorded in FD6 when Task 2 lands.
- Names: `FIREBASE_ENV`, `ROLE_MAP_ENV`, `SESSION_COOKIE`, `FIREBASE_CSP`, `reasoning_effort()`, `EFFORTS` are used consistently across Tasks 1–5.

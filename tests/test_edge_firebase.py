"""The edge guard in firebase mode (FD6, design §4.3).

Driven over a recording app, as `tests/test_edge.py` drives the other two
modes: what reached the application is exactly what the recorder kept.
"""

from __future__ import annotations

import anyio
import pytest
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from caos.api.edge import (
    _DELIVERABLE_RENDER_CSP,
    FIREBASE_CSP,
    SECURITY_HEADER_PAIRS,
    SECURITY_HEADERS,
    EdgeGuard,
    EdgeMode,
    _repeated_credential,
    _secured,
    _security_header_pairs,
    resolve_mode,
)
from caos.refusals import Refusal, RefusalCode

PUBLIC = "https://caos.example.run.app"
FIREBASE_ENV = {
    "CAOS_FIREBASE_PROJECT": "caos-demo",
    "CAOS_PUBLIC_ORIGIN": PUBLIC,
    "CAOS_ROLE_MAP": "a@b.c=ANALYST",
}
FAR = ("203.0.113.9", 4000)


class Recorder:
    """An app that answers 200 and keeps the headers it was handed."""

    def __init__(self) -> None:
        self.seen: list[list[tuple[bytes, bytes]]] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.seen.append(list(scope["headers"]))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"reached"})


@pytest.fixture
def firebase(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in FIREBASE_ENV.items():
        monkeypatch.setenv(name, value)


def _far() -> tuple[Recorder, TestClient]:
    recorder = Recorder()
    client = TestClient(EdgeGuard(recorder), client=FAR, base_url=PUBLIC)
    return recorder, client


def test_the_firebase_project_selects_firebase_mode() -> None:
    mode = resolve_mode(FIREBASE_ENV)
    assert mode.firebase is True
    assert mode.platform is False
    assert mode.public_origin == PUBLIC


@pytest.mark.parametrize(
    "change",
    [
        {"DATABRICKS_APP_NAME": "caos"},
        {"CAOS_TRUST_ROLE_HEADER": "1"},
        {"CAOS_PUBLIC_ORIGIN": None},
        {"CAOS_PUBLIC_ORIGIN": "http://caos.example.run.app"},
        {"CAOS_PUBLIC_ORIGIN": "https://caos.example.run.app/"},
        {"CAOS_ROLE_MAP": ""},
        {"CAOS_ROLE_MAP": None},
        {"CAOS_FIREBASE_PROJECT": ""},
        {"CAOS_FIREBASE_PROJECT": "  "},
    ],
)
def test_firebase_mode_refuses_a_configuration_it_cannot_serve(
    change: dict[str, str | None],
) -> None:
    env = {**FIREBASE_ENV, **change}
    with pytest.raises(Refusal) as refused:
        resolve_mode({k: v for k, v in env.items() if v is not None})
    assert refused.value.code is RefusalCode.EDGE_CONFIG_INVALID


@pytest.mark.usefixtures("firebase")
def test_a_non_loopback_peer_is_served() -> None:
    recorder, client = _far()
    served = client.get("/x")
    assert served.status_code == 200
    assert len(recorder.seen) == 1


@pytest.mark.usefixtures("firebase")
def test_inbound_identity_headers_never_reach_the_app() -> None:
    recorder, client = _far()
    client.get(
        "/x",
        headers={
            "x-caos-user": "00000000-0000-0000-0000-000000000001",
            "x-caos-role": "ADMIN",
            "x-forwarded-groups": "admins",
            "authorization": "Bearer t",
        },
    )
    names = {key for key, _ in recorder.seen[0]}
    assert not names & {b"x-caos-user", b"x-caos-role", b"x-forwarded-groups"}
    assert b"authorization" in names


@pytest.mark.usefixtures("firebase")
def test_two_authorization_headers_are_not_authenticated() -> None:
    recorder, client = _far()
    refused = client.get(
        "/x", headers=[("authorization", "Bearer a"), ("authorization", "Bearer b")]
    )
    assert refused.status_code == 401
    assert refused.json()["code"] == "NOT_AUTHENTICATED"
    assert recorder.seen == []


@pytest.mark.usefixtures("firebase")
def test_an_unsafe_request_from_another_origin_is_refused() -> None:
    recorder, client = _far()
    refused = client.post("/api/v1/x", headers={"origin": "https://evil.example"})
    assert refused.status_code == 403
    assert refused.json()["code"] == "ORIGIN_REFUSED"
    allowed = client.post("/api/v1/x", headers={"origin": PUBLIC})
    assert allowed.status_code == 200
    assert len(recorder.seen) == 1


def test_the_policy_admits_firebase_auth_only_in_firebase_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect = "connect-src 'self' https://identitytoolkit.googleapis.com https://securetoken.googleapis.com;"
    assert connect in FIREBASE_CSP
    assert (
        FIREBASE_CSP.replace(connect, "connect-src 'self';")
        == SECURITY_HEADERS["content-security-policy"]
    )
    dev = TestClient(EdgeGuard(Recorder())).get("/x")
    assert (
        dev.headers["content-security-policy"]
        == SECURITY_HEADERS["content-security-policy"]
    )
    for name, value in FIREBASE_ENV.items():
        monkeypatch.setenv(name, value)
    _, client = _far()
    for response in (
        client.get("/x"),
        client.get(
            "/api/v1/x", headers=[("authorization", "a"), ("authorization", "b")]
        ),
    ):
        assert response.headers["content-security-policy"] == FIREBASE_CSP


@pytest.mark.usefixtures("firebase")
def test_two_cookie_headers_are_not_authenticated() -> None:
    recorder, client = _far()
    refused = client.get(
        "/x", headers=[("cookie", "__session=a"), ("cookie", "__session=b")]
    )
    assert refused.status_code == 401
    assert refused.json()["code"] == "NOT_AUTHENTICATED"
    assert recorder.seen == []


@pytest.mark.usefixtures("firebase")
def test_a_cookie_authenticated_unsafe_request_is_served_only_same_origin() -> None:
    recorder, client = _far()
    cookie = ("cookie", "__session=token")
    refused = client.post(
        "/api/v1/x",
        headers=[
            cookie,
            ("origin", "https://evil.example"),
            ("sec-fetch-site", "cross-site"),
        ],
    )
    assert refused.status_code == 403
    assert refused.json()["code"] == "ORIGIN_REFUSED"
    served = client.post(
        "/api/v1/x",
        headers=[cookie, ("origin", PUBLIC), ("sec-fetch-site", "same-origin")],
    )
    assert served.status_code == 200
    assert len(recorder.seen) == 1


@pytest.mark.usefixtures("firebase")
def test_the_deliverable_render_keeps_its_own_policy_in_firebase_mode() -> None:
    _, client = _far()
    served = client.get("/api/v1/cases/c/revisions/r/render")
    assert served.status_code == 200
    assert served.headers["content-security-policy"] == _DELIVERABLE_RENDER_CSP
    assert served.headers["x-content-type-options"] == "nosniff"


def test_the_policy_helpers_default_to_the_shared_policy() -> None:
    assert _security_header_pairs("/x") == SECURITY_HEADER_PAIRS
    sent: list[Message] = []

    async def record(message: Message) -> None:
        sent.append(message)

    start: Message = {"type": "http.response.start", "status": 200, "headers": []}
    anyio.run(_secured(record, "/x"), start)
    headers = dict(sent[0]["headers"])
    assert (
        headers[b"content-security-policy"]
        == SECURITY_HEADERS["content-security-policy"].encode()
    )


def test_behind_the_platform_one_forwarded_token_passes_and_two_do_not() -> None:
    platform = EdgeMode(public_origin=None, platform=True)
    one = [(b"x-forwarded-access-token", b"a")]
    assert _repeated_credential(platform, one) is False
    assert _repeated_credential(platform, one * 2) is True

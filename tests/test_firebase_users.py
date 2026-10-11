"""The provisioning script hides the password and creates verified users (FD19)."""

from __future__ import annotations

import firebase_users
import pytest

SECRET = "correct-horse-battery-staple"


class _User:
    def __init__(self, uid: str) -> None:
        self.uid = uid


Calls = list[tuple[str, dict[str, object]]]


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> Calls:
    """Records every Admin SDK call instead of reaching Firebase."""
    seen: Calls = []

    def initialize_app(**options: object) -> None:
        seen.append(("init", options))

    def create_user(**fields: object) -> _User:
        seen.append(("create", fields))
        return _User("u1")

    def get_user_by_email(email: str) -> _User:
        seen.append(("get", {"email": email}))
        return _User("u2")

    def update_user(uid: str, **fields: object) -> None:
        seen.append(("update", {"uid": uid, **fields}))

    monkeypatch.setattr(firebase_users.firebase_admin, "initialize_app", initialize_app)
    monkeypatch.setattr(firebase_users.auth, "create_user", create_user)
    monkeypatch.setattr(firebase_users.auth, "get_user_by_email", get_user_by_email)
    monkeypatch.setattr(firebase_users.auth, "update_user", update_user)
    return seen


def test_create_makes_a_verified_user_and_never_prints_the_password(
    calls: Calls, capsys: pytest.CaptureFixture[str]
) -> None:
    code = firebase_users.main(
        ["--project", "p", "create", "a@example.com"], read_password=lambda _: SECRET
    )
    out = capsys.readouterr()
    assert code == 0
    assert (
        "create",
        {"email": "a@example.com", "password": SECRET, "email_verified": True},
    ) in calls
    assert SECRET not in out.out + out.err


def test_create_refuses_a_mismatched_confirmation(
    calls: Calls, capsys: pytest.CaptureFixture[str]
) -> None:
    answers = iter([SECRET, "something-else"])
    code = firebase_users.main(
        ["--project", "p", "create", "a@example.com"],
        read_password=lambda _: next(answers),
    )
    assert code == 1
    assert not [call for call in calls if call[0] == "create"]
    assert SECRET not in capsys.readouterr().err


def test_verify_marks_an_existing_user(calls: Calls) -> None:
    assert firebase_users.main(["--project", "p", "verify", "b@example.com"]) == 0
    assert ("update", {"uid": "u2", "email_verified": True}) in calls


def test_refusal_prints_only_the_error_class(
    calls: Calls, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def exists(**_: object) -> None:
        raise firebase_users.auth.EmailAlreadyExistsError(
            "detail-a@example.com", None, None
        )

    monkeypatch.setattr(firebase_users.auth, "create_user", exists)
    code = firebase_users.main(
        ["--project", "p", "create", "a@example.com"], read_password=lambda _: SECRET
    )
    err = capsys.readouterr().err
    assert code == 1
    assert (
        "EmailAlreadyExistsError" in err and "detail" not in err and SECRET not in err
    )


def test_parser_needs_no_credentials() -> None:
    with pytest.raises(SystemExit) as done:
        firebase_users.parser().parse_args(["--help"])
    assert done.value.code == 0


def test_create_returns_the_new_uid(calls: Calls) -> None:
    assert firebase_users.create("a@example.com", SECRET) == "u1"
    assert calls == [
        (
            "create",
            {"email": "a@example.com", "password": SECRET, "email_verified": True},
        )
    ]


def test_verify_returns_the_existing_uid(calls: Calls) -> None:
    assert firebase_users.verify("b@example.com") == "u2"
    assert calls[-1] == ("update", {"uid": "u2", "email_verified": True})

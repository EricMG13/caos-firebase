"""The provisioning script hides the password and creates verified users (FD19)."""

from __future__ import annotations

import getpass
import inspect

import firebase_users
import pytest

SECRET = "correct-horse-battery-staple"
ONE_INIT = [("init", {"options": {"projectId": "p"}})]


def _inits(
    calls: list[tuple[str, dict[str, object]]],
) -> list[tuple[str, dict[str, object]]]:
    return [call for call in calls if call[0] == "init"]


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
    assert _inits(calls) == ONE_INIT


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
    out = capsys.readouterr()
    assert SECRET not in out.out + out.err
    assert (out.out, out.err) == ("", "create: refused (passwords differ)\n")


def test_verify_marks_an_existing_user(calls: Calls) -> None:
    assert firebase_users.main(["--project", "p", "verify", "b@example.com"]) == 0
    assert ("get", {"email": "b@example.com"}) in calls
    assert ("update", {"uid": "u2", "email_verified": True}) in calls
    assert _inits(calls) == ONE_INIT


def test_password_is_read_without_echo_by_default() -> None:
    default = inspect.signature(firebase_users.main).parameters["read_password"].default
    assert default is getpass.getpass


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
    assert calls == [
        ("get", {"email": "b@example.com"}),
        ("update", {"uid": "u2", "email_verified": True}),
    ]


def test_create_prompts_twice_and_reports_the_uid_on_stdout(
    calls: Calls, capsys: pytest.CaptureFixture[str]
) -> None:
    prompts: list[str] = []

    def read(prompt: str) -> str:
        prompts.append(prompt)
        return SECRET

    code = firebase_users.main(
        ["--project", "p", "create", "a@example.com"], read_password=read
    )
    out = capsys.readouterr()
    assert code == 0
    assert prompts == ["Password: ", "Repeat password: "]
    assert (out.out, out.err) == (
        "create: a@example.com uid=u1 email_verified=true\n",
        "",
    )


@pytest.mark.parametrize("argv", [["create", "a@example.com"], ["--project", "p"]])
def test_the_project_and_a_command_are_both_required(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as done:
        firebase_users.parser().parse_args(argv)
    assert done.value.code == 2
    assert "required" in capsys.readouterr().err


def test_the_help_describes_the_script_its_project_and_each_command() -> None:
    text = " " + " ".join(firebase_users.parser().format_help().split()) + " "
    for phrase in (
        "Create or verify a CAOS sign-in.",
        "Firebase project id",
        "create an email/password user, verified",
        "mark an existing user's email verified",
    ):
        assert f" {phrase} " in text

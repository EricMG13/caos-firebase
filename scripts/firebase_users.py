#!/usr/bin/env python3
"""Create a CAOS sign-in with its email verified, or verify an existing one (FD19).

The owner runs this on their own machine with application-default credentials
(`gcloud auth application-default login --client-id-file=<Desktop OAuth client JSON>`);
it never ships in the image. The password is read without echo, sent once to
Firebase, and never printed or logged. A refusal prints only the error's class.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Callable

import firebase_admin as firebase_admin
from firebase_admin import auth as auth

REFUSALS = (auth.EmailAlreadyExistsError, auth.UserNotFoundError, ValueError)


def parser() -> argparse.ArgumentParser:
    """The command line: `--project <id> create|verify <email>`."""
    root = argparse.ArgumentParser(description="Create or verify a CAOS sign-in.")
    root.add_argument("--project", required=True, help="Firebase project id")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "create", help="create an email/password user, verified"
    ).add_argument("email")
    commands.add_parser(
        "verify", help="mark an existing user's email verified"
    ).add_argument("email")
    return root


def create(email: str, password: str) -> str:
    """The new user's uid; the user is created with `email_verified` true."""
    return str(
        auth.create_user(email=email, password=password, email_verified=True).uid
    )


def verify(email: str) -> str:
    """The existing user's uid, after marking the email verified."""
    uid = str(auth.get_user_by_email(email).uid)
    auth.update_user(uid, email_verified=True)
    return uid


def main(
    argv: list[str] | None = None, read_password: Callable[[str], str] = getpass.getpass
) -> int:
    """Run one command; 0 on success, 1 on a refusal (class name only on stderr)."""
    args = parser().parse_args(argv)
    firebase_admin.initialize_app(options={"projectId": args.project})
    try:
        if args.command == "create":
            password = read_password("Password: ")
            if read_password("Repeat password: ") != password:
                print("create: refused (passwords differ)", file=sys.stderr)
                return 1
            uid = create(args.email, password)
        else:
            uid = verify(args.email)
    except REFUSALS as refused:
        print(f"{args.command}: refused ({type(refused).__name__})", file=sys.stderr)
        return 1
    print(f"{args.command}: {args.email} uid={uid} email_verified=true")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""The fork's own CI files pin every action as upstream's CI does (R23).

`scripts/check_gate_config.py` inspects only `ci.yml`, so this fork-owned test
holds every fork-owned file (`fork.yml`, `upstream-sync.yml`) to the same pins:
each `uses:` is a 40-hex commit SHA, and the same `action@sha` appears in `ci.yml`.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

CI_DIR = Path(__file__).resolve().parent.parent / ".github" / "workflows"
FORK_FILES = ("fork.yml", "upstream-sync.yml")
PINNED = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}$")


def _uses(node: object) -> Iterator[str]:
    """Every `uses:` value anywhere in a parsed CI file."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "uses" and isinstance(value, str):
                yield value
            else:
                yield from _uses(value)
    elif isinstance(node, list):
        for item in node:
            yield from _uses(item)


def _runs(node: object) -> Iterator[str]:
    """Every `run:` script anywhere in a parsed CI file."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "run" and isinstance(value, str):
                yield value
            else:
                yield from _runs(value)
    elif isinstance(node, list):
        for item in node:
            yield from _runs(item)


def _parsed(name: str) -> object:
    return yaml.safe_load((CI_DIR / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", FORK_FILES)
def test_every_fork_action_is_sha_pinned_and_matches_ci(name: str) -> None:
    fork = list(_uses(_parsed(name)))
    ci = set(_uses(_parsed("ci.yml")))
    assert fork, f"{name} names no action"
    for action in fork:
        assert PINNED.match(action), f"{action} is not pinned to a 40-hex SHA"
        assert action in ci, f"{action} is not pinned the same way in ci.yml"


@pytest.mark.parametrize("name", FORK_FILES)
def test_no_expression_is_spliced_into_a_run_script(name: str) -> None:
    for script in _runs(_parsed(name)):
        assert "${{" not in script, f"{name}: pass the value through env: {script[:60]}"


@pytest.mark.parametrize("name", FORK_FILES)
def test_permissions_are_declared(name: str) -> None:
    doc = _parsed(name)
    assert isinstance(doc, dict)
    jobs = doc["jobs"]
    assert "permissions" in doc or all("permissions" in job for job in jobs.values())


@pytest.mark.parametrize("name", FORK_FILES)
def test_no_pull_request_target_trigger(name: str) -> None:
    doc = _parsed(name)
    assert isinstance(doc, dict)
    triggers = doc.get("on", doc.get(True))  # YAML 1.1 reads a bare `on` as True
    assert triggers is not None
    assert "pull_request_target" not in triggers


def test_the_guards_see_a_violation() -> None:
    assert list(_runs({"steps": [{"run": "echo ${{ x }}"}, {"uses": "a/b@c"}]})) == [
        "echo ${{ x }}"
    ]
    assert "pull_request_target" in {"pull_request_target": None}


def test_a_tag_or_a_short_sha_is_not_a_pin() -> None:
    assert not PINNED.match("actions/checkout@v7")
    assert not PINNED.match("actions/checkout@3d3c42e")
    assert list(
        _uses({"jobs": {"a": {"steps": [{"uses": "x/y@v1"}, {"run": "z"}]}}})
    ) == ["x/y@v1"]

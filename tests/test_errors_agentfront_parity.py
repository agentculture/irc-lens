"""Parity tests: irc_lens error layer maps onto agentfront.errors.

Pins the mapping introduced by t2 of the adopt-agentfront-across-cli-and-site
plan (docs/plans/2026-07-02-adopt-agentfront-across-cli-and-site.md): the
irc-lens exit-code constants and ``AfiError`` are aligned onto
``agentfront.errors``' ``EXIT_*`` constants and ``AgentfrontError`` so that
agentfront's own dispatcher (which catches ``AgentfrontError``) can handle
``AfiError`` instances natively ahead of the CLI-dispatcher migration in t3.
"""
from __future__ import annotations

import agentfront.errors as agentfront_errors

from irc_lens._errors import (
    EXIT_ENV_ERROR,
    EXIT_SUCCESS,
    EXIT_USER_ERROR,
    AfiError,
)
from irc_lens.cli._errors import AfiError as ShimAfiError
from irc_lens.cli._errors import EXIT_ENV_ERROR as ShimExitEnvError
from irc_lens.cli._errors import EXIT_SUCCESS as ShimExitSuccess
from irc_lens.cli._errors import EXIT_USER_ERROR as ShimExitUserError


def test_exit_codes_equal_agentfront_constants() -> None:
    assert EXIT_SUCCESS == agentfront_errors.EXIT_SUCCESS
    assert EXIT_USER_ERROR == agentfront_errors.EXIT_USER_ERROR
    assert EXIT_ENV_ERROR == agentfront_errors.EXIT_ENV_ERROR


def test_cli_shim_exit_codes_equal_agentfront_constants() -> None:
    """The cli._errors re-export shim must carry the same identity/values."""
    assert ShimExitSuccess == agentfront_errors.EXIT_SUCCESS
    assert ShimExitUserError == agentfront_errors.EXIT_USER_ERROR
    assert ShimExitEnvError == agentfront_errors.EXIT_ENV_ERROR


def test_cli_shim_reexports_the_same_afierror_class() -> None:
    assert ShimAfiError is AfiError


def test_afierror_is_an_agentfronterror_subclass() -> None:
    """Subclassing (not aliasing) lets agentfront's dispatcher catch AfiError."""
    assert issubclass(AfiError, agentfront_errors.AgentfrontError)
    assert AfiError is not agentfront_errors.AgentfrontError


def test_afierror_to_dict_matches_agentfronterror_shape() -> None:
    afi = AfiError(code=EXIT_USER_ERROR, message="bad thing", remediation="fix it")
    upstream = agentfront_errors.AgentfrontError(
        code=EXIT_USER_ERROR, message="bad thing", remediation="fix it"
    )
    assert afi.to_dict() == upstream.to_dict()
    assert afi.to_dict() == {
        "code": EXIT_USER_ERROR,
        "message": "bad thing",
        "remediation": "fix it",
    }


def test_afierror_default_remediation_matches_agentfronterror_default() -> None:
    afi = AfiError(code=EXIT_SUCCESS, message="ok")
    upstream = agentfront_errors.AgentfrontError(code=EXIT_SUCCESS, message="ok")
    assert afi.remediation == upstream.remediation == ""


def test_afierror_instance_caught_by_except_agentfronterror() -> None:
    """agentfront's own dispatcher catches AgentfrontError; it must catch AfiError too."""
    try:
        raise AfiError(code=EXIT_ENV_ERROR, message="oops", remediation="hint")
    except agentfront_errors.AgentfrontError as err:
        assert isinstance(err, AfiError)
        assert err.to_dict() == {
            "code": EXIT_ENV_ERROR,
            "message": "oops",
            "remediation": "hint",
        }
    else:  # pragma: no cover - defensive, should never happen
        raise AssertionError("AfiError was not caught by except AgentfrontError")


def test_agentfronterror_instance_not_caught_by_except_afierror() -> None:
    """Sanity check the subclass direction: AgentfrontError is not an AfiError."""
    assert not isinstance(
        agentfront_errors.AgentfrontError(code=EXIT_USER_ERROR, message="x"), AfiError
    )

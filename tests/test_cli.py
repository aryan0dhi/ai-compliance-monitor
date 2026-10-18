"""Admin CLI (app/cli.py): systems, accounts, keys, thresholds and ledger verification."""

import re
from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.cli import run
from app.modules.auth.interface import Role, hash_api_key
from tests.fakes import ChainLedger, FixedClock


def cli(uow, *args):
    return run(list(args), uow_factory=lambda: uow)


def field(output: str, name: str) -> str:
    match = re.search(rf"^{name}:\s+(\S+)$", output, re.MULTILINE)
    assert match, f"{name} not in output: {output!r}"
    return match.group(1)


def test_create_system_and_system_account(uow, capsys):
    assert cli(uow, "create-system", "--name", "loan-scorer", "--interval", "60") == 0
    system_id = UUID(field(capsys.readouterr().out, "system_id"))
    system = uow.systems.get(system_id)
    assert (system.sampling_interval_s, system.gap_missed_intervals) == (60, 3)

    assert (
        cli(
            uow,
            "create-user",
            "--name",
            "ls-key",
            "--role",
            "system",
            "--system-id",
            str(system_id),
        )
        == 0
    )
    out = capsys.readouterr()
    api_key = field(out.out, "api_key")
    assert "cannot be shown again" in out.err
    principal = uow.auth.principal_for_key(hash_api_key(api_key))
    assert principal.role is Role.SYSTEM and principal.system_id == system_id


def test_key_rotation_and_revocation(uow, capsys):
    cli(uow, "create-user", "--name", "team-b", "--role", "viewer")
    first = capsys.readouterr().out
    cli(uow, "create-key", "--user", "team-b")
    second = capsys.readouterr().out
    assert cli(uow, "revoke-key", "--key-id", field(first, "key_id")) == 0
    assert uow.auth.principal_for_key(hash_api_key(field(first, "api_key"))) is None
    assert uow.auth.principal_for_key(hash_api_key(field(second, "api_key"))) is not None


@pytest.mark.parametrize(
    "args",
    [
        ["create-user", "--name", "x", "--role", "system"],  # system account without a system
        ["create-user", "--name", "x", "--role", "viewer", "--system-id", str(UUID(int=1))],
        ["create-key", "--user", "nobody"],
        ["revoke-key", "--key-id", str(UUID(int=1))],
        [
            "add-threshold",
            "--system-id",
            str(UUID(int=1)),
            "--metric",
            "m",
            "--type",
            "drift",
            "--direction",
            "above",
            "--limit",
            "0.3",
        ],
    ],
)
def test_invalid_requests_exit_2(uow, capsys, args):
    assert cli(uow, *args) == 2
    assert capsys.readouterr().err.startswith("error:")


def test_duplicate_user_is_refused(uow, capsys):
    cli(uow, "create-user", "--name", "dup", "--role", "admin")
    assert cli(uow, "create-user", "--name", "dup", "--role", "admin") == 2


def test_add_threshold(uow, capsys):
    system_id = uow.seed_system()
    code = cli(
        uow,
        "add-threshold",
        "--system-id",
        str(system_id),
        "--metric",
        "accuracy",
        "--type",
        "performance",
        "--direction",
        "below",
        "--limit",
        "0.70",
        "--valid-from",
        "2026-10-01T00:00:00Z",
    )
    assert code == 0
    (threshold,) = uow.thresholds.rows
    assert (threshold.direction, str(threshold.limit_value)) == ("below", "0.70")
    assert threshold.valid_from == datetime(2026, 10, 1, tzinfo=UTC)


def test_verify_ledger_exit_code(uow, capsys):
    chain = ChainLedger(uow.ledger, FixedClock(datetime(2026, 10, 1, tzinfo=UTC)))
    for _ in range(3):
        chain.append(None, entry_type="VIEW", actor_id=None, payload={})
    assert cli(uow, "verify-ledger") == 0
    assert "valid=True entries_checked=3" in capsys.readouterr().out
    uow.ledger.entries[1].payload = {"tampered": True}
    assert cli(uow, "verify-ledger") == 1
    assert "first_mismatch=2" in capsys.readouterr().out

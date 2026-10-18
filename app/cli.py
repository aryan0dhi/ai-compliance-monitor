"""Administration commands: `python -m app.cli <command> --help`.

Section 5 defines no endpoints for creating systems, accounts or thresholds (D-07), so operators
run these inside the API container, e.g.
    docker compose exec api python -m app.cli create-user --name team-b-viewer --role viewer
API keys are printed once and never stored in clear.
"""

import argparse
import sys
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID

from app.core.clock import get_clock
from app.db.uow import UnitOfWork, get_uow
from app.modules.auth.principal import Principal, Role
from app.modules.auth.service import AccountError, create_key, create_user, revoke_key
from app.modules.ledger.service import verify_ledger
from app.modules.metrics.models import MetricType
from app.modules.systems.service import SystemConfigError, create_system
from app.modules.thresholds.models import Direction
from app.modules.thresholds.service import ThresholdConfigError, add_threshold_version

# verify-ledger runs as an operator, outside any API caller's identity.
_OPERATOR = Principal(actor_id=UUID(int=0), role=Role.ADMIN)


def _aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp needs a UTC offset, e.g. 2026-10-18T00:00:00Z")
    return parsed


def _decimal(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(f"not a number: {value}") from exc
    if not parsed.is_finite():
        raise argparse.ArgumentTypeError("limit must be finite")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create-system", help="register a monitored AI system")
    p.add_argument("--name", required=True)
    p.add_argument("--id", type=UUID, help="fixed system id (default: random)")
    p.add_argument("--interval", type=int, default=300, help="sampling interval in seconds")
    p.add_argument("--gap-intervals", type=int, default=3, help="missed intervals = a gap")

    p = sub.add_parser("create-user", help="create an account and print its first API key")
    p.add_argument("--name", required=True)
    p.add_argument("--role", required=True, choices=[r.value for r in Role])
    p.add_argument("--system-id", type=UUID, help="required for --role system")

    p = sub.add_parser("create-key", help="issue another API key for an account")
    p.add_argument("--user", required=True, help="account name")

    p = sub.add_parser("revoke-key", help="revoke an API key")
    p.add_argument("--key-id", required=True, type=UUID)

    p = sub.add_parser("add-threshold", help="append a threshold version (REQ-03)")
    p.add_argument("--system-id", required=True, type=UUID)
    p.add_argument("--metric", required=True)
    p.add_argument("--type", required=True, choices=[t.value for t in MetricType])
    p.add_argument("--direction", required=True, choices=[d.value for d in Direction])
    p.add_argument("--limit", required=True, type=_decimal)
    p.add_argument("--valid-from", type=_aware, help="default: now")

    sub.add_parser("verify-ledger", help="re-verify the audit hash chain (exit 1 if broken)")
    return parser


def run(argv: Sequence[str], uow_factory=get_uow) -> int:
    args = build_parser().parse_args(argv)
    uow: UnitOfWork = uow_factory()
    try:
        if args.command == "create-system":
            system = create_system(
                uow,
                name=args.name,
                sampling_interval_s=args.interval,
                gap_missed_intervals=args.gap_intervals,
                system_id=args.id,
            )
            print(f"system_id: {system.id}")
        elif args.command in ("create-user", "create-key"):
            if args.command == "create-user":
                issued = create_user(
                    uow, name=args.name, role=Role(args.role), system_id=args.system_id
                )
            else:
                issued = create_key(uow, user_name=args.user)
            print(f"user_id: {issued.user_id}")
            print(f"key_id:  {issued.key_id}")
            print(f"api_key: {issued.api_key}")
            print("Store the key now; it cannot be shown again.", file=sys.stderr)
        elif args.command == "revoke-key":
            revoke_key(uow, key_id=args.key_id, clock=get_clock())
            print(f"revoked: {args.key_id}")
        elif args.command == "add-threshold":
            threshold = add_threshold_version(
                uow,
                system_id=args.system_id,
                metric_name=args.metric,
                metric_type=MetricType(args.type),
                direction=Direction(args.direction),
                limit_value=args.limit,
                valid_from=args.valid_from or get_clock().now(),
                changed_by=None,
            )
            print(f"threshold_version_id: {threshold.id}")
        elif args.command == "verify-ledger":
            result = verify_ledger(principal=_OPERATOR, uow=uow)
            print(
                f"valid={result.valid} entries_checked={result.entries_checked} "
                f"first_mismatch={result.first_mismatch} head_seq={result.head_seq} "
                f"head_hash={result.head_hash}"
            )
            return 0 if result.valid else 1
    except (AccountError, SystemConfigError, ThresholdConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))

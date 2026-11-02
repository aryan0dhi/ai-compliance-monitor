"""The shared fixtures file (docs/pod-interface/fixtures.json, B-20) as the contract suite sees it.

Standard library only: the suite stays black box. CONTRACT_FIXTURES points at another team's
file. Scenario steps carry a `phase`; a run executes those up to the target's /v1/meta phases and
lists the rest as deferred (never skipped).
"""

import json
import os
import re
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "docs" / "pod-interface" / "fixtures.json"
PHASES = (3, 4, 5)
_DURATION = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


def parse_duration(text: str) -> timedelta:
    """An ISO-8601 duration of days, hours, minutes and seconds, e.g. P10DT4H."""
    m = _DURATION.match(text)
    if not m or text.endswith("T") or text == "P":
        raise ValueError(f"not a duration: {text!r}")
    d, h, mi, s = (int(g or 0) for g in m.groups())
    return timedelta(days=d, hours=h, minutes=mi, seconds=s)


@dataclass(frozen=True)
class Step:
    id: str
    phase: int
    at: timedelta  # offset from the scenario's start on the service clock
    actor: str
    action: str
    args: dict[str, Any]
    expect: dict[str, Any]
    needs: tuple[str, ...]
    label: str | None


@dataclass(frozen=True)
class Scenario:
    name: str
    title: str
    system: str
    reqs: tuple[str, ...]
    steps: tuple[Step, ...]
    setup: dict[str, Any] | None  # {"scenario", "steps"}: steps of another scenario run first

    def split(self, phases: list[int], *, webhooks: bool) -> tuple[list[Step], list[Step]]:
        """(steps to run, deferred steps) for a target providing `phases`."""
        top = max(phases, default=0)
        run = [s for s in self.steps if s.phase <= top and (webhooks or "webhooks" not in s.needs)]
        return run, [s for s in self.steps if s not in run]


@dataclass(frozen=True)
class Fixtures:
    systems: dict[str, dict[str, Any]]  # by name
    scenarios: dict[str, Scenario]

    def threshold(self, system: str, *, metric: str | None = None, kind: str | None = None) -> dict:
        """The system's threshold with this metric name or metric_type."""
        for t in self.systems[system]["thresholds"]:
            if t["metric_name"] == metric or t["metric_type"] == kind:
                return t
        raise KeyError(f"{system} has no threshold for {metric or kind}")


def _step(raw: dict[str, Any]) -> Step:
    return Step(
        id=raw["id"],
        phase=raw["phase"],
        at=parse_duration(raw["at"]),
        actor=raw["actor"],
        action=raw["action"],
        args=raw.get("args", {}),
        expect=raw.get("expect", {}),
        needs=tuple(raw.get("needs", ())),
        label=raw.get("label"),
    )


def load_fixtures(path: str | Path | None = None) -> Fixtures:
    path = Path(path or os.environ.get("CONTRACT_FIXTURES") or DEFAULT_PATH)
    data = json.loads(path.read_text(encoding="utf-8"))
    return Fixtures(
        systems={s["name"]: s for s in data["systems"]},
        scenarios={
            name: Scenario(
                name=name,
                title=s["title"],
                system=s["system"],
                reqs=tuple(s["reqs"]),
                steps=tuple(_step(x) for x in s["steps"]),
                setup=s.get("setup"),
            )
            for name, s in data["scenarios"].items()
        },
    )

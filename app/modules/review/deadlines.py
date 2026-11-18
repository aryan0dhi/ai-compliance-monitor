"""Article 73 reporting deadlines (REQ-10, B-10; D-23).

`deadline_utc = A + days(tier)`, where `A` is the time of the first serious-tier disposition and
days are calendar days of exactly 24 h. A revision to a stricter tier can only bring the deadline
forward; a revision to a less strict tier needs a rationale and then moves the deadline to
`A + days(new tier)`. `A` itself never moves once set.

Pure functions, so the rules can be tested exhaustively without a database.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.modules.review.models import DispositionTier

# B-10 / Article 73(2)-(4).
DEADLINE_DAYS: dict[DispositionTier, int] = {
    DispositionTier.CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD: 2,
    DispositionTier.DEATH: 10,
    DispositionTier.OTHER_SERIOUS: 15,
}


class LooserTierNeedsRationale(Exception):
    """A revision to a less strict tier without a rationale (409, B-10)."""


def strictness(tier: DispositionTier) -> float:
    """Days the tier allows; fewer is stricter. NOT_SERIOUS has no deadline at all."""
    return DEADLINE_DAYS.get(tier, float("inf"))


def deadline_for(anchor: datetime, tier: DispositionTier) -> datetime | None:
    days = DEADLINE_DAYS.get(tier)
    return None if days is None else anchor + timedelta(days=days)


@dataclass(frozen=True)
class DeadlineState:
    """Where an event stands after its dispositions so far."""

    tier: DispositionTier | None = None
    # A: when a serious tier was first assigned. None until then.
    anchor: datetime | None = None
    deadline: datetime | None = None


def apply(
    state: DeadlineState, tier: DispositionTier, *, rationale: bool, at: datetime
) -> DeadlineState:
    """The state after one more disposition at `at`. Raises LooserTierNeedsRationale."""
    anchor = state.anchor
    if anchor is None and tier.is_serious:
        anchor = at
    # Only a serious tier has days, and A is set whenever one has been assigned.
    target = deadline_for(anchor, tier) if anchor is not None else None
    if state.tier is None or strictness(tier) < strictness(state.tier):
        # First disposition, or stricter: the deadline can only come forward.
        deadline = (
            target if state.deadline is None or target is None else min(state.deadline, target)
        )
    elif strictness(tier) > strictness(state.tier):
        if not rationale:
            raise LooserTierNeedsRationale(
                f"revising {state.tier} to the less strict {tier} needs a rationale"
            )
        deadline = target
    else:  # the same tier again
        deadline = state.deadline
    return DeadlineState(tier=tier, anchor=anchor, deadline=deadline)


def replay(history: Iterable[tuple[DispositionTier, bool, datetime]]) -> DeadlineState:
    """The state after (tier, has rationale, recorded_at) dispositions, oldest first."""
    state = DeadlineState()
    for tier, rationale, at in history:
        state = apply(state, tier, rationale=rationale, at=at)
    return state

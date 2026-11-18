"""POST /v1/events/{event_id}/disposition request body (pod interface DispositionIn)."""

from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

from app.modules.review.disposition import DispositionRequest, FieldConfirmation, has_text
from app.modules.review.models import DispositionTier, HarmFieldName, HarmFieldStatus

TEXT_MAX_LENGTH = 4000


class HarmFieldConfirmationIn(BaseModel):
    status: Literal["CONFIRMED", "NOT_APPLICABLE"]
    evidence: str | None = Field(None, max_length=TEXT_MAX_LENGTH)


class HarmFieldConfirmationsIn(BaseModel):
    """Any of the four Art. 3(49) fields; all four must end up confirmed or N/A (B-08)."""

    health_safety: HarmFieldConfirmationIn | None = None
    critical_infrastructure: HarmFieldConfirmationIn | None = None
    fundamental_rights: HarmFieldConfirmationIn | None = None
    property_environment: HarmFieldConfirmationIn | None = None


class DispositionIn(BaseModel):
    tier: DispositionTier
    rationale: str | None = Field(
        None,
        max_length=TEXT_MAX_LENGTH,
        description="Required (not blank) for NOT_SERIOUS (B-08).",
    )
    harm_fields: HarmFieldConfirmationsIn | None = None

    @model_validator(mode="after")
    def _not_serious_needs_rationale(self) -> Self:
        if self.tier is DispositionTier.NOT_SERIOUS and not has_text(self.rationale):
            raise ValueError("NOT_SERIOUS needs a non-empty rationale")
        return self

    def to_request(self) -> DispositionRequest:
        confirmations: dict[HarmFieldName, FieldConfirmation] = {}
        if self.harm_fields is not None:
            for name in HarmFieldName:
                given = getattr(self.harm_fields, name.value)
                if given is not None:
                    confirmations[name] = FieldConfirmation(
                        status=HarmFieldStatus(given.status), evidence=given.evidence
                    )
        return DispositionRequest(
            tier=self.tier, rationale=self.rationale, harm_fields=confirmations
        )

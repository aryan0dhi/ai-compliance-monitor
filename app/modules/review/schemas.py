"""POST /v1/events/{event_id}/disposition request body (pod interface DispositionIn)."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.config import JsonDict

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


# B-08 in the published schema, as the pod document states it (allOf/if/then), so clients and
# schema-driven tests know NOT_SERIOUS needs a rationale. The pattern adds D-22: not blank.
NOT_SERIOUS_NEEDS_RATIONALE: JsonDict = {
    "allOf": [
        {
            "if": {"properties": {"tier": {"const": DispositionTier.NOT_SERIOUS.value}}},
            "then": {
                "required": ["rationale"],
                "properties": {"rationale": {"type": "string", "minLength": 1, "pattern": r"\S"}},
            },
        }
    ]
}


class DispositionIn(BaseModel):
    model_config = ConfigDict(json_schema_extra=NOT_SERIOUS_NEEDS_RATIONALE)

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

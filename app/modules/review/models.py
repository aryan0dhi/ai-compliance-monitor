"""Review vocabulary shared by events, systems and the review workflow (REQ-07)."""

from enum import StrEnum


class HarmFieldName(StrEnum):
    """The four Art. 3(49) consequences a reviewer confirms for every flagged event (REQ-07)."""

    HEALTH_SAFETY = "health_safety"
    CRITICAL_INFRASTRUCTURE = "critical_infrastructure"
    FUNDAMENTAL_RIGHTS = "fundamental_rights"
    PROPERTY_ENVIRONMENT = "property_environment"

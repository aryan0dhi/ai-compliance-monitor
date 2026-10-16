"""POST /v1/metrics (REQ-01, REQ-03). HTTP mapping only; logic lives in metrics.service."""

from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.core.clock import Clock, get_clock
from app.db.uow import UnitOfWork, get_uow
from app.modules.auth.interface import Principal, get_principal
from app.modules.ledger.interface import LedgerWriter, get_ledger
from app.modules.metrics.schemas import MetricSampleIn, MetricSampleOut
from app.modules.metrics.service import (
    ConflictingDuplicateError,
    MetricTypeMismatchError,
    SystemScopeError,
    UnknownSystemError,
    ingest_metric,
)
from app.modules.notifications.interface import Notifier, get_notifier

router = APIRouter(tags=["metrics"])


@router.post(
    "/metrics",
    status_code=status.HTTP_201_CREATED,
    response_model=MetricSampleOut,
    response_model_exclude_none=True,
    responses={
        200: {"description": "TEMPORARY(D-04): identical sample already stored"},
        400: {"description": "Malformed or invalid body"},
        401: {"description": "Missing or invalid API key"},
        403: {"description": "API key is not scoped to this system_id"},
        409: {"description": "TEMPORARY(D-04): different sample already stored for this key"},
    },
)
def submit_metric(
    body: MetricSampleIn,
    response: Response,
    principal: Principal = Depends(get_principal),
    uow: UnitOfWork = Depends(get_uow),
    ledger: LedgerWriter = Depends(get_ledger),
    notifier: Notifier = Depends(get_notifier),
    clock: Clock = Depends(get_clock),
) -> MetricSampleOut:
    try:
        result = ingest_metric(
            body.to_submission(),
            principal=principal,
            uow=uow,
            ledger=ledger,
            notifier=notifier,
            clock=clock,
        )
    except SystemScopeError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except (UnknownSystemError, MetricTypeMismatchError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except ConflictingDuplicateError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    if not result.created:
        response.status_code = status.HTTP_200_OK  # TEMPORARY(D-04)
    return MetricSampleOut.from_result(result)

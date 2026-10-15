"""Shared error types."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class UnimplementedError(NotImplementedError):
    """Raised by integration points whose owning module has not been implemented yet.

    Never catch and ignore this: an unimplemented ledger or notification call must abort the
    surrounding transaction rather than let a state change commit without its audit entry or
    reviewer notification.
    """

    def __init__(self, feature: str, requirement: str, owner_module: str) -> None:
        self.feature = feature
        self.requirement = requirement
        self.owner_module = owner_module
        super().__init__(
            f"{feature} is not implemented yet ({requirement}; owner: app/modules/{owner_module})"
        )


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(UnimplementedError)
    async def _unimplemented(_: Request, exc: UnimplementedError) -> JSONResponse:
        return JSONResponse(status_code=501, content={"detail": str(exc)})

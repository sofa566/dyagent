from typing import Any

from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse

from src.core.logging import get_logger

logger = get_logger(__name__)


class APIException(HTTPException):
    def __init__(
        self,
        status_code: int,
        error: str,
        code: str | None = None,
        details: Any | None = None,
    ):
        super().__init__(status_code=status_code, detail={'error': error, 'code': code})
        self.error = error
        self.code = code or f'ERROR_{status_code}'
        self.details = details


async def api_exception_handler(request: Request, exc: APIException) -> JSONResponse:
    logger.error(
        'api_exception',
        path=request.url.path,
        method=request.method,
        status_code=exc.status_code,
        error=exc.error,
        code=exc.code,
    )
    content = {'error': exc.error, 'code': exc.code}
    if exc.details:
        content['details'] = exc.details
    return JSONResponse(status_code=exc.status_code, content=content)


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    logger.warning(
        'http_exception',
        path=request.url.path,
        method=request.method,
        status_code=exc.status_code,
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={'error': exc.detail, 'code': f'HTTP_{exc.status_code}'},
    )


async def general_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception(
        'unhandled_exception',
        path=request.url.path,
        method=request.method,
        error=str(exc),
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={'error': 'Internal server error', 'code': 'INTERNAL_ERROR'},
    )


def not_found_error(resource: str, identifier: str | None = None) -> APIException:
    msg = f'{resource} not found'
    if identifier:
        msg = f'{resource} "{identifier}" not found'
    return APIException(status_code=status.HTTP_404_NOT_FOUND, error=msg, code='NOT_FOUND')


def validation_error(message: str) -> APIException:
    return APIException(status_code=status.HTTP_400_BAD_REQUEST, error=message, code='VALIDATION_ERROR')


def forbidden_error(message: str = 'Permission denied') -> APIException:
    return APIException(status_code=status.HTTP_403_FORBIDDEN, error=message, code='FORBIDDEN')


def unauthorized_error(message: str = 'Authentication required') -> APIException:
    return APIException(status_code=status.HTTP_401_UNAUTHORIZED, error=message, code='UNAUTHORIZED')


def service_unavailable_error(message: str = 'Service temporarily unavailable') -> APIException:
    return APIException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, error=message, code='SERVICE_UNAVAILABLE')

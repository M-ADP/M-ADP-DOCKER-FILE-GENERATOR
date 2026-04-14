import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


class AppException(Exception):
    status_code: int = 500
    message: str = "알 수 없는 오류가 발생했습니다."

    def __init__(self, message: str | None = None) -> None:
        self.message = message or self.__class__.message
        super().__init__(self.message)


class FileTooLargeError(AppException):
    status_code = 413
    message = "파일 크기가 50MB를 초과했습니다."


class InvalidArchiveError(AppException):
    status_code = 400
    message = "압축 파일을 읽을 수 없습니다."


class NoSourceFilesError(AppException):
    status_code = 400
    message = "소스코드 파일을 찾을 수 없습니다."


class DockerfileGenerationError(AppException):
    status_code = 500
    message = "Dockerfile 생성에 실패했습니다."


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppException)
    async def app_exception_handler(
        request: Request, exc: AppException
    ) -> JSONResponse:
        logger.error(
            "AppException: %s | path=%s method=%s",
            exc.message,
            request.url.path,
            request.method,
            exc_info=exc,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={"message": exc.message},
        )

    @app.exception_handler(Exception)
    async def generic_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        logger.error("Unhandled exception", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={"message": "알 수 없는 오류가 발생했습니다."},
        )

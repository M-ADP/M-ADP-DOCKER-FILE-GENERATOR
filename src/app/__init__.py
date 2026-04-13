from fastapi import FastAPI

from src.core.exceptions import register_exception_handlers
from src.endpoint import router


def create_app() -> FastAPI:
    app = FastAPI(title="Dockerfile Generator")
    app.include_router(router)
    register_exception_handlers(app)
    return app

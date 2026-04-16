from pydantic import BaseModel


class ErrorResponse(BaseModel):
    message: str


class DockerfileResponse(BaseModel):
    dockerfile: str
    dockerignore: str
    port: int



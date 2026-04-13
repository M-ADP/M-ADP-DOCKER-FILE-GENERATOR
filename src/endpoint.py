from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import PlainTextResponse

from src.app.generate_dockerfile import GenerateDockerfileUseCase
from src.core.exceptions import FileTooLargeError

router = APIRouter()

MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB


@router.post("/generate", response_class=PlainTextResponse)
async def generate(
    source: UploadFile = File(...),
    use_case: GenerateDockerfileUseCase = Depends(GenerateDockerfileUseCase),
) -> PlainTextResponse:
    content = await source.read()

    if len(content) > MAX_FILE_SIZE:
        raise FileTooLargeError()

    dockerfile = await use_case(content)
    return PlainTextResponse(content=dockerfile, status_code=200)


@router.get("/", tags=["default"])
async def health():
    return {"status": "ok"}

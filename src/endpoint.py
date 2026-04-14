from fastapi import APIRouter, Depends, File, UploadFile

from src.app.generate_dockerfile import GenerateDockerfileUseCase
from src.common.schema import DockerfileResponse
from src.core.exceptions import FileTooLargeError

router = APIRouter()

MAX_FILE_SIZE = 50 * 1024 * 1024


@router.post("/generate", response_model=DockerfileResponse)
async def generate(
    source: UploadFile = File(...),
    use_case: GenerateDockerfileUseCase = Depends(GenerateDockerfileUseCase),
) -> DockerfileResponse:
    content = await source.read()

    if len(content) > MAX_FILE_SIZE:
        raise FileTooLargeError()

    dockerfile, port = await use_case(content)
    return DockerfileResponse(dockerfile=dockerfile, port=port)


@router.get("/", tags=["default"])
async def health():
    return {"status": "ok"}

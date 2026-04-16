from fastapi import APIRouter, Depends, File, Form, UploadFile

from src.app.feedback_dockerfile import FeedbackDockerfileUseCase
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

    dockerfile, dockerignore, port = await use_case(content)
    return DockerfileResponse(
        dockerfile=dockerfile, dockerignore=dockerignore, port=port
    )


@router.post("/feedback", response_model=DockerfileResponse)
async def feedback(
    source: UploadFile = File(...),
    dockerfile: str = Form(...),
    dockerignore: str = Form(...),
    feedback_text: str = Form(..., alias="feedback"),
    use_case: FeedbackDockerfileUseCase = Depends(FeedbackDockerfileUseCase),
) -> DockerfileResponse:
    content = await source.read()

    if len(content) > MAX_FILE_SIZE:
        raise FileTooLargeError()

    result, new_dockerignore, port = await use_case(
        content, dockerfile, dockerignore, feedback_text
    )
    return DockerfileResponse(
        dockerfile=result, dockerignore=new_dockerignore, port=port
    )


@router.get("/", tags=["default"])
async def health():
    return {"status": "ok"}

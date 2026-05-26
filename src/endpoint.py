from fastapi import APIRouter, Depends, File, UploadFile

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
    dockerfile: UploadFile = File(...),
    dockerignore: UploadFile = File(...),
    feedback: UploadFile = File(...),
    use_case: FeedbackDockerfileUseCase = Depends(FeedbackDockerfileUseCase),
) -> DockerfileResponse:
    source_bytes = await source.read()
    dockerfile_text = (await dockerfile.read()).decode(errors="replace")
    dockerignore_text = (await dockerignore.read()).decode(errors="replace")
    feedback_text = (await feedback.read()).decode(errors="replace")

    if len(source_bytes) > MAX_FILE_SIZE:
        raise FileTooLargeError()

    result, dockerignore_out, port = await use_case(
        source_bytes, dockerfile_text, dockerignore_text, feedback_text
    )
    return DockerfileResponse(
        dockerfile=result, dockerignore=dockerignore_out, port=port
    )


@router.get("/", tags=["default"])
async def health():
    return {"status": "ok"}

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from local_meeting_ai.api.dependencies import get_container
from local_meeting_ai.api.schemas import PostprocessOptions
from local_meeting_ai.bootstrap import Container
from local_meeting_ai.domain.errors import NotFoundError

router = APIRouter(prefix="/api/browser-capture", tags=["browser-capture"])
ContainerDependency = Annotated[Container, Depends(get_container)]


class BrowserCaptureStart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    meeting_id: int | None = Field(default=None, ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    profile_id: str = Field(default="default", min_length=1, max_length=40)
    language: str | None = Field(default=None, max_length=20)
    task: Literal["transcribe", "translate"] = "transcribe"
    allow_model_download: bool = False
    use_microphone: bool = True
    use_shared_tab: bool = False
    postprocess_options: PostprocessOptions = Field(default_factory=PostprocessOptions)


class BrowserCaptureStop(BaseModel):
    model_config = ConfigDict(extra="forbid")

    final_transcription: bool = True
    postprocess_options: PostprocessOptions = Field(default_factory=PostprocessOptions)


@router.post("/sessions", status_code=status.HTTP_201_CREATED)
async def start_browser_capture(
    payload: BrowserCaptureStart,
    container: ContainerDependency,
) -> dict[str, Any]:
    return await container.browser_capture_service.start(
        meeting_id=payload.meeting_id,
        title=payload.title,
        profile_id=payload.profile_id,
        language=payload.language,
        task=payload.task,
        allow_model_download=payload.allow_model_download,
        postprocess_options=payload.postprocess_options.model_dump(mode="json"),
        use_microphone=payload.use_microphone,
        use_shared_tab=payload.use_shared_tab,
    )


@router.get("/session")
async def active_browser_capture(
    container: ContainerDependency,
) -> dict[str, Any] | None:
    return await container.browser_capture_service.status()


@router.get("/sessions/{session_id}")
async def browser_capture_status(
    session_id: str,
    container: ContainerDependency,
) -> dict[str, Any]:
    result = await container.browser_capture_service.status(session_id)
    if result is None:
        raise NotFoundError("Browser recording session not found")
    return result


@router.post("/sessions/{session_id}/chunks")
async def upload_browser_audio_chunk(
    session_id: str,
    request: Request,
    container: ContainerDependency,
    sequence: Annotated[int, Header(alias="X-Audio-Sequence", ge=0)],
) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/octet-stream":
        return Response(
            content="Browser PCM chunks must use application/octet-stream",
            media_type="text/plain",
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        )
    pcm = await request.body()
    return await container.browser_capture_service.ingest(
        session_id,
        sequence=sequence,
        pcm_s16le=pcm,
    )


@router.post("/sessions/{session_id}/pause")
async def pause_browser_capture(
    session_id: str,
    container: ContainerDependency,
) -> dict[str, Any]:
    return await container.browser_capture_service.pause(session_id)


@router.post("/sessions/{session_id}/resume")
async def resume_browser_capture(
    session_id: str,
    container: ContainerDependency,
) -> dict[str, Any]:
    return await container.browser_capture_service.resume(session_id)


@router.post("/sessions/{session_id}/stop")
async def stop_browser_capture(
    session_id: str,
    payload: BrowserCaptureStop,
    container: ContainerDependency,
) -> dict[str, Any]:
    return await container.browser_capture_service.stop(
        session_id,
        final_transcription=payload.final_transcription,
        postprocess_options=payload.postprocess_options.model_dump(mode="json"),
    )

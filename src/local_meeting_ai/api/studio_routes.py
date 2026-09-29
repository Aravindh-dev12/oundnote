from __future__ import annotations

import asyncio
import mimetypes
import re
import tempfile
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import Headers, UploadFile

from local_meeting_ai.api.dependencies import get_container
from local_meeting_ai.api.schemas import JobResponse, MeetingResponse, TranscriptionResponse
from local_meeting_ai.bootstrap import Container
from local_meeting_ai.domain.entities import SegmentDraft
from local_meeting_ai.domain.enums import JobStatus, SourceType
from local_meeting_ai.domain.errors import CapabilityUnavailableError, ValidationError

router = APIRouter(prefix="/api/studio")
ContainerDependency = Annotated[Container, Depends(get_container)]
YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"}
YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
MAX_SECONDS, MAX_BYTES = 4 * 60 * 60, 750 * 1024 * 1024


class YouTubeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=8, max_length=2048)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    language: str | None = Field(default=None, max_length=20)
    profile_id: str = Field(default="default", max_length=40)
    allow_model_download: bool = False


def youtube_id(value: str) -> str:
    p = urlparse(value.strip())
    host = (p.hostname or "").lower()
    if p.scheme not in {"http", "https"} or (host not in YT_HOSTS and not host.endswith(".youtube.com")):
        raise ValidationError("Only http(s) YouTube URLs are supported")
    if host.endswith("youtu.be"):
        candidate = p.path.strip("/").split("/", 1)[0]
    else:
        candidate = parse_qs(p.query).get("v", [""])[0]
        parts = [x for x in p.path.split("/") if x]
        if not candidate and len(parts) >= 2 and parts[0] in {"shorts", "embed", "live"}:
            candidate = parts[1]
    if not YT_ID.fullmatch(candidate):
        raise ValidationError("The URL does not contain a valid 11-character YouTube video ID")
    return candidate


def video_info(url: str) -> dict[str, Any]:
    try:
        from yt_dlp import YoutubeDL
        with YoutubeDL({"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True, "cachedir": False}) as ydl:
            info = ydl.extract_info(url, download=False)
        return {
            "video_id": str(info.get("id") or youtube_id(url)),
            "title": str(info.get("title") or "YouTube video"),
            "uploader": str(info.get("uploader") or info.get("channel") or "Unknown channel"),
            "duration_seconds": int(info["duration"]) if info.get("duration") else None,
            "webpage_url": str(info.get("webpage_url") or url),
        }
    except ImportError:
        try:
            import httpx
            r = httpx.get("https://www.youtube.com/oembed", params={"url": url, "format": "json"}, timeout=12, follow_redirects=True, headers={"User-Agent": "Oundnote/0.7"})
            r.raise_for_status()
            p = r.json()
            return {"video_id": youtube_id(url), "title": str(p.get("title") or "YouTube video"), "uploader": str(p.get("author_name") or "Unknown channel"), "duration_seconds": None, "webpage_url": url}
        except Exception as e:
            raise CapabilityUnavailableError("Install the YouTube extra with: pip install -e '.[youtube]'") from e
    except Exception as e:
        raise ValidationError(f"Could not inspect this YouTube video: {e}") from e


def captions(video_id: str, language: str | None) -> tuple[list[dict[str, Any]], dict[str, Any], str | None]:
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        return [], {}, "youtube-transcript-api is not installed"
    try:
        api = YouTubeTranscriptApi()
        requested = language.strip() if language else ""
        if requested:
            fetched = api.fetch(video_id, languages=[requested])
            code = getattr(fetched, "language_code", requested)
            generated = bool(getattr(fetched, "is_generated", False))
            lang = code
        else:
            tracks = list(api.list(video_id))
            if not tracks:
                return [], {}, "No caption tracks are available"
            t = next((x for x in tracks if not bool(getattr(x, "is_generated", False))), tracks[0])
            fetched = t.fetch()
            code = str(getattr(t, "language_code", "") or "")
            generated = bool(getattr(t, "is_generated", False))
            lang = str(getattr(t, "language", "") or code)
        rows = [
            {"text": str(x.get("text") or "").strip(), "start": float(x.get("start") or 0), "duration": float(x.get("duration") or 0)}
            for x in fetched.to_raw_data()
            if str(x.get("text") or "").strip()
        ]
        return rows, {"language": lang, "language_code": code, "is_generated": generated}, None if rows else "The caption track is empty"
    except Exception as e:
        return [], {}, str(e)


def download_audio(url: str, target: Path) -> tuple[Path, dict[str, Any]]:
    try:
        from yt_dlp import YoutubeDL
    except ImportError as e:
        raise CapabilityUnavailableError("Install the YouTube extra with: pip install -e '.[youtube]'") from e
    target.mkdir(parents=True, exist_ok=True)
    def max_duration(info: dict[str, Any], *, incomplete: bool = False) -> str | None:
        del incomplete
        return "This video is longer than Oundnote's 4-hour YouTube limit" if info.get("duration") and float(info["duration"]) > MAX_SECONDS else None
    opts = {
        "quiet": True, "no_warnings": True, "noplaylist": True, "restrictfilenames": True,
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": str(target / "%(id)s.%(ext)s"),
        "max_filesize": MAX_BYTES, "cachedir": False, "match_filter": max_duration,
    }
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        files = [p for p in target.iterdir() if p.is_file()]
        if not files:
            raise RuntimeError("yt-dlp completed without producing audio")
        path = max(files, key=lambda p: p.stat().st_mtime_ns)
        if path.stat().st_size > MAX_BYTES:
            path.unlink(missing_ok=True)
            raise RuntimeError("Downloaded audio exceeds 750 MiB")
        return path, {
            "video_id": str(info.get("id") or youtube_id(url)),
            "title": str(info.get("title") or "YouTube video"),
            "uploader": str(info.get("uploader") or info.get("channel") or "Unknown channel"),
            "duration_seconds": int(info["duration"]) if info.get("duration") else None,
            "webpage_url": str(info.get("webpage_url") or url),
        }
    except Exception as e:
        raise ValidationError(f"Could not download this YouTube video: {e}") from e


async def wait_job(container: Container, job_id: str, timeout: float = 180) -> Any:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        job = container.jobs.get(job_id)
        if not job:
            raise ValidationError("The import job disappeared")
        if job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}:
            return job
        if asyncio.get_running_loop().time() >= deadline:
            raise ValidationError("YouTube media import timed out")
        await asyncio.sleep(.25)


@router.post("/youtube/inspect")
async def inspect(payload: YouTubeRequest) -> dict[str, Any]:
    vid = youtube_id(payload.url)
    info = await asyncio.to_thread(video_info, payload.url)
    rows, track, err = await asyncio.to_thread(captions, vid, None)
    return {
        "video": info,
        "captions": {
            "available": bool(rows), "language": track.get("language"),
            "language_code": track.get("language_code"), "is_generated": track.get("is_generated"),
            "segment_count": len(rows), "error": err,
        },
        "import_modes": {"captions": bool(rows), "audio_fallback": yt_dlp_available()},
    }


@router.post("/youtube/import")
async def import_youtube(payload: YouTubeRequest, container: ContainerDependency) -> dict[str, Any]:
    vid = youtube_id(payload.url)
    info = await asyncio.to_thread(video_info, payload.url)
    if info.get("duration_seconds") and int(info["duration_seconds"]) > MAX_SECONDS:
        raise ValidationError("This video is longer than Oundnote's 4-hour YouTube limit")
    title = (payload.title or info.get("title") or "YouTube capture").strip()[:200]
    rows, track, caption_error = await asyncio.to_thread(captions, vid, payload.language)

    if rows:
        meeting = container.meeting_service.create(
            title=title, description=f"YouTube source: {payload.url}",
            source_type=SourceType.IMPORTED,
            language=str(track.get("language_code") or payload.language or "") or None,
        )
        created = container.transcriptions.create(
            meeting_id=meeting.id, title=title, engine="youtube-captions",
            model="youtube-transcript-api",
            language=str(track.get("language_code") or payload.language or "") or None,
            settings={"source":"youtube","source_url":payload.url,"video_id":vid,
                      "caption_language":track.get("language_code"),
                      "caption_generated":bool(track.get("is_generated",False))},
        )
        segments = [
            SegmentDraft(
                index=i, start_ms=max(0, round(x["start"]*1000)),
                end_ms=max(round(x["start"]*1000), round((x["start"]+x["duration"])*1000)),
                text=x["text"], confidence=None,
                metadata={"source":"youtube","source_url":payload.url,"video_id":vid,"caption":True},
            )
            for i, x in enumerate(rows)
        ]
        completed = container.transcriptions.complete(
            created.id,
            language=str(track.get("language_code") or payload.language or "") or None,
            segments=segments,
        )
        now = container.meeting_service.get(meeting.id)
        container.meeting_service.update(meeting.id, {
            "started_at": now.created_at, "ended_at": now.created_at,
            "duration_ms": segments[-1].end_ms if segments else None, "status": "ready",
        })
        summary_job, summary_error = None, None
        try:
            _, job = await container.summary_service.start(completed.id)
            summary_job = JobResponse.model_validate(job).model_dump(mode="json")
        except Exception as e:
            summary_error = str(e)
        if container.rag_service.can_index_incrementally():
            try:
                await container.rag_service.start_rebuild(meeting_id=meeting.id, force=False)
            except Exception:
                pass
        return {
            "mode":"captions", "video":info,
            "meeting":MeetingResponse.model_validate(container.meeting_service.get(meeting.id)).model_dump(mode="json"),
            "transcription":TranscriptionResponse.model_validate(container.transcriptions.get(completed.id)).model_dump(mode="json"),
            "summary_job":summary_job, "summary_error":summary_error,
            "captions":{"language":track.get("language_code"),"is_generated":bool(track.get("is_generated",False)),"segment_count":len(rows)},
        }

    with tempfile.TemporaryDirectory(prefix="oundnote-youtube-") as temp:
        audio, resolved = await asyncio.to_thread(download_audio, payload.url, Path(temp))
        meeting = container.meeting_service.create(
            title=title, description=f"YouTube source: {payload.url}",
            source_type=SourceType.IMPORTED, language=payload.language,
        )
        try:
            ctype = mimetypes.guess_type(audio.name)[0] or "application/octet-stream"
            with audio.open("rb") as handle:
                upload = UploadFile(filename=audio.name, file=handle, headers=Headers({"content-type":ctype}))
                recording, job = await container.import_service.import_media(meeting.id, upload)
            done = await wait_job(container, job.uuid)
            if done.status != JobStatus.COMPLETED:
                raise ValidationError(done.error_text or "YouTube media import failed")
            transcription, transcription_job = await container.transcription_service.start(
                meeting.id, profile_id=payload.profile_id, language=payload.language, task=None,
                allow_model_download=payload.allow_model_download, title=title, postprocess=True,
                postprocess_options={"diarization":True,"summary":True},
            )
            return {
                "mode":"audio","video":resolved,
                "meeting":MeetingResponse.model_validate(container.meeting_service.get(meeting.id)).model_dump(mode="json"),
                "recording":{"id":recording.id,"original_filename":recording.original_filename,
                             "media_type":recording.media_type,"size_bytes":recording.size_bytes,
                             "metadata":{**recording.metadata,"youtube_source":payload.url}},
                "import_job":JobResponse.model_validate(done).model_dump(mode="json"),
                "transcription":TranscriptionResponse.model_validate(transcription).model_dump(mode="json"),
                "transcription_job":JobResponse.model_validate(transcription_job).model_dump(mode="json"),
                "caption_fallback_reason":caption_error,
            }
        except Exception:
            container.meeting_service.delete(meeting.id)
            raise


def yt_dlp_available() -> bool:
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        return False
    return True

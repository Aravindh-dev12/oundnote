from __future__ import annotations

import asyncio
import logging
import wave
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from local_meeting_ai.application.services import ImportService, MeetingService
from local_meeting_ai.application.transcription_service import TranscriptionService
from local_meeting_ai.domain.entities import (
    AudioCaptureSource,
    CapturedAudio,
    LiveCaptureSession,
    ModelProfile,
    SegmentDraft,
    TranscriptionEngineRequest,
)
from local_meeting_ai.domain.enums import MeetingStatus, SourceType
from local_meeting_ai.domain.errors import (
    NotFoundError,
    ValidationError,
)
from local_meeting_ai.domain.protocols import TranscriptionEngine
from local_meeting_ai.infrastructure.database.repositories import (
    MeetingRepository,
    SettingsRepository,
    TranscriptionRepository,
)
from local_meeting_ai.infrastructure.storage import MeetingStorage

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
CHANNELS = 1
SAMPLE_WIDTH = 2
MAX_CHUNK_BYTES = 512 * 1024
OVERLAP_SECONDS = 1.0


@dataclass(slots=True)
class _BrowserSession:
    session_id: str
    meeting_id: int
    meeting_uuid: str
    transcription_id: int
    title: str
    profile: ModelProfile
    language: str | None
    task: str
    allow_model_download: bool
    destination: Path
    source: AudioCaptureSource
    live_session: LiveCaptureSession
    writer: Any
    started_at: str
    postprocess_options: dict[str, Any]
    lock: asyncio.Lock
    segments: list[SegmentDraft]
    overlap_pcm: bytes = b""
    total_frames: int = 0
    committed_until_ms: int = 0
    last_sequence: int = -1
    paused: bool = False
    stopping: bool = False
    engine_ready: bool = False
    last_error: str | None = None


class BrowserCaptureService:
    """Live ASR for browser-delivered PCM chunks, using the existing Oundnote pipeline.

    The browser is responsible for permissioned audio capture and sends bounded,
    sequenced 16 kHz mono PCM chunks. The service writes the original WAV, emits
    provisional transcript segments while recording, and queues the existing final
    transcription/diarization/summary pipeline when recording stops.
    """

    def __init__(
        self,
        *,
        meetings: MeetingRepository,
        meeting_service: MeetingService,
        transcriptions: TranscriptionRepository,
        transcription_service: TranscriptionService,
        transcription_engine: TranscriptionEngine,
        storage: MeetingStorage,
        import_service: ImportService,
        preferences: SettingsRepository,
        live_assistant: Any,
        webhooks: Any,
    ) -> None:
        self.meetings = meetings
        self.meeting_service = meeting_service
        self.transcriptions = transcriptions
        self.transcription_service = transcription_service
        self.engine = transcription_engine
        self.storage = storage
        self.import_service = import_service
        self.preferences = preferences
        self.live_assistant = live_assistant
        self.webhooks = webhooks
        self._sessions: dict[str, _BrowserSession] = {}
        self._active_session_id: str | None = None
        self._state_lock = asyncio.Lock()

    async def start(
        self,
        *,
        meeting_id: int | None,
        title: str | None,
        profile_id: str,
        language: str | None,
        task: str,
        allow_model_download: bool,
        postprocess_options: dict[str, Any] | None = None,
        use_microphone: bool = True,
        use_shared_tab: bool = False,
    ) -> dict[str, Any]:
        if not use_microphone and not use_shared_tab:
            raise ValidationError("Select a microphone, shared-tab audio, or both")
        async with self._state_lock:
            active = self._sessions.get(self._active_session_id or "")
            if active is not None and not active.stopping:
                raise ValidationError("Another browser recording is already active")

            created_meeting = False
            destination: Path | None = None
            writer: Any = None
            transcription_id: int | None = None
            try:
                if meeting_id is None:
                    clean_title = (title or "").strip() or self.transcriptions.next_default_title()
                    meeting = self.meeting_service.create(
                        title=clean_title,
                        source_type=SourceType.MANUAL,
                        language=language,
                    )
                    created_meeting = True
                else:
                    meeting = self.meetings.get(meeting_id)
                    if meeting is None:
                        raise NotFoundError("Meeting not found")
                    clean_title = (title or "").strip() or meeting.title

                transcription, profile = self.transcription_service.begin_realtime(
                    meeting.id,
                    profile_id=profile_id,
                    language=language,
                    task=task,
                    allow_model_download=allow_model_download,
                    title=clean_title,
                )
                transcription_id = transcription.id
                await self.engine.prepare(
                    profile,
                    allow_model_download=allow_model_download,
                )

                destination = self.storage.new_live_capture_path(meeting.uuid)
                writer = wave.open(str(destination), "wb")
                writer.setnchannels(CHANNELS)
                writer.setsampwidth(SAMPLE_WIDTH)
                writer.setframerate(SAMPLE_RATE)

                source_names = []
                if use_microphone:
                    source_names.append("Browser microphone")
                if use_shared_tab:
                    source_names.append("Shared tab audio")
                source = AudioCaptureSource(
                    id="browser:audio",
                    name=" + ".join(source_names),
                    kind="microphone",
                    backend="browser",
                    host_api="Browser Media Capture",
                    channels=CHANNELS,
                    sample_rate=SAMPLE_RATE,
                    is_default=True,
                )
                session_id = str(uuid4())
                started_at = datetime.now(UTC).isoformat(timespec="milliseconds")
                self.meetings.update(
                    meeting.id,
                    {"started_at": started_at},
                )
                live_session = LiveCaptureSession(
                    session_id=session_id,
                    meeting_id=meeting.id,
                    transcription_id=transcription.id,
                    title=clean_title,
                    state="recording",
                    source=source,
                    sources=(source,),
                    source_levels={source.id: 0.0},
                    elapsed_ms=0,
                    level=0.0,
                    started_at=started_at,
                    profile_id=profile.id,
                    language=language,
                    realtime_status="listening",
                    realtime_message="Waiting for browser audio",
                    segment_count=0,
                )
                session = _BrowserSession(
                    session_id=session_id,
                    meeting_id=meeting.id,
                    meeting_uuid=meeting.uuid,
                    transcription_id=transcription.id,
                    title=clean_title,
                    profile=profile,
                    language=language,
                    task=task,
                    allow_model_download=allow_model_download,
                    destination=destination,
                    source=source,
                    live_session=live_session,
                    writer=writer,
                    started_at=started_at,
                    postprocess_options=_normalize_options(postprocess_options),
                    lock=asyncio.Lock(),
                    segments=[],
                    engine_ready=True,
                )
                self._sessions[session_id] = session
                self._active_session_id = session_id
                if self.webhooks is not None:
                    self.webhooks.publish_live_session("live.session.started", live_session)
                if self.live_assistant is not None:
                    self.live_assistant.session_started(live_session)
                return self._payload(session)
            except Exception:
                if writer is not None:
                    try:
                        writer.close()
                    except Exception:
                        logger.exception("Could not close a failed browser recording writer")
                if destination is not None:
                    destination.unlink(missing_ok=True)
                if transcription_id is not None:
                    self.transcriptions.set_status(transcription_id, "failed")
                if created_meeting:
                    self.meetings.delete(meeting.id)
                raise

    async def ingest(
        self,
        session_id: str,
        *,
        sequence: int,
        pcm_s16le: bytes,
    ) -> dict[str, Any]:
        session = self._require_session(session_id)
        if not pcm_s16le:
            raise ValidationError("The audio chunk is empty")
        if len(pcm_s16le) > MAX_CHUNK_BYTES:
            raise ValidationError("Audio chunks must be 512 KiB or smaller")
        if len(pcm_s16le) % SAMPLE_WIDTH:
            raise ValidationError("PCM audio must contain whole 16-bit samples")

        async with session.lock:
            if session.stopping:
                raise ValidationError("The browser recording is stopping")
            if session.paused:
                raise ValidationError("The browser recording is paused")
            if sequence <= session.last_sequence:
                return self._payload(session)
            if sequence != session.last_sequence + 1:
                raise ValidationError(
                    f"Audio chunk out of order: expected {session.last_sequence + 1}"
                )

            overlap_frames = len(session.overlap_pcm) // SAMPLE_WIDTH
            window = session.overlap_pcm + pcm_s16le
            start_frame = max(0, session.total_frames - overlap_frames)
            temp_path = self.storage.new_realtime_chunk_path(session.meeting_uuid)
            added_segments: list[SegmentDraft] = []
            try:
                await asyncio.to_thread(_write_pcm_wav, temp_path, window)
                if not session.engine_ready:
                    await self.engine.prepare(
                        session.profile,
                        allow_model_download=session.allow_model_download,
                    )
                    session.engine_ready = True

                request = TranscriptionEngineRequest(
                    audio_path=temp_path,
                    model=session.profile.model,
                    device=session.profile.device,
                    compute_type=session.profile.compute_type,
                    language=session.language,
                    task=session.task,
                    beam_size=min(session.profile.beam_size, 3),
                    vad_filter=True,
                    allow_model_download=session.allow_model_download,
                    engine=session.profile.engine,
                    device_index=session.profile.device_index,
                    cpu_threads=session.profile.cpu_threads,
                    num_workers=session.profile.num_workers,
                    vad_min_silence_ms=session.profile.vad_min_silence_ms,
                    word_timestamps=session.profile.word_timestamps,
                    condition_on_previous_text=session.profile.condition_on_previous_text,
                    keep_model_loaded=session.profile.keep_model_loaded,
                    provider_options=dict(session.profile.provider_options or {}),
                )
                result = await self.engine.transcribe(
                    request,
                    lambda _progress, _message: None,
                    lambda: session.stopping,
                    lambda _segment: None,
                )
                committed_text = " ".join(item.text for item in session.segments[-4:])
                window_start_ms = round(start_frame / SAMPLE_RATE * 1000)
                for segment in result.segments:
                    if not segment.text.strip():
                        continue
                    absolute_start = window_start_ms + segment.start_ms
                    absolute_end = window_start_ms + segment.end_ms
                    if absolute_end <= session.committed_until_ms - 100:
                        continue
                    text = _trim_duplicate_prefix(segment.text, committed_text)
                    if not text:
                        continue
                    absolute_start = max(
                        0,
                        absolute_start,
                        session.committed_until_ms - 100,
                    )
                    absolute_end = max(absolute_start, absolute_end)
                    draft = SegmentDraft(
                        index=len(session.segments),
                        start_ms=absolute_start,
                        end_ms=absolute_end,
                        text=text,
                        confidence=segment.confidence,
                        metadata={
                            **(segment.metadata or {}),
                            "realtime": True,
                            "browser_capture": True,
                            "sequence": sequence,
                        },
                    )
                    self.transcriptions.append_segment(
                        session.transcription_id,
                        draft,
                        is_final=False,
                    )
                    session.segments.append(draft)
                    added_segments.append(draft)
                    session.committed_until_ms = max(
                        session.committed_until_ms,
                        absolute_end,
                    )
                    committed_text = f"{committed_text} {text}".strip()

                # Commit PCM only after ASR succeeds, so retrying a failed sequence
                # never duplicates bytes in the recording.
                session.writer.writeframesraw(pcm_s16le)
                session.total_frames += len(pcm_s16le) // SAMPLE_WIDTH
                overlap_bytes = round(SAMPLE_RATE * OVERLAP_SECONDS) * SAMPLE_WIDTH
                session.overlap_pcm = (session.overlap_pcm + pcm_s16le)[-overlap_bytes:]
                session.last_sequence = sequence
                session.live_session = replace(
                    session.live_session,
                    elapsed_ms=round(session.total_frames / SAMPLE_RATE * 1000),
                    realtime_status="live" if added_segments else "listening",
                    realtime_message=(
                        f"{len(session.segments)} live segments"
                        if added_segments
                        else "Listening for speech"
                    ),
                    segment_count=len(session.segments),
                )
                if added_segments and self.webhooks is not None:
                    self.webhooks.publish_live_segments(
                        meeting_id=session.meeting_id,
                        transcription_id=session.transcription_id,
                        meeting_title=session.title,
                        segments=added_segments,
                        sequence=sequence,
                    )
                if added_segments and self.live_assistant is not None:
                    self.live_assistant.publish_segments(
                        session_id=session.session_id,
                        meeting_id=session.meeting_id,
                        transcription_id=session.transcription_id,
                        meeting_title=session.title,
                        segments=added_segments,
                        sequence=sequence,
                    )
                return self._payload(session, new_segments=len(added_segments))
            finally:
                temp_path.unlink(missing_ok=True)

    async def pause(self, session_id: str) -> dict[str, Any]:
        session = self._require_session(session_id)
        async with session.lock:
            if session.stopping:
                raise ValidationError("The browser recording is stopping")
            if session.paused:
                return self._payload(session)
            session.paused = True
            session.live_session = replace(
                session.live_session,
                state="paused",
                realtime_status="paused",
                realtime_message="Browser recording paused",
            )
            if self.webhooks is not None:
                self.webhooks.publish_live_session("live.session.paused", session.live_session)
            return self._payload(session)

    async def resume(self, session_id: str) -> dict[str, Any]:
        session = self._require_session(session_id)
        async with session.lock:
            if session.stopping:
                raise ValidationError("The browser recording is stopping")
            if not session.paused:
                return self._payload(session)
            session.paused = False
            session.live_session = replace(
                session.live_session,
                state="recording",
                realtime_status="listening",
                realtime_message="Listening for browser audio",
            )
            if self.webhooks is not None:
                self.webhooks.publish_live_session("live.session.resumed", session.live_session)
            return self._payload(session)

    async def status(self, session_id: str | None = None) -> dict[str, Any] | None:
        target = session_id or self._active_session_id
        if not target:
            return None
        session = self._sessions.get(target)
        if session is None:
            return None
        async with session.lock:
            return self._payload(session)

    async def stop(
        self,
        session_id: str,
        *,
        final_transcription: bool = True,
        postprocess_options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        session = self._require_session(session_id)
        async with session.lock:
            if session.stopping:
                raise ValidationError("The browser recording is already stopping")
            session.stopping = True
            session.writer.close()
            duration_ms = round(session.total_frames / SAMPLE_RATE * 1000)
            ended_at = datetime.now(UTC).isoformat(timespec="milliseconds")
            self.meetings.update(
                session.meeting_id,
                {"ended_at": ended_at, "duration_ms": duration_ms},
            )
            stopped = replace(
                session.live_session,
                state="stopped",
                elapsed_ms=duration_ms,
                level=0.0,
                realtime_status="finalizing",
                realtime_message="Finalizing the complete recording",
                segment_count=len(session.segments),
                source_levels={session.source.id: 0.0},
            )
            session.live_session = stopped
        try:
            if self.webhooks is not None:
                self.webhooks.publish_live_session("live.session.stopped", stopped)
            if self.live_assistant is not None:
                self.live_assistant.session_stopped(stopped)
            captured = CapturedAudio(
                path=session.destination,
                source=session.source,
                duration_ms=duration_ms,
                sample_rate=SAMPLE_RATE,
                channels=CHANNELS,
                sources=(session.source,),
            )
            recording, import_job = await self.import_service.register_capture(
                session.meeting_id,
                captured,
            )
            transcription, transcription_job = await self.transcription_service.finalize_realtime(
                session.transcription_id,
                recording.id,
                profile_id=session.profile.id,
                language=session.language,
                task=session.task,
                allow_model_download=session.allow_model_download,
                run_final_pass=final_transcription,
                postprocess_options=(
                    postprocess_options
                    if postprocess_options is not None
                    else session.postprocess_options
                ),
            )
            return {
                "session": self._payload(session),
                "recording": recording,
                "import_job": import_job,
                "transcription": transcription,
                "transcription_job": transcription_job,
            }
        except Exception:
            self.transcriptions.set_status(session.transcription_id, "failed")
            self.meetings.set_status(session.meeting_id, MeetingStatus.FAILED)
            raise
        finally:
            async with self._state_lock:
                if self._active_session_id == session.session_id:
                    self._active_session_id = None

    async def shutdown(self) -> None:
        sessions = list(self._sessions.values())
        for session in sessions:
            if session.stopping:
                continue
            async with session.lock:
                session.stopping = True
                try:
                    session.writer.close()
                except Exception:
                    logger.exception("Could not close interrupted browser recording")
                self.transcriptions.set_status(session.transcription_id, "failed")
                self.meetings.set_status(session.meeting_id, MeetingStatus.FAILED)
                if self.live_assistant is not None:
                    self.live_assistant.session_stopped(
                        replace(
                            session.live_session,
                            state="stopped",
                            realtime_status="interrupted",
                        )
                    )
        self._active_session_id = None

    def _require_session(self, session_id: str) -> _BrowserSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise NotFoundError("Browser recording session not found")
        return session

    @staticmethod
    def _payload(
        session: _BrowserSession,
        *,
        new_segments: int = 0,
    ) -> dict[str, Any]:
        return {
            "session_id": session.session_id,
            "meeting_id": session.meeting_id,
            "transcription_id": session.transcription_id,
            "title": session.title,
            "state": (
                "paused"
                if session.paused
                else ("stopping" if session.stopping else "recording")
            ),
            "elapsed_ms": round(session.total_frames / SAMPLE_RATE * 1000),
            "segment_count": len(session.segments),
            "last_sequence": session.last_sequence,
            "new_segments": new_segments,
            "realtime_status": session.live_session.realtime_status,
            "realtime_message": session.live_session.realtime_message,
            "last_error": session.last_error,
        }


def _write_pcm_wav(path: Path, pcm: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(CHANNELS)
        writer.setsampwidth(SAMPLE_WIDTH)
        writer.setframerate(SAMPLE_RATE)
        writer.writeframes(pcm)


def _trim_duplicate_prefix(text: str, committed_text: str) -> str:
    words = text.strip().split()
    previous = committed_text.strip().split()
    if not words or not previous:
        return " ".join(words)

    def normalized(word: str) -> str:
        return word.casefold().strip(".,!?;:()[]{}\"'")

    current_normalized = [normalized(word) for word in words]
    previous_normalized = [normalized(word) for word in previous]
    maximum = min(len(current_normalized), len(previous_normalized), 16)
    for size in range(maximum, 0, -1):
        if previous_normalized[-size:] == current_normalized[:size]:
            return " ".join(words[size:])
    return " ".join(words)


def _normalize_options(value: dict[str, Any] | None) -> dict[str, Any]:
    source = value or {}
    return {
        "diarization": bool(source.get("diarization", True)),
        "speaker_count": source.get("speaker_count"),
        "summary": bool(source.get("summary", True)),
        "summary_template_id": source.get("summary_template_id"),
    }

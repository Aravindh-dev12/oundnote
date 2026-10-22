from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import Any

from local_meeting_ai.application.transcription_config import faster_whisper_config
from local_meeting_ai.domain.entities import (
    Job,
    ModelProfile,
    Transcription,
    TranscriptionEngineRequest,
    TranscriptionResult,
)
from local_meeting_ai.domain.enums import JobStatus, JobType, MeetingStatus
from local_meeting_ai.domain.errors import (
    ConfirmationRequiredError,
    JobCancelledError,
    NotFoundError,
    ValidationError,
)
from local_meeting_ai.infrastructure.database.repositories import (
    JobRepository,
    MeetingRepository,
    RecordingRepository,
    SettingsRepository,
    TranscriptionRepository,
)
from local_meeting_ai.infrastructure.jobs import JobContext, LocalJobQueue
from local_meeting_ai.infrastructure.storage import MeetingStorage
from local_meeting_ai.plugins.contracts import HookContext

logger = logging.getLogger(__name__)


class TranscriptionService:
    """Coordinates ASR jobs, live transcript versions, and transcript editing.

    This module is the application boundary between the job queue, the
    transcription engine, persistent meeting storage, and the API. Heavy model
    work is always executed in the queue (or the capture service's real-time
    worker), never during the HTTP request that creates a transcription.
    """

    def __init__(
        self,
        *,
        meetings: MeetingRepository,
        recordings: RecordingRepository,
        transcriptions: TranscriptionRepository,
        jobs: JobRepository,
        storage: MeetingStorage,
        normalizer: Any,
        engine: Any,
        profiles: Any,
        preferences: SettingsRepository,
        queue: LocalJobQueue,
    ) -> None:
        self.meetings = meetings
        self.recordings = recordings
        self.transcriptions = transcriptions
        self.jobs = jobs
        self.storage = storage
        self.normalizer = normalizer
        self.engine = engine
        self.profiles = profiles
        self.preferences = preferences
        self.queue = queue

    def capability(self) -> dict[str, Any]:
        return self.engine.capability()

    def model_profiles(self) -> list[ModelProfile]:
        return self.profiles.list()

    async def preload_default(self) -> None:
        """Preload the configured default model only when explicitly enabled."""
        config = faster_whisper_config(self.preferences.get_all())
        if not bool(config.get("preload_on_start", False)):
            return
        profile = self.profiles.resolve("default", purpose="final")
        # Never download gigabytes of model weights implicitly at startup.
        if not profile.installed:
            logger.info("Skipping ASR preload because model %s is not installed", profile.model)
            return
        await self.engine.prepare(profile, allow_model_download=False)

    def begin_realtime(
        self,
        meeting_id: int,
        *,
        profile_id: str = "default",
        language: str | None = None,
        task: str = "transcribe",
        allow_model_download: bool = False,
        title: str | None = None,
    ) -> tuple[Transcription, ModelProfile]:
        meeting = self.meetings.get(meeting_id)
        if meeting is None:
            raise NotFoundError("Meeting not found")
        profile = self.profiles.resolve(profile_id, purpose="live")
        self._require_download_confirmation(profile, allow_model_download)
        clean_task = self._resolve_task(task)
        prefs = self.preferences.get_all()
        asr_config = faster_whisper_config(prefs)
        resolved_language = language.strip() if language and language.strip() else asr_config.get("language")
        clean_title = (title or "").strip() or self.transcriptions.next_default_title()
        transcription = self.transcriptions.create(
            meeting_id=meeting_id,
            title=clean_title,
            engine=profile.engine,
            model=profile.model,
            language=resolved_language,
            settings=self._transcription_settings(
                profile,
                language=resolved_language,
                task=clean_task,
                allow_model_download=allow_model_download,
            ),
        )
        self.transcriptions.mark_running(transcription.id)
        refreshed = self.transcriptions.get(transcription.id)
        if refreshed is None:
            raise NotFoundError("Transcription could not be created")
        return refreshed, profile

    async def start(
        self,
        meeting_id: int,
        *,
        profile_id: str = "default",
        language: str | None = None,
        task: str | None = None,
        allow_model_download: bool = False,
        title: str | None = None,
        postprocess: bool = False,
        postprocess_options: dict[str, Any] | Any | None = None,
        **_: Any,
    ) -> tuple[Transcription, Job]:
        meeting = self.meetings.get(meeting_id)
        if meeting is None:
            raise NotFoundError("Meeting not found")
        recording = self.recordings.latest_for_role(meeting_id, "original")
        if recording is None:
            raise ValidationError("Import or record audio before starting transcription")

        profile = self.profiles.resolve(profile_id, purpose="final")
        self._require_download_confirmation(profile, allow_model_download)
        prefs = self.preferences.get_all()
        asr_config = faster_whisper_config(prefs)
        clean_language = language.strip() if language and language.strip() else asr_config.get("language")
        clean_task = self._resolve_task(task or str(asr_config.get("task") or "transcribe"))
        clean_title = (title or "").strip() or self.transcriptions.next_default_title()
        options = self._normalize_postprocess_options(postprocess_options)
        transcription = self.transcriptions.create(
            meeting_id=meeting_id,
            title=clean_title,
            engine=profile.engine,
            model=profile.model,
            language=clean_language,
            settings=self._transcription_settings(
                profile,
                language=clean_language,
                task=clean_task,
                allow_model_download=allow_model_download,
            ),
        )
        job_payload: dict[str, Any] = {
            "recording_id": recording.id,
            "transcription_id": transcription.id,
            "profile_id": profile.id,
            "language": clean_language,
            "task": clean_task,
            "allow_model_download": bool(allow_model_download),
            "postprocess": bool(postprocess),
            "postprocess_options": options,
            "run_final_pass": True,
        }
        job = self.jobs.create(
            meeting_id=meeting_id,
            job_type=JobType.TRANSCRIBE,
            payload=job_payload,
            message="Waiting to transcribe the recording",
        )
        await self.queue.submit(job.uuid)
        refreshed = self.transcriptions.get(transcription.id)
        if refreshed is None:
            raise NotFoundError("Transcription could not be created")
        return refreshed, job

    async def finalize_realtime(
        self,
        transcription_id: int,
        recording_id: int,
        *,
        profile_id: str = "default",
        language: str | None = None,
        task: str = "transcribe",
        allow_model_download: bool = False,
        run_final_pass: bool = True,
        postprocess_options: dict[str, Any] | Any | None = None,
    ) -> tuple[Transcription, Job]:
        transcription = self.transcriptions.get(transcription_id)
        if transcription is None:
            raise NotFoundError("Transcription not found")
        recording = self.recordings.get(recording_id)
        if recording is None or recording.meeting_id != transcription.meeting_id:
            raise NotFoundError("Recording not found")
        profile = self.profiles.resolve(profile_id, purpose="final" if run_final_pass else "live")
        if run_final_pass:
            self._require_download_confirmation(profile, allow_model_download)
        options = self._normalize_postprocess_options(postprocess_options)
        job = self.jobs.create(
            meeting_id=transcription.meeting_id,
            job_type=JobType.TRANSCRIBE,
            payload={
                "recording_id": recording_id,
                "transcription_id": transcription_id,
                "profile_id": profile.id,
                "language": language,
                "task": self._resolve_task(task),
                "allow_model_download": bool(allow_model_download),
                "postprocess": True,
                "postprocess_options": options,
                "run_final_pass": bool(run_final_pass),
                "skip_final_pass": not bool(run_final_pass),
            },
            message=(
                "Refining the complete recording"
                if run_final_pass
                else "Finishing the live transcript"
            ),
        )
        await self.queue.submit(job.uuid)
        refreshed = self.transcriptions.get(transcription_id)
        if refreshed is None:
            raise NotFoundError("Transcription not found")
        return refreshed, job

    async def process(self, job: Job, context: JobContext) -> dict[str, Any]:
        transcription_id = self._int_payload(job.payload, "transcription_id")
        transcription = self.transcriptions.get(transcription_id)
        if transcription is None:
            raise NotFoundError("Transcription not found")
        if job.payload.get("run_final_pass") is False:
            await context.update(0.5, "Keeping the live transcript")
            await context.raise_if_cancelled()
            completed = self.transcriptions.complete_realtime(transcription_id)
            if completed is None:
                raise NotFoundError("Transcription not found")
            segment_count = len(self.transcriptions.segments(transcription_id))
            self.meetings.update(
                completed.meeting_id,
                {"status": MeetingStatus.READY.value},
            )
            return {
                "transcription_id": transcription_id,
                "segment_count": segment_count,
                "language": completed.language,
                "live_transcript_kept": True,
            }

        recording_id = self._int_payload(job.payload, "recording_id")
        recording = self.recordings.get(recording_id)
        if recording is None or recording.meeting_id != transcription.meeting_id:
            raise NotFoundError("Recording not found")
        meeting = self.meetings.get(transcription.meeting_id)
        if meeting is None:
            raise NotFoundError("Meeting not found")
        profile_id = str(job.payload.get("profile_id") or "default")
        profile = self.profiles.resolve(profile_id, purpose="final")
        allow_model_download = bool(job.payload.get("allow_model_download", False))
        self._require_download_confirmation(profile, allow_model_download)
        language_value = job.payload.get("language")
        language = str(language_value).strip() if isinstance(language_value, str) and language_value.strip() else None
        asr_config = faster_whisper_config(self.preferences.get_all())
        if language is None:
            configured_language = asr_config.get("language")
            language = str(configured_language).strip() if configured_language else None
        task = self._resolve_task(str(job.payload.get("task") or asr_config.get("task") or "transcribe"))

        normalized_path: Path | None = None
        try:
            await context.update(0.03, "Preparing the transcription engine")
            await context.raise_if_cancelled()
            await self.engine.prepare(profile, allow_model_download=allow_model_download)
            await context.update(0.08, "Preparing audio for speech recognition")
            await context.raise_if_cancelled()
            normalized_path = self.storage.new_normalized_audio_path(meeting.uuid)
            probe = await self.normalizer.normalize_for_transcription(
                Path(recording.local_path),
                normalized_path,
                sample_rate=16000,
                channels=1,
                is_cancelled=lambda: self._cancelled(job.uuid),
            )
            await context.raise_if_cancelled()
            self._register_normalized_recording(
                meeting_id=meeting.id,
                path=normalized_path,
                original_filename=recording.original_filename,
                duration_ms=probe.duration_ms,
                sample_rate=probe.sample_rate,
                channels=probe.channels,
                metadata=probe.metadata,
            )
            await context.update(0.15, "Transcribing speech")
            request = TranscriptionEngineRequest(
                audio_path=normalized_path,
                model=profile.model,
                device=profile.device,
                compute_type=profile.compute_type,
                language=language,
                task=task,
                beam_size=profile.beam_size,
                vad_filter=profile.vad_filter,
                allow_model_download=allow_model_download,
                engine=profile.engine,
                device_index=profile.device_index,
                cpu_threads=profile.cpu_threads,
                num_workers=profile.num_workers,
                vad_min_silence_ms=profile.vad_min_silence_ms,
                word_timestamps=profile.word_timestamps,
                condition_on_previous_text=profile.condition_on_previous_text,
                keep_model_loaded=profile.keep_model_loaded,
                provider_options=dict(profile.provider_options or {}),
            )

            def report_progress(progress: float, message: str) -> None:
                bounded = max(0.0, min(1.0, float(progress)))
                self.jobs.update_progress(
                    job.uuid,
                    0.15 + bounded * 0.78,
                    message[:300] or "Transcribing speech",
                )

            def report_segment(segment: Any) -> None:
                if not str(getattr(segment, "text", "")).strip():
                    return
                self.transcriptions.append_segment(transcription_id, segment, is_final=False)

            result = await self.engine.transcribe(
                request,
                report_progress,
                lambda: self._cancelled(job.uuid),
                report_segment,
            )
            await context.raise_if_cancelled()
            completed = self.transcriptions.complete(
                transcription_id,
                language=result.language or language,
                segments=result.segments,
            )
            if completed is None:
                raise NotFoundError("Transcription not found")

            speaker_count = len({turn.speaker for turn in result.speaker_turns})
            assigned_segments = 0
            if result.speaker_turns:
                assigned_segments = self.transcriptions.assign_diarization(
                    meeting_id=meeting.id,
                    transcription_id=transcription_id,
                    diarization=result.speaker_turns,
                    minimum_overlap_ratio=0.1,
                )
            duration_ms = result.duration_ms or probe.duration_ms
            self.meetings.update(
                meeting.id,
                {
                    "status": MeetingStatus.READY.value,
                    "duration_ms": duration_ms,
                },
            )
            await context.update(0.98, "Saving transcript")
            return {
                "transcription_id": transcription_id,
                "segment_count": len(result.segments),
                "language": result.language or language,
                "language_probability": result.language_probability,
                "duration_ms": duration_ms,
                "speaker_count": speaker_count,
                "assigned_segments": assigned_segments,
            }
        except JobCancelledError:
            self.transcriptions.set_status(transcription_id, "cancelled")
            raise
        except Exception:
            self.transcriptions.set_status(transcription_id, "failed")
            self.meetings.set_status(transcription.meeting_id, MeetingStatus.FAILED)
            raise
        finally:
            # Normalized audio is retained as a meeting recording for replay and
            # export when it has been registered successfully. A failed normalize
            # that never produced a recording is removed.
            if normalized_path is not None and not self._is_registered_path(normalized_path):
                normalized_path.unlink(missing_ok=True)

    def list(self, meeting_id: int) -> list[Transcription]:
        return self.transcriptions.list_for_meeting(meeting_id)

    def get(self, transcription_id: int) -> tuple[Transcription, list[Any]]:
        transcription = self.transcriptions.get(transcription_id)
        if transcription is None:
            raise NotFoundError("Transcription not found")
        return transcription, self.transcriptions.segments(transcription_id)

    def update_title(self, transcription_id: int, title: str) -> Transcription:
        clean = title.strip()
        if not clean:
            raise ValidationError("A transcription title is required")
        updated = self.transcriptions.update_title(transcription_id, clean)
        if updated is None:
            raise NotFoundError("Transcription not found")
        return updated

    def update_segment(self, segment_id: int, text: str) -> Any:
        clean = text.strip()
        if not clean:
            raise ValidationError("Transcript text cannot be empty")
        updated = self.transcriptions.update_segment(segment_id, clean)
        if updated is None:
            raise NotFoundError("Transcript segment not found")
        return updated

    def activate(self, transcription_id: int) -> Transcription:
        updated = self.transcriptions.activate(transcription_id)
        if updated is None:
            existing = self.transcriptions.get(transcription_id)
            if existing is None:
                raise NotFoundError("Transcription not found")
            raise ValidationError("Only a completed transcription can be activated")
        return updated

    def find_replace(
        self,
        transcription_id: int,
        *,
        find: str,
        replacement: str,
        case_sensitive: bool = False,
    ) -> int:
        if not find:
            raise ValidationError("Find text cannot be empty")
        if replacement is None:
            replacement = ""
        if self.transcriptions.get(transcription_id) is None:
            raise NotFoundError("Transcription not found")
        return self.transcriptions.find_replace(
            transcription_id,
            find=find,
            replacement=replacement,
            case_sensitive=case_sensitive,
        )

    @staticmethod
    def _int_payload(payload: dict[str, Any], key: str) -> int:
        value = payload.get(key)
        if not isinstance(value, int) or value < 1:
            raise ValidationError(f"Transcription job is missing its {key.replace('_', ' ')}")
        return value

    @staticmethod
    def _resolve_task(task: str | None) -> str:
        clean = str(task or "transcribe").strip().lower()
        if clean not in {"transcribe", "translate"}:
            raise ValidationError("Task must be transcribe or translate")
        return clean

    @staticmethod
    def _normalize_postprocess_options(value: dict[str, Any] | Any | None) -> dict[str, Any]:
        if value is None:
            source: dict[str, Any] = {}
        elif isinstance(value, dict):
            source = value
        else:
            model_dump = getattr(value, "model_dump", None)
            if callable(model_dump):
                try:
                    dumped = model_dump(mode="json")
                except TypeError:
                    dumped = model_dump()
                source = dumped if isinstance(dumped, dict) else {}
            else:
                source = {}
        # Keep the queued job payload deterministic. It is persisted and is also
        # used by recovery, the final-processing pipeline, and the public job API.
        return {
            "diarization": bool(source.get("diarization", True)),
            "speaker_count": source.get("speaker_count"),
            "summary": bool(source.get("summary", True)),
            "summary_template_id": source.get("summary_template_id"),
        }

    @staticmethod
    def _require_download_confirmation(profile: ModelProfile, allow_model_download: bool) -> None:
        if not profile.installed and not allow_model_download:
            raise ConfirmationRequiredError(
                f"Confirm the model download for {profile.display_name} before continuing"
            )

    @staticmethod
    def _transcription_settings(
        profile: ModelProfile,
        *,
        language: str | None,
        task: str,
        allow_model_download: bool,
    ) -> dict[str, Any]:
        return {
            "profile_id": profile.id,
            "engine": profile.engine,
            "model": profile.model,
            "device": profile.device,
            "compute_type": profile.compute_type,
            "beam_size": profile.beam_size,
            "vad_filter": profile.vad_filter,
            "language": language,
            "task": task,
            "allow_model_download": bool(allow_model_download),
            "device_index": profile.device_index,
            "cpu_threads": profile.cpu_threads,
            "num_workers": profile.num_workers,
            "vad_min_silence_ms": profile.vad_min_silence_ms,
            "word_timestamps": profile.word_timestamps,
            "condition_on_previous_text": profile.condition_on_previous_text,
            "keep_model_loaded": profile.keep_model_loaded,
            "provider_options": dict(profile.provider_options or {}),
        }

    def _cancelled(self, job_uuid: str) -> bool:
        job = self.jobs.get(job_uuid)
        return job is None or job.cancel_requested

    def _register_normalized_recording(
        self,
        *,
        meeting_id: int,
        path: Path,
        original_filename: str,
        duration_ms: int | None,
        sample_rate: int | None,
        channels: int | None,
        metadata: dict[str, Any],
    ) -> None:
        meeting = self.meetings.get(meeting_id)
        if meeting is None:
            raise NotFoundError("Meeting not found")
        checksum = _sha256_path(path)
        self.recordings.create(
            meeting_id=meeting_id,
            role="normalized",
            local_path=str(path.resolve()),
            original_filename=Path(original_filename or "recording").stem + ".wav",
            media_type="audio/wav",
            size_bytes=path.stat().st_size,
            sha256=checksum,
            metadata={
                **(metadata if isinstance(metadata, dict) else {}),
                "duration_ms": duration_ms,
                "sample_rate": sample_rate,
                "channels": channels,
            },
        )

    def _is_registered_path(self, path: Path) -> bool:
        target = str(path.resolve())
        for meeting in self.meetings.list(limit=1000):
            if any(record.local_path == target for record in self.recordings.list_for_meeting(meeting.id)):
                return True
        return False


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_obj:
        while chunk := file_obj.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()

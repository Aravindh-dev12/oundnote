from __future__ import annotations

import asyncio
import io
import shutil
import time
import wave
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from local_meeting_ai.api.app import create_app
from local_meeting_ai.config import AppSettings
from local_meeting_ai.domain.entities import (
    DiarizationSegment,
    MediaProbe,
    SegmentDraft,
    TranscriptionEngineRequest,
    TranscriptionResult,
)
from local_meeting_ai.domain.protocols import (
    CancellationCheck,
    ProgressReporter,
    SegmentReporter,
)


class FakeNormalizer:
    async def normalize_for_transcription(
        self,
        source: Path,
        destination: Path,
        *,
        sample_rate: int = 16000,
        channels: int = 1,
        is_cancelled: CancellationCheck | None = None,
    ) -> MediaProbe:
        if is_cancelled and is_cancelled():
            raise RuntimeError("cancelled")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        await asyncio.sleep(0)
        return MediaProbe(
            format_name="wav",
            duration_ms=100,
            size_bytes=destination.stat().st_size,
            sample_rate=sample_rate,
            channels=channels,
            has_audio=True,
            has_video=False,
            metadata={"format": "wav"},
        )


class FakeTranscriptionEngine:
    name = "fake-whisper"

    def capability(self) -> dict[str, Any]:
        return {
            "engine": self.name,
            "available": True,
            "cuda_available": False,
            "installed_models": ["small"],
        }

    async def prepare(
        self,
        profile: Any,
        *,
        allow_model_download: bool,
    ) -> None:
        del profile, allow_model_download
        await asyncio.sleep(0)

    async def transcribe(
        self,
        request: TranscriptionEngineRequest,
        progress: ProgressReporter,
        is_cancelled: CancellationCheck,
        segment_ready: SegmentReporter,
    ) -> TranscriptionResult:
        assert request.audio_path.suffix == ".wav"
        assert request.compute_type == "int8"
        drafts = [
            SegmentDraft(
                index=0,
                start_ms=0,
                end_ms=1200,
                text="Welcome to the product review.",
                confidence=0.94,
            ),
            SegmentDraft(
                index=1,
                start_ms=1200,
                end_ms=2800,
                text="We approved the product launch.",
                confidence=0.91,
            ),
        ]
        for index, segment in enumerate(drafts):
            assert not is_cancelled()
            segment_ready(segment)
            progress((index + 1) / len(drafts), f"Segment {index + 1}")
            await asyncio.sleep(0)
        return TranscriptionResult(
            language="en",
            language_probability=0.99,
            duration_ms=2800,
            segments=drafts,
        )

    def shutdown(self) -> None:
        return None

    def unload(self) -> None:
        return None


class CompositeTranscriptionEngine(FakeTranscriptionEngine):
    async def transcribe(
        self,
        request: TranscriptionEngineRequest,
        progress: ProgressReporter,
        is_cancelled: CancellationCheck,
        segment_ready: SegmentReporter,
    ) -> TranscriptionResult:
        result = await super().transcribe(request, progress, is_cancelled, segment_ready)
        return TranscriptionResult(
            language=result.language,
            language_probability=result.language_probability,
            duration_ms=result.duration_ms,
            segments=result.segments,
            speaker_turns=[
                DiarizationSegment(start_ms=0, end_ms=1200, speaker=0),
                DiarizationSegment(start_ms=1200, end_ms=2800, speaker=1),
            ],
        )


def _wav_bytes() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * 1600)
    return buffer.getvalue()


@contextmanager
def _client(
    tmp_path: Path,
    engine: FakeTranscriptionEngine | None = None,
) -> Iterator[TestClient]:
    settings = AppSettings(
        data_dir=tmp_path / "transcription-data",
        testing=True,
        open_browser=False,
        log_level="WARNING",
    )
    with TestClient(
        create_app(
            settings,
            transcription_engine=engine or FakeTranscriptionEngine(),
            audio_normalizer=FakeNormalizer(),
        )
    ) as client:
        yield client


def _wait_for_job(client: TestClient, job_uuid: str) -> dict[str, Any]:
    for _ in range(100):
        job = client.get(f"/api/jobs/{job_uuid}").json()
        if job["status"] in {"completed", "failed", "cancelled"}:
            return job
        time.sleep(0.02)
    raise AssertionError("Job did not reach a terminal state")


def test_transcription_pipeline_editor_and_versions(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        meeting = client.post("/api/meetings", json={"title": "Product review"}).json()
        imported = client.post(
            f"/api/meetings/{meeting['id']}/import",
            files={"file": ("review.wav", _wav_bytes(), "audio/wav")},
        )
        assert imported.status_code == 202
        recording = imported.json()["recording"]

        profiles = client.get("/api/models/transcription")
        assert profiles.status_code == 200
        balanced = next(item for item in profiles.json() if item["id"] == "balanced")
        assert balanced["installed"] is True
        assert balanced["compute_type"] == "int8"

        started = client.post(
            f"/api/meetings/{meeting['id']}/transcriptions",
            json={"profile_id": "balanced", "language": "en"},
        )
        assert started.status_code == 202
        started_payload = started.json()
        terminal = _wait_for_job(client, started_payload["job"]["uuid"])
        assert terminal["status"] == "completed"
        assert terminal["result"]["segment_count"] == 2

        transcription_id = started_payload["transcription"]["id"]
        detail = client.get(f"/api/transcriptions/{transcription_id}")
        assert detail.status_code == 200
        assert detail.json()["transcription"]["is_active"] is True
        assert detail.json()["transcription"]["title"] == "New Transcription"
        assert [segment["text"] for segment in detail.json()["segments"]] == [
            "Welcome to the product review.",
            "We approved the product launch.",
        ]
        assert all(segment["is_final"] for segment in detail.json()["segments"])
        assert 'data-default-title="New Transcription 2"' in client.get(f"/meetings/{meeting['id']}/transcript").text

        renamed = client.patch(
            f"/api/transcriptions/{transcription_id}",
            json={"title": "Product launch decision"},
        )
        assert renamed.status_code == 200
        assert renamed.json()["title"] == "Product launch decision"
        assert client.get(f"/api/meetings/{meeting['id']}").json()["title"] == (
            "Product launch decision"
        )
        assert 'data-default-title="New Transcription"' in client.get(f"/meetings/{meeting['id']}/transcript").text

        first_segment = detail.json()["segments"][0]
        edited = client.patch(
            f"/api/transcript-segments/{first_segment['id']}",
            json={"text": "Welcome to the release review."},
        )
        assert edited.status_code == 200
        assert edited.json()["text"] == "Welcome to the release review."

        replaced = client.post(
            f"/api/transcriptions/{transcription_id}/find-replace",
            json={"find": "product", "replacement": "release", "case_sensitive": False},
        )
        assert replaced.status_code == 200
        assert replaced.json()["replacements"] == 1

        media = client.get(f"/api/recordings/{recording['id']}/media")
        assert media.status_code == 200
        assert media.content.startswith(b"RIFF")

        versions = client.get(f"/api/meetings/{meeting['id']}/transcriptions")
        assert versions.status_code == 200
        assert versions.json()[0]["segment_count"] == 2

        workspace = client.get(f"/meetings/{meeting['id']}/transcript")
        assert workspace.status_code == 200
        assert 'id="workspace-meeting-select"' not in workspace.text
        assert 'id="start-transcription"' in workspace.text
        assert 'id="transcription-dialog"' in workspace.text
        assert "Choose your audio sources" in workspace.text
        assert "Import media" not in workspace.text
        assert 'href="/settings"' in workspace.text
        assert 'id="postprocess-dialog"' in workspace.text
        assert 'data-meeting-tab="transcript"' in workspace.text
        assert 'data-meeting-tab="speakers"' in workspace.text
        assert 'data-meeting-tab="intelligence"' in workspace.text
        assert 'data-meeting-tab="utilities"' in workspace.text
        assert 'id="speaker-rebuild-identification"' in workspace.text
        assert 'id="speaker-rebuild-dialog"' in workspace.text
        assert 'id="ai-toggle-view"' in workspace.text
        assert 'id="live-ai-assistant"' in workspace.text
        assert 'id="export-dialog"' in workspace.text
        assert 'data-audio-export="mp3"' in workspace.text
        assert 'id="download-meeting-json"' in workspace.text

        library = client.get("/meetings")
        assert library.status_code == 200
        assert "Product launch decision" in library.text
        assert "Product review" not in library.text
        assert f'href="/?meeting={meeting["id"]}"' in library.text

        meeting_detail = client.get(f"/meetings/{meeting['id']}")
        assert meeting_detail.status_code == 200
        assert "Product launch decision" in meeting_detail.text
        assert 'data-note-pane="summary"' in meeting_detail.text
        assert 'id="summary-content"' in meeting_detail.text


def test_meeting_audio_can_be_deleted_without_removing_transcript(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        meeting = client.post("/api/meetings", json={"title": "Keep the text"}).json()
        imported = client.post(
            f"/api/meetings/{meeting['id']}/import",
            files={"file": ("private.wav", _wav_bytes(), "audio/wav")},
        ).json()
        _wait_for_job(client, imported["job"]["uuid"])
        started = client.post(
            f"/api/meetings/{meeting['id']}/transcriptions",
            json={"profile_id": "balanced", "language": "en"},
        ).json()
        _wait_for_job(client, started["job"]["uuid"])

        recordings_before = client.get(
            f"/api/meetings/{meeting['id']}/recordings"
        ).json()
        assert {item["role"] for item in recordings_before} == {"original", "normalized"}

        deleted = client.delete(f"/api/meetings/{meeting['id']}/audio")

        assert deleted.status_code == 200
        assert deleted.json()["audio_deleted_at"]
        assert deleted.json()["audio_deleted_bytes"] > 0
        assert deleted.json()["recording_count"] == 0
        assert client.get(f"/api/meetings/{meeting['id']}/recordings").json() == []
        detail = client.get(
            f"/api/transcriptions/{started['transcription']['id']}"
        ).json()
        assert [segment["text"] for segment in detail["segments"]] == [
            "Welcome to the product review.",
            "We approved the product launch.",
        ]
        assert "Keep the text" in client.get("/meetings").text
        assert "Audio deleted" in client.get("/meetings").text


def test_model_download_requires_explicit_confirmation(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        meeting = client.post("/api/meetings", json={"title": "Download consent"}).json()
        client.post(
            f"/api/meetings/{meeting['id']}/import",
            files={"file": ("review.wav", _wav_bytes(), "audio/wav")},
        )

        blocked = client.post(
            f"/api/meetings/{meeting['id']}/transcriptions",
            json={"profile_id": "fast", "allow_model_download": False},
        )
        assert blocked.status_code == 409
        assert "Confirm the model download" in blocked.json()["detail"]

        confirmed = client.post(
            f"/api/meetings/{meeting['id']}/transcriptions",
            json={"profile_id": "fast", "allow_model_download": True},
        )
        assert confirmed.status_code == 202


def test_composite_asr_persists_integrated_speaker_turns(tmp_path: Path) -> None:
    with _client(tmp_path, CompositeTranscriptionEngine()) as client:
        meeting = client.post("/api/meetings", json={"title": "Composite ASR"}).json()
        client.post(
            f"/api/meetings/{meeting['id']}/import",
            files={"file": ("composite.wav", _wav_bytes(), "audio/wav")},
        )
        started = client.post(
            f"/api/meetings/{meeting['id']}/transcriptions",
            json={"profile_id": "balanced", "language": "en"},
        ).json()

        terminal = _wait_for_job(client, started["job"]["uuid"])
        detail = client.get(
            f"/api/transcriptions/{started['transcription']['id']}"
        ).json()

        assert terminal["result"]["speaker_count"] == 2
        assert terminal["result"]["assigned_segments"] == 2
        assert [segment["speaker_id"] is not None for segment in detail["segments"]] == [
            True,
            True,
        ]



def test_primary_capture_page_exposes_browser_recording_controls(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.get("/capture")
        assert response.status_code == 200
        assert 'id="browser-capture-enabled"' in response.text
        assert 'id="browser-mic-enabled"' in response.text
        assert 'id="browser-tab-enabled"' in response.text
        assert "flow-studio.js" in response.text


def test_browser_capture_streams_live_segments_and_finalizes(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        started = client.post(
            "/api/browser-capture/sessions",
            json={
                "title": "Streaming browser meeting",
                "profile_id": "balanced",
                "use_microphone": True,
                "use_shared_tab": False,
                "postprocess_options": {
                    "diarization": False,
                    "speaker_count": None,
                    "summary": False,
                    "summary_template_id": None,
                },
            },
        )
        assert started.status_code == 201
        session = started.json()
        pcm = b"\x00\x00" * (16000 * 3)
        chunk_url = (
            f"/api/browser-capture/sessions/{session['session_id']}/chunks"
        )
        headers = {
            "content-type": "application/octet-stream",
            "x-audio-sequence": "0",
        }
        first = client.post(chunk_url, content=pcm, headers=headers)
        assert first.status_code == 200
        assert first.json()["last_sequence"] == 0
        assert first.json()["segment_count"] == 2

        # A retry after a lost HTTP response is idempotent.
        repeated = client.post(chunk_url, content=pcm, headers=headers)
        assert repeated.status_code == 200
        assert repeated.json()["last_sequence"] == 0
        assert repeated.json()["segment_count"] == 2

        live_detail = client.get(
            f"/api/transcriptions/{session['transcription_id']}"
        )
        assert live_detail.status_code == 200
        assert len(live_detail.json()["segments"]) == 2
        assert all(not segment["is_final"] for segment in live_detail.json()["segments"])

        live_page = client.get(
            f"/api/meetings/{session['meeting_id']}/transcript?cursor=-1&limit=100"
        )
        assert live_page.status_code == 200
        assert len(live_page.json()["segments"]) == 2

        stopped = client.post(
            f"/api/browser-capture/sessions/{session['session_id']}/stop",
            json={
                "final_transcription": True,
                "postprocess_options": {
                    "diarization": False,
                    "speaker_count": None,
                    "summary": False,
                    "summary_template_id": None,
                },
            },
        )
        assert stopped.status_code == 200
        job = stopped.json()["transcription_job"]
        terminal = _wait_for_job(client, job["uuid"])
        assert terminal["status"] == "completed"

        final_detail = client.get(
            f"/api/transcriptions/{session['transcription_id']}"
        )
        assert final_detail.status_code == 200
        assert len(final_detail.json()["segments"]) == 2
        assert all(segment["is_final"] for segment in final_detail.json()["segments"])

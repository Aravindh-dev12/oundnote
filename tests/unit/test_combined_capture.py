from __future__ import annotations

import sys
import wave
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from local_meeting_ai.adapters.audio_capture.combined import CombinedCaptureBackend
from local_meeting_ai.adapters.audio_capture.mixer import AudioMixer
from local_meeting_ai.api.schemas import LiveCaptureStart
from local_meeting_ai.domain.entities import AudioCaptureSource
from local_meeting_ai.domain.errors import CapabilityUnavailableError, ValidationError


def sources() -> list[AudioCaptureSource]:
    mic = AudioCaptureSource("test:0", "Microphone", "microphone", "test", "test", 1, 16000)
    return [
        mic,
        replace(
            mic,
            id="test:1",
            name="Headphones",
            kind="system",
            channels=2,
            sample_rate=48000,
            is_loopback=True,
        ),
    ]


def pcm(value: int, frames: int, channels: int = 1) -> bytes:
    return np.full(frames * channels, value, dtype="<i2").tobytes()


def test_mix_resamples_stereo_and_preserves_both_inputs_without_clipping() -> None:
    mixer = AudioMixer(sources())
    mixer.push("test:0", pcm(30000, 1600), 0)
    mixer.push("test:1", pcm(32000, 4800, 2), 0)
    result = np.frombuffer(mixer.read(4700), dtype="<i2")
    assert np.all(result == 31000)
    assert mixer.levels(0.1)["test:0"] > 0
    assert mixer.levels(1) == {"test:0": 0, "test:1": 0}


def test_missing_loopback_and_delayed_start_keep_the_recording_clock() -> None:
    mixer = AudioMixer(sources())
    mixer.push("test:0", pcm(12000, 1600), 0)
    mixer.push("test:1", pcm(8000, 2400, 2), 0.05)
    result = np.frombuffer(mixer.read(4700), dtype="<i2")
    assert np.all(result[:2400] == 6000)
    assert np.all(result[2400:] == 10000)
    # A loopback source can stop callbacks during silence, then return much later.
    mixer.read(48000 - 4700)
    mixer.push("test:1", pcm(8000, 4800, 2), 1.0)
    assert np.all(np.frombuffer(mixer.read(4800), dtype="<i2") == 4000)


def test_fractional_resampling_is_independent_of_callback_chunk_sizes() -> None:
    selected = [replace(sources()[0], sample_rate=44100), sources()[1]]
    data = (12000 * np.sin(np.arange(44100) * 2 * np.pi * 440 / 44100)).astype("<i2")
    whole = AudioMixer(selected)
    whole.push("test:0", data.tobytes(), 0)
    chunked = AudioMixer(selected)
    for offset in range(0, len(data), 317):
        chunked.push("test:0", data[offset : offset + 317].tobytes(), offset / 44100)
    assert whole.read(48000) == chunked.read(48000)


def test_backward_clock_correction_does_not_double_an_input() -> None:
    mixer = AudioMixer(sources())
    mixer.push("test:1", pcm(20000, 4800, 2), 0)
    mixer.push("test:1", pcm(20000, 4800, 2), 0.06)
    result = np.frombuffer(mixer.read(7680), dtype="<i2")
    assert np.all(result == 10000)


class NativeStub:
    name = "test"

    def list_sources(self) -> list[AudioCaptureSource]:
        return sources()

    def status(self) -> None:
        return None

    def shutdown(self) -> None:
        pass


class Stream:
    def __init__(self, **kwargs: Any) -> None:
        self.callback = kwargs.get("stream_callback", kwargs.get("callback"))
        self.active = False
        self.closed = False

    def start(self) -> None:
        self.active = True

    start_stream = start

    def stop(self) -> None:
        self.active = False

    stop_stream = stop

    def close(self) -> None:
        self.closed = True

    def is_active(self) -> bool:
        return self.active


@pytest.fixture
def driver(monkeypatch: pytest.MonkeyPatch) -> Any:
    streams: list[Stream] = []
    manager = SimpleNamespace(terminated=False)

    def open_stream(**kwargs: Any) -> Stream:
        stream = Stream(**kwargs)
        streams.append(stream)
        return stream

    manager.open = open_stream
    manager.terminate = lambda: setattr(manager, "terminated", True)
    monkeypatch.setitem(
        sys.modules,
        "pyaudiowpatch",
        SimpleNamespace(
            PyAudio=lambda: manager,
            paInt16=8,
            paContinue=0,
        ),
    )
    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(RawInputStream=open_stream))
    return SimpleNamespace(streams=streams, manager=manager)


@pytest.mark.parametrize("platform", ["Windows", "Linux", "Darwin"])
def test_both_native_streams_pause_resume_and_final_wav(
    platform: str,
    driver: Any,
    tmp_path: Path,
) -> None:
    backend = CombinedCaptureBackend(NativeStub(), platform)  # type: ignore[arg-type]
    path = tmp_path / "combined.wav"
    try:
        status = backend.start(
            session_id="session",
            source_id="test:0",
            additional_source_id="test:1",
            destination=path,
        )
        assert len(status.sources) == 2
        assert len(driver.streams) == 2 and all(s.active for s in driver.streams)
        # Deterministic PCM with a shared clock; the worker and controls use this same queue.
        with backend._audio_lock:
            backend._queue.put(("test:0", pcm(12000, 1600), backend._epoch, None))
            backend._queue.put(("test:1", pcm(8000, 4800, 2), backend._epoch, None))
            backend._pump(0.1, flush=True)
        live = backend.drain_frames()
        assert live is not None and live.channels == 1 and live.sample_rate == 48000
        assert np.all(np.frombuffer(live.pcm_s16le[:9400], dtype="<i2") == 10000)
        assert backend.pause().state == "paused"
        assert all(not s.active for s in driver.streams)
        assert backend.resume().state == "recording"
        assert all(s.active for s in driver.streams)
        result = backend.stop()
        assert len(result.sources) == 2
        assert all(s.closed for s in driver.streams)
        if platform == "Windows":
            assert driver.manager.terminated
        with wave.open(str(path), "rb") as audio:
            assert audio.getframerate() == 48000 and audio.getnchannels() == 1
            assert audio.readframes(live.end_frame) == live.pcm_s16le
        assert backend.status() is None
    finally:
        backend.shutdown()


@pytest.mark.parametrize("platform", ["Windows", "Linux", "Darwin"])
def test_second_device_failure_closes_first_and_allows_retry(
    platform: str,
    driver: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = CombinedCaptureBackend(NativeStub(), platform)  # type: ignore[arg-type]
    module = sys.modules["sounddevice"]
    original = driver.manager.open if platform == "Windows" else module.RawInputStream

    def fail_second(**kwargs: Any) -> Any:
        if driver.streams:
            raise OSError("Device disconnected")
        return original(**kwargs)

    monkeypatch.setattr(
        driver.manager if platform == "Windows" else module,
        "open" if platform == "Windows" else "RawInputStream",
        fail_second,
    )
    path = tmp_path / "failed.wav"
    with pytest.raises(CapabilityUnavailableError, match="Device disconnected"):
        backend.start(
            session_id="session",
            source_id="test:0",
            additional_source_id="test:1",
            destination=path,
        )
    assert not path.exists() and driver.streams[0].closed
    assert backend.status() is None
    monkeypatch.setattr(
        driver.manager if platform == "Windows" else module,
        "open" if platform == "Windows" else "RawInputStream",
        original,
    )
    backend.start(
        session_id="retry", source_id="test:0", additional_source_id="test:1", destination=path
    )
    backend.shutdown()
    assert all(s.closed for s in driver.streams)


def test_duplicate_or_wrong_sources_rejected_before_opening_devices(tmp_path: Path) -> None:
    backend = CombinedCaptureBackend(NativeStub(), "Windows")  # type: ignore[arg-type]
    for second in ["test:0", "missing"]:
        with pytest.raises(ValidationError):
            backend.start(
                session_id="session",
                source_id="test:0",
                additional_source_id=second,
                destination=tmp_path / "audio.wav",
            )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"source_ids": []},
        {"source_ids": ["a", "a"]},
        {"source_ids": ["a", "b", "c"]},
        {"source_ids": [""]},
        {"source_id": "a", "source_ids": ["a"]},
    ],
)
def test_capture_request_rejects_ambiguous_or_invalid_selection(payload: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        LiveCaptureStart.model_validate(payload)


def test_capture_request_keeps_legacy_single_source() -> None:
    assert LiveCaptureStart(source_id="mic").source_id == "mic"
    assert LiveCaptureStart(source_ids=["mic", "system"]).source_ids == ["mic", "system"]

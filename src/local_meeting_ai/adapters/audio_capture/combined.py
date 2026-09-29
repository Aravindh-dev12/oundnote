from __future__ import annotations

import importlib
import math
import queue
import threading
import time
import wave
from contextlib import suppress
from pathlib import Path
from typing import Any

from local_meeting_ai.domain.entities import (
    AudioCaptureSource,
    AudioFrameBatch,
    CapturedAudio,
    CaptureStatus,
)
from local_meeting_ai.domain.errors import CapabilityUnavailableError, ValidationError
from local_meeting_ai.domain.protocols import AudioCaptureBackend


class CombinedCaptureBackend:
    """Add microphone + system capture to a platform's single-input backend.

    Native callbacks only enqueue PCM. One worker owns mixing and WAV writes;
    the same mixed frames feed live transcription and the final recording.
    """

    def __init__(self, native: AudioCaptureBackend, platform_name: str) -> None:
        self.native = native
        self.name = native.name
        self.platform_name = platform_name
        self._lock = threading.RLock()
        self._audio_lock = threading.RLock()
        self._streams: list[Any] = []
        self._manager: Any = None
        self._writer: wave.Wave_write | None = None
        self._mixer: Any = None
        self._sources: list[AudioCaptureSource] = []
        self._session_id: str | None = None
        self._destination: Path | None = None
        self._state = "idle"
        self._epoch = 0.0
        self._seconds = 0.0
        self._queue: queue.Queue[tuple[str, bytes, float, str | None]] = queue.Queue(maxsize=128)
        self._pending = bytearray()
        self._pending_frame = 0
        self._error: str | None = None
        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None

    def capability(self) -> dict[str, Any]:
        with self._lock:
            return {
                **self.native.capability(),
                "supports_combined_capture": True,
                "max_capture_sources": 2,
            }

    def list_sources(self) -> list[AudioCaptureSource]:
        with self._lock:
            return self.native.list_sources()

    def probe_level(self, source_id: str) -> float:
        with self._lock:
            if self._session_id:
                status = self._status()
                return status.source_levels.get(source_id, 0.0)
            return self.native.probe_level(source_id)

    def start(
        self,
        *,
        session_id: str,
        source_id: str,
        destination: Path,
        additional_source_id: str | None = None,
    ) -> CaptureStatus:
        with self._lock:
            if self._session_id or self.native.status() is not None:
                raise ValidationError("Another live capture is already active")
            if additional_source_id is None:
                self._mixer = None
                return self.native.start(
                    session_id=session_id,
                    source_id=source_id,
                    destination=destination,
                )
            available = {source.id: source for source in self.list_sources()}
            ids = [source_id, additional_source_id]
            if len(set(ids)) != 2 or any(key not in available for key in ids):
                raise ValidationError("Choose two different, available audio sources")
            sources = [available[key] for key in ids]
            if sum(source.kind == "system" for source in sources) != 1:
                raise ValidationError(
                    "Combined capture requires one microphone and one system source"
                )
            if any(not source.available for source in sources):
                raise ValidationError("The selected audio source is no longer available")
            try:
                from local_meeting_ai.adapters.audio_capture.mixer import AudioMixer
            except ImportError as error:
                raise CapabilityUnavailableError(
                    'Audio mixing is not installed. Run: pip install -e ".[capture]"'
                ) from error
            self._mixer = AudioMixer(sources)
            self._sources = sources
            self._session_id, self._destination = session_id, destination
            self._seconds = 0.0
            self._pending.clear()
            self._pending_frame = 0
            self._error = None
            self._stop_event.clear()
            self._clear_queue()
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                # Owned by the capture worker until stop() closes the session.
                self._writer = wave.open(str(destination), "wb")  # noqa: SIM115
                self._writer.setnchannels(1)
                self._writer.setsampwidth(2)
                self._writer.setframerate(48000)
                self._open_streams()
                self._epoch = time.monotonic()
                self._state = "recording"
                for stream in self._streams:
                    self._stream_action(stream, "start")
                self._worker = threading.Thread(
                    target=self._run,
                    name="combined-audio-capture",
                    daemon=True,
                )
                self._worker.start()
            except Exception as error:
                self._state = "idle"
                self._close_streams()
                if self._writer is not None:
                    self._writer.close()
                self._writer = None
                self._session_id = None
                self._mixer = None
                self._clear_queue()
                destination.unlink(missing_ok=True)
                raise CapabilityUnavailableError(
                    f"Could not start microphone + system: {error}"
                ) from error
            return self._status()

    def _open_streams(self) -> None:
        windows = self.platform_name == "Windows"
        module = importlib.import_module("pyaudiowpatch" if windows else "sounddevice")
        if windows:
            self._manager = module.PyAudio()
        for source in self._sources:
            callback = self._callback(source, module)
            channels = max(1, min(source.channels, 2))
            index = int(source.id.split(":", 1)[1])
            if windows:
                stream = self._manager.open(
                    format=module.paInt16,
                    channels=channels,
                    rate=source.sample_rate,
                    input=True,
                    input_device_index=index,
                    frames_per_buffer=1024,
                    stream_callback=callback,
                    start=False,
                )
            else:
                stream = module.RawInputStream(
                    samplerate=source.sample_rate,
                    channels=channels,
                    device=index,
                    dtype="int16",
                    blocksize=1024,
                    callback=callback,
                )
            self._streams.append(stream)

    def _callback(self, source: AudioCaptureSource, module: Any) -> Any:
        def callback(data: Any, frames: int, timing: Any, flags: Any) -> Any:
            now = time.monotonic()
            windows = self.platform_name == "Windows"
            if self._state == "recording":
                # Convert each stream's native clock to a shared monotonic clock.
                adc = (
                    timing.get("input_buffer_adc_time", 0) if windows else timing.inputBufferAdcTime
                )
                current = timing.get("current_time", 0) if windows else timing.currentTime
                offset = float(adc) - float(current)
                if not adc or not math.isfinite(offset) or not -0.5 <= offset <= 0:
                    offset = -frames / source.sample_rate
                warning = f"Audio input interrupted: {source.name}" if flags else None
                try:
                    self._queue.put_nowait((source.id, bytes(data), now + offset, warning))
                except queue.Full:
                    self._error = "Audio capture could not keep up; part of the audio was lost"
            return (None, module.paContinue) if windows else None

        return callback

    def _stream_action(self, stream: Any, action: str) -> None:
        getattr(stream, f"{action}_stream" if self.platform_name == "Windows" else action)()

    def _close_streams(self) -> None:
        for stream in self._streams:
            with suppress(Exception):
                self._stream_action(stream, "stop")
            with suppress(Exception):
                stream.close()
        self._streams.clear()
        if self._manager is not None:
            with suppress(Exception):
                self._manager.terminate()
            self._manager = None

    def _clear_queue(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def _elapsed(self) -> float:
        return self._seconds + (time.monotonic() - self._epoch if self._state == "recording" else 0)

    def _pump(self, elapsed: float, *, flush: bool = False) -> None:
        while True:
            try:
                source_id, pcm, timestamp, warning = self._queue.get_nowait()
            except queue.Empty:
                break
            self._mixer.push(source_id, pcm, self._seconds + timestamp - self._epoch)
            if warning:
                self._error = warning
        target = max(0, round((elapsed - (0 if flush else 0.15)) * 48000))
        while self._mixer.frame < target:
            pcm = self._mixer.read(min(4800, target - self._mixer.frame))
            assert self._writer is not None
            self._writer.writeframesraw(pcm)
            self._pending.extend(pcm)
        overflow = len(self._pending) - 48000 * 2 * 60
        if overflow > 0:
            del self._pending[:overflow]
            self._pending_frame += overflow // 2

    def _run(self) -> None:
        while not self._stop_event.wait(0.02):
            with self._audio_lock:
                if self._state != "recording":
                    continue
                try:
                    self._pump(self._elapsed())
                    for stream in self._streams:
                        active = (
                            stream.is_active() if self.platform_name == "Windows" else stream.active
                        )
                        if not active:
                            self._error = (
                                "An audio device stopped. Reconnect it and start a new recording."
                            )
                except Exception as error:
                    self._error = f"Audio capture failed: {error}"
                    return

    def _status(self) -> CaptureStatus:
        with self._audio_lock:
            assert self._session_id is not None and self._destination is not None
            levels = (
                self._mixer.levels(self._elapsed())
                if self._state == "recording"
                else {source.id: 0.0 for source in self._sources}
            )
            return CaptureStatus(
                session_id=self._session_id,
                state=self._state,
                source=self._sources[0],
                sources=tuple(self._sources),
                source_levels=levels,
                destination=self._destination,
                elapsed_ms=round(self._elapsed() * 1000),
                level=max(levels.values(), default=0.0),
                error=self._error,
            )

    def status(self) -> CaptureStatus | None:
        with self._lock:
            return self._status() if self._session_id else self.native.status()

    def drain_frames(self) -> AudioFrameBatch | None:
        with self._audio_lock:
            if self._mixer is None:
                return self.native.drain_frames()
            if not self._pending:
                return None
            pcm = bytes(self._pending)
            start = self._pending_frame
            self._pending.clear()
            self._pending_frame += len(pcm) // 2
            return AudioFrameBatch(pcm, 48000, 1, start, self._pending_frame)

    def pause(self) -> CaptureStatus:
        with self._lock:
            if not self._session_id:
                return self.native.pause()
            if self._state != "recording":
                raise ValidationError("Live capture is not recording")
            self._freeze()
            return self._status()

    def _freeze(self) -> None:
        with self._audio_lock:
            elapsed = self._elapsed()
            self._state = "paused"
            for stream in self._streams:
                with suppress(Exception):
                    self._stream_action(stream, "stop")
            self._pump(elapsed, flush=True)
            self._seconds = elapsed
            self._mixer.reset_inputs()

    def resume(self) -> CaptureStatus:
        with self._lock:
            if not self._session_id:
                return self.native.resume()
            if self._state != "paused":
                raise ValidationError("Live capture is not paused")
            with self._audio_lock:
                self._clear_queue()
                self._epoch = time.monotonic()
                self._state = "recording"
                try:
                    for stream in self._streams:
                        self._stream_action(stream, "start")
                except Exception as error:
                    self._freeze()
                    raise CapabilityUnavailableError(
                        f"Could not resume audio capture: {error}"
                    ) from error
            return self._status()

    def stop(self) -> CapturedAudio:
        with self._lock:
            if not self._session_id:
                return self.native.stop()
            self._stop_event.set()
            if self._worker is not None:
                self._worker.join()
            try:
                if self._state == "recording":
                    self._freeze()
            finally:
                self._close_streams()
                if self._writer is not None:
                    self._writer.close()
                self._writer = None
                self._session_id = None
                self._state = "idle"
            assert self._destination is not None
            return CapturedAudio(
                path=self._destination,
                source=self._sources[0],
                sources=tuple(self._sources),
                duration_ms=round(self._mixer.frame / 48),
                sample_rate=48000,
                channels=1,
            )

    def shutdown(self) -> None:
        with self._lock:
            if self._session_id:
                with suppress(Exception):
                    self.stop()
            self.native.shutdown()

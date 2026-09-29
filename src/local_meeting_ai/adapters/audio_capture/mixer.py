from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from local_meeting_ai.domain.entities import AudioCaptureSource

SAMPLE_RATE = 48000


@dataclass
class _Input:
    source: AudioCaptureSource
    blocks: deque[tuple[int, Any]] = field(default_factory=deque)
    input_frames: int = 0
    output_frames: int = 0
    next_frame: int | None = None
    previous: float = 0.0
    level: float = 0.0
    last_time: float = -1.0


class AudioMixer:
    """Clock-aligned mono PCM mix; all methods run on the capture worker's lock.

    Resampling carries its fractional position across callbacks. Device timestamps
    bound clock drift and preserve gaps when a loopback endpoint stops sending
    silence. Missing inputs never hold up the other input or the recording clock.
    """

    def __init__(self, sources: list[AudioCaptureSource]) -> None:
        self.inputs = {source.id: _Input(source) for source in sources}
        self.frame = 0

    def push(self, source_id: str, pcm: bytes, timestamp: float) -> None:
        track = self.inputs[source_id]
        channels = max(1, min(track.source.channels, 2))
        count = len(pcm) // (2 * channels)
        if not count:
            return
        samples = np.frombuffer(pcm[: count * channels * 2], dtype="<i2")
        mono = samples.reshape(-1, channels).mean(axis=1)
        track.level = float(min(1.0, np.sqrt(np.max(np.abs(mono)) / 32768)))
        track.last_time = timestamp + count / track.source.sample_rate
        # Keep the last sample for interpolation across callback boundaries.
        start = track.input_frames
        end = start + count
        output_end = int(np.floor((end - 1) * SAMPLE_RATE / track.source.sample_rate)) + 1
        positions = (
            np.arange(track.output_frames, output_end) * track.source.sample_rate / SAMPLE_RATE
        )
        converted = np.interp(
            positions - start,
            np.arange(-1, count),
            np.concatenate(([track.previous], mono)),
        )
        expected = round(
            timestamp * SAMPLE_RATE
            + (track.output_frames * track.source.sample_rate / SAMPLE_RATE - start)
            * SAMPLE_RATE
            / track.source.sample_rate
        )
        # Ignore callback jitter, but re-anchor after a silence gap or device drift.
        if track.next_frame is None or abs(expected - track.next_frame) > SAMPLE_RATE * 0.03:
            track.next_frame = expected
        # A backwards clock correction must not double the same input over its
        # previous block. Drop the overlap (or already-emitted late samples).
        committed_end = max(
            self.frame,
            track.blocks[-1][0] + len(track.blocks[-1][1]) if track.blocks else self.frame,
        )
        skip = max(0, committed_end - track.next_frame)
        if skip < len(converted):
            track.blocks.append((track.next_frame + skip, converted[skip:]))
        track.next_frame += len(converted)
        track.previous = float(mono[-1])
        track.input_frames = end
        track.output_frames = output_end
        # Also bound memory if a driver returns a bad/future timestamp.
        while len(track.blocks) > 200:
            track.blocks.popleft()

    def read(self, count: int) -> bytes:
        mixed = np.zeros(count, dtype=np.float64)
        end = self.frame + count
        for track in self.inputs.values():
            for start, samples in track.blocks:
                left, right = max(self.frame, start), min(end, start + len(samples))
                if right > left:
                    mixed[left - self.frame : right - self.frame] += samples[
                        left - start : right - start
                    ]
            while track.blocks and track.blocks[0][0] + len(track.blocks[0][1]) <= end:
                track.blocks.popleft()
        self.frame = end
        # Fixed headroom avoids clipping when both people speak at the same time.
        return np.clip(np.rint(mixed / len(self.inputs)), -32768, 32767).astype("<i2").tobytes()

    def levels(self, timestamp: float) -> dict[str, float]:
        return {
            source_id: track.level if timestamp - track.last_time < 0.5 else 0.0
            for source_id, track in self.inputs.items()
        }

    def reset_inputs(self) -> None:
        self.inputs = {key: _Input(track.source) for key, track in self.inputs.items()}

"""Microphone capture."""

from __future__ import annotations

import logging
import threading
from collections import deque

import numpy as np
import sounddevice as sd

from .transcriber import SAMPLE_RATE

log = logging.getLogger(__name__)

# Audio kept from just before the hotkey went down, so the first syllable
# is not lost to reaction time.
PRE_ROLL_SECONDS = 0.3


class Recorder:
    """Records mono 16 kHz float32 audio between start() and stop().

    device: an input device index, part of a device name, or None for the
    system default.

    keep_open: microphones can take over half a second to deliver audio after
    being opened, which would cut off the start of every dictation. With
    keep_open the stream runs for the lifetime of the app and audio outside a
    recording is discarded (apart from the short pre-roll). Turning it off opens
    the microphone only while the hotkey is held.
    """

    def __init__(self, device: int | str | None = None, keep_open: bool = True) -> None:
        self.device = device
        self.keep_open = keep_open
        self.level = 0.0  # RMS of the latest block, for the overlay
        self.device_name = ""
        self._lock = threading.Lock()
        self._recording = False
        self._chunks: list[np.ndarray] = []
        self._pre_roll: deque[np.ndarray] = deque()
        self._pre_roll_samples = 0
        self._stream: sd.InputStream | None = None
        self._refresh_devices()
        if keep_open:
            self._open()

    @staticmethod
    def _refresh_devices() -> None:
        # PortAudio lists devices once, when it initialises. Restart it so
        # microphones plugged in or removed since then are seen.
        sd._terminate()
        sd._initialize()

    def _open(self) -> None:
        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            device=self.device,
            callback=self._callback,
        )
        self._stream.start()
        self.device_name = sd.query_devices(self._stream.device, "input")["name"]
        log.info("Microphone: %s", self.device_name)

    def _callback(self, indata: np.ndarray, frames: int, time, status: sd.CallbackFlags) -> None:
        if status:
            log.debug("audio status: %s", status)
        block = indata[:, 0].copy()
        self.level = float(np.sqrt(np.mean(block * block)))
        with self._lock:
            if self._recording:
                self._chunks.append(block)
                return
            self._pre_roll.append(block)
            self._pre_roll_samples += len(block)
            limit = int(PRE_ROLL_SECONDS * SAMPLE_RATE)
            while self._pre_roll_samples - len(self._pre_roll[0]) >= limit:
                self._pre_roll_samples -= len(self._pre_roll.popleft())

    def start(self) -> None:
        with self._lock:
            self._chunks = list(self._pre_roll)
            self._pre_roll.clear()
            self._pre_roll_samples = 0
            self._recording = True
        if self._stream is None or not self._stream.active:
            try:
                self.close()
                if self.keep_open:
                    # A stream that died while kept open means the device went away.
                    self._refresh_devices()
                self._open()
            except Exception:
                self._recording = False
                raise

    def stop(self) -> np.ndarray:
        """End the recording and return what was captured."""
        with self._lock:
            self._recording = False
            chunks, self._chunks = self._chunks, []
        if not self.keep_open:
            self.close()
        return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)

    def close(self) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None

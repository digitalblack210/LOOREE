"""Speech-to-text with NVIDIA Parakeet via onnx-asr."""

from __future__ import annotations

import logging
import time
from pathlib import Path

import numpy as np
import onnx_asr
import onnxruntime as ort

log = logging.getLogger(__name__)

DEFAULT_MODEL = "nemo-parakeet-tdt-0.6b-v3"
DEFAULT_QUANTIZATION = "int8"
SAMPLE_RATE = 16000
# Longest stretch of audio transcribed in one pass, and how far back from that
# limit to look for a pause to cut at.
CHUNK_SECONDS = 30
SPLIT_SEARCH_SECONDS = 10

CUDA = "CUDAExecutionProvider"
CPU = "CPUExecutionProvider"


def _find_sessions(obj, depth: int = 0, seen: set[int] | None = None) -> list[ort.InferenceSession]:
    """Collect the onnxruntime sessions held anywhere inside an onnx-asr model."""
    seen = set() if seen is None else seen
    if id(obj) in seen or depth > 5:
        return []
    seen.add(id(obj))
    if isinstance(obj, ort.InferenceSession):
        return [obj]
    sessions = []
    for value in getattr(obj, "__dict__", {}).values():
        sessions += _find_sessions(value, depth + 1, seen)
    return sessions


def split_at_pauses(samples: np.ndarray) -> list[np.ndarray]:
    """Cut long audio into pieces of roughly CHUNK_SECONDS or less.

    The model's memory use grows with the square of the audio length, so a
    recording of several minutes cannot go through in one pass. Each cut is
    placed at the quietest moment in the last SPLIT_SEARCH_SECONDS of a piece,
    which in speech is almost always a pause between words.
    """
    limit = CHUNK_SECONDS * SAMPLE_RATE
    search = SPLIT_SEARCH_SECONDS * SAMPLE_RATE
    frame = SAMPLE_RATE // 10
    chunks = []
    start = 0
    # The extra second keeps the final piece from being a useless sliver.
    while len(samples) - start > limit + SAMPLE_RATE:
        window_start = start + limit - search
        window = samples[window_start : start + limit]
        energy = (window.reshape(-1, frame) ** 2).mean(axis=1)
        cut = window_start + int(energy.argmin()) * frame + frame // 2
        chunks.append(samples[start:cut])
        start = cut
    chunks.append(samples[start:])
    return chunks


class Transcriber:
    """Loads the model once and keeps it warm for repeated transcription.

    device: "auto" tries the GPU and falls back to CPU, "cuda" and "cpu" force one.
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        quantization: str | None = DEFAULT_QUANTIZATION,
        device: str = "auto",
    ) -> None:
        if device not in ("auto", "cuda", "cpu"):
            raise ValueError(f"device must be 'auto', 'cuda' or 'cpu', got {device!r}")
        self.model_name = model
        self.quantization = quantization
        self.device = device
        self.provider = CPU
        self._model = None
        self.load()

    def load(self) -> None:
        started = time.perf_counter()
        self._model = None
        if self.device != "cpu":
            self._model = self._try_load_cuda()
        if self._model is None:
            if self.device == "cuda":
                raise RuntimeError("device = 'cuda' was requested but the CUDA provider is unavailable")
            self._model = self._load([CPU])
            self.provider = CPU
        log.info(
            "Loaded %s (%s) on %s in %.1fs",
            self.model_name,
            self.quantization or "full precision",
            self.provider,
            time.perf_counter() - started,
        )
        self._warm_up()

    def _load(self, providers: list[str]):
        options = ort.SessionOptions()
        options.log_severity_level = 3  # errors only
        return onnx_asr.load_model(
            self.model_name,
            quantization=self.quantization,
            sess_options=options,
            providers=providers,
        )

    def _try_load_cuda(self):
        if CUDA not in ort.get_available_providers():
            log.info("CUDA provider not available in this onnxruntime build")
            return None
        try:
            # Picks up the CUDA/cuDNN DLLs installed as pip wheels.
            ort.preload_dlls()
            model = self._load([CUDA, CPU])
        except Exception as exc:
            log.warning("CUDA load failed (%s)", exc)
            return None
        # onnxruntime drops to CPU silently when the CUDA provider cannot
        # initialise, so check what the sessions actually ended up with.
        sessions = _find_sessions(model)
        if not sessions or any(CUDA not in s.get_providers() for s in sessions):
            log.warning("CUDA provider did not initialise for this GPU/driver")
            return None
        self.provider = CUDA
        return model

    def _warm_up(self) -> None:
        # The first inference pays for lazy allocations; spend it at startup.
        self.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32))

    def transcribe(self, samples: np.ndarray) -> str:
        """Transcribe mono 16 kHz float32 samples in [-1, 1]."""
        pieces = (
            self._model.recognize(chunk, sample_rate=SAMPLE_RATE).strip()
            for chunk in split_at_pauses(samples)
        )
        return " ".join(piece for piece in pieces if piece)

    def transcribe_file(self, path: str | Path) -> str:
        return self._model.recognize(str(path)).strip()

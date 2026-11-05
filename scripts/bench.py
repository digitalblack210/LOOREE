"""Compare model settings on your own recordings: speed, memory and word error rate.

Usage: python scripts/bench.py [clips folder] [--configs int8-cpu,full-cuda,...] [--repeats N]

The clips folder holds WAV files, each with a .txt of the same name holding the exact
words spoken. Every setting runs in its own process, so its memory peaks are its own.
Results go to bench_results.csv and bench_results.md in the clips folder.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger("bench")

DEFAULT_CLIPS = Path("samples") / "bench"
# name -> (quantization, device). "" is full precision, as in config.toml.
CONFIGS = {
    "int8-cpu": ("int8", "cpu"),
    "int8-cuda": ("int8", "cuda"),
    "full-cpu": ("", "cpu"),
    "full-cuda": ("", "cuda"),
}
SAMPLE_INTERVAL = 0.02


@dataclass
class Clip:
    name: str
    wav: Path
    reference: str


def find_clips(folder: Path) -> list[Clip]:
    """Every WAV in the folder that has a .txt beside it with words in it."""
    clips = []
    for wav in sorted(folder.glob("*.wav")):
        txt = wav.with_suffix(".txt")
        if not txt.exists():
            log.warning("Skipping %s: no %s", wav.name, txt.name)
            continue
        reference = " ".join(txt.read_text(encoding="utf-8").split())
        if not words(reference):
            log.warning("Skipping %s: %s has no words to score against", wav.name, txt.name)
            continue
        clips.append(Clip(wav.stem, wav, reference))
    return clips


def read_clip(path: Path) -> np.ndarray:
    """Mono 16 kHz float32 samples, the same input Looree gets from the microphone."""
    from onnx_asr.utils import read_wav

    from looree.transcriber import SAMPLE_RATE

    samples, rate = read_wav(str(path))
    if rate != SAMPLE_RATE:
        raise ValueError(f"{path.name} is {rate} Hz; record or export it at {SAMPLE_RATE} Hz")
    return samples.mean(axis=1).astype(np.float32)


# --- word error rate ------------------------------------------------------

# Hyphens and dashes become spaces before punctuation goes, so "push-to-talk" matches
# "push to talk".
_DASHES = str.maketrans(dict.fromkeys("-–—", " "))


def words(text: str) -> list[str]:
    """The words of a transcript as they are scored: lower case, without punctuation.

    Parakeet writes punctuation and capitals; the comparison is on words only.
    Punctuation is every character Unicode files under P, as in jiwer's RemovePunctuation.
    """
    text = text.lower().translate(_DASHES)
    return "".join(ch for ch in text if not unicodedata.category(ch).startswith("P")).split()


def word_errors(reference: list[str], hypothesis: list[str]) -> int:
    """Substitutions, deletions and insertions that turn the reference into the hypothesis."""
    previous = list(range(len(hypothesis) + 1))
    for i, wanted in enumerate(reference, start=1):
        current = [i]
        for j, heard in enumerate(hypothesis, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (wanted != heard)))
        previous = current
    return previous[-1]


def word_error_rate(references: list[str], hypotheses: list[str]) -> float:
    """Word error rate over all the given pairs, as a fraction (0.05 is 5%).

    All the errors over all the reference words, so long clips weigh more than short ones.
    """
    reference_words = [words(reference) for reference in references]
    total = sum(len(reference) for reference in reference_words)
    if not total:
        raise ValueError("the reference transcripts have no words to score against")
    errors = sum(
        word_errors(reference, words(hypothesis))
        for reference, hypothesis in zip(reference_words, hypotheses, strict=True)
    )
    return errors / total


# --- memory sampling ------------------------------------------------------


class PeakMemory:
    """Samples this process's RAM and the GPU's used VRAM in the background.

    VRAM is read for the whole card and reported as the rise over what was in use
    before the model loaded, because Windows does not give per-process figures.
    Other programs using the GPU during a run will show up in it.
    """

    def __init__(self, gpu: bool) -> None:
        import psutil

        self._process = psutil.Process()
        self._stop = threading.Event()
        self.peak_ram = self._process.memory_info().rss
        self._gpu = None
        self.vram_baseline = 0
        self.peak_vram = 0
        if gpu:
            try:
                import pynvml

                pynvml.nvmlInit()
                self._gpu = pynvml.nvmlDeviceGetHandleByIndex(0)
                self.vram_baseline = self.peak_vram = pynvml.nvmlDeviceGetMemoryInfo(self._gpu).used
            except Exception as exc:
                log.warning("VRAM will not be measured (%s)", exc)
                self._gpu = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _sample(self) -> None:
        self.peak_ram = max(self.peak_ram, self._process.memory_info().rss)
        if self._gpu is not None:
            import pynvml

            self.peak_vram = max(self.peak_vram, pynvml.nvmlDeviceGetMemoryInfo(self._gpu).used)

    def _run(self) -> None:
        while not self._stop.wait(SAMPLE_INTERVAL):
            self._sample()

    def stop(self) -> tuple[int, int | None]:
        """Peak RAM in bytes, and peak VRAM rise in bytes (None when not measured)."""
        self._stop.set()
        self._thread.join()
        self._sample()
        # Windows records the true peak, which sampling can miss by a few milliseconds.
        self.peak_ram = max(self.peak_ram, getattr(self._process.memory_info(), "peak_wset", 0))
        vram = self.peak_vram - self.vram_baseline if self._gpu is not None else None
        return self.peak_ram, vram


# --- one setting, in its own process --------------------------------------


def run_config(name: str, model: str, clip_paths: list[Path], repeats: int, clean: bool) -> dict:
    from looree import cleanup
    from looree.transcriber import SAMPLE_RATE, Transcriber

    quantization, device = CONFIGS[name]
    # Read the audio before measuring, so the peaks belong to the model alone.
    audio = [read_clip(path) for path in clip_paths]
    memory = PeakMemory(gpu=device == "cuda")
    started = time.perf_counter()
    try:
        # Includes the warm-up transcription Looree does on every load.
        transcriber = Transcriber(model=model, quantization=quantization or None, device=device)
    except Exception as exc:
        memory.stop()
        return {"error": str(exc)}
    load_seconds = time.perf_counter() - started

    clips = []
    for path, samples in zip(clip_paths, audio, strict=True):
        times = []
        for _ in range(repeats):
            started = time.perf_counter()
            text = transcriber.transcribe(samples)
            times.append(time.perf_counter() - started)
        if clean:
            text = cleanup.remove_fillers(text)
        clips.append(
            {
                "clip": path.stem,
                "audio_seconds": len(samples) / SAMPLE_RATE,
                "seconds": statistics.median(times),
                "hypothesis": text,
            }
        )
    peak_ram, peak_vram = memory.stop()
    return {
        "provider": transcriber.provider,
        "load_seconds": load_seconds,
        "peak_ram": peak_ram,
        "peak_vram": peak_vram,
        "clips": clips,
    }


def download(model: str, quantization: str) -> None:
    """Fetch the model files so the first timed load is not a download."""
    import onnx_asr

    onnx_asr.load_model(model, quantization=quantization or None, providers=["CPUExecutionProvider"])


def worker(args: argparse.Namespace) -> None:
    if args.download is not None:
        download(args.model, args.download)
        return
    result = run_config(args.worker, args.model, [Path(p) for p in args.clip_paths], args.repeats, args.clean)
    Path(args.result).write_text(json.dumps(result), encoding="utf-8")


def spawn(arguments: list[str]) -> subprocess.CompletedProcess:
    script = Path(__file__).resolve()
    return subprocess.run([sys.executable, str(script), *arguments], stdin=subprocess.DEVNULL)


# --- report ---------------------------------------------------------------


def summarise(name: str, result: dict, references: dict[str, str]) -> dict:
    if "error" in result:
        return {"config": name, "error": result["error"]}
    clips = result["clips"]
    audio = sum(clip["audio_seconds"] for clip in clips)
    seconds = sum(clip["seconds"] for clip in clips)
    return {
        "config": name,
        "provider": result["provider"].removesuffix("ExecutionProvider"),
        "load_seconds": result["load_seconds"],
        "median_seconds": statistics.median(clip["seconds"] for clip in clips),
        "speed": audio / seconds if seconds else 0.0,
        "peak_ram_mb": result["peak_ram"] / 2**20,
        "peak_vram_mb": None if result["peak_vram"] is None else result["peak_vram"] / 2**20,
        "wer": word_error_rate(
            [references[clip["clip"]] for clip in clips], [clip["hypothesis"] for clip in clips]
        ),
    }


def markdown_table(rows: list[dict], clip_count: int, audio_seconds: float) -> str:
    lines = [
        f"{clip_count} clips, {audio_seconds:.0f} s of audio. Speed is seconds of audio per second "
        "of transcription; higher is faster.",
        "",
        "| Setting | Ran on | Load (s) | Median per clip (s) | Speed (x real time) "
        "| Peak RAM (MB) | Peak VRAM (MB) | Word error rate |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        if "error" in row:
            lines.append(f"| {row['config']} | not run: {row['error']} | | | | | | |")
            continue
        vram = "" if row["peak_vram_mb"] is None else f"{row['peak_vram_mb']:.0f}"
        lines.append(
            f"| {row['config']} | {row['provider']} | {row['load_seconds']:.1f} "
            f"| {row['median_seconds']:.2f} | {row['speed']:.1f} | {row['peak_ram_mb']:.0f} "
            f"| {vram} | {row['wer']:.1%} |"
        )
    return "\n".join(lines)


def write_csv(path: Path, results: dict[str, dict], references: dict[str, str]) -> None:
    columns = [
        "config",
        "provider",
        "clip",
        "audio_seconds",
        "seconds",
        "speed",
        "wer",
        "reference",
        "hypothesis",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for name, result in results.items():
            for clip in result.get("clips", []):
                reference = references[clip["clip"]]
                writer.writerow(
                    {
                        "config": name,
                        "provider": result["provider"].removesuffix("ExecutionProvider"),
                        "clip": clip["clip"],
                        "audio_seconds": f"{clip['audio_seconds']:.2f}",
                        "seconds": f"{clip['seconds']:.3f}",
                        "speed": f"{clip['audio_seconds'] / clip['seconds']:.1f}",
                        "wer": f"{word_error_rate([reference], [clip['hypothesis']]):.3f}",
                        "reference": reference,
                        "hypothesis": clip["hypothesis"],
                    }
                )


def main() -> None:
    from looree.transcriber import SAMPLE_RATE

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("clips", nargs="?", type=Path, default=DEFAULT_CLIPS, help=f"default {DEFAULT_CLIPS}")
    parser.add_argument(
        "--configs",
        default=",".join(CONFIGS),
        help=f"comma-separated settings to run, from {', '.join(CONFIGS)} (default all)",
    )
    parser.add_argument("--model", default="nemo-parakeet-tdt-0.6b-v3")
    parser.add_argument("--repeats", type=int, default=3, help="times each clip is timed; the median is kept")
    parser.add_argument(
        "--raw",
        dest="clean",
        action="store_false",
        help="score the model's text as is, without removing fillers as Looree does by default",
    )
    parser.add_argument("--out", type=Path, help="folder for the results (default: the clips folder)")
    # Used when the script starts itself for one setting.
    parser.add_argument("--worker", choices=list(CONFIGS), help=argparse.SUPPRESS)
    parser.add_argument("--download", help=argparse.SUPPRESS)
    parser.add_argument("--result", help=argparse.SUPPRESS)
    parser.add_argument("--clip-paths", nargs="*", default=[], help=argparse.SUPPRESS)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if args.worker or args.download is not None:
        worker(args)
        return

    names = [name.strip() for name in args.configs.split(",") if name.strip()]
    unknown = [name for name in names if name not in CONFIGS]
    if unknown:
        parser.error(f"unknown setting {', '.join(unknown)}; choose from {', '.join(CONFIGS)}")
    if args.repeats < 1:
        parser.error("--repeats must be at least 1")

    clips = find_clips(args.clips)
    if not clips:
        sys.exit(f"No clips found in {args.clips}. Each WAV needs a .txt of the same name.")
    # Reading every clip now fails fast on a wrong sample rate, not halfway through.
    try:
        audio_seconds = sum(len(read_clip(clip.wav)) for clip in clips) / SAMPLE_RATE
    except ValueError as exc:
        sys.exit(str(exc))
    references = {clip.name: clip.reference for clip in clips}
    log.info("%d clips, %.0f s of audio", len(clips), audio_seconds)

    for quantization in sorted({CONFIGS[name][0] for name in names}):
        log.info("Checking the %s model is downloaded", quantization or "full precision")
        if spawn(["--model", args.model, "--download", quantization]).returncode:
            sys.exit("Could not download the model")

    results: dict[str, dict] = {}
    with tempfile.TemporaryDirectory() as scratch:
        for name in names:
            log.info("Running %s", name)
            result_file = Path(scratch) / f"{name}.json"
            arguments = ["--model", args.model, "--worker", name, "--result", str(result_file)]
            arguments += ["--repeats", str(args.repeats)] + ([] if args.clean else ["--raw"])
            arguments += ["--clip-paths", *(str(clip.wav) for clip in clips)]
            finished = spawn(arguments)
            if result_file.exists():
                results[name] = json.loads(result_file.read_text(encoding="utf-8"))
            else:
                results[name] = {"error": f"crashed with exit code {finished.returncode}"}
            if "error" in results[name]:
                log.warning("%s not run: %s", name, results[name]["error"])

    out = args.out or args.clips
    out.mkdir(parents=True, exist_ok=True)
    write_csv(out / "bench_results.csv", results, references)
    rows = [summarise(name, result, references) for name, result in results.items()]
    table = markdown_table(rows, len(clips), audio_seconds)
    (out / "bench_results.md").write_text(table + "\n", encoding="utf-8")
    print()
    print(table)
    print()
    ran = [row for row in rows if "error" not in row]
    if ran:
        print(f"Most accurate: {min(ran, key=lambda row: row['wer'])['config']}")
        print(f"Fastest: {max(ran, key=lambda row: row['speed'])['config']}")
    print(f"Per-clip results: {out / 'bench_results.csv'}")


if __name__ == "__main__":
    main()

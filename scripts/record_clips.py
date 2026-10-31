"""Record benchmark clips with Looree's own microphone code.

Usage: python scripts/record_clips.py [folder] [--prompts scripts/bench_prompts.txt] [--mic NAME]

Press Enter to start each clip and Enter again to stop. With --prompts, each line of
the file is shown to read aloud and saved as that clip's transcript; without it, you
type what you said after each clip. Clips are saved as 16 kHz mono WAV files with a
.txt beside each, ready for scripts/bench.py.
"""

import argparse
import logging
import wave
from pathlib import Path

import numpy as np

from looree.audio import Recorder
from looree.config import config_path, parse_config
from looree.transcriber import SAMPLE_RATE

DEFAULT_FOLDER = Path("samples") / "bench"


def save_wav(path: Path, samples: np.ndarray) -> None:
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(pcm.tobytes())


def next_number(folder: Path) -> int:
    numbers = [int(path.stem) for path in folder.glob("*.wav") if path.stem.isdigit()]
    return max(numbers, default=0) + 1


def configured_mic() -> str | int | None:
    """The microphone set in Looree's config.toml, so clips match real dictation."""
    path = config_path()
    if not path.exists():
        return None
    return parse_config(path.read_text(encoding="utf-8")).mic_device or None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "folder", nargs="?", type=Path, default=DEFAULT_FOLDER, help=f"default {DEFAULT_FOLDER}"
    )
    parser.add_argument("--prompts", type=Path, help="text file with one sentence per line to read aloud")
    parser.add_argument("--mic", help="part of a device name or an index (default: Looree's configured mic)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    prompts = None
    if args.prompts:
        prompts = [line.strip() for line in args.prompts.read_text(encoding="utf-8").splitlines()]
        prompts = [line for line in prompts if line and not line.startswith("#")]
    mic = args.mic if args.mic is not None else configured_mic()
    if isinstance(mic, str) and mic.isdigit():
        mic = int(mic)

    args.folder.mkdir(parents=True, exist_ok=True)
    recorder = Recorder(device=mic, keep_open=True)
    number = next_number(args.folder)
    remaining = list(prompts) if prompts is not None else None
    print("Press Ctrl+C to finish.\n")
    try:
        while remaining is None or remaining:
            prompt = remaining.pop(0) if remaining is not None else None
            if prompt:
                print(f"Read this: {prompt}")
            input(f"Clip {number:02d}: press Enter to start recording ")
            recorder.start()
            input("Recording... press Enter to stop ")
            samples = recorder.stop()
            if len(samples) < SAMPLE_RATE // 2:
                print("That was under half a second; try again.\n")
                if prompt:
                    remaining.insert(0, prompt)
                continue
            text = prompt or input("Type exactly what you said: ").strip()
            if not text:
                print("No transcript, so the clip was not kept.\n")
                continue
            save_wav(args.folder / f"{number:02d}.wav", samples)
            (args.folder / f"{number:02d}.txt").write_text(text + "\n", encoding="utf-8")
            print(f"Saved {number:02d}.wav ({len(samples) / SAMPLE_RATE:.1f} s)\n")
            number += 1
    except (KeyboardInterrupt, EOFError):
        print()
    finally:
        recorder.close()
    print(f"Clips are in {args.folder}")


if __name__ == "__main__":
    main()

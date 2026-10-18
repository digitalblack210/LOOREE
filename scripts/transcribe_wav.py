"""Transcribe a WAV file and print the text.

Usage: python scripts/transcribe_wav.py path/to/audio.wav [--device auto|cuda|cpu]
"""

import argparse
import logging
import time

from looree.transcriber import Transcriber


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("wav", help="path to a PCM WAV file")
    parser.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"])
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    transcriber = Transcriber(device=args.device)
    started = time.perf_counter()
    text = transcriber.transcribe_file(args.wav)
    logging.info("Transcribed in %.2fs", time.perf_counter() - started)
    print(text)


if __name__ == "__main__":
    main()

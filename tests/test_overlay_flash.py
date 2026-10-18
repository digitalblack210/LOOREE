"""Checks the glow against the WCAG 2.3.1 general flash threshold.

A flash is a pair of opposing changes in relative luminance of at least 10%
of the maximum, where the darker state is below 0.80. Content must not flash
more than three times in any one-second period.

The glow's brightest pixel (the one at the screen edge) is simulated over
black and white backgrounds. The microphone level jumps between silence and
full scale, which is harsher than real speech.
"""

import random

import pytest

from looree.config import Config
from looree.overlay import GlowTimeline

FRAME = 1 / 60
MAX_FLASHES_PER_SECOND = 3
QUIET, LOUD = 1e-4, 1.0  # RMS: below the level floor, and full scale
TRANSCRIBE_SECONDS = 0.1

COLORS = {"recording": Config.recording_color, "transcribing": Config.transcribing_color}
BACKGROUNDS = {"black": (0, 0, 0), "white": (255, 255, 255)}


def rgb(hex_color: str) -> tuple[int, int, int]:
    return tuple(int(hex_color[i : i + 2], 16) for i in (1, 3, 5))


def relative_luminance(color: tuple[float, float, float]) -> float:
    def linear(channel: float) -> float:
        c = channel / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (linear(c) for c in color)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def count_flashes(luminance: list[float]) -> int:
    """Largest number of flashes in any one-second window."""
    # Record each swing of at least 0.10 away from the running high or low.
    # Swings alternate in direction, so two of them make one flash.
    swings = []
    low = high = luminance[0]
    trend = 0
    for index, value in enumerate(luminance):
        if trend >= 0:
            high = max(high, value)
            if high - value >= 0.10 and value < 0.80:
                swings.append(index)
                trend, low = -1, value
                continue
        if trend <= 0:
            low = min(low, value)
            if value - low >= 0.10 and low < 0.80:
                swings.append(index)
                trend, high = 1, value
    window = round(1 / FRAME)
    return max((sum(start <= i < start + window for i in swings) // 2 for start in swings), default=0)


class Simulation:
    """Runs a GlowTimeline frame by frame and records the edge pixel's luminance."""

    def __init__(self, background: str) -> None:
        self.timeline = GlowTimeline()
        self.background = BACKGROUNDS[background]
        self.luminance: list[float] = []

    def run(self, seconds: float, rms=QUIET) -> None:
        for _ in range(round(seconds / FRAME)):
            shape = self.timeline.step(FRAME, rms() if callable(rms) else rms)
            alpha = shape[0] if shape else 0.0
            glow = rgb(COLORS[self.timeline.color_state])
            pixel = tuple(g * alpha + b * (1 - alpha) for g, b in zip(glow, self.background, strict=True))
            self.luminance.append(relative_luminance(pixel))

    @property
    def flashes(self) -> int:
        return count_flashes(self.luminance)


def test_count_flashes_detects_a_real_flasher():
    strobe = [0.9 if (frame // 5) % 2 else 0.0 for frame in range(120)]  # 6 Hz, full contrast
    assert count_flashes(strobe) > MAX_FLASHES_PER_SECOND


@pytest.mark.parametrize("background", BACKGROUNDS)
@pytest.mark.parametrize("hz", [0.5, 1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10, 15, 30])
def test_recording_with_level_jumping_at_fixed_rate(hz, background):
    sim = Simulation(background)
    sim.timeline.record()
    frame = iter(range(10**6))
    sim.run(6, lambda: LOUD if (next(frame) * FRAME * hz) % 1 < 0.5 else QUIET)
    assert sim.flashes <= MAX_FLASHES_PER_SECOND


@pytest.mark.parametrize("background", BACKGROUNDS)
@pytest.mark.parametrize("seed", range(20))
def test_recording_with_random_level(seed, background):
    rng = random.Random(seed)
    sim = Simulation(background)
    sim.timeline.record()
    for _ in range(80):
        sim.run(rng.uniform(0.02, 0.35), rng.choice((QUIET, LOUD, rng.random())))
    assert sim.flashes <= MAX_FLASHES_PER_SECOND


@pytest.mark.parametrize("background", BACKGROUNDS)
def test_long_transcription(background):
    sim = Simulation(background)
    sim.timeline.record()
    sim.run(1, LOUD)
    sim.timeline.transcribe()
    sim.run(10)
    assert sim.flashes <= MAX_FLASHES_PER_SECOND


@pytest.mark.parametrize("background", BACKGROUNDS)
@pytest.mark.parametrize("hz", [0.5, 0.7, 1, 1.5, 2, 2.5, 3, 4, 6, 8, 12, 20])
def test_hotkey_pressed_repeatedly(hz, background):
    """Tap or re-dictate at a fixed rate, held for half of each cycle, loud throughout."""
    sim = Simulation(background)
    hold = 0.5 / hz
    for _ in range(round(8 * hz)):
        sim.timeline.record()
        sim.run(hold, LOUD)
        if hold * 1000 >= Config.min_hold_ms:
            sim.timeline.transcribe()
            sim.run(min(TRANSCRIBE_SECONDS, hold))
            sim.timeline.fade_out()
            sim.run(hold - min(TRANSCRIBE_SECONDS, hold))
        else:
            sim.timeline.fade_out()
            sim.run(hold)
    assert sim.flashes <= MAX_FLASHES_PER_SECOND


@pytest.mark.parametrize("background", BACKGROUNDS)
@pytest.mark.parametrize("seed", range(20))
def test_hotkey_pressed_at_random(seed, background):
    rng = random.Random(seed)
    sim = Simulation(background)
    for _ in range(60):
        hold = rng.uniform(0.02, 0.6)
        sim.timeline.record()
        sim.run(hold, LOUD)
        if hold * 1000 >= Config.min_hold_ms:
            sim.timeline.transcribe()
            sim.run(rng.uniform(0.05, 0.3))
        sim.timeline.fade_out()
        sim.run(rng.uniform(0.02, 0.6))
    assert sim.flashes <= MAX_FLASHES_PER_SECOND


def test_steady_glow_only_fades_in_and_out():
    sim = Simulation("black")
    sim.timeline.animate = False
    sim.timeline.record()
    sim.run(3, LOUD)
    sim.timeline.transcribe()
    sim.run(3)
    held = sim.luminance[60:]  # after the fade-in
    sim.timeline.fade_out()
    sim.run(2)
    assert sim.timeline.state == "hidden"
    # Only the colour differs between the two states; nothing pulses.
    assert len({round(value, 6) for value in held}) == 2

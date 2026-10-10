"""Screen-edge glow shown while recording and transcribing."""

from __future__ import annotations

import math
import time
from collections.abc import Callable

from PySide6.QtCore import QRect, Qt, QTimer
from PySide6.QtGui import QColor, QGuiApplication, QLinearGradient, QPainter
from PySide6.QtWidgets import QWidget

from . import win32

FRAME_MS = 16
FADE_IN_SECONDS = 0.12
FADE_OUT_SECONDS = 0.35
MIN_VISIBLE_SECONDS = 0.5

# Microphone level range mapped onto 0..1, in dBFS.
LEVEL_FLOOR_DB = -50.0
LEVEL_CEILING_DB = -15.0
LEVEL_RISE_SECONDS = 0.2
LEVEL_FALL_SECONDS = 0.45


def smooth_level(level: float, rms: float, dt: float) -> float:
    """Move the displayed level (0..1) towards the microphone's RMS level.

    The smoothing is deliberately heavy. Speech rises and falls several times
    a second, and brightness must not follow it that fast: flashing above
    about three times a second can trigger photosensitive seizures. The glow
    swells with a phrase, not with each syllable.
    """
    db = 20 * math.log10(max(rms, 1e-6))
    target = min(1.0, max(0.0, (db - LEVEL_FLOOR_DB) / (LEVEL_CEILING_DB - LEVEL_FLOOR_DB)))
    tau = LEVEL_RISE_SECONDS if target > level else LEVEL_FALL_SECONDS
    return level + (target - level) * (1 - math.exp(-dt / tau))


def glow_shape(state: str, elapsed: float, level: float, animate: bool = True) -> tuple[float, float]:
    """Peak opacity (0..1) and reach (fraction of the thickness) of the glow."""
    if not animate:
        return 0.5, 0.6
    if state == "recording":
        pulse = 0.5 + 0.5 * math.sin(elapsed * 2 * math.pi / 2.0)
        return 0.4 + 0.12 * pulse + 0.3 * level, 0.45 + 0.08 * pulse + 0.47 * level
    # A quicker, even throb while the transcript is produced, kept dimmer than
    # the recording glow so the colour change is not a flash.
    pulse = 0.5 + 0.5 * math.sin(elapsed * 2 * math.pi / 0.9)
    return 0.35 + 0.2 * pulse, 0.55 + 0.15 * pulse


class GlowTimeline:
    """What the glow should look like from moment to moment.

    Holds no windows and no clock of its own, so the tests can run it through
    any sequence of events and measure the result.
    """

    def __init__(self, animate: bool = True) -> None:
        self.animate = animate
        self.state = "hidden"  # hidden, recording, transcribing or fading
        self.color_state = "recording"
        self._now = 0.0
        self._state_started = 0.0
        self._fade_after = 0.0
        self._level = 0.0
        self._fade = 0.0

    @property
    def visible(self) -> bool:
        return self.state != "hidden"

    def record(self) -> None:
        if not self.visible:
            self._level = 0.0
        # Once lit, the glow stays lit for a minimum time. Tapping the hotkey
        # rapidly then gives a steady glow instead of a flicker.
        self._fade_after = self._now + MIN_VISIBLE_SECONDS
        self._enter("recording")

    def transcribe(self) -> None:
        if self.visible:
            self._enter("transcribing")

    def fade_out(self) -> None:
        if self.visible:
            self.state = "fading"

    def _enter(self, state: str) -> None:
        self.state = self.color_state = state
        self._state_started = self._now

    def step(self, dt: float, rms: float) -> tuple[float, float] | None:
        """Advance by dt seconds. Returns (peak opacity, reach), or None once hidden."""
        if not self.visible:
            return None
        self._now += dt
        if self.state == "fading" and self._now >= self._fade_after:
            self._fade -= dt / FADE_OUT_SECONDS
            if self._fade <= 0:
                self._fade = 0.0
                self.state = "hidden"
                return None
        elif self.state != "fading":
            self._fade = min(1.0, self._fade + dt / FADE_IN_SECONDS)

        if self.color_state == "recording":
            self._level = smooth_level(self._level, rms, dt)
        elapsed = self._now - self._state_started
        alpha, reach = glow_shape(self.color_state, elapsed, self._level, self.animate)
        return min(1.0, alpha) * self._fade, reach

class _Strip(QWidget):
    """One edge of the glow: a click-through window that never takes focus."""

    def __init__(self, side: str) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.NoDropShadowWindowHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.side = side
        self.color = QColor()
        self.alpha = 0.0  # peak opacity at the screen edge, 0..1
        self.depth = 1.0  # how far the glow reaches into the strip, in pixels

    def _gradient(self, x1: float, y1: float, x2: float, y2: float) -> QLinearGradient:
        gradient = QLinearGradient(x1, y1, x2, y2)
        for step in range(7):
            t = step / 6
            color = QColor(self.color)
            color.setAlphaF(self.alpha * (1 - t) ** 2.4)
            gradient.setColorAt(t, color)
        return gradient

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        w, h, d = self.width(), self.height(), self.depth
        if self.side == "left":
            painter.fillRect(self.rect(), self._gradient(0, 0, d, 0))
        elif self.side == "right":
            painter.fillRect(self.rect(), self._gradient(w, 0, w - d, 0))
        else:
            # Top and bottom span the full width and also carry the ends of the
            # side glows, so the four strips meet without a seam at the corners.
            if self.side == "top":
                painter.fillRect(self.rect(), self._gradient(0, 0, 0, d))
            else:
                painter.fillRect(self.rect(), self._gradient(0, h, 0, h - d))
            painter.fillRect(QRect(0, 0, h, h), self._gradient(0, 0, d, 0))
            painter.fillRect(QRect(w - h, 0, h, h), self._gradient(w, 0, w - d, 0))


class Overlay:
    """Shows a GlowTimeline on the edges of the screen with the focused window."""

    def __init__(
        self,
        get_level: Callable[[], float],
        recording_color: str,
        transcribing_color: str,
        thickness: int,
        animate: bool = True,
    ) -> None:
        self._get_level = get_level
        self._timeline = GlowTimeline(animate)
        self._colors = {
            "recording": QColor(recording_color),
            "transcribing": QColor(transcribing_color),
        }
        for name, fallback in (("recording", "#34C98E"), ("transcribing", "#8C9BFF")):
            if not self._colors[name].isValid():
                self._colors[name] = QColor(fallback)
        self._thickness = max(20, int(thickness))
        self._strips = {side: _Strip(side) for side in ("top", "bottom", "left", "right")}
        self._last_tick = 0.0
        self._timer = QTimer()
        self._timer.setInterval(FRAME_MS)
        self._timer.timeout.connect(self._tick)

    def show_recording(self) -> None:
        if not self._timeline.visible:
            self._place_on_focused_screen()
            self._last_tick = time.monotonic()
            for strip in self._strips.values():
                strip.alpha = 0.0
                strip.show()
            self._timer.start()
        self._timeline.record()

    def show_transcribing(self) -> None:
        self._timeline.transcribe()

    def fade_out(self) -> None:
        self._timeline.fade_out()

    def close(self) -> None:
        self._timer.stop()
        for strip in self._strips.values():
            strip.close()

    def _place_on_focused_screen(self) -> None:
        name = win32.foreground_monitor_name()
        screens = QGuiApplication.screens()
        screen = next((s for s in screens if s.name() == name), QGuiApplication.primaryScreen())
        area, t = screen.geometry(), self._thickness
        rects = {
            "top": QRect(area.left(), area.top(), area.width(), t),
            "bottom": QRect(area.left(), area.bottom() + 1 - t, area.width(), t),
            "left": QRect(area.left(), area.top() + t, t, area.height() - 2 * t),
            "right": QRect(area.right() + 1 - t, area.top() + t, t, area.height() - 2 * t),
        }
        for side, strip in self._strips.items():
            strip.setScreen(screen)
            strip.setGeometry(rects[side])

    def _tick(self) -> None:
        now = time.monotonic()
        dt, self._last_tick = now - self._last_tick, now
        shape = self._timeline.step(dt, self._get_level())
        if shape is None:
            self._timer.stop()
            for strip in self._strips.values():
                strip.hide()
            return
        alpha, reach = shape
        for strip in self._strips.values():
            strip.color = self._colors[self._timeline.color_state]
            strip.alpha = alpha
            strip.depth = max(1.0, self._thickness * reach)
            strip.update()

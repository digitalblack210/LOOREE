"""Wires the hotkey, microphone, transcriber, overlay and tray icon together."""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from collections.abc import Callable
from importlib import resources

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QAction, QIcon, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from . import autostart, cleanup
from .audio import Recorder
from .clipboard import paste_text
from .config import Config, ensure_config_file, load_config, log_dir
from .hotkey import HotkeyListener
from .overlay import Overlay
from .transcriber import SAMPLE_RATE, Transcriber

log = logging.getLogger(__name__)

# While recording, how often to confirm the hotkey is still physically held, and
# how many misses in a row count as a release.
HOLD_CHECK_MS = 200
MISSED_CHECKS_BEFORE_RELEASE = 3


def load_icon() -> QIcon:
    pixmap = QPixmap()
    pixmap.loadFromData(resources.files("turaco").joinpath("assets/turaco.png").read_bytes())
    return QIcon(pixmap)


class App(QObject):
    # Emitted from the hook and worker threads; Qt delivers them on the main thread.
    hotkey_event = Signal(str, float)
    model_loaded = Signal(object, int)
    model_failed = Signal(str, int)
    transcription_done = Signal()

    def __init__(self, qt_app: QApplication) -> None:
        super().__init__()
        self._qt_app = qt_app
        self.config = Config()
        self.transcriber: Transcriber | None = None
        self.recorder: Recorder | None = None
        self.hotkey: HotkeyListener | None = None
        self.overlay: Overlay | None = None
        self._pressed_at: float | None = None
        self._missed_checks = 0
        # Counts reloads, so a model that finishes loading after a newer reload is dropped.
        self._generation = 0
        self._hold_watchdog = QTimer(self)
        self._hold_watchdog.setInterval(HOLD_CHECK_MS)
        self._hold_watchdog.timeout.connect(self._check_hold)
        self._jobs: queue.Queue[Callable[[], None]] = queue.Queue()

        self.hotkey_event.connect(self._on_hotkey_event)
        self.model_loaded.connect(self._on_model_loaded)
        self.model_failed.connect(self._on_model_failed)
        self.transcription_done.connect(self._on_transcription_done)

        self._build_tray()
        threading.Thread(target=self._worker_loop, name="turaco-worker", daemon=True).start()
        self.reload()

    # --- tray -------------------------------------------------------------

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(load_icon())
        menu = QMenu()
        self._status_action = QAction("Starting…")
        self._status_action.setEnabled(False)
        menu.addAction(self._status_action)
        menu.addSeparator()
        menu.addAction("Open config").triggered.connect(lambda: os.startfile(ensure_config_file()))
        menu.addAction("Open log folder").triggered.connect(lambda: os.startfile(log_dir()))
        menu.addAction("Reload config and model").triggered.connect(self.reload)
        autostart_action = menu.addAction("Start with Windows")
        autostart_action.setCheckable(True)
        autostart_action.setChecked(autostart.is_enabled())
        autostart_action.toggled.connect(autostart.set_enabled)
        menu.addSeparator()
        menu.addAction("Quit").triggered.connect(self.quit)
        self._menu = menu
        self.tray.setContextMenu(menu)
        self.tray.show()

    def _set_status(self, text: str) -> None:
        self._status_action.setText(text)
        self.tray.setToolTip(f"Turaco: {text}")

    def _notify_problem(self, text: str) -> None:
        log.error(text)
        self.tray.showMessage("Turaco", text, QSystemTrayIcon.MessageIcon.Warning)

    # --- setup ------------------------------------------------------------

    def reload(self) -> None:
        """(Re)read config.toml and rebuild everything that depends on it."""
        self._teardown()
        self.config = config = load_config()
        self._set_status("Loading model…")

        try:
            device = config.mic_device if config.mic_device != "" else None
            self.recorder = Recorder(device=device, keep_open=config.mic_keep_open)
        except Exception as exc:
            self._notify_problem(f"Microphone unavailable: {exc}")

        if config.overlay_enabled:
            self.overlay = Overlay(
                get_level=lambda: self.recorder.level if self.recorder else 0.0,
                recording_color=config.recording_color,
                transcribing_color=config.transcribing_color,
                thickness=config.overlay_thickness,
                animate=config.overlay_animate,
            )

        callbacks = {
            f"on_{kind}": (lambda kind=kind: self.hotkey_event.emit(kind, time.monotonic()))
            for kind in ("press", "release", "cancel")
        }
        try:
            self.hotkey = HotkeyListener(config.hotkey, **callbacks)
        except ValueError as exc:
            self._notify_problem(f"Bad hotkey in config, using ctrl+win: {exc}")
            self.hotkey = HotkeyListener(Config.hotkey, **callbacks)
        try:
            self.hotkey.start()
        except OSError as exc:
            self._notify_problem(f"Could not install the keyboard hook: {exc}")

        self._generation += 1
        generation = self._generation
        self._jobs.put(lambda: self._load_model(config, generation))

    def _teardown(self) -> None:
        self._hold_watchdog.stop()
        self._pressed_at = None
        self.transcriber = None
        if self.hotkey:
            self.hotkey.stop()
            self.hotkey = None
        if self.recorder:
            self.recorder.close()
            self.recorder = None
        if self.overlay:
            self.overlay.close()
            self.overlay = None

    def quit(self) -> None:
        self._teardown()
        self.tray.hide()
        self._qt_app.quit()

    # --- worker thread ----------------------------------------------------

    def _worker_loop(self) -> None:
        while True:
            job = self._jobs.get()
            try:
                job()
            except Exception:
                log.exception("Worker job failed")

    def _load_model(self, config: Config, generation: int) -> None:
        try:
            transcriber = Transcriber(
                model=config.model,
                quantization=config.quantization or None,
                device=config.model_device,
            )
        except Exception as exc:
            log.exception("Model load failed")
            self.model_failed.emit(str(exc), generation)
            return
        self.model_loaded.emit(transcriber, generation)

    def _transcribe_and_paste(self, transcriber: Transcriber, samples, config: Config) -> None:
        try:
            started = time.perf_counter()
            text = transcriber.transcribe(samples)
            # The transcript itself is never logged.
            log.info(
                "Transcribed %.1fs of audio in %.2fs (%d characters)",
                len(samples) / SAMPLE_RATE,
                time.perf_counter() - started,
                len(text),
            )
            if config.remove_fillers:
                text = cleanup.remove_fillers(text)
        finally:
            self.transcription_done.emit()
        text = cleanup.single_line(text)
        if text:
            if config.trailing_space:
                text += " "
            paste_text(text, config.restore_delay_ms / 1000)

    # --- main thread handlers ---------------------------------------------

    def _on_model_loaded(self, transcriber: Transcriber, generation: int) -> None:
        if generation != self._generation:
            return
        self.transcriber = transcriber
        provider = transcriber.provider.removesuffix("ExecutionProvider")
        self._set_status(f"Ready ({provider}). Hold {self.config.hotkey} to dictate")
        log.info("Ready. Hold %s to dictate.", self.config.hotkey)

    def _on_model_failed(self, message: str, generation: int) -> None:
        if generation != self._generation:
            return
        self._set_status("Model failed to load")
        self._notify_problem(f"Model failed to load: {message}")

    def _on_transcription_done(self) -> None:
        # A new recording may already be on screen; leave that one alone.
        if self.overlay and self._pressed_at is None:
            self.overlay.fade_out()

    def _check_hold(self) -> None:
        """End a recording whose key release never arrived, or that ran too long.

        The hook cannot see keys while an administrator window or the lock
        screen has focus, so a release there would otherwise record forever.
        """
        if self._pressed_at is None:
            self._hold_watchdog.stop()
            return
        now = time.monotonic()
        if now - self._pressed_at >= self.config.max_hold_seconds:
            log.warning("Recording stopped at the %d second limit", self.config.max_hold_seconds)
        elif self.hotkey and not self.hotkey.is_held():
            self._missed_checks += 1
            if self._missed_checks < MISSED_CHECKS_BEFORE_RELEASE:
                return
            log.warning("Recording stopped: the hotkey was released without Turaco seeing it")
            self.hotkey.reset()
        else:
            self._missed_checks = 0
            return
        self._on_hotkey_event("release", now)

    def _on_hotkey_event(self, kind: str, at: float) -> None:
        if kind == "press":
            if self.transcriber is None or self.recorder is None:
                log.info("Hotkey ignored: not ready yet")
                return
            try:
                self.recorder.start()
            except Exception as exc:
                self._notify_problem(f"Could not start recording: {exc}")
                return
            self._pressed_at = at
            self._missed_checks = 0
            self._hold_watchdog.start()
            if self.overlay:
                self.overlay.show_recording()
            return

        if self._pressed_at is None:
            return
        held = at - self._pressed_at
        self._pressed_at = None
        self._hold_watchdog.stop()
        samples = self.recorder.stop()
        if kind == "cancel":
            log.info("Cancelled (another key was pressed)")
        elif held * 1000 < self.config.min_hold_ms:
            log.info("Ignored %.0f ms tap", held * 1000)
        elif samples.size == 0:
            log.warning("The microphone delivered no audio")
        else:
            if self.overlay:
                self.overlay.show_transcribing()
            transcriber, config = self.transcriber, self.config
            self._jobs.put(lambda: self._transcribe_and_paste(transcriber, samples, config))
            return
        if self.overlay:
            self.overlay.fade_out()

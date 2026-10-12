"""Loads settings from config.toml in the user's application data folder."""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

log = logging.getLogger(__name__)


def config_dir() -> Path:
    return Path(os.environ.get("APPDATA") or Path.home()) / "Turaco"


def log_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Turaco"


def config_path() -> Path:
    return config_dir() / "config.toml"


@dataclass
class Config:
    hotkey: str = "ctrl+win"
    min_hold_ms: int = 250
    max_hold_seconds: int = 600
    mic_device: str | int = ""
    mic_keep_open: bool = True
    model: str = "nemo-parakeet-tdt-0.6b-v3"
    quantization: str = "int8"
    model_device: str = "cpu"
    overlay_enabled: bool = True
    recording_color: str = "#34C98E"
    transcribing_color: str = "#8C9BFF"
    overlay_thickness: int = 90
    overlay_animate: bool = True
    trailing_space: bool = True
    restore_delay_ms: int = 300
    remove_fillers: bool = True
    vocabulary: dict[str, list[str]] = field(default_factory=dict)   # wanted word -> phrases the model mishears it as


# (section, key) in config.toml -> Config field
_KEYS = {
    ("hotkey", "keys"): "hotkey",
    ("hotkey", "min_hold_ms"): "min_hold_ms",
    ("hotkey", "max_hold_seconds"): "max_hold_seconds",
    ("microphone", "device"): "mic_device",
    ("microphone", "keep_open"): "mic_keep_open",
    ("model", "name"): "model",
    ("model", "quantization"): "quantization",
    ("model", "device"): "model_device",
    ("overlay", "enabled"): "overlay_enabled",
    ("overlay", "recording_color"): "recording_color",
    ("overlay", "transcribing_color"): "transcribing_color",
    ("overlay", "thickness"): "overlay_thickness",
    ("overlay", "animate"): "overlay_animate",
    ("paste", "trailing_space"): "trailing_space",
    ("paste", "restore_delay_ms"): "restore_delay_ms",
    ("cleanup", "remove_fillers"): "remove_fillers",
}


# Numbers outside these ranges are pulled back to the nearest end.
_LIMITS = {
    "min_hold_ms": (0, 5000),
    "max_hold_seconds": (5, 3600),
    "overlay_thickness": (20, 400),
    "restore_delay_ms": (0, 5000),
}


def default_config_text() -> str:
    return resources.files("turaco").joinpath("default_config.toml").read_text(encoding="utf-8")


def ensure_config_file() -> Path:
    """Create config.toml from the bundled defaults on first run."""
    path = config_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(default_config_text(), encoding="utf-8")
        log.info("Created %s", path)
    return path


def _parse_hears(value) -> dict[str, list[str]]:
    """[vocabulary.hears]: Acme = ["ack me", "ak me"]. Entries that are not a word with a list of phrases are skipped."""
    hears: dict[str, list[str]] = {}
    if not isinstance(value, dict):
        log.warning("config: [vocabulary.hears] must be a table of word = [phrases], ignoring it")
        return hears
    for wanted, phrases in value.items():
        if isinstance(phrases, list) and all(isinstance(phrase, str) for phrase in phrases):
            hears[wanted] = [phrase for phrase in phrases if phrase.strip()]
        else:
            log.warning("config: [vocabulary.hears] %s should be a list of phrases, skipping it", wanted)
    return hears


def parse_config(text: str) -> Config:
    """Build a Config from TOML text. Bad or unknown entries keep their defaults."""
    config = Config()
    for section, values in tomllib.loads(text).items():
        if not isinstance(values, dict):
            log.warning("config: ignoring top-level key %r", section)
            continue
        for key, value in values.items():
            if (section, key) == ("vocabulary", "hears"):
                config.vocabulary = _parse_hears(value)
                continue
            field = _KEYS.get((section, key))
            if field is None:
                log.warning("config: unknown setting [%s] %s", section, key)
                continue
            expected = type(getattr(config, field))
            accepted = (str, int) if field == "mic_device" else expected
            # bool is an int subclass; do not let true/false pass as a number.
            if not isinstance(value, accepted) or (expected is int and isinstance(value, bool)):
                log.warning("config: [%s] %s has the wrong type, using the default", section, key)
                continue
            setattr(config, field, value)
    for field, (low, high) in _LIMITS.items():
        value = getattr(config, field)
        if not low <= value <= high:
            log.warning("config: %s = %s is out of range, using %s", field, value, min(max(value, low), high))
            setattr(config, field, min(max(value, low), high))
    return config


def load_config() -> Config:
    try:
        return parse_config(ensure_config_file().read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        log.error("Could not read config.toml (%s); using defaults", exc)
        return Config()

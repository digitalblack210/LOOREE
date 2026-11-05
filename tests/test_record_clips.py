"""scripts/record_clips.py records from the microphone the app would use."""

import importlib.util
import sys
from pathlib import Path

import pytest


def _load_record_clips():
    try:
        import looree.audio  # noqa: F401
    except OSError:
        # sounddevice bundles PortAudio on Windows; elsewhere it may not be installed.
        pytest.skip("PortAudio is not installed")
    path = Path(__file__).resolve().parent.parent / "scripts" / "record_clips.py"
    spec = importlib.util.spec_from_file_location("looree_record_clips", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("setting", "device"),
    [
        ("0", 0),  # an index, even the first one
        ('"1"', "1"),  # a string is part of a device name, as in the app
        ('"USB"', "USB"),
        ('""', None),  # the system default
    ],
)
def test_configured_microphone_is_the_one_the_app_opens(tmp_path, monkeypatch, setting, device):
    record_clips = _load_record_clips()
    config = tmp_path / "config.toml"
    config.write_text(f"[microphone]\ndevice = {setting}\n", encoding="utf-8")
    monkeypatch.setattr(record_clips, "config_path", lambda: config)
    assert record_clips.configured_mic() == device


def test_no_config_file_means_the_system_default(tmp_path, monkeypatch):
    record_clips = _load_record_clips()
    monkeypatch.setattr(record_clips, "config_path", lambda: tmp_path / "missing.toml")
    assert record_clips.configured_mic() is None

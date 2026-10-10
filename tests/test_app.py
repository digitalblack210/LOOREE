"""The recording watchdog, driven directly with stand-ins for the app's parts."""

import time
from types import SimpleNamespace

from turaco.app import MISSED_CHECKS_BEFORE_RELEASE, App
from turaco.config import Config


def make_app(held: bool, pressed_seconds_ago: float = 1.0):
    events = []
    app = SimpleNamespace(
        _pressed_at=time.monotonic() - pressed_seconds_ago,
        _missed_checks=0,
        config=Config(),
        _hold_watchdog=SimpleNamespace(stop=lambda: events.append("watchdog stopped")),
        hotkey=SimpleNamespace(is_held=lambda: held, reset=lambda: events.append("hotkey reset")),
        _on_hotkey_event=lambda kind, at: events.append(kind),
    )
    return app, events


def test_nothing_happens_while_the_keys_are_held():
    app, events = make_app(held=True)
    for _ in range(10):
        App._check_hold(app)
    assert events == []


def test_a_missed_release_ends_the_recording_after_a_few_checks():
    app, events = make_app(held=False)
    for _ in range(MISSED_CHECKS_BEFORE_RELEASE - 1):
        App._check_hold(app)
    assert events == []
    App._check_hold(app)
    assert events == ["hotkey reset", "release"]


def test_one_bad_reading_is_forgiven():
    app, events = make_app(held=False)
    App._check_hold(app)
    app.hotkey.is_held = lambda: True
    App._check_hold(app)
    assert app._missed_checks == 0
    assert events == []


def test_recording_ends_at_the_length_limit_even_if_keys_are_held():
    app, events = make_app(held=True, pressed_seconds_ago=Config.max_hold_seconds + 1)
    App._check_hold(app)
    assert events == ["release"]


def test_watchdog_stops_itself_when_nothing_is_recording():
    app, events = make_app(held=True)
    app._pressed_at = None
    App._check_hold(app)
    assert events == ["watchdog stopped"]

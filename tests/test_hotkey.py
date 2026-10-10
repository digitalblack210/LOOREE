import pytest

from turaco import hotkey
from turaco.hotkey import HotkeyListener, parse_hotkey

LCTRL, RCTRL, LWIN, KEY_D = 0xA2, 0xA3, 0x5B, 0x44


@pytest.fixture
def listener(monkeypatch):
    """A listener fed key events directly, with no hook and no injected keys."""
    held: set[int] = set()
    sent = []
    monkeypatch.setattr(hotkey.win32, "is_key_down", lambda vk: vk in held)
    monkeypatch.setattr(hotkey.win32, "send_keys", lambda *events: sent.append(events))
    events = []
    made = HotkeyListener(
        "ctrl+win",
        on_press=lambda: events.append("press"),
        on_release=lambda: events.append("release"),
        on_cancel=lambda: events.append("cancel"),
    )

    def key(vk: int, down: bool) -> None:
        made._handle(vk, down)
        if down:
            held.add(vk)
        else:
            held.discard(vk)

    made.key, made.events, made.sent, made.held = key, events, sent, held
    return made


def test_parse_hotkey():
    assert parse_hotkey("Ctrl + Win") == [hotkey.KEY_GROUPS["ctrl"], hotkey.KEY_GROUPS["win"]]
    assert parse_hotkey("f9") == [frozenset({0x78})]
    with pytest.raises(ValueError):
        parse_hotkey("ctrl+banana")
    with pytest.raises(ValueError):
        parse_hotkey("")


def test_press_and_release(listener):
    listener.key(LCTRL, True)
    assert listener.events == []
    listener.key(LWIN, True)
    assert listener.events == ["press"]
    listener.key(LWIN, False)
    listener.key(LCTRL, False)
    assert listener.events == ["press", "release"]


def test_either_order_and_either_side(listener):
    listener.key(LWIN, True)
    listener.key(RCTRL, True)
    listener.key(RCTRL, False)
    assert listener.events == ["press", "release"]


def test_start_menu_is_masked_while_win_is_held(listener):
    listener.key(LCTRL, True)
    listener.key(LWIN, True)
    assert listener.sent == [((hotkey.MASK_VK, True), (hotkey.MASK_VK, False))]


def test_auto_repeat_does_not_retrigger(listener):
    listener.key(LCTRL, True)
    listener.key(LWIN, True)
    for _ in range(5):
        listener.key(LWIN, True)
        listener.key(LCTRL, True)
    assert listener.events == ["press"]


def test_another_key_cancels_and_does_not_rearm(listener):
    listener.key(LCTRL, True)
    listener.key(LWIN, True)
    listener.key(KEY_D, True)
    listener.key(KEY_D, False)
    listener.key(LWIN, True)  # auto-repeat of the key still held
    listener.key(LWIN, False)
    listener.key(LCTRL, False)
    assert listener.events == ["press", "cancel"]


def test_other_keys_alone_do_nothing(listener):
    listener.key(KEY_D, True)
    listener.key(KEY_D, False)
    listener.key(LWIN, True)
    listener.key(LWIN, False)
    assert listener.events == []


def test_missed_key_up_does_not_leave_a_key_stuck(listener):
    listener.key(LCTRL, True)
    listener.held.discard(LCTRL)  # released where the hook could not see it
    listener.key(LWIN, True)
    assert listener.events == []


def test_is_held_follows_the_physical_keys(listener):
    assert not listener.is_held()
    listener.key(LCTRL, True)
    assert not listener.is_held()
    listener.key(LWIN, True)
    assert listener.is_held()
    listener.held.discard(LWIN)
    assert not listener.is_held()


def test_reset_allows_a_fresh_press(listener):
    listener.key(LCTRL, True)
    listener.key(LWIN, True)
    listener.held.clear()  # both released where the hook could not see
    listener.reset()
    listener.key(LCTRL, True)
    listener.key(LWIN, True)
    assert listener.events == ["press", "press"]

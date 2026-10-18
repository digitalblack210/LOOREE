from looree.config import Config, default_config_text, parse_config


def test_bundled_config_matches_the_defaults():
    config = parse_config(default_config_text())
    assert config == Config()


def test_empty_config_gives_the_defaults():
    assert parse_config("") == Config()


def test_settings_are_read():
    config = parse_config(
        """
        [hotkey]
        keys = "ctrl+alt"
        [microphone]
        device = "Headset"
        keep_open = false
        [overlay]
        recording_color = "#112233"
        animate = false
        """
    )
    assert config.hotkey == "ctrl+alt"
    assert config.mic_device == "Headset"
    assert config.mic_keep_open is False
    assert config.recording_color == "#112233"
    assert config.overlay_animate is False


def test_microphone_device_may_be_an_index():
    assert parse_config("[microphone]\ndevice = 3").mic_device == 3


def test_bad_entries_keep_their_defaults():
    config = parse_config(
        """
        stray = 1
        [hotkey]
        keys = 5
        min_hold_ms = true
        unknown = "x"
        [nonsense]
        a = 1
        [paste]
        restore_delay_ms = 500
        """
    )
    assert config.hotkey == Config.hotkey
    assert config.min_hold_ms == Config.min_hold_ms
    assert config.restore_delay_ms == 500


def test_out_of_range_numbers_are_pulled_back():
    config = parse_config(
        """
        [hotkey]
        min_hold_ms = -5
        max_hold_seconds = 999999
        [overlay]
        thickness = 100000
        [paste]
        restore_delay_ms = -1
        """
    )
    assert config.min_hold_ms == 0
    assert config.max_hold_seconds == 3600
    assert config.overlay_thickness == 400
    assert config.restore_delay_ms == 0

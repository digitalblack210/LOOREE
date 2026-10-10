import numpy as np

from turaco.transcriber import CHUNK_SECONDS, SAMPLE_RATE, split_at_pauses


def speech_with_pauses(seconds: int, pause_every: float = 7.0) -> np.ndarray:
    """Noise standing in for speech, with a 0.3 s silence every few seconds."""
    rng = np.random.default_rng(0)
    audio = rng.uniform(-0.3, 0.3, seconds * SAMPLE_RATE).astype(np.float32)
    for start in np.arange(pause_every, seconds, pause_every):
        begin = int(start * SAMPLE_RATE)
        audio[begin : begin + int(0.3 * SAMPLE_RATE)] = 0.0
    return audio


def test_short_audio_is_not_split():
    audio = speech_with_pauses(20)
    chunks = split_at_pauses(audio)
    assert len(chunks) == 1
    assert np.array_equal(chunks[0], audio)


def test_empty_audio():
    assert [len(chunk) for chunk in split_at_pauses(np.zeros(0, dtype=np.float32))] == [0]


def test_long_audio_is_split_without_losing_samples():
    audio = speech_with_pauses(200)
    chunks = split_at_pauses(audio)
    assert len(chunks) > 1
    assert np.array_equal(np.concatenate(chunks), audio)
    assert all(SAMPLE_RATE <= len(chunk) <= (CHUNK_SECONDS + 1) * SAMPLE_RATE for chunk in chunks)


def test_cuts_land_in_pauses():
    audio = speech_with_pauses(200)
    position = 0
    for chunk in split_at_pauses(audio)[:-1]:
        position += len(chunk)
        assert not audio[position - 400 : position + 400].any()


def test_audio_with_no_pause_is_still_split():
    audio = np.full(100 * SAMPLE_RATE, 0.2, dtype=np.float32)
    chunks = split_at_pauses(audio)
    assert sum(len(chunk) for chunk in chunks) == len(audio)
    assert max(len(chunk) for chunk in chunks) <= (CHUNK_SECONDS + 1) * SAMPLE_RATE

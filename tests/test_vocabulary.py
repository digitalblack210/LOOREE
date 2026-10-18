import pytest

from looree.cleanup import apply_vocabulary
from looree.config import default_config_text, parse_config

HEARS = {"Acme": ["ack me", "ak me", "akmee"]}


@pytest.mark.parametrize(
    ("heard", "fixed"),
    [
        ("Ack me is a wolf.", "Acme is a wolf."),
        ("I like acme a lot.", "I like Acme a lot."),
        ("The ak me game.", "The Acme game."),
        ("Ak   me's wings.", "Acme's wings."),
        ("AKMEE!", "Acme!"),
        ("akmee and ack me", "Acme and Acme"),
        ("ack-me", "Acme"),
    ],
)
def test_misheard_words_are_replaced(heard, fixed):
    assert apply_vocabulary(heard, HEARS) == fixed


@pytest.mark.parametrize(
    "text",
    ["I back me up.", "A black me.", "Reakmee", "akmees", "nothing to change here."],
)
def test_other_words_are_left_alone(text):
    assert apply_vocabulary(text, HEARS) == text


def test_no_vocabulary_changes_nothing():
    assert apply_vocabulary("ack me", {}) == "ack me"


def test_config_reads_the_hears_table():
    text = '[vocabulary.hears]\nAcme = ["ack me", "ak me"]\nBad = "bad"\n'
    assert parse_config(text).vocabulary == {"Acme": ["ack me", "ak me"]}

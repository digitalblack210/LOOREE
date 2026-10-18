import pytest

from looree.cleanup import remove_fillers, single_line


@pytest.mark.parametrize(
    ("spoken", "cleaned"),
    [
        ("Um I think the green is pleasant.", "I think the green is pleasant."),
        ("Um, I think so.", "I think so."),
        ("So, uh, yes.", "So, yes."),
        ("I think I'll let uh Codex have a go.", "I think I'll let Codex have a go."),
        ("It burns a bit. Uh it's not that bad.", "It burns a bit. It's not that bad."),
        ("So I I like it a lot.", "So I like it a lot."),
        ("Erm, to to be fair, and and so on.", "To be fair, and so on."),
        ("Well, um.", "Well."),
        ("Bit. Uh. It's fine.", "Bit. It's fine."),
        ("Um.", ""),
        ("UM OKAY", "OKAY"),
    ],
)
def test_fillers_and_stutters_are_removed(spoken, cleaned):
    assert remove_fillers(spoken) == cleaned


@pytest.mark.parametrize(
    "text",
    [
        "Her umbrella, uh-oh, summer.",
        "I said that that was fine.",
        "He had had enough.",
        "The error was in the buffer.",
        "Use e.g. the first one.",
        "",
    ],
)
def test_ordinary_text_is_left_alone(text):
    assert remove_fillers(text) == text


@pytest.mark.parametrize(
    ("text", "flat"),
    [
        ("One line.", "One line."),
        ("First line.\nshutdown now\r\n", "First line. shutdown now"),
        ("Tabs\tand\x00control\x1bcharacters", "Tabs and control characters"),
        ("  spaced   out  ", "spaced out"),
        ("Café ✓ naïve", "Café ✓ naïve"),
        ("\n\n", ""),
    ],
)
def test_pasted_text_is_always_one_line(text, flat):
    assert single_line(text) == flat

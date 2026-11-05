"""scripts/bench.py: scoring, settings, the report, and the timing and failure paths with stand-ins.

No model is loaded and nothing here needs the bench extras (psutil, nvidia-ml-py).
"""

import argparse
import csv
import importlib.util
import json
import re
import subprocess
import sys
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import looree.transcriber as transcriber_module


def _load_bench():
    path = Path(__file__).resolve().parent.parent / "scripts" / "bench.py"
    spec = importlib.util.spec_from_file_location("looree_bench", path)
    module = importlib.util.module_from_spec(spec)
    # Registered before running it: @dataclass looks the module up by name.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bench = _load_bench()


def write_wav(path: Path, samples: np.ndarray, rate: int = 16000, channels: int = 1) -> None:
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm.tobytes())


def make_clip(folder: Path, name: str, text: str, seconds: float = 1.0) -> Path:
    wav = folder / f"{name}.wav"
    write_wav(wav, np.full(int(seconds * 16000), 0.1, dtype=np.float32))
    (folder / f"{name}.txt").write_text(text + "\n", encoding="utf-8")
    return wav


# --- word error rate ------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Hello, World!", ["hello", "world"]),
        ("push-to-talk", ["push", "to", "talk"]),
        ("two–three—four", ["two", "three", "four"]),
        ("I'll be there.", ["ill", "be", "there"]),
        ("“Quoted” … done", ["quoted", "done"]),
        ("  spaced \t out\n", ["spaced", "out"]),
        ("Ninety eight, 98.", ["ninety", "eight", "98"]),
        ("...", []),
        ("", []),
    ],
)
def test_words_ignore_case_and_punctuation(text, expected):
    assert bench.words(text) == expected


@pytest.mark.parametrize(
    ("reference", "hypothesis", "errors"),
    [
        ("a b c", "a b c", 0),
        ("a b c", "a x c", 1),  # substitution
        ("a b c", "a c", 1),  # deletion
        ("a c", "a b c", 1),  # insertion
        ("a b c", "", 3),
        ("", "a b", 2),
        ("", "", 0),
        ("k i t t e n", "s i t t i n g", 3),
    ],
)
def test_word_errors_count_edits(reference, hypothesis, errors):
    assert bench.word_errors(reference.split(), hypothesis.split()) == errors


def test_word_error_rate_is_over_all_words_not_a_mean_of_clips():
    references = ["one two three four", "five"]
    hypotheses = ["one two three four", ""]
    # One word wrong out of five; the mean of the two clips' rates would be 50%.
    assert bench.word_error_rate(references, hypotheses) == pytest.approx(0.2)


def test_word_error_rate_ignores_capitals_punctuation_and_hyphens():
    assert bench.word_error_rate(["Push-to-talk works, mostly."], ["push to talk works mostly"]) == 0.0


def test_empty_transcript_counts_every_word_as_missed():
    # The old stand-in for an empty transcript was a word that could match this reference.
    assert bench.word_error_rate(["the bin is empty"], [""]) == 1.0
    assert bench.word_error_rate(["okay"], ["."]) == 1.0


def test_word_error_rate_refuses_what_it_cannot_score():
    with pytest.raises(ValueError):
        bench.word_error_rate(["one", "two"], ["one"])
    with pytest.raises(ValueError):
        bench.word_error_rate(["..."], ["anything"])


# --- settings -------------------------------------------------------------


def test_cpu_settings_run_on_the_cpu_only():
    names = bench.parse_settings("int8-cpu,full-cpu")
    assert names == ["int8-cpu", "full-cpu"]
    assert {bench.CONFIGS[name][1] for name in names} == {"cpu"}


def test_settings_are_trimmed_and_run_once_each():
    assert bench.parse_settings(" full-cuda , int8-cpu,,full-cuda ") == ["full-cuda", "int8-cpu"]


def test_default_settings_are_all_four():
    assert bench.parse_settings(",".join(bench.CONFIGS)) == ["int8-cpu", "int8-cuda", "full-cpu", "full-cuda"]


@pytest.mark.parametrize("text", ["", ",", " , "])
def test_no_settings_is_an_error(text):
    with pytest.raises(ValueError, match="no settings"):
        bench.parse_settings(text)


def test_unknown_setting_is_named():
    with pytest.raises(ValueError, match="int4-cpu"):
        bench.parse_settings("int8-cpu,int4-cpu")


def test_cpu_settings_do_not_need_nvml(monkeypatch):
    asked = []
    monkeypatch.setattr(bench.importlib.util, "find_spec", lambda module: asked.append(module))
    assert bench.missing_extras(["int8-cpu", "full-cpu"]) == ["psutil"]
    assert asked == ["psutil"]
    asked.clear()
    assert bench.missing_extras(["int8-cuda"]) == ["psutil", "nvidia-ml-py"]
    assert asked == ["psutil", "pynvml"]


# --- clips ----------------------------------------------------------------


def test_find_clips_keeps_only_clips_with_words_to_score(tmp_path):
    make_clip(tmp_path, "a", "Hello there.")
    (tmp_path / "b.wav").write_bytes(b"")  # no transcript
    make_clip(tmp_path, "c", "")
    make_clip(tmp_path, "d", "...")
    make_clip(tmp_path, "e", "  Spread\n  over   lines ")
    clips = bench.find_clips(tmp_path)
    assert [(clip.name, clip.reference) for clip in clips] == [
        ("a", "Hello there."),
        ("e", "Spread over lines"),
    ]


def test_read_clip_gives_mono_float32(tmp_path):
    left = np.full(1600, 0.5, dtype=np.float32)
    right = np.zeros(1600, dtype=np.float32)
    write_wav(tmp_path / "stereo.wav", np.column_stack([left, right]).reshape(-1), channels=2)
    samples = bench.read_clip(tmp_path / "stereo.wav")
    assert samples.dtype == np.float32
    assert samples.shape == (1600,)
    assert np.allclose(samples, 0.25, atol=1e-3)


def test_read_clip_names_a_wrong_sample_rate(tmp_path):
    write_wav(tmp_path / "loud.wav", np.zeros(4410, dtype=np.float32), rate=44100)
    with pytest.raises(ValueError, match=r"loud\.wav is 44100 Hz"):
        bench.read_clip(tmp_path / "loud.wav")


def test_read_clip_names_a_file_that_is_not_a_wav(tmp_path):
    (tmp_path / "notes.wav").write_bytes(b"this is not audio")
    with pytest.raises(ValueError, match=r"notes\.wav is not a PCM WAV file"):
        bench.read_clip(tmp_path / "notes.wav")


# --- one setting, with a stand-in model and clock -------------------------


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class FakePeakMemory:
    created = []

    def __init__(self, gpu):
        FakePeakMemory.created.append(gpu)
        self.vram_error = None

    def stop(self):
        return 200 * 2**20, None


def fake_transcriber(clock, run_times, text="Um, hello there.", fail_on=None):
    """A Transcriber stand-in: loading takes 5 s and its warm-up 2 s on the clock."""

    class FakeTranscriber:
        made = []

        def __init__(self, model, quantization, device):
            FakeTranscriber.made.append((model, quantization, device))
            self.provider = "CPUExecutionProvider"
            self.warming = True
            clock.now += 5.0
            self.transcribe(np.zeros(16000, dtype=np.float32))
            self.warming = False

        def transcribe(self, samples):
            if self.warming:
                clock.now += 2.0
                return ""
            if fail_on is not None and len(samples) == fail_on:
                raise MemoryError("out of memory on a long clip")
            clock.now += run_times.pop(0)
            return text

    return FakeTranscriber


@pytest.fixture
def stand_ins(monkeypatch):
    clock = FakeClock()
    FakePeakMemory.created = []
    monkeypatch.setattr(bench, "time", SimpleNamespace(perf_counter=clock))
    monkeypatch.setattr(bench, "PeakMemory", FakePeakMemory)

    def install(*args, **kwargs):
        transcriber = fake_transcriber(clock, *args, **kwargs)
        monkeypatch.setattr(transcriber_module, "Transcriber", transcriber)
        return transcriber

    return install


def test_load_time_includes_the_warm_up_and_clip_times_do_not(tmp_path, stand_ins):
    stand_ins([0.9, 0.3, 0.4, 0.5, 0.2, 0.2])
    clips = [make_clip(tmp_path, "01", "Hello there.", 1.0), make_clip(tmp_path, "02", "Hello there.", 2.0)]
    result = bench.run_config("int8-cpu", "model", clips, repeats=3, clean=True)
    assert result["load_seconds"] == pytest.approx(7.0)
    first, second = result["clips"]
    assert first["seconds"] == pytest.approx(0.4)  # median of 0.9, 0.3, 0.4
    assert first["first_seconds"] == pytest.approx(0.9)
    assert second["seconds"] == pytest.approx(0.2)
    assert second["first_seconds"] == pytest.approx(0.5)
    assert first["audio_seconds"] == pytest.approx(1.0)
    assert second["audio_seconds"] == pytest.approx(2.0)


def test_fillers_are_removed_unless_raw(tmp_path, stand_ins):
    clips = [make_clip(tmp_path, "01", "Hello there.")]
    stand_ins([0.1])
    assert (
        bench.run_config("int8-cpu", "model", clips, 1, clean=True)["clips"][0]["hypothesis"]
        == "Hello there."
    )
    stand_ins([0.1])
    raw = bench.run_config("int8-cpu", "model", clips, 1, clean=False)["clips"][0]["hypothesis"]
    assert raw == "Um, hello there."


def test_each_setting_loads_what_it_says(tmp_path, stand_ins):
    clips = [make_clip(tmp_path, "01", "Hello there.")]
    made = stand_ins([0.1] * 4).made
    for name in bench.CONFIGS:
        bench.run_config(name, "model", clips, 1, clean=True)
    assert made == [
        ("model", "int8", "cpu"),
        ("model", "int8", "cuda"),
        ("model", None, "cpu"),
        ("model", None, "cuda"),
    ]
    # Only the CUDA settings read the GPU's memory.
    assert FakePeakMemory.created == [False, True, False, True]


def test_a_failure_part_way_is_recorded_with_its_reason(tmp_path, stand_ins):
    stand_ins([0.1, 0.1], fail_on=32000)
    clips = [make_clip(tmp_path, "01", "Hello.", 1.0), make_clip(tmp_path, "02", "Hello.", 2.0)]
    clip_list = tmp_path / "clips.json"
    clip_list.write_text(json.dumps([str(clip) for clip in clips]), encoding="utf-8")
    result_file = tmp_path / "result.json"
    args = argparse.Namespace(
        download=None,
        worker="int8-cpu",
        model="model",
        clip_list=str(clip_list),
        repeats=1,
        clean=True,
        result=str(result_file),
    )
    assert bench.worker(args) == 1
    result = json.loads(result_file.read_text(encoding="utf-8"))
    assert result == {"error": "MemoryError: out of memory on a long clip"}


# --- report ---------------------------------------------------------------


def ran(provider="CPUExecutionProvider", peak_vram=None, vram_error=None):
    return {
        "provider": provider,
        "load_seconds": 4.25,
        "peak_ram": 300 * 2**20,
        "peak_vram": peak_vram,
        "vram_error": vram_error,
        "clips": [
            {
                "clip": "01",
                "audio_seconds": 2.0,
                "seconds": 0.1,
                "first_seconds": 0.5,
                "hypothesis": "one two",
            },
            {
                "clip": "02",
                "audio_seconds": 6.0,
                "seconds": 0.3,
                "first_seconds": 0.7,
                "hypothesis": "three, x",
            },
        ],
    }


REFERENCES = {"01": "One two.", "02": "Three, four."}


def test_summary_figures():
    row = bench.summarise("full-cuda", ran("CUDAExecutionProvider", peak_vram=1024 * 2**20), REFERENCES)
    assert row["provider"] == "CUDA"
    assert row["load_seconds"] == 4.25
    assert row["median_seconds"] == pytest.approx(0.2)
    assert row["first_seconds"] == pytest.approx(0.6)
    assert row["speed"] == pytest.approx(20.0)  # 8 s of audio in 0.4 s
    assert row["peak_ram_mb"] == pytest.approx(300)
    assert row["peak_vram_mb"] == pytest.approx(1024)
    assert row["wer"] == pytest.approx(0.25)


def test_summary_of_a_setting_that_did_not_run():
    assert bench.summarise("int8-cuda", {"error": "no GPU"}, REFERENCES) == {
        "config": "int8-cuda",
        "error": "no GPU",
    }


def columns(line: str) -> int:
    return len(re.findall(r"(?<!\\)\|", line)) - 1


def test_table_rows_line_up_and_show_each_case():
    rows = [
        bench.summarise("int8-cpu", ran(), REFERENCES),
        bench.summarise("int8-cuda", ran("CUDAExecutionProvider", peak_vram=512 * 2**20), REFERENCES),
        bench.summarise(
            "full-cuda", ran("CUDAExecutionProvider", vram_error="NVML Shared Library Not Found"), REFERENCES
        ),
        {"config": "full-cpu", "error": "RuntimeError: bad | thing\non two lines"},
    ]
    table = bench.markdown_table(rows, clip_count=2, audio_seconds=8.0)
    lines = table.splitlines()
    assert lines[0].startswith("2 clips, 8 s of audio.")
    grid = [line for line in lines if line.startswith("|")]
    assert len(grid) == 2 + len(rows)
    assert {columns(line) for line in grid} == {9}
    header, _, cpu, cuda, unmeasured, failed = grid
    assert "First run per clip (s)" in header
    assert cpu == "| int8-cpu | CPU | 4.2 | 0.20 | 0.60 | 20.0 | 300 |  | 25.0% |"
    assert "| 512 |" in cuda
    assert "| not measured |" in unmeasured
    assert failed.startswith(r"| full-cpu | not run: RuntimeError: bad \| thing on two lines |")
    assert lines[-1] == "VRAM was not measured for full-cuda: NVML Shared Library Not Found"


def test_csv_has_a_row_per_clip_for_settings_that_ran(tmp_path):
    results = {"int8-cpu": ran(), "int8-cuda": {"error": "no GPU"}}
    path = tmp_path / "bench_results.csv"
    bench.write_csv(path, results, REFERENCES)
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["clip"] for row in rows] == ["01", "02"]
    assert {row["config"] for row in rows} == {"int8-cpu"}
    second = rows[1]
    assert second["provider"] == "CPU"
    assert second["seconds"] == "0.300"
    assert second["first_seconds"] == "0.700"
    assert second["speed"] == "20.0"
    assert second["wer"] == "0.500"
    assert second["reference"] == "Three, four."
    assert second["hypothesis"] == "three, x"


# --- the whole run, with the child processes stood in for -----------------


def fake_spawn(seen, crash=()):
    """Answers for the child processes: settings in `crash` die without a result."""

    def spawn(arguments):
        seen.append(arguments)
        if "--download" in arguments:
            return subprocess.CompletedProcess(arguments, 0)
        name = arguments[arguments.index("--worker") + 1]
        clip_list = Path(arguments[arguments.index("--clip-list") + 1])
        clips = [Path(p) for p in json.loads(clip_list.read_text(encoding="utf-8"))]
        if name in crash:
            return subprocess.CompletedProcess(arguments, 0xC0000005)
        result = {
            "provider": "CPUExecutionProvider",
            "load_seconds": 1.0,
            "peak_ram": 2**20,
            "peak_vram": None,
            "vram_error": None,
            "clips": [
                {
                    "clip": p.stem,
                    "audio_seconds": 1.0,
                    "seconds": 0.1,
                    "first_seconds": 0.2,
                    "hypothesis": "hi",
                }
                for p in clips
            ],
        }
        Path(arguments[arguments.index("--result") + 1]).write_text(json.dumps(result), encoding="utf-8")
        return subprocess.CompletedProcess(arguments, 0)

    return spawn


@pytest.fixture
def run_main(tmp_path, monkeypatch):
    make_clip(tmp_path, "01", "Hi.")
    make_clip(tmp_path, "02", "Hi.")
    monkeypatch.setattr(bench, "missing_extras", lambda names: [])

    def run(*argv, crash=()):
        seen = run.seen = []
        monkeypatch.setattr(bench, "spawn", fake_spawn(seen, crash))
        monkeypatch.setattr(sys, "argv", ["bench.py", str(tmp_path), *argv])
        bench.main()
        return seen

    return run


def test_cpu_only_run_succeeds_and_starts_no_cuda_setting(tmp_path, run_main, capsys):
    seen = run_main("--settings", "int8-cpu,full-cpu")
    # Both score the same here, so both are named.
    assert "Most accurate: int8-cpu, full-cpu" in capsys.readouterr().out
    workers = [arguments[arguments.index("--worker") + 1] for arguments in seen if "--worker" in arguments]
    assert workers == ["int8-cpu", "full-cpu"]
    table = (tmp_path / "bench_results.md").read_text(encoding="utf-8")
    assert "| int8-cpu | CPU |" in table
    assert "| full-cpu | CPU |" in table


def test_a_setting_that_did_not_run_fails_the_run(tmp_path, run_main):
    with pytest.raises(SystemExit, match="1 of 2 settings did not run: full-cpu"):
        run_main("--settings", "int8-cpu,full-cpu", crash={"full-cpu"})
    # The results that did come in are still written.
    table = (tmp_path / "bench_results.md").read_text(encoding="utf-8")
    assert "| int8-cpu | CPU |" in table
    assert "crashed with exit code 3221225477 (0xC0000005)" in table
    assert "int8-cpu" in (tmp_path / "bench_results.csv").read_text(encoding="utf-8")


def test_an_empty_settings_list_is_refused_before_anything_runs(run_main):
    with pytest.raises(SystemExit) as stopped:
        run_main("--settings", ",")
    assert stopped.value.code == 2
    assert run_main.seen == []


def test_peak_memory_reads_this_process_when_psutil_is_installed():
    pytest.importorskip("psutil")
    peak_ram, peak_vram = bench.PeakMemory(gpu=False).stop()
    assert peak_ram > 0
    assert peak_vram is None

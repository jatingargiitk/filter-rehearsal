"""Every output recorded by examples/readme_examples.sh must appear in README.md."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
OUTS = sorted((ROOT / "examples" / "out").glob("*.txt"))


def test_outputs_exist():
    assert {p.stem for p in OUTS} == {"run", "try", "baseline", "explain"}


@pytest.mark.parametrize("path", OUTS, ids=lambda p: p.stem)
def test_recorded_output_is_in_readme(path):
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    body, code_line = path.read_text(encoding="utf-8").rstrip("\n").rsplit("\n", 1)
    assert code_line.startswith("exit ")
    assert body in readme, f"README is missing the output of {path.name}; re-run examples/readme_examples.sh and update README"
    if path.stem != "baseline":  # markdown block shows no shell prompt
        assert body + "\n$ echo $?\n" + code_line.split()[1] in readme


def test_example_configs_are_shown_verbatim():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for name in ("proposed.json", "must-keep.json"):
        assert (ROOT / "examples" / name).read_text(encoding="utf-8").rstrip() in readme

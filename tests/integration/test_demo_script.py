import subprocess
import sys
from pathlib import Path

import pytest

from scripts.reset import validate_database_path

ROOT = Path(__file__).resolve().parents[2]


def test_demo_reports_all_four_scenarios(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "scripts.demo", "--database", str(tmp_path / "demo.db")],
        capture_output=True, text=True, timeout=20, cwd=ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines()[-4:] == [
        "PASS normal fill",
        "PASS rest then fill",
        "PASS adversarial block",
        "PASS out of stock",
    ]


def test_reset_refuses_paths_outside_data(tmp_path):
    with pytest.raises(SystemExit):
        validate_database_path(tmp_path / "x.db", explicit=False)
    with pytest.raises(SystemExit):
        validate_database_path(tmp_path / "important.sqlite", explicit=True)
    assert validate_database_path(tmp_path / "x.db", explicit=True).name == "x.db"

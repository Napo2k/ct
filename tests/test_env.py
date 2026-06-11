"""Tests for the stdlib .env loader."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cycle.env import load_dotenv, parse_dotenv


def test_parse_basic_and_comments():
    content = (
        "# comment line\n"
        "\n"
        "OANDA_API_TOKEN=abc123\n"
        "export OANDA_ACCOUNT_ID=101-004-1234567-001\n"
        "QUOTED=\"hello world\"\n"
        "SINGLE='spaced value'\n"
        "TRAILING=value # inline comment\n"
        "not a valid line\n"
        "1BADKEY=x\n"
    )
    values = parse_dotenv(content)
    assert values["OANDA_API_TOKEN"] == "abc123"
    assert values["OANDA_ACCOUNT_ID"] == "101-004-1234567-001"
    assert values["QUOTED"] == "hello world"
    assert values["SINGLE"] == "spaced value"
    assert values["TRAILING"] == "value"
    assert "1BADKEY" not in values
    assert len(values) == 5


def test_hash_inside_quotes_preserved():
    values = parse_dotenv('TOKEN="abc#123"\n')
    assert values["TOKEN"] == "abc#123"


def test_load_dotenv_sets_and_never_overrides(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("CT_TEST_NEW=from_file\nCT_TEST_EXISTING=from_file\n")
    monkeypatch.delenv("CT_TEST_NEW", raising=False)
    monkeypatch.setenv("CT_TEST_EXISTING", "from_real_env")

    loaded = load_dotenv(env_file)
    assert loaded == 1
    assert os.environ["CT_TEST_NEW"] == "from_file"
    assert os.environ["CT_TEST_EXISTING"] == "from_real_env"  # real env wins
    monkeypatch.delenv("CT_TEST_NEW", raising=False)


def test_load_dotenv_missing_file_is_fine(tmp_path):
    assert load_dotenv(tmp_path / "absent.env") == 0

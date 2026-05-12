"""Shared fixtures.

- Loads .env so LLM-bound tests in tests/blackbox/ see TOGETHER_API_KEY /
  GEMINI_API_KEY without each test having to import dotenv.
- Exposes a session-scoped `scenarios` fixture that loads
  tests/scenarios.yaml once and is reused by the parametrized scenario
  suite.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_REPO_ROOT / ".env")


@pytest.fixture(scope="session")
def scenarios_path() -> Path:
    return _REPO_ROOT / "tests" / "scenarios.yaml"


@pytest.fixture(scope="session")
def scenarios(scenarios_path: Path) -> list[dict]:
    raw = yaml.safe_load(scenarios_path.read_text())
    return list(raw["scenarios"])

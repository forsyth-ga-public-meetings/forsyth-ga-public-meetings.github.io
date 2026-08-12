"""Configuration loading. All tunables live in config/*.toml, not in code."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"
DATA_DIR = REPO_ROOT / "data"


def _load(name: str) -> dict[str, Any]:
    path = CONFIG_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"missing config file: {path}")
    with path.open("rb") as fh:
        return tomllib.load(fh)


@dataclass(frozen=True)
class Config:
    sources: dict[str, Any]
    topics: dict[str, Any]

    # -- convenience accessors -------------------------------------------------
    @property
    def user_agent(self) -> str:
        return self.sources["politeness"]["user_agent"]

    @property
    def delay_seconds(self) -> float:
        return float(self.sources["politeness"]["delay_seconds"])

    @property
    def timeout(self) -> int:
        return int(self.sources["politeness"]["timeout_seconds"])

    @property
    def max_retries(self) -> int:
        return int(self.sources["politeness"]["max_retries"])

    @property
    def days_back(self) -> int:
        return int(self.sources["window"]["days_back"])

    @property
    def days_forward(self) -> int:
        return int(self.sources["window"]["days_forward"])

    @property
    def local_tz(self) -> str:
        return self.sources["civicclerk"]["local_timezone"]

    @property
    def large_dollar_threshold(self) -> int:
        return int(self.sources["flags"]["large_dollar_threshold"])

    @property
    def consent_markers(self) -> list[str]:
        return [m.lower() for m in self.topics["consent"]["markers"]]

    @property
    def consent_resets(self) -> list[str]:
        return [m.lower() for m in self.topics["consent"]["resets"]]

    @property
    def term_patterns(self) -> list[str]:
        return list(self.topics["term"]["regex"])

    def topic_clusters(self) -> dict[str, dict[str, Any]]:
        """Topic key -> {label, description, keywords, regex}."""
        reserved = {"consent", "term"}
        return {
            k: v
            for k, v in self.topics.items()
            if k not in reserved and isinstance(v, dict) and "label" in v
        }


@lru_cache(maxsize=1)
def load_config() -> Config:
    return Config(sources=_load("sources.toml"), topics=_load("topics.toml"))

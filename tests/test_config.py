from __future__ import annotations

from pathlib import Path

from bmsbot.config import PROJECT_ROOT, Settings


def test_relative_database_path_is_resolved_against_project_root() -> None:
    settings = Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite:///./data/test.db",
        data_dir="./data",
    )
    assert settings.data_dir == (PROJECT_ROOT / "data").resolve()
    assert settings.database_url.startswith("sqlite+aiosqlite:////")
    assert settings.database_url.endswith("/data/test.db")


def test_absolute_database_path_is_kept() -> None:
    settings = Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite:////tmp/absolute.db",
        data_dir=Path("/tmp"),
    )
    assert settings.database_url == "sqlite+aiosqlite:////tmp/absolute.db"

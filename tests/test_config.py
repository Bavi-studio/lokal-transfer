from __future__ import annotations

from pathlib import Path

import pytest

from app.config import load_settings


def test_data_dir_defaults_to_project_data(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOCAL_TRANSFER_DATA_DIR", raising=False)
    settings = load_settings()
    project_root = Path(__file__).resolve().parents[1]
    assert settings.data_dir == (project_root / "data").resolve()


def test_data_dir_can_be_changed_inside_project(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCAL_TRANSFER_DATA_DIR", "private-data\\transfer")
    settings = load_settings()
    project_root = Path(__file__).resolve().parents[1]
    assert settings.data_dir == (project_root / "private-data" / "transfer").resolve()
    assert settings.data_dir.is_relative_to(project_root)


@pytest.mark.parametrize("unsafe", ["..", "../outside", "data/../../outside", "C:\\\\Windows\\Temp", "/tmp/outside"])
def test_data_dir_rejects_absolute_or_parent_paths(monkeypatch: pytest.MonkeyPatch, unsafe: str) -> None:
    monkeypatch.setenv("LOCAL_TRANSFER_DATA_DIR", unsafe)
    monkeypatch.delenv("LOCAL_TRANSFER_ALLOW_EXTERNAL_DATA_DIR", raising=False)
    with pytest.raises(ValueError, match="LOCAL_TRANSFER_DATA_DIR"):
        load_settings()

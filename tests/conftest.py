from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        token="test-secret-token",
        max_file_size=1024,
        max_text_size=64,
        max_concurrent_uploads=2,
        text_history_limit=5,
        chunk_size=8,
        allowed_hosts=("testserver",),
    )


@pytest.fixture()
def client(settings: Settings):
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def auth() -> dict[str, str]:
    return {"Authorization": "Bearer test-secret-token"}

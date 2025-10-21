from contextlib import contextmanager
from typing import Generator

import pytest
from fastapi.testclient import TestClient

from app.config import load_settings
from app.database import close_pool
from app.main import create_app


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORTFOLIO_DB_HOST", "localhost")
    monkeypatch.setenv("PORTFOLIO_DB_PORT", "5432")
    monkeypatch.setenv("PORTFOLIO_DB_NAME", "test_db")
    monkeypatch.setenv("PORTFOLIO_DB_USER", "test_user")
    monkeypatch.setenv("PORTFOLIO_DB_PASSWORD", "test_pass")
    monkeypatch.setenv("VISUALIZER_BASIC_AUTH_USER", "apiuser")
    monkeypatch.setenv("VISUALIZER_BASIC_AUTH_PASSWORD", "apipass")
    monkeypatch.setenv(
        "VISUALIZER_CORS_ORIGINS", "http://localhost:4200,http://localhost:8120"
    )
    load_settings.cache_clear()
    close_pool()


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    # prevent pool initialisation
    monkeypatch.setattr("app.database.init_pool", lambda settings=None: None)
    monkeypatch.setattr("app.database.close_pool", lambda: None)

    @contextmanager
    def fake_conn():
        yield object()

    monkeypatch.setattr("app.database.get_db_connection", lambda: fake_conn())

    app = create_app()
    with TestClient(app) as test_client:
        yield test_client

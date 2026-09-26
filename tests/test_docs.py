"""FastAPI 自带 Swagger /docs 可用（默认调试页）。"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from service.app import create_app


@pytest.fixture
def client(tmp_path: Path):
    app = create_app(token="docs-token-000000000001", ledger_path=tmp_path / "ledger.sqlite3")
    return TestClient(app)


def test_docs_available(client):
    resp = client.get("/docs")
    assert resp.status_code == 200
    assert "swagger" in resp.text.lower()


def test_openapi_lists_v1_apis(client):
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    paths = resp.json()["paths"]
    for path in ("/v1/health", "/v1/contacts:query", "/v1/commands/read", "/v1/commands/send", "/v1/chat/history"):
        assert path in paths

import pytest
from fastapi.testclient import TestClient
from lan_transfer.api import create_app
from lan_transfer.auth import issue_code
from lan_transfer.config import Config


@pytest.fixture
def config(tmp_path):
    return Config(storage=tmp_path / "storage", hosts=["testserver"], origins=["http://testserver"], safety_bytes=0, chunk_size=4 * 1024 * 1024)


@pytest.fixture
def client(config):
    with TestClient(create_app(config), client=("127.0.0.1", 50000)) as client:
        code = issue_code(client.app.state.service.repo)
        token = client.post("/api/v1/pair", json={"code": code, "client": "cli"}).json()["token"]
        client.headers["Authorization"] = "Bearer " + token
        yield client

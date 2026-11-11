import pytest
from fastapi.testclient import TestClient

from r2.care_actions import create_app
from walker import KEY


@pytest.fixture()
def client():
    return TestClient(create_app(api_key=KEY, allow_faults=True, use_default_brain=False))


@pytest.fixture()
def H():
    return {"X-API-Key": KEY}


@pytest.fixture()
def UI(client):
    return {"X-Care-Token": client.app.state.ui_token}

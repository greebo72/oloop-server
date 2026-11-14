from fastapi.testclient import TestClient

from oloop_server.main import app


def test_healthz_needs_no_authentication_and_returns_no_data():
    response = TestClient(app).get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_the_interactive_docs_are_not_served():
    client = TestClient(app)

    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404

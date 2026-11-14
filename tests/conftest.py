import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from oloop_server.config import Settings
from oloop_server.main import create_app
from oloop_server.migrate import upgrade
from oloop_server.models import User
from oloop_server.security import hash_password

PASSWORD = "correct horse battery"


@pytest.fixture
def settings(tmp_path):
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        blob_dir=tmp_path / "blobs",
        _env_file=None,
    )


@pytest.fixture
def app(settings):
    upgrade(settings.database_url)
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def db(app):
    with Session(app.state.engine) as session:
        yield session


@pytest.fixture
def make_user(app):
    def make(username="alice", password=PASSWORD):
        with Session(app.state.engine) as session:
            session.add(User(username=username, username_key=username.casefold(),
                             password_hash=hash_password(password)))
            session.commit()
    return make


def login(client, username="alice", password=PASSWORD, device="laptop"):
    return client.post("/v1/auth/login",
                       json={"username": username, "password": password, "deviceName": device})


def bearer(token):
    return {"Authorization": f"Bearer {token}"}

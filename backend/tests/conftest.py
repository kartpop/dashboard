"""Shared fixtures for the goal-8 (multi-tenant, per-user creds) test suite.

The app is DONE; these fixtures adapt the tests to the new contract:

- The engine imports EVERY model module before `create_all` so the `user`,
  `allowed_email`, `user_settings`, overlay, and router tables (and their FKs)
  all exist.
- Two seeded users (A + B) exercise per-user row isolation.
- `DummyCreds` is an opaque sentinel — every Google call is monkeypatched, so
  the creds object is never actually used to talk to Google.
- The authenticated `client` overrides the FastAPI auth dependencies so a request
  acts as a chosen user with dummy creds.
"""

from __future__ import annotations

# Import every table-defining module BEFORE create_all so all tables + FKs exist.
import app.auth.models  # noqa: F401
import app.news.models  # noqa: F401
import app.overlay.models  # noqa: F401
import app.router.models  # noqa: F401
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.auth.deps import get_current_credentials, get_current_user
from app.auth.models import User
from app.db import get_session
from app.main import app


@pytest.fixture(autouse=True)
def _token_encryption_key(monkeypatch):
    """A valid Fernet key for encrypt/decrypt round-trips (autouse — every test)."""
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())


class DummyCreds:
    """Opaque credentials sentinel — Google calls are always mocked, so this is
    only ever passed through, never used to build a real service."""


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(eng)
    yield eng
    SQLModel.metadata.drop_all(eng)


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


def _make_user(session: Session, **fields) -> User:
    user = User(**fields)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


@pytest.fixture
def user_a(session) -> User:
    return _make_user(
        session,
        google_sub="sub-a",
        email="a@example.com",
        name="User A",
    )


@pytest.fixture
def user_b(session) -> User:
    return _make_user(
        session,
        google_sub="sub-b",
        email="b@example.com",
        name="User B",
    )


@pytest.fixture
def seeded_user(user_a) -> User:
    """The default authenticated user for single-tenant endpoint tests."""
    return user_a


@pytest.fixture
def auth(engine):
    """A per-user authenticated TestClient factory.

    `auth.as_user(user)` points `get_current_user` at that row; the session +
    credentials dependencies are overridden once. Returns a live `TestClient`
    bound to the same in-memory engine as the `session` fixture.
    """

    def _override_session():
        with Session(engine) as s:
            yield s

    state = {"user": None}

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_current_user] = lambda: state["user"]
    app.dependency_overrides[get_current_credentials] = lambda: DummyCreds()

    class _Auth:
        client = TestClient(app)

        def as_user(self, user: User) -> TestClient:
            state["user"] = user
            return self.client

    yield _Auth()
    app.dependency_overrides.clear()


@pytest.fixture
def client(auth, seeded_user):
    """A TestClient authenticated as the default seeded user (User A)."""
    return auth.as_user(seeded_user)

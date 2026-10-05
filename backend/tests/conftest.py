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
- Goal 17: tasks live in the local store. Seeded users are already "imported"
  (`tasks_imported_at` set) so no test triggers the Google import by accident; the
  `store` fixture seeds lists/tasks and reads them back. Importer tests build their
  own un-imported user.
"""

from __future__ import annotations

# Import every table-defining module BEFORE create_all so all tables + FKs exist.
import app.auth.models  # noqa: F401
import app.dev.models  # noqa: F401
import app.news.models  # noqa: F401
import app.overlay.models  # noqa: F401
import app.router.models  # noqa: F401
import app.tasks_store.models  # noqa: F401
import app.threads.models  # noqa: F401
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.auth.deps import get_current_credentials, get_current_user
from datetime import date, datetime, timezone

from app.auth.models import User
from app.db import get_session
from app.main import app
from app.tasks_store.models import Task, TaskList

# Default list ids seeded by the `store` fixture (two pinned lists + one other).
MINE, FOLLOW, OTHER = "L_MINE", "L_FOLLOW", "L_OTHER"


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
    fields.setdefault("tasks_imported_at", datetime.now(timezone.utc))
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


class StoreHelper:
    """Seeds and reads the local task store (goal 17) for tests."""

    def __init__(self, session: Session):
        self.session = session
        self._n = 0

    def lists(self, user: User, lists: dict[str, str] | None = None) -> None:
        """Create task lists `{id: title}` for `user` (default: My Tasks,
        Follow-ups, Groceries as MINE / FOLLOW / OTHER, suffixed per user so two
        users never share an id)."""
        if lists is None:
            sfx = "" if user.google_sub == "sub-a" else f"_{user.id}"
            lists = {
                MINE + sfx: "My Tasks",
                FOLLOW + sfx: "Follow-ups",
                OTHER + sfx: "Groceries",
            }
        for i, (lid, title) in enumerate(lists.items()):
            self.session.add(
                TaskList(id=lid, user_id=user.id, title=title, position=float(i))
            )
        self.session.commit()

    def add(
        self,
        user: User,
        tasklist_id: str,
        task_id: str | None = None,
        title: str = "A task",
        *,
        due: str | date | None = None,
        status: str = "needsAction",
        notes: str | None = None,
        rank: float | None = None,
        group_id: int | None = None,
        completed_at: datetime | None = None,
    ) -> Task:
        self._n += 1
        if isinstance(due, str):
            due = date.fromisoformat(due[:10])
        if status == "completed" and completed_at is None:
            completed_at = datetime.now(timezone.utc)
        task = Task(
            id=task_id or f"T{self._n}",
            user_id=user.id,
            tasklist_id=tasklist_id,
            title=title,
            notes=notes,
            status=status,
            due=due,
            completed_at=completed_at,
            position=float(self._n),
            rank=rank,
            group_id=group_id,
        )
        self.session.add(task)
        self.session.commit()
        self.session.refresh(task)
        return task

    def get(self, task_id: str) -> Task | None:
        self.session.expire_all()
        return self.session.get(Task, task_id)

    def tasks(self, user: User, tasklist_id: str | None = None) -> list[Task]:
        from sqlmodel import select

        self.session.expire_all()
        q = select(Task).where(Task.user_id == user.id)
        if tasklist_id is not None:
            q = q.where(Task.tasklist_id == tasklist_id)
        return list(self.session.exec(q.order_by(Task.position)).all())


@pytest.fixture
def store(session) -> StoreHelper:
    return StoreHelper(session)

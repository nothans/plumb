import io
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import cli  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import connect  # noqa: E402
from app.main import create_app  # noqa: E402

SESSIONS = {role: token for role, (token, _) in cli.DEMO_SESSIONS.items()}


def ts(delta_hours: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=delta_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = Settings(
        data_dir=tmp_path / "data", demo=True, fixtures_path=ROOT / "fixtures.json",
        base_url="http://testserver", secure_cookies=False, admin_email=None, admin_password=None, webhooks=False,
    )
    cli.bootstrap(s, out=io.StringIO())
    return s


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def db(settings):
    conn = connect(settings.database_path)
    yield conn
    conn.close()


def client_for(app, role: str | None = None) -> TestClient:
    c = TestClient(app, base_url="http://testserver")
    if role:
        c.cookies.set("plumb_session", SESSIONS[role])
    return c


@pytest.fixture
def anon(app):
    return client_for(app)


@pytest.fixture
def organizer(app):
    return client_for(app, "organizer")


@pytest.fixture
def judge_a(app):
    return client_for(app, "judge_a")


@pytest.fixture
def judge_b(app):
    return client_for(app, "judge_b")


@pytest.fixture
def participant(app):
    return client_for(app, "participant")


def csrf_of(client: TestClient, path: str = "/") -> str:
    html = client.get(path).text
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    assert m, f"no csrf field on {path}"
    return m.group(1)


def signup(app, email: str, name: str = "Test Person", password: str = "correct horse battery") -> TestClient:
    c = client_for(app)
    token = csrf_of(c, "/signup")
    r = c.post("/signup", data={"csrf": token, "email": email, "name": name, "password": password, "next": "/"},
               follow_redirects=False)
    assert r.status_code == 303, r.text
    return c


def post_form(client: TestClient, path: str, data: dict, *, csrf_from: str = "/me", **kw):
    return client.post(path, data={"csrf": csrf_of(client, csrf_from), **data}, follow_redirects=False, **kw)

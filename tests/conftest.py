import os
import tempfile

# Must be set BEFORE the app is imported: isolated DB, known API key, fixed timezone.
os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/test.db"
os.environ["API_KEY"] = "test-key"
os.environ["APP_TIMEZONE"] = "Asia/Kolkata"

from datetime import datetime, timedelta  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import linkedin  # noqa: E402
from app.db import Account, Post, SessionLocal  # noqa: E402
from app.main import app  # noqa: E402

H = {"X-API-Key": "test-key"}


class FakeLinkedIn:
    """Stands in for the LinkedIn network calls. Records every publish."""
    def __init__(self):
        self.calls, self.fail_with = [], None

    def create_post(self, token, author_urn, text, image_urns, visibility="PUBLIC"):
        if self.fail_with:
            raise self.fail_with
        self.calls.append({"text": text, "visibility": visibility, "at": datetime.utcnow()})
        return f"urn:li:share:fake{len(self.calls)}"


@pytest.fixture()
def fake(monkeypatch):
    f = FakeLinkedIn()
    monkeypatch.setattr(linkedin, "create_post", f.create_post)
    monkeypatch.setattr(linkedin, "upload_image", lambda *a, **k: "urn:li:image:fake")
    return f


@pytest.fixture(scope="session")
def _app_client():
    with TestClient(app) as c:  # app lifespan (and the real scheduler) starts ONCE, like in production
        yield c


@pytest.fixture()
def client(_app_client):
    with SessionLocal() as db:  # fresh state for every test
        db.query(Post).delete(); db.query(Account).delete()
        db.add(Account(member_id="m1", name="Test User", access_token="tok",
                       expires_at=datetime.utcnow() + timedelta(days=30)))
        db.commit()
    yield _app_client

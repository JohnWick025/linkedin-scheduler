import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.db import Account, SessionLocal
from app.linkedin import LinkedInError
from conftest import H

IST = ZoneInfo("Asia/Kolkata")


def future(seconds=0, days=0):
    return (datetime.now(IST) + timedelta(seconds=seconds, days=days)).isoformat(timespec="seconds")


def wait_for(client, post_id, status, timeout=12):
    end = time.time() + timeout
    while time.time() < end:
        body = client.get(f"/api/v1/posts/{post_id}", headers=H).json()
        if body["status"] == status:
            return body
        time.sleep(0.3)
    raise AssertionError(f"post {post_id} never reached '{status}', last: {body}")


# ---------- auth ----------
def test_auth_required(client):
    assert client.get("/api/v1/status").status_code == 401
    assert client.get("/api/v1/status", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/api/v1/status", headers=H).status_code == 200
    assert client.get("/api/v1/status", headers={"Authorization": "Bearer test-key"}).status_code == 200


def test_server_time_tells_agent_the_timezone(client):
    body = client.get("/api/v1/time", headers=H).json()
    assert body["timezone"] == "Asia/Kolkata" and body["now_local"].endswith("+05:30") and body["weekday"]


# ---------- scheduling & timezones ----------
def test_schedule_with_explicit_offset_is_stored_as_correct_utc(client):
    r = client.post("/api/v1/posts", headers=H, json={"text": "Monday 2pm", "scheduled_at": "2027-03-01T14:00:00+05:30"})
    assert r.status_code == 201
    b = r.json()
    assert b["status"] == "scheduled" and b["scheduled_at_utc"] == "2027-03-01T08:30:00Z"
    assert b["scheduled_at_local"] == "2027-03-01T14:00:00+05:30"


def test_naive_time_is_interpreted_in_server_timezone(client):
    b = client.post("/api/v1/posts", headers=H, json={"text": "x", "scheduled_at": "2027-03-01T14:00:00"}).json()
    assert b["scheduled_at_utc"] == "2027-03-01T08:30:00Z"


def test_rejects_past_missing_and_garbage_times(client):
    assert client.post("/api/v1/posts", headers=H, json={"text": "x", "scheduled_at": "2020-01-01T10:00:00+05:30"}).status_code == 422
    assert client.post("/api/v1/posts", headers=H, json={"text": "x"}).status_code == 422
    assert client.post("/api/v1/posts", headers=H, json={"text": "x", "scheduled_at": "next monday"}).status_code == 422
    assert client.post("/api/v1/posts", headers=H, json={"text": "x" * 3001, "scheduled_at": future(days=1)}).status_code == 422


def test_idempotency_key_prevents_duplicates(client):
    body = {"text": "once", "scheduled_at": future(days=1), "idempotency_key": "tg-msg-42"}
    first = client.post("/api/v1/posts", headers=H, json=body)
    second = client.post("/api/v1/posts", headers=H, json=body)
    assert first.status_code == 201 and second.status_code == 200
    assert first.json()["id"] == second.json()["id"] and second.json()["duplicate"] is True
    assert len(client.get("/api/v1/posts", headers=H).json()) == 1


def test_warns_when_token_expires_before_post_is_due(client):
    with SessionLocal() as db:
        a = db.query(Account).first(); a.expires_at = datetime.utcnow() + timedelta(days=2); db.commit()
    b = client.post("/api/v1/posts", headers=H, json={"text": "x", "scheduled_at": future(days=5)}).json()
    assert b["warnings"] and "token expires" in b["warnings"][0]


def test_refuses_to_schedule_with_expired_token(client):
    with SessionLocal() as db:
        a = db.query(Account).first(); a.expires_at = datetime.utcnow() - timedelta(days=1); db.commit()
    assert client.post("/api/v1/posts", headers=H, json={"text": "x", "scheduled_at": future(days=1)}).status_code == 409


# ---------- the important one: does it actually fire at the scheduled time? ----------
def test_post_is_published_at_the_scheduled_second(client, fake):
    due = datetime.now(IST) + timedelta(seconds=4)
    r = client.post("/api/v1/posts", headers=H, json={"text": "Fire on time #test", "scheduled_at": due.isoformat(timespec="seconds"), "visibility": "CONNECTIONS"})
    pid = r.json()["id"]
    assert client.get(f"/api/v1/posts/{pid}", headers=H).json()["status"] == "scheduled"
    assert fake.calls == []                                   # nothing sent early
    done = wait_for(client, pid, "posted")
    assert done["linkedin_post_id"].startswith("urn:li:share:fake")
    assert len(fake.calls) == 1                               # exactly once (timer + poller can't double-post)
    assert fake.calls[0]["text"] == "Fire on time #test" and fake.calls[0]["visibility"] == "CONNECTIONS"
    fired = fake.calls[0]["at"].replace(tzinfo=timezone.utc)
    assert abs((fired - due.astimezone(timezone.utc)).total_seconds()) < 3   # within ~3s of the target


def test_publish_now_mode(client, fake):
    pid = client.post("/api/v1/posts", headers=H, json={"text": "now!", "mode": "now"}).json()["id"]
    wait_for(client, pid, "posted", timeout=6)
    assert len(fake.calls) == 1


def test_transient_linkedin_error_is_retried(client, fake):
    fake.fail_with = LinkedInError("503: unavailable", transient=True)
    pid = client.post("/api/v1/posts", headers=H, json={"text": "retry me", "mode": "now"}).json()["id"]
    time.sleep(2)
    b = client.get(f"/api/v1/posts/{pid}", headers=H).json()
    assert b["status"] == "scheduled" and b["attempts"] == 1 and "retrying" in b["error"]


def test_hard_linkedin_error_fails_without_retry(client, fake):
    fake.fail_with = LinkedInError("401: token revoked", transient=False)
    pid = client.post("/api/v1/posts", headers=H, json={"text": "bad", "mode": "now"}).json()["id"]
    b = wait_for(client, pid, "failed", timeout=6)
    assert "401" in b["error"]


# ---------- lifecycle ----------
def test_draft_patch_reschedule_and_cancel(client, fake):
    pid = client.post("/api/v1/posts", headers=H, json={"text": "idea", "mode": "draft"}).json()["id"]
    assert client.get(f"/api/v1/posts/{pid}", headers=H).json()["status"] == "draft"
    b = client.patch(f"/api/v1/posts/{pid}", headers=H, json={"text": "idea v2", "scheduled_at": future(days=2)}).json()
    assert b["status"] == "scheduled" and b["text"] == "idea v2"
    b = client.patch(f"/api/v1/posts/{pid}", headers=H, json={"scheduled_at": "2027-04-05T09:00:00+05:30"}).json()
    assert b["scheduled_at_utc"] == "2027-04-05T03:30:00Z"
    assert client.delete(f"/api/v1/posts/{pid}", headers=H).status_code == 204
    assert client.get(f"/api/v1/posts/{pid}", headers=H).status_code == 404
    assert fake.calls == []                                   # cancelled posts never reach LinkedIn


def test_cannot_edit_or_cancel_a_published_post(client, fake):
    pid = client.post("/api/v1/posts", headers=H, json={"text": "done", "mode": "now"}).json()["id"]
    wait_for(client, pid, "posted", timeout=6)
    assert client.patch(f"/api/v1/posts/{pid}", headers=H, json={"text": "changed"}).status_code == 409
    assert client.delete(f"/api/v1/posts/{pid}", headers=H).status_code == 409


def test_list_filter_and_status_endpoint(client):
    client.post("/api/v1/posts", headers=H, json={"text": "a", "mode": "draft"})
    client.post("/api/v1/posts", headers=H, json={"text": "b", "scheduled_at": future(days=1)})
    assert len(client.get("/api/v1/posts?status=draft", headers=H).json()) == 1
    s = client.get("/api/v1/status", headers=H).json()
    assert s["linkedin_connected"] and s["scheduler_running"] and s["queued"] == 1

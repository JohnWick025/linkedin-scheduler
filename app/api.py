"""REST API for automation (e.g. a Telegram agent). Auth: `X-API-Key: <API_KEY>` or `Authorization: Bearer <API_KEY>`."""
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel, Field

from . import config, scheduler
from .db import Account, Post, SessionLocal

TZ = ZoneInfo(config.APP_TIMEZONE)
EDITABLE = ("draft", "scheduled", "failed")
MAX_HORIZON_DAYS = 365


def require_key(x_api_key: str | None = Header(None), authorization: str | None = Header(None)) -> None:
    if not config.API_KEY:
        raise HTTPException(503, "REST API is disabled. Set API_KEY in .env and restart.")
    supplied = x_api_key or (authorization[7:] if authorization and authorization.lower().startswith("bearer ") else "")
    if not secrets.compare_digest(supplied.encode(), config.API_KEY.encode()):
        raise HTTPException(401, "Invalid or missing API key.")


router = APIRouter(prefix="/api/v1", tags=["api"], dependencies=[Depends(require_key)])


class PostIn(BaseModel):
    text: str = Field(min_length=1, max_length=3000)
    mode: Literal["schedule", "now", "draft"] = "schedule"
    scheduled_at: str | None = Field(None, description="ISO 8601, e.g. 2026-11-23T14:00:00+05:30. No offset = server timezone.")
    visibility: Literal["PUBLIC", "CONNECTIONS"] = "PUBLIC"
    idempotency_key: str | None = Field(None, max_length=64, description="Same key twice returns the first post instead of creating a duplicate.")


class PostPatch(BaseModel):
    text: str | None = Field(None, min_length=1, max_length=3000)
    scheduled_at: str | None = None
    visibility: Literal["PUBLIC", "CONNECTIONS"] | None = None


def _utc_iso(dt: datetime | None) -> str | None:
    return dt.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z") if dt else None


def _local_iso(dt: datetime | None) -> str | None:
    return dt.replace(tzinfo=timezone.utc).astimezone(TZ).isoformat(timespec="seconds") if dt else None


def _out(p: Post, **extra) -> dict:
    return {"id": p.id, "status": p.status, "text": p.text, "visibility": p.visibility,
            "scheduled_at_utc": _utc_iso(p.scheduled_at) if p.status != "draft" else None,
            "scheduled_at_local": _local_iso(p.scheduled_at) if p.status != "draft" else None,
            "timezone": config.APP_TIMEZONE, "published_at_utc": _utc_iso(p.published_at),
            "linkedin_post_id": p.linkedin_post_id, "attempts": p.attempts, "error": p.error, **extra}


def _parse_future(value: str | None) -> datetime:
    """ISO 8601 -> naive UTC, validated to be in the future and within a year."""
    if not value:
        raise HTTPException(422, "scheduled_at is required when mode is 'schedule'.")
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(422, "scheduled_at must be ISO 8601, e.g. 2026-11-23T14:00:00+05:30.")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    utc = dt.astimezone(timezone.utc).replace(tzinfo=None)
    now = datetime.utcnow()
    if utc <= now:
        raise HTTPException(422, f"scheduled_at is in the past (server time is {_local_iso(now)}).")
    if utc > now + timedelta(days=MAX_HORIZON_DAYS):
        raise HTTPException(422, f"scheduled_at is more than {MAX_HORIZON_DAYS} days away.")
    return utc


def _account(db) -> Account:
    acct = db.query(Account).first()
    if not acct:
        raise HTTPException(409, "No LinkedIn account connected. Open the web UI and connect LinkedIn first.")
    return acct


def _get(db, post_id: int) -> Post:
    post = db.get(Post, post_id)
    if not post:
        raise HTTPException(404, "Post not found.")
    return post


@router.get("/time", summary="Server clock + timezone, so an agent can resolve 'Monday 2pm' correctly")
def server_time():
    now = datetime.now(timezone.utc).astimezone(TZ)
    return {"now_utc": _utc_iso(now.astimezone(timezone.utc).replace(tzinfo=None)), "now_local": now.isoformat(timespec="seconds"),
            "timezone": config.APP_TIMEZONE, "weekday": now.strftime("%A")}


@router.get("/status")
def status():
    with SessionLocal() as db:
        acct = db.query(Account).first()
        return {"linkedin_connected": bool(acct), "account_name": acct.name if acct else None,
                "token_expired": acct.is_expired if acct else None,
                "token_expires_at_utc": _utc_iso(acct.expires_at) if acct else None,
                "scheduler_running": scheduler.scheduler.running,
                "queued": db.query(Post).filter(Post.status == "scheduled").count()}


@router.post("/posts", status_code=201, summary="Create a post: schedule, publish now, or save a draft")
def create_post(body: PostIn, response: Response):
    text = body.text.strip()
    if not text:
        raise HTTPException(422, "text is empty.")
    with SessionLocal() as db:
        if body.idempotency_key:
            dup = db.query(Post).filter(Post.idem_key == body.idempotency_key).first()
            if dup:
                response.status_code = 200
                return _out(dup, duplicate=True)
        acct = _account(db)
        warnings = []
        if body.mode == "draft":
            status, run_at = "draft", datetime.utcnow()
        elif body.mode == "now":
            status, run_at = "scheduled", datetime.utcnow()
        else:
            status, run_at = "scheduled", _parse_future(body.scheduled_at)
        if status == "scheduled":
            if acct.is_expired:
                raise HTTPException(409, "LinkedIn token has expired. Reconnect LinkedIn in the web UI.")
            if acct.expires_at <= run_at:
                warnings.append("The LinkedIn token expires before this post is due; reconnect LinkedIn before then or it will fail.")
        post = Post(account_id=acct.id, text=text, status=status, scheduled_at=run_at,
                    visibility=body.visibility, idem_key=body.idempotency_key)
        db.add(post)
        db.commit()
        post_id = post.id
        out = _out(post, warnings=warnings)
    if body.mode == "now":
        scheduler.scheduler.add_job(scheduler.publish_post, args=[post_id])
    elif body.mode == "schedule":
        scheduler.arm(post_id, run_at)
    return out


@router.get("/posts", summary="List posts, optionally filtered by status")
def list_posts(status: Literal["draft", "scheduled", "posting", "posted", "failed"] | None = None, limit: int = Query(50, ge=1, le=200)):
    with SessionLocal() as db:
        q = db.query(Post)
        if status:
            q = q.filter(Post.status == status)
        return [_out(p) for p in q.order_by(Post.scheduled_at.asc()).limit(limit)]


@router.get("/posts/{post_id}")
def get_post(post_id: int):
    with SessionLocal() as db:
        return _out(_get(db, post_id))


@router.patch("/posts/{post_id}", summary="Edit text/visibility or reschedule")
def patch_post(post_id: int, body: PostPatch):
    run_at = _parse_future(body.scheduled_at) if body.scheduled_at else None
    with SessionLocal() as db:
        post = _get(db, post_id)
        if post.status not in EDITABLE:
            raise HTTPException(409, f"A post that is '{post.status}' can no longer be edited.")
        if body.text is not None:
            post.text = body.text.strip()
        if body.visibility:
            post.visibility = body.visibility
        if run_at:
            post.scheduled_at, post.status = run_at, "scheduled"
        post.error, post.attempts = None, 0
        db.commit()
        out = _out(post)
    if run_at:
        scheduler.arm(post_id, run_at)
    return out


@router.delete("/posts/{post_id}", status_code=204, summary="Cancel a draft/scheduled/failed post")
def delete_post(post_id: int):
    with SessionLocal() as db:
        post = _get(db, post_id)
        if post.status not in EDITABLE:
            raise HTTPException(409, f"A post that is '{post.status}' can't be cancelled.")
        scheduler.delete_files(post.images)
        db.delete(post)
        db.commit()
    scheduler.disarm(post_id)
    return Response(status_code=204)


@router.post("/posts/{post_id}/publish-now", status_code=202)
def publish_now(post_id: int):
    with SessionLocal() as db:
        post = _get(db, post_id)
        if post.status not in EDITABLE:
            raise HTTPException(409, f"A post that is '{post.status}' can't be published.")
        post.status, post.scheduled_at, post.error, post.attempts = "scheduled", datetime.utcnow(), None, 0
        db.commit()
        out = _out(post)
    scheduler.disarm(post_id)
    scheduler.scheduler.add_job(scheduler.publish_post, args=[post_id])
    return out

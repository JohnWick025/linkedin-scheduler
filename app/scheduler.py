import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import update

from . import linkedin
from .db import Post, SessionLocal

_logger = logging.getLogger(__name__)
scheduler = BackgroundScheduler(timezone="UTC")
MAX_ATTEMPTS = 3
BACKOFF_MINUTES = (2, 10)  # wait before attempt 2, attempt 3


def arm(post_id: int, run_at: datetime) -> None:
    """Fire publish_post at the exact second (run_at = naive UTC). The 30s poller stays as a
    safety net, e.g. after a restart; publish_post claims atomically so it can never double-post."""
    scheduler.add_job(publish_post, "date", run_date=run_at.replace(tzinfo=timezone.utc), args=[post_id],
                      id=f"post-{post_id}", replace_existing=True, misfire_grace_time=3600)


def disarm(post_id: int) -> None:
    try:
        scheduler.remove_job(f"post-{post_id}")
    except Exception:
        pass


def delete_files(paths: list[str]) -> None:
    for p in paths:
        Path(p).unlink(missing_ok=True)


def publish_post(post_id: int) -> None:
    """Claim the post atomically (so two workers never double-post), then publish it.
    Transient errors (5xx / 429 / network) are retried automatically with backoff."""
    with SessionLocal() as db:
        claimed = db.execute(
            update(Post).where(Post.id == post_id, Post.status == "scheduled").values(status="posting")
        ).rowcount
        db.commit()
        if not claimed:
            return

        post = db.get(Post, post_id)
        account = post.account
        post.attempts = (post.attempts or 0) + 1
        try:
            if account.is_expired:
                raise linkedin.LinkedInError("Access token expired - reconnect LinkedIn.")
            image_urns = [
                linkedin.upload_image(account.access_token, account.urn, Path(p).read_bytes())
                for p in post.images
            ]
            post.linkedin_post_id = linkedin.create_post(
                account.access_token, account.urn, post.text, image_urns, post.visibility)
            post.status = "posted"
            post.published_at = datetime.utcnow()
            post.error = None
            delete_files(post.images)  # LinkedIn has the images now; free disk space
            post.image_paths = ""
        except Exception as exc:  # keep the scheduler alive no matter what
            _logger.exception("Publishing post %s failed", post_id)
            transient = getattr(exc, "transient", False) or isinstance(exc, requests.RequestException)
            if transient and post.attempts < MAX_ATTEMPTS:
                wait = BACKOFF_MINUTES[post.attempts - 1]
                post.status = "scheduled"
                post.scheduled_at = datetime.utcnow() + timedelta(minutes=wait)
                post.error = f"Attempt {post.attempts}/{MAX_ATTEMPTS} failed, retrying in {wait} min: {exc}"
            else:
                post.status = "failed"
                post.error = str(exc)
        db.commit()
        if post.status == "scheduled":  # transient failure -> retry at its backoff time
            arm(post_id, post.scheduled_at)


def publish_due_posts() -> None:
    with SessionLocal() as db:
        due = db.query(Post.id).filter(
            Post.status == "scheduled", Post.scheduled_at <= datetime.utcnow()
        ).all()
    for (post_id,) in due:
        publish_post(post_id)


def recover_stuck_posts() -> None:
    """A crash mid-publish leaves posts in 'posting'; put them back in the queue."""
    with SessionLocal() as db:
        db.execute(update(Post).where(Post.status == "posting").values(status="scheduled"))
        db.commit()


def start() -> None:
    recover_stuck_posts()
    scheduler.add_job(publish_due_posts, "interval", seconds=30,
                      id="publish_due", max_instances=1, coalesce=True, replace_existing=True)
    scheduler.start()
    with SessionLocal() as db:  # re-arm exact timers for everything already queued
        for p in db.query(Post).filter(Post.status == "scheduled"):
            arm(p.id, p.scheduled_at)

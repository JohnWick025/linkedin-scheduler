import secrets
import shutil
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import config, linkedin, scheduler
from .api import router as api_router
from .db import Account, Post, SessionLocal, init_db

TZ = ZoneInfo(config.APP_TIMEZONE)
MAX_LEN = 3000  # LinkedIn commentary limit
MAX_IMAGES, MAX_IMAGE_MB = 9, 8
EDITABLE = ("draft", "scheduled", "failed")
templates = Jinja2Templates(directory=str(config.BASE_DIR / "app" / "templates"))


def to_local(dt: datetime | None) -> str:
    if not dt:
        return ""
    return dt.replace(tzinfo=timezone.utc).astimezone(TZ).strftime("%Y-%m-%d %H:%M")


templates.env.filters["local"] = to_local


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler.start()
    yield
    scheduler.scheduler.shutdown(wait=False)


app = FastAPI(title="LinkedIn Scheduler", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY, same_site="lax")
app.include_router(api_router)


def _flash(request: Request, message: str) -> None:
    request.session["flash"] = message


@app.get("/")
def index(request: Request):
    with SessionLocal() as db:
        account = db.query(Account).first()
        posts = db.query(Post).order_by(Post.scheduled_at.desc()).limit(200).all()
    stats = {k: sum(p.status == k for p in posts) for k in ("scheduled", "draft", "posted", "failed")}
    nxt = min((p for p in posts if p.status == "scheduled"), key=lambda p: p.scheduled_at, default=None)
    days_left = (account.expires_at - datetime.utcnow()).days if account else None
    local_iso = lambda dt: dt.replace(tzinfo=timezone.utc).astimezone(TZ).strftime("%Y-%m-%dT%H:%M")
    return templates.TemplateResponse(request, "index.html", {
        "account": account,
        "posts": posts, "stats": stats, "next_post": nxt, "days_left": days_left,
        "local_iso": local_iso,
        "flash": request.session.pop("flash", None),
        "tz": config.APP_TIMEZONE,
        "configured": bool(config.CLIENT_ID and config.CLIENT_SECRET),
    })


# ---------------------------------------------------------------- OAuth
@app.get("/connect")
def connect(request: Request):
    if not (config.CLIENT_ID and config.CLIENT_SECRET):
        _flash(request, "Set LINKEDIN_CLIENT_ID and LINKEDIN_CLIENT_SECRET in .env first.")
        return RedirectResponse("/")
    state = secrets.token_urlsafe(24)
    request.session["oauth_state"] = state  # CSRF protection
    return RedirectResponse(linkedin.authorization_url(state))


@app.get("/callback")
def callback(request: Request, code: str | None = None, state: str | None = None,
             error: str | None = None, error_description: str | None = None):
    if error:
        _flash(request, f"LinkedIn: {error_description or error}")
        return RedirectResponse("/")
    if not code or not state or state != request.session.pop("oauth_state", None):
        _flash(request, "Authentication failed (invalid state). Please try again.")
        return RedirectResponse("/")
    try:
        token = linkedin.exchange_code(code)
        info = linkedin.get_userinfo(token["access_token"])
    except linkedin.LinkedInError as exc:
        _flash(request, str(exc))
        return RedirectResponse("/")

    expires_at = datetime.utcnow() + timedelta(seconds=int(token.get("expires_in", 5184000)))
    with SessionLocal() as db:
        account = db.query(Account).filter_by(member_id=info["sub"]).first()
        if account is None:
            account = Account(member_id=info["sub"])
            db.add(account)
        account.name = info.get("name", "")
        account.access_token = token["access_token"]
        account.expires_at = expires_at
        db.commit()
    _flash(request, f"Connected as {info.get('name', 'your account')}.")
    return RedirectResponse("/", status_code=303)


@app.post("/disconnect")
def disconnect(request: Request):
    with SessionLocal() as db:
        for post in db.query(Post).filter(Post.status == "scheduled"):
            db.delete(post)
        for account in db.query(Account):
            db.delete(account)
        db.commit()
    _flash(request, "Disconnected. Scheduled posts were removed.")
    return RedirectResponse("/", status_code=303)


# ---------------------------------------------------------------- Posts
def _back(request: Request, msg: str):
    _flash(request, msg)
    return RedirectResponse("/", status_code=303)


def _parse_local(value: str) -> datetime | None:
    """datetime-local string in APP_TIMEZONE -> naive UTC."""
    try:
        return datetime.fromisoformat(value).replace(tzinfo=TZ).astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        return None


def _resolve_time(action: str, scheduled_at: str):
    """Returns (status, run_at, error)."""
    if action == "draft":
        return "draft", datetime.utcnow(), None
    if action == "now":
        return "scheduled", datetime.utcnow(), None
    run_at = _parse_local(scheduled_at)
    if run_at is None:
        return None, None, "Pick a valid date and time."
    if run_at <= datetime.utcnow():
        return None, None, "Scheduled time must be in the future."
    return "scheduled", run_at, None


def _check_text(text: str) -> str | None:
    if not text:
        return "Post text is required."
    if len(text) > MAX_LEN:
        return f"Post is too long ({len(text)}/{MAX_LEN})."
    return None


@app.post("/posts")
async def create_post(
    request: Request,
    text: str = Form(...),
    action: str = Form("now"),             # now | schedule | draft
    scheduled_at: str = Form(""),          # datetime-local, in APP_TIMEZONE
    visibility: str = Form("PUBLIC"),
    images: list[UploadFile] = File(default=[]),
):
    text = text.strip()
    if err := _check_text(text):
        return _back(request, err)
    files = [u for u in images if u.filename]
    if len(files) > MAX_IMAGES:
        return _back(request, f"Max {MAX_IMAGES} images per post.")

    with SessionLocal() as db:
        account = db.query(Account).first()
        if not account:
            return _back(request, "Connect LinkedIn first.")
        status, run_at, err = _resolve_time(action, scheduled_at)
        if err:
            return _back(request, err)

        saved = []
        for upload in files:
            suffix = "." + upload.filename.rsplit(".", 1)[-1].lower() if "." in upload.filename else ""
            data = await upload.read()
            if suffix not in (".jpg", ".jpeg", ".png", ".gif"):
                scheduler.delete_files(saved)
                return _back(request, "Only JPG, PNG or GIF images are supported.")
            if len(data) > MAX_IMAGE_MB * 1024 * 1024:
                scheduler.delete_files(saved)
                return _back(request, f"{upload.filename} is larger than {MAX_IMAGE_MB} MB.")
            path = config.UPLOAD_DIR / f"{uuid.uuid4().hex}{suffix}"
            path.write_bytes(data)
            saved.append(str(path))

        post = Post(account_id=account.id, text=text, scheduled_at=run_at, status=status,
                    visibility="CONNECTIONS" if visibility == "CONNECTIONS" else "PUBLIC",
                    image_paths="\n".join(saved))
        db.add(post)
        db.commit()
        post_id = post.id

    if action == "now":
        scheduler.scheduler.add_job(scheduler.publish_post, args=[post_id])  # don't block the request
        return _back(request, "Publishing now - status updates below.")
    if action == "schedule":
        scheduler.arm(post_id, run_at)
    if action == "draft":
        return _back(request, "Saved as draft.")
    return _back(request, f"Scheduled for {to_local(run_at)} ({config.APP_TIMEZONE}).")


@app.post("/posts/{post_id}/edit")
def edit_post(post_id: int, request: Request, text: str = Form(...), action: str = Form("schedule"),
              scheduled_at: str = Form(""), visibility: str = Form("PUBLIC")):
    text = text.strip()
    if err := _check_text(text):
        return _back(request, err)
    with SessionLocal() as db:
        post = db.get(Post, post_id)
        if not post or post.status not in EDITABLE:
            return _back(request, "This post can no longer be edited.")
        status, run_at, err = _resolve_time(action, scheduled_at)
        if err:
            return _back(request, err)
        post.text, post.status, post.scheduled_at, post.error = text, status, run_at, None
        post.visibility = "CONNECTIONS" if visibility == "CONNECTIONS" else "PUBLIC"
        post.attempts = 0
        db.commit()
    scheduler.arm(post_id, run_at) if status == "scheduled" else scheduler.disarm(post_id)
    return _back(request, "Draft updated." if status == "draft" else f"Rescheduled for {to_local(run_at)}.")


@app.post("/posts/{post_id}/publish-now")
def publish_now(post_id: int, request: Request):
    with SessionLocal() as db:
        post = db.get(Post, post_id)
        if not post or post.status not in ("draft", "scheduled", "failed"):
            return _back(request, "Nothing to publish.")
        post.status, post.scheduled_at, post.error, post.attempts = "scheduled", datetime.utcnow(), None, 0
        db.commit()
    scheduler.scheduler.add_job(scheduler.publish_post, args=[post_id])
    return _back(request, "Publishing now - status updates below.")


@app.post("/posts/{post_id}/duplicate")
def duplicate_post(post_id: int, request: Request):
    with SessionLocal() as db:
        post = db.get(Post, post_id)
        if not post:
            return _back(request, "Post not found.")
        copies = []
        for p in post.images:  # copy files so deleting one post never breaks the other
            if Path(p).exists():
                dest = config.UPLOAD_DIR / f"{uuid.uuid4().hex}{Path(p).suffix}"
                shutil.copy(p, dest)
                copies.append(str(dest))
        db.add(Post(account_id=post.account_id, text=post.text, status="draft",
                    scheduled_at=datetime.utcnow(), visibility=post.visibility,
                    image_paths="\n".join(copies)))
        db.commit()
    return _back(request, "Duplicated as a draft.")


@app.post("/posts/{post_id}/delete")
def delete_post(post_id: int, request: Request):
    with SessionLocal() as db:
        post = db.get(Post, post_id)
        if post and post.status in EDITABLE:
            scheduler.delete_files(post.images)
            db.delete(post)
            db.commit()
            scheduler.disarm(post_id)
    return RedirectResponse("/", status_code=303)


@app.post("/posts/{post_id}/retry")
def retry_post(post_id: int):
    with SessionLocal() as db:
        post = db.get(Post, post_id)
        if post and post.status == "failed":
            post.status, post.scheduled_at, post.error, post.attempts = "scheduled", datetime.utcnow(), None, 0
            db.commit()
    return RedirectResponse("/", status_code=303)


@app.get("/health")
def health():
    return JSONResponse({"ok": True, "scheduler_running": scheduler.scheduler.running})

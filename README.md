# LinkedIn Scheduler (personal profile)

Connect LinkedIn via OAuth, then post now or schedule posts to your **personal profile**.

## Setup

```powershell
cd linkedin_scheduler
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env      # then fill LINKEDIN_CLIENT_ID / LINKEDIN_CLIENT_SECRET
uvicorn app.main:app --port 8000
```

Open http://localhost:8000 and click **Connect LinkedIn**.

In the LinkedIn developer app, **Auth** tab → Authorized redirect URLs must contain exactly
`http://localhost:8000/callback`, and the Products tab needs *Share on LinkedIn* and
*Sign In with LinkedIn using OpenID Connect*.

Run a single process (no `--reload`, no multiple workers): the scheduler lives in-process.
Posts are claimed atomically in the DB so a duplicate tick cannot double-post.

## Notes
- Tokens last ~60 days; the UI shows a Reconnect button when expired.
- Tokens are stored in plain text in `scheduler.db` – keep the file private.
- Scheduler checks for due posts every 30 seconds.

## Features
- Compose with live LinkedIn-style preview, character counter, drag & drop images (max 9, 8 MB each)
- Post now, schedule, or save as draft; quick-schedule chips; edit / reschedule / duplicate / delete
- Visibility: Public or Connections only; #hashtags become clickable
- Auto-retry on temporary failures (3 attempts: now, +2 min, +10 min); manual Retry for hard failures
- Crash recovery: posts stuck in "posting" are re-queued on startup
- Token-expiry warning banner (7 days), queue filters, stats, `/health` endpoint
- Existing `scheduler.db` is migrated automatically on startup

## UI
Two tabs: Write post and Queue. The page never scrolls; only the queue's record list does. Departure-board queue with flip-tile times, 14-day picker, Write/Preview, manual light/dark toggle (true black in dark mode; remembers your choice, defaults to system), keyboard focus and reduced-motion support. Fonts load from Google Fonts and fall back to system fonts offline.

## How scheduling works (important)
LinkedIn's public API has **no "publish at" field** - a post sent to `POST /rest/posts` goes live immediately. So the schedule lives
in this app's database, and the app calls LinkedIn at the chosen time. Each scheduled post gets its own exact-time timer (fires at the
chosen second), backed by a 30-second safety poller that also re-arms everything after a restart. Posts are claimed atomically, so a
post can never be published twice. **The app must be running at the scheduled time.**

## REST API
Set `API_KEY` in `.env` (see `.env.example`), restart, and send it as `X-API-Key: <key>` (or `Authorization: Bearer <key>`).
Interactive docs: `http://localhost:8000/docs`. LinkedIn must already be connected through the web UI.

| Method & path | Purpose |
|---|---|
| `GET /api/v1/time` | Server clock, weekday and timezone (lets the agent resolve "Monday 2pm") |
| `GET /api/v1/status` | LinkedIn connected? token expiry, scheduler running, queue size |
| `POST /api/v1/posts` | Create: `mode` = `schedule` (default) / `now` / `draft` |
| `GET /api/v1/posts?status=scheduled` | List (filter: draft, scheduled, posting, posted, failed) |
| `GET /api/v1/posts/{id}` | One post (poll this for `posted` + `linkedin_post_id`) |
| `PATCH /api/v1/posts/{id}` | Edit text / visibility / reschedule |
| `DELETE /api/v1/posts/{id}` | Cancel a draft / scheduled / failed post |
| `POST /api/v1/posts/{id}/publish-now` | Publish immediately |

```bash
curl -X POST http://localhost:8000/api/v1/posts -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" -d '{
  "text": "Shipping day! #product",
  "scheduled_at": "2026-11-23T14:00:00+05:30",
  "visibility": "PUBLIC",
  "idempotency_key": "telegram-chat123-msg456"
}'
```
`scheduled_at` is ISO 8601. With an offset (`+05:30`, `Z`) it is exact; with no offset it is read in `APP_TIMEZONE`.
Past times, times over 365 days away, text over 3000 chars and an expired token are rejected with a clear 4xx message.
`idempotency_key` makes retries safe: sending the same key twice returns the first post (HTTP 200, `"duplicate": true`).
The API is text-only for now; images go through the web UI.

### Telegram agent wiring
1. The agent turns "post this at 2 pm on Monday" into an ISO timestamp. It should call `GET /api/v1/time` first so "Monday" is
   resolved against the server's date and timezone.
2. It calls `POST /api/v1/posts` with `idempotency_key` = the Telegram `chat_id:message_id`.
3. It replies using the response (`scheduled_at_local`, `warnings`), and can poll `GET /api/v1/posts/{id}` later for the LinkedIn URN.
4. If the agent runs on another machine, expose this app over HTTPS (reverse proxy, Cloudflare Tunnel or ngrok). Never expose it without `API_KEY`.

## Testing
```bash
pip install -r requirements.txt -r requirements-dev.txt

# 1) Automated tests - no LinkedIn needed (LinkedIn calls are faked, everything else is real, incl. the timer)
pytest -v

# 2) Manual walkthrough against your running app and your real LinkedIn account
export API_KEY=...            # same value as in .env
./scripts/api_demo.sh         # schedules a post 2 minutes ahead and polls until it is published
```
What the automated suite proves: auth, timezone conversion (`14:00+05:30` -> `08:30Z`), past/garbage input rejection, idempotency,
token-expiry handling, the post firing at the scheduled second exactly once, retry on temporary LinkedIn errors, no retry on hard
errors, edit/reschedule/cancel (a cancelled post never reaches LinkedIn), and that published posts cannot be edited.

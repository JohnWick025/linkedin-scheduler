#!/usr/bin/env bash
# Manual walkthrough of the REST API against a RUNNING server.
#   export API_KEY=...   (same value as in .env)    BASE=http://localhost:8000 ./scripts/api_demo.sh
BASE="${BASE:-http://localhost:8000}"; KEY="${API_KEY:?set API_KEY first}"
H=(-H "X-API-Key: $KEY" -H "Content-Type: application/json")
say() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

say "1. Is the app connected to LinkedIn and is the scheduler running?"
curl -s "${H[@]}" "$BASE/api/v1/status"; echo

say "2. What time does the server think it is? (the agent uses this to resolve 'Monday 2pm')"
curl -s "${H[@]}" "$BASE/api/v1/time"; echo

say "3. Schedule a post 2 minutes from now (watch it publish)"
WHEN=$(python3 -c "from datetime import datetime,timedelta;from zoneinfo import ZoneInfo;import os;print((datetime.now(ZoneInfo(os.getenv('APP_TIMEZONE','Asia/Kolkata')))+timedelta(minutes=2)).isoformat(timespec='seconds'))")
RESP=$(curl -s "${H[@]}" -X POST "$BASE/api/v1/posts" -d "{\"text\":\"API test post - safe to delete $(date +%H:%M)\",\"scheduled_at\":\"$WHEN\",\"idempotency_key\":\"demo-$(date +%s)\"}")
echo "$RESP"; ID=$(echo "$RESP" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")

say "4. Poll until it is published (Ctrl-C to stop)"
for i in $(seq 1 30); do
  S=$(curl -s "${H[@]}" "$BASE/api/v1/posts/$ID"); echo "$S" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['status'],d.get('linkedin_post_id') or '',d.get('error') or '')"
  echo "$S" | grep -q '"status":"posted"' && break; sleep 10
done

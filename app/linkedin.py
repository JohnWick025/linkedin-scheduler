"""Thin wrapper around the LinkedIn OAuth + Posts REST APIs (personal profile)."""
import re
from urllib.parse import urlencode

import requests

from . import config

API = "https://api.linkedin.com/rest/"
AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
USERINFO_URL = "https://api.linkedin.com/v2/userinfo"


class LinkedInError(Exception):
    """transient=True means a retry may succeed (5xx, 429, network errors)."""

    def __init__(self, message: str, transient: bool = False):
        super().__init__(message)
        self.transient = transient


def authorization_url(state: str) -> str:
    return AUTH_URL + "?" + urlencode({
        "response_type": "code",
        "client_id": config.CLIENT_ID,
        "redirect_uri": config.REDIRECT_URI,
        "state": state,
        "scope": config.SCOPE,
    })


def exchange_code(code: str) -> dict:
    """Returns {'access_token': ..., 'expires_in': seconds, ...}."""
    resp = requests.post(TOKEN_URL, data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": config.REDIRECT_URI,
        "client_id": config.CLIENT_ID,
        "client_secret": config.CLIENT_SECRET,
    }, timeout=10)
    data = resp.json()
    if not resp.ok or "access_token" not in data:
        raise LinkedInError(data.get("error_description") or resp.text)
    return data


def get_userinfo(token: str) -> dict:
    resp = requests.get(USERINFO_URL, headers={"Authorization": f"Bearer {token}"}, timeout=10)
    if not resp.ok:
        raise LinkedInError(f"Could not read profile: {resp.text}")
    return resp.json()  # sub, name, picture, ...


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "X-Restli-Protocol-Version": "2.0.0",
        "LinkedIn-Version": config.LINKEDIN_VERSION,
        "cache-control": "no-cache",
    }


_RESERVED = re.compile(r"[\(\)\<\>\{\}\[\]\_\|\*\~\#\@]")
_HASHTAG = re.compile(r"(?<![\w#])#([A-Za-z0-9_]+)")


def escape_little_text(text: str) -> str:
    """Escape LinkedIn 'little text' reserved chars, but keep #hashtags clickable."""
    out, last = [], 0
    for m in _HASHTAG.finditer(text):
        out.append(_RESERVED.sub(lambda r: "\\" + r.group(0), text[last:m.start()]))
        out.append("{hashtag|\\#|" + m.group(1) + "}")
        last = m.end()
    out.append(_RESERVED.sub(lambda r: "\\" + r.group(0), text[last:]))
    return "".join(out)


def upload_image(token: str, owner_urn: str, data: bytes) -> str:
    """Two-step upload. Returns the image URN."""
    resp = requests.post(
        API + "images?action=initializeUpload",
        headers=_headers(token),
        json={"initializeUploadRequest": {"owner": owner_urn}},
        timeout=10,
    )
    value = resp.json().get("value") if resp.ok else None
    if not value or "uploadUrl" not in value:
        raise LinkedInError(f"Image upload registration failed: {resp.text}")

    up = requests.post(
        value["uploadUrl"],
        data=data,
        headers={**_headers(token), "Content-Type": "application/octet-stream"},
        timeout=30,
    )
    if not up.ok:
        raise LinkedInError(f"Image upload failed: {up.text}")
    return value["image"]


def create_post(token: str, author_urn: str, text: str, image_urns: list[str],
                visibility: str = "PUBLIC") -> str:
    """Publish a post. Returns the post URN (urn:li:share:... / urn:li:ugcPost:...)."""
    body = {
        "author": author_urn,
        "commentary": escape_little_text(text),
        "visibility": visibility if visibility in ("PUBLIC", "CONNECTIONS") else "PUBLIC",
        "distribution": {"feedDistribution": "MAIN_FEED"},
        "lifecycleState": "PUBLISHED",
    }
    if len(image_urns) == 1:
        body["content"] = {"media": {"id": image_urns[0]}}
    elif len(image_urns) > 1:
        body["content"] = {"multiImage": {"images": [{"id": u} for u in image_urns]}}

    try:
        resp = requests.post(API + "posts", headers=_headers(token), json=body, timeout=15)
    except requests.RequestException as exc:
        raise LinkedInError(f"Network error: {exc}", transient=True)
    post_id = resp.headers.get("x-restli-id")
    if resp.ok and post_id:
        return post_id
    try:
        message = resp.json().get("message", resp.text)
    except ValueError:
        message = resp.text
    raise LinkedInError(f"{resp.status_code}: {message}",
                        transient=resp.status_code >= 500 or resp.status_code == 429)

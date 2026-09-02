#!/usr/bin/env python3
"""
Google Drive integration for the "Save to Google Drive" feature.

Saving is one-click from the UI — nobody signs in or approves a consent
screen at use time. The backend holds one set of credentials and uses them
for every upload; the only setup is generating those credentials once.

Folder layout produced on Drive:

    <ROOT_FOLDER>/
      <Course Name>/                              (created once, reused after)
        <Course Name> - <Unit Number> - <Chapter Number>.mp4
        <Course Name> - <Unit Number> - <Chapter Number>.mp4
        ...
      <Another Course>/
        ...

CONFIGURATION (env vars on the Railway backend service)

Mode 1 — OAuth refresh token. PREFERRED, and identical to the Podcast
Creator, so the same three credential values work in both services:

    GDRIVE_CLIENT_ID        xxx.apps.googleusercontent.com
    GDRIVE_CLIENT_SECRET    the matching client secret
    GDRIVE_REFRESH_TOKEN    generated once locally, never expires in prod
    GDRIVE_FOLDER_URL       root folder — full URL or bare ID

Uploads happen as *you*, against your own Drive storage quota, so an
ordinary personal My Drive folder works.

Mode 2 — Service account. Fallback only, used when the three GDRIVE_*
values above are not all present:

    GOOGLE_SERVICE_ACCOUNT_JSON   full key JSON as a single-line string
    GOOGLE_SERVICE_ACCOUNT_FILE   path to a key JSON file instead
    GOOGLE_DRIVE_ROOT_FOLDER_ID   root folder ID

A service account has no storage quota of its own, so it cannot own files
in a personal My Drive — mode 2 only works if the root folder lives inside
a Google Workspace Shared Drive. If you are seeing storageQuotaExceeded,
you are in mode 2 and should be in mode 1; check that all three GDRIVE_*
variables are set, since a single missing one silently falls back here.
"""
import json
import os
import re
import threading
import unicodedata
from pathlib import Path

from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials as UserCredentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/drive"]

# Fallback root, used only when neither folder variable is set.
DEFAULT_ROOT_FOLDER_ID = "1cY7v7956TyrJbGPGno4QQJ5Zpj6bXDjZ"

FOLDER_MIME = "application/vnd.google-apps.folder"

_service = None
_service_lock = threading.Lock()

# Folder ids already found/created this process, so repeat saves into the
# same course don't re-search Drive every time.
_folder_cache: dict[str, str] = {}
_folder_cache_lock = threading.Lock()


class DriveNotConfigured(RuntimeError):
    """Raised when no usable Drive credentials are configured."""


# ── configuration helpers ────────────────────────────────────────────────

def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def auth_mode() -> str:
    """Which credential mode this process will use: "oauth",
    "service_account", or "unconfigured". Exposed so /healthz can report it
    — this is the single most useful thing to check when Drive saves fail,
    because the fallback to service_account is otherwise silent."""
    if _env("GDRIVE_CLIENT_ID") and _env("GDRIVE_CLIENT_SECRET") and _env("GDRIVE_REFRESH_TOKEN"):
        return "oauth"
    if _env("GOOGLE_SERVICE_ACCOUNT_JSON") or _env("GOOGLE_SERVICE_ACCOUNT_FILE"):
        return "service_account"
    return "unconfigured"


def _extract_folder_id(value: str) -> str:
    """Accept a bare folder ID or a full Drive URL, so the variable can be
    pasted straight from the browser address bar."""
    value = (value or "").strip()
    if not value:
        return ""
    match = re.search(r"/folders/([a-zA-Z0-9_-]+)", value)
    if match:
        return match.group(1)
    match = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", value)
    if match:
        return match.group(1)
    return value  # already a bare ID


def _root_folder_id() -> str:
    """Resolved per call rather than at import time, so a Railway variable
    change takes effect on restart with no code change."""
    return (
        _extract_folder_id(_env("GDRIVE_FOLDER_URL"))
        or _extract_folder_id(_env("GOOGLE_DRIVE_ROOT_FOLDER_ID"))
        or DEFAULT_ROOT_FOLDER_ID
    )


def _clean(name: str, max_len: int = 120) -> str:
    """Same conservative cleanup pipeline.py uses for filenames, reused for
    Drive folder/file names."""
    if not name:
        return ""
    name = unicodedata.normalize("NFKC", name)
    name = "".join(ch for ch in name if unicodedata.category(ch)[0] != "C")
    name = re.sub(r"[\\/]+", "-", name)  # "/" is path-like in some Drive UIs
    name = re.sub(r"\s+", " ", name).strip()
    return name[:max_len] or "Untitled"


# ── credentials ──────────────────────────────────────────────────────────

def _get_credentials():
    mode = auth_mode()

    if mode == "oauth":
        creds = UserCredentials(
            token=None,
            refresh_token=_env("GDRIVE_REFRESH_TOKEN"),
            client_id=_env("GDRIVE_CLIENT_ID"),
            client_secret=_env("GDRIVE_CLIENT_SECRET"),
            token_uri="https://oauth2.googleapis.com/token",
            scopes=SCOPES,
        )
        # Trade the refresh token for an access token now, so a bad
        # credential fails here with a clear message instead of midway
        # through a large upload.
        creds.refresh(GoogleAuthRequest())
        return creds

    if mode == "service_account":
        raw_json = _env("GOOGLE_SERVICE_ACCOUNT_JSON")
        if raw_json:
            info = json.loads(raw_json)
            return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        return service_account.Credentials.from_service_account_file(
            _env("GOOGLE_SERVICE_ACCOUNT_FILE"), scopes=SCOPES
        )

    raise DriveNotConfigured(
        "Google Drive isn't configured. Set GDRIVE_CLIENT_ID, "
        "GDRIVE_CLIENT_SECRET, GDRIVE_REFRESH_TOKEN and GDRIVE_FOLDER_URL on "
        "this backend service — the same four values the Podcast Creator uses."
    )


def get_service():
    """Lazily build and cache the Drive API client."""
    global _service
    with _service_lock:
        if _service is None:
            # NOTE: the keyword is cache_discovery (snake_case). On
            # google-api-python-client 2.x static discovery is the default
            # anyway, so it is simply omitted.
            _service = build("drive", "v3", credentials=_get_credentials())
        return _service


def reset_service_cache() -> None:
    """Drop the cached client and folder ids. Useful after changing
    credentials without a full restart."""
    global _service
    with _service_lock:
        _service = None
    with _folder_cache_lock:
        _folder_cache.clear()


# ── folders ──────────────────────────────────────────────────────────────

def _find_folder(service, name: str, parent_id: str) -> str | None:
    safe_name = name.replace("'", "\\'")
    query = (
        f"mimeType = '{FOLDER_MIME}' and name = '{safe_name}' "
        f"and '{parent_id}' in parents and trashed = false"
    )
    resp = service.files().list(
        q=query, spaces="drive", fields="files(id, name)", pageSize=1,
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute()
    files = resp.get("files", [])
    return files[0]["id"] if files else None


def _create_folder(service, name: str, parent_id: str) -> str:
    metadata = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}
    folder = service.files().create(
        body=metadata, fields="id", supportsAllDrives=True,
    ).execute()
    return folder["id"]


def get_or_create_course_folder(course_name: str) -> str:
    """Find this course's folder under the root, or create it on the first
    video for that course and reuse it for every unit/chapter after."""
    clean_name = _clean(course_name)
    cache_key = clean_name.lower()

    with _folder_cache_lock:
        cached = _folder_cache.get(cache_key)
    if cached:
        return cached

    service = get_service()
    root_id = _root_folder_id()

    # Folder lookup/creation is wrapped too — a wrong root folder id fails
    # here, before any upload starts, and the raw Google error is unhelpful.
    try:
        folder_id = _find_folder(service, clean_name, root_id)
        if not folder_id:
            folder_id = _create_folder(service, clean_name, root_id)
    except HttpError as e:
        raise RuntimeError(_explain_drive_error(e)) from e

    with _folder_cache_lock:
        _folder_cache[cache_key] = folder_id
    return folder_id


def build_drive_filename(course_name: str, unit_number: str, chapter_number: str) -> str:
    """course name + unit number + chapter number. Chapter is optional —
    some courses are organised by unit alone — so it drops out when blank."""
    parts = [p for p in (course_name, unit_number, chapter_number) if p and p.strip()]
    return _clean(" - ".join(p.strip() for p in parts)) + ".mp4"


# ── upload ───────────────────────────────────────────────────────────────

def upload_video(course_name: str, unit_number: str, chapter_number: str,
                 file_path: str | Path) -> dict:
    """Upload one finished video into <root>/<course name>/. Returns
    {file_id, name, web_view_link, folder_id}."""
    service = get_service()
    folder_id = get_or_create_course_folder(course_name)
    filename = build_drive_filename(course_name, unit_number, chapter_number)

    metadata = {"name": filename, "parents": [folder_id]}
    media = MediaFileUpload(
        str(file_path), mimetype="video/mp4", resumable=True,
        chunksize=8 * 1024 * 1024,
    )

    request = service.files().create(
        body=metadata, media_body=media, fields="id, name, webViewLink",
        supportsAllDrives=True,
    )
    try:
        response = None
        while response is None:
            _status, response = request.next_chunk()
    except HttpError as e:
        raise RuntimeError(_explain_drive_error(e)) from e

    return {
        "file_id": response["id"],
        "name": response["name"],
        "web_view_link": response.get("webViewLink"),
        "folder_id": folder_id,
    }


def _explain_drive_error(exc: Exception) -> str:
    """Translate the Drive failures that actually come up during setup into
    messages that name the fix. These surface directly in the UI, so they
    are written for whoever is configuring the deploy — and they branch on
    auth_mode(), because the same Google error means different things in
    the two modes."""
    text = str(exc)
    mode = auth_mode()

    if "storageQuotaExceeded" in text:
        if mode == "oauth":
            return (
                "Your Google account's Drive storage is full, so the upload "
                "was rejected. Free up space in Drive (or move the root "
                "folder to an account with room) and try again."
            )
        return (
            "This backend is using SERVICE ACCOUNT credentials, and a "
            "service account has no storage of its own, so it cannot save "
            "into a personal My Drive. Set GDRIVE_CLIENT_ID, "
            "GDRIVE_CLIENT_SECRET and GDRIVE_REFRESH_TOKEN on this service "
            "to upload as a real Google account instead — all three must be "
            "present or it silently falls back to the service account. "
            "Check /healthz to see which mode is active."
        )

    if "notFound" in text or "File not found" in text:
        if mode == "oauth":
            return (
                "Drive could not find the root folder. Check GDRIVE_FOLDER_URL "
                "points at a folder the signed-in Google account can open, and "
                "that the ID was copied whole (no truncation, no ?usp=sharing "
                "fragment attached to a bare ID)."
            )
        return (
            "Drive could not find the root folder. Check "
            "GOOGLE_DRIVE_ROOT_FOLDER_ID is just the ID from the URL and that "
            "the folder is shared with the service account."
        )

    if "insufficientFilePermissions" in text or "forbidden" in text.lower():
        if mode == "oauth":
            return (
                "The signed-in Google account can see the root folder but "
                "cannot write to it. Use a folder that account owns, or have "
                "the owner grant it Editor access."
            )
        return (
            "The service account can see the folder but cannot write to it. "
            "Re-share the folder with its client_email as Editor, not Viewer."
        )

    if "invalid_grant" in text:
        if mode == "oauth":
            return (
                "The refresh token was rejected. Usually this means the OAuth "
                "consent screen is still in Testing mode, where refresh tokens "
                "expire after 7 days — publish it to In production and "
                "regenerate the token. It also happens if access was revoked "
                "or the token was truncated when pasted."
            )
        return (
            "Authentication failed. The private key in "
            "GOOGLE_SERVICE_ACCOUNT_JSON is likely malformed — re-paste the "
            "key file contents into the Railway variable."
        )

    return f"Google Drive error: {text}"

#!/usr/bin/env python3
"""
Google Drive integration for the "Save to Google Drive" feature.

Saving is one-click from the UI — nobody signs in or approves a consent
screen at use time. The backend holds a single set of credentials that it
uses for every upload; the one-time setup is generating those credentials
(see the configuration block below).

Folder layout produced on Drive:

    <ROOT_FOLDER>/
      <Course Name>/                              (created once, reused after)
        <Course Name>_<Unit Number>_<Chapter Number>.mp4
        <Course Name>_<Unit Number>_<Chapter Number>.mp4
        ...
      <Another Course>/
        ...

Configuration (env vars, set on Railway) — preferred OAuth mode, identical
to the Podcast Creator so the same values can be pasted into both services:

  GDRIVE_CLIENT_ID        OAuth client ID   (xxx.apps.googleusercontent.com)
  GDRIVE_CLIENT_SECRET    OAuth client secret
  GDRIVE_REFRESH_TOKEN    long-lived refresh token generated once, locally
  GDRIVE_FOLDER_URL       the root Drive folder — full URL or bare ID

Legacy service-account mode is still supported as a fallback:

  GOOGLE_SERVICE_ACCOUNT_JSON   full key JSON as a single-line string
  GOOGLE_SERVICE_ACCOUNT_FILE   path to a key JSON file instead
  GOOGLE_DRIVE_ROOT_FOLDER_ID   root folder ID

Only use the service-account mode if the root folder lives inside a Google
Workspace **Shared Drive** — service accounts have no storage quota of
their own and cannot own files in a personal My Drive.
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

# The folder ID from the link you shared. Overridable via env var so this
# can point at a different root without a code change.
DEFAULT_ROOT_FOLDER_ID = "1cY7v7956TyrJbGPGno4QQJ5Zpj6bXDjZ"


def _extract_folder_id(value: str) -> str:
    """Accept either a bare folder ID or a full Drive folder URL, so
    GDRIVE_FOLDER_URL can be pasted straight from the browser address bar
    (this matches how the Podcast Creator reads the same variable)."""
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
    """Resolved per call rather than at import time, so changing the
    Railway variable takes effect on restart without a code change."""
    return (
        _extract_folder_id(os.environ.get("GDRIVE_FOLDER_URL", ""))
        or _extract_folder_id(os.environ.get("GOOGLE_DRIVE_ROOT_FOLDER_ID", ""))
        or DEFAULT_ROOT_FOLDER_ID
    )

FOLDER_MIME = "application/vnd.google-apps.folder"

_service = None
_service_lock = threading.Lock()

# Caches folder ids we've already created/found this process, so repeat
# saves into the same course don't re-search Drive every time.
_folder_cache: dict[str, str] = {}
_folder_cache_lock = threading.Lock()


class DriveNotConfigured(RuntimeError):
    """Raised when no service-account credentials are set up yet."""


def _clean(name: str, max_len: int = 120) -> str:
    """Same conservative cleanup pipeline.py uses for filenames, reused
    here for Drive folder/file names (Drive is far more permissive than a
    filesystem, but we still want to strip control chars and slashes)."""
    if not name:
        return ""
    name = unicodedata.normalize("NFKC", name)
    name = "".join(ch for ch in name if unicodedata.category(ch)[0] != "C")
    name = re.sub(r"[\\/]+", "-", name)  # Drive treats "/" as a path-like char in some UIs
    name = re.sub(r"\s+", " ", name).strip()
    return name[:max_len] or "Untitled"


def _get_credentials():
    """Two supported modes, checked in this order:

    1. **OAuth refresh token** (GDRIVE_CLIENT_ID / GDRIVE_CLIENT_SECRET /
       GDRIVE_REFRESH_TOKEN) — the same scheme the Podcast Creator uses.
       Uploads happen *as you*, using your own Drive storage quota, so an
       ordinary personal My Drive folder works fine. Preferred.
    2. **Service account** (GOOGLE_SERVICE_ACCOUNT_JSON / _FILE) — kept as
       a fallback, but note a service account has no storage quota of its
       own and can therefore only write inside a Workspace Shared Drive.
    """
    client_id = os.environ.get("GDRIVE_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GDRIVE_CLIENT_SECRET", "").strip()
    refresh_token = os.environ.get("GDRIVE_REFRESH_TOKEN", "").strip()

    if client_id and client_secret and refresh_token:
        creds = UserCredentials(
            token=None,
            refresh_token=refresh_token,
            client_id=client_id,
            client_secret=client_secret,
            token_uri="https://oauth2.googleapis.com/token",
            scopes=SCOPES,
        )
        # Exchange the long-lived refresh token for a short-lived access
        # token now, so a bad credential fails here with a clear message
        # rather than midway through a large upload.
        creds.refresh(GoogleAuthRequest())
        return creds

    raw_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    key_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")

    if raw_json:
        info = json.loads(raw_json)
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    if key_file:
        return service_account.Credentials.from_service_account_file(key_file, scopes=SCOPES)

    raise DriveNotConfigured(
        "Google Drive isn't configured yet. Set GDRIVE_CLIENT_ID, "
        "GDRIVE_CLIENT_SECRET, GDRIVE_REFRESH_TOKEN and GDRIVE_FOLDER_URL on "
        "the backend (the same four values the Podcast Creator uses)."
    )


def get_service():
    """Lazily build (and cache) the Drive API client. Building it per-call
    would work fine too, but this avoids repeating the credential parsing
    on every single save."""
    global _service
    with _service_lock:
        if _service is None:
            creds = _get_credentials()
            # NOTE: the keyword is cache_discovery (snake_case), not
            # cacheDiscovery. On google-api-python-client 2.x static
            # discovery is the default anyway, so it's simply omitted.
            _service = build("drive", "v3", credentials=creds)
        return _service


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
    """Find the existing Drive folder for this course under the root, or
    create one if this is the first video for that course. Reused on every
    subsequent unit/chapter for the same course, per the required layout."""
    clean_name = _clean(course_name)
    cache_key = clean_name.lower()

    with _folder_cache_lock:
        cached = _folder_cache.get(cache_key)
    if cached:
        return cached

    service = get_service()
    root_id = _root_folder_id()
    folder_id = _find_folder(service, clean_name, root_id)
    if not folder_id:
        folder_id = _create_folder(service, clean_name, root_id)

    with _folder_cache_lock:
        _folder_cache[cache_key] = folder_id
    return folder_id


def build_drive_filename(course_name: str, unit_number: str, chapter_number: str) -> str:
    """course name + unit number + chapter number, per the requested
    naming convention. Chapter is optional — some courses are organised by
    unit alone — so it's dropped cleanly when blank."""
    parts = [p for p in (course_name, unit_number, chapter_number) if p and p.strip()]
    return _clean(" - ".join(p.strip() for p in parts)) + ".mp4"


def upload_video(course_name: str, unit_number: str, chapter_number: str,
                  file_path: str | Path) -> dict:
    """Uploads one finished video into <root>/<course name>/, creating the
    course folder on first use and reusing it after. Returns
    {file_id, name, web_view_link, folder_id}."""
    service = get_service()
    folder_id = get_or_create_course_folder(course_name)
    filename = build_drive_filename(course_name, unit_number, chapter_number)

    metadata = {"name": filename, "parents": [folder_id]}
    media = MediaFileUpload(str(file_path), mimetype="video/mp4", resumable=True)

    request = service.files().create(
        body=metadata, media_body=media, fields="id, name, webViewLink",
        supportsAllDrives=True,
    )
    try:
        response = None
        while response is None:
            _status, response = request.next_chunk()
    except HttpError as e:
        # The raw Google errors are unreadable in the UI, so translate the
        # handful that actually come up during setup.
        raise RuntimeError(_explain_drive_error(e)) from e

    return {
        "file_id": response["id"],
        "name": response["name"],
        "web_view_link": response.get("webViewLink"),
        "folder_id": folder_id,
    }


def _explain_drive_error(exc: Exception) -> str:
    """Turn the common Google Drive setup failures into messages that say
    what to actually go and fix, since these surface directly in the UI."""
    text = str(exc)

    if "storageQuotaExceeded" in text:
        return (
            "The service account has no storage quota of its own, so it "
            "cannot own files in a personal My Drive. Move the root folder "
            "into a Google Workspace Shared Drive, add the service account "
            "as a member with Content manager access, and point "
            "GOOGLE_DRIVE_ROOT_FOLDER_ID at a folder inside that Shared "
            "Drive. Sharing a normal My Drive folder is not enough."
        )
    if "notFound" in text or "File not found" in text:
        return (
            "Drive could not find the root folder. Check that "
            "GOOGLE_DRIVE_ROOT_FOLDER_ID is just the ID from the URL (no "
            "https://, no ?usp=sharing) and that the folder is shared with "
            "the service account."
        )
    if "insufficientFilePermissions" in text or "forbidden" in text.lower():
        return (
            "The service account can see the folder but cannot write to it. "
            "Re-share the folder with its client_email and set the role to "
            "Editor rather than Viewer."
        )
    if "invalid_grant" in text:
        return (
            "Authentication failed. The private key in "
            "GOOGLE_SERVICE_ACCOUNT_JSON is likely malformed — re-paste the "
            "key file contents into the Railway variable."
        )
    return f"Google Drive error: {text}"

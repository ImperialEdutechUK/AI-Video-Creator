#!/usr/bin/env python3
"""
Google Drive integration for the "Save to Google Drive" feature.

Uses a Google Cloud *service account* rather than per-user OAuth, so saving
is genuinely one-click from the UI — nobody has to sign in or approve a
consent screen. The trade-off is a one-time setup step: the target Drive
folder has to be *shared with the service account's email address* before
uploads will work (service accounts don't have their own visible "My
Drive", they only see what's explicitly shared with them).

Folder layout produced on Drive:

    <ROOT_FOLDER>/
      <Course Name>/                              (created once, reused after)
        <Course Name>_<Unit Number>_<Chapter Number>.mp4
        <Course Name>_<Unit Number>_<Chapter Number>.mp4
        ...
      <Another Course>/
        ...

Configuration (env vars, set on Railway):
  GOOGLE_SERVICE_ACCOUNT_JSON   the full service-account key JSON, as a
                                 single-line string (recommended for Railway)
  GOOGLE_SERVICE_ACCOUNT_FILE   path to a key JSON file instead, if you'd
                                 rather mount it as a file
  GOOGLE_DRIVE_ROOT_FOLDER_ID   the Drive folder everything gets organised
                                 under. Defaults to the folder you shared:
                                 https://drive.google.com/drive/folders/1cY7v7956TyrJbGPGno4QQJ5Zpj6bXDjZ
"""
import json
import os
import re
import threading
import unicodedata
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/drive"]

# The folder ID from the link you shared. Overridable via env var so this
# can point at a different root without a code change.
DEFAULT_ROOT_FOLDER_ID = "1cY7v7956TyrJbGPGno4QQJ5Zpj6bXDjZ"
ROOT_FOLDER_ID = os.environ.get("GOOGLE_DRIVE_ROOT_FOLDER_ID", DEFAULT_ROOT_FOLDER_ID)

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
    raw_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    key_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")

    if raw_json:
        info = json.loads(raw_json)
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    if key_file:
        return service_account.Credentials.from_service_account_file(key_file, scopes=SCOPES)
    raise DriveNotConfigured(
        "Google Drive isn't configured yet. Set GOOGLE_SERVICE_ACCOUNT_JSON "
        "(or GOOGLE_SERVICE_ACCOUNT_FILE) on the backend, and make sure the "
        "target Drive folder is shared with that service account's email."
    )


def get_service():
    """Lazily build (and cache) the Drive API client. Building it per-call
    would work fine too, but this avoids repeating the credential parsing
    on every single save."""
    global _service
    with _service_lock:
        if _service is None:
            creds = _get_credentials()
            _service = build("drive", "v3", credentials=creds, cacheDiscovery=False)
        return _service


def _find_folder(service, name: str, parent_id: str) -> str | None:
    safe_name = name.replace("'", "\\'")
    query = (
        f"mimeType = '{FOLDER_MIME}' and name = '{safe_name}' "
        f"and '{parent_id}' in parents and trashed = false"
    )
    resp = service.files().list(
        q=query, spaces="drive", fields="files(id, name)", pageSize=1,
    ).execute()
    files = resp.get("files", [])
    return files[0]["id"] if files else None


def _create_folder(service, name: str, parent_id: str) -> str:
    metadata = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}
    folder = service.files().create(body=metadata, fields="id").execute()
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
    folder_id = _find_folder(service, clean_name, ROOT_FOLDER_ID)
    if not folder_id:
        folder_id = _create_folder(service, clean_name, ROOT_FOLDER_ID)

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
    )
    response = None
    while response is None:
        _status, response = request.next_chunk()

    return {
        "file_id": response["id"],
        "name": response["name"],
        "web_view_link": response.get("webViewLink"),
        "folder_id": folder_id,
    }

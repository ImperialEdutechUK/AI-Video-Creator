"""
Google Drive integration for AI Video Creator.

Saves generated videos into Drive, organised as:

    <DRIVE_ROOT_FOLDER_ID>/
        Biology/
            Biology_Unit1_Chapter1.mp4
            Biology_Unit1_Chapter2.mp4
        Chemistry/
            Chemistry_Unit3_Chapter2.mp4

Required environment variables on Railway:

    GOOGLE_SERVICE_ACCOUNT_JSON   full contents of the service account .json
        (or)
    GOOGLE_SERVICE_ACCOUNT_B64    the same file, base64 encoded

    DRIVE_ROOT_FOLDER_ID          ID of a Drive folder you own, shared with
                                  the service account's client_email as Editor

Dependencies (add to requirements.txt):

    google-api-python-client
    google-auth
"""

import base64
import json
import os
import re
import threading

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/drive"]
FOLDER_MIME = "application/vnd.google-apps.folder"

_service = None
_service_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _load_credentials():
    """Build credentials from whichever env var is present."""
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")

    if not raw:
        encoded = os.environ.get("GOOGLE_SERVICE_ACCOUNT_B64")
        if encoded:
            raw = base64.b64decode(encoded).decode("utf-8")

    if not raw:
        raise RuntimeError(
            "No service account credentials found. Set either "
            "GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_SERVICE_ACCOUNT_B64 "
            "in your Railway variables."
        )

    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "Service account credentials are not valid JSON. If you pasted "
            "the file contents directly, try the base64 method instead."
        ) from exc

    return service_account.Credentials.from_service_account_info(
        info, scopes=SCOPES
    )


def get_service():
    """Return a cached Drive API client, building it on first use."""
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                # NOTE: the argument is cache_discovery (snake_case), and on
                # google-api-python-client 2.x it can be omitted entirely.
                _service = build(
                    "drive",
                    "v3",
                    credentials=_load_credentials(),
                )
    return _service


def get_root_folder_id():
    folder_id = os.environ.get("DRIVE_ROOT_FOLDER_ID")
    if not folder_id:
        raise RuntimeError(
            "DRIVE_ROOT_FOLDER_ID is not set. Create a folder in your own "
            "Drive, share it with the service account as Editor, and put its "
            "ID (the part of the URL after /folders/) in this variable."
        )
    return folder_id.strip()


# ---------------------------------------------------------------------------
# Naming helpers
# ---------------------------------------------------------------------------

def sanitize(name):
    """
    Make a string safe and consistent for use as a Drive folder/file name.

    Collapses whitespace and strips characters that cause trouble, so that
    'Biology ' and 'biology' don't end up as two separate course folders.
    """
    cleaned = re.sub(r'[\\/:*?"<>|]', "", str(name))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        raise ValueError("Name is empty after sanitising")
    return cleaned


def build_video_filename(course_name, unit_number, chapter_number, extension="mp4"):
    """Biology + 2 + 5  ->  'Biology_Unit2_Chapter5.mp4'"""
    course = sanitize(course_name).replace(" ", "_")
    return f"{course}_Unit{unit_number}_Chapter{chapter_number}.{extension}"


def _escape_for_query(value):
    """Escape a value for use inside a Drive API query string."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


# ---------------------------------------------------------------------------
# Folder / file operations
# ---------------------------------------------------------------------------

def find_folder(service, name, parent_id):
    """Return the ID of a folder with this name under parent_id, or None."""
    query = (
        f"name = '{_escape_for_query(name)}' "
        f"and mimeType = '{FOLDER_MIME}' "
        f"and '{_escape_for_query(parent_id)}' in parents "
        f"and trashed = false"
    )
    response = service.files().list(
        q=query,
        spaces="drive",
        fields="files(id, name)",
        pageSize=1,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()

    files = response.get("files", [])
    return files[0]["id"] if files else None


def find_or_create_folder(service, name, parent_id):
    """
    Return the ID of the folder called `name` inside `parent_id`,
    creating it only if it doesn't already exist.

    This is what makes a second upload for the same course land in the
    existing course folder instead of making a duplicate.
    """
    existing = find_folder(service, name, parent_id)
    if existing:
        return existing

    created = service.files().create(
        body={
            "name": name,
            "mimeType": FOLDER_MIME,
            "parents": [parent_id],
        },
        fields="id",
        supportsAllDrives=True,
    ).execute()
    return created["id"]


def find_file(service, name, parent_id):
    """Return the ID of a non-folder file with this name, or None."""
    query = (
        f"name = '{_escape_for_query(name)}' "
        f"and '{_escape_for_query(parent_id)}' in parents "
        f"and trashed = false"
    )
    response = service.files().list(
        q=query,
        spaces="drive",
        fields="files(id, name)",
        pageSize=1,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()

    files = response.get("files", [])
    return files[0]["id"] if files else None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def save_video_to_drive(
    local_path,
    course_name,
    unit_number,
    chapter_number,
    mime_type="video/mp4",
    overwrite=True,
):
    """
    Upload one video into <root>/<course name>/<course>_Unit<n>_Chapter<n>.mp4

    Args:
        local_path:     path to the finished video on disk
        course_name:    e.g. "Biology"
        unit_number:    e.g. 2
        chapter_number: e.g. 5
        overwrite:      if True, re-saving the same chapter replaces the
                        existing file rather than creating a duplicate

    Returns a dict: {file_id, file_name, folder_id, web_view_link, updated}
    """
    if not os.path.isfile(local_path):
        raise FileNotFoundError(f"No video found at {local_path}")

    service = get_service()
    root_id = get_root_folder_id()

    course_folder_name = sanitize(course_name)
    filename = build_video_filename(course_name, unit_number, chapter_number)

    try:
        course_folder_id = find_or_create_folder(
            service, course_folder_name, root_id
        )

        media = MediaFileUpload(
            local_path,
            mimetype=mime_type,
            resumable=True,
            chunksize=5 * 1024 * 1024,
        )

        existing_id = find_file(service, filename, course_folder_id) if overwrite else None

        if existing_id:
            result = service.files().update(
                fileId=existing_id,
                media_body=media,
                fields="id, name, webViewLink",
                supportsAllDrives=True,
            ).execute()
            updated = True
        else:
            result = service.files().create(
                body={"name": filename, "parents": [course_folder_id]},
                media_body=media,
                fields="id, name, webViewLink",
                supportsAllDrives=True,
            ).execute()
            updated = False

    except HttpError as exc:
        raise RuntimeError(_explain_http_error(exc)) from exc

    return {
        "file_id": result["id"],
        "file_name": result["name"],
        "folder_id": course_folder_id,
        "web_view_link": result.get("webViewLink"),
        "updated": updated,
    }


def _explain_http_error(exc):
    """Turn common Drive API errors into something actionable."""
    text = str(exc)

    if "storageQuotaExceeded" in text:
        return (
            "Drive rejected the upload: the service account has no storage of "
            "its own. Make sure DRIVE_ROOT_FOLDER_ID points at a folder in "
            "your personal Drive that is shared with the service account's "
            "client_email as an Editor."
        )
    if "notFound" in text or "File not found" in text:
        return (
            "Drive could not find the root folder. Check DRIVE_ROOT_FOLDER_ID "
            "is just the ID from the URL (no https://, no ?usp=sharing), and "
            "that the folder is shared with the service account."
        )
    if "insufficientFilePermissions" in text or "forbidden" in text.lower():
        return (
            "The service account does not have write access. Re-share the "
            "root folder with its client_email and set the role to Editor, "
            "not Viewer."
        )
    if "invalid_grant" in text:
        return (
            "Authentication failed. The private key in the service account "
            "JSON is probably malformed — try the base64 method for storing "
            "it in Railway."
        )
    return f"Google Drive error: {text}"

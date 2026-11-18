#!/usr/bin/env python3
"""
NotebookLM download task (ECS Fargate Spot, started by the API every 30 min
only when the sheet has rows waiting).

For each pending row (DynamoDB AUTO# item):
  claim -> download the video from NotebookLM -> upload to S3 uploads/auto/
  -> queue a normal video job (same shape the web app creates, plus auto=True)
  -> poke the API so a video worker starts.
Failures are recorded on the item as "failing"; the API's 1-minute
reconciler writes "NO" in the sheet. This task never touches the sheet.

The Google session (cookies) lives encrypted in S3 and is saved back after
every run, so Google's routine cookie refreshes are kept.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import socket
import sys
import tempfile
import threading
import time
import uuid
from decimal import Decimal
from pathlib import Path

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from playwright.sync_api import sync_playwright

import creator_runner
import extras_runner
import notebooklm
import rules
from notebooklm import NotebookError, SignInRequired

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("slc-automation")

REGION = os.environ.get("AWS_REGION", "eu-west-2")
TABLE_NAME = os.environ["JOBS_TABLE"]
BUCKET = os.environ["MEDIA_BUCKET"]
API_FUNCTION = os.environ.get("API_FUNCTION", "")
SESSION_KEY = os.environ.get("GOOGLE_SESSION_KEY", "automation/google-session.json")
DEBUG_PREFIX = "automation/debug/"
MAX_QUEUED_JOBS = int(os.environ.get("MAX_QUEUED_JOBS", "20"))
TEAM_RESERVE = int(os.environ.get("TEAM_QUEUE_RESERVE", "5"))         # queue slots always left for people
MAX_ACTIVE_AUTO_JOBS = int(os.environ.get("MAX_ACTIVE_AUTO_JOBS", "4"))
RUN_LIMIT_SECONDS = int(os.environ.get("RUN_LIMIT_SECONDS", "2400"))  # no new rows after 40 min
ROW_LIMIT_SECONDS = int(os.environ.get("ROW_LIMIT_SECONDS", "1200"))  # one row may take at most 20 min
OUTPUT_RETENTION_DAYS = int(os.environ.get("OUTPUT_RETENTION_DAYS", "3"))
AUTOMATION_SID = "automation-bot-session"
CREATOR_NOTEBOOK_URL = os.environ.get("CREATOR_NOTEBOOK_URL", "")   # set by the API when it starts the task
CREATOR_TIME_SECONDS = int(os.environ.get("CREATOR_TIME_SECONDS", "900"))   # no new videos started after this
CREATOR_GRACE_SECONDS = 600                                                  # ...and the step is stopped 10 min later
EXTRAS_TIME_SECONDS = int(os.environ.get("EXTRAS_TIME_SECONDS", "900"))      # podcasts / study guides step
OWNER = f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}"

ddb = boto3.resource("dynamodb", region_name=REGION)
table = ddb.Table(TABLE_NAME)
s3 = boto3.client("s3", region_name=REGION)
lambda_client = boto3.client("lambda", region_name=REGION)

_current: dict | None = None


def _now() -> Decimal:
    return Decimal(str(round(time.time(), 3)))


# ── DynamoDB ─────────────────────────────────────────────────────────────

def _query_active(value: str) -> list[dict]:
    items, kwargs = [], {}
    while True:
        resp = table.query(IndexName="active", KeyConditionExpression=Key("active").eq(value),
                           ScanIndexForward=True, **kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            return items
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def capacity_left() -> int:
    team_active = sum(1 for i in _query_active("1") if i.get("qstate") in ("queued", "processing", "reserved"))
    auto_busy = sum(1 for i in _query_active("A") if i.get("qstate") in ("processing", "saving", "verifying"))
    return max(0, min(MAX_QUEUED_JOBS - TEAM_RESERVE - team_active, MAX_ACTIVE_AUTO_JOBS - auto_busy))


def claim_next() -> dict | None:
    for i in _query_active("A"):
        if i.get("qstate") != "pending":
            continue
        try:
            resp = table.update_item(
                Key={"pk": i["pk"]},
                UpdateExpression=("SET qstate = :d, owner_id = :o, heartbeat = :now, updated_at = :now, "
                                  "attempts = if_not_exists(attempts, :zero) + :one REMOVE #e"),
                ConditionExpression="qstate = :p",
                ExpressionAttributeNames={"#e": "error"},
                ExpressionAttributeValues={":d": "downloading", ":p": "pending", ":o": OWNER,
                                           ":now": _now(), ":zero": 0, ":one": 1},
                ReturnValues="ALL_NEW",
            )
            return resp["Attributes"]
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
    return None



def with_updated_at(expr: str) -> str:
    """Add "updated_at = :now" to the SET clause of an update expression.
    (Appending it at the end breaks expressions that also REMOVE fields.)"""
    if not expr.startswith("SET "):
        raise ValueError(f"update expression must start with SET: {expr!r}")
    return "SET updated_at = :now, " + expr[4:]


def _update_item(pk: str, expr: str, values: dict, names: dict | None = None) -> bool:
    values = {**values, ":d": "downloading", ":o": OWNER, ":now": _now()}
    try:
        table.update_item(
            Key={"pk": pk}, UpdateExpression=with_updated_at(expr),
            ConditionExpression="qstate = :d AND owner_id = :o",
            ExpressionAttributeValues=values, **({"ExpressionAttributeNames": names} if names else {}),
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            log.warning("Item %s is no longer owned by this task", pk)
            return False
        raise


def heartbeat(pk: str) -> None:
    _update_item(pk, "SET heartbeat = :now", {})


def mark_failing(item: dict, message: str, final: bool = False) -> None:
    """final=True: retrying can't help (e.g. a person must choose between
    videos), so use up the remaining attempts. Clearing I in the sheet still
    restarts the row."""
    log.warning("Row %s (U%sC%s) failed: %s", item.get("row"), item.get("unit"), item.get("chapter"), message)
    expr = "SET qstate = :f, #e = :m"
    values = {":f": "failing", ":m": message[:400]}
    if final:
        expr += ", attempts = :max"
        values[":max"] = rules.MAX_ATTEMPTS
    _update_item(item["pk"], expr + " REMOVE owner_id", values, {"#e": "error"})


def release(item: dict, attempts_back: bool = True) -> None:
    """Put a claimed row back as pending without counting the attempt."""
    expr = "SET qstate = :pe REMOVE owner_id"
    vals = {":pe": "pending"}
    if attempts_back:
        expr = "SET qstate = :pe, attempts = attempts - :one REMOVE owner_id"
        vals[":one"] = 1
    _update_item(item["pk"], expr, vals)


def create_job(item: dict, upload_key: str, filename: str, size: int) -> str:
    """Queue a normal video job - identical to what POST /jobs creates."""
    job_id = uuid.uuid4().hex[:12]
    table.put_item(
        Item={
            "pk": f"JOB#{job_id}",
            "kind": "job",
            "job_id": job_id,
            "sid": AUTOMATION_SID,
            "status": "queued",
            "active": "1",
            "qstate": "queued",
            "progress": [],
            "course_name": item["course_name"],
            "unit_number": f"Unit {item['unit']}",
            "chapter_number": f"Chapter {item['chapter']}",
            "awarding_body": item.get("awarding_body", ""),
            "original_filename": filename[:255],
            "created_at": _now(),
            "upload_size_bytes": size,
            "upload_key": upload_key,
            "drive_status": "idle",
            "auto": True,
            "auto_pk": item["pk"],
        },
        ConditionExpression="attribute_not_exists(pk)",
    )
    return job_id


def poke_api() -> None:
    """Ask the API to start a video worker now instead of within a minute."""
    if not API_FUNCTION:
        return
    try:
        lambda_client.invoke(FunctionName=API_FUNCTION, InvocationType="Event",
                             Payload=json.dumps({"action": "reconcile"}).encode())
    except Exception:
        log.exception("Could not poke the API (the 1-minute reconciler will start the worker)")


# ── Google session ───────────────────────────────────────────────────────

def load_session() -> dict | None:
    try:
        obj = s3.get_object(Bucket=BUCKET, Key=SESSION_KEY)
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "404", "AccessDenied"):
            return None
        raise
    return json.loads(obj["Body"].read())


def save_session(state: dict) -> None:
    if not state.get("cookies"):
        return
    s3.put_object(Bucket=BUCKET, Key=SESSION_KEY, Body=json.dumps(state).encode(),
                  ContentType="application/json", ServerSideEncryption="AES256")


def send_alert(kind: str, subject: str, message: str) -> None:
    """Email the team via the API (it throttles repeats)."""
    if not API_FUNCTION:
        log.warning("ALERT %s: %s", subject, message)
        return
    try:
        lambda_client.invoke(FunctionName=API_FUNCTION, InvocationType="Event", Payload=json.dumps(
            {"action": "automation_alert", "kind": kind, "subject": subject, "message": message}).encode())
    except Exception:
        log.exception("Could not send alert %s", kind)


def docx_to_pdf(docx: bytes, name: str) -> bytes:
    """Word's PDF of a study guide, made by the API with the team's Microsoft
    sign-in (Word for the web renders it with the document's real fonts)."""
    if not API_FUNCTION:
        raise RuntimeError("API_FUNCTION is not set - can't convert the study guide to PDF.")
    stamp = f"{int(time.time() * 1000)}-{os.getpid()}"
    src, dst = f"automation/extras/tmp/{stamp}.docx", f"automation/extras/tmp/{stamp}.pdf"
    s3.put_object(Bucket=BUCKET, Key=src, Body=docx, ServerSideEncryption="AES256")
    try:
        resp = lambda_client.invoke(FunctionName=API_FUNCTION, InvocationType="RequestResponse", Payload=json.dumps(
            {"action": "extras_docx_to_pdf", "docx_key": src, "pdf_key": dst, "name": name}).encode())
        out = json.loads(resp["Payload"].read() or b"{}")
        if resp.get("FunctionError") or not out.get("ok"):
            raise RuntimeError(f"Word could not make the PDF: {str(out)[:300]}")
        return s3.get_object(Bucket=BUCKET, Key=dst)["Body"].read()
    finally:
        for key in (src, dst):
            try:
                s3.delete_object(Bucket=BUCKET, Key=key)
            except Exception:
                pass


def set_signin_flag(required: bool, reason: str = "") -> None:
    if required:
        table.put_item(Item={"pk": "AUTOCFG#google", "kind": "autocfg", "signin_required": True,
                             "reason": reason[:300], "updated_at": _now()})
        poke = {"action": "automation_alert", "kind": "google_signin",
                "subject": "SLC automation: please sign in to Google again",
                "message": (
                    "The video automation can't reach NotebookLM because the saved Google sign-in for the "
                    "content team account has stopped working.\n\n"
                    f"Reason: {reason}\n\n"
                    "Fix (about 2 minutes): on any Windows PC with the project, run\n"
                    "    py -3.12 aws\\scripts\\automation_google_signin.py\n"
                    "and sign in when Chrome opens. Waiting rows are picked up automatically afterwards; "
                    "nothing is lost."
                )}
        if API_FUNCTION:
            try:
                lambda_client.invoke(FunctionName=API_FUNCTION, InvocationType="Event",
                                     Payload=json.dumps(poke).encode())
            except Exception:
                log.exception("Could not send the sign-in alert")
    else:
        try:
            table.delete_item(Key={"pk": "AUTOCFG#google"})
        except ClientError:
            pass


# ── main ─────────────────────────────────────────────────────────────────

def _safe_name(name: str) -> str:
    stem = re.sub(r"[^\w.\-]+", "_", name).strip("._") or "video.mp4"
    return stem[:120]


def _debug_shot(context, item: dict) -> str:
    try:
        pages = [p for p in context.pages if not p.is_closed()]
        if not pages:
            return ""
        key = f"{DEBUG_PREFIX}{item['pk'].split('#')[-1]}-{int(time.time())}.png"
        s3.put_object(Bucket=BUCKET, Key=key, Body=pages[-1].screenshot(full_page=True),
                      ContentType="image/png", ServerSideEncryption="AES256")
        return key
    except Exception:
        return ""


class RowWatchdog(threading.Thread):
    """Reports "still working" every minute while a row is downloading, and
    ends the task if one row takes longer than ROW_LIMIT_SECONDS - so a frozen
    browser can never hold a row (or keep billing) for long. The row is marked
    failing first; the API writes NO and retries it on a later run."""

    def __init__(self, item: dict):
        super().__init__(daemon=True)
        self.item = item
        self.started = time.time()
        self.stop_event = threading.Event()

    def run(self):
        while not self.stop_event.wait(60):
            if time.time() - self.started > ROW_LIMIT_SECONDS:
                log.error("Row %s took longer than %ss - stopping the task", self.item.get("row"), ROW_LIMIT_SECONDS)
                try:
                    mark_failing(self.item, f"The NotebookLM step took longer than {ROW_LIMIT_SECONDS // 60} "
                                            "minutes and was stopped.")
                finally:
                    os._exit(1)
            try:
                heartbeat(self.item["pk"])
            except Exception:
                log.exception("Heartbeat failed")

    def stop(self):
        self.stop_event.set()


def handle(context, item: dict) -> None:
    pk = item["pk"]
    work = Path(tempfile.mkdtemp(prefix="nlm-"))
    watchdog = RowWatchdog(item)
    watchdog.start()
    try:
        res = notebooklm.download_video(context, item["url"], item["unit"], item["chapter"], work,
                                        course=item.get("course_name", ""),
                                        awarding_body=item.get("awarding_body", ""))
        heartbeat(pk)
        size = res.path.stat().st_size
        filename = _safe_name(f"U{item['unit']}C{item['chapter']}{res.path.suffix or '.mp4'}")
        upload_key = f"uploads/auto/{pk.split('#')[-1]}/{uuid.uuid4().hex[:8]}-{filename}"
        s3.upload_file(str(res.path), BUCKET, upload_key, ExtraArgs={"ServerSideEncryption": "AES256"})
        job_id = create_job(item, upload_key, filename, size)
        ok = _update_item(pk, "SET qstate = :pr, job_id = :j, video_title = :t, notebook_id = :n REMOVE owner_id",
                          {":pr": "processing", ":j": job_id, ":t": res.title, ":n": res.notebook_id})
        if not ok:
            log.warning("Lost ownership of %s after queueing job %s", pk, job_id)
        log.info("Row %s: downloaded %s (%.1f MB) -> job %s", item.get("row"), res.title[:40], size / 1e6, job_id)
        poke_api()
    except SignInRequired:
        release(item)
        raise
    except NotebookError as e:
        shot = _debug_shot(context, item)
        mark_failing(item, str(e) + (f" [screenshot: {shot}]" if shot else ""), final=not e.retry)
    except Exception as e:
        log.exception("Unexpected error on row %s", item.get("row"))
        shot = _debug_shot(context, item)
        mark_failing(item, f"Unexpected error: {e.__class__.__name__}: {str(e)[:200]}"
                     + (f" [screenshot: {shot}]" if shot else ""))
    finally:
        watchdog.stop()
        shutil.rmtree(work, ignore_errors=True)


def _on_sigterm(signum, frame):
    log.warning("SIGTERM (Spot interruption or stop)")
    if _current:
        try:
            release(_current)
        except Exception:
            log.exception("Could not release %s", _current.get("pk"))
    sys.exit(0)


def main() -> int:
    global _current
    signal.signal(signal.SIGTERM, _on_sigterm)
    started = time.time()
    state = load_session()
    if not state:
        set_signin_flag(True, "No saved Google session yet.")
        log.error("No Google session in S3 - run automation_google_signin.py")
        return 0

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True, channel="chromium",
            args=["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage"],
        )
        major = browser.version.split(".")[0]
        # Same browser family as the PC the account signed in on, so Google
        # sees a consistent device (no "HeadlessChrome" user agent).
        ua = state.get("user_agent") or (
            f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36")
        context = browser.new_context(
            storage_state={"cookies": state.get("cookies", []), "origins": state.get("origins", [])},
            accept_downloads=True, user_agent=ua, locale="en-GB", timezone_id="Europe/London",
            viewport={"width": 1600, "height": 1000},
        )
        context.set_default_timeout(30_000)
        # The chapter-video creator copies share links via the clipboard.
        for origin in ("https://notebook.google.com", "https://notebooklm.google.com"):
            context.grant_permissions(["clipboard-read", "clipboard-write"], origin=origin)
        page = context.new_page()
        if not notebooklm.check_signed_in(page):
            set_signin_flag(True, "Google sent the saved session to the sign-in page.")
            log.error("Google session is no longer signed in")
            browser.close()
            return 0
        page.close()
        set_signin_flag(False)

        done, signed_out = 0, False
        try:
            if CREATOR_NOTEBOOK_URL:
                # Chapter-video creator first: starting a video takes ~1 minute and
                # NotebookLM then needs ~30 minutes, so start them as early as possible.
                # Watchdog: a frozen browser must not hold the task. A half-started
                # video is noticed and redone by the next run.
                def _creator_frozen():
                    log.error("Chapter-video creator step ran over %ss - stopping the task",
                              CREATOR_TIME_SECONDS + CREATOR_GRACE_SECONDS)
                    os._exit(1)
                timer = threading.Timer(CREATOR_TIME_SECONDS + CREATOR_GRACE_SECONDS, _creator_frozen)
                timer.daemon = True
                timer.start()
                try:
                    rep = creator_runner.run(context, table, CREATOR_NOTEBOOK_URL,
                                             deadline=time.time() + CREATOR_TIME_SECONDS, alert=send_alert)
                    log.info("Creator: %s", rep)
                except SignInRequired:
                    raise
                except Exception:
                    log.exception("Chapter-video creator pass failed")
                finally:
                    timer.cancel()
            if CREATOR_NOTEBOOK_URL and extras_runner.has_work(table):
                # Unit podcasts and study guides (same watchdog idea).
                def _extras_frozen():
                    log.error("Podcast / study guide step ran over %ss - stopping the task",
                              EXTRAS_TIME_SECONDS + CREATOR_GRACE_SECONDS)
                    os._exit(1)
                timer = threading.Timer(EXTRAS_TIME_SECONDS + CREATOR_GRACE_SECONDS, _extras_frozen)
                timer.daemon = True
                timer.start()
                try:
                    rep = extras_runner.run(context, table, CREATOR_NOTEBOOK_URL,
                                            deadline=time.time() + EXTRAS_TIME_SECONDS, s3=s3, bucket=BUCKET,
                                            to_pdf=docx_to_pdf, alert=send_alert)
                    log.info("Podcasts / study guides: %s", rep)
                except SignInRequired:
                    raise
                except Exception:
                    log.exception("Podcast / study guide pass failed")
                finally:
                    timer.cancel()
            while time.time() - started < RUN_LIMIT_SECONDS:
                if capacity_left() <= 0:
                    log.info("Video queue is busy - leaving remaining rows for the next run")
                    break
                item = claim_next()
                if not item:
                    break
                _current = item
                handle(context, item)
                _current = None
                done += 1
        except SignInRequired as e:
            log.error("Google sign-in required: %s", e)
            signed_out = True
            set_signin_flag(True, str(e))
        finally:
            try:
                if not signed_out:  # never overwrite the stored session with a signed-out one
                    fresh = context.storage_state()
                    fresh["user_agent"] = ua
                    save_session(fresh)
            except Exception:
                log.exception("Could not save the refreshed Google session")
            browser.close()
        log.info("Run finished: %d row(s) handled", done)
    return 0


if __name__ == "__main__":
    sys.exit(main())

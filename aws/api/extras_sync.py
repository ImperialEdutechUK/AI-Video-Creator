"""
Unit podcasts and study guides, API side (Lambda): the sheet and Google Drive.

scan()      every 10 min: keep the bot's marks right in columns E/F (podcasts) and
            G/H (study guides) of "Doc Formatting Completed Units"; take the next
            unit whose column is empty (newest first): WordPress -> its chapter
            PDFs and "Unit N - <name>"; nothing for it in Drive yet -> claim
            (DynamoDB first, then "auto" in E / G).
follow_up() every minute: a unit whose file the task has made ("upload") goes into
            Drive (podcasts: WebTool_Generated_Podcasts/<course>/Unit_N_-_<name>.mp4,
            study guides: PSC Project - Study Guides/<course>/unit-NN.pdf, the
            course folder made once and reused), then "Done" in F / H. A unit
            that can't be made is reported by email and its "auto" removed.
docx_to_pdf the task's study guide DOCX -> PDF made by Word (Microsoft sign-in).
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from decimal import Decimal
from pathlib import Path

from botocore.exceptions import ClientError

import automation as A
import creator_rules as cr
import creator_sync as C
import extras_rules as er
import rules
from excel import GraphError, LayoutError, Workbook
from wordpress import WordPressError, WordPressSignInRequired

log = logging.getLogger("extras")

ENABLED = os.environ.get("EXTRAS_ENABLED", "false").lower() == "true"
FOLDERS = {
    "podcast": os.environ.get("PODCAST_FOLDER_ID", "1M53kFacEkHVE8UTU3j4jqOFLJxWVD7Cp"),       # WebTool_Generated_Podcasts
    "studyguide": os.environ.get("STUDYGUIDE_FOLDER_ID", "1E0sHc1ltbIbtNwq2AMI5Bi6UwTdY7bwb"),  # PSC Project - Study Guides
}
MAX_ACTIVE = {"podcast": int(os.environ.get("EXTRAS_MAX_PODCASTS", "2")),
              "studyguide": int(os.environ.get("EXTRAS_MAX_STUDY_GUIDES", "2"))}
PER_RUN = 1
SKIP_RECHECK_SECONDS = 24 * 3600
STALE_SECONDS = 3 * 3600          # no progress this long -> email
MAX_SECONDS = 24 * 3600           # ... and this long -> given up
MAX_MARK_FIXES_PER_RUN = 40
MIME = {"podcast": "video/mp4", "studyguide": "application/pdf"}


def _now() -> Decimal:
    return Decimal(str(round(time.time(), 3)))


def items(kind: er.Kind, active_only: bool = True) -> list[dict]:
    if active_only:
        out, kwargs = [], {}
        while True:
            resp = A.table.query(IndexName="active", KeyConditionExpression=C.Key("active").eq(kind.active), **kwargs)
            out += [i["pk"] for i in resp.get("Items", [])]
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        full = A._batch_get(out)
        return [full[pk] for pk in out if pk in full]
    found, kwargs = [], {}
    while True:
        resp = A.table.scan(FilterExpression=C.Attr("kind").eq(kind.item_kind), **kwargs)
        found += resp.get("Items", [])
        if "LastEvaluatedKey" not in resp:
            return found
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def needs_task() -> bool:
    return ENABLED and any(i.get("state") in er.RUNNER_STATES for k in er.KINDS for i in items(k))


# ── Drive ────────────────────────────────────────────────────────────────

def _drive():
    from handler import _drive_module
    return _drive_module()


def course_folder(drive, kind: er.Kind, course_raw: str, create: bool) -> str | None:
    """The course's folder in this kind's Drive root (named exactly like the sheet's
    course name). One folder per course: an existing one is always reused, a new
    one is created once (DynamoDB lock, like the chapter videos' folders)."""
    root = FOLDERS[kind.name]
    name = rules.norm_space(course_raw)
    pk = f"EXFOLDER#{root}#{name.lower()}"
    svc = drive.get_service()
    deadline = time.time() + 60
    while True:
        rec = A.table.get_item(Key={"pk": pk}, ConsistentRead=True).get("Item")
        if rec and rec.get("folder_id"):
            if A._folder_usable(svc, rec["folder_id"]):
                return rec["folder_id"]
            A.table.delete_item(Key={"pk": pk})
            continue
        found = A._oldest_folder(drive, svc, name, root)
        if found or not create:
            if found:
                A.table.put_item(Item={"pk": pk, "kind": "exfolder", "folder_id": found, "name": name})
            return found
        try:
            A.table.put_item(Item={"pk": pk, "kind": "exfolder", "name": name,
                                   "lock_until": Decimal(str(time.time() + 120))},
                             ConditionExpression="attribute_not_exists(pk) OR "
                                                 "(attribute_not_exists(folder_id) AND lock_until < :now)",
                             ExpressionAttributeValues={":now": _now()})
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            if time.time() > deadline:
                raise RuntimeError("Timed out waiting for another save to create the course folder.")
            time.sleep(2)
            continue
        folder = A._oldest_folder(drive, svc, name, root) or drive._create_folder(svc, name, root)
        A.table.put_item(Item={"pk": pk, "kind": "exfolder", "folder_id": folder, "name": name})
        return folder


def existing_file(drive, folder_id: str, names: list[str]) -> dict | None:
    svc = drive.get_service()
    for name in names:
        safe = name.replace("\\", "\\\\").replace("'", "\\'")
        resp = svc.files().list(q=f"name = '{safe}' and '{folder_id}' in parents and trashed = false",
                                fields="files(id, name, size, webViewLink)", pageSize=5,
                                supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
        files = [f for f in resp.get("files", []) if int(f.get("size") or 0) > 0]
        if files:
            return files[0]
    return None


def target_names(kind: er.Kind, unit: int, unit_title: str) -> list[str]:
    """The file the bot makes, plus the names people used for the same thing."""
    if kind is er.PODCAST:
        return [er.podcast_file_name(unit_title)]
    return [er.studyguide_file_name(unit), f"unit {unit}.docx", f"unit-{unit}.pdf", f"unit {unit}.pdf",
            f"unit-{er.two_digits(unit)}.docx"]


# ── scan: marks + claims ─────────────────────────────────────────────────

def scan(client, log_wb: Workbook, dry_run: bool) -> dict:
    if not ENABLED:
        return {"enabled": False}
    report: dict = {}
    doc_wb = client.open_tab(log_wb, C.DOC_TAB)
    first, grid = client.read_grid(doc_wb)
    if first != 1 or not grid:
        raise LayoutError(f"{C.DOC_TAB!r} does not start at row 1.")
    problem = er.check_headers(grid[0])
    if problem:
        raise LayoutError(f"{C.DOC_TAB!r} header changed: {problem}")
    rows = cr.doc_rows(1, grid)[1:]
    for kind in er.KINDS:
        report[kind.name] = _scan_kind(client, doc_wb, kind, rows, grid, dry_run)
    return report


def _scan_kind(client, doc_wb: Workbook, kind: er.Kind, rows: list, grid: list, dry_run: bool) -> dict:
    report = {"claimed": [], "skipped": {}, "problems": []}
    every = items(kind, active_only=False)
    fixed = _repair_marks(client, doc_wb, kind, rows, grid, every, dry_run)
    if fixed:
        report["marks_repaired"] = fixed
    working = [i for i in every if i.get("active") == kind.active]
    report["in_progress"] = len(working)
    if C.paused():
        report["paused"] = C.paused()
        return report
    if C.wp_signout():
        report["paused"] = "WordPress sign-in required"
        return report
    budget = min(PER_RUN, MAX_ACTIVE[kind.name] - len(working))
    by_pk = {i["pk"]: i for i in every}
    wp = drive = None
    for row in cr.order_units(rows):
        pk = er.unit_key(kind, row.link, row.unit) if (row.unit and row.link) else ""
        d = er.decide(kind, row, grid[row.row - 1], by_pk.get(pk), time.time())
        if d.action == "skip":
            report["skipped"][d.reason] = report["skipped"].get(d.reason, 0) + 1
            continue
        if budget <= 0:
            report["skipped"]["waiting for a later run"] = report["skipped"].get("waiting for a later run", 0) + 1
            continue
        budget -= 1
        try:
            wp = wp or C.wordpress()
            unit = wp.unit(row.link, row.unit)
            bad = [c.number for c in unit.chapters if len(c.pdf_urls) != 1]
            if bad:
                raise WordPressError(f"Chapter(s) {bad} of Unit {row.unit} don't have exactly one PDF in WordPress.")
        except WordPressSignInRequired as e:
            C._set_wp_signout(str(e))
            report["paused"] = "WordPress sign-in required"
            break
        except WordPressError as e:
            report["problems"].append({"row": row.row, "unit": row.unit, "course": row.course_raw, "problem": str(e)})
            if not dry_run:
                _record_skip(kind, pk, row, str(e))
            budget += 1
            continue
        # Never make one twice: already in Drive (made by a person or earlier) -> leave it.
        drive = drive or _drive()
        folder = course_folder(drive, kind, row.course_raw, create=False)
        there = existing_file(drive, folder, target_names(kind, row.unit, unit.unit_title)) if folder else None
        if there:
            msg = f"already in Drive: {there['name']}"
            report["skipped"][f"{kind.label} already in Drive"] = report["skipped"].get(f"{kind.label} already in Drive", 0) + 1
            if not dry_run:
                _record_skip(kind, pk, row, msg)
            budget += 1
            continue
        summary = {"row": row.row, "unit": row.unit, "course": row.course_raw, "unit_title": unit.unit_title,
                   "chapters": [c.number for c in unit.chapters]}
        if not dry_run and not _claim(client, doc_wb, kind, row, pk, unit):
            continue
        report["claimed"].append(summary)
    return report


def _record_skip(kind: er.Kind, pk: str, row, message: str) -> None:
    A.table.put_item(Item={
        "pk": pk, "kind": kind.item_kind, "state": "skipped", "error": message[:400], "doc_row": row.row,
        "course_raw": row.course_raw, "course_link": row.link, "unit": row.unit,
        "retry_at": Decimal(str(time.time() + SKIP_RECHECK_SECONDS)), "updated_at": _now(),
        "ttl": int(time.time()) + 60 * 86400,
    })


def _claim(client, doc_wb: Workbook, kind: er.Kind, row, pk: str, unit) -> bool:
    # Re-read the row right before writing: a person may have just taken it.
    cur = (client.read_range(doc_wb, f"A{row.row}:J{row.row}") or [[]])[0] + [""] * 10
    if (rules.norm_space(cur[0]) != row.course_raw or rules.norm_space(cur[1]) != row.link
            or rules.norm_space(cur[2]) != row.unit_raw or rules.norm_space(cur[kind.who_idx])):
        log.info("Doc row %s changed before claiming the %s - skipped", row.row, kind.label)
        return False
    now = _now()
    try:
        A.table.put_item(
            Item={
                "pk": pk, "kind": kind.item_kind, "active": kind.active, "state": "queued", "attempts": 0,
                "created_at": now, "updated_at": now, "doc_row": row.row,
                "doc_wb_drive": doc_wb.drive_id, "doc_wb_item": doc_wb.item_id, "doc_ws_id": doc_wb.worksheet_id,
                "course_raw": row.course_raw, "course_link": row.link, "unit": row.unit,
                "unit_title": unit.unit_title[:200],
                "chapters": [{"n": c.number, "title": c.title[:200], "pdf_url": c.pdf_urls[0]}
                             for c in sorted(unit.chapters, key=lambda c: c.number)],
            },
            ConditionExpression="attribute_not_exists(pk) OR #s = :sk",
            ExpressionAttributeNames={"#s": "state"},
            ExpressionAttributeValues={":sk": "skipped"},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise
    client.write_cell(doc_wb, kind.who_col, row.row, er.BOT_NAME)
    return True


def _repair_marks(client, doc_wb: Workbook, kind: er.Kind, rows, grid, every, dry_run: bool) -> list[dict]:
    fixes = er.plan_mark_fixes(kind, rows, grid, every)
    if len(fixes) > MAX_MARK_FIXES_PER_RUN:
        A.alert(f"extras_marks_{kind.name}", "SLC automation: 'Doc Formatting Completed Units' needs a look",
                f"{len(fixes)} {kind.label} marks look out of place in column {kind.who_col}/{kind.status_col} - "
                f"more than the {MAX_MARK_FIXES_PER_RUN} the bot fixes by itself. Was a column moved or the tab "
                "partly sorted? Nothing was changed.")
        return [{"skipped": f"{len(fixes)} fixes needed - too many, alert sent"}]
    done, by_row = [], {}
    rows_by = {r.row: r for r in rows}
    for f in fixes:
        by_row.setdefault(f.row, []).append(f)
    for rnum, row_fixes in by_row.items():
        before = rows_by.get(rnum)
        if dry_run or before is None:
            done += [{"row": f.row, "col": f.col, "value": f.value, "dry_run": dry_run} for f in row_fixes]
            continue
        cur = (client.read_range(doc_wb, f"A{rnum}:J{rnum}") or [[]])[0] + [""] * 10
        g = grid[rnum - 1] if rnum - 1 < len(grid) else []
        if (rules.norm_space(cur[0]) != before.course_raw or rules.norm_space(cur[1]) != before.link
                or rules.norm_space(cur[2]) != before.unit_raw
                or rules.norm_space(cur[kind.who_idx]) != er.cell(g, kind.who_idx)):
            continue
        for f in row_fixes:
            client.write_cell(doc_wb, f.col, f.row, f.value)
            done.append({"row": f.row, "col": f.col, "value": f.value or "(cleared)", "reason": f.reason})
    if done and not dry_run:
        log.warning("Repaired %d %s mark(s): %s", len(done), kind.label, json.dumps(done)[:1500])
    return done


# ── follow-up: Drive + "Done" ────────────────────────────────────────────

def follow_up() -> dict:
    if not ENABLED:
        return {}
    report = {"uploaded": [], "failed": []}
    for kind in er.KINDS:
        for it in items(kind):
            state = it.get("state")
            try:
                if state == er.UPLOAD_STATE:
                    report["uploaded"].append(_upload(kind, it))
                elif state == "failed":
                    _close_failed(kind, it)
                    report["failed"].append({"unit": it.get("unit"), "course": it.get("course_raw"),
                                             "error": it.get("error")})
                elif state in er.RUNNER_STATES:
                    _check_stale(kind, it)
            except Exception:
                log.exception("%s follow-up failed for %s", kind.label, it.get("pk"))
    return report


def _upload(kind: er.Kind, it: dict) -> dict:
    drive = _drive()
    folder = course_folder(drive, kind, it["course_raw"], create=True)
    name = it["file_name"]
    there = existing_file(drive, folder, [name])
    if not there:
        local = Path(tempfile.mkdtemp()) / name
        A.s3.download_file(A.BUCKET, it["s3_key"], str(local))
        media = drive.MediaFileUpload(str(local), mimetype=MIME[kind.name], resumable=True,
                                      chunksize=8 * 1024 * 1024)
        request = drive.get_service().files().create(body={"name": name, "parents": [folder]}, media_body=media,
                                                      fields="id, name, size, webViewLink", supportsAllDrives=True)
        response = None
        while response is None:
            _status, response = request.next_chunk()
        size_ok = int(response.get("size") or 0) == local.stat().st_size
        local.unlink(missing_ok=True)
        if not size_ok:
            raise RuntimeError(f"Drive has a different size for {name} than the file uploaded.")
        there = response
    link = there.get("webViewLink") or f"https://drive.google.com/file/d/{there['id']}/view"
    A.table.update_item(
        Key={"pk": it["pk"]},
        UpdateExpression="SET #s = :d, drive_file_id = :f, drive_link = :l, finished_at = :now, updated_at = :now, "
                         "#ttl = :ttl REMOVE active",
        ConditionExpression="#s = :u",
        ExpressionAttributeNames={"#s": "state", "#ttl": "ttl"},
        ExpressionAttributeValues={":d": "done", ":f": there["id"], ":l": link, ":now": _now(),
                                   ":ttl": int(time.time()) + 180 * 86400, ":u": er.UPLOAD_STATE})
    try:
        A.s3.delete_object(Bucket=A.BUCKET, Key=it["s3_key"])
    except Exception:
        pass
    _mark_done(kind, it)
    return {"unit": it.get("unit"), "course": it.get("course_raw"), "file": name, "link": link}


def _doc_row(client, kind: er.Kind, it: dict):
    """The unit's row, found by its content (people move rows), while it still says 'auto'."""
    wb = Workbook(it["doc_wb_drive"], it["doc_wb_item"], "", it["doc_ws_id"], C.DOC_TAB)
    first, grid = client.read_grid(wb)
    for i, r in enumerate(cr.doc_rows(first, grid)):
        if (r.course_raw == it["course_raw"] and r.link == it["course_link"] and r.unit == int(it["unit"])
                and er.cell(grid[i], kind.who_idx).lower() == er.BOT_NAME):
            return wb, r.row, grid[i]
    return None


def _mark_done(kind: er.Kind, it: dict) -> None:
    try:
        client, _ = A.open_sheet()
        hit = _doc_row(client, kind, it)
        if hit and not er.cell(hit[2], kind.status_idx):
            client.write_cell(hit[0], kind.status_col, hit[1], er.STATUS_DONE)
    except (GraphError, LayoutError):
        log.exception("Could not write Done for %s row %s", kind.label, it.get("doc_row"))


def _close_failed(kind: er.Kind, it: dict) -> None:
    """Email once, and take 'auto' off the row so a person sees it isn't done."""
    try:
        A.table.update_item(Key={"pk": it["pk"]}, UpdateExpression="SET #ttl = :ttl, updated_at = :now REMOVE active",
                            ConditionExpression="#s = :f", ExpressionAttributeNames={"#s": "state", "#ttl": "ttl"},
                            ExpressionAttributeValues={":f": "failed", ":now": _now(),
                                                       ":ttl": int(time.time()) + 60 * 86400})
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return
        raise
    A.alert(f"extras_failed_{it['pk'][-8:]}", f"SLC automation: a unit {kind.label} could not be made",
            f"{kind.label.capitalize()} for Unit {it.get('unit')} of {it.get('course_raw')} "
            f"(Doc Formatting row {it.get('doc_row')}) could not be made:\n\n{it.get('error', '')}\n\n"
            f"The bot took 'auto' off column {kind.who_col} - please make it by hand.")
    try:
        client, _ = A.open_sheet()
        hit = _doc_row(client, kind, it)
        if hit:
            client.write_cell(hit[0], kind.who_col, hit[1], "")
    except (GraphError, LayoutError):
        log.exception("Could not clear 'auto' for %s row %s", kind.label, it.get("doc_row"))


def _check_stale(kind: er.Kind, it: dict) -> None:
    now = time.time()
    name = f"{kind.label.capitalize()} for Unit {it.get('unit')} of {it.get('course_raw')} (row {it.get('doc_row')})"
    waiting = C.paused() or (A.google_signout() is not None) or (C.wp_signout() is not None)
    if A._n(it.get("created_at"), now) < now - MAX_SECONDS and not waiting:
        try:
            A.table.update_item(Key={"pk": it["pk"]}, UpdateExpression="SET #s = :f, #e = :m, updated_at = :now",
                                ConditionExpression="#s IN (:a, :b, :c)",
                                ExpressionAttributeNames={"#s": "state", "#e": "error"},
                                ExpressionAttributeValues={":f": "failed", ":now": _now(),
                                                           ":m": f"Not finished within {MAX_SECONDS // 3600} hours "
                                                                 f"(last step: {it.get('state')}). {it.get('error', '')}"[:400],
                                                           ":a": "queued", ":b": "audio", ":c": "making"})
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
        return
    if A._n(it.get("updated_at"), now) < now - STALE_SECONDS:
        A.alert(f"extras_stuck_{it['pk'][-8:]}", f"SLC automation: a unit {kind.label} is not progressing",
                f"{name} has had no progress for {STALE_SECONDS // 3600} hours"
                + (f" (the bot is waiting: {waiting})" if waiting else "") + ".")


# ── DOCX -> PDF for the task ─────────────────────────────────────────────

def docx_to_pdf(event: dict) -> dict:
    key_in, key_out = event["docx_key"], event["pdf_key"]
    if not (key_in.startswith("automation/extras/tmp/") and key_out.startswith("automation/extras/tmp/")):
        return {"ok": False, "error": "unexpected S3 key"}
    docx = A.s3.get_object(Bucket=A.BUCKET, Key=key_in)["Body"].read()
    client, _ = A.open_sheet()
    pdf = client.docx_to_pdf(docx, event.get("name") or "study-guide")
    A.s3.put_object(Bucket=A.BUCKET, Key=key_out, Body=pdf, ServerSideEncryption="AES256",
                    ContentType="application/pdf")
    return {"ok": True, "bytes": len(pdf)}


def status() -> dict:
    out = {"enabled": ENABLED}
    for kind in er.KINDS:
        out[kind.name] = [{"row": i.get("doc_row"), "unit": i.get("unit"), "course": i.get("course_raw"),
                           "state": i.get("state"), "error": i.get("error", "")[:120]} for i in items(kind)]
    return out

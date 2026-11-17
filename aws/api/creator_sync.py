"""
Chapter-video creator, API (Lambda) side.

Every run (with the download automation's 10-minute tick):
  - pick the next unit from "Doc Formatting Completed Units" (newest first,
    empty "Chapter-wise Videos", not already in the video log),
  - read its chapters' PDF links from WordPress,
  - write "auto" in "Chapter-wise Videos" and record the unit (CUNIT# item).
The NotebookLM task then makes one video per chapter. When every chapter of a
unit is finished, the 1-minute follow-up appends one row per chapter to the
"Chapter-wise AI video log" (A date, B awarding body, C course, D unit,
E chapter, F "auto", G link) - H and I stay empty so the download bot takes it
through rebranding to Drive like every other row.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import time
from decimal import Decimal
from zoneinfo import ZoneInfo

from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

import automation as A
import creator_rules as cr
import rules
from excel import GraphError, LayoutError, Workbook
from wordpress import WordPress, WordPressError, WordPressSignInRequired

log = logging.getLogger("creator")

ENABLED = os.environ.get("CREATOR_ENABLED", "false").lower() == "true"
DOC_TAB = os.environ.get("CREATOR_DOC_TAB", "Doc Formatting Completed Units")
MAX_ACTIVE_UNITS = int(os.environ.get("CREATOR_MAX_ACTIVE_UNITS", "3"))
UNITS_PER_RUN = int(os.environ.get("CREATOR_UNITS_PER_RUN", "1"))
WP_SESSION_KEY = "automation/wordpress-session.json"
SKIP_RECHECK_SECONDS = 24 * 3600
LOG_LOCK_SECONDS = 300
UNIT_STALE_SECONDS = 2 * 3600           # a unit with no progress for this long -> email
UNIT_MAX_SECONDS = 24 * 3600            # ... and after this long it is closed (ready chapters logged)
ACTIVE = "C"                            # 'active' GSI value for creator units
TZ = ZoneInfo("Europe/London")


def _now() -> Decimal:
    return A._now()


def notebook_url() -> str:
    try:
        resp = A.ssm.get_parameters(Names=[f"{A.SSM_PREFIX}/automation/creator/notebook_url"])
    except ClientError:
        return ""
    params = resp.get("Parameters", [])
    return params[0]["Value"].strip() if params else ""


def wordpress() -> WordPress:
    try:
        state = json.loads(A.s3.get_object(Bucket=A.BUCKET, Key=WP_SESSION_KEY)["Body"].read())
    except ClientError:
        raise WordPressSignInRequired("No saved WordPress sign-in yet.") from None
    return WordPress(state.get("cookies", []), state.get("user_agent", ""))


def wp_signout() -> dict | None:
    return A.table.get_item(Key={"pk": "AUTOCFG#wordpress"}).get("Item")


def _set_wp_signout(reason: str) -> None:
    A.table.put_item(Item={"pk": "AUTOCFG#wordpress", "kind": "autocfg", "signin_required": True,
                           "reason": reason[:300], "updated_at": _now()})
    A.alert("wordpress_signin", "SLC automation: please sign in to WordPress again",
            "The chapter-video creator can't read the course pages because the saved WordPress sign-in "
            f"stopped working.\n\nReason: {reason}\n\nFix (about 1 minute): on the PC with the project run\n"
            "    py -3.12 aws\\scripts\\automation_wordpress_signin.py\n"
            "and sign in (tick 'Remember Me'). Nothing is lost; new units wait until then.")


def paused() -> str:
    """Why new videos can't be started right now ('' = not paused)."""
    p = A.table.get_item(Key={"pk": "AUTOCFG#creator_pause"}).get("Item")
    if p and A._n(p.get("until")) > time.time():
        return str(p.get("reason", "paused"))
    return ""


def active_units() -> list[dict]:
    items, kwargs = [], {}
    while True:
        resp = A.table.query(IndexName="active", KeyConditionExpression=Key("active").eq(ACTIVE), **kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    # the GSI only projects a few fields: read the full items
    full = A._batch_get([i["pk"] for i in items])
    return [full[i["pk"]] for i in items if i["pk"] in full]


def needs_task(units: list[dict] | None = None) -> bool:
    """True when the NotebookLM task has creator work (start or check videos)."""
    for u in units if units is not None else active_units():
        if u.get("cstate") == "chapters" and any(
                c.get("state") in ("queued", "starting", "generating") for c in u.get("chapters", [])):
            return True
    return False


# ── claiming units ───────────────────────────────────────────────────────

def scan(client, log_wb: Workbook, log_rows: list, dry_run: bool) -> dict:
    report = {"claimed": [], "skipped": {}, "problems": []}
    if not ENABLED:
        return {"enabled": False}
    if not notebook_url():
        return {"error": f"No PCS notebook set ({A.SSM_PREFIX}/automation/creator/notebook_url)."}
    units = active_units()
    working = [u for u in units if u.get("cstate") in ("chapters", "logging")]
    report["units_in_progress"] = len(working)

    doc_wb = client.open_tab(log_wb, DOC_TAB)
    first, grid = client.read_grid(doc_wb)
    if first != 1 or not grid:
        raise LayoutError(f"{DOC_TAB!r} does not start at row 1.")
    problem = cr.check_doc_header(grid[0])
    if problem:
        raise LayoutError(f"{DOC_TAB!r} header changed: {problem}")
    all_rows = cr.doc_rows(1, grid)[1:]
    # Every run: put the bot's marks back on the right rows (people move rows).
    fixed = repair_marks(client, doc_wb, all_rows, grid, dry_run)
    if fixed:
        report["marks_repaired"] = fixed

    why_paused = paused()
    if why_paused:
        report["paused"] = why_paused
        return report
    if wp_signout():
        report["paused"] = "WordPress sign-in required"
        return report
    budget = min(UNITS_PER_RUN, MAX_ACTIVE_UNITS - len(working))
    if budget <= 0:
        report["skipped"]["waiting: enough units in progress"] = 1
        return report
    rows = cr.order_units(all_rows)
    logged = cr.logged_units(log_rows)
    items = A._batch_get([cr.unit_key(r.link, r.unit) for r in rows if r.unit and r.link])
    wp = None
    for row in rows:
        pk = cr.unit_key(row.link, row.unit) if (row.unit and row.link) else ""
        d = cr.decide_unit(row, logged, items.get(pk), time.time())
        if d.action == "skip":
            report["skipped"][d.reason] = report["skipped"].get(d.reason, 0) + 1
            continue
        if budget <= 0:
            report["skipped"]["waiting for a later run"] = report["skipped"].get("waiting for a later run", 0) + 1
            continue
        budget -= 1
        try:
            wp = wp or wordpress()
            unit = wp.unit(row.link, row.unit)
        except WordPressSignInRequired as e:
            _set_wp_signout(str(e))
            report["paused"] = "WordPress sign-in required"
            break
        except WordPressError as e:
            report["problems"].append({"row": row.row, "unit": row.unit, "course": row.course_raw, "problem": str(e)})
            if not dry_run:
                _record_skip(pk, row, str(e))
            budget += 1  # try the next unit instead
            continue
        bad = [c.number for c in unit.chapters if len(c.pdf_urls) != 1]
        if bad:
            msg = f"Chapter(s) {bad} of Unit {row.unit} don't have exactly one PDF in WordPress."
            report["problems"].append({"row": row.row, "unit": row.unit, "course": row.course_raw, "problem": msg})
            if not dry_run:
                _record_skip(pk, row, msg)
            budget += 1
            continue
        ab, course = cr.split_course_name(row.course_raw)
        ab = ab or cr.canonical_awarding_body(unit.awarding_body)
        summary = {"row": row.row, "unit": row.unit, "awarding_body": ab, "course": course,
                   "chapters": [c.number for c in unit.chapters]}
        if not dry_run:
            if not _claim(client, doc_wb, row, pk, ab, course, unit):
                continue
        report["claimed"].append(summary)
    return report


MAX_MARK_FIXES_PER_RUN = 40


def all_units() -> list[dict]:
    """Every unit the bot has a record of (in progress and finished)."""
    out, kwargs = [], {}
    while True:
        resp = A.table.scan(FilterExpression=Attr("kind").eq("cunit"), **kwargs)
        out.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            return out
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def repair_marks(client, doc_wb: Workbook, rows: list, grid: list[list[str]], dry_run: bool) -> list[dict]:
    fixes = cr.plan_mark_fixes(rows, grid, all_units())
    if len(fixes) > MAX_MARK_FIXES_PER_RUN:
        # Something big happened to the tab (e.g. a column moved): don't mass-edit.
        A.alert("creator_marks", "SLC automation: 'Doc Formatting Completed Units' needs a look",
                f"{len(fixes)} 'auto'/'Done' marks look out of place in column I/J - more than the "
                f"{MAX_MARK_FIXES_PER_RUN} the bot fixes by itself. Was a column moved or the tab partly sorted? "
                "Nothing was changed.")
        return [{"skipped": f"{len(fixes)} fixes needed - too many, alert sent"}]
    done = []
    by_row: dict[int, list] = {}
    for f in fixes:
        by_row.setdefault(f.row, []).append(f)
    for row, row_fixes in by_row.items():
        before = rows_by_number(rows).get(row)
        if dry_run or before is None:
            done += [{"row": f.row, "col": f.col, "value": f.value, "reason": f.reason, "dry_run": dry_run}
                     for f in row_fixes]
            continue
        # Re-read the row right before writing: a person may be editing it now.
        cur = (client.read_range(doc_wb, f"A{row}:J{row}") or [[]])[0] + [""] * 10
        if (rules.norm_space(cur[0]) != before.course_raw or rules.norm_space(cur[1]) != before.link
                or rules.norm_space(cur[2]) != before.unit_raw or rules.norm_space(cur[8]) != before.videos_by):
            log.info("Doc row %s changed while repairing marks - left for the next run", row)
            continue
        for f in row_fixes:
            client.write_cell(doc_wb, f.col, f.row, f.value)
            done.append({"row": f.row, "col": f.col, "value": f.value or "(cleared)",
                         "course": f.course[:60], "unit": f.unit, "reason": f.reason})
    if done and not dry_run:
        log.warning("Repaired %d mark(s) in %s: %s", len(done), DOC_TAB, json.dumps(done)[:2000])
    return done


def rows_by_number(rows: list) -> dict[int, cr.DocRow]:
    return {r.row: r for r in rows}


def _record_skip(pk: str, row: cr.DocRow, message: str) -> None:
    A.table.put_item(Item={
        "pk": pk, "kind": "cunit", "cstate": "skipped", "error": message[:400], "doc_row": row.row,
        "course_raw": row.course_raw, "course_link": row.link, "unit": row.unit,
        "retry_at": Decimal(str(time.time() + SKIP_RECHECK_SECONDS)), "updated_at": _now(),
        "ttl": int(time.time()) + 60 * 86400,
    })


def _claim(client, doc_wb: Workbook, row: cr.DocRow, pk: str, ab: str, course: str, unit) -> bool:
    # Re-read the row right before writing: a person may have just taken it.
    cur = client.read_range(doc_wb, f"A{row.row}:I{row.row}")
    cur = (cur[0] if cur else []) + [""] * 9
    if rules.norm_space(cur[0]) != row.course_raw or rules.norm_space(cur[8]):
        log.info("Doc row %s changed before claiming - skipped", row.row)
        return False
    now = _now()
    try:
        A.table.put_item(
            Item={
                "pk": pk, "kind": "cunit", "active": ACTIVE, "cstate": "chapters",
                "created_at": now, "updated_at": now, "doc_row": row.row,
                "doc_wb_drive": doc_wb.drive_id, "doc_wb_item": doc_wb.item_id, "doc_ws_id": doc_wb.worksheet_id,
                "course_raw": row.course_raw, "course_link": row.link, "awarding_body": ab,
                "course_name": course, "unit": row.unit, "unit_title": unit.unit_title[:200],
                "chapters": [{"n": c.number, "title": c.title[:200], "pdf_url": c.pdf_urls[0],
                              "state": "queued", "attempts": 0} for c in sorted(unit.chapters, key=lambda c: c.number)],
            },
            ConditionExpression="attribute_not_exists(pk) OR cstate = :sk",
            ExpressionAttributeValues={":sk": "skipped"},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise
    client.write_cell(doc_wb, "I", row.row, cr.BOT_NAME)
    return True


# ── follow-up: write finished units into the video log ───────────────────

def follow_up() -> dict:
    if not ENABLED:
        return {}
    out = {"logged": 0}
    units = active_units()
    for u in units:
        if u.get("cstate") == "logging":
            try:
                _log_unit(u)
                out["logged"] += 1
            except (GraphError, LayoutError) as e:
                log.exception("Could not write unit %s to the log", u["pk"])
                if A._n(u.get("updated_at")) < time.time() - 2 * 3600:
                    A.alert("creator_log_write", "SLC automation: can't add chapter videos to the log",
                            f"Unit {u.get('unit')} of {u.get('course_name')}: {e}. The bot keeps retrying.")
        elif u.get("cstate") == "chapters":
            _check_stale(u)
    return out


def _check_stale(u: dict) -> None:
    """Nothing may sit unfinished for long: email after UNIT_STALE_SECONDS without
    progress; after UNIT_MAX_SECONDS close the unit (log what's ready, report
    the rest) - except while the creator is waiting for a sign-in or the
    NotebookLM limit, which is not the unit's fault."""
    now = time.time()
    name = f"Unit {u.get('unit')} of {u.get('awarding_body', '')} {u.get('course_name')} (Doc Formatting row {u.get('doc_row')})"
    waiting = paused() or (A.google_signout() is not None) or (wp_signout() is not None)
    if A._n(u.get("created_at"), now) < now - UNIT_MAX_SECONDS and not waiting:
        chapters = [c if c.get("state") == "ready" else
                    {**c, "state": "failed", "error": f"Not finished within {UNIT_MAX_SECONDS // 3600} hours "
                                                      f"(last state: {c.get('state')}). {c.get('error', '')}"[:300]}
                    for c in u["chapters"]]
        try:
            A.table.update_item(Key={"pk": u["pk"]}, UpdateExpression="SET chapters = :c, cstate = :lg, updated_at = :now",
                                ConditionExpression="cstate = :ch",
                                ExpressionAttributeValues={":c": chapters, ":lg": "logging", ":ch": "chapters",
                                                           ":now": _now()})
            log.warning("Closed %s after %s hours", name, UNIT_MAX_SECONDS // 3600)
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
        return
    if A._n(u.get("updated_at"), now) < now - UNIT_STALE_SECONDS:
        A.alert(f"creator_stuck_{u['pk'][-8:]}", "SLC automation: chapter videos are not progressing",
                f"{name} has had no progress for {UNIT_STALE_SECONDS // 3600} hours"
                + (f" (the creator is waiting: {waiting})" if waiting else "")
                + ".\nChapters: " + ", ".join(f"{c['n']}: {c.get('state')}" for c in u["chapters"])
                + f"\nIt is closed automatically after {UNIT_MAX_SECONDS // 3600} hours: ready chapters go to "
                  "the log and the rest are reported.")


def _log_unit(u: dict) -> None:
    pk = u["pk"]
    lock_until = Decimal(str(time.time() + LOG_LOCK_SECONDS))
    try:  # one writer at a time
        A.table.update_item(Key={"pk": pk}, UpdateExpression="SET log_lock = :l",
                            ConditionExpression="cstate = :lg AND (attribute_not_exists(log_lock) OR log_lock < :now)",
                            ExpressionAttributeValues={":l": lock_until, ":lg": "logging", ":now": _now()})
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return
        raise
    client, log_wb = A.open_sheet()
    ready = [c for c in sorted(u["chapters"], key=lambda c: int(c["n"])) if c.get("state") == "ready"]
    failed = [c for c in u["chapters"] if c.get("state") != "ready"]
    # Idempotent: skip chapters whose link is already in column G (a retry after a crash).
    existing = {r.url: r.row for r in client.read_rows(log_wb) if r.url}
    todo = [c for c in ready if c["link"] not in existing]
    today = dt.datetime.now(TZ).date()
    written_rows = sorted(existing[c["link"]] for c in ready if c["link"] in existing)
    if todo:
        start = client.last_used_row(log_wb) + 1
        end = start + len(todo) - 1
        blank = client.read_range(log_wb, f"A{start}:I{end}")
        if any(any(str(x).strip() for x in r) for r in blank):
            raise LayoutError(f"Rows {start}-{end} of the log are not empty; will retry.")
        values = [cr.log_row(today, u["awarding_body"], u["course_name"], int(u["unit"]), int(c["n"]), c["link"])
                  for c in todo]
        client.write_range(log_wb, f"A{start}:G{end}", values)
        want = [c["link"] for c in todo]
        for attempt in range(4):  # Excel online can take a moment to show a fresh write
            check = client.read_range(log_wb, f"G{start}:G{end}")
            got = [r[0].strip() if r else "" for r in check]
            if got == want:
                break
            time.sleep(2 + 2 * attempt)
        else:
            raise LayoutError(f"The log rows {start}-{end} did not read back as written (will re-check).")
        written_rows = sorted(written_rows + list(range(start, end + 1)))
    final = "done" if ready else "failed"
    if not ready:
        _unmark_doc(client, u)  # nothing made: let a person see it's not done
    elif not failed:
        _mark_doc_done(client, u)  # every chapter is in the log: Status = Done
    A.table.update_item(
        Key={"pk": pk},
        UpdateExpression="SET cstate = :d, finished_at = :now, updated_at = :now, log_rows = :r, #ttl = :ttl "
                         "REMOVE active, log_lock",
        ConditionExpression="cstate = :lg",
        ExpressionAttributeNames={"#ttl": "ttl"},
        ExpressionAttributeValues={":d": final, ":now": _now(), ":r": written_rows, ":lg": "logging",
                                   ":ttl": int(time.time()) + 180 * 86400},
    )
    if failed:
        lines = "\n".join(f"  Chapter {c['n']}: {c.get('error', 'not made')}" for c in failed)
        A.alert(f"creator_failed_{pk[-8:]}", "SLC automation: some chapter videos could not be made",
                f"Unit {u['unit']} of {u['awarding_body']} {u['course_name']} (Doc Formatting row {u['doc_row']}):\n"
                f"{lines}\n\nThe other chapters were added to the video log. Make the missing ones by hand.")


def _doc_row(client, u: dict) -> tuple[Workbook, int, list[str]] | None:
    """The unit's row in the Doc Formatting tab, found by its content (people
    sort and move rows there), only while it still says 'auto'."""
    wb = Workbook(u["doc_wb_drive"], u["doc_wb_item"], "", u["doc_ws_id"], DOC_TAB)
    first, grid = client.read_grid(wb)
    for i, r in enumerate(cr.doc_rows(first, grid)):
        if (r.course_raw == u["course_raw"] and r.link == u["course_link"] and r.unit == int(u["unit"])
                and r.videos_by.lower() == cr.BOT_NAME):
            return wb, r.row, list(grid[i]) + [""] * 12
    return None


def _unmark_doc(client, u: dict) -> None:
    try:
        hit = _doc_row(client, u)
        if hit:
            client.write_cell(hit[0], "I", hit[1], "")
    except (GraphError, LayoutError):
        log.exception("Could not clear 'auto' on Doc row %s", u.get("doc_row"))


def _mark_doc_done(client, u: dict) -> None:
    """Column J ('Status' next to 'Chapter-wise Videos') = Done, if still empty."""
    try:
        hit = _doc_row(client, u)
        if hit and not rules.norm_space(hit[2][9]):
            client.write_cell(hit[0], "J", hit[1], cr.STATUS_DONE)
    except (GraphError, LayoutError):
        log.exception("Could not set Status=Done on Doc row %s", u.get("doc_row"))


def status() -> dict:
    units = active_units()
    return {
        "enabled": ENABLED,
        "paused": paused() or ("WordPress sign-in required" if wp_signout() else ""),
        "units": [{"row": u.get("doc_row"), "unit": u.get("unit"), "course": u.get("course_name"),
                   "state": u.get("cstate"),
                   "chapters": {str(c["n"]): c.get("state") for c in u.get("chapters", [])}} for u in units],
    }

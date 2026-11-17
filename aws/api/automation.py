"""
Sheet -> NotebookLM -> video pipeline -> Google Drive automation (API Lambda side).

Events handled (see handler.py):
  {"action": "automation_tick"}      every 30 min: read the Excel log, claim new rows
                                     (H = "auto"), start the NotebookLM download task
  {"action": "automation_dry_run"}   same scan, but only REPORTS what it would do
  {"action": "automation_daily"}     daily summary email
  {"action": "automation_alert", ...} alert from the download task (throttled)
  reconcile (every minute)           follow_up(): job done -> save to Drive ->
                                     verify in Drive -> I = "Yes" (or "NO") -> clean up S3

One DynamoDB item per sheet row (pk "AUTO#<hash>", see rules.row_key):
  pending -> downloading -> processing -> saving -> verifying -> writing -> done
                    \\-> failing ------------------------------------------> failed
"failed" rows (I = "NO") are retried by later ticks up to rules.MAX_ATTEMPTS.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import time
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

import rules
from excel import AuthError, GraphClient, GraphError, LayoutError, Workbook

log = logging.getLogger()

REGION = os.environ.get("AWS_REGION", "eu-west-2")
TABLE_NAME = os.environ["JOBS_TABLE"]
BUCKET = os.environ["MEDIA_BUCKET"]
SSM_PREFIX = os.environ.get("SSM_PREFIX", "/slc-video-aws").rstrip("/")
CLUSTER = os.environ.get("WORKER_CLUSTER", "")
AUTOMATION_TASK_FAMILY = os.environ.get("AUTOMATION_TASK_FAMILY", "")
SUBNETS = [s for s in os.environ.get("WORKER_SUBNETS", "").split(",") if s]
SECURITY_GROUP = os.environ.get("WORKER_SECURITY_GROUP", "")
ALERT_TOPIC_ARN = os.environ.get("ALERT_TOPIC_ARN", "")
ENABLED = os.environ.get("AUTOMATION_ENABLED", "false").lower() == "true"
ALLOW_PRODUCTION_SHEET = os.environ.get("ALLOW_PRODUCTION_SHEET", "false").lower() == "true"
MAX_PER_RUN = max(1, int(os.environ.get("AUTOMATION_MAX_PER_RUN", "5")))
ALLOW_ON_DEMAND_FALLBACK = os.environ.get("ALLOW_ON_DEMAND_FALLBACK", "true").lower() == "true"
FUNCTION_NAME = os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "")

# Nothing may stay stuck: every state has a limit (see _advance).
PENDING_LIMIT_SECONDS = 2 * 3600        # waiting for the download task -> NO, retried later
DOWNLOAD_STALE_SECONDS = 20 * 60        # no "still working" signal -> row released / NO
PROCESSING_LIMIT_SECONDS = 90 * 60      # video job queued/processing -> NO
DRIVE_SAVE_TIMEOUT_SECONDS = 30 * 60    # Drive upload -> NO
VERIFY_LIMIT_SECONDS = 30 * 60          # Drive check keeps erroring -> NO
SHEET_WRITE_ALERT_SECONDS = 2 * 3600    # can't write Yes/NO -> email (keeps retrying)
TASK_LIMIT_SECONDS = 60 * 60            # a download task running longer is stopped
ALERT_THROTTLE_SECONDS = 12 * 3600
COLOUR_RECHECK_SECONDS = 6 * 3600
ITEM_TTL_DAYS = 120

ddb = boto3.resource("dynamodb", region_name=REGION)
table = ddb.Table(TABLE_NAME)
s3 = boto3.client("s3", region_name=REGION)
ssm = boto3.client("ssm", region_name=REGION)
ecs = boto3.client("ecs", region_name=REGION)
sns = boto3.client("sns", region_name=REGION)
lambda_client = boto3.client("lambda", region_name=REGION)


def _now() -> Decimal:
    return Decimal(str(round(time.time(), 3)))


def _n(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ── settings (SSM Parameter Store, set by the setup scripts) ─────────────

_settings: dict = {}
_settings_at = 0.0


def settings() -> dict:
    global _settings, _settings_at
    if _settings and time.time() - _settings_at < 300:
        return _settings
    base = f"{SSM_PREFIX}/automation"
    names = {
        f"{base}/ms/tenant": "tenant", f"{base}/ms/client_id": "client_id",
        f"{base}/ms/refresh_token": "refresh_token", f"{base}/sheet_url": "sheet_url",
        f"{base}/sheet_tab": "sheet_tab", f"{base}/since_date": "since_date",
        f"{base}/skip_colors": "skip_colors", f"{base}/drive_folder_url": "drive_folder_url",
        f"{base}/start_row": "start_row",
    }
    out = {v: "" for v in names.values()}
    names_list = list(names)
    for i in range(0, len(names_list), 10):
        resp = ssm.get_parameters(Names=names_list[i:i + 10], WithDecryption=True)
        for p in resp.get("Parameters", []):
            out[names[p["Name"]]] = p["Value"].strip()
    out["sheet_tab"] = out["sheet_tab"] or "chapterwise"
    # Off by default. Set automation/skip_colors to e.g. "red,yellow" to skip rows
    # whose status cells (F/G) have those colours.
    out["skip_colors"] = {c.strip().lower() for c in out["skip_colors"].split(",") if c.strip()}
    if out["since_date"]:
        try:
            out["since"] = dt.date.fromisoformat(out["since_date"])
        except ValueError:
            out["since"] = None
            log.warning("Ignoring invalid automation/since_date %r", out["since_date"])
    else:
        out["since"] = None
    # Rows from start_row downwards are done first, then the older rows above it
    # (nearest first). Blank = top to bottom.
    out["start_row"] = int(out["start_row"]) if out["start_row"].isdigit() else 0
    _settings, _settings_at = out, time.time()
    return out


def _store_refresh_token(token: str) -> None:
    ssm.put_parameter(Name=f"{SSM_PREFIX}/automation/ms/refresh_token", Value=token,
                      Type="SecureString", Overwrite=True)
    _settings["refresh_token"] = token


_graph: GraphClient | None = None


def graph() -> GraphClient:
    global _graph
    cfg = settings()
    if _graph is None or _graph.client_id != cfg["client_id"]:
        _graph = GraphClient(cfg["tenant"], cfg["client_id"], cfg["refresh_token"],
                             on_new_refresh_token=_store_refresh_token)
    return _graph


class SheetGuard(RuntimeError):
    pass


def open_sheet() -> tuple[GraphClient, Workbook]:
    cfg = settings()
    if not cfg["sheet_url"]:
        raise SheetGuard(f"No sheet configured. Set {SSM_PREFIX}/automation/sheet_url "
                         "(run automation_microsoft_signin.py).")
    client = graph()
    wb = client.open_workbook(cfg["sheet_url"], cfg["sheet_tab"])
    if not ALLOW_PRODUCTION_SHEET and "test" not in wb.name.lower():
        raise SheetGuard(
            f"Test mode: the configured workbook {wb.name!r} does not have TEST in its name, so the "
            "automation refuses to write to it. Use a copy named e.g. 'TEST - ...', or set "
            "AllowProductionSheet=true at cutover."
        )
    return client, wb


def _workbook_of(item: dict) -> Workbook:
    return Workbook(item["wb_drive"], item["wb_item"], item.get("wb_name", ""),
                    item["wb_ws_id"], item.get("wb_ws_name", ""))


# ── alerts ───────────────────────────────────────────────────────────────

def alert(kind: str, subject: str, message: str, throttle: int = ALERT_THROTTLE_SECONDS) -> bool:
    """Email the team (SNS), at most once per `throttle` seconds per kind."""
    if not ALERT_TOPIC_ARN:
        log.warning("ALERT (no topic configured) %s: %s", subject, message)
        return False
    now = time.time()
    try:
        table.put_item(
            Item={"pk": f"AUTOCFG#alert#{kind}", "kind": "autocfg", "sent_at": _now(),
                  "ttl": int(now) + 7 * 86400},
            ConditionExpression="attribute_not_exists(pk) OR sent_at < :cut",
            ExpressionAttributeValues={":cut": Decimal(str(now - throttle))},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise
    sns.publish(TopicArn=ALERT_TOPIC_ARN, Subject=subject[:99], Message=message)
    return True


def clear_alert(kind: str) -> None:
    try:
        table.delete_item(Key={"pk": f"AUTOCFG#alert#{kind}"})
    except ClientError:
        pass


def handle_alert_event(event: dict) -> dict:
    sent = alert(str(event.get("kind", "generic")), str(event.get("subject", "SLC automation")),
                 str(event.get("message", "")))
    return {"sent": sent}


# ── scanning the sheet (every 30 min) ────────────────────────────────────

def _batch_get(pks: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    pks = list(dict.fromkeys(pks))  # DynamoDB rejects a batch with repeated keys
    for i in range(0, len(pks), 100):
        keys = [{"pk": pk} for pk in pks[i:i + 100]]
        req = {TABLE_NAME: {"Keys": keys, "ConsistentRead": True}}
        while req:
            resp = ddb.batch_get_item(RequestItems=req)
            for it in resp.get("Responses", {}).get(TABLE_NAME, []):
                out[it["pk"]] = it
            req = resp.get("UnprocessedKeys") or None
            if req:
                time.sleep(0.5)
    return out


_last_scan: tuple | None = None


def scan(dry_run: bool) -> dict:
    cfg = settings()
    client, wb = open_sheet()
    rows = rules.order_rows(client.read_rows(wb), cfg["start_row"])
    global _last_scan
    _last_scan = (client, wb, rows)  # reused by the chapter-video creator in the same tick
    keyed =[(r, rules.row_key(wb.key, r.url, r.unit, r.chapter, r.course)) for r in rows
             if r.url and r.unit and r.chapter]
    items = _batch_get([k for _, k in keyed] + ["SKIP" + k for _, k in keyed])
    report = {"workbook": wb.name, "tab": wb.worksheet_name, "rows": len(rows),
              "claimed": [], "retrying": [], "skipped_colour": [], "in_progress": 0, "other_skips": {}}
    budget = MAX_PER_RUN
    key_by_row = {r.row: k for r, k in keyed}
    first_row_of: dict[str, int] = {}
    for row in rows:
        pk = key_by_row.get(row.row)
        if pk:
            if pk in first_row_of:
                # Same link, unit, chapter AND course as another row: the video is
                # handled once, on the first row; the repeat is left for a person.
                bucket = "repeat of another row (same link, unit, chapter and course)"
                report["other_skips"][bucket] = report["other_skips"].get(bucket, 0) + 1
                continue
            first_row_of[pk] = row.row
        item = items.get(pk) if pk else None
        decision = rules.decide(row, item)
        if decision.action == "skip":
            reason = decision.reason
            if reason.startswith("in progress"):
                report["in_progress"] += 1
            else:
                bucket = "done by a person" if reason.startswith("done by") else reason
                report["other_skips"][bucket] = report["other_skips"].get(bucket, 0) + 1
                if (reason.startswith("done by") and item and item.get("qstate") == "pending"
                        and not dry_run):
                    # A person wrote their name over "auto" before the bot started:
                    # drop the row so the bot never downloads or uploads it.
                    _set(pk, "pending", "SET qstate = :ab, #e = :m REMOVE active",
                         {":ab": "abandoned", ":m": f"Taken over by {row.done_by} before it started"},
                         {"#e": "error"})
                    report["taken_over"] = report.get("taken_over", 0) + 1
            continue
        if cfg["since"]:
            d = rules.row_date(row)
            if d is None or d < cfg["since"]:
                report["other_skips"]["before the start date"] = report["other_skips"].get("before the start date", 0) + 1
                continue
        if budget <= 0:
            report["other_skips"]["waiting for a later run"] = report["other_skips"].get("waiting for a later run", 0) + 1
            continue
        summary = {"row": row.row, "unit": row.unit, "chapter": row.chapter,
                   "awarding_body": row.awarding_body, "course": row.course, "url": row.url}
        if decision.action == "new" and not cfg["skip_colors"]:
            # Colour check is off (the default): cell colours are ignored.
            if not dry_run:
                _claim_new(client, wb, row, pk, decision.reset_attempts, item)
            report["claimed"].append(summary)
        elif decision.action == "new":
            cached = items.get("SKIP" + pk)
            if cached and _n(cached.get("checked_at")) > time.time() - COLOUR_RECHECK_SECONDS:
                report["skipped_colour"].append({**summary, "colour": cached.get("colour", "?"), "cached": True})
                continue
            row.colors = client.cell_colors(wb, row.row)
            colour = rules.skip_for_color(row.colors, cfg["skip_colors"])
            if colour:
                report["skipped_colour"].append({**summary, "colour": colour})
                if not dry_run:  # don't re-check this row's colour on every run
                    table.put_item(Item={"pk": "SKIP" + pk, "kind": "autoskip", "colour": colour,
                                         "checked_at": _now(), "ttl": int(time.time()) + 86400})
                continue
            summary["colours"] = row.colors
            if not dry_run:
                _claim_new(client, wb, row, pk, decision.reset_attempts, item)
            report["claimed"].append(summary)
        else:  # retry
            if not dry_run:
                _requeue(pk)
            report["retrying"].append({**summary, "attempts": int(item.get("attempts", 0))})
        budget -= 1
    return report


def _claim_new(client: GraphClient, wb: Workbook, row: rules.SheetRow, pk: str,
               reset: bool, previous: dict | None) -> None:
    # Column H first, so people see straight away the bot has taken the row.
    if rules.norm_space(row.done_by).lower() != rules.BOT_NAME:
        client.write_cell(wb, "H", row.row, rules.BOT_NAME)
    now = _now()
    attempts = 0 if (reset or not previous) else int(previous.get("attempts", 0))
    table.put_item(Item={
        "pk": pk, "kind": "auto", "active": "A", "qstate": "pending",
        "created_at": now, "updated_at": now, "attempts": attempts,
        "row": row.row, "url": row.url, "unit": row.unit, "chapter": row.chapter,
        "awarding_body": row.awarding_body, "course_raw": row.course_raw, "course_name": row.course,
        "sheet_date": row.date,
        "wb_drive": wb.drive_id, "wb_item": wb.item_id, "wb_name": wb.name,
        "wb_ws_id": wb.worksheet_id, "wb_ws_name": wb.worksheet_name,
    })


def _requeue(pk: str) -> None:
    table.update_item(
        Key={"pk": pk},
        UpdateExpression="SET qstate = :p, active = :a, updated_at = :now, created_at = :now "
                         "REMOVE #e, job_id, #ttl",
        ConditionExpression="qstate = :f",
        ExpressionAttributeNames={"#e": "error", "#ttl": "ttl"},
        ExpressionAttributeValues={":p": "pending", ":a": "A", ":f": "failed", ":now": _now()},
    )


def _active_auto_items() -> list[dict]:
    items, kwargs = [], {}
    while True:
        resp = table.query(IndexName="active", KeyConditionExpression=Key("active").eq("A"), **kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            return items
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def _automation_task_running() -> bool:
    resp = ecs.list_tasks(cluster=CLUSTER, family=AUTOMATION_TASK_FAMILY, desiredStatus="RUNNING")
    return bool(resp.get("taskArns"))


def _stop_overdue_tasks(now: float) -> int:
    """Backstop: stop any download task running longer than TASK_LIMIT_SECONDS.
    Its rows go back to pending (via the 20-minute heartbeat rule)."""
    if not AUTOMATION_TASK_FAMILY:
        return 0
    arns = ecs.list_tasks(cluster=CLUSTER, family=AUTOMATION_TASK_FAMILY, desiredStatus="RUNNING").get("taskArns", [])
    if not arns:
        return 0
    stopped = 0
    for t in ecs.describe_tasks(cluster=CLUSTER, tasks=arns).get("tasks", []):
        started = t.get("startedAt") or t.get("createdAt")
        if started and now - started.timestamp() > TASK_LIMIT_SECONDS:
            ecs.stop_task(cluster=CLUSTER, task=t["taskArn"],
                          reason=f"Automation download task ran over {TASK_LIMIT_SECONDS // 60} minutes")
            log.warning("Stopped overdue download task %s", t["taskArn"])
            stopped += 1
    return stopped


def _start_download_task() -> bool:
    if not AUTOMATION_TASK_FAMILY or _automation_task_running():
        return False
    net = {"awsvpcConfiguration": {"subnets": SUBNETS, "securityGroups": [SECURITY_GROUP],
                                   "assignPublicIp": "ENABLED"}}
    common = dict(cluster=CLUSTER, taskDefinition=AUTOMATION_TASK_FAMILY, count=1,
                  networkConfiguration=net, propagateTags="TASK_DEFINITION")
    try:
        import creator_sync
        nb = creator_sync.notebook_url() if creator_sync.ENABLED else ""
    except Exception:
        log.exception("Could not read the creator notebook setting")
        nb = ""
    if nb:
        common["overrides"] = {"containerOverrides": [
            {"name": "automation", "environment": [{"name": "CREATOR_NOTEBOOK_URL", "value": nb}]}]}
    resp = ecs.run_task(capacityProviderStrategy=[{"capacityProvider": "FARGATE_SPOT", "weight": 1}], **common)
    if not resp.get("tasks") and ALLOW_ON_DEMAND_FALLBACK:
        log.warning("Spot capacity unavailable for the download task (%s) - using regular Fargate",
                    resp.get("failures"))
        resp = ecs.run_task(launchType="FARGATE", **common)
    if not resp.get("tasks"):
        log.error("Could not start the download task: %s", resp.get("failures"))
        return False
    return True


SESSION_KEY = "automation/google-session.json"
# Open NotebookLM at least this often even when no rows are waiting, so the
# saved Google sign-in keeps being refreshed in AWS (and a sign-out is noticed
# early, not when the next row arrives).
KEEPALIVE_SECONDS = int(os.environ.get("AUTOMATION_KEEPALIVE_HOURS", "6")) * 3600


def _session_age() -> float | None:
    try:
        head = s3.head_object(Bucket=BUCKET, Key=SESSION_KEY)
    except ClientError:
        return None
    return time.time() - head["LastModified"].timestamp()


def tick(dry_run: bool = False) -> dict:
    if not ENABLED and not dry_run:
        return {"enabled": False}
    signout = google_signout()
    if signout and not dry_run:
        # Paused: claiming rows now would only turn them into NO later.
        # The sign-in script clears the flag and the next run resumes.
        alert("google_signin", "SLC automation: paused - please sign in to Google again",
              "The video automation is paused because the saved Google sign-in for the content team "
              "account has stopped working.\n\n"
              f"Reason: {signout.get('reason', 'unknown')}\n\n"
              "Nothing is lost: rows already marked 'auto' wait, and no new rows are taken.\n"
              "To resume (about 2 minutes, needs the account's 2-step code): on the PC with the project run\n"
              "    py -3.12 aws\\scripts\\automation_google_signin.py\n"
              "and sign in in the browser window that opens.")
        return {"paused": "Google sign-in required", "reason": signout.get("reason", "")}
    try:
        from handler import _drive_module
        automation_root(_drive_module())
    except DriveFolderGuard as e:
        alert("drive_folder", "SLC automation: stopped - Drive folder not set", str(e))
        return {"error": str(e)}
    try:
        report = scan(dry_run)
    except AuthError as e:
        alert("microsoft_signin", "SLC automation: please sign in to Microsoft again",
              "The video automation can't open the Excel log because the saved Microsoft sign-in "
              f"stopped working.\n\nDetail: {e}\n\nFix: run  py -3.12 aws\\scripts\\automation_microsoft_signin.py  "
              "and sign in. Rows waiting in the sheet are picked up on the next run.")
        return {"error": str(e)}
    except (SheetGuard, LayoutError) as e:
        alert("sheet_problem", "SLC automation: stopped - sheet problem",
              f"The video automation did not touch the sheet:\n\n{e}")
        return {"error": str(e)}
    except GraphError as e:
        log.exception("Graph error during the sheet scan")
        return {"error": str(e)}
    clear_alert("microsoft_signin")
    clear_alert("sheet_problem")
    creator_work = False
    try:
        import creator_sync
        if creator_sync.ENABLED and _last_scan:
            client, wb, log_rows = _last_scan
            report["creator"] = creator_sync.scan(client, wb, log_rows, dry_run)
            creator_work = creator_sync.needs_task()
    except (LayoutError, GraphError) as e:
        alert("creator_sheet", "SLC automation: chapter-video creator stopped - sheet problem",
              f"The chapter-video creator did not touch the sheets:\n\n{e}")
        report["creator"] = {"error": str(e)}
    except Exception as e:  # never let the creator break the download automation
        log.exception("Chapter-video creator scan failed")
        report["creator"] = {"error": f"{e.__class__.__name__}: {e}"}
    if not dry_run:
        pending = [i for i in _active_auto_items() if i.get("qstate") == "pending"]
        report["pending_total"] = len(pending)
        keepalive = False
        if not pending and not creator_work:
            age = _session_age()
            keepalive = age is not None and age > KEEPALIVE_SECONDS
            if keepalive:
                report["keepalive"] = f"Google session last refreshed {age / 3600:.1f} h ago"
        report["download_task_started"] = bool(pending or keepalive or creator_work) and _start_download_task()
    log.info("Automation %s: %s", "dry run" if dry_run else "tick", json.dumps(report, default=str)[:3000])
    return report


# ── follow-up (every minute, from the reconciler) ────────────────────────


def with_updated_at(expr: str) -> str:
    """Add "updated_at = :now" to the SET clause of an update expression.
    (Appending it at the end breaks expressions that also REMOVE fields.)"""
    if not expr.startswith("SET "):
        raise ValueError(f"update expression must start with SET: {expr!r}")
    return "SET updated_at = :now, " + expr[4:]


def _set(pk: str, from_state: str, expr: str, values: dict | None = None, names: dict | None = None) -> bool:
    values = {**(values or {}), ":from": from_state, ":now": _now()}
    try:
        table.update_item(Key={"pk": pk}, UpdateExpression=with_updated_at(expr),
                          ConditionExpression="qstate = :from", ExpressionAttributeValues=values,
                          **({"ExpressionAttributeNames": names} if names else {}))
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def _fail(item: dict, message: str, count_attempt: bool = False) -> None:
    expr = "SET qstate = :f, #e = :m"
    values = {":f": "failing", ":m": message[:400]}
    if count_attempt:  # failures that happen before the download task claims the row
        expr += ", attempts = if_not_exists(attempts, :zero) + :one"
        values.update({":zero": 0, ":one": 1})
    _set(item["pk"], item["qstate"], expr, values, {"#e": "error"})
    item["qstate"], item["error"] = "failing", message[:400]


def _locate_row(client: GraphClient, wb: Workbook, item: dict) -> rules.SheetRow | None:
    """The row this item belongs to, even if rows were inserted above it."""
    def same(r: rules.SheetRow | None) -> bool:
        return bool(r) and rules.row_key(wb.key, r.url, r.unit, r.chapter, r.course) == item["pk"]

    try:
        r = client.read_row(wb, int(item["row"]))
    except GraphError:
        r = None
    if same(r):
        return r
    for r in client.read_rows(wb):
        if same(r):
            return r
    return None


def _write_result(item: dict, value: str) -> str:
    """Write column I. Returns 'written', 'taken_over' or raises GraphError."""
    client = graph()
    wb = _workbook_of(item)
    row = _locate_row(client, wb, item)
    if row is None or rules.norm_space(row.done_by).lower() not in ("", rules.BOT_NAME):
        return "taken_over"  # a person put their name on it, or the row was deleted
    if row.row != int(item["row"]):
        table.update_item(Key={"pk": item["pk"]}, UpdateExpression="SET #r = :r",
                          ExpressionAttributeNames={"#r": "row"}, ExpressionAttributeValues={":r": row.row})
    if rules.norm_space(row.done_by).lower() != rules.BOT_NAME:
        client.write_cell(wb, "H", row.row, rules.BOT_NAME)
    client.write_cell(wb, "I", row.row, value)
    return "written"


def _cleanup_s3(job: dict | None) -> None:
    if not job:
        return
    for key in (job.get("result_key"), job.get("upload_key")):
        if key:
            try:
                s3.delete_object(Bucket=BUCKET, Key=key)
            except ClientError:
                pass


def _get_job(job_id: str | None) -> dict | None:
    if not job_id:
        return None
    return table.get_item(Key={"pk": f"JOB#{job_id}"}, ConsistentRead=True).get("Item")


def _start_drive_save(job: dict) -> bool:
    try:
        table.update_item(
            Key={"pk": job["pk"]},
            UpdateExpression="SET drive_status = :saving REMOVE drive_error",
            ConditionExpression="drive_status <> :saving AND drive_status <> :saved",
            ExpressionAttributeValues={":saving": "saving", ":saved": "saved"},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return False
    lambda_client.invoke(FunctionName=FUNCTION_NAME, InvocationType="Event",
                         Payload=json.dumps({"action": "drive_save", "job_id": job["job_id"]}).encode())
    return True


class DriveFolderGuard(RuntimeError):
    pass


def automation_root(drive) -> str:
    """Root Drive folder for automated uploads.

    automation/drive_folder_url, when set, is used ONLY for automated rows; the
    web app's "Save to Drive" keeps using gdrive/folder_url. In test mode
    (AllowProductionSheet=false) a separate test folder is required, so the
    automation can never write into the folder the team uses."""
    from handler import PRODUCTION_DRIVE_FOLDER_ID

    own = drive._extract_folder_id(settings().get("drive_folder_url", ""))
    if not ALLOW_PRODUCTION_SHEET:
        if not own:
            raise DriveFolderGuard(
                f"Test mode: set {SSM_PREFIX}/automation/drive_folder_url to a separate TEST Drive folder "
                "(automation_microsoft_signin.py asks for it).")
        if own in (PRODUCTION_DRIVE_FOLDER_ID, drive._root_folder_id()):
            raise DriveFolderGuard(
                "Test mode: automation/drive_folder_url points at the folder the team uses. "
                "Use a separate, new TEST folder.")
    root = own or drive._root_folder_id()
    if not root:
        raise DriveFolderGuard("No Drive folder is configured for the automation.")
    return root


_course_folders: dict[tuple[str, str], str] = {}


FOLDER_LOCK_SECONDS = 60
FOLDER_WAIT_SECONDS = 120


def _oldest_folder(drive, svc, name: str, parent: str) -> str | None:
    """Oldest folder with this name, so every save, duplicate check and Drive
    check agrees on ONE folder even if duplicates exist."""
    safe = name.replace("\\", "\\\\").replace("'", "\\'")
    resp = svc.files().list(
        q=f"mimeType = '{drive.FOLDER_MIME}' and name = '{safe}' and '{parent}' in parents and trashed = false",
        fields="files(id, createdTime)", orderBy="createdTime", pageSize=10,
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute()
    files = resp.get("files", [])
    return files[0]["id"] if files else None


def _folder_usable(svc, folder_id: str) -> bool:
    try:
        f = svc.files().get(fileId=folder_id, fields="id, trashed", supportsAllDrives=True).execute()
        return not f.get("trashed")
    except Exception:
        return False


def course_folder(drive, job: dict) -> str:
    """"<Awarding Body> - <Course>" under the automation's root - same naming as drive.py.

    Several videos of a NEW course are often saved at the same moment. A Drive
    search can't prevent two of them creating the folder twice (that happened
    in testing), so creation is serialised with a DynamoDB lock, and the
    folder id is recorded there for the other saves to use."""
    import hashlib

    root = automation_root(drive)
    parts = [p.strip() for p in (job.get("awarding_body", ""), job["course_name"]) if p and p.strip()]
    name = drive._clean(" - ".join(parts))
    key = (root, name.lower())
    if key in _course_folders:
        return _course_folders[key]
    svc = drive.get_service()
    pk = "AUTOFOLDER#" + hashlib.sha1(f"{root}|{name.lower()}".encode()).hexdigest()[:24]
    deadline = time.time() + FOLDER_WAIT_SECONDS
    try:
        while True:
            rec = table.get_item(Key={"pk": pk}, ConsistentRead=True).get("Item") or {}
            if rec.get("folder_id"):
                if _folder_usable(svc, rec["folder_id"]):
                    folder_id = rec["folder_id"]
                    break
                table.delete_item(Key={"pk": pk})  # folder was deleted/trashed by someone
                continue
            folder_id = _oldest_folder(drive, svc, name, root)
            if folder_id:
                table.put_item(Item={"pk": pk, "kind": "autofolder", "folder_id": folder_id, "name": name})
                break
            now = time.time()
            try:
                table.put_item(
                    Item={"pk": pk, "kind": "autofolder", "name": name,
                          "lock_until": Decimal(str(now + FOLDER_LOCK_SECONDS))},
                    ConditionExpression="attribute_not_exists(pk) OR "
                                        "(attribute_not_exists(folder_id) AND lock_until < :now)",
                    ExpressionAttributeValues={":now": Decimal(str(now))},
                )
            except ClientError as e:
                if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                    raise
                if time.time() > deadline:
                    raise RuntimeError("Timed out waiting for another save to create the course folder.")
                time.sleep(2)  # another save is creating it right now
                continue
            folder_id = _oldest_folder(drive, svc, name, root) or drive._create_folder(svc, name, root)
            table.put_item(Item={"pk": pk, "kind": "autofolder", "folder_id": folder_id, "name": name})
            break
    except drive.HttpError as e:
        raise RuntimeError(drive._explain_drive_error(e)) from e
    _course_folders[key] = folder_id
    return folder_id


def upload_video(drive, job: dict, local) -> dict:
    """drive.upload_video, but into the automation's own root folder."""
    folder_id = course_folder(drive, job)
    name = drive.build_drive_filename(job["course_name"], job["unit_number"], job.get("chapter_number", ""))
    media = drive.MediaFileUpload(str(local), mimetype="video/mp4", resumable=True, chunksize=8 * 1024 * 1024)
    request = drive.get_service().files().create(
        body={"name": name, "parents": [folder_id]}, media_body=media,
        fields="id, name, webViewLink", supportsAllDrives=True,
    )
    try:
        response = None
        while response is None:
            _status, response = request.next_chunk()
    except drive.HttpError as e:
        raise RuntimeError(drive._explain_drive_error(e)) from e
    return {"file_id": response["id"], "name": response["name"],
            "web_view_link": response.get("webViewLink"), "folder_id": folder_id}


def find_existing_drive_file(drive, job: dict) -> dict | None:
    """Same course folder + same "Unit X | Chapter Y.mp4" already in Drive?
    Then the video is already there - never upload a duplicate."""
    folder_id = course_folder(drive, job)
    name = drive.build_drive_filename(job["course_name"], job["unit_number"], job.get("chapter_number", ""))
    safe = name.replace("\\", "\\\\").replace("'", "\\'")
    resp = drive.get_service().files().list(
        q=f"name = '{safe}' and '{folder_id}' in parents and trashed = false",
        fields="files(id, name, size, md5Checksum, webViewLink)", pageSize=5,
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute()
    files = [f for f in resp.get("files", []) if int(f.get("size") or 0) > 0]
    if not files:
        return None
    f = files[0]
    return {"file_id": f["id"], "name": f["name"], "web_view_link": f.get("webViewLink"),
            "folder_id": folder_id}


def verify_drive(drive, job: dict) -> str | None:
    """Re-read the file from Drive. Returns None when it is right, else the problem."""
    file_id = job.get("drive_file_id")
    if not file_id:
        return "Drive did not return a file id"
    f = drive.get_service().files().get(
        fileId=file_id, fields="id, name, parents, size, md5Checksum, trashed",
        supportsAllDrives=True,
    ).execute()
    folder_id = course_folder(drive, job)
    name = drive.build_drive_filename(job["course_name"], job["unit_number"], job.get("chapter_number", ""))
    if f.get("trashed"):
        return "the file is in the Drive bin"
    if folder_id not in (f.get("parents") or []):
        return "the file is not in the course folder"
    if f.get("name") != name:
        return f"the file is named {f.get('name')!r}, expected {name!r}"
    size = int(f.get("size") or 0)
    if size <= 0:
        return "the file in Drive is empty"
    if job.get("drive_size") and size != int(job["drive_size"]):
        return f"size mismatch (Drive {size} bytes, expected {int(job['drive_size'])})"
    if job.get("drive_md5") and f.get("md5Checksum") and f["md5Checksum"] != job["drive_md5"]:
        return "checksum mismatch - the upload is corrupt"
    return None


def follow_up() -> dict:
    """Advance every in-flight row one step. Cheap when nothing is in flight."""
    # Always (not only while download rows are in flight): a NotebookLM task
    # doing chapter-video work must also be stopped if it runs too long.
    if AUTOMATION_TASK_FAMILY:
        try:
            _stop_overdue_tasks(time.time())
        except Exception:
            log.exception("Could not check for overdue NotebookLM tasks")
    out = _follow_up_downloads()
    try:
        import creator_sync
        if creator_sync.ENABLED:
            c = creator_sync.follow_up()
            if c.get("logged"):
                out["creator_logged"] = c["logged"]
    except Exception:
        log.exception("Chapter-video creator follow-up failed")
    return out


def _follow_up_downloads() -> dict:
    items = _active_auto_items()
    if not items:
        return {}
    from handler import _drive_module  # loaded lazily; same module object as the Lambda entry point
    now = time.time()
    counts: dict[str, int] = {}
    paused = bool(google_signout())
    for brief in items:
        state = brief.get("qstate")
        item = table.get_item(Key={"pk": brief["pk"]}, ConsistentRead=True).get("Item")
        if not item or item.get("qstate") != state:
            continue
        try:
            _advance(item, now, _drive_module, google_paused=paused)
            counts[item["qstate"]] = counts.get(item["qstate"], 0) + 1
        except AuthError as e:
            alert("microsoft_signin", "SLC automation: please sign in to Microsoft again",
                  f"The automation can't write to the Excel log: {e}\n\nRun  py -3.12 "
                  "aws\\scripts\\automation_microsoft_signin.py  and sign in.")
            break
        except Exception:
            log.exception("Automation follow-up failed for %s (state %s)", item["pk"], state)
    return counts


def google_signout() -> dict | None:
    """The "Google sign-in required" flag set by the download task, if any."""
    return table.get_item(Key={"pk": "AUTOCFG#google"}).get("Item")


def _advance(item: dict, now: float, drive_loader, google_paused: bool = False) -> None:
    pk, state = item["pk"], item["qstate"]
    age = now - _n(item.get("updated_at"), now)  # time spent in the current state

    if state == "pending":
        if google_paused:
            # Waiting for a Google sign-in is not a failure of this row: keep
            # its clock at zero so the 2-hour limit starts again after sign-in.
            _set(pk, state, "SET qstate = :p", {":p": "pending"})
            return
        if age > PENDING_LIMIT_SECONDS:
            _fail(item, "The NotebookLM download didn't start within 2 hours (Google sign-in problem or "
                        "AWS capacity). It will be retried.", count_attempt=True)
        return

    if state == "downloading":
        if _n(item.get("heartbeat")) < now - DOWNLOAD_STALE_SECONDS:
            if int(item.get("attempts", 0)) >= rules.MAX_ATTEMPTS:
                _fail(item, "The NotebookLM download step stopped repeatedly.")
            else:
                _set(pk, state, "SET qstate = :p REMOVE owner_id", {":p": "pending"})
        return

    if state == "processing":
        job = _get_job(item.get("job_id"))
        if not job:
            _fail(item, "The video job disappeared before it finished.")
        elif job.get("status") == "failed":
            _fail(item, f"Video processing failed: {job.get('error') or 'unknown error'}")
        elif job.get("status") == "done":
            _start_drive_save(job)
            _set(pk, state, "SET qstate = :s, saving_since = :now", {":s": "saving"})
        elif age > PROCESSING_LIMIT_SECONDS:
            _fail(item, "Video processing didn't finish within 90 minutes.")
        return

    if state == "saving":
        job = _get_job(item.get("job_id"))
        if not job:
            _fail(item, "The video job disappeared before it was saved to Drive.")
            return
        ds = job.get("drive_status")
        if ds == "saved":
            if not _set(pk, state, "SET qstate = :v", {":v": "verifying"}):
                return
            item["qstate"] = state = "verifying"
        elif ds == "failed":
            _fail(item, f"Saving to Google Drive failed: {job.get('drive_error') or 'unknown error'}")
            return
        elif ds == "idle":
            _start_drive_save(job)  # first trigger was lost
            return
        else:
            if _n(item.get("saving_since")) < now - DRIVE_SAVE_TIMEOUT_SECONDS:
                _fail(item, "Saving to Google Drive took more than 30 minutes.")
            return

    if state == "verifying":
        if age > VERIFY_LIMIT_SECONDS:  # Drive kept erroring while we checked the file
            _fail(item, "Could not check the file in Google Drive for 30 minutes.")
            return
        job = _get_job(item.get("job_id"))
        problem = verify_drive(drive_loader(), job) if job else "the video job disappeared"
        if problem:
            _fail(item, f"Drive check failed: {problem}")
            return
        _set(pk, state, "SET qstate = :w, drive_link = :l, drive_file_id = :f",
             {":w": "writing", ":l": job.get("drive_link") or "", ":f": job.get("drive_file_id") or ""})
        item["qstate"] = state = "writing"

    if state in ("writing", "failing") and age > SHEET_WRITE_ALERT_SECONDS:
        # Can't finish without writing to the sheet; keep retrying, but tell someone.
        alert("sheet_write_stuck", "SLC automation: can't write to the Excel log",
              f"Row {item.get('row')} (Unit {item.get('unit')} Chapter {item.get('chapter')}) has been waiting "
              f"over 2 hours to write column I. Check that the log file is not locked, deleted or moved, "
              "and that the Microsoft sign-in still works. The bot keeps retrying every minute.")

    if state == "writing":
        outcome = _write_result(item, rules.ADDED_YES)
        job = _get_job(item.get("job_id"))
        _cleanup_s3(job)  # the video is safely in Drive: don't keep paying for S3
        final = "done" if outcome == "written" else "abandoned"
        _set(pk, state, "SET qstate = :d, finished_at = :now, #ttl = :ttl REMOVE active",
             {":d": final, ":ttl": int(now) + ITEM_TTL_DAYS * 86400}, {"#ttl": "ttl"})
        return

    if state == "failing":
        outcome = _write_result(item, rules.ADDED_NO)
        _cleanup_s3(_get_job(item.get("job_id")))
        final = "failed" if outcome == "written" else "abandoned"
        _set(pk, state, "SET qstate = :d, finished_at = :now, #ttl = :ttl REMOVE active",
             {":d": final, ":ttl": int(now) + ITEM_TTL_DAYS * 86400}, {"#ttl": "ttl"})


# ── daily summary ────────────────────────────────────────────────────────

def daily_summary() -> dict:
    if not ENABLED:
        return {"enabled": False}
    since = time.time() - 86400
    items, kwargs = [], {}
    while True:
        resp = table.scan(FilterExpression=Attr("kind").eq("auto") & Attr("updated_at").gte(Decimal(str(since))),
                          **kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    google = table.get_item(Key={"pk": "AUTOCFG#google"}).get("Item")
    done = [i for i in items if i.get("qstate") == "done"]
    failed = [i for i in items if i.get("qstate") == "failed"]
    taken = [i for i in items if i.get("qstate") == "abandoned"]
    busy = [i for i in items if i.get("qstate") in rules.ACTIVE_STATES]
    if not (done or failed or taken or google):
        return {"sent": False, "reason": "no activity"}

    def line(i):
        return (f"  - Row {i.get('row')}: {i.get('awarding_body', '')} / {i.get('course_name', '')} - "
                f"Unit {i.get('unit')} Chapter {i.get('chapter')}")

    parts = [f"Video automation - last 24 hours ({dt.date.today().isoformat()})", ""]
    if google:
        parts += ["!! Google sign-in needed: the bot can't open NotebookLM until someone runs",
                  "   py -3.12 aws\\scripts\\automation_google_signin.py", ""]
    parts.append(f"Added to Drive (column I = Yes): {len(done)}")
    parts += [line(i) for i in sorted(done, key=lambda x: int(x.get('row', 0)))[:50]]
    parts += ["", f"Not added (column I = NO): {len(failed)}"]
    for i in sorted(failed, key=lambda x: int(x.get("row", 0)))[:50]:
        left = rules.MAX_ATTEMPTS - int(i.get("attempts", 0))
        retry = f"will retry ({left} left)" if left > 0 else "no more retries - needs a person"
        parts += [line(i), f"      reason: {i.get('error', 'unknown')}", f"      {retry}"]
    if taken:
        parts += ["", f"Taken over by a person (bot stopped): {len(taken)}"] + [line(i) for i in taken[:20]]
    if busy:
        parts += ["", f"Still in progress: {len(busy)}"]
    sns_sent = False
    if ALERT_TOPIC_ARN:
        sns.publish(TopicArn=ALERT_TOPIC_ARN, Subject="SLC video automation - daily summary",
                    Message="\n".join(parts))
        sns_sent = True
    return {"sent": sns_sent, "done": len(done), "failed": len(failed)}


def status() -> dict:
    """Small public summary for /healthz (no row details)."""
    google = table.get_item(Key={"pk": "AUTOCFG#google"}).get("Item")
    active = _active_auto_items()
    by_state: dict[str, int] = {}
    for i in active:
        by_state[i.get("qstate", "?")] = by_state.get(i.get("qstate", "?"), 0) + 1
    return {"enabled": ENABLED, "production_sheet_allowed": ALLOW_PRODUCTION_SHEET,
            "google_signin_required": bool(google), "in_flight": by_state}

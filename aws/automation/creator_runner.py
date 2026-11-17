"""
Chapter-video creator, NotebookLM side (runs inside the download task, with
the same signed-in browser).

Each run:
  1. collect finished videos: for every chapter "generating", find its Studio
     item; when ready, check it was made from that chapter's PDF only, rename
     it "U<unit>C<chapter> - <course>" and copy its share link -> "ready";
  2. start new videos while fewer than MAX_GENERATING are being made:
     download the chapter PDF, upload it to the PCS notebook, Video Overview
     (Explainer, Classic, only that PDF, the team's prompt) -> "generating";
  3. a unit whose chapters are all finished goes to "logging" (the API then
     writes the rows into the video log).
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tempfile
import time
import urllib.request
from decimal import Decimal
from pathlib import Path

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

import creator
import creator_rules as cr
from creator import CreatorError, NotebookUnavailable, QuotaReached

log = logging.getLogger("creator")

MAX_GENERATING = int(os.environ.get("CREATOR_MAX_GENERATING", "3"))
GENERATING_LIMIT_SECONDS = int(os.environ.get("CREATOR_GENERATING_LIMIT", str(90 * 60)))
QUOTA_PAUSE_SECONDS = 3 * 3600
ACTIVE = "C"


def _now() -> Decimal:
    return Decimal(str(round(time.time(), 3)))


class Store:
    """DynamoDB access for creator units (CUNIT# items, chapters inside)."""

    def __init__(self, table):
        self.table = table

    def units(self) -> list[dict]:
        pks, kwargs = [], {}
        while True:
            resp = self.table.query(IndexName="active", KeyConditionExpression=Key("active").eq(ACTIVE), **kwargs)
            pks += [i["pk"] for i in resp.get("Items", [])]
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        out = []
        for pk in pks:
            it = self.table.get_item(Key={"pk": pk}, ConsistentRead=True).get("Item")
            if it and it.get("cstate") == "chapters":
                out.append(it)
        return sorted(out, key=lambda u: float(u.get("created_at", 0)))

    def save_chapter(self, unit: dict, index: int, **fields) -> None:
        """Update one chapter entry (only while the unit is still being made)."""
        sets, values, names = ["updated_at = :now"], {":now": _now(), ":ch": "chapters"}, {}
        for k, v in fields.items():
            names[f"#{k}"] = k
            values[f":{k}"] = v
            sets.append(f"chapters[{index}].#{k} = :{k}")
        self.table.update_item(Key={"pk": unit["pk"]}, UpdateExpression="SET " + ", ".join(sets),
                               ConditionExpression="cstate = :ch", ExpressionAttributeNames=names,
                               ExpressionAttributeValues=values)
        unit["chapters"][index].update(fields)

    def finish_unit_if_done(self, unit: dict) -> bool:
        if any(c.get("state") not in ("ready", "failed") for c in unit["chapters"]):
            return False
        try:
            self.table.update_item(Key={"pk": unit["pk"]}, UpdateExpression="SET cstate = :lg, updated_at = :now",
                                   ConditionExpression="cstate = :ch",
                                   ExpressionAttributeValues={":lg": "logging", ":ch": "chapters", ":now": _now()})
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
        return True

    def pause(self, reason: str) -> None:
        self.table.put_item(Item={"pk": "AUTOCFG#creator_pause", "kind": "autocfg", "reason": reason[:300],
                                  "until": Decimal(str(time.time() + QUOTA_PAUSE_SECONDS)), "updated_at": _now()})

    def paused(self) -> bool:
        p = self.table.get_item(Key={"pk": "AUTOCFG#creator_pause"}).get("Item")
        return bool(p and float(p.get("until", 0)) > time.time())


def _fail(store: Store, unit: dict, i: int, message: str) -> None:
    ch = unit["chapters"][i]
    attempts = int(ch.get("attempts", 0))
    state = "failed" if attempts >= cr.MAX_ATTEMPTS else "queued"
    log.warning("Unit %s chapter %s: %s (%s)", unit.get("unit"), ch.get("n"), message, state)
    store.save_chapter(unit, i, state=state, error=message[:300])


def _download_pdf(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
    if not data.startswith(b"%PDF"):
        raise CreatorError(f"The chapter PDF link did not return a PDF: {url[-80:]}")
    dest.write_bytes(data)


def run(context, table, notebook_url: str, deadline: float, alert=None) -> dict:
    """One creator pass. `alert(kind, subject, message)` emails the team."""
    store = Store(table)
    units = store.units()
    report = {"collected": 0, "started": 0}
    if not units or not notebook_url:
        return report
    page = context.new_page()
    work = Path(tempfile.mkdtemp(prefix="creator-"))
    try:
        creator.open_notebook(page, notebook_url)
        items = {a.id: a for a in creator.studio_items(page)}

        # 1. collect finished videos
        for unit in units:
            for i, ch in enumerate(unit["chapters"]):
                if ch.get("state") == "starting":
                    # The task stopped between uploading and noting the new video.
                    _fail(store, unit, i, "The previous run stopped while starting this video.")
                    continue
                if ch.get("state") != "generating":
                    continue
                a = items.get(ch.get("artifact_id", ""))
                title = creator.video_title(int(unit["unit"]), int(ch["n"]), unit["course_name"])
                source = ch.get("source", "")
                if a is None:
                    _fail(store, unit, i, "The video disappeared from NotebookLM Studio.")
                elif a.generating:
                    if time.time() - float(ch.get("started_at", 0)) > GENERATING_LIMIT_SECONDS:
                        _fail(store, unit, i, f"NotebookLM was still making the video after "
                                              f"{GENERATING_LIMIT_SECONDS // 60} minutes.")
                elif a.failed or not a.ready_video:
                    _fail(store, unit, i, f"NotebookLM could not make the video ({a.title} {a.details}).")
                else:
                    try:
                        used = creator.item_sources(page, a.id)
                        if used != [source]:
                            _fail(store, unit, i, f"The video was made from {used}, not only {source}.")
                            continue
                        if a.title != title:
                            creator.rename_item(page, a.id, title)
                        link = creator.share_link(page, a.id)
                        store.save_chapter(unit, i, state="ready", link=link, video_title=title,
                                           ready_at=_now(), error="")
                        report["collected"] += 1
                    except CreatorError as e:
                        _fail(store, unit, i, str(e))
            store.finish_unit_if_done(unit)

        # 2. start new videos
        if store.paused():
            report["paused"] = True
            return report
        generating = sum(1 for u in units for c in u["chapters"] if c.get("state") == "generating")
        for unit in units:
            for i, ch in enumerate(unit["chapters"]):
                if generating >= MAX_GENERATING or time.time() > deadline:
                    return report
                if ch.get("state") != "queued":
                    continue
                u, n, course = int(unit["unit"]), int(ch["n"]), unit["course_name"]
                title = creator.video_title(u, n, course)
                try:
                    raw = work / f"chapter-{u}-{n}.pdf"
                    _download_pdf(ch["pdf_url"], raw)
                    problem = creator.pdf_matches(raw, course, u)
                    if problem:
                        # Wrong PDF in WordPress: retrying can't help - a person must fix the page.
                        store.save_chapter(unit, i, state="failed", attempts=cr.MAX_ATTEMPTS,
                                           error=f"Not made: {problem}"[:300])
                        log.warning("U%sC%s %s: %s", u, n, course, problem)
                        continue
                    digest = hashlib.sha1(raw.read_bytes()).hexdigest()
                    source = creator.source_name(u, n, course, digest)
                    pdf = work / source
                    raw.rename(pdf)
                    # Already made from this exact PDF (e.g. a retry after a crash)? Reuse it.
                    existing = [a for a in creator.studio_items(page) if a.title == title and a.ready_video]
                    if existing and creator.item_sources(page, existing[0].id) == [source]:
                        store.save_chapter(unit, i, state="generating", artifact_id=existing[0].id, source=source,
                                           started_at=_now(), attempts=int(ch.get("attempts", 0)) + 1)
                        continue
                    creator.upload_source(page, pdf)
                    store.save_chapter(unit, i, state="starting", source=source,
                                       attempts=int(ch.get("attempts", 0)) + 1)
                    artifact_id = creator.generate(page, source)
                    store.save_chapter(unit, i, state="generating", artifact_id=artifact_id, started_at=_now())
                    generating += 1
                    report["started"] += 1
                    log.info("Started U%sC%s - %s (%s)", u, n, course, artifact_id)
                except QuotaReached as e:
                    store.save_chapter(unit, i, state="queued", attempts=int(ch.get("attempts", 0)))
                    store.pause(f"NotebookLM limit: {e}")
                    if alert:
                        alert("creator_quota", "SLC automation: NotebookLM video limit reached",
                              f"NotebookLM won't make more videos right now:\n\n{e}\n\n"
                              "The creator waits 3 hours and tries again. Nothing is lost.")
                    report["paused"] = True
                    return report
                except (CreatorError, OSError) as e:
                    _fail(store, unit, i, str(e))
                finally:
                    try:
                        page.keyboard.press("Escape")
                    except Exception:
                        pass
        return report
    except NotebookUnavailable as e:
        if alert:
            alert("creator_notebook", "SLC automation: can't open the PCS notebook", str(e))
        report["error"] = str(e)
        return report
    finally:
        shutil.rmtree(work, ignore_errors=True)
        try:
            page.close()
        except Exception:
            pass

"""
Unit podcasts and study guides, browser side (runs inside the download task,
after the chapter-video creator, with the same signed-in browser).

Podcast (PUNIT# items):
  queued -> every chapter PDF of the unit is a source in the PCS notebook (uploaded
            if missing) -> Audio Overview: Deep dive (length Default), ALL the unit's chapter PDFs and
            only them, the team's prompt -> "audio"
  audio  -> when NotebookLM has made it: check its sources, rename it, download it,
            make the podcast video with the Podcast Creator's own code (thumbnail
            with the course name - no awarding body - and "Unit N - <name>" from
            WordPress) -> mp4 in S3 -> "upload" (the API puts it in Drive)

Study guide (SUNIT# items):
  queued -> the unit's chapter PDFs + the cover photo of one of them -> the StudyGen
            website -> DOCX -> footers laid out like the reference -> PDF by Word
            (through the API) -> footers checked on every page -> PDF in S3 ->
            "upload"

A problem with one unit never stops the run; each unit gets MAX_ATTEMPTS tries.
"""
from __future__ import annotations

import hashlib
import json
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
from playwright.sync_api import Error as BrowserError

import creator
import extras_rules as er
import studyguide as sg
from creator import CreatorError, NotebookUnavailable, QuotaReached
from studyguide import StudyGuideError

log = logging.getLogger("extras")

MAX_AUDIO_GENERATING = int(os.environ.get("EXTRAS_MAX_AUDIO", "2"))
AUDIO_LIMIT_SECONDS = int(os.environ.get("EXTRAS_AUDIO_LIMIT", str(60 * 60)))
MAX_FOOTER_SHRINKS = 3
S3_PREFIX = "automation/extras/"


def _now() -> Decimal:
    return Decimal(str(round(time.time(), 3)))


class Store:
    def __init__(self, table):
        self.table = table

    def units(self, kind: er.Kind) -> list[dict]:
        pks, kwargs = [], {}
        while True:
            resp = self.table.query(IndexName="active", KeyConditionExpression=Key("active").eq(kind.active), **kwargs)
            pks += [i["pk"] for i in resp.get("Items", [])]
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        out = []
        for pk in pks:
            it = self.table.get_item(Key={"pk": pk}, ConsistentRead=True).get("Item")
            if it and it.get("state") in er.RUNNER_STATES:
                out.append(it)
        return sorted(out, key=lambda u: float(u.get("created_at", 0)))

    def save(self, item: dict, **fields) -> None:
        """Update the unit while it is still the runner's (never after the API took it over)."""
        names = {f"#{k}": k for k in fields}
        values = {f":{k}": v for k, v in fields.items()}
        values.update({":now": _now(), ":s0": er.RUNNER_STATES[0], ":s1": er.RUNNER_STATES[1],
                       ":s2": er.RUNNER_STATES[2]})
        sets = ", ".join(f"#{k} = :{k}" for k in fields) + ", updated_at = :now"
        self.table.update_item(Key={"pk": item["pk"]}, UpdateExpression="SET " + sets,
                               ConditionExpression="#state IN (:s0, :s1, :s2)",
                               ExpressionAttributeNames={**names, "#state": "state"},
                               ExpressionAttributeValues=values)
        item.update(fields)

    def pause(self, reason: str) -> None:
        self.table.put_item(Item={"pk": "AUTOCFG#creator_pause", "kind": "autocfg", "reason": reason[:300],
                                  "until": Decimal(str(time.time() + 3 * 3600)), "updated_at": _now()})

    def paused(self) -> bool:
        p = self.table.get_item(Key={"pk": "AUTOCFG#creator_pause"}).get("Item")
        return bool(p and float(p.get("until", 0)) > time.time())


def _fail(store: Store, item: dict, message: str, final: bool = False) -> None:
    attempts = int(item.get("attempts", 0))
    state = "failed" if final or attempts >= er.MAX_ATTEMPTS else "queued"
    log.warning("%s U%s %s: %s (%s)", item.get("pk", "")[:5], item.get("unit"), item.get("course_raw", "")[:50],
                message, state)
    fields = {"state": state, "error": message[:400]}
    if state == "failed":
        fields["failed_at"] = _now()
    store.save(item, **fields)


def _download_pdf(url: str, dest: Path) -> Path:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
    if not data.startswith(b"%PDF"):
        raise CreatorError(f"The chapter PDF link did not return a PDF: {url[-80:]}")
    dest.write_bytes(data)
    return dest


def _unit_pdfs(item: dict, work: Path) -> list[tuple[dict, Path]]:
    """Download the unit's chapter PDFs and check each is really this course's unit."""
    course = er.course_title(item["course_raw"])
    out = []
    for ch in sorted(item["chapters"], key=lambda c: int(c["n"])):
        p = _download_pdf(ch["pdf_url"], work / f"u{item['unit']}c{ch['n']}.pdf")
        problem = creator.pdf_matches(p, course, int(item["unit"]))
        if problem:
            raise StudyGuideError(f"Chapter {ch['n']}: {problem}")
        out.append((ch, p))
    return out


# ── podcasts ─────────────────────────────────────────────────────────────

def _podcast_sources(page, item: dict, work: Path) -> list[str]:
    """Every chapter PDF of the unit as a source in the notebook (named like the
    chapter-video sources, so ones already there are reused); returns their names."""
    course = er.course_title(item["course_raw"])
    names = []
    present = set(creator.source_names(page))
    for ch, pdf in _unit_pdfs(item, work):
        digest = hashlib.sha1(pdf.read_bytes()).hexdigest()
        name = creator.source_name(int(item["unit"]), int(ch["n"]), course, digest)
        if name not in present:
            named = work / name
            shutil.copyfile(pdf, named)
            creator.upload_source(page, named)
            present.add(name)
        names.append(name)
    return names


def _start_podcast(store: Store, page, item: dict, work: Path) -> bool:
    sources = _podcast_sources(page, item, work)
    title = er.podcast_audio_title(int(item["unit"]), item["course_raw"])
    # Already made from exactly these sources (a retry, or a test)? Reuse it.
    for a in creator.studio_items(page):
        if (a.title == title and a.ready_audio and creator.audio_format(a) == creator.AUDIO_FORMAT
                and sorted(creator.item_sources(page, a.id)) == sorted(sources)):
            store.save(item, state="audio", artifact_id=a.id, sources=sources, started_at=_now(),
                       attempts=int(item.get("attempts", 0)) + 1)
            return False
    store.save(item, attempts=int(item.get("attempts", 0)) + 1, sources=sources)
    aid = creator.generate_audio(page, sources, er.podcast_prompt(int(item["unit"]), item["course_raw"]))
    store.save(item, state="audio", artifact_id=aid, started_at=_now())
    log.info("Started podcast audio U%s %s (%s)", item["unit"], item["course_raw"][:60], aid)
    return True


def _render_podcast(item: dict, audio: Path, work: Path) -> Path:
    import podcast_creator  # the Podcast Creator tool's own rendering code (unchanged copy)
    thumb = work / "thumbnail.jpg"
    out = work / er.podcast_file_name(item["unit_title"])
    podcast_creator.create_thumbnail(er.course_title(item["course_raw"]), item["unit_title"], str(thumb))
    podcast_creator.create_video(str(thumb), str(audio), str(out))
    if out.stat().st_size < 100_000:
        raise CreatorError("The rendered podcast video is too small.")
    # The tool's 1-frame-per-second still video can run ~20 s past the audio;
    # cut it to the audio's exact length (stream copy: nothing is re-encoded).
    sound = podcast_creator._ffprobe_duration_seconds(str(audio))
    if podcast_creator._ffprobe_duration_seconds(str(out)) > sound + 1:
        import subprocess
        cut = work / ("cut-" + out.name)
        subprocess.run(["ffmpeg", "-y", "-nostdin", "-v", "error", "-i", str(out), "-t", f"{sound:.3f}",
                        "-c", "copy", "-movflags", "+faststart", str(cut)], check=True, timeout=300,
                       stdin=subprocess.DEVNULL, capture_output=True)
        cut.replace(out)
    if abs(podcast_creator._ffprobe_duration_seconds(str(out)) - sound) > 2:
        raise CreatorError("The podcast video's length doesn't match the audio.")
    return out


def _collect_podcast(store: Store, page, item: dict, items: dict, work: Path, s3, bucket: str) -> bool:
    a = items.get(item.get("artifact_id", ""))
    if a is None:
        _fail(store, item, "NotebookLM could not make the podcast audio (generation failed).")
        return False
    if a.generating:
        if time.time() - float(item.get("started_at", 0)) > AUDIO_LIMIT_SECONDS:
            _fail(store, item, f"NotebookLM was still making the audio after {AUDIO_LIMIT_SECONDS // 60} minutes.")
        return False
    if a.failed or not a.ready_audio:
        _fail(store, item, f"NotebookLM could not make the podcast audio ({a.title} {a.details}).")
        return False
    fmt = creator.audio_format(a)
    if fmt and fmt != creator.AUDIO_FORMAT:
        _fail(store, item, f"The audio was made as '{fmt}', not '{creator.AUDIO_FORMAT}'.")
        return False
    used = creator.item_sources(page, a.id)
    if sorted(used) != sorted(item.get("sources", [])):
        _fail(store, item, f"The audio was made from {used}, not exactly the unit's chapter PDFs.")
        return False
    title = er.podcast_audio_title(int(item["unit"]), item["course_raw"])
    if a.title != title:
        creator.rename_item(page, a.id, title)
    audio = creator.download_item(page, a.id, work)
    video = _render_podcast(item, audio, work)
    key = f"{S3_PREFIX}{item['pk'].replace('#', '-')}.mp4"
    s3.upload_file(str(video), bucket, key, ExtraArgs={"ServerSideEncryption": "AES256", "ContentType": "video/mp4"})
    store.save(item, state=er.UPLOAD_STATE, s3_key=key, file_name=video.name, ready_at=_now(), error="")
    log.info("Podcast ready U%s %s -> %s", item["unit"], item["course_raw"][:60], video.name)
    return True


def run_podcasts(store: Store, page, notebook_url: str, deadline: float, work: Path, s3, bucket: str,
                 alert=None) -> dict:
    report = {"podcasts_ready": 0, "podcasts_started": 0}
    units = store.units(er.PODCAST)
    if not units:
        return report
    creator.open_notebook(page, notebook_url)
    items = {a.id: a for a in creator.studio_items(page)}
    for item in [u for u in units if u.get("state") == "audio"]:
        if time.time() > deadline:
            return report
        try:
            if _collect_podcast(store, page, item, items, work, s3, bucket):
                report["podcasts_ready"] += 1
        except (CreatorError, StudyGuideError, BrowserError, OSError, RuntimeError) as e:
            _fail(store, item, str(e))
        finally:
            _escape(page)
    if store.paused():
        return report
    generating = sum(1 for u in units if u.get("state") == "audio")
    for item in [u for u in units if u.get("state") == "queued"]:
        if generating >= MAX_AUDIO_GENERATING or time.time() > deadline:
            break
        try:
            if _start_podcast(store, page, item, work):
                report["podcasts_started"] += 1
            generating += 1
        except QuotaReached as e:
            store.pause(f"NotebookLM limit: {e}")
            if alert:
                alert("creator_quota", "SLC automation: NotebookLM limit reached",
                      f"NotebookLM won't make more right now:\n\n{e}\n\nThe bot waits 3 hours and tries again.")
            break
        except StudyGuideError as e:   # wrong PDF in WordPress: a person must fix the page
            _fail(store, item, f"Not made: {e}", final=True)
        except (CreatorError, BrowserError, OSError) as e:
            _fail(store, item, str(e))
        finally:
            _escape(page)
    return report


def _escape(page) -> None:
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass


# ── study guides ─────────────────────────────────────────────────────────

def _make_study_guide(store: Store, page, item: dict, work: Path, s3, bucket: str, to_pdf) -> None:
    store.save(item, state="making", attempts=int(item.get("attempts", 0)) + 1, started_at=_now())
    unit = int(item["unit"])
    course = er.course_title(item["course_raw"])
    pdfs = [p for _, p in _unit_pdfs(item, work)]
    cover = sg.cover_image(pdfs, work / "cover.jpg")
    raw_docx = sg.generate_docx(page, pdfs, course, unit, cover, work)
    problems = []
    for shrink in range(MAX_FOOTER_SHRINKS + 1):
        fixed = work / f"{sg.file_name(unit)}-fixed.docx"
        sg.fix_footers(raw_docx, fixed, course, shrink_steps=shrink)
        pdf = work / er.studyguide_file_name(unit)
        pdf.write_bytes(to_pdf(fixed.read_bytes(), sg.file_name(unit)))
        problems = sg.check_footers(pdf, course)
        if not problems:
            break
        if not all("two lines" in p for p in problems):
            break   # something else is wrong: shrinking won't help
    if problems:
        raise StudyGuideError("The footers are not right: " + "; ".join(problems[:5]))
    key = f"{S3_PREFIX}{item['pk'].replace('#', '-')}.pdf"
    s3.upload_file(str(pdf), bucket, key, ExtraArgs={"ServerSideEncryption": "AES256", "ContentType": "application/pdf"})
    store.save(item, state=er.UPLOAD_STATE, s3_key=key, file_name=pdf.name, ready_at=_now(), error="")
    log.info("Study guide ready U%s %s", unit, item["course_raw"][:60])


def run_study_guides(store: Store, page, deadline: float, work: Path, s3, bucket: str, to_pdf) -> dict:
    report = {"study_guides_ready": 0}
    for item in store.units(er.STUDYGUIDE):
        if time.time() > deadline:
            break
        if item.get("state") == "making":
            # A previous run stopped half-way: count it as a try and start again.
            _fail(store, item, "The previous run stopped while making this study guide.")
            if item.get("state") != "queued":
                continue
        sub = Path(tempfile.mkdtemp(dir=work))
        try:
            _make_study_guide(store, page, item, sub, s3, bucket, to_pdf)
            report["study_guides_ready"] += 1
        except StudyGuideError as e:
            final = "PDF linked in WordPress" in str(e)
            _fail(store, item, str(e), final=final)
        except (CreatorError, BrowserError, OSError, RuntimeError, ValueError) as e:
            _fail(store, item, f"{e.__class__.__name__}: {e}")
        finally:
            shutil.rmtree(sub, ignore_errors=True)
    return report


# ── entry point ──────────────────────────────────────────────────────────

def has_work(table) -> bool:
    store = Store(table)
    return any(store.units(k) for k in er.KINDS)


def run(context, table, notebook_url: str, deadline: float, s3, bucket: str, to_pdf, alert=None) -> dict:
    """One pass: podcasts (NotebookLM) first, then study guides (StudyGen)."""
    store = Store(table)
    report = {}
    work = Path(tempfile.mkdtemp(prefix="extras-"))
    page = context.new_page()
    try:
        if notebook_url:
            try:
                report.update(run_podcasts(store, page, notebook_url, deadline, work, s3, bucket, alert))
            except NotebookUnavailable as e:
                report["podcast_error"] = str(e)
        report.update(run_study_guides(store, page, deadline, work, s3, bucket, to_pdf))
        return report
    finally:
        shutil.rmtree(work, ignore_errors=True)
        try:
            page.close()
        except Exception:
            pass

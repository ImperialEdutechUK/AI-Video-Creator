"""
Download one chapter video from NotebookLM with Playwright (headless Chromium),
signed in as the content team's Google account through a saved session.

What it does, the same way a person does it:
  1. Open the link from the sheet. Short links (notebooklm.link.google/...) and
     links that point straight at a video both end up on a notebook URL; only
     the notebook id is kept.
  2. Open the notebook itself (direct video links have no Download option).
  3. In the Studio panel, find the item whose title is the row's unit and
     chapter, e.g. "U22C6" for Unit 22 / Chapter 6 (the Ctrl+F step).
  4. Open its menu -> Download, and keep the file only if it is a video.

Exactly one video must match. None or several -> the row is marked NO and a
person decides. Nothing in NotebookLM is ever created, changed or deleted.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, Page
from playwright.sync_api import TimeoutError as PWTimeout

import rules

log = logging.getLogger(__name__)

# NotebookLM is also served as "Gemini Notebook" on notebook.google.com; both hosts work.
APP_HOSTS = ("notebooklm.google.com", "notebook.google.com")
HOME_URL = "https://notebooklm.google.com/"
NOTEBOOK_URL_RE = re.compile(r"https://(notebook(?:lm)?\.google\.com)/notebook/([A-Za-z0-9_-]+)")
DOWNLOAD_TIMEOUT_MS = 4 * 60 * 1000   # a ~15 MB video normally takes seconds
MAX_CANDIDATES = 3


class NotebookError(Exception):
    """A problem with this row's notebook/video (the row gets "NO")."""
    retry = True  # worth trying again on a later run (e.g. the video may not exist yet)


class SignInRequired(Exception):
    """The saved Google session no longer works - stop and ask for a sign-in."""


class NoAccess(NotebookError):
    retry = False  # stays that way until the notebook is shared with the account


NO_ACCESS_MESSAGE = ("The content team Google account has no access to this notebook (Google shows "
                     "'You need access'). Share the notebook with that account, then clear column I "
                     "to let the bot try again - or do this row by hand.")


def _no_access_page(page: Page, wait_ms: int = 8_000) -> bool:
    """Google's "You need access" page (notebook.google.com/accessrequest/...).
    A shared single-video link first opens /notebook/<id>/artifact/<id> and is
    then redirected there when the account isn't a member of the notebook."""
    deadline = time.time() + wait_ms / 1000
    while True:
        if "/accessrequest/" in page.url:
            return True
        try:
            text = page.inner_text("body", timeout=1_000)
            if re.search(r"You need access|do not have access to view this notebook", text):
                return True
        except Exception:
            pass
        if time.time() >= deadline:
            return False
        page.wait_for_timeout(1_000)


class VideoNotFound(NotebookError):
    pass


class AmbiguousVideo(NotebookError):
    retry = False  # the same videos will still be there - a person must choose


class DownloadFailed(NotebookError):
    pass


@dataclass
class DownloadResult:
    path: Path
    title: str
    notebook_id: str


def title_pattern(unit: str, chapter: str) -> str:
    """JS/Python regex for one unit+chapter title: U22C6, U22 C6, Unit 22 Chapter 6, U22C06..."""
    return (rf"^\s*U(?:nit)?[\s._-]*0*{int(unit)}[\s._-]*C(?:h(?:apter)?)?[\s._-]*0*{int(chapter)}(?!\d)")


ANY_TITLE = r"^\s*U(?:nit)?[\s._-]*\d{1,4}[\s._-]*C(?:h(?:apter)?)?[\s._-]*\d{1,4}(?!\d)"


# ── session ──────────────────────────────────────────────────────────────

def _on_app(page: Page) -> bool:
    return urlparse(page.url).netloc in APP_HOSTS


def check_signed_in(page: Page) -> bool:
    """Signed-out visitors are sent to accounts.google.com or the
    notebooklm.google marketing site; signed-in ones stay on the app."""
    # Both addresses: notebooklm.google.com can still show the app while the
    # Google session itself is signed out (seen in production), and
    # notebook.google.com - where the sheet links go - then asks to sign in.
    for url in (HOME_URL, "https://notebook.google.com/"):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_load_state("networkidle", timeout=20_000)
        except PWTimeout:
            pass
        if not _on_app(page) or "accounts.google.com" in page.url:
            return False
    return True


# ── notebook ─────────────────────────────────────────────────────────────

def resolve_notebook_id(page: Page, link: str) -> tuple[str, str]:
    """(host, notebook id) the link leads to."""
    page.goto(link, wait_until="domcontentloaded", timeout=60_000)
    try:
        page.wait_for_url(NOTEBOOK_URL_RE, timeout=30_000)
    except PWTimeout:
        pass
    if "accounts.google.com" in page.url:
        raise SignInRequired("Google asked to sign in while opening a notebook link")
    if _no_access_page(page, wait_ms=8_000 if "/artifact/" in page.url else 0):
        raise NoAccess(NO_ACCESS_MESSAGE)
    m = NOTEBOOK_URL_RE.search(page.url)
    if not m:
        body = ""
        try:
            body = page.inner_text("body", timeout=5_000)[:300]
        except Exception:
            pass
        if re.search(r"(?i)access|permission|not found|doesn.t exist", body):
            raise NoAccess("The notebook link opened, but the account has no access to it (or it was deleted).")
        raise NoAccess(f"The link did not open a NotebookLM notebook (ended at {urlparse(page.url).netloc}).")
    return m.group(1), m.group(2)


_SCROLL_JS = """
() => {
  let moved = 0;
  for (const el of document.querySelectorAll('body *')) {
    const s = getComputedStyle(el);
    if ((s.overflowY === 'auto' || s.overflowY === 'scroll') && el.scrollHeight > el.clientHeight + 20) {
      const before = el.scrollTop;
      el.scrollTop = el.scrollHeight;
      if (el.scrollTop !== before) moved++;
    }
  }
  return moved;
}
"""

# Marks Studio rows whose title matches, and returns a short description of
# each. A "row" is the smallest ancestor of the title that has a button and a
# media duration (e.g. "7:20 · Explainer"), and holds exactly one U..C.. title.
_FIND_ROWS_JS = """
(args) => {
  const want = new RegExp(args.pattern, 'i');
  const anyTitle = new RegExp(args.any, 'i');
  const anyTitleG = new RegExp(args.any.replace('^\\\\s*', '(?:^|\\\\n)\\\\s*'), 'gi');
  const dur = /(^|[^\\d:])\\d{1,2}:\\d{2}(:\\d{2})?([^\\d:]|$)/;
  document.querySelectorAll('[data-slc-row]').forEach(e => e.removeAttribute('data-slc-row'));
  const ownText = el => Array.from(el.childNodes).filter(n => n.nodeType === 3)
      .map(n => n.textContent).join(' ').trim();
  const rows = [], titles = [];
  for (const el of document.querySelectorAll('body *')) {
    let t = ownText(el);
    if (!t && el.children.length === 0) t = (el.textContent || '').trim();
    if (!t || t.length > 200 || !want.test(t)) continue;
    let node = el, found = null;
    for (let i = 0; i < 10 && node && node !== document.body; i++, node = node.parentElement) {
      const txt = node.innerText || '';
      if (txt.length > 600) break;
      if (dur.test(txt) && node.querySelector('button, [role=button]')) { found = node; break; }
    }
    if (!found || rows.includes(found)) continue;
    const titleCount = ((found.innerText || '').match(anyTitleG) || []).length;
    if (titleCount !== 1) continue;
    rows.push(found);
    titles.push(t);
  }
  return rows.map((r, i) => {
    r.setAttribute('data-slc-row', String(i));
    const icons = Array.from(r.querySelectorAll('mat-icon, .material-icons, .material-symbols-outlined, [data-mat-icon-name]'))
      .map(x => (x.getAttribute('data-mat-icon-name') || x.textContent || '').trim()).filter(Boolean);
    return { index: i, title: titles[i], text:(r.innerText || '').replace(/\\s+/g, ' ').slice(0, 200), icons: icons.slice(0, 8) };
  });
}
"""


def _studio_ready(page: Page) -> bool:
    try:
        return page.evaluate(
            "(a) => { const re = new RegExp(a, 'i'); const t = document.body.innerText || '';"
            " return /Studio/.test(t) && (t.split('\\n').some(l => re.test(l)) || /Video Overview/.test(t)); }",
            ANY_TITLE,
        )
    except Exception:
        return False


def find_rows(page: Page, unit: str, chapter: str, wait_s: int = 45) -> list[dict]:
    deadline = time.time() + wait_s
    while time.time() < deadline and not _studio_ready(page):
        page.wait_for_timeout(1_000)
    page.wait_for_timeout(2_000)
    for _ in range(4):  # load every Studio item if the list is long
        if not page.evaluate(_SCROLL_JS):
            break
        page.wait_for_timeout(1_200)
    rows = page.evaluate(_FIND_ROWS_JS, {"pattern": title_pattern(unit, chapter), "any": ANY_TITLE})
    # Video-looking rows first, so an audio overview with the same title is tried last.
    rows.sort(key=lambda r: 0 if any(re.search(r"(?i)video|movie|subscriptions|smart_display|slideshow", i)
                                     for i in r.get("icons", [])) else 1)
    return rows


def _more_button(row):
    buttons = row.locator("button, [role=button]")
    count = buttons.count()
    best = None
    for i in range(count):
        b = buttons.nth(i)
        label = " ".join(filter(None, [
            b.get_attribute("aria-label") or "", b.get_attribute("mattooltip") or "",
            b.get_attribute("title") or "", (b.inner_text(timeout=2_000) or "")[:40],
        ]))
        if re.search(r"(?i)more|option|menu|actions|more_vert|more_horiz", label):
            return b
        best = b
    return best  # the overflow menu is the last button in a Studio row


def _menu_download_item(page: Page):
    for loc in (
        page.get_by_role("menuitem", name=re.compile(r"download", re.I)),
        page.locator("[role=menu] >> text=/download/i"),
        page.locator(".mat-mdc-menu-panel >> text=/download/i"),
    ):
        try:
            loc.first.wait_for(state="visible", timeout=4_000)
            return loc.first
        except PWTimeout:
            continue
    return None


def _save_download(page: Page, trigger, dest_dir: Path) -> Path:
    """Click `trigger` and save whatever it downloads. Falls back to a new tab
    that opens the media file directly."""
    context = page.context
    pages_before = set(context.pages)
    try:
        with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as info:
            trigger.click()
        dl = info.value
        if dl.failure():
            raise DownloadFailed(f"NotebookLM download failed: {dl.failure()}")
        name = re.sub(r"[^\w.\- ]+", "_", dl.suggested_filename or "video.mp4")[:120]
        path = dest_dir / name
        dl.save_as(str(path))
        return path
    except PWTimeout:
        new_pages = [p for p in context.pages if p not in pages_before]
        for p in new_pages:
            url = p.url
            if url.startswith("http"):
                resp = context.request.get(url, timeout=DOWNLOAD_TIMEOUT_MS)
                p.close()
                if resp.ok:
                    path = dest_dir / "video.mp4"
                    path.write_bytes(resp.body())
                    return path
        raise DownloadFailed("Clicking Download did not finish a download within 4 minutes.")


def download_row(page: Page, index: int, dest_dir: Path) -> Path:
    row = page.locator(f'[data-slc-row="{index}"]')
    row.scroll_into_view_if_needed(timeout=10_000)
    row.hover()
    page.wait_for_timeout(400)
    btn = _more_button(row)
    item = None
    if btn is not None:
        btn.click()
        item = _menu_download_item(page)
    if item is None:
        # Some versions only offer Download inside the opened item.
        page.keyboard.press("Escape")
        row.click()
        page.wait_for_timeout(2_000)
        direct = page.get_by_role("button", name=re.compile(r"download", re.I))
        if direct.count():
            return _save_download(page, direct.first, dest_dir)
        more = page.get_by_role("button", name=re.compile(r"more|option", re.I))
        if more.count():
            more.last.click()
            item = _menu_download_item(page)
    if item is None:
        page.keyboard.press("Escape")
        raise DownloadFailed("No Download option was found for this Studio item.")
    path = _save_download(page, item, dest_dir)
    page.keyboard.press("Escape")
    return path


def media_kind(path: Path) -> str:
    """'video', 'audio' or 'other' from the file's first bytes."""
    try:
        with open(path, "rb") as f:
            head = f.read(64)
    except OSError:
        return "other"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"M4A ", b"M4B ", b"M4P "):
            return "audio"
        return "video" if path.stat().st_size > 100_000 else "other"
    if head[:4] == b"\x1aE\xdf\xa3":  # WebM / Matroska
        return "video" if path.suffix.lower() in (".webm", ".mkv", ".mp4") else "audio"
    if head[:3] == b"ID3" or head[:4] in (b"RIFF", b"OggS") or head[:2] == b"\xff\xfb":
        return "audio"
    return "other"


def download_video(context: BrowserContext, link: str, unit: str, chapter: str, dest_dir: Path,
                   course: str = "", awarding_body: str = "") -> DownloadResult:
    dest_dir.mkdir(parents=True, exist_ok=True)
    page = context.new_page()
    try:
        host, notebook_id = resolve_notebook_id(page, link)
        page.goto(f"https://{host}/notebook/{notebook_id}", wait_until="domcontentloaded", timeout=60_000)
        if "accounts.google.com" in page.url:
            raise SignInRequired("Google asked to sign in while opening a notebook")
        if _no_access_page(page, wait_ms=3_000):
            raise NoAccess(NO_ACCESS_MESSAGE)
        rows = find_rows(page, unit, chapter)
        label = f"U{int(unit)}C{int(chapter)}"
        if not rows:
            raise VideoNotFound(f"No Studio item titled {label} was found in the notebook.")
        log.info("Notebook %s: %d candidate(s) for %s: %s", notebook_id, len(rows), label,
                 [r.get("title") or r["text"][:60] for r in rows])
        if course:
            # Shared notebooks hold several courses: "U2C4 - Level 3 Certificate in ..."
            keep, note = rules.pick_by_course([r.get("title") or "" for r in rows], course, awarding_body)
            if not keep:
                raise VideoNotFound(f"No {label} video for this course was found ({note}).")
            if len(keep) < len(rows):
                log.info("Kept %d of %d by course name: %s", len(keep), len(rows),
                         [rows[i].get("title") for i in keep])
            rows = [rows[i] for i in keep]
        videos, problems = [], []
        for r in rows[:MAX_CANDIDATES]:
            sub = dest_dir / f"cand{r['index']}"
            sub.mkdir(exist_ok=True)
            try:
                # Re-mark rows: the list can re-render after a menu closes.
                find_rows(page, unit, chapter, wait_s=5)
                path = download_row(page, r["index"], sub)
            except DownloadFailed as e:
                problems.append(str(e))
                continue
            kind = media_kind(path)
            if kind == "video":
                videos.append((r, path))
            else:
                path.unlink(missing_ok=True)
        if not videos:
            detail = f" ({problems[0]})" if problems else " (only non-video items have that title)"
            raise VideoNotFound(f"Could not download a video titled {label}{detail}")
        if len(videos) > 1:
            for _, p in videos:
                p.unlink(missing_ok=True)
            raise AmbiguousVideo(f"{len(videos)} different videos are titled {label} - a person needs to choose.")
        row, path = videos[0]
        return DownloadResult(path=path, title=(row.get("title") or row["text"])[:120], notebook_id=notebook_id)
    except SignInRequired:
        raise
    except NotebookError:
        raise
    except PWTimeout as e:
        raise DownloadFailed(f"NotebookLM did not respond in time: {str(e).splitlines()[0][:150]}") from None
    finally:
        try:
            page.close()
        except Exception:
            pass

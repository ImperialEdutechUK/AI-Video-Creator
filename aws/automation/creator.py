"""
Create chapter videos in NotebookLM (the "PCS-Nandika" notebook):

  upload the chapter PDF as a source (named "U1C1 - <course>.pdf")
  -> Studio > Video Overview > customise: Explainer, Classic, only that source,
     the team's focus prompt -> Generate now
  -> when it's ready: rename it "U1C1 - <course>" and copy its share link.

Only touches items the bot made itself (matched by their source name). Never
deletes anything.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PWTimeout

log = logging.getLogger("creator")

VIDEO_PROMPT = (
    "Clean academic slide presentation style suitable for UK vocational and professional online courses that "
    "utilises a modern, vibrant, and visually engaging aesthetic. Use high-quality stock photos, professional "
    "icons, and relevant graphics on every slide to create a polished, colourful appearance that resembles a "
    "high-end digital textbook. Use Montserrat for headings and Open Sans for body text (or Calibri if "
    "unavailable), ensuring high contrast and clear visibility. Focus strictly on the uploaded chapter document "
    "to explain all key concepts, definitions, principles, processes, models, and legislation through a detailed "
    "narration script, while the on-screen text remains strictly minimalist. Use keywords or anchor terms only, "
    "with a maximum of five words per bullet point and no more than five bullet points per slide to ensure the "
    "video remains visually clean. If a concept is complex, split the information across multiple slides rather "
    "than increasing the word count on a single page. The video narration must perform the heavy lifting by "
    "providing full professional explanations, while the slides serve as visual reinforcements that highlight "
    "essential terminology and assessment-relevant content using a subtle accent colour. Ensure the structure "
    "begins with a colourful overview of the chapter purpose, followed by a step-by-step logical progression of "
    "the main points, and concludes with a concise recap. You must use correct and complete British English "
    "spelling throughout, such as ‘organisation’ and ‘behaviour’, and you are strictly "
    "prohibited from using any abbreviated or shortened word forms. Always write words like ‘management’, "
    "‘department’, ‘introduction’, and ‘organisation’ in full, and rephrase any "
    "labels or bullet points that are too long rather than truncating them. Where appropriate, integrate simple "
    "labelled diagrams, flowcharts, arrows, or basic shapes to explain relationships or processes visually "
    "alongside the imagery. Maintain a professional and supportive tone tailored for adult learners, ensuring the "
    "video clarifies the written material without adding external information or creative examples not found in "
    "the source document. Please avoid mentioning the course name in either the video visuals or the generated "
    "audio."
)

FORMAT = "Explainer"
VISUAL_STYLE = "Classic"


class CreatorError(Exception):
    """A problem creating this chapter's video (retried on a later run)."""


class QuotaReached(Exception):
    """NotebookLM says the account can't make more videos right now."""


class NotebookUnavailable(Exception):
    """The PCS notebook can't be opened (moved, deleted or no access)."""


@dataclass
class StudioItem:
    index: int
    title: str
    meta: str          # e.g. "7:54 · Explainer · 1 source · 1d ago" (empty while generating)
    is_video: bool
    generating: bool


_QUOTA_RE = re.compile(r"(?i)(daily limit|limit reached|reached your limit|reached the limit|try again tomorrow|"
                       r"out of .*(credits|usage)|usage limit)")


def safe_name(text: str, limit: int = 150) -> str:
    text = re.sub(r'[\\/:*?"<>|‘’“”]+', " ", text)
    return " ".join(text.split())[:limit].strip(" .")


def video_title(unit: int, chapter: int, course: str) -> str:
    return f"U{unit}C{chapter} - {course.strip()}"


def source_name(unit: int, chapter: int, course: str, pdf_hash: str = "") -> str:
    """Source file name in NotebookLM. The short content hash keeps a corrected
    PDF from ever reusing an old, wrong upload with the same chapter name."""
    tag = f" [{pdf_hash[:8]}]" if pdf_hash else ""
    return safe_name(video_title(unit, chapter, course)) + tag + ".pdf"


def pdf_matches(pdf: Path, course: str, unit: int) -> str | None:
    """None if the PDF's first pages name this course and unit, else the problem.
    (Catches a chapter page in WordPress that links another course's PDF.)"""
    from pypdf import PdfReader
    try:
        reader = PdfReader(str(pdf))
        text = " ".join((reader.pages[i].extract_text() or "") for i in range(min(3, len(reader.pages))))
    except Exception as e:
        return f"the PDF could not be read ({e.__class__.__name__})"
    flat = " ".join(re.sub(r"[^a-z0-9 ]", " ", text.lower()).split())
    if not flat:
        return None  # scanned/image-only PDF: nothing to compare, allow
    words = [w for w in re.sub(r"[^a-z0-9 ]", " ", course.lower()).split()
             if w not in {"in", "and", "the", "for", "of", "to", "rqf", "a", "an", "with"}]
    found = sum(1 for w in words if re.search(rf"\b{re.escape(w)}\b", flat))
    level = re.search(r"(?i)\blevel\s*0*(\d+)", course)
    if level and not re.search(rf"\blevel\s*0*{level.group(1)}\b", flat):
        head = " ".join(text.split())[:160]
        return f"the PDF linked in WordPress is not for a Level {level.group(1)} course (it starts: \"{head}\")"
    if words and found / len(words) < 0.75:
        head = " ".join(text.split())[:160]
        return f"the PDF linked in WordPress seems to be for another course (it starts: \"{head}\")"
    # Many chapter PDFs don't say "Unit N" at all; only reject one that names a
    # different unit.
    units = {int(u) for u in re.findall(r"\bunit\s*0*(\d{1,3})\b", flat)}
    if units and unit not in units:
        head = " ".join(text.split())[:160]
        return (f"the PDF linked in WordPress is for Unit {', '.join(map(str, sorted(units)))}, not Unit {unit} "
                f"(it starts: \"{head}\")")
    return None


# ── notebook & sources ───────────────────────────────────────────────────

def open_notebook(page: Page, url: str) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=90_000)
    if "accounts.google.com" in page.url:
        from notebooklm import SignInRequired
        raise SignInRequired("Google asked to sign in while opening the PCS notebook")
    if "/accessrequest/" in page.url:
        raise NotebookUnavailable("The content team account has no access to the PCS notebook.")
    try:
        page.get_by_role("button", name="Add source").wait_for(timeout=60_000)
    except PWTimeout:
        raise NotebookUnavailable(f"The PCS notebook did not open ({page.url[:100]}).") from None
    page.wait_for_timeout(3_000)
    _dismiss_banners(page)


def _dismiss_banners(page: Page) -> None:
    for b in page.get_by_role("button", name="Dismiss banner").all():
        try:
            if b.is_visible():
                b.click(timeout=2_000)
        except Exception:
            pass


def source_names(page: Page) -> list[str]:
    """Names of the notebook's sources (from their 'Select <name>' checkboxes)."""
    labels = page.evaluate("""() => Array.from(document.querySelectorAll('input[type=checkbox][aria-label^="Select "]'))
        .map(e => e.getAttribute('aria-label').slice(7)).filter(n => n !== 'all sources')""")
    return labels


def upload_source(page: Page, pdf: Path, timeout_s: int = 300) -> None:
    """Upload a PDF as a source and wait until NotebookLM has read it."""
    if pdf.name in source_names(page):
        return
    page.get_by_role("button", name="Add source").click()
    dialog = page.locator("mat-dialog-container").last
    dialog.wait_for(timeout=20_000)
    with page.expect_file_chooser(timeout=20_000) as fc:
        dialog.get_by_role("button", name=re.compile("Upload files")).click()
    fc.value.set_files(str(pdf))
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        page.wait_for_timeout(3_000)
        _check_quota(page)
        box = page.locator(f'input[type=checkbox][aria-label="Select {pdf.name}"]')
        if box.count() and box.first.is_enabled():
            page.wait_for_timeout(2_000)
            return
    raise CreatorError(f"NotebookLM did not finish reading {pdf.name} within {timeout_s // 60} minutes.")


# ── video overview ───────────────────────────────────────────────────────

def _check_quota(page: Page) -> None:
    try:
        texts = page.evaluate("""() => Array.from(document.querySelectorAll(
            'simple-snack-bar, .mat-mdc-snack-bar-label, [role=alert], [role=status], mat-dialog-container'))
            .map(e => e.innerText).join(' | ')""")
    except Exception:
        return
    if texts and _QUOTA_RE.search(texts):
        raise QuotaReached(" ".join(texts.split())[:300])


def start_video(page: Page, source: str, prompt: str = VIDEO_PROMPT, dry_run: bool = False,
                screenshot: str = "") -> None:
    """Open Video Overview > customise, set Explainer / Classic / only `source` /
    the focus prompt, and press Generate now (dry_run: close instead)."""
    card = page.locator("[role=button][aria-label='Video Overview']")
    card.hover()
    card.locator(".option-icon").evaluate("e => e.click()")  # the ">" on the card opens the settings
    page.locator("mat-dialog-container").filter(has_text="Customise Video Overview").wait_for(timeout=20_000)
    # (not filtered by its title: the Sources picker replaces the title while open)
    dialog = page.locator("mat-dialog-container").last
    dialog.get_by_text(FORMAT, exact=True).click()
    page.wait_for_timeout(1_000)
    dialog.get_by_text(VISUAL_STYLE, exact=True).click()
    page.wait_for_timeout(500)
    _select_only_source(page, dialog, source)
    dialog.get_by_role("button", name="Customised topic").click()
    box = dialog.get_by_role("textbox", name="What should the video focus on?")
    box.fill(prompt)
    _verify_settings(dialog, source)
    if screenshot:
        page.screenshot(path=screenshot)
    if dry_run:
        dialog.get_by_role("button", name="Close dialogue").click()
        return
    dialog.get_by_role("button", name="Generate now").click()
    try:
        dialog.wait_for(state="detached", timeout=30_000)
    except PWTimeout:
        _check_quota(page)
        raise CreatorError("The Video Overview settings window did not close after 'Generate now'.") from None
    page.wait_for_timeout(3_000)
    _check_quota(page)


def _select_only_source(page: Page, dialog, source: str) -> None:
    """The settings window's Sources picker: a list of options (one per source,
    aria-selected) with a "Select all" box. Leave only `source` selected."""
    dialog.get_by_role("button", name=re.compile(r"\d+\s+sources?")).first.click()
    options = dialog.locator("mat-list-option[role=option]")
    try:
        options.first.wait_for(timeout=15_000)
    except PWTimeout:
        raise CreatorError("The Video Overview source list did not open.") from None

    def state() -> list[tuple[str, bool]]:
        return options.evaluate_all("""els => els.map(e => [
            ((e.querySelector('.item-name') || e).innerText || '').trim(),
            e.getAttribute('aria-selected') === 'true'])""")

    current = state()
    names = [n for n, _ in current]
    if source not in names:
        raise CreatorError(f"The source {source} is not in the Video Overview source list.")
    if names.count(source) > 1:
        raise CreatorError(f"The notebook has {names.count(source)} sources named {source}.")
    # Untick "Select all" first when everything is ticked (one click instead of hundreds).
    if all(sel for _, sel in current) and len(current) > 1:
        dialog.locator("mat-checkbox.select-all-checkbox").click()
        page.wait_for_timeout(800)
        current = state()
    for i, (name, selected) in enumerate(current):
        if selected != (name == source):
            options.nth(i).click()
            page.wait_for_timeout(150)
    final = state()
    picked = [n for n, sel in final if sel]
    if picked != [source]:
        raise CreatorError(f"Could not select only {source} (selected: {picked[:5]}).")
    dialog.get_by_role("button", name="Confirm").click()
    page.wait_for_timeout(1_500)


def _verify_settings(dialog, source: str) -> None:
    text = dialog.inner_text()
    if not re.search(r"\b1 source\b", text):
        raise CreatorError(f"The Video Overview is not set to exactly one source ({source}).")
    checked = dialog.evaluate("""d => Array.from(d.querySelectorAll('input[type=radio]:checked'))
        .map(r => (r.closest('label, mat-radio-button, [role=radio], div') || r).innerText.trim().split('\\n')[0])""")
    log.debug("checked radios: %s", checked)


# ── studio list ──────────────────────────────────────────────────────────

_STUDIO_JS = r"""() => Array.from(document.querySelectorAll('.artifact-button-content')).map(n => {
  const labels = n.querySelector('.artifact-labels');
  const icon = n.querySelector('mat-icon.artifact-icon');
  const t = n.querySelector('.artifact-title');
  const d = n.querySelector('.artifact-details');
  return {
    id: labels ? (labels.id || '').replace('artifact-labels-', '') : '',
    title: t ? t.innerText.trim() : '',
    details: d ? d.innerText.replace(/\s+/g, ' ').trim() : '',
    icon: icon ? icon.innerText.trim() : '',
    kind: (n.querySelector('button.artifact-stretched-button') || {getAttribute: () => ''}).getAttribute('aria-description') || '',
  };
})"""


@dataclass
class Artifact:
    id: str
    title: str
    details: str
    icon: str
    kind: str

    @property
    def generating(self) -> bool:
        return self.icon == "progress_activity" or self.title.startswith("Generating")

    @property
    def failed(self) -> bool:
        return bool(re.search(r"(?i)\b(failed|couldn.t|could not|error)\b", self.title + " " + self.details))

    @property
    def ready_video(self) -> bool:
        return (self.kind == "Video Overview" and not self.generating and not self.failed
                and bool(re.search(r"\b\d{1,2}:\d{2}\b", self.details)))


def studio_items(page: Page) -> list[Artifact]:
    return [Artifact(**x) for x in page.evaluate(_STUDIO_JS) if x.get("id")]


def _item(page: Page, artifact_id: str):
    row = page.locator(f".artifact-button-content:has(#artifact-labels-{artifact_id})")
    if not row.count():
        raise CreatorError(f"Studio item {artifact_id} is no longer in the notebook.")
    return row.first


def _menu(page: Page, artifact_id: str, item: str) -> None:
    row = _item(page, artifact_id)
    row.scroll_into_view_if_needed(timeout=10_000)
    row.hover()
    row.get_by_role("button", name="More").click()
    page.get_by_role("menuitem", name=re.compile(item, re.I)).click(timeout=10_000)


def item_sources(page: Page, artifact_id: str) -> list[str]:
    """Source names the item was made from ("View prompt and sources")."""
    _menu(page, artifact_id, "View prompt and sources")
    dialog = page.locator("mat-dialog-container").filter(has_text="Prompt and sources").last
    dialog.wait_for(timeout=20_000)
    page.wait_for_timeout(1_000)
    names = dialog.evaluate("""d => Array.from(d.querySelectorAll('*')).filter(e => e.children.length === 0)
        .map(e => (e.innerText || '').trim()).filter(t => /\\.(pdf|docx?|txt|pptx?)$/i.test(t))""")
    page.keyboard.press("Escape")
    page.wait_for_timeout(500)
    return names


def rename_item(page: Page, artifact_id: str, title: str) -> None:
    _menu(page, artifact_id, "Rename")
    # The title turns into an input in place (no dialog).
    box = _item(page, artifact_id).locator("input.artifact-title-input")
    try:
        box.wait_for(timeout=10_000)
    except PWTimeout:
        raise CreatorError("The Rename box did not appear.") from None
    box.fill(title)
    box.press("Enter")
    page.wait_for_timeout(2_500)
    now = next((a for a in studio_items(page) if a.id == artifact_id), None)
    if not now or now.title != title:
        raise CreatorError(f"Renaming the video to {title!r} did not stick (shows {now.title if now else '?'!r}).")


def share_link(page: Page, artifact_id: str) -> str:
    """Direct link to the video (the item's 'Open in new tab' link):
    https://notebook.google.com/notebook/<notebook>/artifact/<video>. It opens the
    same place as the short 'Copy link' links (whose button stays disabled in an
    automated browser), for anyone with access to the notebook."""
    row = _item(page, artifact_id)
    row.scroll_into_view_if_needed(timeout=10_000)
    row.hover()
    row.get_by_role("button", name="More").click()
    item = page.get_by_role("menuitem", name=re.compile("Open in new tab"))
    href = (item.get_attribute("href", timeout=10_000) or "").strip()
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    if href.startswith("/"):
        href = "https://" + (re.match(r"https://([^/]+)", page.url).group(1)) + href
    if not re.match(rf"^https://notebook(lm)?\.google\.com/notebook/[\w-]+/artifact/{re.escape(artifact_id)}$", href):
        raise CreatorError(f"Could not read the video's link ({href[:100]!r}).")
    return href


def generate(page: Page, source: str, prompt: str = VIDEO_PROMPT) -> str:
    """Start a video for `source`; returns the new Studio item's id."""
    before = {a.id for a in studio_items(page)}
    start_video(page, source, prompt)
    for _ in range(30):
        new = [a for a in studio_items(page) if a.id not in before]
        if new:
            if len(new) > 1:
                log.warning("Several new Studio items appeared at once: %s", [a.id for a in new])
            return new[0].id
        page.wait_for_timeout(2_000)
    _check_quota(page)
    raise CreatorError("No new Video Overview appeared in Studio after 'Generate now'.")

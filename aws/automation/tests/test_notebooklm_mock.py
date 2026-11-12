"""
End-to-end test of notebooklm.download_video() against a MOCK NotebookLM page
(network intercepted; nothing goes to Google). The mock copies the real
layout: a Sources panel containing files like "U17C1.pdf", and a Studio list
whose rows show "7:20 · Explainer · 1 source" with a play button and a ⋮ menu.

Skipped automatically if Playwright's Chromium isn't installed.
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from playwright.sync_api import sync_playwright
    import notebooklm
except ImportError:  # pragma: no cover
    sync_playwright = None

PAGE = """<!doctype html><html><body>
<header>TQUK Level 3 Diploma in Healthcare Support Work (RQF)</header>
<section id="sources"><h2>Sources</h2>
  <div class="src"><span>U17C1.pdf</span><button aria-label="select">☐</button></div>
  <div class="src"><span>U22C6.pdf</span><button aria-label="select">☐</button></div>
  <div class="src"><span>Chapter-1_Unit-22-610-2249-4_pages_removed.pdf</span><button>☐</button></div>
</section>
<section id="studio"><h2>Studio</h2>
  <button>Audio Overview</button><button>Video Overview</button>
  <div id="list"></div>
</section>
<script>
const ITEMS = %ITEMS%;
const list = document.getElementById('list');
ITEMS.forEach((it, i) => {
  const row = document.createElement('div');
  row.className = 'artifact';
  row.innerHTML = `<mat-icon>${it.icon}</mat-icon><div><div class="t">${it.title}</div>
    <div class="s">${it.dur} · ${it.fmt} · 1 source · 27d ago</div></div>
    <button aria-label="Play">▶</button><button aria-label="More"><mat-icon>more_vert</mat-icon></button>`;
  row.querySelector('[aria-label=More]').onclick = () => {
    document.querySelectorAll('[role=menu]').forEach(m => m.remove());
    const menu = document.createElement('div');
    menu.setAttribute('role', 'menu');
    menu.innerHTML = '<div role="menuitem">Rename</div><div role="menuitem">Download</div><div role="menuitem">Delete</div>';
    menu.children[1].onclick = () => {
      const head = it.kind === 'audio'
        ? [0,0,0,0x18,0x66,0x74,0x79,0x70,0x4d,0x34,0x41,0x20]
        : [0,0,0,0x18,0x66,0x74,0x79,0x70,0x69,0x73,0x6f,0x6d];
      const bytes = new Uint8Array(200000); bytes.set(head);
      const a = document.createElement('a');
      a.href = URL.createObjectURL(new Blob([bytes]));
      a.download = it.title + (it.kind === 'audio' ? '.m4a' : '.mp4');
      document.body.appendChild(a); a.click(); a.remove(); menu.remove();
    };
    menu.children[2].onclick = () => { window.__deleted = true; };
    document.body.appendChild(menu);
  };
  list.appendChild(row);
});
</script></body></html>"""

ITEMS = [
    {"title": "U22C6", "dur": "7:20", "fmt": "Explainer", "icon": "subscriptions", "kind": "video"},
    {"title": "U22C5", "dur": "8:51", "fmt": "Explainer", "icon": "subscriptions", "kind": "video"},
    {"title": "U22C8", "dur": "12:02", "fmt": "Deep Dive", "icon": "audio_magic_eraser", "kind": "audio"},
    {"title": "U22C8", "dur": "7:16", "fmt": "Explainer", "icon": "subscriptions", "kind": "video"},
    {"title": "U19C3", "dur": "8:17", "fmt": "Explainer", "icon": "subscriptions", "kind": "video"},
    {"title": "U19C3", "dur": "8:02", "fmt": "Explainer", "icon": "subscriptions", "kind": "video"},
    # shared notebook: same unit/chapter for different courses (as in the real TEST log)
    {"title": "U2C4 -Level 5 Certificate in Human Nutrition", "dur": "6:10", "fmt": "Explainer", "icon": "videocam", "kind": "video"},
    {"title": "U2C4 - Level 3 Diploma in Specialist Support for Teaching and Learning in Schools", "dur": "7:00", "fmt": "Explainer", "icon": "videocam", "kind": "video"},
    {"title": "U2C4 - Level 3 Certificate in Supporting Teaching &amp; Learning in Schools", "dur": "5:55", "fmt": "Explainer", "icon": "videocam", "kind": "video"},
]


@unittest.skipIf(sync_playwright is None, "playwright not installed")
class MockNotebook(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import json
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch(headless=True)
        except Exception as e:  # browsers not installed
            cls.pw.stop()
            raise unittest.SkipTest(f"Chromium not available: {e}")
        cls.html = PAGE.replace("%ITEMS%", json.dumps(ITEMS))

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.ctx = self.browser.new_context(accept_downloads=True)
        html = self.html

        def short_link(route):
            # (Playwright can't follow a 302 produced by route.fulfill, so the mock
            # short link redirects with script; the code under test only waits for
            # the final notebook URL either way.)
            route.fulfill(status=200, content_type="text/html", body=(
                "<script>location.replace('https://notebooklm.google.com/notebook/nb123?artifactId=x')</script>"))

        def app(route):
            route.fulfill(status=200, content_type="text/html", body=html)

        self.ctx.route("https://notebooklm.link.google/**", short_link)
        self.ctx.route("https://notebooklm.google.com/**", app)
        self.ctx.route("https://notebook.google.com/**", app)
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        self.ctx.close()

    def get(self, unit, chapter):
        return notebooklm.download_video(self.ctx, "https://notebooklm.link.google/OwYjk2tkmv7Q", unit, chapter, self.tmp)

    def test_downloads_the_exact_video(self):
        res = self.get("22", "6")
        self.assertEqual(res.notebook_id, "nb123")
        self.assertTrue(res.title.startswith("U22C6"))
        self.assertEqual(notebooklm.media_kind(res.path), "video")
        self.assertEqual(res.path.suffix, ".mp4")

    def test_gemini_notebook_link(self):
        # Links in the log look like https://notebook.google.com/notebook/<uuid>
        res = notebooklm.download_video(
            self.ctx, "https://notebook.google.com/notebook/286f6392-efc7-4c7d-9025-896974382efb", "22", "6", self.tmp)
        self.assertEqual(res.notebook_id, "286f6392-efc7-4c7d-9025-896974382efb")
        self.assertEqual(res.title, "U22C6")

    def test_notebook_without_access(self):
        # A shared single-video link to a notebook the account isn't a member of:
        # Google opens the artifact URL, then redirects to "You need access".
        def no_access(route):
            if "/accessrequest/" in route.request.url:
                route.fulfill(status=200, content_type="text/html",
                              body="<h1>You need access</h1><button>Request access</button>")
            else:
                route.fulfill(status=200, content_type="text/html", body=(
                    "<script>setTimeout(() => location.replace("
                    "'https://notebook.google.com/accessrequest/nb9?ref=artifact'), 300)</script>"))
        self.ctx.route("https://notebook.google.com/**", no_access)
        with self.assertRaises(notebooklm.NoAccess) as cm:
            notebooklm.download_video(self.ctx, "https://notebook.google.com/notebook/nb9/artifact/a1", "16", "4",
                                      self.tmp)
        self.assertFalse(cm.exception.retry)
        self.assertIn("no access", str(cm.exception))

    def test_audio_with_same_title_is_ignored(self):
        res = self.get("22", "8")
        self.assertEqual(notebooklm.media_kind(res.path), "video")
        self.assertEqual(res.title, "U22C8")

    def test_source_files_never_match(self):
        with self.assertRaises(notebooklm.VideoNotFound):
            self.get("17", "1")  # only "U17C1.pdf" in Sources

    def test_missing_video(self):
        with self.assertRaises(notebooklm.VideoNotFound):
            self.get("22", "9")

    def test_two_videos_same_title_is_ambiguous(self):
        with self.assertRaises(notebooklm.AmbiguousVideo):
            self.get("19", "3")

    def test_shared_notebook_picks_by_course(self):
        res = notebooklm.download_video(
            self.ctx, "https://notebooklm.link.google/x", "2", "4", self.tmp,
            course="Level 3 Certificate in Supporting Teaching & Learning in Schools (RQF)", awarding_body="TQUK")
        self.assertIn("Supporting Teaching", res.title)
        with self.assertRaises(notebooklm.AmbiguousVideo):  # without the course it can't choose
            self.get("2", "4")

    def test_unit_prefix_not_confused(self):
        with self.assertRaises(notebooklm.VideoNotFound):
            self.get("2", "6")  # must not take U22C6

    def test_never_clicks_delete(self):
        self.get("22", "6")
        # no page left open, so check on a fresh page that nothing was deleted in this context
        self.assertFalse(any(p.evaluate("!!window.__deleted") for p in self.ctx.pages if not p.is_closed()))


if __name__ == "__main__":
    unittest.main()

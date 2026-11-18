"""extras_runner with NotebookLM, StudyGen, Word and the renderer replaced by fakes
(moto DynamoDB + S3); plus the pure rules in extras_rules."""
import os
import sys
import time
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for k, v in {"AWS_DEFAULT_REGION": "eu-west-2", "AWS_ACCESS_KEY_ID": "t", "AWS_SECRET_ACCESS_KEY": "t",
             "AWS_SESSION_TOKEN": "t"}.items():
    os.environ[k] = v
os.environ.pop("AWS_PROFILE", None)

try:
    from moto import mock_aws
except ImportError:  # pragma: no cover
    mock_aws = None

import creator  # noqa: E402
import creator_rules as cr  # noqa: E402
import extras_rules as er  # noqa: E402
import extras_runner as X  # noqa: E402
import studyguide as sg  # noqa: E402

RAW = "Focus Awards Level 3 Diploma in Gym (RQF)"
LINK = "https://southlondoncollege.org/course/gym/"


class Rules(unittest.TestCase):
    def test_prompt_and_names(self):
        p = er.podcast_prompt(3, RAW)
        self.assertIn("This is the unit 03 of the course Level 3 Diploma in Gym (RQF)", p)
        self.assertNotIn("Focus", p)                                      # no awarding body in materials
        self.assertEqual(er.podcast_file_name("Unit 2 - Health and Safety"), "Unit_2_-_Health_and_Safety.mp4")
        self.assertEqual(er.studyguide_file_name(3), "unit-03.pdf")
        self.assertEqual(sg.unit_label(3), "03")

    def test_decide(self):
        row = cr.DocRow(5, RAW, LINK, "3", "Anjani", "")
        g = [RAW, LINK, "3", "Anjani", "", "", "Rukaiya", "Done", "", ""]
        self.assertEqual(er.decide(er.PODCAST, row, g, None, 0).action, "claim")
        self.assertEqual(er.decide(er.STUDYGUIDE, row, g, None, 0).reason, "study guide done by a person")
        g2 = [RAW, LINK, "3", "Anjani", "auto", "", "", "", "", ""]
        self.assertEqual(er.decide(er.PODCAST, row, g2, None, 0).reason, "marked auto but unknown to the bot")
        self.assertEqual(er.decide(er.PODCAST, row, g2, {"state": "audio"}, 0).reason, "bot: audio")
        g3 = [RAW, LINK, "3", "Anjani", "", "Done", "", "", "", ""]
        self.assertEqual(er.decide(er.PODCAST, row, g3, None, 0).reason, "already marked Done")

    def test_headers(self):
        h = ["Course Name ", "Course Link", "Unit Received", "Done by", "Podcasts", "Status", "Study Guides",
             "Status", "Chapter-wise Videos ", "Status"]
        self.assertIsNone(er.check_headers(h))
        self.assertIn("column G", er.check_headers(h[:6] + ["Other"] + h[7:]))

    def test_course_size_one_line(self):
        width = 9026
        self.assertEqual(sg.course_size("Level 5 Diploma in Cyber Security (RQF)", width, odd=True), 18)
        long = "Level 5 Diploma in Leadership for Health and Social Care and Children and Young People's Services"
        small = sg.course_size(long, width, odd=True)
        self.assertLess(small, 18)
        self.assertLessEqual(sg.text_width_pt(long, small, True), (width - 2 * sg.SIDE_TWIPS) / 20)
        self.assertEqual(sg.course_size(long, width, odd=True, shrink_steps=2), small - 2)


class FakeNB:
    def __init__(self):
        self.items, self.sources, self.uploaded, self.n = {}, {}, [], 0
        self.notebook_sources = []

    def patch(self, test):
        for name in ("open_notebook", "studio_items", "upload_source", "generate_audio", "item_sources",
                     "rename_item", "download_item", "source_names", "pdf_matches"):
            p = mock.patch.object(creator, name, getattr(self, name))
            p.start()
            test.addCleanup(p.stop)

    def open_notebook(self, page, url): pass
    def studio_items(self, page): return list(self.items.values())
    def source_names(self, page): return list(self.notebook_sources)

    def upload_source(self, page, pdf):
        self.uploaded.append(pdf.name)
        self.notebook_sources.append(pdf.name)

    def generate_audio(self, page, sources, prompt):
        self.n += 1
        aid = f"a{self.n}"
        self.items[aid] = creator.Artifact(aid, "Generating Audio Overview…", "Come back in a few minutes",
                                           "progress_activity", "Audio Overview")
        self.sources[aid], self.prompt = list(sources), prompt
        return aid

    def finish(self, aid, fmt="Deep dive"):
        self.items[aid] = creator.Artifact(aid, "Some Title", f"12:40 · {fmt} · 2 sources · 1m ago", "audio_spark",
                                           "Audio Overview")

    def item_sources(self, page, aid): return self.sources[aid]

    def rename_item(self, page, aid, title):
        a = self.items[aid]
        self.items[aid] = creator.Artifact(aid, title, a.details, a.icon, a.kind)

    def download_item(self, page, aid, dest):
        p = Path(dest) / "audio.m4a"
        p.write_bytes(b"x" * 60_000)
        return p

    def pdf_matches(self, pdf, course, unit):
        return "the PDF linked in WordPress is for Unit 9" if b"wrong" in pdf.read_bytes() else None


class Ctx:
    def new_page(self):
        return mock.MagicMock()


@unittest.skipIf(mock_aws is None, "moto not installed")
class Runner(unittest.TestCase):
    def setUp(self):
        self.m = mock_aws()
        self.m.start()
        self.addCleanup(self.m.stop)
        import boto3
        ddb = boto3.resource("dynamodb", region_name="eu-west-2")
        self.table = ddb.create_table(
            TableName="t", BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                                  {"AttributeName": "active", "AttributeType": "S"},
                                  {"AttributeName": "created_at", "AttributeType": "N"}],
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            GlobalSecondaryIndexes=[{"IndexName": "active", "KeySchema": [
                {"AttributeName": "active", "KeyType": "HASH"}, {"AttributeName": "created_at", "KeyType": "RANGE"}],
                "Projection": {"ProjectionType": "KEYS_ONLY"}}])
        self.s3 = boto3.client("s3", region_name="eu-west-2")
        self.s3.create_bucket(Bucket="media-bucket", CreateBucketConfiguration={"LocationConstraint": "eu-west-2"})
        self.nb = FakeNB()
        self.nb.patch(self)
        self.pdf = b"%PDF-1.4 chapter"
        p = mock.patch.object(X, "_download_pdf", lambda url, dest: (dest.write_bytes(self.pdf + url.encode()), dest)[1])
        p.start()
        self.addCleanup(p.stop)

    def put(self, kind, chapters=2):
        pk = er.unit_key(kind, LINK, 3)
        self.table.put_item(Item={
            "pk": pk, "kind": kind.item_kind, "active": kind.active, "state": "queued", "attempts": 0,
            "created_at": Decimal("1"), "course_raw": RAW, "course_link": LINK, "unit": 3,
            "unit_title": "Unit 3 - Fitness", "doc_row": 9,
            "chapters": [{"n": n, "pdf_url": f"https://x/{n}.pdf"} for n in range(1, chapters + 1)]})
        return pk

    def get(self, pk):
        return self.table.get_item(Key={"pk": pk})["Item"]

    def run_once(self, to_pdf=None):
        return X.run(Ctx(), self.table, "https://notebook.google.com/notebook/nb", time.time() + 600,
                     s3=self.s3, bucket="media-bucket", to_pdf=to_pdf or (lambda b, n: b"%PDF"))

    def test_podcast_flow(self):
        pk = self.put(er.PODCAST)
        def fake_render(item, audio, work):
            out = work / er.podcast_file_name(item["unit_title"])
            out.write_bytes(b"v" * 200_000)
            return out
        render = mock.patch.object(X, "_render_podcast", fake_render)
        render.start()
        self.addCleanup(render.stop)
        rep = self.run_once()
        self.assertEqual(rep["podcasts_started"], 1)
        it = self.get(pk)
        self.assertEqual(it["state"], "audio")
        self.assertEqual(len(it["sources"]), 2)                      # ALL the unit's chapter PDFs
        self.assertTrue(all(s.startswith("U3C") and s.endswith(".pdf") for s in it["sources"]))
        self.assertEqual(self.nb.sources["a1"], it["sources"])
        self.assertIn("unit 03 of the course Level 3 Diploma in Gym (RQF)", self.nb.prompt)
        self.run_once()                                             # still generating: nothing happens
        self.assertEqual(self.get(pk)["state"], "audio")
        self.nb.finish("a1")
        rep = self.run_once()
        it = self.get(pk)
        self.assertEqual((it["state"], it["file_name"]), ("upload", "Unit_3_-_Fitness.mp4"))
        self.assertEqual(self.nb.items["a1"].title, "Podcast U3 - Level 3 Diploma in Gym (RQF)")
        self.assertEqual(self.s3.head_object(Bucket="media-bucket", Key=it["s3_key"])["ContentLength"], 200_000)

    def test_audio_format(self):
        A = lambda d: creator.Artifact("x", "t", d, "audio_spark", "Audio Overview")
        self.assertEqual(creator.AUDIO_FORMAT, "Deep dive")
        self.assertEqual(creator.audio_format(A("12:40 · Deep Dive · 2 sources · 1m ago")), "Deep dive")
        self.assertEqual(creator.audio_format(A("1:51 · Brief · 2 sources")), "Brief")
        self.assertIsNone(creator.audio_format(A("2 sources · 1m ago")))

    def test_brief_audio_is_not_used(self):
        pk = self.put(er.PODCAST)
        # an old Brief audio with the same title and sources is never reused ...
        self.nb.notebook_sources += [creator.source_name(3, n, "Level 3 Diploma in Gym (RQF)",
                                                         __import__("hashlib").sha1(self.pdf + f"https://x/{n}.pdf".encode()).hexdigest())
                                     for n in (1, 2)]
        self.nb.items["old"] = creator.Artifact("old", "Podcast U3 - Level 3 Diploma in Gym (RQF)",
                                                "1:51 · Brief · 2 sources", "audio_spark", "Audio Overview")
        self.nb.sources["old"] = sorted(self.nb.notebook_sources)
        self.run_once()
        self.assertEqual(self.get(pk)["artifact_id"], "a1")         # a new Deep dive one was started
        # ... and a Brief audio is never turned into a podcast
        self.nb.finish("a1", fmt="Brief")
        self.run_once()
        it = self.get(pk)
        self.assertIn("'Brief', not 'Deep dive'", it["error"])
        self.assertNotIn("s3_key", it)

    def test_podcast_generation_failure_retries_then_fails(self):
        pk = self.put(er.PODCAST)
        self.run_once()
        for _ in range(2):
            self.nb.items.clear()                                   # NotebookLM: generation failed
            self.run_once()
        it = self.get(pk)
        self.assertEqual(it["state"], "failed")
        self.assertEqual(self.nb.uploaded.count(it["sources"][0]), 1)  # sources uploaded once, reused after

    def test_wrong_pdf_is_never_made(self):
        pk = self.put(er.PODCAST)
        self.pdf = b"%PDF wrong"
        self.run_once()
        it = self.get(pk)
        self.assertEqual(it["state"], "failed")
        self.assertIn("Unit 9", it["error"])
        self.assertEqual(self.nb.uploaded, [])

    def _fake_guide(self, footer_problems):
        calls = {"fix": [], "check": 0}
        patches = [
            mock.patch.object(sg, "cover_image", lambda pdfs, out: out.write_bytes(b"jpg") or out),
            mock.patch.object(sg, "generate_docx", lambda page, pdfs, course, unit, cover, d:
                              (d / "unit-03.docx").write_bytes(b"docx") or d / "unit-03.docx"),
            mock.patch.object(sg, "fix_footers", lambda a, b, course, shrink_steps=0:
                              calls["fix"].append((course, shrink_steps)) or b.write_bytes(b"fixed")),
            mock.patch.object(sg, "check_footers", lambda pdf, course: footer_problems.pop(0) if footer_problems else []),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return calls

    def test_study_guide_flow_shrinks_footer_until_one_line(self):
        pk = self.put(er.STUDYGUIDE)
        calls = self._fake_guide([["page 2: the footer runs onto two lines (course name too long)"], []])
        converted = []
        rep = self.run_once(to_pdf=lambda b, n: converted.append(n) or b"%PDF-1.7 guide")
        self.assertEqual(rep["study_guides_ready"], 1)
        self.assertEqual(calls["fix"], [("Level 3 Diploma in Gym (RQF)", 0), ("Level 3 Diploma in Gym (RQF)", 1)])
        it = self.get(pk)
        self.assertEqual((it["state"], it["file_name"]), ("upload", "unit-03.pdf"))
        self.assertEqual(converted, ["unit-03", "unit-03"])

    def test_study_guide_with_bad_footer_is_not_uploaded(self):
        pk = self.put(er.STUDYGUIDE)
        self._fake_guide([["page 3: '2 | Page' should be on the right"]] * 4)
        self.run_once()
        it = self.get(pk)
        self.assertEqual(it["state"], "queued")                     # retried next run, never uploaded
        self.assertIn("footers are not right", it["error"])
        self.assertNotIn("s3_key", it)


if __name__ == "__main__":
    unittest.main()

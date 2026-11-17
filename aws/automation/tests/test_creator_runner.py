"""creator_runner state machine with NotebookLM replaced by a fake (moto DynamoDB)."""
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
import creator_runner as R  # noqa: E402

GOOD_PDF = b"%PDF-1.4 good"
COURSE = "Level 2 Award in Support Work in Schools and Colleges"


class FakeNotebook:
    """Stands in for the creator.* browser functions."""

    def __init__(self):
        self.items: dict[str, creator.Artifact] = {}
        self.sources: dict[str, str] = {}      # artifact id -> source name
        self.uploaded: list[str] = []
        self.quota = False
        self.n = 0

    def patch(self, test):
        for name in ("open_notebook", "studio_items", "upload_source", "generate", "item_sources", "rename_item",
                     "share_link", "pdf_matches"):
            p = mock.patch.object(creator, name, getattr(self, name))
            p.start()
            test.addCleanup(p.stop)
        p = mock.patch.object(R, "_download_pdf", lambda url, dest: dest.write_bytes(GOOD_PDF + url.encode()))
        p.start()
        test.addCleanup(p.stop)

    def open_notebook(self, page, url):
        pass

    def studio_items(self, page):
        return list(self.items.values())

    def upload_source(self, page, pdf):
        self.uploaded.append(pdf.name)

    def generate(self, page, source):
        if self.quota:
            raise creator.QuotaReached("You have reached your daily limit")
        self.n += 1
        aid = f"a{self.n}"
        self.items[aid] = creator.Artifact(aid, "Generating explainer Video Overview…", "Looking through sources…",
                                           "progress_activity", "Video Overview")
        self.sources[aid] = source
        return aid

    def finish(self, aid):
        self.items[aid] = creator.Artifact(aid, "Some Auto Title", "8:04 · Explainer · 1 source · 1m ago",
                                           "videocam", "Video Overview")

    def item_sources(self, page, aid):
        return [self.sources[aid]]

    def rename_item(self, page, aid, title):
        a = self.items[aid]
        self.items[aid] = creator.Artifact(aid, title, a.details, a.icon, a.kind)

    def share_link(self, page, aid):
        return f"https://notebook.google.com/notebook/nb/artifact/{aid}"

    def pdf_matches(self, pdf, course, unit):
        return "the PDF linked in WordPress is for Unit 3, not Unit 1" if b"wrong" in pdf.read_bytes() else None


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
        self.nb = FakeNotebook()
        self.nb.patch(self)
        self.alerts = []

    def unit(self, chapters=3, pdf=lambda n: f"https://x/ch{n}.pdf"):
        self.table.put_item(Item={
            "pk": "CUNIT#1", "kind": "cunit", "active": "C", "cstate": "chapters", "created_at": Decimal("1"),
            "unit": 1, "course_name": COURSE, "awarding_body": "NCFE CACHE", "doc_row": 533,
            "chapters": [{"n": n, "pdf_url": pdf(n), "state": "queued", "attempts": 0} for n in range(1, chapters + 1)]})

    def run_once(self):
        return R.run(Ctx(), self.table, "https://notebook.google.com/notebook/nb", time.time() + 600,
                     alert=lambda *a: self.alerts.append(a[0]))

    def get(self):
        return self.table.get_item(Key={"pk": "CUNIT#1"})["Item"]

    def test_full_flow(self):
        self.unit(chapters=4)
        rep = self.run_once()
        self.assertEqual(rep["started"], 3)                       # MAX_GENERATING
        states = [c["state"] for c in self.get()["chapters"]]
        self.assertEqual(states, ["generating", "generating", "generating", "queued"])
        self.assertTrue(all(n.startswith("U1C") and "[" in n for n in self.nb.uploaded))
        for aid in list(self.nb.items):
            self.nb.finish(aid)
        rep = self.run_once()
        self.assertEqual(rep["collected"], 3)
        self.assertEqual(rep["started"], 1)
        u = self.get()
        self.assertEqual(u["chapters"][0]["state"], "ready")
        self.assertEqual(u["chapters"][0]["video_title"], f"U1C1 - {COURSE}")
        self.assertEqual(self.nb.items["a1"].title, f"U1C1 - {COURSE}")
        self.nb.finish("a4")
        self.run_once()
        u = self.get()
        self.assertEqual(u["cstate"], "logging")                  # the API writes the log rows next
        self.assertEqual([c["link"][-2:] for c in u["chapters"]], ["a1", "a2", "a3", "a4"])

    def test_wrong_pdf_is_not_made(self):
        self.unit(chapters=2, pdf=lambda n: "https://x/wrong.pdf" if n == 1 else "https://x/ok.pdf")
        self.run_once()
        ch = self.get()["chapters"]
        self.assertEqual(ch[0]["state"], "failed")
        self.assertIn("Unit 3", ch[0]["error"])
        self.assertEqual(ch[1]["state"], "generating")
        self.assertEqual(len(self.nb.uploaded), 1)                # the wrong PDF was never uploaded

    def test_video_from_other_source_is_rejected(self):
        self.unit(chapters=1)
        self.run_once()
        self.nb.finish("a1")
        self.nb.sources["a1"] = "something else.pdf"
        self.run_once()
        ch = self.get()["chapters"][0]
        self.assertIn("not only", ch["error"])
        self.assertEqual(ch["state"], "generating")               # rejected, then made again
        self.assertEqual(ch["artifact_id"], "a2")
        self.assertEqual(int(ch["attempts"]), 2)
        self.assertNotEqual(self.nb.items["a1"].title, f"U1C1 - {COURSE}")  # the wrong one is never renamed

    def test_quota_pauses(self):
        self.unit(chapters=2)
        self.nb.quota = True
        rep = self.run_once()
        self.assertTrue(rep["paused"])
        self.assertEqual(self.alerts, ["creator_quota"])
        self.assertEqual([c["state"] for c in self.get()["chapters"]], ["queued", "queued"])
        self.nb.quota = False
        self.assertTrue(self.run_once().get("paused"))            # waits out the pause

    def test_stuck_generation_times_out(self):
        self.unit(chapters=1)
        self.run_once()
        self.table.update_item(Key={"pk": "CUNIT#1"}, UpdateExpression="SET chapters[0].started_at = :t",
                               ExpressionAttributeValues={":t": Decimal(str(time.time() - 3 * 3600))})
        self.run_once()
        self.assertEqual(self.get()["chapters"][0]["state"], "generating")  # requeued and started again
        self.assertEqual(int(self.get()["chapters"][0]["attempts"]), 2)


if __name__ == "__main__":
    unittest.main()

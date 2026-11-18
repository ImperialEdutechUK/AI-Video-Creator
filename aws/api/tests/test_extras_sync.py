"""extras_sync: podcasts / study guides - claiming units (columns E / G), marks,
Drive upload and 'Done' (moto DynamoDB + S3, fake Graph, WordPress and Drive)."""
import os
import sys
import time
import unittest
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent))
import test_creator_sync as base  # noqa: E402  (sets the environment, fakes)

LINK = base.LINK
mock_aws = base.mock_aws


class FakeFiles:
    def __init__(self, drive):
        self.d = drive

    def list(self, q="", **kw):
        import re
        name = re.search(r"name = '((?:[^'\\]|\\.)*)'", q)
        parent = re.search(r"'([^']+)' in parents", q)
        folder = "mimeType" in q
        hits = [f for f in self.d.files if (not name or f["name"] == name.group(1).replace("\\'", "'"))
                and (not parent or parent.group(1) in f["parents"]) and f["folder"] == folder]
        return _Exec({"files": hits})

    def create(self, body=None, media_body=None, **kw):
        size = os.path.getsize(media_body.path) if media_body else 0
        f = {"id": f"f{len(self.d.files)}", "name": body["name"], "parents": body["parents"], "folder": False,
             "size": str(size), "webViewLink": f"https://drive/f{len(self.d.files)}"}
        return _Upload(self.d, f)

    def get(self, fileId=None, **kw):
        return _Exec(next(dict(f, trashed=False) for f in self.d.files if f["id"] == fileId))


class _Exec:
    def __init__(self, v):
        self.v = v

    def execute(self):
        return self.v


class _Upload:
    def __init__(self, d, f):
        self.d, self.f = d, f

    def next_chunk(self):
        self.d.files.append(self.f)
        return None, self.f


class FakeDrive:
    FOLDER_MIME = "application/vnd.google-apps.folder"
    HttpError = Exception

    def __init__(self):
        self.files = []

    def get_service(self):
        svc = type("Svc", (), {})()
        svc.files = lambda: FakeFiles(self)
        return svc

    def _create_folder(self, svc, name, parent):
        f = {"id": f"d{len(self.files)}", "name": name, "parents": [parent], "folder": True}
        self.files.append(f)
        return f["id"]

    class MediaFileUpload:
        def __init__(self, path, **kw):
            self.path = path


def doc_row(course, unit, podcast="", ps="", guide="", gs="", link=LINK):
    return [course, link, str(unit), "Anjani", podcast, ps, guide, gs, "", "", "", ""]


@unittest.skipIf(mock_aws is None, "moto not installed")
class ExtrasSync(unittest.TestCase):
    def setUp(self):
        os.environ["JOBS_TABLE"] = "creator-test-jobs"
        self.m = mock_aws()
        self.m.start()
        self.addCleanup(self.m.stop)
        import boto3
        boto3.resource("dynamodb", region_name="eu-west-2").create_table(
            TableName="creator-test-jobs", BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                                  {"AttributeName": "active", "AttributeType": "S"},
                                  {"AttributeName": "created_at", "AttributeType": "N"}],
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            GlobalSecondaryIndexes=[{"IndexName": "active", "KeySchema": [
                {"AttributeName": "active", "KeyType": "HASH"}, {"AttributeName": "created_at", "KeyType": "RANGE"}],
                "Projection": {"ProjectionType": "INCLUDE", "NonKeyAttributes": ["qstate", "sid"]}}])
        old_bucket = os.environ.get("MEDIA_BUCKET")
        os.environ["MEDIA_BUCKET"] = "media-test-bucket"
        self.addCleanup(lambda: os.environ.__setitem__("MEDIA_BUCKET", old_bucket or "m"))
        boto3.client("s3", region_name="eu-west-2").create_bucket(
            Bucket="media-test-bucket", CreateBucketConfiguration={"LocationConstraint": "eu-west-2"})
        for mod in ("automation", "creator_sync", "extras_sync"):
            sys.modules.pop(mod, None)
        import automation
        import creator_sync
        import extras_sync
        from excel import Workbook
        self.A, self.C, self.X = automation, creator_sync, extras_sync
        self.X.ENABLED = True
        self.alerts = []
        self.A.alert = lambda kind, subject, msg, **kw: self.alerts.append(kind) or True
        self.drive = FakeDrive()
        self.X._drive = lambda: self.drive
        self.C.wordpress = lambda: base.FakeWP(chapters=2)
        self.wb = Workbook("d", "i", "wb", "log-ws", "log")

    def graph(self, rows):
        g = base.FakeGraph(rows, [["h"] * 9])
        self.A.open_sheet = lambda: (g, self.wb)
        return g

    def test_claims_newest_empty_units_for_each_kind(self):
        g = self.graph([doc_row("NCFE CACHE Level 2 Award in A", 1, podcast="Rukaiya", ps="Done"),
                        doc_row("NCFE CACHE Level 2 Award in A", 2)])
        rep = self.X.scan(g, self.wb, dry_run=False)
        self.assertEqual([c["row"] for c in rep["podcast"]["claimed"]], [3])
        self.assertEqual([c["row"] for c in rep["studyguide"]["claimed"]], [3])
        self.assertIn(("doc-ws", "E3", "auto"), g.writes)
        self.assertIn(("doc-ws", "G3", "auto"), g.writes)
        self.assertNotIn(("doc-ws", "E2", "auto"), g.writes)          # a person's podcast: untouched
        it = self.A.table.get_item(Key={"pk": self.X.er.unit_key(self.X.er.PODCAST, LINK, 2)})["Item"]
        self.assertEqual((it["state"], it["unit_title"], len(it["chapters"])), ("queued", "Unit 2 - X", 2))
        self.assertTrue(self.X.needs_task())

    def test_already_in_drive_is_not_made_again(self):
        g = self.graph([doc_row("NCFE CACHE Level 2 Award in A", 4)])
        folder = self.drive._create_folder(None, "NCFE CACHE Level 2 Award in A", self.X.FOLDERS["studyguide"])
        self.drive.files.append({"id": "x", "name": "unit 4.docx", "parents": [folder], "folder": False, "size": "9"})
        rep = self.X.scan(g, self.wb, dry_run=False)
        self.assertEqual(rep["studyguide"]["claimed"], [])
        self.assertNotIn(("doc-ws", "G2", "auto"), g.writes)
        self.assertEqual(len(rep["podcast"]["claimed"]), 1)          # the podcast isn't in Drive: made

    def test_row_taken_meanwhile_is_left_alone(self):
        g = self.graph([doc_row("NCFE CACHE Level 2 Award in A", 1)])
        real = g.read_range
        g.read_range = lambda wb, a: [[*r[:4], "Piyumi", *r[5:]] for r in real(wb, a)]
        rep = self.X.scan(g, self.wb, dry_run=False)
        self.assertEqual(rep["podcast"]["claimed"], [])
        self.assertFalse(any(w[1] == "E2" for w in g.writes))

    def _ready(self, kind, unit, course="NCFE CACHE Level 2 Award in A", name="unit-01.pdf"):
        pk = self.X.er.unit_key(kind, LINK, unit)
        self.A.s3.put_object(Bucket="media-test-bucket", Key=f"automation/extras/{pk}", Body=b"%PDF-1.7 study guide")
        self.A.table.put_item(Item={
            "pk": pk, "kind": kind.item_kind, "active": kind.active, "state": "upload", "created_at": Decimal("1"),
            "updated_at": Decimal(str(time.time())), "doc_row": 2, "doc_wb_drive": "d", "doc_wb_item": "i",
            "doc_ws_id": "doc-ws", "course_raw": course, "course_link": LINK, "unit": unit, "unit_title": "Unit 1 - X",
            "chapters": [], "s3_key": f"automation/extras/{pk}", "file_name": name})
        return pk

    def test_upload_to_course_folder_then_done(self):
        g = self.graph([doc_row("Moved row", 9), doc_row("NCFE CACHE Level 2 Award in A", 1, guide="auto")])
        pk = self._ready(self.X.er.STUDYGUIDE, 1)
        rep = self.X.follow_up()
        self.assertEqual(len(rep["uploaded"]), 1)
        folders = [f for f in self.drive.files if f["folder"]]
        self.assertEqual([f["name"] for f in folders], ["NCFE CACHE Level 2 Award in A"])  # full sheet name
        self.assertEqual([f["name"] for f in self.drive.files if not f["folder"]], ["unit-01.pdf"])
        self.assertIn(("doc-ws", "H3", "Done"), g.writes)          # found by content, not old row number
        it = self.A.table.get_item(Key={"pk": pk})["Item"]
        self.assertEqual(it["state"], "done")
        self.assertNotIn("active", it)
        # a second study guide of the same course goes into the SAME folder
        self._ready(self.X.er.STUDYGUIDE, 2, name="unit-02.pdf")
        g.doc.append(doc_row("NCFE CACHE Level 2 Award in A", 2, guide="auto"))
        self.X.follow_up()
        self.assertEqual(len([f for f in self.drive.files if f["folder"]]), 1)

    def test_failed_unit_is_reported_and_auto_removed(self):
        g = self.graph([doc_row("NCFE CACHE Level 2 Award in A", 1, podcast="auto")])
        pk = self.X.er.unit_key(self.X.er.PODCAST, LINK, 1)
        self.A.table.put_item(Item={
            "pk": pk, "kind": "punit", "active": "P", "state": "failed", "error": "NotebookLM said no",
            "created_at": Decimal("1"), "doc_row": 2, "doc_wb_drive": "d", "doc_wb_item": "i", "doc_ws_id": "doc-ws",
            "course_raw": "NCFE CACHE Level 2 Award in A", "course_link": LINK, "unit": 1, "chapters": []})
        self.X.follow_up()
        self.assertEqual(self.alerts, [f"extras_failed_{pk[-8:]}"])
        self.assertIn(("doc-ws", "E2", ""), g.writes)
        self.X.follow_up()                                          # only once
        self.assertEqual(len(self.alerts), 1)

    def test_marks_follow_the_unit(self):
        g = self.graph([doc_row("Other", 5, podcast="auto"), doc_row("NCFE CACHE Level 2 Award in A", 1)])
        pk = self.X.er.unit_key(self.X.er.PODCAST, LINK, 1)
        self.A.table.put_item(Item={
            "pk": pk, "kind": "punit", "active": "P", "state": "audio", "created_at": Decimal(str(time.time())),
            "course_raw": "NCFE CACHE Level 2 Award in A", "course_link": LINK, "unit": 1, "chapters": []})
        self.X.scan(g, self.wb, dry_run=False)
        self.assertIn(("doc-ws", "E2", ""), g.writes)              # stray auto cleared
        self.assertIn(("doc-ws", "E3", "auto"), g.writes)          # put back on the unit's row


if __name__ == "__main__":
    unittest.main()

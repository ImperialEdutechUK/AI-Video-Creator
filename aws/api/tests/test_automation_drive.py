"""The automation's own Drive folder: test-mode guard, and course folders that
are created ONCE even when several saves for a new course run at the same time
(this created duplicate folders during the first live test)."""
import os
import sys
import threading
import time
import types
import unittest
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE.parents[2] / "automation"))
for k, v in {"JOBS_TABLE": "drive-test-jobs", "MEDIA_BUCKET": "m", "WORKER_CLUSTER": "c", "WORKER_TASK_FAMILY": "f",
             "AWS_REGION": "eu-west-2", "AWS_DEFAULT_REGION": "eu-west-2", "AWS_ACCESS_KEY_ID": "testing",
             "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing"}.items():
    os.environ[k] = v
os.environ.pop("AWS_PROFILE", None)

try:
    from moto import mock_aws
except ImportError:  # pragma: no cover
    mock_aws = None

PROD = "1cY7v7956TyrJbGPGno4QQJ5Zpj6bXDjZ"


class FakeDriveService:
    """Just enough of the Drive v3 client. New folders become visible to
    search only after `lag` seconds, like Drive's eventual consistency."""

    def __init__(self, lag=0.0, create_delay=0.0):
        self.folders = []          # dicts: id, name, parent, created, trashed
        self.lag, self.create_delay = lag, create_delay
        self.lock = threading.Lock()

    def files(self):
        return self

    def list(self, q, **kw):
        import re
        name = re.search(r"name = '((?:[^'\\]|\\.)*)'", q).group(1).replace("\\'", "'")
        parent = re.search(r"'([^']+)' in parents", q).group(1)
        now = time.time()
        with self.lock:
            hits = [f for f in self.folders if f["name"] == name and f["parent"] == parent
                    and not f["trashed"] and now - f["created"] >= self.lag]
        hits.sort(key=lambda f: f["created"])
        return types.SimpleNamespace(execute=lambda: {"files": [{"id": f["id"]} for f in hits]})

    def get(self, fileId, **kw):
        f = next(f for f in self.folders if f["id"] == fileId)
        return types.SimpleNamespace(execute=lambda: {"id": f["id"], "trashed": f["trashed"]})

    def create_folder(self, name, parent):
        time.sleep(self.create_delay)
        with self.lock:
            fid = f"F{len(self.folders) + 1}"
            self.folders.append({"id": fid, "name": name, "parent": parent, "created": time.time(),
                                 "trashed": False})
        return fid


def fake_drive(svc=None, team_root=PROD):
    svc = svc or FakeDriveService()
    d = types.SimpleNamespace()
    d.FOLDER_MIME = "application/vnd.google-apps.folder"
    d._extract_folder_id = lambda v: (v or "").rstrip("/").split("/")[-1]
    d._root_folder_id = lambda: team_root
    d._clean = lambda n: n
    d._create_folder = lambda s, name, parent: svc.create_folder(name, parent)
    d.get_service = lambda: svc
    d.HttpError = RuntimeError
    d.svc = svc
    return d


@unittest.skipIf(mock_aws is None, "moto not installed")
class DriveFolders(unittest.TestCase):
    def setUp(self):
        os.environ["JOBS_TABLE"] = "drive-test-jobs"  # other test files set their own name at import
        self.mock = mock_aws()
        self.mock.start()
        self.addCleanup(self.mock.stop)
        import boto3
        boto3.resource("dynamodb", region_name="eu-west-2").create_table(
            TableName="drive-test-jobs", BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}])
        for m in ("automation", "handler"):
            sys.modules.pop(m, None)
        import automation
        self.a = automation
        self.use("https://drive.google.com/drive/folders/AUTOTEST")

    def use(self, folder_url, production=False):
        self.a.settings = lambda: {"drive_folder_url": folder_url}
        self.a.ALLOW_PRODUCTION_SHEET = production
        self.a._course_folders.clear()

    JOB = {"awarding_body": "TQUK", "course_name": "Level 3 Diploma in Specialist Support"}

    # ── test-mode guard ──
    def test_test_mode_requires_own_folder(self):
        self.use("")
        with self.assertRaises(self.a.DriveFolderGuard):
            self.a.automation_root(fake_drive())

    def test_test_mode_refuses_team_folders(self):
        self.use(f"https://drive.google.com/drive/folders/{PROD}")
        with self.assertRaises(self.a.DriveFolderGuard):
            self.a.automation_root(fake_drive())
        self.use("https://drive.google.com/drive/folders/TEAMTEST")
        with self.assertRaises(self.a.DriveFolderGuard):
            self.a.automation_root(fake_drive(team_root="TEAMTEST"))

    def test_production_after_cutover(self):
        self.use("", production=True)
        self.assertEqual(self.a.automation_root(fake_drive()), PROD)

    # ── course folders ──
    def test_creates_under_own_root(self):
        d = fake_drive()
        fid = self.a.course_folder(d, self.JOB)
        self.assertEqual(d.svc.folders[0]["parent"], "AUTOTEST")
        self.assertEqual(d.svc.folders[0]["name"], "TQUK - Level 3 Diploma in Specialist Support")
        self.assertEqual(self.a.course_folder(d, self.JOB), fid)

    def test_concurrent_saves_create_one_folder(self):
        # 5 saves for a NEW course at once; Drive search lags 3 s behind creation.
        svc = FakeDriveService(lag=3.0, create_delay=0.5)
        results, errors = [], []

        def save():
            try:
                self.a._course_folders.clear()
                results.append(self.a.course_folder(fake_drive(svc), self.JOB))
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=save) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(len(svc.folders), 1, "the course folder was created more than once")
        self.assertEqual(set(results), {svc.folders[0]["id"]})

    def test_existing_duplicates_use_oldest(self):
        svc = FakeDriveService()
        a = svc.create_folder("TQUK - Level 3 Diploma in Specialist Support", "AUTOTEST")
        time.sleep(0.01)
        svc.create_folder("TQUK - Level 3 Diploma in Specialist Support", "AUTOTEST")
        self.assertEqual(self.a.course_folder(fake_drive(svc), self.JOB), a)

    def test_trashed_folder_is_replaced(self):
        svc = FakeDriveService()
        first = self.a.course_folder(fake_drive(svc), self.JOB)
        svc.folders[0]["trashed"] = True
        self.a._course_folders.clear()
        second = self.a.course_folder(fake_drive(svc), self.JOB)
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()

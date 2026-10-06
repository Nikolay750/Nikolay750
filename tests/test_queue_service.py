"""Step 6: очередь оператора — чистая логика (без FastAPI)."""

import importlib
import os
import shutil
import sys
import tempfile
import unittest


class QueueServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="queue_test_")
        os.environ["MINIAPP_DATA_DIR"] = self.tmp_dir
        for mod in ("app.storage", "app.queue_service", "app.models"):
            sys.modules.pop(mod, None)
        self.storage = importlib.import_module("app.storage")
        self.models = importlib.import_module("app.models")
        self.svc = importlib.import_module("app.queue_service")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        os.environ.pop("MINIAPP_DATA_DIR", None)

    def _make(self, client_id, status, findings=None):
        pkg = self.models.Package.create(client_id=client_id, client_name=f"Клиент {client_id}")
        pkg.status = status
        if findings:
            pkg.set_findings(findings)
        self.storage.save_package(pkg)
        return pkg

    def test_draft_packages_are_not_in_queue(self):
        self._make("1", self.models.PackageStatus.NEW.value)
        self.assertEqual(self.svc.list_queue(), [])

    def test_active_statuses_appear_in_queue(self):
        q = self._make("1", self.models.PackageStatus.QUEUED.value)
        p = self._make("2", self.models.PackageStatus.PROCESSING.value)
        r = self._make("3", self.models.PackageStatus.READY_FOR_REVIEW.value)
        e = self._make("4", self.models.PackageStatus.ERROR.value)

        ids = {item["id"] for item in self.svc.list_queue()}
        self.assertEqual(ids, {q.id, p.id, r.id, e.id})

    def test_history_hidden_unless_requested(self):
        self._make("1", self.models.PackageStatus.SENT.value)
        self.assertEqual(self.svc.list_queue(), [])
        self.assertEqual(len(self.svc.list_queue(include_history=True)), 1)

    def test_queue_is_sorted_oldest_first(self):
        first = self._make("1", self.models.PackageStatus.READY_FOR_REVIEW.value)
        second = self._make("2", self.models.PackageStatus.READY_FOR_REVIEW.value)
        ids = [item["id"] for item in self.svc.list_queue()]
        self.assertEqual(ids, [first.id, second.id])

    def test_summary_counts_findings_by_status_and_pending_decisions(self):
        finding_ok = self.models.Finding(id="f1", rule_id="S-01", status="OK", text="ок")
        finding_warn = self.models.Finding(id="f2", rule_id="S-02", status="WARNING", text="есть расхождение")
        pkg = self._make("1", self.models.PackageStatus.READY_FOR_REVIEW.value,
                          findings=[finding_ok, finding_warn])

        detail = self.svc.get_package_detail(pkg.client_id, pkg.id)
        self.assertEqual(detail["findings_by_status"], {"OK": 1, "WARNING": 1})
        self.assertEqual(detail["pending_decisions"], 2)
        self.assertEqual(len(detail["findings"]), 2)

    def test_get_package_detail_missing_raises_keyerror(self):
        with self.assertRaises(KeyError):
            self.svc.get_package_detail("ghost", "ghost")

    def test_find_package_detail_without_known_client(self):
        pkg = self._make("55", self.models.PackageStatus.READY_FOR_REVIEW.value)
        detail = self.svc.find_package_detail(pkg.id)
        self.assertIsNotNone(detail)
        self.assertEqual(detail["client_id"], "55")

    def test_find_package_detail_missing_returns_none(self):
        self.assertIsNone(self.svc.find_package_detail("no-such-id"))


if __name__ == "__main__":
    unittest.main()

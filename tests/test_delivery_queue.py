"""Step 8 (продолжение): очередь на фоновую рассылку клиентам."""

import importlib
import os
import shutil
import sys
import tempfile
import unittest


class DeliveryQueueTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="delivery_queue_test_")
        os.environ["MINIAPP_DATA_DIR"] = self.tmp_dir
        for mod in ("app.storage", "app.delivery_service", "app.models"):
            sys.modules.pop(mod, None)
        self.storage = importlib.import_module("app.storage")
        self.models = importlib.import_module("app.models")
        self.svc = importlib.import_module("app.delivery_service")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        os.environ.pop("MINIAPP_DATA_DIR", None)

    def test_only_reviewed_with_report_and_unsent_are_listed(self):
        reviewed_ready = self.models.Package.create(client_id="1")
        reviewed_ready.status = self.models.PackageStatus.REVIEWED.value
        reviewed_ready.client_report_path = "client_report.xlsx"
        self.storage.save_package(reviewed_ready)

        reviewed_no_report = self.models.Package.create(client_id="2")
        reviewed_no_report.status = self.models.PackageStatus.REVIEWED.value
        self.storage.save_package(reviewed_no_report)

        already_sent = self.models.Package.create(client_id="3")
        already_sent.status = self.models.PackageStatus.SENT.value
        already_sent.client_report_path = "client_report.xlsx"
        already_sent.sent_at = "2026-01-01T00:00:00+00:00"
        self.storage.save_package(already_sent)

        still_in_review = self.models.Package.create(client_id="4")
        still_in_review.status = self.models.PackageStatus.READY_FOR_REVIEW.value
        self.storage.save_package(still_in_review)

        ids = {p.id for p in self.svc.list_ready_to_send()}
        self.assertEqual(ids, {reviewed_ready.id})

    def test_marking_sent_removes_it_from_queue(self):
        pkg = self.models.Package.create(client_id="5")
        pkg.status = self.models.PackageStatus.REVIEWED.value
        pkg.client_report_path = "client_report.xlsx"
        self.storage.save_package(pkg)

        self.assertEqual(len(self.svc.list_ready_to_send()), 1)
        self.svc.mark_sent("5", pkg.id)
        self.assertEqual(len(self.svc.list_ready_to_send()), 0)


if __name__ == "__main__":
    unittest.main()

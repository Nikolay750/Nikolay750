"""Step 7: разбор находок оператором (confirm/reject/edit) + закрытие пакета."""

import importlib
import os
import shutil
import sys
import tempfile
import unittest


class ReviewServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="review_test_")
        os.environ["MINIAPP_DATA_DIR"] = self.tmp_dir
        for mod in ("app.storage", "app.review_service", "app.queue_service", "app.models"):
            sys.modules.pop(mod, None)
        self.storage = importlib.import_module("app.storage")
        self.models = importlib.import_module("app.models")
        self.svc = importlib.import_module("app.review_service")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        os.environ.pop("MINIAPP_DATA_DIR", None)

    def _ready_package(self, n_findings=2):
        pkg = self.models.Package.create(client_id="1", client_name="Клиент")
        findings = [
            self.models.Finding(id=f"f{i}", rule_id=f"S-0{i}", status="WARNING", text=f"находка {i}")
            for i in range(n_findings)
        ]
        pkg.set_findings(findings)
        pkg.status = self.models.PackageStatus.READY_FOR_REVIEW.value
        self.storage.save_package(pkg)
        return pkg

    def test_confirm_finding(self):
        pkg = self._ready_package(1)
        result = self.svc.decide_finding("1", pkg.id, "f0", "confirmed")
        self.assertEqual(result["decision"], "confirmed")
        self.assertIsNotNone(result["decided_at"])

        reloaded = self.storage.load_package("1", pkg.id)
        self.assertEqual(reloaded.findings[0].decision, "confirmed")

    def test_reject_finding(self):
        pkg = self._ready_package(1)
        result = self.svc.decide_finding("1", pkg.id, "f0", "rejected")
        self.assertEqual(result["decision"], "rejected")

    def test_edit_finding_requires_text(self):
        pkg = self._ready_package(1)
        with self.assertRaises(self.svc.ReviewError):
            self.svc.decide_finding("1", pkg.id, "f0", "edited", edited_text="  ")

    def test_edit_finding_stores_edited_text_and_final_text_uses_it(self):
        pkg = self._ready_package(1)
        self.svc.decide_finding("1", pkg.id, "f0", "edited", edited_text="Уточнённый текст")
        reloaded = self.storage.load_package("1", pkg.id)
        self.assertEqual(reloaded.findings[0].final_text(), "Уточнённый текст")

    def test_unknown_decision_rejected(self):
        pkg = self._ready_package(1)
        with self.assertRaises(self.svc.ReviewError):
            self.svc.decide_finding("1", pkg.id, "f0", "maybe")

    def test_unknown_finding_id_rejected(self):
        pkg = self._ready_package(1)
        with self.assertRaises(self.svc.ReviewError):
            self.svc.decide_finding("1", pkg.id, "no-such-finding", "confirmed")

    def test_cannot_decide_on_package_not_yet_checked(self):
        pkg = self.models.Package.create(client_id="2")
        pkg.set_findings([self.models.Finding(id="f0", rule_id="S-01", status="OK", text="x")])
        pkg.status = self.models.PackageStatus.QUEUED.value
        self.storage.save_package(pkg)
        with self.assertRaises(self.svc.ReviewError):
            self.svc.decide_finding("2", pkg.id, "f0", "confirmed")

    def test_finalize_requires_all_findings_decided(self):
        pkg = self._ready_package(2)
        self.svc.decide_finding("1", pkg.id, "f0", "confirmed")
        with self.assertRaises(self.svc.ReviewError):
            self.svc.finalize_review("1", pkg.id)
        self.svc.decide_finding("1", pkg.id, "f1", "rejected")
        detail = self.svc.finalize_review("1", pkg.id)
        self.assertEqual(detail["status"], self.models.PackageStatus.REVIEWED.value)

    def test_finalize_with_zero_findings_succeeds(self):
        pkg = self.models.Package.create(client_id="3")
        pkg.status = self.models.PackageStatus.READY_FOR_REVIEW.value
        self.storage.save_package(pkg)
        detail = self.svc.finalize_review("3", pkg.id)
        self.assertEqual(detail["status"], self.models.PackageStatus.REVIEWED.value)

    def test_finalize_twice_raises(self):
        pkg = self._ready_package(1)
        self.svc.decide_finding("1", pkg.id, "f0", "confirmed")
        self.svc.finalize_review("1", pkg.id)
        with self.assertRaises(self.svc.ReviewError):
            self.svc.finalize_review("1", pkg.id)

    def test_changing_decision_after_finalize_reopens_package(self):
        pkg = self._ready_package(1)
        self.svc.decide_finding("1", pkg.id, "f0", "confirmed")
        self.svc.finalize_review("1", pkg.id)

        # оператор передумал после закрытия — пакет должен снова стать разбираемым,
        # а не тихо остаться "готов к отправке" со старым решением
        self.svc.decide_finding("1", pkg.id, "f0", "rejected")
        reloaded = self.storage.load_package("1", pkg.id)
        self.assertEqual(reloaded.status, self.models.PackageStatus.READY_FOR_REVIEW.value)
        self.assertEqual(reloaded.findings[0].decision, "rejected")

        # и может быть снова закрыт
        detail = self.svc.finalize_review("1", pkg.id)
        self.assertEqual(detail["status"], self.models.PackageStatus.REVIEWED.value)

    def test_pending_findings_lists_only_undecided(self):
        pkg = self._ready_package(2)
        self.svc.decide_finding("1", pkg.id, "f0", "confirmed")
        pending = self.svc.pending_findings("1", pkg.id)
        self.assertEqual([f["id"] for f in pending], ["f1"])


if __name__ == "__main__":
    unittest.main()

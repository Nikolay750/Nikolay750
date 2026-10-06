"""Step 5: связка очереди (Package) с движком автопроверки (smeta_check).

Юнит-тесты подменяют run_pipeline_for_package, чтобы не зависеть от реальных
смет/актов; test_end_to_end_on_real_mosoblles_files — отдельный интеграционный
тест на настоящих файлах "ЛСР Мособллес"/"КС-2 Мособллес" (пропускается, если
файлы не нашлись локально, например в CI без доступа к Projects)."""

import importlib
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

REAL_LSR = ("/root/.claude/projects/-home-claude/d7057cb8-63eb-5903-91cf-c01414915a74/"
            "tool-results/project-file-85125e97-332f-4d51-987a-726c29355ccd-"
            "_____________________._.xlsx")
REAL_KS2 = ("/root/.claude/projects/-home-claude/d7057cb8-63eb-5903-91cf-c01414915a74/"
            "tool-results/project-file-32f69928-b2d4-46a0-b120-1bf8cf73a05d-"
            "__-2__________.xlsx")


class CheckerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="checker_test_")
        os.environ["MINIAPP_DATA_DIR"] = self.tmp_dir
        for mod in ("app.storage", "app.checker", "app.models", "app.smeta_bridge"):
            sys.modules.pop(mod, None)
        self.storage = importlib.import_module("app.storage")
        self.models = importlib.import_module("app.models")
        self.checker = importlib.import_module("app.checker")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        os.environ.pop("MINIAPP_DATA_DIR", None)

    def _queued_package_with_file(self, name="a.xlsx", content=b"DATA"):
        pkg = self.models.Package.create(client_id="111", client_name="Клиент")
        fdir = self.storage.files_dir(pkg.client_id, pkg.id)
        (fdir / name).write_bytes(content)
        pkg.add_file(self.models.SourceFile(name=name, path=name, size=len(content), source="direct"))
        pkg.status = self.models.PackageStatus.QUEUED.value
        self.storage.save_package(pkg)
        return pkg

    def test_happy_path_moves_to_ready_for_review_with_findings(self):
        pkg = self._queued_package_with_file()

        fake_result = {
            "mode": "acts", "title": "Смета № 1 ↔ КС-2",
            "findings": [
                {"rule": "S-02", "status": "WARNING", "title": "Превышен объём",
                 "docs": "КС-2 №1", "where": "поз. 5", "text": "Объём больше сметного", "rub": 1000.0},
                {"rule": "S-01", "status": "OK", "title": "Позиции совпадают",
                 "docs": "—", "where": "—", "text": "Все позиции найдены в смете", "rub": None},
            ],
            "report": "report.xlsx",
        }

        with mock.patch.object(self.checker, "run_pipeline_for_package", return_value=fake_result):
            result = self.checker.process_package(pkg)

        self.assertEqual(result.status, self.models.PackageStatus.READY_FOR_REVIEW.value)
        self.assertEqual(len(result.findings), 2)
        self.assertEqual(result.findings[0].rule_id, "S-02")
        self.assertEqual(result.findings[0].status, "WARNING")
        self.assertIn("Превышен объём", result.findings[0].text)
        self.assertEqual(result.findings[0].decision, self.models.FindingDecision.PENDING.value)
        self.assertEqual(result.object_title, "Смета № 1 ↔ КС-2")
        self.assertEqual(result.report_path, "report.xlsx")
        self.assertIsNone(result.error)

        reloaded = self.storage.load_package(pkg.client_id, pkg.id)
        self.assertEqual(reloaded.status, self.models.PackageStatus.READY_FOR_REVIEW.value)
        self.assertEqual(len(reloaded.findings), 2)

    def test_pipeline_exception_sets_error_status_not_raises(self):
        pkg = self._queued_package_with_file()

        with mock.patch.object(self.checker, "run_pipeline_for_package", side_effect=RuntimeError("битый xlsx")):
            result = self.checker.process_package(pkg)

        self.assertEqual(result.status, self.models.PackageStatus.ERROR.value)
        self.assertIn("битый xlsx", result.error)

    def test_missing_file_on_disk_sets_error_without_calling_pipeline(self):
        pkg = self.models.Package.create(client_id="222")
        pkg.add_file(self.models.SourceFile(name="пропавший.xlsx", path="пропавший.xlsx", size=10))
        pkg.status = self.models.PackageStatus.QUEUED.value
        self.storage.save_package(pkg)

        with mock.patch.object(self.checker, "run_pipeline_for_package") as fake:
            result = self.checker.process_package(pkg)
            fake.assert_not_called()

        self.assertEqual(result.status, self.models.PackageStatus.ERROR.value)
        self.assertIn("пропавший.xlsx", result.error)

    def test_process_all_queued_only_touches_queued_packages(self):
        queued = self._queued_package_with_file("q.xlsx")
        other = self.models.Package.create(client_id="333")
        other.status = self.models.PackageStatus.NEW.value
        self.storage.save_package(other)

        fake_result = {"mode": "acts", "title": "T", "findings": [], "report": None}
        with mock.patch.object(self.checker, "run_pipeline_for_package", return_value=fake_result):
            processed = self.checker.process_all_queued()

        self.assertEqual([p.id for p in processed], [queued.id])
        still_new = self.storage.load_package("333", other.id)
        self.assertEqual(still_new.status, self.models.PackageStatus.NEW.value)

    @unittest.skipUnless(os.path.exists(REAL_LSR) and os.path.exists(REAL_KS2),
                          "реальные файлы Мособллес не найдены в этом окружении")
    def test_end_to_end_on_real_mosoblles_files(self):
        pkg = self.models.Package.create(client_id="444", client_name='ООО "Мособллес"')
        fdir = self.storage.files_dir(pkg.client_id, pkg.id)
        shutil.copy(REAL_LSR, fdir / "ЛСР Мособллес с пониж.к.xlsx")
        shutil.copy(REAL_KS2, fdir / "КС-2 Мособллес.xlsx")
        pkg.add_file(self.models.SourceFile(name="ЛСР Мособллес с пониж.к.xlsx",
                                             path="ЛСР Мособллес с пониж.к.xlsx", size=1))
        pkg.add_file(self.models.SourceFile(name="КС-2 Мособллес.xlsx", path="КС-2 Мособллес.xlsx", size=1))
        pkg.status = self.models.PackageStatus.QUEUED.value
        self.storage.save_package(pkg)

        result = self.checker.process_package(pkg)

        self.assertEqual(result.status, self.models.PackageStatus.READY_FOR_REVIEW.value)
        self.assertGreater(len(result.findings), 0)
        statuses = {f.status for f in result.findings}
        self.assertTrue(statuses & {"WARNING", "NEEDS_CONTEXT", "OK", "INFO", "FAIL"})
        report = fdir.parent / "report.xlsx"
        self.assertTrue(report.exists())


if __name__ == "__main__":
    unittest.main()

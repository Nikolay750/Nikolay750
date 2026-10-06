"""Step 4: приём пакета от клиента — бизнес-логика без aiogram.

aiogram не установлен в этой песочнице (нет сетевого доступа к PyPI для
новых пакетов — pypi.org отвечает 403 даже напрямую, не только через
agent-прокси; подтверждено отдельно). Поэтому вся логика диалога с клиентом
вынесена в app/package_service.py и тестируется здесь напрямую; bot/bot.py
— тонкая, не протестированная автоматически обвязка поверх неё (см. её
докстринг и README) — нужен смоук-тест на реальном токене бота перед продом.
"""

import importlib
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.models import PackageStatus
from app.ingest.errors import RemoteUnavailableError


class PackageServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="pkgservice_test_")
        os.environ["MINIAPP_DATA_DIR"] = self.tmp_dir
        for mod in ("app.storage", "app.package_service", "app.models"):
            sys.modules.pop(mod, None)
        self.storage = importlib.import_module("app.storage")
        self.svc = importlib.import_module("app.package_service")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        os.environ.pop("MINIAPP_DATA_DIR", None)

    def _write_tmp_file(self, name: str, content: bytes) -> str:
        p = Path(self.tmp_dir) / "incoming" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        return str(p)

    def test_direct_files_accumulate_into_one_draft(self):
        tmp1 = self._write_tmp_file("a.pdf", b"AAA")
        tmp2 = self._write_tmp_file("b.xlsx", b"BBB")

        pkg1 = self.svc.add_direct_file("111", "Клиент", "a.pdf", tmp1, 3)
        pkg2 = self.svc.add_direct_file("111", "Клиент", "b.xlsx", tmp2, 3)

        self.assertEqual(pkg1.id, pkg2.id)  # второй файл попал в тот же черновик
        self.assertEqual(pkg2.status, PackageStatus.NEW.value)
        self.assertEqual(len(pkg2.files), 2)

    def test_different_clients_get_different_drafts(self):
        tmp1 = self._write_tmp_file("a.pdf", b"AAA")
        tmp2 = self._write_tmp_file("a.pdf", b"ZZZ")

        pkg_a = self.svc.add_direct_file("A", None, "a.pdf", tmp1, 3)
        pkg_b = self.svc.add_direct_file("B", None, "a.pdf", tmp2, 3)

        self.assertNotEqual(pkg_a.id, pkg_b.id)
        self.assertEqual(pkg_a.client_id, "A")
        self.assertEqual(pkg_b.client_id, "B")

    def test_name_collision_within_draft_is_deduplicated(self):
        tmp1 = self._write_tmp_file("dup1.pdf", b"ONE")
        tmp2 = self._write_tmp_file("dup2.pdf", b"TWO")

        self.svc.add_direct_file("222", None, "акт.pdf", tmp1, 3)
        pkg = self.svc.add_direct_file("222", None, "акт.pdf", tmp2, 3)

        names = sorted(f.name for f in pkg.files)
        self.assertEqual(names, ["акт (1).pdf", "акт.pdf"])

    def test_finalize_without_draft_raises(self):
        with self.assertRaises(self.svc.PackageServiceError):
            self.svc.finalize_draft("no-such-client")

    def test_finalize_without_files_raises(self):
        self.svc.get_or_create_draft("333", "Клиент")
        with self.assertRaises(self.svc.PackageServiceError):
            self.svc.finalize_draft("333")

    def test_finalize_queues_draft_and_starts_new_one(self):
        tmp1 = self._write_tmp_file("a.pdf", b"AAA")
        self.svc.add_direct_file("444", None, "a.pdf", tmp1, 3)

        queued = self.svc.finalize_draft("444")
        self.assertEqual(queued.status, PackageStatus.QUEUED.value)

        # следующий присланный файл должен уйти в НОВЫЙ черновик, не в завершённый
        tmp2 = self._write_tmp_file("b.pdf", b"BBB")
        pkg2 = self.svc.add_direct_file("444", None, "b.pdf", tmp2, 3)
        self.assertNotEqual(pkg2.id, queued.id)
        self.assertEqual(pkg2.status, PackageStatus.NEW.value)

    def test_cloud_link_happy_path_queues_immediately(self):
        fake_files = []

        def fake_fetch(link, dest_dir):
            p = dest_dir / "смета.xlsx"
            p.write_bytes(b"XLSX")
            from app.models import SourceFile
            f = SourceFile(name="смета.xlsx", path="смета.xlsx", size=4, source="yandex", origin_url=link)
            fake_files.append(f)
            return [f]

        with mock.patch.object(self.svc, "fetch_by_link", fake_fetch):
            pkg = self.svc.add_cloud_link("555", "Клиент", "https://yadi.sk/d/abc")

        self.assertEqual(pkg.status, PackageStatus.QUEUED.value)
        self.assertEqual(len(pkg.files), 1)
        self.assertEqual(pkg.files[0].source, "yandex")

    def test_cloud_link_failure_degrades_to_error_status_not_exception(self):
        def failing_fetch(link, dest_dir):
            raise RemoteUnavailableError("облако недоступно")

        with mock.patch.object(self.svc, "fetch_by_link", failing_fetch):
            pkg = self.svc.add_cloud_link("666", None, "https://cloud.mail.ru/public/x/y")

        self.assertEqual(pkg.status, PackageStatus.ERROR.value)
        self.assertIn("недоступно", pkg.error)

    def test_cloud_link_does_not_touch_existing_draft(self):
        tmp1 = self._write_tmp_file("a.pdf", b"AAA")
        draft = self.svc.add_direct_file("777", None, "a.pdf", tmp1, 3)

        def fake_fetch(link, dest_dir):
            p = dest_dir / "смета.xlsx"
            p.write_bytes(b"XLSX")
            from app.models import SourceFile
            return [SourceFile(name="смета.xlsx", path="смета.xlsx", size=4, source="yandex")]

        with mock.patch.object(self.svc, "fetch_by_link", fake_fetch):
            link_pkg = self.svc.add_cloud_link("777", None, "https://yadi.sk/d/abc")

        self.assertNotEqual(link_pkg.id, draft.id)
        reloaded_draft = self.storage.load_package("777", draft.id)
        self.assertEqual(reloaded_draft.status, PackageStatus.NEW.value)
        self.assertEqual(len(reloaded_draft.files), 1)


if __name__ == "__main__":
    unittest.main()

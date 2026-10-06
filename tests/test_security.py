"""Step 9: защита от path traversal — имена файлов и client_id, пришедшие
извне (Telegram, Яндекс Диск, Mail.ru), никогда не должны позволить выйти
за пределы каталога пакета на диске."""

import importlib
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from app.security import safe_client_id, safe_filename


class SecurityUtilsTestCase(unittest.TestCase):
    def test_path_traversal_in_filename_is_stripped(self):
        self.assertNotIn("..", safe_filename("../../../etc/cron.d/evil"))
        self.assertNotIn("/", safe_filename("../../../etc/cron.d/evil"))

    def test_windows_style_traversal_is_stripped(self):
        name = safe_filename("..\\..\\windows\\system32\\evil.exe")
        self.assertNotIn("\\", name)
        self.assertNotIn("..", name)

    def test_absolute_path_collapses_to_basename(self):
        self.assertEqual(safe_filename("/etc/passwd"), "passwd")

    def test_cyrillic_and_typical_id_punctuation_survive(self):
        self.assertEqual(safe_filename("АОСР № 12 (копия).pdf"), "АОСР № 12 (копия).pdf")

    def test_empty_or_dots_only_falls_back_to_default(self):
        self.assertEqual(safe_filename(""), "file")
        self.assertEqual(safe_filename("..."), "file")
        self.assertEqual(safe_filename(None), "file")

    def test_long_name_truncated(self):
        self.assertLessEqual(len(safe_filename("a" * 500)), 150)

    def test_client_id_traversal_is_stripped(self):
        self.assertNotIn("/", safe_client_id("../../other-client"))
        self.assertNotIn("..", safe_client_id("../../other-client"))

    def test_client_id_normal_telegram_id_unaffected(self):
        self.assertEqual(safe_client_id("123456789"), "123456789")


class StorageTraversalTestCase(unittest.TestCase):
    """end-to-end: подсунуть вредоносное имя через публичный API модулей и
    убедиться, что результат остаётся внутри каталога пакета."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="security_test_")
        os.environ["MINIAPP_DATA_DIR"] = self.tmp_dir
        for mod in ("app.storage", "app.package_service", "app.models", "app.security"):
            sys.modules.pop(mod, None)
        self.storage = importlib.import_module("app.storage")
        self.svc = importlib.import_module("app.package_service")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        os.environ.pop("MINIAPP_DATA_DIR", None)

    def test_malicious_direct_filename_stays_inside_package_dir(self):
        evil_dir = tempfile.mkdtemp(prefix="outside_")
        tmp_src = os.path.join(evil_dir, "payload")
        with open(tmp_src, "wb") as f:
            f.write(b"PAYLOAD")

        pkg = self.svc.add_direct_file("999", None, "../../../../tmp/evil_cron", tmp_src, 7)

        fdir = self.storage.files_dir(pkg.client_id, pkg.id)
        written = list(fdir.iterdir())
        self.assertEqual(len(written), 1)
        self.assertTrue(str(written[0]).startswith(str(fdir)))
        shutil.rmtree(evil_dir, ignore_errors=True)

    def test_malicious_client_id_cannot_escape_packages_dir(self):
        pkg = self.svc.add_direct_file("../../outside", None, "x.pdf",
                                        self._write_tmp(b"X"), 1)
        # сам Package.create уже санитизировал client_id — путь обязан
        # остаться под PACKAGES_DIR, не выше него
        pkg_root = self.storage._package_dir(pkg.client_id, pkg.id)
        self.assertTrue(str(pkg_root.resolve()).startswith(str(self.storage.PACKAGES_DIR.resolve())))

    def _write_tmp(self, content: bytes) -> str:
        fd, path = tempfile.mkstemp(dir=self.tmp_dir)
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        return path


if __name__ == "__main__":
    unittest.main()

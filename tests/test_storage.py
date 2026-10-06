"""
Step 1: проверка хранилища и модели данных.

Критерий из плана: создать пакет, перезапустить "сервер" (то есть — заново
импортировать модуль, читающий диск, без какого-либо in-memory состояния),
убедиться, что пакет не потерялся. Плюс: пакеты разных клиентов не путаются,
и находки/решения оператора переживают перезапуск.
"""

import importlib
import os
import shutil
import sys
import tempfile
import unittest


class StorageTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="miniapp_test_")
        os.environ["MINIAPP_DATA_DIR"] = self.tmp_dir
        # Чистая перезагрузка модулей, чтобы DATA_DIR пересчитался из env
        for mod in ("app.storage", "app.models"):
            sys.modules.pop(mod, None)
        self.models = importlib.import_module("app.models")
        self.storage = importlib.import_module("app.storage")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        os.environ.pop("MINIAPP_DATA_DIR", None)

    def _reload_storage(self):
        """Имитация перезапуска сервера: модуль хранилища переимпортируется,
        никакого in-memory состояния между вызовами не остаётся."""
        sys.modules.pop("app.storage", None)
        self.storage = importlib.import_module("app.storage")

    def test_create_and_persist_after_restart(self):
        models, storage = self.models, self.storage

        pkg = models.Package.create(client_id="111", client_name='ООО "СЭМ"', object_title="Гидросталь")
        pkg.add_file(models.SourceFile(name="АОСР.pdf", path="files/aosr.pdf", size=1234, source="yandex"))
        storage.save_package(pkg)

        self._reload_storage()

        loaded = self.storage.load_package("111", pkg.id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.id, pkg.id)
        self.assertEqual(loaded.client_name, 'ООО "СЭМ"')
        self.assertEqual(len(loaded.files), 1)
        self.assertEqual(loaded.files[0].name, "АОСР.pdf")

    def test_clients_are_isolated(self):
        models, storage = self.models, self.storage

        pkg_a = models.Package.create(client_id="A", client_name="Клиент A")
        pkg_b = models.Package.create(client_id="B", client_name="Клиент B")
        storage.save_package(pkg_a)
        storage.save_package(pkg_b)

        self._reload_storage()

        only_a = self.storage.list_packages_for_client("A")
        only_b = self.storage.list_packages_for_client("B")
        self.assertEqual([p.id for p in only_a], [pkg_a.id])
        self.assertEqual([p.id for p in only_b], [pkg_b.id])

        all_pkgs = self.storage.list_all_packages()
        self.assertEqual(sorted(p.id for p in all_pkgs), sorted([pkg_a.id, pkg_b.id]))

    def test_findings_and_decisions_survive_restart(self):
        models, storage = self.models, self.storage

        pkg = models.Package.create(client_id="222")
        finding = models.Finding(
            id=models.new_id(),
            rule_id="rule-1-chain",
            status=models.FindingStatus.FAIL.value,
            text="Акт №12 ссылается на несогласованный акт №11",
            source_docs=["Акт №12", "Акт №11"],
        )
        pkg.set_findings([finding])
        storage.save_package(pkg)

        self._reload_storage()
        loaded = self.storage.load_package("222", pkg.id)
        loaded.findings[0].decision = models.FindingDecision.EDITED.value
        loaded.findings[0].edited_text = "Уточнённый текст замечания"
        self.storage.save_package(loaded)

        self._reload_storage()
        reloaded = self.storage.load_package("222", pkg.id)
        self.assertEqual(reloaded.findings[0].decision, "edited")
        self.assertEqual(reloaded.findings[0].final_text(), "Уточнённый текст замечания")

    def test_find_package_without_known_client(self):
        models, storage = self.models, self.storage
        pkg = models.Package.create(client_id="333")
        storage.save_package(pkg)
        self._reload_storage()
        found = self.storage.find_package(pkg.id)
        self.assertIsNotNone(found)
        self.assertEqual(found.client_id, "333")


if __name__ == "__main__":
    unittest.main()

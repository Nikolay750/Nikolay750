"""Step 2: адаптер Яндекс Диска.

Сеть к cloud-api.yandex.net из песочницы разработки закрыта политикой egress-
прокси, поэтому здесь HTTP-сессия подменяется фейком, воспроизводящим реальный
контракт API (см. docs: https://yandex.ru/dev/disk/api/reference/public.html).
Перед реальным запуском в проде нужна ручная проверка на живой публичной ссылке.
"""

import io
import os
import tempfile
import unittest
from pathlib import Path

from app.ingest import yandex
from app.ingest.errors import IngestError, RemoteNotFoundError, RemoteUnavailableError, UnsupportedLinkError


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, content=b"", chunk_size=1024 * 64):
        self.status_code = status_code
        self._json = json_data or {}
        self._content = content
        self._chunk_size = chunk_size

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=1024 * 256):
        buf = io.BytesIO(self._content)
        while True:
            chunk = buf.read(chunk_size)
            if not chunk:
                break
            yield chunk

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeSession:
    """Отвечает по словарю {(url, frozenset(params.items())): FakeResponse}
    плюс отдельная карта href -> содержимое файла (для шага download по href)."""

    def __init__(self, metadata_routes: dict, file_contents: dict):
        self.metadata_routes = metadata_routes
        self.file_contents = file_contents
        self.calls = []

    def get(self, url, params=None, timeout=None, stream=False):
        params = params or {}
        self.calls.append((url, dict(params)))
        if url in self.file_contents:
            return FakeResponse(200, content=self.file_contents[url])
        key = (url, tuple(sorted(params.items())))
        if key in self.metadata_routes:
            return self.metadata_routes[key]
        # падение по умолчанию — похоже на реальный ответ "не найдено"
        return FakeResponse(404, json_data={"message": "not found"})


class YandexAdapterTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="yadisk_test_"))

    def test_is_yandex_disk_link(self):
        self.assertTrue(yandex.is_yandex_disk_link("https://yadi.sk/d/abc123"))
        self.assertTrue(yandex.is_yandex_disk_link("https://disk.yandex.ru/d/abc123"))
        self.assertFalse(yandex.is_yandex_disk_link("https://cloud.mail.ru/public/abc/def"))

    def test_unsupported_link_raises(self):
        session = FakeSession({}, {})
        with self.assertRaises(UnsupportedLinkError):
            yandex.fetch_public_resource("https://cloud.mail.ru/public/x/y", self.tmp, session)

    def test_single_public_file(self):
        public_url = "https://yadi.sk/d/singlefile"
        content = b"PDF-CONTENT-BYTES"
        meta_key = (yandex.API_BASE, tuple(sorted({"public_key": public_url, "limit": 0}.items())))
        download_key = (yandex.DOWNLOAD_API, tuple(sorted({"public_key": public_url}.items())))
        href = "https://downloader.example/singlefile.pdf"
        session = FakeSession(
            metadata_routes={
                meta_key: FakeResponse(200, json_data={"type": "file", "name": "АОСР.pdf"}),
                download_key: FakeResponse(200, json_data={"href": href}),
            },
            file_contents={href: content},
        )

        files = yandex.fetch_public_resource(public_url, self.tmp, session)

        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].name, "АОСР.pdf")
        self.assertEqual(files[0].source, "yandex")
        saved = self.tmp / "АОСР.pdf"
        self.assertTrue(saved.exists())
        self.assertEqual(saved.read_bytes(), content)

    def test_public_folder_recursive(self):
        public_url = "https://yadi.sk/d/folder1"

        def meta_key(path, limit=0, offset=None):
            params = {"public_key": public_url, "limit": limit, "path": path}
            if offset is not None:
                params["offset"] = offset
            return (yandex.API_BASE, tuple(sorted(params.items())))

        root_meta_key = (yandex.API_BASE, tuple(sorted({"public_key": public_url, "limit": 0}.items())))
        list_root_key = (yandex.API_BASE, tuple(sorted(
            {"public_key": public_url, "path": "/", "limit": yandex.LIST_PAGE_LIMIT, "offset": 0}.items())))
        list_sub_key = (yandex.API_BASE, tuple(sorted(
            {"public_key": public_url, "path": "/sub", "limit": yandex.LIST_PAGE_LIMIT, "offset": 0}.items())))

        def dl_key(path):
            return (yandex.DOWNLOAD_API, tuple(sorted({"public_key": public_url, "path": path}.items())))

        href_a = "https://downloader.example/a.xlsx"
        href_b = "https://downloader.example/b.xlsx"

        session = FakeSession(
            metadata_routes={
                root_meta_key: FakeResponse(200, json_data={"type": "dir", "name": "Пакет"}),
                list_root_key: FakeResponse(200, json_data={"_embedded": {"total": 2, "items": [
                    {"type": "file", "name": "a.xlsx", "path": "/a.xlsx"},
                    {"type": "dir", "name": "sub", "path": "/sub"},
                ]}}),
                list_sub_key: FakeResponse(200, json_data={"_embedded": {"total": 1, "items": [
                    {"type": "file", "name": "b.xlsx", "path": "/sub/b.xlsx"},
                ]}}),
                dl_key("/a.xlsx"): FakeResponse(200, json_data={"href": href_a}),
                dl_key("/sub/b.xlsx"): FakeResponse(200, json_data={"href": href_b}),
            },
            file_contents={href_a: b"AAA", href_b: b"BBB"},
        )

        files = yandex.fetch_public_resource(public_url, self.tmp, session)

        names = sorted(f.name for f in files)
        self.assertEqual(names, ["a.xlsx", "b.xlsx"])
        self.assertEqual((self.tmp / "a.xlsx").read_bytes(), b"AAA")
        self.assertEqual((self.tmp / "b.xlsx").read_bytes(), b"BBB")

    def test_name_collision_is_deduplicated(self):
        public_url = "https://yadi.sk/d/dupes"
        root_meta_key = (yandex.API_BASE, tuple(sorted({"public_key": public_url, "limit": 0}.items())))
        list_root_key = (yandex.API_BASE, tuple(sorted(
            {"public_key": public_url, "path": "/", "limit": yandex.LIST_PAGE_LIMIT, "offset": 0}.items())))
        list_sub_key = (yandex.API_BASE, tuple(sorted(
            {"public_key": public_url, "path": "/sub", "limit": yandex.LIST_PAGE_LIMIT, "offset": 0}.items())))

        def dl_key(path):
            return (yandex.DOWNLOAD_API, tuple(sorted({"public_key": public_url, "path": path}.items())))

        href1 = "https://downloader.example/1.pdf"
        href2 = "https://downloader.example/2.pdf"
        session = FakeSession(
            metadata_routes={
                root_meta_key: FakeResponse(200, json_data={"type": "dir", "name": "Пакет"}),
                list_root_key: FakeResponse(200, json_data={"_embedded": {"total": 2, "items": [
                    {"type": "file", "name": "акт.pdf", "path": "/акт.pdf"},
                    {"type": "dir", "name": "sub", "path": "/sub"},
                ]}}),
                list_sub_key: FakeResponse(200, json_data={"_embedded": {"total": 1, "items": [
                    {"type": "file", "name": "акт.pdf", "path": "/sub/акт.pdf"},
                ]}}),
                dl_key("/акт.pdf"): FakeResponse(200, json_data={"href": href1}),
                dl_key("/sub/акт.pdf"): FakeResponse(200, json_data={"href": href2}),
            },
            file_contents={href1: b"ONE", href2: b"TWO"},
        )

        files = yandex.fetch_public_resource(public_url, self.tmp, session)
        names = sorted(f.name for f in files)
        self.assertEqual(names, ["акт (1).pdf", "акт.pdf"])

    def test_not_found_raises_remote_not_found(self):
        public_url = "https://yadi.sk/d/gone"
        session = FakeSession(
            metadata_routes={
                (yandex.API_BASE, tuple(sorted({"public_key": public_url, "limit": 0}.items()))):
                    FakeResponse(404, json_data={"message": "not found"}),
            },
            file_contents={},
        )
        with self.assertRaises(RemoteNotFoundError):
            yandex.fetch_public_resource(public_url, self.tmp, session)

    def test_file_over_limit_is_aborted_and_cleaned_up(self):
        public_url = "https://yadi.sk/d/bigfile"
        meta_key = (yandex.API_BASE, tuple(sorted({"public_key": public_url, "limit": 0}.items())))
        download_key = (yandex.DOWNLOAD_API, tuple(sorted({"public_key": public_url}.items())))
        href = "https://downloader.example/big.pdf"
        big_content = b"X" * 1000
        session = FakeSession(
            metadata_routes={
                meta_key: FakeResponse(200, json_data={"type": "file", "name": "big.pdf"}),
                download_key: FakeResponse(200, json_data={"href": href}),
            },
            file_contents={href: big_content},
        )
        with self.assertRaises(IngestError):
            yandex.fetch_public_resource(public_url, self.tmp, session, max_file_bytes=500)
        self.assertFalse((self.tmp / "big.pdf").exists())

    def test_network_failure_maps_to_remote_unavailable(self):
        public_url = "https://yadi.sk/d/network-down"

        class BrokenSession:
            def get(self, *a, **kw):
                raise ConnectionError("no route to host")

        with self.assertRaises(RemoteUnavailableError):
            yandex.fetch_public_resource(public_url, self.tmp, BrokenSession())


if __name__ == "__main__":
    unittest.main()

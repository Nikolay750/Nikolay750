"""Step 3: адаптер Облака Mail.ru.

У Mail.ru нет официального API — схема в app/ingest/mailru.py взята из
reverse-engineering и НЕ подтверждена живым вызовом (сеть закрыта политикой
прокси в этой песочнице, см. docstring модуля). Поэтому здесь проверяется
не точность конкретных URL (это можно будет сделать только на реальной
ссылке), а то, что заявлено архитектурой как обязательное свойство:
при ЛЮБОМ расхождении формата ответа адаптер поднимает IngestError (и его
подклассы), а не падает необработанным исключением — чтобы вызывающий
код (bot) мог надёжно откатиться на "пришлите файлы напрямую".
"""

import tempfile
import unittest
from pathlib import Path

from app.ingest import mailru
from app.ingest.errors import IngestError, RemoteNotFoundError, RemoteUnavailableError, UnsupportedLinkError


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, content=b""):
        self.status_code = status_code
        self._json = json_data
        self._content = content

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json

    def iter_content(self, chunk_size=1024 * 256):
        for i in range(0, len(self._content), chunk_size):
            yield self._content[i:i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeSession:
    def __init__(self, routes: dict):
        self.routes = routes  # {(url_prefix): FakeResponse or callable(params)->FakeResponse}
        self.calls = []

    def get(self, url, params=None, timeout=None, stream=False):
        self.calls.append((url, params or {}))
        for prefix, handler in self.routes.items():
            if url.startswith(prefix):
                return handler(params or {}) if callable(handler) else handler
        return FakeResponse(404, json_data={})


DISPATCHER_OK = FakeResponse(200, json_data={"body": {"get": [{"url": "https://cloclo15.datacloudmail.ru/get/"}]}})


class MailruAdapterTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mailru_test_"))

    def test_is_mailru_link(self):
        self.assertTrue(mailru.is_mailru_link("https://cloud.mail.ru/public/AbCd/XyZ123"))
        self.assertFalse(mailru.is_mailru_link("https://yadi.sk/d/abc"))

    def test_unsupported_link_raises(self):
        session = FakeSession({})
        with self.assertRaises(UnsupportedLinkError):
            mailru.fetch_public_resource("https://yadi.sk/d/abc", self.tmp, session)

    def test_single_public_file_happy_path(self):
        public_url = "https://cloud.mail.ru/public/AbCd/file.pdf"
        content = b"HELLO-PDF"

        def folder_handler(params):
            self.assertEqual(params.get("weblink"), "AbCd/file.pdf")
            return FakeResponse(200, json_data={"body": {"type": "file", "name": "АОСР.pdf"}})

        session = FakeSession({
            mailru.FOLDER_API: folder_handler,
            mailru.DISPATCHER_API: DISPATCHER_OK,
            "https://cloclo15.datacloudmail.ru/get/": FakeResponse(200, content=content),
        })

        files = mailru.fetch_public_resource(public_url, self.tmp, session)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].name, "АОСР.pdf")
        self.assertEqual(files[0].source, "mailru")
        self.assertEqual((self.tmp / "АОСР.pdf").read_bytes(), content)

    def test_folder_downloads_only_files(self):
        public_url = "https://cloud.mail.ru/public/AbCd/folder1"

        def folder_handler(params):
            return FakeResponse(200, json_data={"body": {"type": "folder", "list": [
                {"type": "file", "name": "a.xlsx", "weblink": "AbCd/folder1/a.xlsx"},
                {"type": "folder", "name": "вложенная папка"},
                {"type": "file", "name": "b.xlsx", "weblink": "AbCd/folder1/b.xlsx"},
            ]}})

        session = FakeSession({
            mailru.FOLDER_API: folder_handler,
            mailru.DISPATCHER_API: DISPATCHER_OK,
            "https://cloclo15.datacloudmail.ru/get/AbCd/folder1/a.xlsx": FakeResponse(200, content=b"AAA"),
            "https://cloclo15.datacloudmail.ru/get/AbCd/folder1/b.xlsx": FakeResponse(200, content=b"BBB"),
        })

        files = mailru.fetch_public_resource(public_url, self.tmp, session)
        names = sorted(f.name for f in files)
        self.assertEqual(names, ["a.xlsx", "b.xlsx"])

    def test_unexpected_response_shape_degrades_to_ingest_error(self):
        """Ключевое свойство: формат ответа мог измениться у Mail.ru — адаптер
        не должен падать необработанным исключением, только IngestError."""
        public_url = "https://cloud.mail.ru/public/AbCd/weird"

        def folder_handler(params):
            # ни "type", ни ожидаемых полей — имитация изменившегося формата ответа
            return FakeResponse(200, json_data={"body": {"surprise": True}})

        session = FakeSession({
            mailru.FOLDER_API: folder_handler,
            mailru.DISPATCHER_API: DISPATCHER_OK,
        })

        with self.assertRaises(IngestError):
            mailru.fetch_public_resource(public_url, self.tmp, session)

    def test_dispatcher_failure_degrades_gracefully(self):
        public_url = "https://cloud.mail.ru/public/AbCd/file.pdf"
        session = FakeSession({
            mailru.FOLDER_API: FakeResponse(200, json_data={"body": {"type": "file", "name": "x.pdf"}}),
            mailru.DISPATCHER_API: FakeResponse(200, json_data={"body": {}}),  # нет "get"
        })
        with self.assertRaises(RemoteUnavailableError):
            mailru.fetch_public_resource(public_url, self.tmp, session)

    def test_not_found(self):
        public_url = "https://cloud.mail.ru/public/AbCd/gone"
        session = FakeSession({mailru.FOLDER_API: FakeResponse(404, json_data={})})
        with self.assertRaises(RemoteNotFoundError):
            mailru.fetch_public_resource(public_url, self.tmp, session)

    def test_network_failure_maps_to_remote_unavailable(self):
        public_url = "https://cloud.mail.ru/public/AbCd/x"

        class BrokenSession:
            def get(self, *a, **kw):
                raise ConnectionError("dns fail")

        with self.assertRaises(RemoteUnavailableError):
            mailru.fetch_public_resource(public_url, self.tmp, BrokenSession())


if __name__ == "__main__":
    unittest.main()

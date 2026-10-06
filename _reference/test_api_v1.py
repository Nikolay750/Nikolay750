"""Проверка HTTP-слоя (нужен FastAPI):  pip install -r requirements.txt && python -m unittest -v tests.test_api"""
import os
import tempfile
import time
import unittest

from tests.test_core import TOKEN, make_init_data


class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app import config
        config.DATA_DIR, config.BOT_TOKEN, config.DEV_MODE = tempfile.mkdtemp(), TOKEN, False
        from fastapi.testclient import TestClient
        from app.main import app
        cls.c = TestClient(app)

    def test_index(self):
        self.assertIn("Проверка пакета", self.c.get("/").text)

    def test_no_auth(self):
        self.assertEqual(self.c.get("/api/jobs").status_code, 401)

    def test_full_cycle(self):
        h = {"X-Tg-Init-Data": make_init_data(user_id=5)}
        paths = ["/mnt/project/ЛСР_Мособллес_с_пониж_к.xlsx", "/mnt/project/КС-2_Мособллес.xlsx"]
        files = [("files", (os.path.basename(p), open(p, "rb"))) for p in paths if os.path.exists(p)]
        if len(files) < 2:
            self.skipTest("нет тестовых файлов — подставьте свои ЛСР и КС-2")
        jid = self.c.post("/api/jobs", files=files, data={"title": "тест"}, headers=h).json()["id"]
        for _ in range(120):
            j = self.c.get(f"/api/jobs/{jid}", headers=h).json()
            if j["status"] in ("done", "error"):
                break
            time.sleep(0.5)
        self.assertEqual(j["status"], "done", j.get("error"))
        r = self.c.get(j["report_url"])
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.content.startswith(b"PK"))                      # это xlsx
        other = {"X-Tg-Init-Data": make_init_data(user_id=6)}
        self.assertEqual(self.c.get(f"/api/jobs/{jid}", headers=other).status_code, 404)
        self.assertEqual(self.c.get(f"/api/jobs/{jid}/report?t=1.bad").status_code, 403)


if __name__ == "__main__":
    unittest.main()

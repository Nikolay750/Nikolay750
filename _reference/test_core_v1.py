"""python -m unittest -v tests.test_core   (без FastAPI: проверяет подпись, безопасность и цикл задания)"""
import hashlib
import hmac
import io
import json
import os
import shutil
import tempfile
import time
import unittest
from urllib.parse import urlencode

TOKEN = "123456:TEST-token"


def make_init_data(user_id=42, token=TOKEN, auth_date=None):
    """Так initData подписывает сам Telegram."""
    fields = {"auth_date": str(auth_date or int(time.time())), "query_id": "AAH",
              "user": json.dumps({"id": user_id, "first_name": "Иван"}, ensure_ascii=False)}
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class Auth(unittest.TestCase):
    def test_valid(self):
        from app.tg_auth import validate_init_data
        self.assertEqual(validate_init_data(make_init_data(), TOKEN)["id"], 42)

    def test_tampered_user(self):
        from app.tg_auth import AuthError, validate_init_data
        bad = make_init_data().replace("42", "43")
        self.assertRaises(AuthError, validate_init_data, bad, TOKEN)

    def test_other_bot(self):
        from app.tg_auth import AuthError, validate_init_data
        self.assertRaises(AuthError, validate_init_data, make_init_data(token="999:other"), TOKEN)

    def test_expired(self):
        from app.tg_auth import AuthError, validate_init_data
        old = make_init_data(auth_date=int(time.time()) - 2 * 86400)
        self.assertRaises(AuthError, validate_init_data, old, TOKEN, 86400)

    def test_report_link(self):
        from app.tg_auth import check_link, sign_link
        t = sign_link("s", "job1")
        self.assertTrue(check_link("s", "job1", t))
        self.assertFalse(check_link("s", "job2", t))
        self.assertFalse(check_link("s", "job1", t[:-1] + ("0" if t[-1] != "0" else "1")))
        self.assertFalse(check_link("s", "job1", sign_link("s", "job1", ttl=-1)))


class Jobs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app import config
        cls.tmp = tempfile.mkdtemp()
        config.DATA_DIR, config.BOT_TOKEN, config.MAX_UPLOAD_MB = cls.tmp, "", 500

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_safe_name(self):
        from app.service import safe_name
        self.assertEqual(safe_name("../../etc/passwd"), "passwd")
        self.assertEqual(safe_name("C:\\Users\\x\\КС-2 №1.xlsx"), "КС-2 №1.xlsx")

    def test_bad_ext(self):
        from app import service
        self.assertRaises(service.UploadError, service.create_job, {"id": 1}, [("a.exe", io.BytesIO(b"x"))])

    def test_limit(self):
        from app import config, service
        config.MAX_UPLOAD_MB = 50
        try:
            before = set(os.listdir(service._jobs_dir()))
            big = io.BytesIO(b"0" * (51 * 1024 * 1024))
            self.assertRaises(service.UploadError, service.create_job, {"id": 1}, [("a.pdf", big)])
            self.assertEqual(set(os.listdir(service._jobs_dir())), before)   # недогруженный пакет удалён
        finally:
            config.MAX_UPLOAD_MB = 500

    def _wait(self, jid, uid):
        from app import service
        for _ in range(240):
            m = service.get_job(jid, uid)
            if m["status"] in ("done", "error"):
                return m
            time.sleep(0.5)
        self.fail("timeout")

    def test_acts_package(self):
        from app import service
        files = [(os.path.basename(p), open(p, "rb")) for p in
                 ["/mnt/project/ЛСР_Мособллес_с_пониж_к.xlsx", "/mnt/project/КС-2_Мособллес.xlsx"]]
        jid = service.create_job({"id": 7}, files, "Мособллес")
        [f.close() for _, f in files]
        m = self._wait(jid, 7)
        self.assertEqual(m["status"], "done", m.get("error"))
        self.assertEqual(m["result"]["mode"], "acts")
        self.assertGreater(m["result"]["over_rub"], 90000)
        self.assertTrue(os.path.exists(service.report_path(jid)))
        self.assertRaises(KeyError, service.get_job, jid, 8)          # чужой пользователь не видит
        self.assertEqual(service.list_jobs(7)[0]["id"], jid)
        self.assertEqual(service.list_jobs(8), [])

    def test_contract_package_with_scan(self):
        from app import service
        U = "/mnt/user-data/uploads/"
        files = [(n, open(U + n, "rb")) for n in ["Д_37к_2026_от_12_08_2026.pdf", "Скан_ИД_ЭМ-1.pdf"]]
        jid = service.create_job({"id": 9}, files)
        [f.close() for _, f in files]
        m = self._wait(jid, 9)
        self.assertEqual(m["status"], "done", m.get("error"))
        r = m["result"]
        self.assertEqual(r["mode"], "contract")
        self.assertEqual(len(r["plan"]), 54)
        self.assertIn("Скан без текстового слоя", [f["title"] for f in r["findings"]])
        json.dumps(r, ensure_ascii=False)                               # ответ API сериализуется


if __name__ == "__main__":
    unittest.main()

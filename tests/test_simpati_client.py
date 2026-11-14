import hashlib
import hmac
import json
import unittest

import requests

from backend.simpati_client import AccessDenied, SimpatiClient, SurveyClient, UpstreamError

TOKEN = "integration-token-123"
PROFILE = {"username": "dps.tambang", "province": "RIAU", "regency": "KABUPATEN KAMPAR", "district": "TAMBANG",
           "territory": "Kecamatan Tambang, Kabupaten Kampar, Provinsi Riau", "chair": "Budi", "secretary": "Siti",
           "account_version": "v1", "survei_module_allowed": True}


def sign(body, token=TOKEN):
    canon = json.dumps(body, sort_keys=True, separators=(",", ":"))
    out = dict(body)
    out["signature"] = hmac.new(token.encode(), canon.encode(), hashlib.sha256).hexdigest()
    return out


class Resp:
    def __init__(self, status, data=None, headers=None, text=None):
        self.status_code, self._data, self.headers, self.text = status, data, headers or {}, text

    def json(self):
        if self._data is None:
            raise ValueError("bukan json")
        return self._data


class FakeHttp:
    """Meniru server: membaca nonce permintaan lalu menjawab lewat fungsi `answer(nonce, request)`."""

    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def _nonce(self, kw):
        return (kw.get("json") or kw.get("params") or {}).get("nonce")

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        return self.answer(self._nonce(kw), kw)

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        return self.answer(self._nonce(kw), kw)


class SimpatiTests(unittest.TestCase):
    def client(self, answer):
        return SimpatiClient("https://simpati.test/", TOKEN, 5, http=FakeHttp(answer))

    def test_login_ok_sends_token_and_nonce(self):
        c = self.client(lambda n, kw: Resp(200, sign({"ok": True, "profile": PROFILE, "nonce": n})))
        self.assertEqual(c.login("dps.tambang", "pw")["district"], "TAMBANG")
        _, url, kw = c.http.calls[0]
        self.assertEqual(url, "https://simpati.test/api/integrasi/proposal/login")
        self.assertEqual(kw["headers"]["X-SIMPATI-Token"], TOKEN)
        self.assertEqual(kw["json"]["username"], "dps.tambang")
        self.assertGreaterEqual(len(kw["json"]["nonce"]), 16)

    def test_denied_reason(self):
        c = self.client(lambda n, kw: Resp(403, sign({"ok": False, "reason": "must_change_password", "nonce": n})))
        with self.assertRaises(AccessDenied) as ctx:
            c.login("a", "b")
        self.assertEqual(ctx.exception.reason, "must_change_password")

    def test_forged_signature_rejected(self):
        c = self.client(lambda n, kw: Resp(200, sign({"ok": True, "profile": PROFILE, "nonce": n}, token="salah")))
        with self.assertRaises(UpstreamError):
            c.login("a", "b")

    def test_replayed_nonce_rejected(self):
        old = sign({"ok": True, "profile": PROFILE, "nonce": "nonce-lama-1234567890"})
        c = self.client(lambda n, kw: Resp(200, old))
        with self.assertRaises(UpstreamError):
            c.login("a", "b")

    def test_unsigned_error_not_trusted(self):
        c = self.client(lambda n, kw: Resp(403, {"detail": "Forbidden"}))
        with self.assertRaises(UpstreamError):
            c.login("a", "b")

    def test_token_mismatch_and_lockout_and_network(self):
        with self.assertRaises(UpstreamError):
            self.client(lambda n, kw: Resp(401, {"detail": "x"})).login("a", "b")
        with self.assertRaises(AccessDenied) as ctx:
            self.client(lambda n, kw: Resp(429, {"detail": "x"}, {"Retry-After": "120"})).login("a", "b")
        self.assertEqual((ctx.exception.reason, ctx.exception.retry_after), ("locked", 120))

        def boom(n, kw):
            raise requests.ConnectionError("down")
        with self.assertRaises(UpstreamError) as e2:
            self.client(boom).login("a", "b")
        self.assertEqual(e2.exception.status_code, 503)

    def test_non_json_and_incomplete_profile(self):
        with self.assertRaises(UpstreamError):
            self.client(lambda n, kw: Resp(502, None, text="<html>")).login("a", "b")
        bad = dict(PROFILE, district="")
        with self.assertRaises(UpstreamError):
            self.client(lambda n, kw: Resp(200, sign({"ok": True, "profile": bad, "nonce": n}))).login("a", "b")

    def test_not_configured(self):
        with self.assertRaises(UpstreamError):
            SimpatiClient("", "", 5, http=FakeHttp(lambda n, kw: None)).login("a", "b")


class SurveyTests(unittest.TestCase):
    def client(self, answer):
        return SurveyClient("https://survey.test", TOKEN, 5, http=FakeHttp(answer))

    def test_list_uses_scope_from_profile_only(self):
        c = self.client(lambda n, kw: Resp(200, sign({"items": [{"id": "7"}], "nonce": n})))
        self.assertEqual(c.list_verified(PROFILE), [{"id": "7"}])
        _, url, kw = c.http.calls[0]
        self.assertEqual(url, "https://survey.test/api/integrasi/proposal/verified-results")
        self.assertEqual({k: kw["params"][k] for k in ("province", "regency", "district")},
                         {"province": "RIAU", "regency": "KABUPATEN KAMPAR", "district": "TAMBANG"})

    def test_detail_ok_and_not_found(self):
        rec = {"id": "7", "applicant_name": "Ani"}
        c = self.client(lambda n, kw: Resp(200, sign({"ok": True, "record": rec, "nonce": n})))
        self.assertEqual(c.detail(PROFILE, 7), rec)
        c = self.client(lambda n, kw: Resp(404, sign({"ok": False, "reason": "not_found", "nonce": n})))
        self.assertIsNone(c.detail(PROFILE, 8))

    def test_old_survey_server_without_endpoint(self):
        c = self.client(lambda n, kw: Resp(404, {"detail": "Not Found"}))
        with self.assertRaises(UpstreamError):
            c.list_verified(PROFILE)

    def test_tampered_items_rejected(self):
        def answer(n, kw):
            data = sign({"items": [{"id": "7"}], "nonce": n})
            data["items"] = [{"id": "999"}]
            return Resp(200, data)
        with self.assertRaises(UpstreamError):
            self.client(answer).list_verified(PROFILE)


if __name__ == "__main__":
    unittest.main()

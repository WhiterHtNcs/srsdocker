import base64
from http import HTTPStatus
from io import BytesIO
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import app


class JsonRequestBodyTests(unittest.TestCase):
    def make_handler(self, body):
        return SimpleNamespace(headers={"Content-Length": str(len(body))},
                               rfile=BytesIO(body), send_json=Mock())

    def test_invalid_utf8_returns_json_bad_request(self):
        for body in [b'\xff', b'{"name":"\xff"}', b'{"name":"\xe4\xb8"}']:
            with self.subTest(body=body):
                handler = self.make_handler(body)
                self.assertIsNone(app.AppHandler.read_json_body(handler))
                handler.send_json.assert_called_once_with(
                    {"error": "JSON body must use valid UTF-8."}, status=HTTPStatus.BAD_REQUEST)

    def test_valid_unicode_body_is_preserved(self):
        payload = {"name": "机场", "use_for_dns": True}
        handler = self.make_handler(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        self.assertEqual(app.AppHandler.read_json_body(handler), payload)
        handler.send_json.assert_not_called()

    def test_invalid_json_still_returns_json_bad_request(self):
        handler = self.make_handler(b'{"name":')
        self.assertIsNone(app.AppHandler.read_json_body(handler))
        handler.send_json.assert_called_once_with(
            {"error": "Invalid JSON body"}, status=HTTPStatus.BAD_REQUEST)


class WebAuthAndApiRoutingTests(unittest.TestCase):
    def test_basic_auth_disabled_by_default_and_enforced_when_configured(self):
        with patch.dict("os.environ", {}, clear=False):
            os_env_backup = app.os.environ.pop("WEB_PASSWORD", None)
            try:
                self.assertTrue(app.verify_basic_auth_header(None))
            finally:
                if os_env_backup is not None:
                    app.os.environ["WEB_PASSWORD"] = os_env_backup

        valid = "Basic " + base64.b64encode(b"ops:s3cr3t").decode("ascii")
        wrong = "Basic " + base64.b64encode(b"ops:wrong").decode("ascii")
        with patch.dict("os.environ", {"WEB_USERNAME": "ops", "WEB_PASSWORD": "s3cr3t"}, clear=False):
            self.assertFalse(app.verify_basic_auth_header(None))
            self.assertFalse(app.verify_basic_auth_header("Bearer token"))
            self.assertFalse(app.verify_basic_auth_header(wrong))
            self.assertTrue(app.verify_basic_auth_header(valid))

    def test_unknown_api_get_returns_json_not_found(self):
        handler = SimpleNamespace(
            path="/api/does-not-exist",
            headers={},
            check_auth=lambda: True,
            send_json=Mock(),
        )
        with patch.dict("os.environ", {"WEB_PASSWORD": ""}, clear=False):
            app.AppHandler.do_GET(handler)
        handler.send_json.assert_called_once_with({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)

    def test_unauthorized_request_sends_401_challenge(self):
        wfile = BytesIO()
        handler = SimpleNamespace(
            headers={},
            command="GET",
            wfile=wfile,
            send_response=Mock(),
            send_header=Mock(),
            end_headers=Mock(),
        )
        with patch.dict("os.environ", {"WEB_PASSWORD": "secret"}, clear=False):
            self.assertFalse(app.AppHandler.check_auth(handler))
        handler.send_response.assert_called_once_with(HTTPStatus.UNAUTHORIZED)
        self.assertEqual(json.loads(wfile.getvalue().decode("utf-8")), {"error": "Unauthorized"})


class RemoteSyncAndConcurrencyTests(unittest.TestCase):
    def test_remote_sync_uses_single_listing_skips_unchanged_sha_and_detects_missing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            geosite_dir = root / "geosite"
            geoip_dir = root / "geoip"
            geosite_dir.mkdir()
            geoip_dir.mkdir()

            existing_bytes = b'{"version": 3, "rules": [{"domain_suffix": ["cn"]}]}\n'
            (geosite_dir / "cn.json").write_bytes(existing_bytes)
            cn_sha = app.compute_git_blob_sha(geosite_dir / "cn.json")

            listing = [
                {
                    "type": "file",
                    "name": "cn.json",
                    "sha": cn_sha,
                    "download_url": "https://raw.example/geosite/cn.json",
                },
                {
                    "type": "file",
                    "name": "google.json",
                    "sha": "deadbeef" * 5,
                    "download_url": "https://raw.example/geosite/google.json",
                },
            ]

            def fake_download(url, output_path, **_kwargs):
                Path(output_path).write_text('{"version": 3, "rules": []}\n', encoding="utf-8")
                return {"ok": True, "size": 28, "elapsed_seconds": 0.01}

            config = {
                "geosite_url": "https://api.github.com/repos/MetaCubeX/meta-rules-dat/contents/geo/geosite?ref=sing",
                "geoip_url": "https://api.github.com/repos/MetaCubeX/meta-rules-dat/contents/geo/geoip?ref=sing",
                "github_token": "",
            }
            required = {"geosite": {"cn", "google", "missing_tag"}, "geoip": set()}

            with patch.object(app, "RULES_DAT_DIR", root), \
                 patch.object(app, "load_remote_json", return_value=listing) as load_json, \
                 patch.object(app, "download_file", side_effect=fake_download) as download_mock, \
                 patch.object(app, "download_rules_dat_rule_file") as per_file_mock:
                result = app.update_remote_rules(config=config, required=required, missing_only=False)

            load_json.assert_called_once()
            per_file_mock.assert_not_called()
            download_mock.assert_called_once()
            geosite_res = result["results"]["geosite"]
            self.assertEqual(geosite_res["unchanged_count"], 1)
            self.assertEqual(geosite_res["downloaded_count"], 1)
            self.assertEqual(geosite_res["skipped_count"], 1)
            self.assertEqual(geosite_res["skipped"][0]["name"], "missing_tag")

    def test_download_file_and_load_remote_json_enforce_size_limits(self):
        class FakeResponse:
            def __init__(self, data):
                self._bio = BytesIO(data)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, size=-1):
                return self._bio.read(size)

        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "huge.json"
            with patch("urllib.request.urlopen", return_value=FakeResponse(b"x" * 100)):
                with self.assertRaisesRegex(ValueError, "size limit"):
                    app.download_file("https://example.com/huge.json", out, max_bytes=50)
            self.assertFalse(out.exists())
            self.assertEqual(list(Path(temp).iterdir()), [])

            with patch("urllib.request.urlopen", return_value=FakeResponse(b'{"a":"' + b"y" * 100 + b'"}')):
                with self.assertRaisesRegex(ValueError, "size limit"):
                    app.load_remote_json("https://example.com/list.json", max_bytes=40)

    def test_rule_order_does_not_acquire_config_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            order_path = Path(temp) / "order.json"
            held = threading.Event()
            done = threading.Event()

            def hold_config_lock():
                with app.CONFIG_LOCK:
                    held.set()
                    done.wait(timeout=5)

            worker = threading.Thread(target=hold_config_lock)
            worker.start()
            try:
                self.assertTrue(held.wait(timeout=2))
                with patch.object(app, "ORDER_PATH", order_path):
                    saved = app.save_rule_order(["Direct.txt", "AI.txt"])
                    loaded = app.read_rule_order()
                self.assertEqual(saved, ["Direct.txt", "AI.txt"])
                self.assertEqual(loaded, ["Direct.txt", "AI.txt"])
            finally:
                done.set()
                worker.join(timeout=2)


if __name__ == "__main__":
    unittest.main()

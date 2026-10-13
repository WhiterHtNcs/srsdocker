from http import HTTPStatus
from io import BytesIO
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

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


if __name__ == "__main__":
    unittest.main()

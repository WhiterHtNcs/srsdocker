from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

import app


class AsnRuleTests(unittest.TestCase):
    def test_openclash_payload_and_complete_rules_use_numeric_asn(self):
        lines = ["keyword:openai", "ipasn:AS399358", "198.51.100.0/24"]
        expected = ["DOMAIN-KEYWORD,openai", "IP-ASN,399358",
                    "IP-CIDR,198.51.100.0/24,no-resolve"]
        result = app.convert_to_openclash_yaml(lines)
        self.assertEqual(result["payload"], expected)
        self.assertEqual(yaml.safe_load(result["yaml"])["payload"], expected)
        targeted = app.convert_to_openclash_yaml(lines, rule_target="AI")["payload"]
        self.assertEqual(targeted, ["DOMAIN-KEYWORD,openai,AI", "IP-ASN,399358,AI",
                                    "IP-CIDR,198.51.100.0/24,AI,no-resolve"])

    def test_asn_normalization_deduplication_and_boundaries(self):
        result = app.convert_to_openclash_yaml([
            "  ipasn: AS399358  ", "ipasn:as0000399358", "ipasn:AS1", "ipasn:AS4294967295",
        ])
        self.assertEqual(result["payload"], ["IP-ASN,399358", "IP-ASN,1", "IP-ASN,4294967295"])

    def test_invalid_asn_reports_its_line_in_openclash(self):
        for value in ["", "399358", "AS", "AS0", "AS-1", "AS4294967296", "AS99999999999",
                      "AS39.9358", "AS٣٩٩٣٥٨", "AS399358,AI", "AS399358 extra"]:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "Line 2: ipasn"):
                app.convert_to_openclash_yaml(["# comment", "ipasn:" + value])

    def test_srs_skips_asn_lines_and_preserves_other_rules(self):
        regular = ["domain:example.com", "full:login.example.com", "keyword:openai",
                   "regexp:^api", "198.51.100.0/24", "2001:db8::/32"]
        expected = app.convert_to_singbox_json(regular)
        self.assertEqual(app.convert_to_singbox_json([
            "ipasn:AS399358", *regular, "ipasn:AS13335",
        ]), expected)
        self.assertNotIn("AS399358", str(expected))
        with patch.object(app, "compile_singbox_json_to_srs", return_value={"content": b"srs"}) as compile_srs:
            self.assertEqual(app.convert_to_srs([*regular, "ipasn:AS399358"]), {"content": b"srs"})
            compile_srs.assert_called_once_with(expected)

    def test_srs_skips_even_invalid_asn_values_without_creating_rules(self):
        self.assertEqual(app.convert_to_singbox_json([
            "# OpenClash-only", "ipasn:AS399358", "ipasn:", "ipasn:not-an-asn",
        ]), {"version": 3, "rules": []})

    def test_explicit_keyword_remains_a_keyword(self):
        self.assertEqual(app.convert_to_openclash_yaml(["keyword:AS399358"])["payload"],
                         ["DOMAIN-KEYWORD,AS399358"])
        self.assertEqual(app.convert_to_singbox_json(["keyword:AS399358"])["rules"],
                         [{"domain_keyword": ["AS399358"]}])

    def test_companion_aiip_rules_route_asn_to_ai_and_skip_it_for_srs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "AI.txt").write_text("keyword:openai\n", encoding="utf-8")
            (root / "AIIP.txt").write_text("ipasn:AS399358\n198.51.100.0/24\n", encoding="utf-8")
            with patch.object(app, "RULES_DIR", root):
                name, _, lines = app.load_rule_lines_with_merged_ip("AIIP")
                self.assertEqual(name, "AI")
                output = yaml.safe_load(app.generate_rules_yaml({"AI": "AI"}, []))
                self.assertEqual(output["rules"], ["DOMAIN-KEYWORD,openai,AI", "IP-ASN,399358,AI",
                                                    "IP-CIDR,198.51.100.0/24,AI,no-resolve"])
                self.assertEqual(app.convert_to_singbox_json(lines)["rules"],
                                 [{"domain_keyword": ["openai"], "ip_cidr": ["198.51.100.0/24"]}])


if __name__ == "__main__":
    unittest.main()

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

import app


PREAMBLE = """dns:
  enable: true
  nameserver: [https://resolver.example/dns-query]
proxy-groups:
  - name: Proxy
    type: select
    proxies: [DIRECT]
"""
MARKER = "# ===== Generator-Only Sections =====\n"


class TemplateParsingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        config = root / "config"
        config.mkdir()
        rules = root / "rules"
        rules.mkdir()
        output = root / "output"
        output.mkdir()
        paths = {
            "BASE_DIR": root,
            "CONFIG_PATH": config / "config.json",
            "ORDER_PATH": config / "order.json",
            "TEMPLATE_PATH": config / "template.yaml",
            "SUBSCRIBE_PATH": config / "subscribe.json",
            "PORTS_PATH": config / "ports.json",
            "RULES_DIR": rules,
            "OPENCLASH_DIR": output,
        }
        for name, path in paths.items():
            patcher = patch.object(app, name, path)
            patcher.start()
            self.addCleanup(patcher.stop)
        app.CONFIG_PATH.write_text("{}", encoding="utf-8")
        app.SUBSCRIBE_PATH.write_text('{"providers": []}', encoding="utf-8")
        (rules / "Direct.txt").write_text("domain:example.com\n", encoding="utf-8")

    def write_template(self, sections, preamble=PREAMBLE):
        app.TEMPLATE_PATH.write_text(preamble + MARKER + sections, encoding="utf-8")

    def test_inline_comments_removed_and_quoted_characters_preserved(self):
        self.write_template("""rule_mapping: # routing categories
  "Direct": DIRECT # fixed route
  AI: 'AI #1: preferred' # quoted hash and colon
  Media: "Stream's group" # apostrophe inside double quotes
custom_rules: # manual rules
  - 'DOMAIN,custom.example,AI #1: preferred' # rule comment
  - "MATCH,DIRECT" # final route
""")
        parsed = app.read_template()
        self.assertEqual(parsed["rule_mapping"], {
            "Direct": "DIRECT", "AI": "AI #1: preferred", "Media": "Stream's group",
        })
        self.assertEqual(parsed["custom_rules"], [
            "DOMAIN,custom.example,AI #1: preferred", "MATCH,DIRECT",
        ])

    def test_complete_generation_uses_clean_targets_and_custom_rules(self):
        self.write_template("""rule_mapping:
  "Direct": DIRECT # fixed route
custom_rules:
  - 'DOMAIN,custom.example,Proxy' # manual route
  - 'MATCH,DIRECT' # final route
""")
        result = app.generate_all_openclash_rules()
        self.assertTrue(result["ok"], result)
        output = app.OPENCLASH_DIR / app.OPENCLASH_ALL_FILENAME
        generated = yaml.safe_load(output.read_text(encoding="utf-8"))
        self.assertEqual(generated["rules"], [
            "DOMAIN,custom.example,Proxy", "DOMAIN-SUFFIX,example.com,DIRECT", "MATCH,DIRECT",
        ])

    def test_preamble_with_placeholders_is_preserved_without_yaml_parsing(self):
        preamble = "proxy-groups:\n  __PROVIDER_GROUPS__\n"
        self.write_template("rule_mapping: {Direct: DIRECT}\ncustom_rules: []\n", preamble)
        self.assertEqual(app.read_template()["preamble"], preamble.rstrip())
        app.TEMPLATE_PATH.write_text(preamble, encoding="utf-8")
        self.assertEqual(app.read_template(), {
            "preamble": preamble, "rule_mapping": {}, "custom_rules": [],
        })

    def test_empty_or_omitted_sections_use_empty_defaults(self):
        cases = [
            ("# no sections\n", {}, []),
            ("rule_mapping: {}\ncustom_rules: []\n", {}, []),
            ("rule_mapping:\ncustom_rules:\n", {}, []),
            ("rule_mapping: {Direct: DIRECT}\n", {"Direct": "DIRECT"}, []),
            ("custom_rules: ['MATCH,DIRECT']\n", {}, ["MATCH,DIRECT"]),
        ]
        for sections, mapping, rules in cases:
            with self.subTest(sections=sections):
                self.write_template(sections)
                parsed = app.read_template()
                self.assertEqual(parsed["rule_mapping"], mapping)
                self.assertEqual(parsed["custom_rules"], rules)

    def test_invalid_generator_field_types_are_rejected(self):
        for sections in ["- item\n", "scalar\n", "rule_mapping: []\n",
                         "rule_mapping: {Direct: null}\n", "rule_mapping: {1: DIRECT}\n",
                         "custom_rules: MATCH,DIRECT\n", "custom_rules: {}\n",
                         "custom_rules: [123]\n"]:
            with self.subTest(sections=sections):
                self.write_template(sections)
                with self.assertRaises(ValueError):
                    app.read_template()

    def test_invalid_yaml_preserves_previous_full_configuration(self):
        self.write_template("rule_mapping:\n  Direct: [unterminated\n")
        output = app.OPENCLASH_DIR / app.OPENCLASH_ALL_FILENAME
        output.write_text("previous full configuration", encoding="utf-8")
        result = app.generate_all_openclash_rules()
        self.assertFalse(result["ok"])
        self.assertIn("YAML", result["full_config"]["error"])
        self.assertEqual(output.read_text(encoding="utf-8"), "previous full configuration")


if __name__ == "__main__":
    unittest.main()

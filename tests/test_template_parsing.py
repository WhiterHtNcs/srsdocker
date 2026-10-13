from pathlib import Path
import json
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

    def test_special_targets_round_trip_in_complete_rules(self):
        for target in ["AI #1: preferred", "AI: preferred", "中文's 组", r"AI\primary"]:
            with self.subTest(target=target):
                preamble = yaml.safe_dump({"proxy-groups": [
                    {"name": target, "type": "select", "proxies": ["DIRECT"]},
                ]}, allow_unicode=True, sort_keys=False)
                manual = f"DOMAIN,manual.example,{target}"
                match = f"MATCH,{target}"
                self.write_template(yaml.safe_dump({"rule_mapping": {"Direct": target},
                                                    "custom_rules": [manual, match]}, allow_unicode=True), preamble)
                result = app.generate_all_openclash_rules()
                self.assertTrue(result["ok"], result)
                generated = yaml.safe_load((app.OPENCLASH_DIR / app.OPENCLASH_ALL_FILENAME).read_text(encoding="utf-8"))
                self.assertEqual(generated["rules"], [manual, f"DOMAIN-SUFFIX,example.com,{target}", match])

    def test_invalid_preamble_or_unexpanded_markers_preserve_output_without_dns(self):
        for preamble in ["proxy-groups: [unterminated\n",
                         "proxy-groups:\n  - name: Proxy\n    type: select\n    use:\n      __ALLNODES__\n",
                         "- scalar root\n"]:
            with self.subTest(preamble=preamble):
                self.write_template("rule_mapping: {Direct: DIRECT}\ncustom_rules: ['MATCH,DIRECT']\n", preamble)
                output = app.OPENCLASH_DIR / app.OPENCLASH_ALL_FILENAME
                output.write_text("previous full configuration", encoding="utf-8")
                result = app.generate_all_openclash_rules()
                self.assertFalse(result["ok"])
                self.assertIsNone(result["combined_output"])
                self.assertEqual(output.read_text(encoding="utf-8"), "previous full configuration")

    def test_valid_yaml_with_mapping_instead_of_rule_string_is_rejected(self):
        content = "rules:\n  - DOMAIN,example.com,AI: preferred\n"
        self.assertIsInstance(yaml.safe_load(content)["rules"][0], dict)
        with self.assertRaisesRegex(ValueError, "rules"):
            app.validate_generated_openclash_config(content)

    def test_unknown_rule_targets_preserve_previous_full_configuration(self):
        cases = [
            {"rule_mapping": {"Direct": "MissingGroup"}, "custom_rules": []},
            {"rule_mapping": {"Direct": "DIRECT"},
             "custom_rules": ["DOMAIN,custom.example,MissingGroup"]},
            {"rule_mapping": {"Direct": "DIRECT"}, "custom_rules": ["MATCH,MissingGroup"]},
        ]
        output = app.OPENCLASH_DIR / app.OPENCLASH_ALL_FILENAME
        for sections in cases:
            with self.subTest(sections=sections):
                self.write_template(yaml.safe_dump(sections))
                output.write_text("previous full configuration", encoding="utf-8")
                result = app.generate_all_openclash_rules()
                self.assertFalse(result["ok"])
                self.assertIn("MissingGroup", result["full_config"]["error"])
                self.assertIsNone(result["combined_output"])
                self.assertEqual(output.read_text(encoding="utf-8"), "previous full configuration")

    def test_duplicate_generated_airport_group_preserves_previous_full_configuration(self):
        app.SUBSCRIBE_PATH.write_text(json.dumps({"providers": [
            {"name": "Proxy", "url": "https://example.test/sub", "use_for_ai": False},
        ]}), encoding="utf-8")
        self.write_template("rule_mapping: {Direct: DIRECT}\ncustom_rules: ['MATCH,DIRECT']\n",
                            PREAMBLE + "  __PROVIDER_GROUPS__\n")
        output = app.OPENCLASH_DIR / app.OPENCLASH_ALL_FILENAME
        output.write_text("previous full configuration", encoding="utf-8")
        result = app.generate_all_openclash_rules()
        self.assertFalse(result["ok"])
        self.assertIn("Proxy", result["full_config"]["error"])
        self.assertIsNone(result["combined_output"])
        self.assertEqual(output.read_text(encoding="utf-8"), "previous full configuration")

    def test_duplicate_groups_nodes_and_builtin_names_are_rejected(self):
        group = {"name": "Proxy", "type": "select", "proxies": ["DIRECT"]}
        node = {"name": "Proxy", "type": "direct"}
        cases = [
            {"proxy-groups": [group, group]},
            {"proxy-groups": [group], "proxies": [node]},
            {"proxies": [node, node]},
            *({"proxy-groups": [{**group, "name": name}]} for name in
              ["DIRECT", "REJECT", "REJECT-DROP", "COMPATIBLE", "PASS", "PASS-RULE"]),
        ]
        for config in cases:
            with self.subTest(config=config):
                with self.assertRaisesRegex(ValueError, "重复"):
                    app.validate_generated_openclash_config(yaml.safe_dump(config))

    def test_rule_reference_validation_accepts_builtins_nodes_and_complex_payloads(self):
        config = {
            "proxies": [{"name": "LocalProxy", "type": "direct"}],
            "proxy-groups": [{"name": "Proxy", "type": "select", "proxies": ["LocalProxy"]}],
            "rules": [
                "DOMAIN,example.com,LocalProxy",
                "IP-CIDR,198.51.100.0/24,Proxy,no-resolve",
                "IP-ASN,399358,Proxy,src,no-resolve",
                "AND,((DOMAIN,example.com),(NETWORK,UDP)),Proxy",
                "OR,((DOMAIN,example.com),(NETWORK,UDP)),Proxy",
                "NOT,((NETWORK,UDP)),Proxy",
                r"DOMAIN-REGEX,^test{1,3}\.example$,Proxy",
                r"PROCESS-NAME-REGEX,^client{1,3}\.exe$,Proxy",
                r"PROCESS-PATH-REGEX,^/opt/client{1,3}$,Proxy",
                " domain , example.com , Proxy ",
                *[f"MATCH,{name}" for name in
                  ["DIRECT", "REJECT", "REJECT-DROP", "COMPATIBLE", "PASS", "PASS-RULE", "GLOBAL"]],
            ],
        }
        app.validate_generated_openclash_config(yaml.safe_dump(config))
        for rule in ["IP-CIDR,198.51.100.0/24,MissingGroup,no-resolve",
                     "AND,((DOMAIN,example.com),(NETWORK,UDP)),MissingGroup",
                     r"DOMAIN-REGEX,^test{1,3}\.example$,MissingGroup"]:
            with self.subTest(rule=rule):
                config["rules"] = [rule]
                with self.assertRaisesRegex(ValueError, "MissingGroup"):
                    app.validate_generated_openclash_config(yaml.safe_dump(config))

    def test_sub_rule_targets_use_the_sub_rule_namespace(self):
        config = {"sub-rules": {"AI": ["MATCH,DIRECT"]},
                  "rules": ["SUB-RULE,(NETWORK,TCP),AI", "MATCH,GLOBAL"]}
        app.validate_generated_openclash_config(yaml.safe_dump(config))
        config["rules"][0] = "SUB-RULE,(NETWORK,TCP),MissingSubRule"
        with self.assertRaisesRegex(ValueError, "MissingSubRule"):
            app.validate_generated_openclash_config(yaml.safe_dump(config))
        config["rules"][0] = "SUB-RULE,(NETWORK,TCP),AI"
        config["sub-rules"]["AI"] = ["MATCH,MissingGroup"]
        with self.assertRaisesRegex(ValueError, "MissingGroup"):
            app.validate_generated_openclash_config(yaml.safe_dump(config))

    def test_invalid_subscription_file_preserves_existing_output(self):
        self.write_template("rule_mapping: {Direct: DIRECT}\ncustom_rules: ['MATCH,DIRECT']\n")
        invalid = [b"{", b"\xff", b"[]", b'{"providers": {}}',
                   b'{"providers": [{"name":"Airport","url":"https://example.test/sub","use_for_dns":"false"}]}']
        for content in invalid:
            with self.subTest(content=content):
                app.SUBSCRIBE_PATH.write_bytes(content)
                output = app.OPENCLASH_DIR / app.OPENCLASH_ALL_FILENAME
                output.write_text("previous full configuration", encoding="utf-8")
                result = app.generate_all_openclash_rules()
                self.assertFalse(result["ok"])
                self.assertIn("subscribe.json", result["full_config"]["error"])
                self.assertEqual(output.read_text(encoding="utf-8"), "previous full configuration")

    def test_unreadable_subscription_reports_error_and_preserves_output(self):
        self.write_template("rule_mapping: {Direct: DIRECT}\ncustom_rules: []\n")
        output = app.OPENCLASH_DIR / app.OPENCLASH_ALL_FILENAME
        output.write_text("previous full configuration", encoding="utf-8")
        original_read = Path.read_text

        def read(path, *args, **kwargs):
            if path == app.SUBSCRIBE_PATH:
                raise PermissionError("Access denied")
            return original_read(path, *args, **kwargs)

        with patch.object(Path, "read_text", read):
            result = app.generate_all_openclash_rules()
        self.assertFalse(result["ok"])
        self.assertIn("subscribe.json", result["full_config"]["error"])
        self.assertEqual(output.read_text(encoding="utf-8"), "previous full configuration")

    def test_missing_subscription_keeps_empty_default_and_legacy_ua_is_preserved(self):
        app.SUBSCRIBE_PATH.unlink()
        self.assertEqual(app.load_subscribe_config(), ([], None))
        app.SUBSCRIBE_PATH.write_text(json.dumps({"providers": [], "default_user_agent": "Legacy-UA"}), encoding="utf-8-sig")
        self.assertEqual(app.load_subscribe_config(), ([], "Legacy-UA"))

    def test_generated_provider_strings_round_trip(self):
        ua = 'Client "Desktop"\\Build'
        provider = {"name": "false", "url": "https://example.test/sub", "use_for_ai": False,
                    "override": {"udp": True, "additional-prefix": 'Home "ISP"'}}
        app.SUBSCRIBE_PATH.write_text(json.dumps({"providers": [provider], "user_agent": ua}), encoding="utf-8")
        self.write_template("rule_mapping: {Direct: DIRECT}\ncustom_rules: ['MATCH,DIRECT']\n",
                            "__PROXY_PROVIDERS__\nproxy-groups:\n  __PROVIDER_GROUPS__\n"
                            "  - name: SELECTOR\n    type: select\n    use:\n      __ALLNODES__\n")
        result = app.generate_all_openclash_rules()
        self.assertTrue(result["ok"], result)
        generated = yaml.safe_load((app.OPENCLASH_DIR / app.OPENCLASH_ALL_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(generated["proxy-providers"]["false"]["header"]["User-Agent"], [ua])
        self.assertEqual(generated["proxy-providers"]["false"]["override"], provider["override"])
        self.assertEqual([group["name"] for group in generated["proxy-groups"]], ["false", "SELECTOR"])
        self.assertEqual(generated["proxy-groups"][1]["use"], ["false"])

    def test_rule_order_written_only_when_it_changes(self):
        app.list_rules()
        original = app.ORDER_PATH.read_bytes()
        stat = app.ORDER_PATH.stat()
        with patch.object(app, "write_text_file_atomic", wraps=app.write_text_file_atomic) as write:
            app.list_rules()
            app.list_rules()
            app.save_rule_order(["Direct.txt"])
            write.assert_not_called()
        self.assertEqual(app.ORDER_PATH.read_bytes(), original)
        self.assertEqual(app.ORDER_PATH.stat().st_mtime_ns, stat.st_mtime_ns)
        (app.RULES_DIR / "AI.txt").write_text("keyword:openai\n", encoding="utf-8")
        with patch.object(app, "write_text_file_atomic", wraps=app.write_text_file_atomic) as write:
            app.list_rules()
            app.list_rules()
            self.assertEqual(write.call_count, 1)
        self.assertEqual(app.read_rule_order(), ["Direct.txt", "AI.txt"])
        with patch.object(app, "write_text_file_atomic", wraps=app.write_text_file_atomic) as write:
            app.update_rule_order(["AI.txt", "Direct.txt"])
            app.list_rules()
            self.assertEqual(write.call_count, 1)
        self.assertEqual(app.read_rule_order(), ["AI.txt", "Direct.txt"])

    def test_unchanged_legacy_or_compact_order_is_not_rewritten(self):
        for content in ['["Direct.txt"]', "# rule order\nDirect.txt\n"]:
            with self.subTest(content=content):
                app.ORDER_PATH.write_text(content, encoding="utf-8")
                with patch.object(app, "write_text_file_atomic") as write:
                    app.list_rules()
                    write.assert_not_called()
                self.assertEqual(app.ORDER_PATH.read_text(encoding="utf-8"), content)

    def test_batch_reuses_one_snapshot_and_one_conversion_per_merged_rule_set(self):
        self.write_template("rule_mapping: {AI: Proxy, Direct: DIRECT}\ncustom_rules: ['DST-PORT,53,DIRECT', 'MATCH,DIRECT']\n",
                            PREAMBLE + "  - name: Direct\n    type: select\n    proxies: [DIRECT]\n")
        (app.RULES_DIR / "DirectIP.txt").write_text("198.51.100.0/24\ngeoip:cn\n", encoding="utf-8")
        (app.RULES_DIR / "AI.txt").write_text("keyword:openai\n", encoding="utf-8")
        (app.RULES_DIR / "AIIP.txt").write_text("ipasn:AS399358\n2001:db8::/32\n", encoding="utf-8")
        app.PORTS_PATH.write_text('{"direct_ports": ["5000-5010"]}', encoding="utf-8")
        reads = {}
        original_read = Path.read_text

        def read(path, *args, **kwargs):
            if path.parent == app.RULES_DIR:
                reads[path.name] = reads.get(path.name, 0) + 1
            return original_read(path, *args, **kwargs)

        with patch.object(Path, "read_text", read), \
             patch.object(app, "list_rules", wraps=app.list_rules) as listing, \
             patch.object(app, "convert_to_openclash_yaml", wraps=app.convert_to_openclash_yaml) as convert:
            result = app.generate_all_openclash_rules()
        self.assertTrue(result["ok"], result)
        listing.assert_called_once_with()
        self.assertEqual(convert.call_count, 2)
        self.assertEqual(reads, {"Direct.txt": 1, "DirectIP.txt": 1, "AI.txt": 1, "AIIP.txt": 1})
        generated = yaml.safe_load((app.OPENCLASH_DIR / app.OPENCLASH_ALL_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(generated["rules"], [
            "DST-PORT,53,DIRECT", "DST-PORT,5000-5010,Direct", "DOMAIN-KEYWORD,openai,Proxy",
            "IP-ASN,399358,Proxy", "IP-CIDR6,2001:db8::/32,Proxy,no-resolve",
            "DOMAIN-SUFFIX,example.com,DIRECT", "IP-CIDR,198.51.100.0/24,DIRECT,no-resolve",
            "GEOIP,CN,DIRECT,no-resolve", "MATCH,DIRECT",
        ])
        merged = {item["rule"]["name"]: item.get("merged_into") for item in result["results"]}
        self.assertEqual(merged["AIIP"], "AI")
        self.assertEqual(merged["DirectIP"], "Direct")

    def test_rule_changes_after_conversion_do_not_change_the_current_output(self):
        self.write_template("rule_mapping: {Direct: DIRECT}\ncustom_rules: ['MATCH,DIRECT']\n")
        ip_path = app.RULES_DIR / "DirectIP.txt"
        ip_path.write_text("198.51.100.0/24\n", encoding="utf-8")
        original_convert = app.convert_to_openclash_yaml

        def convert(lines, *args, **kwargs):
            converted = original_convert(lines, *args, **kwargs)
            (app.RULES_DIR / "Direct.txt").write_text("ipasn:ASinvalid\n", encoding="utf-8")
            ip_path.write_text("203.0.113.0/24\n", encoding="utf-8")
            (app.RULES_DIR / "Later.txt").write_text("keyword:later\n", encoding="utf-8")
            return converted

        with patch.object(app, "convert_to_openclash_yaml", side_effect=convert) as converter:
            result = app.generate_all_openclash_rules()
        self.assertTrue(result["ok"], result)
        self.assertEqual(converter.call_count, 1)
        self.assertEqual(result["total"], 2)
        generated = yaml.safe_load((app.OPENCLASH_DIR / app.OPENCLASH_ALL_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(generated["rules"], ["DOMAIN-SUFFIX,example.com,DIRECT",
                                             "IP-CIDR,198.51.100.0/24,DIRECT,no-resolve", "MATCH,DIRECT"])

    def test_standalone_complete_generation_uses_snapshot_without_reloading_files(self):
        self.write_template("rule_mapping: {Direct: DIRECT}\ncustom_rules: ['MATCH,DIRECT']\n")
        (app.RULES_DIR / "DirectIP.txt").write_text("198.51.100.0/24\n", encoding="utf-8")
        with patch.object(app, "list_rules", wraps=app.list_rules) as listing, \
             patch.object(app, "load_rule_lines_with_merged_ip") as reload:
            result = app.generate_full_openclash_config()
        self.assertTrue(result["ok"], result)
        listing.assert_called_once_with()
        reload.assert_not_called()
        generated = yaml.safe_load((app.OPENCLASH_DIR / app.OPENCLASH_ALL_FILENAME).read_text(encoding="utf-8"))
        self.assertIn("IP-CIDR,198.51.100.0/24,DIRECT,no-resolve", generated["rules"])

    def test_rule_only_fallback_preserves_targets_without_a_template(self):
        app.TEMPLATE_PATH.unlink(missing_ok=True)
        (app.RULES_DIR / "DirectIP.txt").write_text("198.51.100.0/24\n", encoding="utf-8")
        with patch.object(app, "convert_to_openclash_yaml", wraps=app.convert_to_openclash_yaml) as convert:
            result = app.generate_all_openclash_rules()
        self.assertTrue(result["ok"], result)
        self.assertEqual(convert.call_count, 1)
        generated = yaml.safe_load((app.OPENCLASH_DIR / app.OPENCLASH_ALL_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(generated["payload"], ["DOMAIN-SUFFIX,example.com,Direct",
                                               "IP-CIDR,198.51.100.0/24,Direct,no-resolve"])


if __name__ == "__main__":
    unittest.main()

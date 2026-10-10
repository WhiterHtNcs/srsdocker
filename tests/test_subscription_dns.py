import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

import app


BASE_CONFIG = """dns:
  enable: true
  nameserver: [https://original.example/dns-query]
  proxy-server-nameserver: [https://original.example/dns-query]
  fake-ip-filter: ['+.lan']
proxy-groups:
  - name: SELECTOR
    type: select
    use:
      __ALLNODES__
  __PROVIDER_GROUPS__
# ===== Generator-Only Sections =====
rule_mapping:
  Direct: DIRECT
custom_rules:
  - 'MATCH,DIRECT'
"""


def subscription(servers, resolvers=None):
    return yaml.safe_dump({
        "dns": {"nameserver": resolvers or ["https://airport.example/dns-query"]},
        "proxies": [{"name": f"node-{i}", "type": "anytls", "server": server}
                    for i, server in enumerate(servers)],
    })


class SubscriptionDnsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        config_dir = root / "config"
        config_dir.mkdir()
        rules_dir = root / "rules"
        rules_dir.mkdir()
        output_dir = root / "output"
        output_dir.mkdir()
        paths = {
            "BASE_DIR": root, "CONFIG_PATH": config_dir / "config.json",
            "ORDER_PATH": config_dir / "order.json",
            "TEMPLATE_PATH": config_dir / "template.yaml",
            "SUBSCRIBE_PATH": config_dir / "subscribe.json",
            "PORTS_PATH": config_dir / "ports.json",
            "RULES_DIR": rules_dir, "OPENCLASH_DIR": output_dir,
        }
        for name, path in paths.items():
            patcher = patch.object(app, name, path)
            patcher.start()
            self.addCleanup(patcher.stop)
        app.TEMPLATE_PATH.write_text(BASE_CONFIG, encoding="utf-8")
        app.CONFIG_PATH.write_text("{}", encoding="utf-8")
        (rules_dir / "Direct.txt").write_text("domain:example.com\n", encoding="utf-8")
        self.providers = [{"name": "Airport A", "url": "https://a.example/sub", "use_for_ai": False}]
        self.save_providers()

    def save_providers(self):
        app.SUBSCRIBE_PATH.write_text(json.dumps({"providers": self.providers}), encoding="utf-8")

    def read_generated(self):
        return yaml.safe_load((app.OPENCLASH_DIR / app.OPENCLASH_ALL_FILENAME).read_text(encoding="utf-8"))

    def test_option_defaults_off_and_rejects_non_boolean(self):
        payload = {"providers": self.providers}
        self.assertFalse(app.validate_subscribe_data(payload)["providers"][0]["use_for_dns"])
        for value in ["true", 1, None, []]:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "use_for_dns"):
                app.validate_subscribe_data({"providers": [{**self.providers[0], "use_for_dns": value}]})
        normalized = app.validate_subscribe_data({"providers": [{**self.providers[0], "use_for_dns": True, "extra": "keep"}]})
        app.save_subscribe_data(normalized)
        self.assertTrue(app.load_subscribe_data()["providers"][0]["use_for_dns"])
        self.assertEqual(app.load_subscribe_data()["providers"][0]["extra"], "keep")

    def test_no_selection_skips_download_and_keeps_output_identical(self):
        with patch.object(app, "download_provider_subscription") as download:
            unchanged, info = app.apply_provider_dns_policy("arbitrary original text", self.providers)
            self.assertEqual(unchanged, "arbitrary original text")
            self.assertEqual(info["dns_policy_count"], 0)
            self.assertTrue(app.generate_full_openclash_config()["ok"])
            download.assert_not_called()
        self.assertNotIn("proxy-server-nameserver-policy", self.read_generated()["dns"])

    def test_extract_normalizes_domains_and_skips_ipv4_ipv6(self):
        policy = app.extract_subscription_dns_policy(subscription([
            "Node.Example.", "node.example", "1.2.3.4", "2001:db8::1", "[2001:db8::2]",
        ]))
        self.assertEqual(policy, {"node.example": ["https://airport.example/dns-query"]})

    def test_node_resolvers_take_priority_over_global_resolvers(self):
        content = yaml.safe_load(subscription(["node.example"]))
        content["dns"]["proxy-server-nameserver"] = ["https://node-dns.example/query"]
        self.assertEqual(app.extract_subscription_dns_policy(yaml.safe_dump(content))["node.example"],
                         ["https://node-dns.example/query"])

    def test_multiple_airports_only_selected_domains_are_merged(self):
        self.providers = [
            {**self.providers[0], "use_for_dns": True},
            {"name": "Airport B", "url": "https://b.example/sub", "use_for_dns": False},
            {"name": "Airport C", "url": "https://c.example/sub", "use_for_dns": True},
        ]
        self.save_providers()
        with patch.object(app, "download_provider_subscription", side_effect=[
            subscription(["a.example", "a.example"]),
            subscription(["c.example"], ["https://c-dns.example/query"]),
        ]) as download:
            result = app.generate_full_openclash_config()
        self.assertTrue(result["ok"], result)
        self.assertEqual([call.args[0]["name"] for call in download.call_args_list], ["Airport A", "Airport C"])
        config = self.read_generated()
        self.assertEqual(list(config["dns"]["proxy-server-nameserver-policy"]), ["a.example", "c.example"])
        original_dns = yaml.safe_load(BASE_CONFIG.split("proxy-groups:", 1)[0])["dns"]
        for key, value in original_dns.items():
            self.assertEqual(config["dns"][key], value)
        self.assertEqual(result["dns_policy_count"], 2)
        self.assertEqual([group["name"] for group in config["proxy-groups"]],
                         ["SELECTOR", "Airport A", "Airport B", "Airport C"])

    def test_existing_policy_preserved_and_missing_node_resolver_filled(self):
        baseline = yaml.safe_load(BASE_CONFIG.split("proxy-groups:", 1)[0])
        baseline["dns"].pop("proxy-server-nameserver")
        baseline["dns"]["proxy-server-nameserver-policy"] = {"old.example": "https://old.example/query"}
        provider = {**self.providers[0], "use_for_dns": True}
        with patch.object(app, "download_provider_subscription", return_value=subscription(["new.example"])):
            output, _ = app.apply_provider_dns_policy(yaml.safe_dump(baseline), [provider])
        dns = yaml.safe_load(output)["dns"]
        self.assertEqual(dns["proxy-server-nameserver"], baseline["dns"]["nameserver"])
        self.assertEqual(dns["proxy-server-nameserver-policy"]["old.example"], "https://old.example/query")
        self.assertIn("new.example", dns["proxy-server-nameserver-policy"])

    def test_ip_only_nodes_create_no_policy(self):
        provider = {**self.providers[0], "use_for_dns": True}
        with patch.object(app, "download_provider_subscription", return_value=subscription(["1.2.3.4"])):
            output, info = app.apply_provider_dns_policy("original", [provider])
        self.assertEqual(output, "original")
        self.assertEqual(info["dns_policy_count"], 0)

    def test_domain_dns_conflicts_fail(self):
        providers = [{**self.providers[0], "use_for_dns": True},
                     {"name": "Airport B", "use_for_dns": True}]
        with patch.object(app, "download_provider_subscription", side_effect=[
            subscription(["shared.example"]), subscription(["shared.example"], ["https://other.example/query"]),
        ]), self.assertRaisesRegex(ValueError, "冲突"):
            app.apply_provider_dns_policy("original", providers)
        baseline = {"dns": {"enable": True, "nameserver": ["1.1.1.1"],
                            "proxy-server-nameserver-policy": {"shared.example": ["9.9.9.9"]}}}
        with patch.object(app, "download_provider_subscription", return_value=subscription(["shared.example"])), \
             self.assertRaisesRegex(ValueError, "冲突"):
            app.apply_provider_dns_policy(yaml.safe_dump(baseline), providers[:1])

    def test_invalid_or_node_only_subscription_fails_without_leaking_content(self):
        for content in ["proxies: []", "dns: [", "dmxlc3M6Ly9zZWNyZXQ=", "dns: {}\nproxies: []",
                        "!!python/object/apply:builtins.eval ['1+1']"]:
            with self.subTest(content=content), self.assertRaises(ValueError):
                app.extract_subscription_dns_policy(content)

    def test_download_uses_effective_user_agent_and_decodes_bom(self):
        provider = {**self.providers[0], "user_agent": ["Custom-UA", "Other-UA"]}
        with patch.object(app.urllib.request, "urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = b"\xef\xbb\xbfdns: {}"
            self.assertEqual(app.download_provider_subscription(provider, "Global-UA"), "dns: {}")
            self.assertEqual(urlopen.call_args.args[0].get_header("User-agent"), "Custom-UA")
            self.assertEqual(urlopen.call_args.kwargs["timeout"], 20)
            app.download_provider_subscription(self.providers[0], ["Global-UA", "Other-UA"])
            self.assertEqual(urlopen.call_args.args[0].get_header("User-agent"), "Global-UA")
            app.download_provider_subscription(self.providers[0])
            self.assertEqual(urlopen.call_args.args[0].get_header("User-agent"), "clash.meta")

    def test_download_errors_are_sanitized_and_oversized_responses_rejected(self):
        error = app.urllib.error.URLError("https://secret.example/?token=private")
        with patch.object(app.urllib.request, "urlopen", side_effect=error), self.assertRaises(ValueError) as caught:
            app.download_provider_subscription(self.providers[0])
        self.assertNotIn("private", str(caught.exception))
        with patch.object(app.urllib.request, "urlopen") as urlopen, self.assertRaisesRegex(ValueError, "10 MiB"):
            urlopen.return_value.__enter__.return_value.read.return_value = b"x" * (10 * 1024 * 1024 + 1)
            app.download_provider_subscription(self.providers[0])

    def test_failure_does_not_overwrite_existing_config_through_public_generator(self):
        self.providers[0]["use_for_dns"] = True
        self.save_providers()
        output = app.OPENCLASH_DIR / app.OPENCLASH_ALL_FILENAME
        for failure in [ValueError("下载订阅失败。"), None]:
            with self.subTest(failure=failure):
                output.write_text("previous full configuration", encoding="utf-8")
                with patch.object(app, "download_provider_subscription", side_effect=failure, return_value="proxies: []"):
                    result = app.generate_all_openclash_rules()
                self.assertFalse(result["full_config"]["ok"])
                self.assertFalse(result["ok"])
                self.assertIsNone(result["combined_output"])
                self.assertEqual(output.read_text(encoding="utf-8"), "previous full configuration")


if __name__ == "__main__":
    unittest.main()

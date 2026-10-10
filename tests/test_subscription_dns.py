import json
from pathlib import Path
import re
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
        (config_dir / "providers").mkdir()
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
            "RESIDENTIAL_FILTER_PATH": config_dir / "residential_filter.json",
            "COUNTRY_FILTER_PATH": config_dir / "country_filter.json",
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

    def save_countries(self, countries):
        app.COUNTRY_FILTER_PATH.write_text(json.dumps({"countries": countries}), encoding="utf-8")

    def use_country_template(self):
        app.TEMPLATE_PATH.write_text(BASE_CONFIG.replace(
            "# ===== Generator-Only Sections",
            "  __PROVIDER_RESIDENTIAL_GROUPS__\n"
            "  __PROVIDER_COUNTRY_GROUPS__\n"
            "  - name: AI\n    type: select\n    proxies:\n"
            "      __RESIDENTIAL_NODE__\n      __PROVIDER_COUNTRY_NODES__\n"
            "      - DIRECT\n# ===== Generator-Only Sections"
        ), encoding="utf-8")

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

    def test_valid_download_saved_under_exact_airport_name_and_refreshed(self):
        provider = {"name": "测试机场", "url": "https://example.test/sub"}
        path = app.SUBSCRIBE_PATH.parent / "providers" / "测试机场.yaml"
        for domain in ["first.example", "updated.example"]:
            content = subscription([domain])
            with patch.object(app, "download_provider_subscription", return_value=content):
                policy, info = app.load_provider_dns_policy(provider)
            self.assertEqual(path.read_text(encoding="utf-8"), content)
            self.assertIn(domain, policy)
            self.assertEqual(info, {"source": "download", "file": "测试机场.yaml"})

    def test_failed_download_uses_local_file_through_public_generator(self):
        self.providers[0]["use_for_dns"] = True
        self.save_providers()
        path = app.get_provider_config_path(self.providers[0])
        content = subscription(["cached.example"])
        path.write_text(content, encoding="utf-8")
        with patch.object(app, "download_provider_subscription", side_effect=ValueError("下载订阅失败（HTTP 403）。")):
            result = app.generate_all_openclash_rules()
        self.assertTrue(result["ok"], result)
        info = result["full_config"]["dns_providers"][0]
        self.assertEqual(info["source"], "local")
        self.assertEqual(info["file"], "Airport A.yaml")
        self.assertIn("403", info["fallback_reason"])
        self.assertIn("cached.example", self.read_generated()["dns"]["proxy-server-nameserver-policy"])
        self.assertEqual(path.read_text(encoding="utf-8"), content)

    def test_old_cache_migrates_to_providers_and_remains_usable_when_download_fails(self):
        old_path = app.SUBSCRIBE_PATH.parent / "Airport A.yaml"
        content = subscription(["old-cache.example"])
        old_path.write_text(content, encoding="utf-8")
        providers_dir = app.SUBSCRIBE_PATH.parent / "providers"
        providers_dir.rmdir()
        with patch.object(app, "download_provider_subscription", side_effect=ValueError("HTTP 403")):
            policy, info = app.load_provider_dns_policy(self.providers[0])
        self.assertIn("old-cache.example", policy)
        self.assertEqual(info["source"], "local")
        self.assertFalse(old_path.exists())
        self.assertEqual((providers_dir / "Airport A.yaml").read_text(encoding="utf-8"), content)

    def test_existing_providers_cache_takes_priority_over_legacy_file(self):
        old_path = app.SUBSCRIBE_PATH.parent / "Airport A.yaml"
        old_content = subscription(["old-cache.example"])
        old_path.write_text(old_content, encoding="utf-8")
        path = app.get_provider_config_path(self.providers[0])
        content = subscription(["current-cache.example"])
        path.write_text(content, encoding="utf-8")
        with patch.object(app, "download_provider_subscription", side_effect=ValueError("HTTP 403")):
            policy, info = app.load_provider_dns_policy(self.providers[0])
        self.assertEqual(list(policy), ["current-cache.example"])
        self.assertEqual(info["source"], "local")
        self.assertEqual(path.read_text(encoding="utf-8"), content)
        self.assertEqual(old_path.read_text(encoding="utf-8"), old_content)

    def test_invalid_online_config_does_not_replace_usable_local_file(self):
        path = app.get_provider_config_path(self.providers[0])
        content = subscription(["cached.example"])
        path.write_text(content, encoding="utf-8-sig")
        original_bytes = path.read_bytes()
        for invalid in ["proxies: []", "<html>Forbidden</html>", "dns: ["]:
            with self.subTest(invalid=invalid), patch.object(app, "download_provider_subscription", return_value=invalid):
                policy, info = app.load_provider_dns_policy(self.providers[0])
            self.assertIn("cached.example", policy)
            self.assertEqual(info["source"], "local")
            self.assertEqual(path.read_bytes(), original_bytes)

    def test_missing_or_invalid_local_file_reports_both_failures(self):
        path = app.get_provider_config_path(self.providers[0])
        with patch.object(app, "download_provider_subscription", side_effect=ValueError("HTTP 403")):
            with self.assertRaisesRegex(ValueError, "Airport A.yaml 不存在"):
                app.load_provider_dns_policy(self.providers[0])
            path.write_text("proxies: []", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "本地文件 Airport A.yaml 也无法使用"):
                app.load_provider_dns_policy(self.providers[0])

    def test_unsafe_airport_cache_names_cannot_overwrite_template_or_escape_config(self):
        original = app.TEMPLATE_PATH.read_bytes()
        for name in ["../outside", "Airport/Other", "Airport\\Other", "template", "TEMPLATE"]:
            with self.subTest(name=name), patch.object(app, "download_provider_subscription") as download:
                with self.assertRaises(ValueError):
                    app.load_provider_dns_policy({"name": name})
                download.assert_not_called()
        self.assertEqual(app.TEMPLATE_PATH.read_bytes(), original)

    def test_no_dns_selection_ignores_local_configs(self):
        path = app.get_provider_config_path(self.providers[0])
        path.write_text("invalid local file", encoding="utf-8")
        with patch.object(app, "load_provider_dns_policy") as loader:
            output, _ = app.apply_provider_dns_policy("unchanged", self.providers)
            loader.assert_not_called()
        self.assertEqual(output, "unchanged")
        self.assertEqual(path.read_text(encoding="utf-8"), "invalid local file")

    def test_residential_defaults_created_once_without_overwriting_edits(self):
        app.ensure_residential_filter_file()
        self.assertEqual(json.loads(app.RESIDENTIAL_FILTER_PATH.read_text(encoding="utf-8")), {"keywords": ["家宽"]})
        custom = {"keywords": ["住宅"]}
        app.RESIDENTIAL_FILTER_PATH.write_text(json.dumps(custom), encoding="utf-8")
        app.ensure_residential_filter_file()
        self.assertEqual(json.loads(app.RESIDENTIAL_FILTER_PATH.read_text(encoding="utf-8")), custom)

    def test_residential_keywords_reload_and_country_scope_is_preserved(self):
        default = re.compile(app.get_residential_node_filter())
        self.assertIsNotNone(default.search("SG 新加坡 家宽"))
        self.assertIsNone(default.search("US 美国 Residential"))
        app.RESIDENTIAL_FILTER_PATH.write_text(json.dumps({"keywords": ["家寬", "Residential"]}), encoding="utf-8")
        current = re.compile(app.get_residential_node_filter())
        for node in ["US 美国 residential 01", "Residential JP 日本 02", "台湾 家寬", "新加坡 家寬"]:
            self.assertIsNotNone(current.search(node), node)
        for node in ["香港 家寬", "英国 Residential", "US 美国 普通线路", "台湾 家宽"]:
            self.assertIsNone(current.search(node), node)
        self.assertEqual(set(json.loads(app.RESIDENTIAL_FILTER_PATH.read_text(encoding="utf-8"))), {"keywords"})

    def test_residential_keywords_are_literal_and_yaml_quotes_round_trip(self):
        app.RESIDENTIAL_FILTER_PATH.write_text(json.dumps({"keywords": ["Home+", "Home's"]}), encoding="utf-8")
        provider = {**self.providers[0], "use_for_residential": True}
        rendered = app.generate_provider_residential_groups_yaml([provider])
        group = yaml.safe_load("proxy-groups:\n  " + rendered)["proxy-groups"][0]
        pattern = re.compile(group["filter"])
        self.assertIsNotNone(pattern.search("US Home+"))
        self.assertIsNotNone(pattern.search("Home's 台湾"))
        self.assertIsNone(pattern.search("US Homeeeee"))
        self.assertEqual(group["name"], "Airport A·家宽")
        self.assertEqual(group["type"], "select")

    def test_invalid_residential_config_reports_error_only_when_group_enabled(self):
        for value in ["{", "[]", '{"keywords": []}', '{"keywords": [true]}']:
            with self.subTest(value=value):
                app.RESIDENTIAL_FILTER_PATH.write_text(value, encoding="utf-8")
                with self.assertRaises(ValueError):
                    app.get_residential_node_filter()
                self.assertEqual(app.get_provider_residential_groups(self.providers), [])
        app.TEMPLATE_PATH.write_text(BASE_CONFIG.replace(
            "# ===== Generator-Only Sections", "  __PROVIDER_RESIDENTIAL_GROUPS__\n# ===== Generator-Only Sections"
        ), encoding="utf-8")
        self.providers[0]["use_for_residential"] = True
        self.save_providers()
        output = app.OPENCLASH_DIR / app.OPENCLASH_ALL_FILENAME
        output.write_text("previous full configuration", encoding="utf-8")
        result = app.generate_full_openclash_config()
        self.assertFalse(result["ok"])
        self.assertIn("residential_filter.json", result["error"])
        self.assertEqual(output.read_text(encoding="utf-8"), "previous full configuration")

    def test_country_defaults_created_once_and_keep_existing_edits(self):
        app.ensure_country_filter_file()
        self.assertEqual([name for name, _ in app.get_country_node_filters()],
                         ["美国", "新加坡", "台湾", "日本"])
        self.save_countries([{"name": "加拿大", "filter": r"(?i)(加拿大|\bCA\b)"}])
        original = app.COUNTRY_FILTER_PATH.read_bytes()
        app.ensure_country_filter_file()
        self.assertEqual(app.COUNTRY_FILTER_PATH.read_bytes(), original)
        self.assertEqual([name for name, _ in app.get_country_node_filters()], ["加拿大"])

    def test_ai_and_residential_share_country_selection_filters_and_order(self):
        self.use_country_template()
        self.providers = [
            {**self.providers[0], "use_for_ai": True, "use_for_residential": True},
            {"name": "Airport B", "url": "https://b.example/sub", "use_for_ai": True},
        ]
        self.save_providers()
        countries = [
            {"name": "加拿大", "enabled": True, "filter": r"(?i)(加拿大|\bCA\b)"},
            {"name": "美国", "enabled": False, "filter": r"(?i)(美国|\bUS\b)"},
            {"name": "新加坡", "enabled": True, "filter": r"(?i)(新加坡|\bSG\b)"},
        ]
        self.save_countries(countries)
        with patch.object(app, "get_country_node_filters", wraps=app.get_country_node_filters) as load:
            result = app.generate_full_openclash_config()
            load.assert_called_once_with()
        self.assertTrue(result["ok"], result)
        groups = self.read_generated()["proxy-groups"]
        by_name = {group["name"]: group for group in groups}
        country_names = [group["name"] for group in groups if "·" in group["name"] and "家宽" not in group["name"]]
        self.assertEqual(country_names, ["Airport A·加拿大", "Airport A·新加坡", "Airport B·加拿大", "Airport B·新加坡"])
        self.assertEqual(by_name["AI"]["proxies"], ["Airport A·家宽", *country_names, "DIRECT"])
        self.assertEqual(by_name["Airport A·加拿大"]["filter"], countries[0]["filter"])
        home = by_name["Airport A·家宽"]
        self.assertEqual(home["type"], "select")
        self.assertEqual(home["use"], ["Airport A"])
        self.assertNotIn("Airport B·家宽", by_name)
        pattern = re.compile(home["filter"])
        for node in ["ca 家宽", "家宽 加拿大", "SG 家宽", "家宽 新加坡"]:
            self.assertIsNotNone(pattern.search(node), node)
        for node in ["US 家宽", "日本 家宽", "CA 普通", "家宽 香港"]:
            self.assertIsNone(pattern.search(node), node)

        self.save_countries([{"name": "日本", "enabled": True, "filter": r"(?i)(日本|\bJP\b)"}])
        self.assertTrue(app.generate_full_openclash_config()["ok"])
        updated = {group["name"]: group for group in self.read_generated()["proxy-groups"]}
        self.assertEqual(updated["AI"]["proxies"], ["Airport A·家宽", "Airport A·日本", "Airport B·日本", "DIRECT"])
        self.assertIsNone(re.search(updated["Airport A·家宽"]["filter"], "CA 家宽"))
        self.assertIsNotNone(re.search(updated["Airport A·家宽"]["filter"], "JP 家宽"))

    def test_empty_or_disabled_countries_leave_no_groups_or_dangling_references(self):
        self.use_country_template()
        self.providers[0].update(use_for_ai=True, use_for_residential=True)
        self.save_providers()
        for countries in [[], [{"name": "美国", "enabled": False, "filter": "US"}]]:
            with self.subTest(countries=countries):
                self.save_countries(countries)
                result = app.generate_full_openclash_config()
                self.assertTrue(result["ok"], result)
                groups = self.read_generated()["proxy-groups"]
                self.assertEqual([group["name"] for group in groups], ["SELECTOR", "Airport A", "AI"])
                self.assertEqual(groups[-1]["proxies"], ["DIRECT"])
                self.assertIsNone(re.search(app.get_residential_node_filter(), "US 家宽"))

    def test_country_yaml_quotes_and_scoped_flags_round_trip(self):
        self.save_countries([{"name": "自定义", "filter": r"(?i)(US|John's)"}])
        rendered = app.generate_provider_country_groups_yaml(self.providers, app.DEFAULT_URL_TEST_CONFIG)
        group = yaml.safe_load("proxy-groups:\n  " + rendered)["proxy-groups"][0]
        self.assertEqual(group["filter"], r"(?i)(US|John's)")
        home = re.compile(app.get_residential_node_filter())
        self.assertIsNotNone(home.search("john's 家宽"))
        self.assertIsNotNone(home.search("家宽 us"))

    def test_country_case_rules_are_shared_while_home_keywords_ignore_case(self):
        self.save_countries([{"name": "美国", "filter": r"\bUS\b"}])
        app.RESIDENTIAL_FILTER_PATH.write_text(json.dumps({"keywords": ["Home"]}), encoding="utf-8")
        ai = re.compile(app.get_provider_country_groups(self.providers)[0][2])
        home = re.compile(app.get_residential_node_filter())
        for node in ["US home", "HOME US"]:
            self.assertIsNotNone(ai.search(node))
            self.assertIsNotNone(home.search(node))
        for node in ["us home", "HOME us"]:
            self.assertIsNone(ai.search(node))
            self.assertIsNone(home.search(node))

    def test_invalid_country_configs_fail_without_overwriting_existing_output(self):
        self.use_country_template()
        self.providers[0]["use_for_ai"] = True
        self.save_providers()
        output = app.OPENCLASH_DIR / app.OPENCLASH_ALL_FILENAME
        invalid = ["{", "[]", '{"countries": {}}']
        invalid += [json.dumps({"countries": countries}) for countries in [
            [True], [{"name": "", "filter": "US"}], [{"name": "美国:节点", "filter": "US"}],
            [{"name": "美国", "filter": "US"}, {"name": "美国", "filter": "USA"}],
            [{"name": "美国", "enabled": "true", "filter": "US"}],
            [{"name": "美国", "filter": "["}], [{"name": "美国", "filter": "US\nSG"}],
        ]]
        for value in invalid:
            with self.subTest(value=value):
                app.COUNTRY_FILTER_PATH.write_text(value, encoding="utf-8")
                output.write_text("previous full configuration", encoding="utf-8")
                result = app.generate_full_openclash_config()
                self.assertFalse(result["ok"])
                self.assertIn("country_filter.json", result["error"])
                self.assertEqual(output.read_text(encoding="utf-8"), "previous full configuration")

    def test_invalid_country_file_ignored_when_ai_and_residential_are_disabled(self):
        self.use_country_template()
        app.COUNTRY_FILTER_PATH.write_text("invalid", encoding="utf-8")
        with patch.object(app, "get_country_node_filters") as load:
            self.assertTrue(app.generate_full_openclash_config()["ok"])
            load.assert_not_called()
        self.assertEqual(app.get_provider_country_groups([]), [])
        self.assertEqual(app.get_provider_residential_groups(self.providers), [])


if __name__ == "__main__":
    unittest.main()

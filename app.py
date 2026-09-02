#!/usr/bin/env python3
"""singbox-srs-generator HTTP service."""

from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import ipaddress
from urllib.parse import urlparse
from urllib.parse import parse_qs
from urllib.parse import urlencode
from urllib.parse import urlunparse


BASE_DIR = Path(__file__).resolve().parent
MAPPING_DIR = BASE_DIR / "mapping"
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", str(MAPPING_DIR / "config" / "config.json")))
ORDER_PATH = Path(os.environ.get("ORDER_PATH", str(CONFIG_PATH.parent / "order.json")))
WEB_DIR = BASE_DIR / "web"
RULES_DIR = MAPPING_DIR / "rules"
RULE_SET_DIR = MAPPING_DIR / "rule-set"
SRS_DIR = RULE_SET_DIR / "srs"
OPENCLASH_DIR = RULE_SET_DIR / "openclash"
OPENCLASH_ALL_FILENAME = "openclash.yaml"
TEMPLATE_PATH = MAPPING_DIR / "config" / "template.yaml"
SUBSCRIBE_PATH = MAPPING_DIR / "config" / "subscribe.json"
PORTS_PATH = MAPPING_DIR / "config" / "ports.json"
RULES_DAT_DIR = MAPPING_DIR / "rules-dat"
SING_BOX_PATH = Path(os.environ.get("SING_BOX_PATH", str(MAPPING_DIR / "bin" / ("sing-box.exe" if os.name == "nt" else "sing-box"))))
CRON_FILE = Path(os.environ.get("CRON_FILE", "/etc/cron.d/singbox-srs-generator"))
APP_PORT = 9044
MAX_JSON_BODY_BYTES = 1024 * 1024

CONFIG_LOCK = threading.RLock()
RULES_LOCK = threading.RLock()
RULES_DAT_LOCK = threading.RLock()
SUBSCRIBE_LOCK = threading.RLock()
GENERATE_LOCK = threading.Lock()
REMOTE_UPDATE_LOCK = threading.Lock()


DEFAULT_URL_TEST_CONFIG = {
    "url": "https://www.gstatic.com/generate_204",
    "interval": 300,
    "tolerance": 50,
    "timeout": 5000,
    "lazy": True,
}


DEFAULT_CONFIG = {
    "geosite_url": "https://api.github.com/repos/MetaCubeX/meta-rules-dat/contents/geo/geosite?ref=sing",
    "geoip_url": "https://api.github.com/repos/MetaCubeX/meta-rules-dat/contents/geo/geoip?ref=sing",
    "github_token": "",
    "auto_update_enabled": False,
    "auto_update_cron": "0 4 * * *",
    "url_test": DEFAULT_URL_TEST_CONFIG,
}

RULE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
GEO_CODE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.!@+\-]{0,127}$")
CRON_FIELD_PATTERN = re.compile(r"^[A-Za-z0-9*/,\-]+$")
DOMAIN_LIKE_PATTERN = re.compile(r"^(?=.{1,253}$)(?!-)[A-Za-z0-9_.*-]+(?:\.[A-Za-z0-9_.*-]+)+\.?$")
KEYWORD_LIKE_PATTERN = re.compile(r"^[A-Za-z0-9_.!@+*-]+$")
PROVIDER_NAME_PATTERN = re.compile(r"^[^\r\n:#{}\[\],&*!|>'\"%@`]{1,128}$")

COUNTRY_NODE_FILTERS = (
    (
        "美国",
        r"(?i)(美国|美國|\bUS\b|\bUSA\b|United States|洛杉矶|洛杉磯|圣何塞|聖何塞|西雅图|西雅圖|纽约|紐約|芝加哥|达拉斯|達拉斯)",
    ),
    ("新加坡", r"(?i)(新加坡|狮城|獅城|\bSG\b|\bSGP\b|Singapore)"),
    ("台湾", r"(?i)(台湾|台灣|\bTW\b|\bTWN\b|Taiwan|台北|臺北|新北|高雄|台中|臺中)"),
    ("日本", r"(?i)(日本|\bJP\b|\bJPN\b|Japan|东京|東京|大阪|札幌|川崎)"),
    ("英国", r"(?i)(英国|英國|\bUK\b|\bGB\b|\bGBR\b|United Kingdom|London|伦敦|倫敦|曼彻斯特|曼徹斯特)"),
)


class RuleConversionError(Exception):
    def __init__(self, message, result=None):
        super().__init__(message)
        self.result = result or {}


def ensure_directories():
    MAPPING_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    RULES_DIR.mkdir(exist_ok=True)
    RULE_SET_DIR.mkdir(exist_ok=True)
    SRS_DIR.mkdir(exist_ok=True)
    OPENCLASH_DIR.mkdir(exist_ok=True)
    (RULES_DAT_DIR / "geosite").mkdir(parents=True, exist_ok=True)
    (RULES_DAT_DIR / "geoip").mkdir(parents=True, exist_ok=True)
    WEB_DIR.mkdir(exist_ok=True)


def load_stored_config():
    with CONFIG_LOCK:
        if not CONFIG_PATH.exists():
            save_config(DEFAULT_CONFIG)
            return dict(DEFAULT_CONFIG)

        with CONFIG_PATH.open("r", encoding="utf-8") as file:
            data = json.load(file)

        config = dict(DEFAULT_CONFIG)
        config["url_test"] = dict(DEFAULT_URL_TEST_CONFIG)
        config.update({key: value for key, value in data.items() if key != "url_test"})
        if isinstance(data.get("url_test"), dict):
            config["url_test"].update(data["url_test"])
        config.pop("web_port", None)
        return config


def load_config():
    return apply_environment_overrides(load_stored_config())


def apply_environment_overrides(config):
    geosite_url = os.environ.get("GEOSITE_URL")
    if geosite_url:
        config["geosite_url"] = geosite_url

    geoip_url = os.environ.get("GEOIP_URL")
    if geoip_url:
        config["geoip_url"] = geoip_url

    github_token = os.environ.get("GITHUB_TOKEN")
    if github_token and github_token.strip():
        config["github_token"] = github_token

    return config


def save_config(config):
    with CONFIG_LOCK:
        config = dict(config)
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp_fd, temp_name = tempfile.mkstemp(
            prefix=f".{CONFIG_PATH.name}.",
            suffix=".tmp",
            dir=str(CONFIG_PATH.parent),
        )
        os.close(temp_fd)
        temp_path = Path(temp_name)

        try:
            with temp_path.open("w", encoding="utf-8") as file:
                json.dump(config, file, indent=2, ensure_ascii=False)
                file.write("\n")
            os.replace(temp_path, CONFIG_PATH)
        finally:
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except OSError:
                pass


def public_config(config):
    public = dict(config)
    token = public.pop("github_token", "")
    env_token = os.environ.get("GITHUB_TOKEN", "")
    env_token_effective = env_token and env_token.strip()
    public["github_token_configured"] = bool(token or env_token_effective)
    public["github_token_source"] = "environment" if env_token_effective else ("config" if token else "none")
    public["config_path"] = str(CONFIG_PATH)
    return public


def normalize_rule_name(name):
    if not isinstance(name, str):
        raise ValueError("Rule name must be a string.")

    normalized = name.strip()
    if normalized.endswith(".txt"):
        normalized = normalized[:-4]

    if not RULE_NAME_PATTERN.fullmatch(normalized):
        raise ValueError("Rule name may only contain letters, numbers, dots, underscores, and hyphens.")

    return normalized


def get_rule_path(name):
    normalized = normalize_rule_name(name)
    rules_root = RULES_DIR.resolve()
    rule_path = (RULES_DIR / f"{normalized}.txt").resolve()

    if rule_path.parent != rules_root:
        raise ValueError("Invalid rule path.")

    return normalized, rule_path


def normalize_rule_filename(filename):
    if not isinstance(filename, str):
        raise ValueError("Rule filename must be a string.")

    normalized = filename.strip()
    if not normalized.endswith(".txt"):
        normalized = f"{normalized}.txt"

    name = normalized[:-4]
    rule_name = normalize_rule_name(name)
    if normalized != f"{rule_name}.txt":
        raise ValueError("Rule filename may only contain letters, numbers, dots, underscores, and hyphens.")

    return normalized


def get_srs_paths(name):
    normalized = normalize_rule_name(name)
    rule_set_root = RULE_SET_DIR.resolve()
    srs_root = SRS_DIR.resolve()
    json_path = (RULE_SET_DIR / f"{normalized}.json").resolve()
    srs_path = (SRS_DIR / f"{normalized}.srs").resolve()

    if json_path.parent != rule_set_root or srs_path.parent != srs_root:
        raise ValueError("Invalid output path.")

    return normalized, json_path, srs_path


def normalize_geo_code(code):
    if not isinstance(code, str):
        raise ValueError("Geo rule code must be a string.")

    normalized = code.strip().lower()
    if normalized.endswith(".json"):
        normalized = normalized[:-5]

    if not GEO_CODE_PATTERN.fullmatch(normalized):
        raise ValueError("Geo rule code may only contain letters, numbers, dots, underscores, hyphens, !, @, and +.")

    return normalized


def get_rules_dat_json_path(kind, code):
    if kind not in ("geosite", "geoip"):
        raise ValueError("Invalid geo rule type.")

    normalized = normalize_geo_code(code)
    root = (RULES_DAT_DIR / kind).resolve()
    path = (RULES_DAT_DIR / kind / f"{normalized}.json").resolve()

    if path.parent != root:
        raise ValueError("Invalid rules-dat path.")

    return normalized, path


def read_rule_order():
    with CONFIG_LOCK:
        if not ORDER_PATH.exists():
            return []

        ordered = []
        seen = set()
        raw_content = ORDER_PATH.read_text(encoding="utf-8").strip()
        if not raw_content:
            return []

        try:
            data = json.loads(raw_content)
            candidates = data if isinstance(data, list) else []
        except json.JSONDecodeError:
            candidates = [
                line.strip()
                for line in raw_content.splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]

        for item in candidates:
            try:
                filename = normalize_rule_filename(item)
            except ValueError:
                continue

            if filename in seen:
                continue

            seen.add(filename)
            ordered.append(filename)

        return ordered


def save_rule_order(filenames):
    normalized = []
    seen = set()
    for filename in filenames:
        item = normalize_rule_filename(filename)
        if item in seen:
            continue
        seen.add(item)
        normalized.append(item)

    content = json.dumps(normalized, ensure_ascii=False, indent=2) + "\n"

    with CONFIG_LOCK:
        ORDER_PATH.parent.mkdir(parents=True, exist_ok=True)
        write_text_file_atomic(ORDER_PATH, content)

    return normalized


def reconcile_rule_order(rules):
    existing = {rule["filename"] for rule in rules}
    current_order = [filename for filename in read_rule_order() if filename in existing]
    current_set = set(current_order)
    missing = [
        rule["filename"]
        for rule in sorted(rules, key=lambda item: (item["created_ns"], item["filename"].lower()))
        if rule["filename"] not in current_set
    ]
    return save_rule_order(current_order + missing)


def apply_rule_order(rules):
    order = reconcile_rule_order(rules)
    positions = {filename: index for index, filename in enumerate(order)}

    for rule in rules:
        rule["order"] = positions.get(rule["filename"], len(positions))

    return sorted(rules, key=lambda rule: (rule["order"], rule["filename"].lower()))


def list_rules():
    with RULES_LOCK:
        rules = []
        for path in RULES_DIR.glob("*.txt"):
            if not path.is_file():
                continue

            stat = path.stat()
            created = getattr(stat, "st_birthtime", stat.st_ctime)
            created_ns = getattr(stat, "st_birthtime_ns", stat.st_ctime_ns)
            rules.append(
                {
                    "name": path.stem,
                    "filename": path.name,
                    "content": path.read_text(encoding="utf-8"),
                    "size": stat.st_size,
                    "created": created,
                    "created_ns": created_ns,
                    "modified": stat.st_mtime,
                }
            )

        return apply_rule_order(rules)


def update_rule_order(filenames):
    requested = []
    seen = set()
    for filename in filenames:
        normalized = normalize_rule_filename(filename)
        if normalized in seen:
            continue
        seen.add(normalized)
        requested.append(normalized)

    with RULES_LOCK:
        existing = existing_rule_filenames()
        unknown = [filename for filename in requested if filename not in existing]
        if unknown:
            raise ValueError(f"Unknown rule filename: {unknown[0]}")

        missing = sorted(existing - set(requested), key=str.lower)
        order = save_rule_order(requested + missing)

    return order


def existing_rule_filenames():
    return {
        path.name
        for path in RULES_DIR.glob("*.txt")
        if path.is_file()
    }


def list_srs_files():
    files = []
    for path in sorted(RULE_SET_DIR.glob("*.json"), key=lambda item: item.name.lower()):
        if not path.is_file():
            continue

        stat = path.stat()
        files.append(
            {
                "filename": path.name,
                "path": str(path.relative_to(BASE_DIR)),
                "size": stat.st_size,
                "modified": stat.st_mtime,
            }
        )

    for path in sorted(SRS_DIR.glob("*.srs"), key=lambda item: item.name.lower()):
        if not path.is_file() or path.suffix not in (".json", ".srs"):
            continue

        stat = path.stat()
        files.append(
            {
                "filename": path.name,
                "path": str(path.relative_to(BASE_DIR)),
                "size": stat.st_size,
                "modified": stat.st_mtime,
            }
        )

    for path in sorted(OPENCLASH_DIR.glob("*.yaml"), key=lambda item: item.name.lower()):
        if not path.is_file():
            continue

        stat = path.stat()
        files.append(
            {
                "filename": path.name,
                "path": str(path.relative_to(BASE_DIR)),
                "size": stat.st_size,
                "modified": stat.st_mtime,
            }
        )

    return files


def write_srs_files_index():
    ensure_directories()
    filenames = [
        path.name
        for path in sorted(SRS_DIR.glob("*.srs"), key=lambda item: item.name.lower())
        if path.is_file()
    ]
    content = "\n".join(filenames)
    if content:
        content += "\n"

    index_path = SRS_DIR / "files.txt"
    temp_fd, temp_name = tempfile.mkstemp(
        prefix=f".{index_path.name}.",
        suffix=".tmp",
        dir=str(SRS_DIR),
    )
    os.close(temp_fd)
    temp_path = Path(temp_name)

    try:
        temp_path.write_text(content, encoding="utf-8")
        os.replace(temp_path, index_path)
    finally:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except OSError:
            pass

    return {
        "path": str(index_path.relative_to(BASE_DIR)),
        "count": len(filenames),
        "files": filenames,
    }


def get_remote_rule_files():
    with RULES_DAT_LOCK:
        files = {}
        for kind in ("geosite", "geoip"):
            directory = RULES_DAT_DIR / kind
            items = []
            for path in sorted(directory.glob("*.json"), key=lambda item: item.name.lower()):
                if not path.is_file():
                    continue
                stat = path.stat()
                items.append(
                    {
                        "name": path.stem,
                        "filename": path.name,
                        "path": str(path),
                        "size": stat.st_size,
                        "modified": stat.st_mtime,
                    }
                )
            files[kind] = {
                "path": str(directory),
                "count": len(items),
                "items": items,
            }
        return files


def validate_download_url(url):
    if not isinstance(url, str) or not url.strip():
        raise ValueError("Download URL is empty.")

    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Download URL must use http or https.")

    if not parsed.netloc:
        raise ValueError("Download URL must include a host.")

    return url.strip()


def validate_url_test_config(value):
    """Validate and normalize shared url-test settings."""
    if not isinstance(value, dict):
        raise ValueError("URL-test configuration must be an object.")

    config = dict(DEFAULT_URL_TEST_CONFIG)
    config.update(value)

    url = validate_download_url(config["url"])
    normalized = {"url": url}
    for key, minimum, maximum in (
        ("interval", 10, 86400),
        ("tolerance", 0, 10000),
        ("timeout", 1000, 60000),
    ):
        raw_value = config[key]
        if isinstance(raw_value, bool) or not isinstance(raw_value, int):
            raise ValueError(f"URL-test {key} must be an integer.")
        if not minimum <= raw_value <= maximum:
            raise ValueError(f"URL-test {key} must be between {minimum} and {maximum}.")
        normalized[key] = raw_value

    if not isinstance(config["lazy"], bool):
        raise ValueError("URL-test lazy must be true or false.")
    normalized["lazy"] = config["lazy"]
    return normalized


def get_url_test_option_lines(url_test_config):
    """Return url-test settings as YAML key/value lines without indentation."""
    return [
        f"url: {json.dumps(url_test_config['url'], ensure_ascii=False)}",
        f"interval: {url_test_config['interval']}",
        f"tolerance: {url_test_config['tolerance']}",
        f"timeout: {url_test_config['timeout']}",
        f"lazy: {str(url_test_config['lazy']).lower()}",
    ]


def generate_url_test_options_yaml(url_test_config, continuation_indent="    "):
    """Generate options for a template marker whose first line inherits its indent."""
    lines = get_url_test_option_lines(url_test_config)
    return "\n".join([lines[0], *[f"{continuation_indent}{line}" for line in lines[1:]]])


def validate_cron_expression(expression):
    if not isinstance(expression, str):
        raise ValueError("Cron expression must be a string.")

    fields = expression.strip().split()
    if len(fields) != 5:
        raise ValueError("Cron expression must contain exactly 5 fields.")

    for field in fields:
        if not CRON_FIELD_PATTERN.fullmatch(field):
            raise ValueError("Cron expression contains invalid characters.")

    return " ".join(fields)


def download_file(url, output_path, timeout=60, github_token=None):
    url = validate_download_url(url)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    temp_fd, temp_name = tempfile.mkstemp(prefix=f".{output_path.name}.", suffix=".download", dir=str(output_path.parent))
    os.close(temp_fd)
    temp_path = Path(temp_name)
    started_at = time.time()

    try:
        request = urllib.request.Request(url, headers=build_download_headers(github_token))
        with urllib.request.urlopen(request, timeout=timeout) as response:
            with temp_path.open("wb") as file:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    file.write(chunk)

        os.replace(temp_path, output_path)
        stat = output_path.stat()
        return {
            "ok": True,
            "url": url,
            "path": str(output_path),
            "size": stat.st_size,
            "elapsed_seconds": round(time.time() - started_at, 3),
        }
    finally:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except OSError:
            pass


def build_download_headers(github_token=None, accept="application/json"):
    headers = {
        "Accept": accept,
        "User-Agent": "singbox-srs-generator/0.1",
    }

    token = github_token or ""
    if token:
        headers["Authorization"] = f"Bearer {token}"

    return headers


def load_remote_json(url, github_token=None, timeout=60):
    url = validate_download_url(url)
    request = urllib.request.Request(
        url,
        headers=build_download_headers(github_token, accept="application/vnd.github+json, application/json"),
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def normalize_rules_dat_listing_url(url, kind):
    url = validate_download_url(url)
    parsed = urlparse(url)

    if kind not in ("geosite", "geoip"):
        raise ValueError("Invalid geo rule type.")

    if parsed.netloc.lower() == "github.com":
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 4 and parts[2] in ("tree", "blob"):
            owner, repo, _, branch = parts[:4]
            repo_path_parts = parts[4:]
            if not repo_path_parts:
                repo_path_parts = ["geo", kind]
            elif repo_path_parts[-1] == "geo":
                repo_path_parts.append(kind)
            elif repo_path_parts[-1] not in ("geosite", "geoip"):
                repo_path_parts.append(kind)

            api_path = "/".join(repo_path_parts)
            return f"https://api.github.com/repos/{owner}/{repo}/contents/{api_path}?ref={branch}"

    if parsed.netloc.lower() == "api.github.com" and "/contents/" in parsed.path:
        path = parsed.path
        query = parse_qs(parsed.query)

        if path.endswith("/geo"):
            path = f"{path}/{kind}"
        elif not path.endswith(f"/{kind}"):
            path = f"{path.rstrip('/')}/{kind}"

        query_string = urlencode({key: values[-1] for key, values in query.items()})
        return urlunparse(parsed._replace(path=path, query=query_string))

    return url


def normalize_rules_dat_file_url(url, kind, code):
    url = validate_download_url(url)
    normalized_code = normalize_geo_code(code)
    parsed = urlparse(url)

    if kind not in ("geosite", "geoip"):
        raise ValueError("Invalid geo rule type.")

    filename = f"{normalized_code}.json"

    if parsed.netloc.lower() == "github.com":
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 4 and parts[2] in ("tree", "blob"):
            owner, repo, _, branch = parts[:4]
            repo_path_parts = parts[4:]
            if repo_path_parts and repo_path_parts[-1].endswith(".json"):
                api_path = "/".join(repo_path_parts)
            else:
                if not repo_path_parts:
                    repo_path_parts = ["geo", kind]
                elif repo_path_parts[-1] == "geo":
                    repo_path_parts.append(kind)
                elif repo_path_parts[-1] not in ("geosite", "geoip"):
                    repo_path_parts.append(kind)
                repo_path_parts.append(filename)
                api_path = "/".join(repo_path_parts)

            return f"https://api.github.com/repos/{owner}/{repo}/contents/{api_path}?ref={branch}"

    if parsed.netloc.lower() == "api.github.com" and "/contents/" in parsed.path:
        path = parsed.path.rstrip("/")
        if not path.endswith(".json"):
            if path.endswith("/geo"):
                path = f"{path}/{kind}"
            elif not path.endswith(f"/{kind}"):
                path = f"{path}/{kind}"
            path = f"{path}/{filename}"

        query = parse_qs(parsed.query)
        query_string = urlencode({key: values[-1] for key, values in query.items()})
        return urlunparse(parsed._replace(path=path, query=query_string))

    if parsed.netloc.lower() == "raw.githubusercontent.com":
        path = parsed.path.rstrip("/")
        if not path.endswith(".json"):
            if path.endswith("/geo"):
                path = f"{path}/{kind}"
            elif not path.endswith(f"/{kind}"):
                path = f"{path}/{kind}"
            path = f"{path}/{filename}"
        return urlunparse(parsed._replace(path=path, query=""))

    path = parsed.path.rstrip("/")
    if not path.endswith(".json"):
        path = f"{path}/{filename}"
    return urlunparse(parsed._replace(path=path))


def download_rules_dat_collection(listing_url, kind, github_token=None):
    ensure_directories()
    started_at = time.time()
    normalized_url = normalize_rules_dat_listing_url(listing_url, kind)
    target_dir = RULES_DAT_DIR / kind
    listing = load_remote_json(normalized_url, github_token=github_token)

    if not isinstance(listing, list):
        raise ValueError("Remote rules-dat listing must be a JSON array.")

    downloaded = []
    skipped = []
    for item in listing:
        if not isinstance(item, dict):
            continue

        name = item.get("name", "")
        download_url = item.get("download_url")
        item_type = item.get("type")

        if item_type != "file" or not name.endswith(".json"):
            skipped.append(name)
            continue

        code = name[:-5]
        _, output_path = get_rules_dat_json_path(kind, code)
        result = download_file(download_url, output_path, github_token=github_token)
        downloaded.append(
            {
                "name": code,
                "filename": name,
                "url": download_url,
                "size": result["size"],
            }
        )

    return {
        "ok": True,
        "kind": kind,
        "url": normalized_url,
        "path": str(target_dir),
        "downloaded_count": len(downloaded),
        "skipped_count": len(skipped),
        "downloaded": downloaded,
        "elapsed_seconds": round(time.time() - started_at, 3),
    }


def download_rules_dat_rule_file(base_url, kind, code, github_token=None):
    ensure_directories()
    normalized_code, output_path = get_rules_dat_json_path(kind, code)
    file_url = normalize_rules_dat_file_url(base_url, kind, normalized_code)

    if urlparse(file_url).netloc.lower() == "api.github.com":
        metadata = load_remote_json(file_url, github_token=github_token)
        if not isinstance(metadata, dict):
            raise ValueError(f"{kind}:{normalized_code} metadata response is invalid.")

        download_url = metadata.get("download_url")
        if not download_url:
            raise ValueError(f"{kind}:{normalized_code} does not have a download_url.")
    else:
        download_url = file_url

    result = download_file(download_url, output_path, github_token=github_token)
    return {
        "ok": True,
        "kind": kind,
        "name": normalized_code,
        "filename": output_path.name,
        "url": file_url,
        "download_url": download_url,
        "path": str(output_path),
        "size": result["size"],
        "elapsed_seconds": result["elapsed_seconds"],
    }


def collect_geo_rules_from_lines(rule_lines):
    required = {
        "geosite": set(),
        "geoip": set(),
    }

    for raw_line in rule_lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        geo_reference = parse_geo_reference(line)
        if not geo_reference:
            continue

        kind, code = geo_reference
        required[kind].add(normalize_geo_code(code))

    return required


def collect_required_geo_rules():
    required = {
        "geosite": set(),
        "geoip": set(),
    }

    for rule in list_rules():
        rule_required = collect_geo_rules_from_lines(rule["content"].splitlines())
        for kind in required:
            required[kind].update(rule_required[kind])

    return required


def rules_dat_rule_is_complete(kind, code):
    normalized, path = get_rules_dat_json_path(kind, code)

    if not path.exists() or not path.is_file():
        return False

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False

    return isinstance(data.get("rules"), list)


def collect_incomplete_geo_rules(required):
    incomplete = {
        "geosite": set(),
        "geoip": set(),
    }

    with RULES_DAT_LOCK:
        for kind in incomplete:
            for code in required.get(kind, set()):
                if not rules_dat_rule_is_complete(kind, code):
                    incomplete[kind].add(normalize_geo_code(code))

    return incomplete


def update_remote_rules(config=None, required=None, missing_only=False):
    with REMOTE_UPDATE_LOCK:
        return _update_remote_rules(config, required=required, missing_only=missing_only)


def ensure_required_geo_rules(required, config=None):
    return update_remote_rules(config=config, required=required, missing_only=True)


def _update_remote_rules(config=None, required=None, missing_only=False):
    config = config or load_config()
    github_token = config.get("github_token", "")
    targets = {
        "geosite": "geosite_url",
        "geoip": "geoip_url",
    }
    results = {}
    required = required or collect_required_geo_rules()
    missing = collect_incomplete_geo_rules(required) if missing_only else None

    for name, config_key in targets.items():
        codes = sorted((missing if missing_only else required)[name])
        downloaded = []
        failed = []
        skipped = []
        started_at = time.time()

        if not codes:
            results[name] = {
                "ok": True,
                "kind": name,
                "needed_count": len(required.get(name, [])),
                "download_needed_count": 0,
                "downloaded_count": 0,
                "failed_count": 0,
                "skipped_count": 0,
                "downloaded": [],
                "failed": [],
                "skipped": [],
                "elapsed_seconds": 0,
            }
            continue

        try:
            base_url = config.get(config_key, "")
            with RULES_DAT_LOCK:
                for code in codes:
                    try:
                        downloaded.append(download_rules_dat_rule_file(base_url, name, code, github_token=github_token))
                    except urllib.error.HTTPError as error:
                        if error.code == 404:
                            skipped.append(
                                {
                                    "name": code,
                                    "error": "remote JSON rule not found",
                                }
                            )
                        else:
                            failed.append(
                                {
                                    "name": code,
                                    "error": str(error),
                                }
                            )
                    except (ValueError, OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
                        failed.append(
                            {
                                "name": code,
                                "error": str(error),
                            }
                        )

            results[name] = {
                "ok": not failed and not skipped,
                "kind": name,
                "url": config.get(config_key, ""),
                "path": str(RULES_DAT_DIR / name),
                "needed_count": len(required.get(name, [])),
                "download_needed_count": len(codes),
                "downloaded_count": len(downloaded),
                "failed_count": len(failed),
                "skipped_count": len(skipped),
                "downloaded": downloaded,
                "failed": failed,
                "skipped": skipped,
                "elapsed_seconds": round(time.time() - started_at, 3),
            }
        except Exception as error:
            results[name] = {
                "ok": False,
                "kind": name,
                "url": config.get(config_key, ""),
                "path": str(RULES_DAT_DIR / name),
                "needed_count": len(required.get(name, [])),
                "download_needed_count": len(codes),
                "downloaded_count": len(downloaded),
                "failed_count": len(failed) + 1,
                "skipped_count": len(skipped),
                "downloaded": downloaded,
                "failed": failed + [{"error": str(error)}],
                "skipped": skipped,
                "elapsed_seconds": round(time.time() - started_at, 3),
            }

    return {
        "ok": all(item.get("ok") for item in results.values()),
        "rules_dat_dir": str(RULES_DAT_DIR),
        "required": {kind: sorted(codes) for kind, codes in required.items()},
        "missing_only": missing_only,
        "incomplete": {kind: sorted(codes) for kind, codes in (missing or {}).items()},
        "files": {
            "geosite": "rules-dat/geosite/*.json",
            "geoip": "rules-dat/geoip/*.json",
        },
        "results": results,
    }


def build_cron_content(config):
    enabled = bool(config.get("auto_update_enabled", False))
    schedule = validate_cron_expression(config.get("auto_update_cron", DEFAULT_CONFIG["auto_update_cron"]))
    env_lines = []
    for key in ("GEOSITE_URL", "GEOIP_URL", "GITHUB_TOKEN"):
        value = os.environ.get(key)
        if value and "\n" not in value and "\r" not in value:
            env_lines.append(f'{key}="{value}"')

    if not enabled:
        return "\n".join(env_lines + ["# singbox-srs-generator remote rule auto update is disabled", ""])

    python_bin = os.environ.get("PYTHON_BIN", sys.executable)
    job = (
        f"{schedule} root cd {BASE_DIR} && {python_bin} {BASE_DIR / 'app.py'} "
        "--update-remote-rules >> /proc/1/fd/1 2>> /proc/1/fd/2"
    )
    return "\n".join(env_lines + [job, ""])


def sync_cron_file(config):
    if os.name == "nt":
        return None

    content = build_cron_content(config)
    CRON_FILE.write_text(content, encoding="utf-8")
    CRON_FILE.chmod(0o644)
    return str(CRON_FILE)


def load_rules_dat_rule(kind, code, line_number=None):
    normalized, path = get_rules_dat_json_path(kind, code)

    with RULES_DAT_LOCK:
        if not path.exists() or not path.is_file():
            prefix = f"Line {line_number}: " if line_number is not None else ""
            raise ValueError(f"{prefix}{kind}:{normalized} not found in rules-dat. Run remote update first.")

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            prefix = f"Line {line_number}: " if line_number is not None else ""
            raise ValueError(f"{prefix}{path.name} is not valid JSON: {error}") from error

    rules = data.get("rules")
    if not isinstance(rules, list):
        prefix = f"Line {line_number}: " if line_number is not None else ""
        raise ValueError(f"{prefix}{path.name} does not contain a rules array.")

    return {
        "kind": kind,
        "name": normalized,
        "path": str(path),
        "rules": rules,
    }


def parse_geo_reference(line):
    lower_line = line.lower()
    for kind in ("geosite", "geoip"):
        prefix = f"{kind}:"
        if lower_line.startswith(prefix):
            return kind, line[len(prefix) :].strip()

        csv_prefix = f"{kind},"
        if lower_line.startswith(csv_prefix):
            parts = [part.strip() for part in line.split(",")]
            if len(parts) >= 2:
                return kind, parts[1]

    return None


def is_plain_domain(value):
    if not isinstance(value, str):
        return False

    candidate = value.strip()
    if not candidate or any(char.isspace() for char in candidate):
        return False

    try:
        ipaddress.ip_network(candidate, strict=False)
        return False
    except ValueError:
        pass

    return bool(DOMAIN_LIKE_PATTERN.fullmatch(candidate))


def parse_plain_ip_cidr(value):
    if not isinstance(value, str):
        return None

    candidate = value.strip()
    if not candidate:
        return None

    try:
        return str(ipaddress.ip_network(candidate, strict=False))
    except ValueError:
        return None


def is_plain_keyword(value):
    if not isinstance(value, str):
        return False

    candidate = value.strip()
    if not candidate or any(char.isspace() for char in candidate):
        return False

    if ":" in candidate or "," in candidate or "/" in candidate:
        return False

    return bool(KEYWORD_LIKE_PATTERN.fullmatch(candidate))


def convert_to_singbox_json(rule_lines):
    domain = []
    domain_suffix = []
    domain_keyword = []
    domain_regex = []
    ip_cidr = []
    merged_rules = []

    for line_number, raw_line in enumerate(rule_lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        geo_reference = parse_geo_reference(line)
        if geo_reference:
            kind, code = geo_reference
            source = load_rules_dat_rule(kind, code, line_number=line_number)
            merged_rules.extend(source["rules"])
            continue

        if line.startswith("domain:"):
            value = line[len("domain:") :].strip()
            if not value:
                raise ValueError(f"Line {line_number}: domain value is empty.")
            domain_suffix.append(value)
            continue

        if line.startswith("full:"):
            value = line[len("full:") :].strip()
            if not value:
                raise ValueError(f"Line {line_number}: full value is empty.")
            domain.append(value.rstrip("."))
            continue

        if line.startswith("keyword:"):
            value = line[len("keyword:") :].strip()
            if not value:
                raise ValueError(f"Line {line_number}: keyword value is empty.")
            domain_keyword.append(value)
            continue

        if line.startswith("regexp:"):
            value = line[len("regexp:") :].strip()
            if not value:
                raise ValueError(f"Line {line_number}: regexp value is empty.")
            domain_regex.append(value)
            continue

        if is_plain_domain(line):
            domain_keyword.append(line.rstrip("."))
            continue

        parsed_ip_cidr = parse_plain_ip_cidr(line)
        if parsed_ip_cidr:
            ip_cidr.append(parsed_ip_cidr)
            continue

        if is_plain_keyword(line):
            domain_keyword.append(line)
            continue

        raise ValueError(f"Line {line_number}: unsupported rule format.")

    rule = {}
    if domain:
        rule["domain"] = domain
    if domain_suffix:
        rule["domain_suffix"] = domain_suffix
    if domain_keyword:
        rule["domain_keyword"] = domain_keyword
    if domain_regex:
        rule["domain_regex"] = domain_regex
    if ip_cidr:
        rule["ip_cidr"] = ip_cidr

    if rule:
        merged_rules.append(rule)

    return {
        "version": 3,
        "rules": merged_rules,
    }


def openclash_rule_type_for_ip_cidr(value):
    return "IP-CIDR6" if ":" in str(value) else "IP-CIDR"


def append_openclash_rule(lines, seen, rule_type, value, options=None):
    if value is None:
        return

    item = str(value).strip()
    if not item:
        return

    parts = [rule_type, item]
    if options:
        parts.extend(options)
    line = ",".join(parts)

    if line in seen:
        return

    seen.add(line)
    lines.append(line)


def dump_openclash_yaml(payload):
    if not payload:
        return "# Generated by singbox-srs-generator.\npayload: []\n"

    lines = [
        "# Generated by singbox-srs-generator.",
        "payload:",
    ]
    lines.extend(f"  - {json.dumps(item, ensure_ascii=False)}" for item in payload)
    return "\n".join(lines) + "\n"


def write_text_file_atomic(output_path, content):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_fd, temp_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.",
        suffix=".tmp",
        dir=str(output_path.parent),
    )
    os.close(temp_fd)
    temp_path = Path(temp_name)

    try:
        temp_path.write_text(content, encoding="utf-8")
        os.replace(temp_path, output_path)
    finally:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except OSError:
            pass


def openclash_rule_options(rule_target=None, no_resolve=False):
    options = []
    if rule_target:
        options.append(str(rule_target).strip())
    if no_resolve:
        options.append("no-resolve")
    return options


def convert_to_openclash_yaml(rule_lines, rule_target=None):
    payload = []
    seen = set()

    for line_number, raw_line in enumerate(rule_lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        geo_reference = parse_geo_reference(line)
        if geo_reference:
            kind, code = geo_reference
            normalized_code = normalize_geo_code(code)
            if kind == "geosite":
                append_openclash_rule(
                    payload,
                    seen,
                    "GEOSITE",
                    normalized_code,
                    options=openclash_rule_options(rule_target),
                )
            else:
                append_openclash_rule(
                    payload,
                    seen,
                    "GEOIP",
                    normalized_code.upper(),
                    options=openclash_rule_options(rule_target, no_resolve=True),
                )
            continue

        if line.startswith("domain:"):
            value = line[len("domain:") :].strip()
            if not value:
                raise ValueError(f"Line {line_number}: domain value is empty.")
            append_openclash_rule(payload, seen, "DOMAIN-SUFFIX", value, options=openclash_rule_options(rule_target))
            continue

        if line.startswith("full:"):
            value = line[len("full:") :].strip()
            if not value:
                raise ValueError(f"Line {line_number}: full value is empty.")
            append_openclash_rule(payload, seen, "DOMAIN", value.rstrip("."), options=openclash_rule_options(rule_target))
            continue

        if line.startswith("keyword:"):
            value = line[len("keyword:") :].strip()
            if not value:
                raise ValueError(f"Line {line_number}: keyword value is empty.")
            append_openclash_rule(payload, seen, "DOMAIN-KEYWORD", value, options=openclash_rule_options(rule_target))
            continue

        if line.startswith("regexp:"):
            value = line[len("regexp:") :].strip()
            if not value:
                raise ValueError(f"Line {line_number}: regexp value is empty.")
            append_openclash_rule(payload, seen, "DOMAIN-REGEX", value, options=openclash_rule_options(rule_target))
            continue

        if is_plain_domain(line):
            append_openclash_rule(
                payload,
                seen,
                "DOMAIN-KEYWORD",
                line.rstrip("."),
                options=openclash_rule_options(rule_target),
            )
            continue

        parsed_ip_cidr = parse_plain_ip_cidr(line)
        if parsed_ip_cidr:
            append_openclash_rule(
                payload,
                seen,
                openclash_rule_type_for_ip_cidr(parsed_ip_cidr),
                parsed_ip_cidr,
                options=openclash_rule_options(rule_target, no_resolve=True),
            )
            continue

        if is_plain_keyword(line):
            append_openclash_rule(payload, seen, "DOMAIN-KEYWORD", line, options=openclash_rule_options(rule_target))
            continue

        raise ValueError(f"Line {line_number}: unsupported rule format.")

    return {
        "yaml": dump_openclash_yaml(payload),
        "payload": payload,
        "skipped": [],
    }


def compile_singbox_json_to_srs(singbox_json):
    if not SING_BOX_PATH.exists():
        raise RuleConversionError("sing-box binary not found.", {"command": [str(SING_BOX_PATH)]})

    ensure_directories()
    json_fd, temp_json_name = tempfile.mkstemp(prefix=".srs-build-", suffix=".json", dir=str(RULE_SET_DIR))
    srs_fd, temp_srs_name = tempfile.mkstemp(prefix=".srs-build-", suffix=".srs", dir=str(SRS_DIR))
    os.close(json_fd)
    os.close(srs_fd)

    temp_json_path = Path(temp_json_name)
    temp_srs_path = Path(temp_srs_name)

    try:
        temp_json_path.write_text(json.dumps(singbox_json, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp_srs_path.unlink()
        command = [
            str(SING_BOX_PATH),
            "rule-set",
            "compile",
            str(temp_json_path),
            "-o",
            str(temp_srs_path),
        ]
        completed = subprocess.run(
            command,
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        result = {
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }

        if completed.returncode != 0:
            raise RuleConversionError("sing-box rule-set compile failed.", result)

        if not temp_srs_path.exists():
            raise RuleConversionError("sing-box did not create an SRS file.", result)

        result["content"] = temp_srs_path.read_bytes()
        return result
    finally:
        for path in (temp_json_path, temp_srs_path):
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass


def convert_to_srs(rule_lines):
    return compile_singbox_json_to_srs(convert_to_singbox_json(rule_lines))


def load_rule_lines_with_merged_ip(name):
    """Read a rule file and append its companion XIP.txt file when present.

    Calling this with DirectIP while Direct.txt exists resolves to Direct, so
    both single-rule and batch generation produce one merged rule set.
    """
    normalized_name = normalize_rule_name(name)
    if normalized_name.endswith("IP"):
        base_name = normalized_name[:-2]
        if base_name:
            _, base_rule_path = get_rule_path(base_name)
            if base_rule_path.is_file():
                normalized_name = base_name

    _, rule_path = get_rule_path(normalized_name)
    if not rule_path.is_file():
        raise FileNotFoundError("Rule not found.")

    rule_lines = rule_path.read_text(encoding="utf-8").splitlines()
    ip_rule_name = f"{normalized_name}IP"
    _, ip_rule_path = get_rule_path(ip_rule_name)
    if ip_rule_path.is_file():
        rule_lines.extend(ip_rule_path.read_text(encoding="utf-8").splitlines())

    return normalized_name, rule_path, rule_lines


def generate_rule_by_name(name, ensure_remote_rules=True):
    with GENERATE_LOCK:
        with RULES_LOCK:
            normalized_name, rule_path, rule_lines = load_rule_lines_with_merged_ip(name)

        _, json_path, srs_path = get_srs_paths(normalized_name)

        remote_update_result = None
        if ensure_remote_rules:
            required_geo_rules = collect_geo_rules_from_lines(rule_lines)
            remote_update_result = ensure_required_geo_rules(required_geo_rules, load_config())
            if not remote_update_result["ok"]:
                raise RuleConversionError(
                    "Referenced remote JSON rules could not be synchronized.",
                    {"remote_update": remote_update_result},
                )

        with RULES_DAT_LOCK:
            singbox_json = convert_to_singbox_json(rule_lines)

        srs_result = compile_singbox_json_to_srs(singbox_json)
        srs_content = srs_result.pop("content")

        json_fd, temp_json_name = tempfile.mkstemp(
            prefix=f".{json_path.name}.",
            suffix=".tmp",
            dir=str(json_path.parent),
        )
        srs_fd, temp_srs_name = tempfile.mkstemp(
            prefix=f".{srs_path.name}.",
            suffix=".tmp",
            dir=str(srs_path.parent),
        )
        os.close(json_fd)
        os.close(srs_fd)
        temp_json_path = Path(temp_json_name)
        temp_srs_path = Path(temp_srs_name)

        try:
            temp_json_path.write_text(
                json.dumps(singbox_json, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temp_srs_path.write_bytes(srs_content)
            os.replace(temp_json_path, json_path)
            os.replace(temp_srs_path, srs_path)
            files_index = write_srs_files_index()
        finally:
            for path in (temp_json_path, temp_srs_path):
                try:
                    if path.exists():
                        path.unlink()
                except OSError:
                    pass

        return {
            "ok": True,
            "rule": {"name": normalized_name, "filename": rule_path.name},
            "outputs": {
                "json": str(json_path.relative_to(BASE_DIR)),
                "srs": str(srs_path.relative_to(BASE_DIR)),
            },
            "singbox_json": singbox_json,
            "execution": srs_result,
            "files_index": files_index,
            "remote_update": remote_update_result,
        }


def generate_all_rules():
    results = []
    rules = list_rules()
    _, ip_skip_rules = get_merged_rule_names(rules)
    generated_rules = [rule for rule in rules if rule["name"] not in ip_skip_rules]
    required_geo_rules = {
        "geosite": set(),
        "geoip": set(),
    }

    for rule in rules:
        rule_required = collect_geo_rules_from_lines(rule["content"].splitlines())
        for kind in required_geo_rules:
            required_geo_rules[kind].update(rule_required[kind])

    remote_update_result = ensure_required_geo_rules(required_geo_rules, load_config())
    if not remote_update_result["ok"]:
        return {
            "ok": False,
            "total": len(generated_rules),
            "success_count": 0,
            "failure_count": len(generated_rules),
            "results": [],
            "remote_update": remote_update_result,
        }

    for rule in generated_rules:
        try:
            result = generate_rule_by_name(rule["name"], ensure_remote_rules=False)
            results.append(result)
        except Exception as error:
            results.append(
                {
                    "ok": False,
                    "rule": {"name": rule["name"], "filename": rule["filename"]},
                    "error": str(error),
                }
            )

    success_count = sum(1 for result in results if result.get("ok"))
    failure_count = len(results) - success_count
    return {
        "ok": failure_count == 0,
        "total": len(results),
        "success_count": success_count,
        "failure_count": failure_count,
        "results": results,
        "remote_update": remote_update_result,
    }


def get_merged_rule_names(rules):
    """Build (merged_names, ip_skip) where XIP rules are merged into X.

    If Direct.txt and DirectIP.txt both exist, DirectIP is merged into Direct.
    If only DirectIP.txt exists (no Direct), it stays standalone.
    """
    names = {r["name"] for r in rules}
    merged = set()
    ip_skip = set()
    for name in sorted(names):
        if name.endswith("IP") and name[:-2] in names:
            ip_skip.add(name)
        else:
            merged.add(name)
    return merged, ip_skip


def generate_all_openclash_rules():
    results = []
    rules = list_rules()
    combined_payload = []
    seen = set()
    skipped = []

    # Build merge map: XIP rules that should be merged into their base X
    _, ip_skip_rules = get_merged_rule_names(rules)

    for rule in rules:
        if rule["name"] in ip_skip_rules:
            results.append(
                {
                    "ok": True,
                    "rule": {"name": rule["name"], "filename": rule["filename"]},
                    "payload_count": 0,
                    "skipped": [],
                    "merged_into": rule["name"][:-2],
                }
            )
            continue

        try:
            with RULES_LOCK:
                _, _, rule_lines = load_rule_lines_with_merged_ip(rule["name"])

            openclash = convert_to_openclash_yaml(rule_lines, rule_target=rule["name"])

            for item in openclash["payload"]:
                if item in seen:
                    continue
                seen.add(item)
                combined_payload.append(item)

            if openclash["skipped"]:
                skipped.append(
                    {
                        "rule": {"name": rule["name"], "filename": rule["filename"]},
                        "skipped": openclash["skipped"],
                    }
                )

            results.append(
                {
                    "ok": True,
                    "rule": {"name": rule["name"], "filename": rule["filename"]},
                    "payload_count": len(openclash["payload"]),
                    "skipped": openclash["skipped"],
                }
            )
        except Exception as error:
            results.append(
                {
                    "ok": False,
                    "rule": {"name": rule["name"], "filename": rule["filename"]},
                    "error": str(error),
                }
            )

    success_count = sum(1 for result in results if result.get("ok"))
    failure_count = len(results) - success_count
    combined_output = None
    full_config_result = None
    if failure_count == 0:
        # Backward-compatible combined payload
        output_path = OPENCLASH_DIR / OPENCLASH_ALL_FILENAME
        write_text_file_atomic(output_path, dump_openclash_yaml(combined_payload))
        combined_output = {
            "path": str(output_path.relative_to(BASE_DIR)),
            "payload_count": len(combined_payload),
            "skipped": skipped,
        }

        # Generate full OpenClash config (overwrites openclash.yaml)
        try:
            full_config_result = generate_full_openclash_config()
        except Exception as error:
            full_config_result = {"ok": False, "error": str(error)}

    return {
        "ok": failure_count == 0,
        "total": len(results),
        "success_count": success_count,
        "failure_count": failure_count,
        "results": results,
        "combined_output": combined_output,
        "full_config": full_config_result,
    }


def load_subscribe_config():
    """Read subscribe.json and return (providers list, global_user_agent)."""
    if not SUBSCRIBE_PATH.exists():
        return [], None
    try:
        data = json.loads(SUBSCRIBE_PATH.read_text(encoding="utf-8"))
        providers = data.get("providers", [])
        global_ua = data.get("user_agent") or data.get("default_user_agent")
        return providers, global_ua
    except (json.JSONDecodeError, OSError):
        return [], None


def load_subscribe_data():
    """Read the full editable subscription configuration for the web UI."""
    with SUBSCRIBE_LOCK:
        if not SUBSCRIBE_PATH.exists():
            return {"user_agent": "", "providers": []}
        try:
            data = json.loads(SUBSCRIBE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as error:
            raise ValueError(f"Unable to read subscribe.json: {error}") from error

    if not isinstance(data, dict):
        raise ValueError("subscribe.json must contain a JSON object.")
    return data


def validate_subscribe_data(payload):
    """Validate editable subscription data while preserving supported extra fields."""
    if not isinstance(payload, dict):
        raise ValueError("Subscription configuration must be an object.")

    normalized = dict(payload)
    user_agent = payload.get("user_agent", "")
    if isinstance(user_agent, str):
        normalized_user_agent = user_agent.strip()
        if len(normalized_user_agent) > 1024:
            raise ValueError("User-Agent is too long.")
    elif isinstance(user_agent, list):
        if len(user_agent) > 20 or not all(isinstance(item, str) and item.strip() for item in user_agent):
            raise ValueError("User-Agent list must contain at most 20 non-empty strings.")
        normalized_user_agent = [item.strip() for item in user_agent]
    else:
        raise ValueError("User-Agent must be a string or a list of strings.")

    providers = payload.get("providers", [])
    if not isinstance(providers, list) or len(providers) > 100:
        raise ValueError("providers must be a list containing at most 100 items.")

    normalized_providers = []
    provider_names = set()
    for index, provider in enumerate(providers, start=1):
        if not isinstance(provider, dict):
            raise ValueError(f"Provider {index} must be an object.")

        name = provider.get("name", "")
        url = provider.get("url", "")
        if not isinstance(name, str) or not PROVIDER_NAME_PATTERN.fullmatch(name.strip()):
            raise ValueError(f"Provider {index} has an invalid name.")
        if not isinstance(url, str):
            raise ValueError(f"Provider {index} URL must be a string.")

        name = name.strip()
        url = url.strip()
        parsed_url = urlparse(url)
        if parsed_url.scheme not in ("http", "https") or not parsed_url.netloc:
            raise ValueError(f"Provider {index} URL must be a valid HTTP(S) URL.")
        if name in provider_names:
            raise ValueError(f"Provider name '{name}' is duplicated.")
        provider_names.add(name)

        interval = provider.get("interval", 86400)
        if isinstance(interval, bool):
            raise ValueError(f"Provider {index} interval must be a number.")
        try:
            interval = int(interval)
        except (TypeError, ValueError) as error:
            raise ValueError(f"Provider {index} interval must be a number.") from error
        if not 60 <= interval <= 2_592_000:
            raise ValueError(f"Provider {index} interval must be between 60 and 2592000 seconds.")

        use_for_ai = provider.get("use_for_ai", True)
        if not isinstance(use_for_ai, bool):
            raise ValueError(f"Provider {index} use_for_ai must be true or false.")

        use_for_latency = provider.get("use_for_latency", False)
        if not isinstance(use_for_latency, bool):
            raise ValueError(f"Provider {index} use_for_latency must be true or false.")

        canonical_keys = {"name", "url", "interval", "use_for_ai", "use_for_latency"}
        normalized_provider = {
            "name": name,
            "url": url,
            "interval": interval,
            "use_for_ai": use_for_ai,
            "use_for_latency": use_for_latency,
        }
        normalized_provider.update(
            {key: value for key, value in provider.items() if key not in canonical_keys}
        )
        normalized_providers.append(normalized_provider)

    normalized["user_agent"] = normalized_user_agent
    normalized["providers"] = normalized_providers
    return normalized


def save_subscribe_data(data):
    """Atomically save the editable subscription configuration."""
    with SUBSCRIBE_LOCK:
        write_text_file_atomic(
            SUBSCRIBE_PATH,
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        )


def generate_proxy_providers_yaml(providers, global_user_agent=None):
    """Generate proxy-providers YAML section from subscribe.json providers list."""

    def sanitize_yaml_string(value):
        if value is None:
            return ""
        s = str(value)
        if any(c in s for c in ":,#{}[]&*!|>'\"%@`"):
            return json.dumps(s, ensure_ascii=False)
        return s

    lines = [
        "# ===== 代理提供商（自动生成，编辑 config/subscribe.json 修改）=====",
    ]

    # If there's a global UA, define it once as a YAML anchor
    ua_anchor = None
    if global_user_agent:
        ua_list = global_user_agent if isinstance(global_user_agent, list) else [global_user_agent]
        ua_anchor = "x-ua"
        lines.append(f"{ua_anchor}: &{ua_anchor}")
        lines.append(f"  header:")
        lines.append(f"    User-Agent:")
        for ua in ua_list:
            lines.append(f'      - "{ua}"')
        lines.append("")

    lines.append("proxy-providers:")

    for p in providers:
        name = sanitize_yaml_string(p.get("name", "Unknown"))
        url = str(p.get("url", ""))
        interval = int(p.get("interval", 86400))
        hc = p.get("health_check")
        override = p.get("override", {})

        lines.append(f"  {name}:")

        # If provider has its own UA, write inline; otherwise use anchor
        custom_ua = p.get("user_agent")
        if ua_anchor and not custom_ua:
            lines.append(f"    <<: *{ua_anchor}")
        elif custom_ua:
            ua_list = custom_ua if isinstance(custom_ua, list) else [custom_ua]
            lines.append(f"    header:")
            lines.append(f"      User-Agent:")
            for ua in ua_list:
                lines.append(f'        - "{ua}"')

        lines.append(f"    type: http")
        lines.append(f"    interval: {interval}")

        # Health check (only when explicitly configured)
        if isinstance(hc, dict) and hc.get("enable", True):
            hc_url = sanitize_yaml_string(
                hc.get("url", "https://www.gstatic.com/generate_204")
            )
            hc_interval = int(hc.get("interval", 300))
            lines.append(f"    health-check:")
            lines.append(f"      enable: true")
            lines.append(f"      url: {hc_url}")
            lines.append(f"      interval: {hc_interval}")

        # Override (e.g. additional-prefix)
        if override:
            lines.append(f"    override:")
            for k, v in override.items():
                # Normalize underscores to hyphens for OpenClash YAML keys
                yaml_key = k.replace("_", "-")
                lines.append(f"      {yaml_key}: {sanitize_yaml_string(v)}")

        # Path and URL - keep Unicode characters, only strip forbidden path chars
        safe_name = re.sub(r'[\\/:*?"<>|]', '_', name)
        lines.append(f'    path: "./proxies/{safe_name}.yaml"')
        lines.append(f"    url: {sanitize_yaml_string(url)}")

    return "\n".join(lines) + "\n"


def get_provider_country_groups(providers):
    """Return (group_name, provider_name, filter) tuples for every airport/country pair."""
    return [
        (f"{provider.get('name', 'Unknown')}·{country}", provider.get("name", "Unknown"), node_filter)
        for provider in providers
        for country, node_filter in COUNTRY_NODE_FILTERS
    ]


def get_ai_providers(providers):
    """Return providers enabled for the AI policy group (enabled by default)."""
    return [provider for provider in providers if provider.get("use_for_ai", True)]


def generate_provider_country_groups_yaml(providers, url_test_config):
    """Generate latency-test groups that filter one provider to one country/region."""
    groups = get_provider_country_groups(providers)
    lines = []
    for index, (group_name, provider_name, node_filter) in enumerate(groups):
        if index:
            lines.append("")
        prefix = "- name" if index == 0 else "  - name"
        lines.extend(
            [
                f"{prefix}: {group_name}",
                "    type: url-test",
                *[f"    {line}" for line in get_url_test_option_lines(url_test_config)],
                "    use:",
                f"      - {provider_name}",
                f"    filter: '{node_filter}'",
            ]
        )
    return "\n".join(lines)


def generate_provider_country_references_yaml(providers):
    """Generate proxy-group references for every airport/country pair."""
    names = [group_name for group_name, _, _ in get_provider_country_groups(providers)]
    if not names:
        return ""
    return "\n".join([f"- {names[0]}", *[f"      - {name}" for name in names[1:]]])


def read_template():
    """Read template.yaml, return preamble + parsed generator sections.

    Returns:
        dict with keys: preamble, rule_mapping, custom_rules
        Returns None if template not found.
    """
    if not TEMPLATE_PATH.exists():
        return None

    text = TEMPLATE_PATH.read_text(encoding="utf-8")

    # Split at the generator-only marker
    marker = "# ===== Generator-Only Sections"
    marker_idx = text.find(marker)

    if marker_idx == -1:
        # No marker — whole file is preamble
        return {
            "preamble": text,
            "rule_mapping": {},
            "custom_rules": [],
        }

    preamble = text[:marker_idx].rstrip()
    generator_text = text[marker_idx:]

    rule_mapping = {}
    custom_rules = []
    current_section = None

    for raw_line in generator_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("#"):
            continue

        # Detect section switches (top-level keys at column 0)
        if line == "rule_mapping:":
            current_section = "rule_mapping"
            continue
        if line == "custom_rules:":
            current_section = "custom_rules"
            continue

        # Skip sub-keys of sections we don't parse
        if current_section is None:
            continue

        # A line back at column 0 means a new section started
        if not raw_line.startswith(" ") and raw_line.strip():
            current_section = None
            continue

        if current_section == "rule_mapping" and ":" in line:
            key, _, value = line.partition(":")
            rule_mapping[key.strip()] = value.strip().strip('"').strip("'")

        elif current_section == "custom_rules" and line.startswith("- "):
            custom_rules.append(line[2:].strip().strip('"').strip("'"))

    return {
        "preamble": preamble,
        "rule_mapping": rule_mapping,
        "custom_rules": custom_rules,
    }


def load_ports_config():
    """Read ports.json and return list of direct port ranges."""
    if not PORTS_PATH.exists():
        return []
    try:
        data = json.loads(PORTS_PATH.read_text(encoding="utf-8"))
        return data.get("direct_ports", [])
    except (json.JSONDecodeError, OSError):
        return []


def generate_rules_yaml(rule_mapping, custom_rules, direct_ports=None):
    """Generate rules: YAML section with inline rules from rules/*.txt + custom_rules.

    Each rule file is read, converted to classical format with the mapped proxy
    group as the target, and inlined directly into the rules section.
    No external rule-provider references needed.
    """
    if not rule_mapping:
        return ""

    lines = [
        "# ===== 分流规则（自动生成）=====",
        "rules:",
    ]

    # Non-MATCH custom rules first (DST-PORT etc.)
    match_rules = [r for r in custom_rules if r.startswith("MATCH,")]
    for rule in custom_rules:
        if not rule.startswith("MATCH,"):
            lines.append(f"  - {rule}")

    # BT/PT direct ports from ports.json
    if direct_ports:
        for port_range in direct_ports:
            lines.append(f"  - DST-PORT,{port_range},Direct")

    # Inline rules from each mapped rule file (in template definition order)
    for rule_name, proxy_group in rule_mapping.items():
        try:
            _, _, rule_lines = load_rule_lines_with_merged_ip(rule_name)
            converted = convert_to_openclash_yaml(rule_lines, rule_target=proxy_group)
            for item in converted["payload"]:
                lines.append(f"  - {item}")
        except (OSError, ValueError):
            # Skip rules that can't be read
            pass

    # MATCH rules at the very end (catch-all)
    for rule in match_rules:
        lines.append(f"  - {rule}")

    return "\n".join(lines) + "\n"


def generate_full_openclash_config():
    """Generate full OpenClash configuration YAML.

    Combines:
      1. template.yaml preamble (base settings, proxy-groups)
      2. proxy-providers from subscribe.json
      3. rule-providers from rules/*.txt + rule_mapping
      4. rules from rule_mapping + custom_rules

    Returns dict with result info, or {"ok": false, "error": ...} on failure.
    """
    # Step 1: Load template
    template = read_template()
    if template is None:
        return {
            "ok": False,
            "error": "Template not found: openclash/template.yaml",
        }

    rule_mapping = template.get("rule_mapping", {})
    custom_rules = template.get("custom_rules", [])

    # Filter rule_mapping: remove IP rules that will be merged into their base
    rules_list = list_rules()
    effective_names, _ = get_merged_rule_names(rules_list)
    rule_mapping = {k: v for k, v in rule_mapping.items() if k in effective_names}

    if not rule_mapping:
        return {"ok": False, "error": "rule_mapping is empty in template."}

    # Step 2: Load subscriptions and shared url-test settings
    providers, global_ua = load_subscribe_config()
    try:
        url_test_config = validate_url_test_config(load_stored_config().get("url_test", {}))
    except ValueError as error:
        return {"ok": False, "error": f"Invalid URL-test configuration: {error}"}

    # Step 2.5: Replace provider-related placeholders
    provider_names = [p.get("name", "Unknown") for p in providers]
    ai_providers = get_ai_providers(providers)
    preamble = template["preamble"]
    if provider_names and "__ALLNODES__" in preamble:
        # First line inherits indentation from where __ALLNODES__ sits in the template
        provider_lines = [f"- {provider_names[0]}"]
        provider_lines += [f"      - {name}" for name in provider_names[1:]]
        providers_yaml_block = "\n".join(provider_lines)
        preamble = preamble.replace("__ALLNODES__", providers_yaml_block)

    if provider_names and "__PROVIDER_GROUPS__" in preamble:
        group_lines = []
        for i, provider in enumerate(providers):
            name = provider.get("name", "Unknown")
            if i == 0:
                # First line inherits template indentation
                group_lines.append(f"- name: {name}")
            else:
                group_lines.append(f"  - name: {name}")
            group_lines.append("    type: url-test" if provider.get("use_for_latency", False) else "    type: select")
            if provider.get("use_for_latency", False):
                group_lines.extend(f"    {line}" for line in get_url_test_option_lines(url_test_config))
            group_lines.append(f"    use:")
            group_lines.append(f"      - {name}")
        provider_groups_block = "\n".join(group_lines)
        preamble = preamble.replace("__PROVIDER_GROUPS__", provider_groups_block)

    if "__PROVIDER_COUNTRY_GROUPS__" in preamble:
        preamble = preamble.replace(
            "__PROVIDER_COUNTRY_GROUPS__", generate_provider_country_groups_yaml(ai_providers, url_test_config)
        )

    if "__URL_TEST_OPTIONS__" in preamble:
        preamble = preamble.replace(
            "__URL_TEST_OPTIONS__", generate_url_test_options_yaml(url_test_config)
        )

    if "__PROVIDER_COUNTRY_NODES__" in preamble:
        preamble = preamble.replace(
            "__PROVIDER_COUNTRY_NODES__", generate_provider_country_references_yaml(ai_providers)
        )

    if "__RULE_GROUPS__" in preamble:
        rule_group_lines = []
        # Auto-generate select groups for rules where rule_name == target_group
        auto_rules = [(k, v) for k, v in sorted(rule_mapping.items()) if k == v]
        for i, (rule_name, _) in enumerate(auto_rules):
            if i > 0:
                rule_group_lines.append("")
            if len(rule_group_lines) == 0:
                rule_group_lines.append(f"- name: {rule_name}")
            else:
                rule_group_lines.append(f"  - name: {rule_name}")
            rule_group_lines.append(f"  type: select")
            rule_group_lines.append(f"  proxies:")
            rule_group_lines.append(f"    - ALL·延迟最低")
            for name in provider_names:
                rule_group_lines.append(f"    - {name}")
            rule_group_lines.append(f"    - 🌐 本机·本地直连")
        rule_groups_block = "\n".join(rule_group_lines)
        preamble = preamble.replace("__RULE_GROUPS__", rule_groups_block)

    # Step 3: Generate YAML sections
    proxy_providers_yaml = generate_proxy_providers_yaml(providers, global_ua) if providers else ""
    direct_ports = load_ports_config()
    rules_yaml = generate_rules_yaml(rule_mapping, custom_rules, direct_ports)

    # Step 4: Assemble
    # Insert proxy-providers at __PROXY_PROVIDERS__ marker in the preamble
    proxy_marker = "__PROXY_PROVIDERS__"
    if proxy_marker in preamble and proxy_providers_yaml:
        before, after = preamble.split(proxy_marker, 1)
        parts = [before.rstrip(), "", proxy_providers_yaml.rstrip(), after]
    else:
        # No marker: append at end (fallback)
        parts = [preamble.rstrip()]
        if proxy_providers_yaml:
            parts.append("")
            parts.append(proxy_providers_yaml.rstrip())

    parts.append("")
    parts.append(rules_yaml.rstrip())
    parts.append("")

    full_yaml = "\n".join(parts)

    # Step 5: Write output
    output_path = OPENCLASH_DIR / OPENCLASH_ALL_FILENAME
    write_text_file_atomic(output_path, full_yaml)

    return {
        "ok": True,
        "path": str(output_path.relative_to(BASE_DIR)),
        "mapping_count": len(rule_mapping),
        "provider_count": len(providers),
        "proxy_providers": [p.get("name", "Unknown") for p in providers],
    }


def update_remote_rules_and_generate(config=None):
    remote_result = update_remote_rules(config)
    generate_result = None

    if remote_result["ok"]:
        generate_result = generate_all_rules()

    return {
        "ok": remote_result["ok"] and (generate_result is None or generate_result["ok"]),
        "remote_update": remote_result,
        "generate_all": generate_result,
    }


class AppHandler(SimpleHTTPRequestHandler):
    server_version = "SingboxSrsGenerator/0.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/config":
            self.send_json(public_config(load_config()))
            return

        if parsed.path == "/api/subscribe":
            try:
                self.send_json({"subscribe": load_subscribe_data(), "subscribe_path": str(SUBSCRIBE_PATH)})
            except ValueError as error:
                self.send_json({"error": str(error)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        if parsed.path == "/api/rules":
            self.send_json({"rules": list_rules()})
            return

        if parsed.path == "/api/rules/order":
            self.send_json({"order": read_rule_order(), "order_path": str(ORDER_PATH)})
            return

        if parsed.path == "/api/srs":
            self.send_json({"files": list_srs_files()})
            return

        if parsed.path == "/api/remote/status":
            self.send_json({"rules_dat_dir": str(RULES_DAT_DIR), "files": get_remote_rule_files()})
            return

        if parsed.path == "/":
            self.path = "/index.html"

        super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/config":
            payload = self.read_json_body()
            if payload is None:
                return

            config = load_stored_config()
            try:
                for key in ("geosite_url", "geoip_url"):
                    if key in payload:
                        config[key] = payload[key]

                if "github_token" in payload:
                    config["github_token"] = str(payload["github_token"]).strip()

                if "auto_update_enabled" in payload:
                    config["auto_update_enabled"] = bool(payload["auto_update_enabled"])

                if "auto_update_cron" in payload:
                    config["auto_update_cron"] = validate_cron_expression(payload["auto_update_cron"])

                if "url_test" in payload:
                    current_url_test = config.get("url_test")
                    merged_url_test = dict(DEFAULT_URL_TEST_CONFIG)
                    if isinstance(current_url_test, dict):
                        merged_url_test.update(current_url_test)
                    if not isinstance(payload["url_test"], dict):
                        raise ValueError("URL-test configuration must be an object.")
                    merged_url_test.update(payload["url_test"])
                    config["url_test"] = validate_url_test_config(merged_url_test)
            except ValueError as error:
                self.send_json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
                return

            save_config(config)
            effective_config = apply_environment_overrides(dict(config))
            response = {
                "ok": True,
                "config": public_config(config),
                "effective_config": public_config(effective_config),
                "config_path": str(CONFIG_PATH),
            }
            try:
                cron_file = sync_cron_file(effective_config)
                if cron_file:
                    response["cron_file"] = cron_file
            except OSError as error:
                response["cron_warning"] = str(error)
            self.send_json(response)
            return

        if parsed.path == "/api/subscribe":
            payload = self.read_json_body()
            if payload is None:
                return
            try:
                subscribe_data = validate_subscribe_data(payload)
                save_subscribe_data(subscribe_data)
            except ValueError as error:
                self.send_json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
                return
            except OSError as error:
                self.send_json({"error": str(error)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            self.send_json(
                {
                    "ok": True,
                    "subscribe": subscribe_data,
                    "subscribe_path": str(SUBSCRIBE_PATH),
                }
            )
            return

        if parsed.path == "/api/rules/create":
            self.handle_rule_create()
            return

        if parsed.path == "/api/rules/update":
            self.handle_rule_update()
            return

        if parsed.path == "/api/rules/delete":
            self.handle_rule_delete()
            return

        if parsed.path == "/api/rules/order":
            self.handle_rule_order()
            return

        if parsed.path == "/api/generate":
            self.handle_generate()
            return

        if parsed.path == "/api/generate/all":
            self.handle_generate_all()
            return

        if parsed.path == "/api/generate/openclash/all":
            self.handle_generate_openclash_all()
            return

        if parsed.path == "/api/remote/update":
            self.handle_remote_update()
            return

        self.send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)

    def handle_rule_create(self):
        payload = self.read_json_body()
        if payload is None:
            return

        content = payload.get("content", "")
        if not isinstance(content, str):
            self.send_json({"error": "Rule content must be a string."}, status=HTTPStatus.BAD_REQUEST)
            return

        try:
            name, rule_path = get_rule_path(payload.get("name"))
        except ValueError as error:
            self.send_json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
            return

        with RULES_LOCK:
            if rule_path.exists():
                self.send_json({"error": "Rule already exists."}, status=HTTPStatus.CONFLICT)
                return

            rule_path.write_text(content, encoding="utf-8")
            existing = existing_rule_filenames()
            current_order = [
                filename
                for filename in read_rule_order()
                if filename in existing and filename != rule_path.name
            ]
            save_rule_order(current_order + [rule_path.name])
        self.send_json({"ok": True, "rule": {"name": name, "filename": rule_path.name}}, status=HTTPStatus.CREATED)

    def handle_rule_update(self):
        payload = self.read_json_body()
        if payload is None:
            return

        content = payload.get("content", "")
        if not isinstance(content, str):
            self.send_json({"error": "Rule content must be a string."}, status=HTTPStatus.BAD_REQUEST)
            return

        try:
            name, rule_path = get_rule_path(payload.get("name"))
        except ValueError as error:
            self.send_json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
            return

        with RULES_LOCK:
            if not rule_path.exists() or not rule_path.is_file():
                self.send_json({"error": "Rule not found."}, status=HTTPStatus.NOT_FOUND)
                return

            rule_path.write_text(content, encoding="utf-8")
        self.send_json({"ok": True, "rule": {"name": name, "filename": rule_path.name}})

    def handle_rule_delete(self):
        payload = self.read_json_body()
        if payload is None:
            return

        try:
            name, rule_path = get_rule_path(payload.get("name"))
        except ValueError as error:
            self.send_json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
            return

        with RULES_LOCK:
            if not rule_path.exists() or not rule_path.is_file():
                self.send_json({"error": "Rule not found."}, status=HTTPStatus.NOT_FOUND)
                return

            rule_path.unlink()
            existing = existing_rule_filenames()
            save_rule_order([filename for filename in read_rule_order() if filename in existing])
        self.send_json({"ok": True, "rule": {"name": name, "filename": rule_path.name}})

    def handle_rule_order(self):
        payload = self.read_json_body()
        if payload is None:
            return

        filenames = payload.get("order", payload.get("filenames"))
        if not isinstance(filenames, list):
            self.send_json({"error": "Rule order must be a list of filenames."}, status=HTTPStatus.BAD_REQUEST)
            return

        try:
            order = update_rule_order(filenames)
        except ValueError as error:
            self.send_json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
            return
        except OSError as error:
            self.send_json({"error": str(error)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        self.send_json({"ok": True, "order": order, "order_path": str(ORDER_PATH)})

    def handle_generate(self):
        payload = self.read_json_body()
        if payload is None:
            return

        try:
            result = generate_rule_by_name(payload.get("name"))
        except ValueError as error:
            self.send_json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
            return
        except FileNotFoundError:
            self.send_json({"error": "Rule not found."}, status=HTTPStatus.NOT_FOUND)
            return
        except subprocess.TimeoutExpired as error:
            self.send_json(
                {
                    "error": "sing-box rule-set compile timed out.",
                    "command": error.cmd,
                    "timeout": error.timeout,
                },
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return
        except OSError as error:
            self.send_json({"error": str(error)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        except RuleConversionError as error:
            response = {"error": str(error)}
            response.update(error.result)
            self.send_json(response, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        self.send_json(result)

    def handle_generate_all(self):
        self.send_json(generate_all_rules())

    def handle_generate_openclash_all(self):
        self.send_json(generate_all_openclash_rules())

    def handle_remote_update(self):
        result = update_remote_rules(load_config())
        status = HTTPStatus.OK if result["ok"] else HTTPStatus.BAD_GATEWAY
        self.send_json(result, status=status)

    def read_json_body(self):
        content_length = self.headers.get("Content-Length", "0")
        try:
            length = int(content_length)
        except ValueError:
            self.send_json({"error": "Invalid Content-Length"}, status=HTTPStatus.BAD_REQUEST)
            return None

        if length < 0:
            self.send_json({"error": "Invalid Content-Length"}, status=HTTPStatus.BAD_REQUEST)
            return None

        if length > MAX_JSON_BODY_BYTES:
            self.send_json(
                {
                    "error": "JSON body is too large.",
                    "max_bytes": MAX_JSON_BODY_BYTES,
                },
                status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
            return None

        raw_body = self.rfile.read(length) if length > 0 else b"{}"
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_json({"error": "Invalid JSON body"}, status=HTTPStatus.BAD_REQUEST)
            return None

        if not isinstance(payload, dict):
            self.send_json({"error": "JSON body must be an object."}, status=HTTPStatus.BAD_REQUEST)
            return None

        return payload

    def send_json(self, payload, status=HTTPStatus.OK):
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run():
    ensure_directories()

    server = ThreadingHTTPServer(("0.0.0.0", APP_PORT), AppHandler)
    print(f"singbox-srs-generator listening on http://127.0.0.1:{APP_PORT}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        server.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="singbox-srs-generator")
    parser.add_argument(
        "--update-remote-rules",
        action="store_true",
        help="Download remote sing-box geosite and geoip JSON rule files, generate all rule sets, then exit.",
    )
    args = parser.parse_args(argv)

    if args.update_remote_rules:
        result = update_remote_rules_and_generate(load_config())
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1

    run()
    return 0


if __name__ == "__main__":
    sys.exit(main())

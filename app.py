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
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", str(BASE_DIR / "config" / "config.json")))
WEB_DIR = BASE_DIR / "web"
RULES_DIR = BASE_DIR / "rules"
RULE_SET_DIR = BASE_DIR / "rule-set"
SRS_DIR = RULE_SET_DIR / "srs"
RULES_DAT_DIR = BASE_DIR / "rules-dat"
SING_BOX_PATH = Path(os.environ.get("SING_BOX_PATH", str(BASE_DIR / "bin" / ("sing-box.exe" if os.name == "nt" else "sing-box"))))
CRON_FILE = Path(os.environ.get("CRON_FILE", "/etc/cron.d/singbox-srs-generator"))
APP_PORT = 9044
MAX_JSON_BODY_BYTES = 1024 * 1024

CONFIG_LOCK = threading.RLock()
RULES_LOCK = threading.RLock()
RULES_DAT_LOCK = threading.RLock()
GENERATE_LOCK = threading.Lock()
REMOTE_UPDATE_LOCK = threading.Lock()


DEFAULT_CONFIG = {
    "geosite_url": "https://api.github.com/repos/MetaCubeX/meta-rules-dat/contents/geo/geosite?ref=sing",
    "geoip_url": "https://api.github.com/repos/MetaCubeX/meta-rules-dat/contents/geo/geoip?ref=sing",
    "github_token": "",
    "auto_update_enabled": False,
    "auto_update_cron": "0 4 * * *",
}

RULE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
GEO_CODE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.!@+\-]{0,127}$")
CRON_FIELD_PATTERN = re.compile(r"^[A-Za-z0-9*/,\-]+$")
DOMAIN_LIKE_PATTERN = re.compile(r"^(?=.{1,253}$)(?!-)[A-Za-z0-9_.*-]+(?:\.[A-Za-z0-9_.*-]+)+\.?$")
KEYWORD_LIKE_PATTERN = re.compile(r"^[A-Za-z0-9_.!@+*-]+$")


class RuleConversionError(Exception):
    def __init__(self, message, result=None):
        super().__init__(message)
        self.result = result or {}


def ensure_directories():
    RULES_DIR.mkdir(exist_ok=True)
    RULE_SET_DIR.mkdir(exist_ok=True)
    SRS_DIR.mkdir(exist_ok=True)
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
        config.update(data)
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

        return sorted(rules, key=lambda rule: (rule["created_ns"], rule["filename"].lower()))


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

    return files


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


def collect_required_geo_rules():
    required = {
        "geosite": set(),
        "geoip": set(),
    }

    for rule in list_rules():
        for raw_line in rule["content"].splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue

            geo_reference = parse_geo_reference(line)
            if not geo_reference:
                continue

            kind, code = geo_reference
            required[kind].add(normalize_geo_code(code))

    return required


def update_remote_rules(config=None):
    with REMOTE_UPDATE_LOCK:
        return _update_remote_rules(config)


def _update_remote_rules(config=None):
    config = config or load_config()
    github_token = config.get("github_token", "")
    targets = {
        "geosite": "geosite_url",
        "geoip": "geoip_url",
    }
    results = {}
    required = collect_required_geo_rules()

    for name, config_key in targets.items():
        codes = sorted(required[name])
        downloaded = []
        failed = []
        skipped = []
        started_at = time.time()

        if not codes:
            results[name] = {
                "ok": True,
                "kind": name,
                "needed_count": 0,
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
                "needed_count": len(codes),
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
                "needed_count": len(codes),
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


def generate_rule_by_name(name):
    with GENERATE_LOCK:
        normalized_name, rule_path = get_rule_path(name)
        _, json_path, srs_path = get_srs_paths(normalized_name)

        with RULES_LOCK:
            if not rule_path.exists() or not rule_path.is_file():
                raise FileNotFoundError("Rule not found.")
            rule_lines = rule_path.read_text(encoding="utf-8").splitlines()

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

        if parsed.path == "/api/rules":
            self.send_json({"rules": list_rules()})
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

        if parsed.path == "/api/rules/create":
            self.handle_rule_create()
            return

        if parsed.path == "/api/rules/update":
            self.handle_rule_update()
            return

        if parsed.path == "/api/rules/delete":
            self.handle_rule_delete()
            return

        if parsed.path == "/api/generate":
            self.handle_generate()
            return

        if parsed.path == "/api/generate/all":
            self.handle_generate_all()
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
        self.send_json({"ok": True, "rule": {"name": name, "filename": rule_path.name}})

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
        results = []

        for rule in list_rules():
            try:
                result = generate_rule_by_name(rule["name"])
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
        self.send_json(
            {
                "ok": failure_count == 0,
                "total": len(results),
                "success_count": success_count,
                "failure_count": failure_count,
                "results": results,
            }
        )

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
        help="Download remote sing-box geosite and geoip JSON rule files according to config.json, then exit.",
    )
    args = parser.parse_args(argv)

    if args.update_remote_rules:
        result = update_remote_rules(load_config())
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1

    run()
    return 0


if __name__ == "__main__":
    sys.exit(main())

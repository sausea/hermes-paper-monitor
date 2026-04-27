#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import email.utils
import functools
import html
import hashlib
import http.server
import http.cookiejar
import json
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


DEFAULT_TIMEOUT_SECONDS = 25
DEFAULT_MAX_ITEMS = 30
DEFAULT_USER_AGENT = "HermesPaperMonitor/0.1 (+local agent)"
DEFAULT_SUMMARY_MAX_OUTPUT_TOKENS = 1024
DEFAULT_TITLE_MAX_OUTPUT_TOKENS = 512
MAX_LLM_OUTPUT_TOKENS = 4096
DEFAULT_TRANSLATION_SYSTEM_PROMPT = (
    "You are a careful academic translator. Translate the provided English paper abstract into a "
    "faithful and concise Simplified Chinese summary in 2-4 sentences. Focus on the problem, "
    "method, and contribution when they are present. Return Chinese only and do not add headings."
)
DEFAULT_TITLE_TRANSLATION_SYSTEM_PROMPT = (
    "You are a careful academic translator. Translate the provided English paper title into an "
    "accurate and concise Simplified Chinese academic title. Preserve technical meaning, return "
    "Chinese only, and do not add quotation marks or explanations."
)
KEYWORD_ALIASES = {
    "visual inertial": ["visual-inertial", "visual inertial odometry", "vio", "visual odometry", "视觉惯性", "视惯"],
    "vehicle": ["vehicles", "automotive", "autonomous driving", "intelligent vehicle", "车辆", "车载", "无人车", "自动驾驶"],
    "state estimation": ["state-estimation", "pose estimation", "状态估计", "位姿估计"],
    "location": ["localization", "定位", "定姿"],
    "inertial": ["imu", "imu-aided", "inertial navigation", "惯性", "惯导", "惯性测量"],
    "odometry": ["odo", "lio", "vio", "里程计", "激光惯性里程计", "视觉里程计"],
    "navigation": ["positioning", "guidance", "导航", "组合导航", "卫星导航"],
    "ins": ["sins", "imu", "inertial navigation system", "惯性导航", "组合惯导"],
    "gnss": ["global navigation satellite system", "bds", "beidou", "北斗", "卫星导航"],
    "gps": ["global positioning system", "global positioning", "gps定位"],
    "5g": ["5g-a", "5g positioning", "5g localization", "5g定位"],
    "slam": ["vslam", "lio-sam", "simultaneous localization and mapping", "同步定位与建图"],
    "geomagnetic": ["magnetic", "地磁", "geomagnetic field"],
    "gpr": ["ground penetrating radar", "ground-penetrating radar", "探地雷达"],
}
ARTICLE_URL_HINTS = (
    "/article/doi/",
    "/zh/article/doi/",
    "/cn/article/doi/",
    "/article/id/",
    "/document/",
    "/science/article/",
    "doi.org/10.",
)
UTILITY_LINK_TEXT = {
    "",
    "abstract",
    "html",
    "pdf",
    "full text",
    "download pdf",
    "references",
    "related articles",
    "supplementary material",
    "access statistics",
    "摘要",
    "全文",
    "参考文献",
    "相关文章",
    "资源附件",
    "访问统计",
}
UTILITY_URL_HINTS = (
    "viewtype=references",
    "viewtype=relative",
    "viewtype=resource",
    "viewtype=access",
    "citedby-info",
    "/top_view",
    "/top_down",
    "/gettopcitedby",
)
DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    paper_id TEXT PRIMARY KEY,
    dedupe_key TEXT NOT NULL UNIQUE,
    slug TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_label TEXT NOT NULL,
    publisher TEXT,
    collection TEXT,
    title TEXT NOT NULL,
    zh_title TEXT,
    abstract TEXT,
    zh_summary TEXT,
    authors_json TEXT NOT NULL,
    affiliations_json TEXT NOT NULL,
    doi TEXT,
    arxiv_id TEXT,
    categories_json TEXT NOT NULL,
    keywords_json TEXT NOT NULL,
    landing_url TEXT,
    pdf_url TEXT,
    pdf_path TEXT,
    published_at TEXT,
    discovered_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_papers_published_at ON papers (published_at DESC);
CREATE INDEX IF NOT EXISTS idx_papers_source_id ON papers (source_id);
CREATE INDEX IF NOT EXISTS idx_papers_status ON papers (status);
"""


@dataclass
class LLMTranslationConfig:
    enabled: bool = False
    api_endpoint: str = ""
    model: str = ""
    api_key: str = ""
    api_key_env: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    extra_body: dict[str, Any] = field(default_factory=dict)
    system_prompt: str = DEFAULT_TRANSLATION_SYSTEM_PROMPT
    title_system_prompt: str = DEFAULT_TITLE_TRANSLATION_SYSTEM_PROMPT
    temperature: float = 0.2
    max_output_tokens: int = DEFAULT_SUMMARY_MAX_OUTPUT_TOKENS
    title_max_output_tokens: int = DEFAULT_TITLE_MAX_OUTPUT_TOKENS
    only_translate_english: bool = True

    @classmethod
    def from_dict(cls, raw: Any) -> "LLMTranslationConfig":
        if not isinstance(raw, dict):
            return cls()
        api_key_env = clean_text(str(raw.get("api_key_env", "")))
        api_key = clean_text(str(raw.get("api_key", "")))
        api_key = resolve_api_key(api_key, api_key_env)
        system_prompt = str(raw.get("system_prompt", "")).strip() or DEFAULT_TRANSLATION_SYSTEM_PROMPT
        title_system_prompt = (
            str(raw.get("title_system_prompt", "")).strip() or DEFAULT_TITLE_TRANSLATION_SYSTEM_PROMPT
        )
        return cls(
            enabled=bool(raw.get("enabled", False)),
            api_endpoint=resolve_translation_endpoint(str(raw.get("api_endpoint", ""))),
            model=clean_text(str(raw.get("model", ""))),
            api_key=api_key,
            api_key_env=api_key_env,
            headers={str(k): str(v) for k, v in dict(raw.get("headers", {})).items()},
            extra_body=dict(raw.get("extra_body", {})),
            system_prompt=system_prompt,
            title_system_prompt=title_system_prompt,
            temperature=float(raw.get("temperature", 0.2)),
            max_output_tokens=int(raw.get("max_output_tokens", DEFAULT_SUMMARY_MAX_OUTPUT_TOKENS)),
            title_max_output_tokens=int(raw.get("title_max_output_tokens", DEFAULT_TITLE_MAX_OUTPUT_TOKENS)),
            only_translate_english=bool(raw.get("only_translate_english", True)),
        )

    def validate(self) -> None:
        if not self.enabled:
            return
        missing: list[str] = []
        if not self.api_endpoint:
            missing.append("api_endpoint")
        if not self.model:
            missing.append("model")
        if not self.api_key:
            missing.append("api_key/api_key_env")
        if missing:
            raise ValueError(
                "llm_translation is enabled but missing required fields: " + ", ".join(missing)
            )


@dataclass
class ServiceConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    refresh_interval_minutes: int = 360
    daily_refresh_times: list[str] = field(default_factory=list)
    initial_refresh: bool = True

    @classmethod
    def from_dict(cls, raw: Any) -> "ServiceConfig":
        if not isinstance(raw, dict):
            return cls()
        host = clean_text(str(raw.get("host", ""))) or "127.0.0.1"
        port = int(raw.get("port", 8765))
        refresh_interval_minutes = int(raw.get("refresh_interval_minutes", 360))
        daily_refresh_times = normalize_refresh_times(raw.get("daily_refresh_times"))
        initial_refresh = bool(raw.get("initial_refresh", True))
        return cls(
            host=host,
            port=port,
            refresh_interval_minutes=refresh_interval_minutes,
            daily_refresh_times=daily_refresh_times,
            initial_refresh=initial_refresh,
        )


@dataclass
class RuntimePaths:
    root_dir: Path
    config_path: Path
    database_path: Path
    pdf_dir: Path
    site_dir: Path
    staging_dir: Path
    assets_dir: Path
    download_pdfs: bool
    since_days: int
    timeout_seconds: int
    max_items_per_source: int
    user_agent: str
    workflow_mode: str
    translation: LLMTranslationConfig
    service: ServiceConfig


@dataclass
class SourceSpec:
    id: str
    label: str
    kind: str
    url: str
    journal_issn: str = ""
    api_endpoint: str = ""
    api_payload: dict[str, Any] = field(default_factory=dict)
    article_url_templates: list[str] = field(default_factory=list)
    enrichment_url_templates: list[str] = field(default_factory=list)
    publisher: str = ""
    collection: str = ""
    enabled: bool = True
    requires_cookies: bool = False
    cookie_file: str = ""
    pdf_policy: str = "best-effort"
    include_url_patterns: list[str] = field(default_factory=list)
    exclude_url_patterns: list[str] = field(default_factory=list)
    follow_article_pages: bool = True
    max_items: int = DEFAULT_MAX_ITEMS
    categories: list[str] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any], default_max_items: int) -> "SourceSpec":
        return cls(
            id=str(raw.get("id", "")).strip(),
            label=str(raw.get("label", "")).strip(),
            kind=str(raw.get("kind", "")).strip(),
            url=str(raw.get("url", "")).strip(),
            journal_issn=clean_text(str(raw.get("journal_issn", ""))),
            api_endpoint=str(raw.get("api_endpoint", "")).strip(),
            api_payload=dict(raw.get("api_payload", {})),
            article_url_templates=clean_list(raw.get("article_url_templates")),
            enrichment_url_templates=clean_list(raw.get("enrichment_url_templates")),
            publisher=clean_text(str(raw.get("publisher", ""))),
            collection=clean_text(str(raw.get("collection", ""))),
            enabled=bool(raw.get("enabled", True)),
            requires_cookies=bool(raw.get("requires_cookies", False)),
            cookie_file=str(raw.get("cookie_file", "")).strip(),
            pdf_policy=clean_text(str(raw.get("pdf_policy", "best-effort"))).lower() or "best-effort",
            include_url_patterns=clean_list(raw.get("include_url_patterns")),
            exclude_url_patterns=clean_list(raw.get("exclude_url_patterns")),
            follow_article_pages=bool(raw.get("follow_article_pages", True)),
            max_items=int(raw.get("max_items", default_max_items or DEFAULT_MAX_ITEMS)),
            categories=clean_list(raw.get("categories")),
            headers={str(k): str(v) for k, v in dict(raw.get("headers", {})).items()},
        )


class CollectorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta_tags: list[dict[str, str]] = []
        self.link_tags: list[dict[str, str]] = []
        self.anchors: list[dict[str, str]] = []
        self._current_href: str | None = None
        self._current_anchor_text: list[str] = []
        self._in_title = False
        self._title_parts: list[str] = []
        self.title_text = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {str(key).lower(): str(value or "") for key, value in attrs}
        if tag == "meta":
            self.meta_tags.append(attr_map)
        elif tag == "link":
            self.link_tags.append(attr_map)
        elif tag == "a":
            self._current_href = attr_map.get("href", "")
            self._current_anchor_text = []
        elif tag == "title":
            self._in_title = True
            self._title_parts = []

    def handle_data(self, data: str) -> None:
        if self._current_href is not None:
            self._current_anchor_text.append(data)
        if self._in_title:
            self._title_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._current_href is not None:
            self.anchors.append(
                {
                    "href": self._current_href,
                    "text": clean_text("".join(self._current_anchor_text)),
                }
            )
            self._current_href = None
            self._current_anchor_text = []
        elif tag == "title":
            self._in_title = False
            self.title_text = clean_text("".join(self._title_parts))
            self._title_parts = []


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def isoformat_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def clean_text(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    value = value.replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def clean_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [item for item in (clean_text(str(entry)) for entry in value) if item]
    text = clean_text(str(value))
    if not text:
        return []
    if ";" in text:
        parts = text.split(";")
    elif "|" in text:
        parts = text.split("|")
    else:
        parts = text.split(",")
    return [item for item in (clean_text(part) for part in parts) if item]


def resolve_api_key(api_key: str, api_key_env: str) -> str:
    normalized_api_key = clean_text(api_key)
    if normalized_api_key:
        return normalized_api_key
    normalized_env_name = clean_text(api_key_env)
    if not normalized_env_name:
        return ""
    env_value = clean_text(str(os.environ.get(normalized_env_name, "")))
    if env_value:
        return env_value
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", normalized_env_name):
        return normalized_env_name
    return ""


def resolve_translation_endpoint(raw_url: str) -> str:
    normalized = clean_text(raw_url)
    if not normalized:
        return ""
    parsed = urllib.parse.urlparse(normalized)
    path = parsed.path.rstrip("/")
    if path.endswith("/chat/completions") or path.endswith("/responses") or path.endswith("/completions"):
        return normalized
    if path in {"", "/v1"} or re.fullmatch(r"/v\d+", path):
        next_path = f"{path}/chat/completions" if path else "/chat/completions"
        return urllib.parse.urlunparse(parsed._replace(path=next_path))
    return normalized


def normalize_refresh_times(value: Any) -> list[str]:
    raw_items: list[str] = []
    if isinstance(value, list):
        for entry in value:
            raw_items.extend(clean_list(entry))
    else:
        raw_items = clean_list(value)

    normalized: list[str] = []
    seen: set[str] = set()
    for entry in raw_items:
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", clean_text(entry))
        if not match:
            raise ValueError(f"invalid refresh time '{entry}'; expected HH:MM")
        hour = int(match.group(1))
        minute = int(match.group(2))
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            raise ValueError(f"invalid refresh time '{entry}'; expected HH:MM")
        normalized_value = f"{hour:02d}:{minute:02d}"
        if normalized_value in seen:
            continue
        seen.add(normalized_value)
        normalized.append(normalized_value)
    return sorted(normalized)


def next_daily_refresh_time(refresh_times: list[str], reference: datetime | None = None) -> datetime:
    if not refresh_times:
        raise ValueError("refresh_times must not be empty")
    current = reference or datetime.now().astimezone()
    if current.tzinfo is None:
        current = current.astimezone()

    candidates: list[datetime] = []
    for refresh_time in refresh_times:
        hour_text, minute_text = refresh_time.split(":", 1)
        candidate = current.replace(
            hour=int(hour_text),
            minute=int(minute_text),
            second=0,
            microsecond=0,
        )
        if candidate < current:
            candidate += timedelta(days=1)
        candidates.append(candidate)
    return min(candidates)


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=False)


def decode_json_list(value: str) -> list[str]:
    value = value or ""
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return clean_list(value)
    if isinstance(parsed, list):
        return [item for item in (clean_text(str(entry)) for entry in parsed) if item]
    return clean_list(value)


def slugify(value: str, fallback: str = "item") -> str:
    normalized = clean_text(value).lower()
    normalized = re.sub(r"[^a-z0-9]+", "-", normalized)
    normalized = re.sub(r"-{2,}", "-", normalized).strip("-")
    return normalized or fallback


def local_name(tag: str) -> str:
    return tag.split("}", 1)[-1].lower()


def parse_datetime(raw: str) -> str:
    raw = clean_text(raw)
    if not raw:
        return ""
    iso_candidate = raw.replace("Z", "+00:00")
    try:
        value = datetime.fromisoformat(iso_candidate)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return isoformat_utc(value)
    except ValueError:
        pass
    try:
        value = email.utils.parsedate_to_datetime(raw)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return isoformat_utc(value)
    except (TypeError, ValueError, IndexError):
        return ""


def parse_date_safe(raw: str) -> datetime | None:
    parsed = parse_datetime(raw)
    if not parsed:
        return None
    return datetime.fromisoformat(parsed.replace("Z", "+00:00"))


def load_config(config_path: Path) -> tuple[RuntimePaths, list[str], list[str], list[SourceSpec]]:
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)

    workspace = raw.get("workspace", {})
    keywords = raw.get("keywords", {})
    translation = LLMTranslationConfig.from_dict(raw.get("llm_translation", {}))
    service = ServiceConfig.from_dict(raw.get("service", {}))
    root_dir = config_path.parent.resolve()
    database_path = resolve_path(root_dir, workspace.get("database", "../runtime/papers.sqlite"))
    pdf_dir = resolve_path(root_dir, workspace.get("pdf_dir", "../site/pdfs"))
    site_dir = resolve_path(root_dir, workspace.get("site_dir", "../site"))
    staging_dir = resolve_path(root_dir, workspace.get("staging_dir", "../runtime/staging"))
    assets_dir = root_dir.parent / "assets" / "site"

    runtime = RuntimePaths(
        root_dir=root_dir.parent,
        config_path=config_path.resolve(),
        database_path=database_path,
        pdf_dir=pdf_dir,
        site_dir=site_dir,
        staging_dir=staging_dir,
        assets_dir=assets_dir,
        download_pdfs=bool(workspace.get("download_pdfs", True)),
        since_days=int(workspace.get("since_days", 7)),
        timeout_seconds=int(workspace.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)),
        max_items_per_source=int(workspace.get("max_items_per_source", DEFAULT_MAX_ITEMS)),
        user_agent=str(workspace.get("user_agent", DEFAULT_USER_AGENT)).strip() or DEFAULT_USER_AGENT,
        workflow_mode=clean_text(str(workspace.get("workflow_mode", "full"))).lower() or "full",
        translation=translation,
        service=service,
    )

    source_specs = [
        SourceSpec.from_dict(entry, runtime.max_items_per_source)
        for entry in raw.get("sources", [])
        if isinstance(entry, dict)
    ]
    keyword_terms = clean_list(keywords.get("terms"))
    category_terms = clean_list(keywords.get("categories"))
    return runtime, keyword_terms, category_terms, source_specs


def resolve_path(base_dir: Path, raw_value: Any) -> Path:
    path = Path(str(raw_value)).expanduser()
    if path.is_absolute():
        return path
    return (base_dir / path).resolve()


def resolve_url(base_dir: Path, raw_url: str) -> str:
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.scheme:
        return raw_url
    return resolve_path(base_dir, raw_url).as_uri()


def ensure_workspace(runtime: RuntimePaths) -> None:
    runtime.database_path.parent.mkdir(parents=True, exist_ok=True)
    runtime.pdf_dir.mkdir(parents=True, exist_ok=True)
    runtime.site_dir.mkdir(parents=True, exist_ok=True)
    runtime.staging_dir.mkdir(parents=True, exist_ok=True)


def connect_database(runtime: RuntimePaths) -> sqlite3.Connection:
    ensure_workspace(runtime)
    connection = sqlite3.connect(runtime.database_path)
    connection.row_factory = sqlite3.Row
    connection.executescript(DB_SCHEMA)
    ensure_database_columns(connection)
    return connection


def ensure_database_columns(connection: sqlite3.Connection) -> None:
    columns = {
        str(row["name"]): row
        for row in connection.execute("PRAGMA table_info(papers)").fetchall()
    }
    if "zh_title" not in columns:
        connection.execute("ALTER TABLE papers ADD COLUMN zh_title TEXT")
        connection.commit()


def build_headers(runtime: RuntimePaths, source: SourceSpec) -> dict[str, str]:
    headers = {
        "User-Agent": runtime.user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    headers.update(source.headers)
    return headers


def build_opener(source: SourceSpec, runtime: RuntimePaths) -> urllib.request.OpenerDirector:
    handlers: list[Any] = []
    cookie_file = source.cookie_file.strip()
    if cookie_file:
        jar = http.cookiejar.MozillaCookieJar()
        cookie_path = resolve_path(runtime.config_path.parent, cookie_file)
        if cookie_path.exists():
            jar.load(str(cookie_path), ignore_discard=True, ignore_expires=True)
            handlers.append(urllib.request.HTTPCookieProcessor(jar))
    return urllib.request.build_opener(*handlers)


def has_auth_material(source: SourceSpec, runtime: RuntimePaths) -> bool:
    if source.headers:
        return True
    cookie_file = source.cookie_file.strip()
    if not cookie_file:
        return False
    cookie_path = resolve_path(runtime.config_path.parent, cookie_file)
    return cookie_path.exists()


def fetch_bytes(url: str, runtime: RuntimePaths, source: SourceSpec) -> tuple[bytes, str, str]:
    opener = build_opener(source, runtime)
    request = urllib.request.Request(url, headers=build_headers(runtime, source))
    with opener.open(request, timeout=runtime.timeout_seconds) as response:
        body = response.read()
        final_url = response.geturl()
        content_type = response.headers.get("Content-Type", "")
        return body, final_url, content_type


def fetch_text(url: str, runtime: RuntimePaths, source: SourceSpec) -> tuple[str, str]:
    body, final_url, _ = fetch_bytes(url, runtime, source)
    return body.decode("utf-8", errors="replace"), final_url


def fetch_json_post(url: str, payload: dict[str, Any], runtime: RuntimePaths, source: SourceSpec) -> tuple[dict[str, Any], str]:
    opener = build_opener(source, runtime)
    headers = build_headers(runtime, source)
    headers.update(
        {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }
    )
    encoded_payload = urllib.parse.urlencode(
        [
            (str(key), "" if value is None else str(value))
            for key, value in payload.items()
        ]
    ).encode("utf-8")
    request = urllib.request.Request(url, data=encoded_payload, headers=headers, method="POST")
    with opener.open(request, timeout=runtime.timeout_seconds) as response:
        body = response.read().decode("utf-8", errors="replace")
        final_url = response.geturl()
    return json.loads(body), final_url


def post_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout_seconds: int) -> tuple[dict[str, Any], str]:
    encoded_payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=encoded_payload, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        body = response.read().decode("utf-8", errors="replace")
        final_url = response.geturl()
    return json.loads(body), final_url


def download_file(url: str, target_path: Path, runtime: RuntimePaths, source: SourceSpec) -> None:
    body, _, _ = fetch_bytes(url, runtime, source)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(body)


def collect_document(html_text: str) -> CollectorParser:
    parser = CollectorParser()
    parser.feed(html_text)
    parser.close()
    return parser


def meta_map(document: CollectorParser) -> dict[str, list[str]]:
    mapped: dict[str, list[str]] = {}
    for meta in document.meta_tags:
        key = (
            meta.get("name")
            or meta.get("property")
            or meta.get("itemprop")
            or meta.get("http-equiv")
            or ""
        ).strip().lower()
        value = clean_text(meta.get("content", ""))
        if not key or not value:
            continue
        mapped.setdefault(key, []).append(value)
    return mapped


def first_meta(meta_values: dict[str, list[str]], *keys: str) -> str:
    for key in keys:
        values = meta_values.get(key.lower(), [])
        for value in values:
            if clean_text(value):
                return clean_text(value)
    return ""


def guess_pdf_url(document: CollectorParser, page_url: str) -> str:
    for link in document.link_tags:
        href = link.get("href", "")
        if href and href.lower().endswith(".pdf"):
            return urllib.parse.urljoin(page_url, href)
    for anchor in document.anchors:
        href = anchor.get("href", "")
        if not href:
            continue
        href_lower = href.lower()
        text_lower = anchor.get("text", "").lower()
        if href_lower.endswith(".pdf") or "/pdf" in href_lower or "downloadpdf" in href_lower:
            return urllib.parse.urljoin(page_url, href)
        if "pdf" in text_lower:
            return urllib.parse.urljoin(page_url, href)
    return ""


def split_keywords(raw: str) -> list[str]:
    if not raw:
        return []
    parts = re.split(r"[;,|；，]", raw)
    return [item for item in (clean_text(part) for part in parts) if item]


def extract_doi(value: str) -> str:
    match = re.search(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", value or "", re.IGNORECASE)
    if not match:
        return ""
    return clean_text(match.group(0))


def render_url_template(template: str, doi: str) -> str:
    template = clean_text(template)
    doi = clean_text(doi)
    if not template or not doi:
        return ""
    encoded_doi = urllib.parse.quote(doi, safe="/-._;():")
    return template.replace("{doi}", encoded_doi)


def parse_datetime_value(value: Any) -> str:
    if isinstance(value, (int, float)):
        if value <= 0:
            return ""
        timestamp = float(value)
        if timestamp > 1_000_000_000_000:
            timestamp = timestamp / 1000.0
        return isoformat_utc(datetime.fromtimestamp(timestamp, tz=timezone.utc))
    return parse_datetime(str(value))


def is_probably_english_text(text: str) -> bool:
    normalized = clean_text(text)
    if not normalized:
        return False
    ascii_word_count = len(re.findall(r"[A-Za-z]{2,}", normalized))
    ascii_letter_count = len(re.findall(r"[A-Za-z]", normalized))
    cjk_count = len(re.findall(r"[\u4e00-\u9fff]", normalized))
    return ascii_word_count >= 12 and ascii_letter_count >= max(cjk_count * 3, 40)


def contains_cjk_text(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", clean_text(text)))


def is_probably_english_title(text: str) -> bool:
    normalized = clean_text(text)
    if not normalized:
        return False
    ascii_word_count = len(re.findall(r"[A-Za-z]{2,}", normalized))
    ascii_letter_count = len(re.findall(r"[A-Za-z]", normalized))
    cjk_count = len(re.findall(r"[\u4e00-\u9fff]", normalized))
    return ascii_word_count >= 2 and ascii_letter_count >= max(cjk_count * 2 + 4, 8)


def extract_chat_message_text(response_payload: dict[str, Any]) -> str:
    choices = response_payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        return ""
    message = first_choice.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return clean_text(content)
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            text_value = clean_text(str(item.get("text", "")))
            if text_value:
                parts.append(text_value)
        return clean_text(" ".join(parts))
    return ""


def normalize_llm_output_tokens(value: int) -> int:
    if value <= 0:
        return 0
    return min(value, MAX_LLM_OUTPUT_TOKENS)


def strip_translation_error_notes(
    note: str,
    *,
    remove_title_errors: bool = False,
    remove_summary_errors: bool = False,
) -> str:
    cleaned = clean_text(note)
    if not cleaned:
        return ""

    markers: list[str] = []
    if remove_title_errors:
        markers.append("llm-title-translation-error:")
    if remove_summary_errors:
        markers.append("llm-translation-error:")
    if not markers:
        return cleaned

    boundary = r"(?=\s(?:llm-title-translation-error:|llm-translation-error:|discovered-via-[^\s]+|[a-z][a-z0-9_-]*:)|$)"
    for marker in markers:
        pattern = re.compile(rf"(?:^|\s){re.escape(marker)}.*?{boundary}")
        cleaned = pattern.sub(" ", cleaned)
    return clean_text(cleaned)


def describe_empty_chat_response(response_payload: dict[str, Any]) -> str:
    choices = response_payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return "translation API returned an empty completion"
    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        return "translation API returned an empty completion"
    finish_reason = clean_text(str(first_choice.get("finish_reason", ""))).lower()
    message = first_choice.get("message")
    if isinstance(message, dict):
        reasoning = clean_text(str(message.get("reasoning_content", "")))
        if reasoning:
            if finish_reason == "length":
                return (
                    "translation API returned reasoning but no final content; "
                    "max_tokens is likely too low for the selected model"
                )
            return "translation API returned reasoning but no final content"
    if finish_reason == "length":
        return "translation API stopped before producing a final completion"
    return "translation API returned an empty completion"


def build_title_translation_user_prompt(record: dict[str, Any]) -> str:
    title = clean_text(str(record.get("title", "")))
    parts = [
        "Please translate the following English paper title into an accurate Simplified Chinese title.",
        "Requirements: preserve technical meaning, Chinese only, no explanations.",
        f"Title: {title}",
    ]
    return "\n\n".join(parts)


def build_summary_translation_user_prompt(record: dict[str, Any]) -> str:
    title = clean_text(str(record.get("title", "")))
    abstract = clean_text(str(record.get("abstract", "")))
    parts = [
        "Please translate the following English paper abstract into a concise Simplified Chinese summary.",
        "Requirements: 2-4 sentences, faithful to the source, no invented details, Chinese only.",
    ]
    if title:
        parts.append(f"Title: {title}")
    parts.append(f"Abstract: {abstract}")
    return "\n\n".join(parts)


def translate_with_llm(
    user_prompt: str,
    system_prompt: str,
    runtime: RuntimePaths,
    max_output_tokens: int,
) -> str:
    config = runtime.translation
    effective_max_output_tokens = normalize_llm_output_tokens(max_output_tokens)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    headers.update(config.headers)

    payload: dict[str, Any] = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": config.temperature,
    }
    if effective_max_output_tokens > 0:
        payload["max_tokens"] = effective_max_output_tokens
    payload.update(config.extra_body)

    response_payload, _ = post_json(
        config.api_endpoint,
        payload,
        headers,
        runtime.timeout_seconds,
    )
    translated = extract_chat_message_text(response_payload)
    if translated:
        return translated
    raise ValueError(describe_empty_chat_response(response_payload))


def translate_title_with_llm(record: dict[str, Any], runtime: RuntimePaths) -> str:
    return translate_with_llm(
        build_title_translation_user_prompt(record),
        runtime.translation.title_system_prompt,
        runtime,
        runtime.translation.title_max_output_tokens,
    )


def translate_summary_with_llm(record: dict[str, Any], runtime: RuntimePaths) -> str:
    return translate_with_llm(
        build_summary_translation_user_prompt(record),
        runtime.translation.system_prompt,
        runtime,
        runtime.translation.max_output_tokens,
    )


def extract_affiliations_from_html(html_text: str) -> list[str]:
    affiliations: list[str] = []
    for value in re.findall(r'(?is)<a[^>]+/organ/detail[^>]*>(.*?)</a>', html_text or ""):
        text = clean_text(value)
        text = re.sub(r"^\d+\.\s*", "", text)
        if text:
            affiliations.append(text)
    return unique_preserve(affiliations)


def extract_keywords_from_html(html_text: str) -> list[str]:
    hidden_values = [
        clean_text(value)
        for value in re.findall(r'(?is)<input[^>]+class=["\'][^"\']*param-keyrowd[^"\']*["\'][^>]+value=["\'](.*?)["\']', html_text or "")
    ]
    if hidden_values:
        return unique_preserve([value for value in hidden_values if value])

    match = re.search(
        r'(?is)<span[^>]*>\s*关键词[:：]?\s*</span>\s*<p[^>]+class=["\'][^"\']*keywords[^"\']*["\'][^>]*>(.*?)</p>',
        html_text or "",
    )
    if not match:
        return []
    return unique_preserve(split_keywords(clean_text(match.group(1))))


def normalize_affiliation_text(value: str) -> str:
    text = clean_text(value)
    text = re.sub(r"^\d+\.\s*", "", text)
    text = text.rstrip(";；")
    return text


def extract_tpldata_records(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    data = payload.get("data")
    if not isinstance(data, dict):
        return [], 0
    if data.get("result") == "success" and isinstance(data.get("data"), dict):
        data = data.get("data") or {}

    if isinstance(data.get("pagerFilter"), dict):
        pager = data.get("pagerFilter") or {}
        records = pager.get("records") or []
        totalpage = int(pager.get("totalpage") or 0)
        return [item for item in records if isinstance(item, dict)], totalpage

    if isinstance(data.get("data"), dict):
        inner = data.get("data") or {}
        records = inner.get("records") or []
        totalpage = int(inner.get("totalpage") or 0)
        return [item for item in records if isinstance(item, dict)], totalpage

    records = data.get("records") or []
    totalpage = int(data.get("totalpage") or 0)
    return [item for item in records if isinstance(item, dict)], totalpage


def extract_tpldata_authors(item: dict[str, Any]) -> list[str]:
    authors: list[str] = []
    for author in item.get("authors") or []:
        if not isinstance(author, dict):
            continue
        name = clean_text(str(author.get("authorNameCn", ""))) or clean_text(str(author.get("authorNameEn", "")))
        if name:
            authors.append(name)
    return unique_preserve(authors)


def extract_tpldata_affiliations(item: dict[str, Any]) -> list[str]:
    affiliations: list[str] = []
    for entry in item.get("affiliations") or []:
        if not isinstance(entry, dict):
            continue
        text = normalize_affiliation_text(
            str(entry.get("addressCn", "")) or str(entry.get("addressEn", ""))
        )
        if text:
            affiliations.append(text)
    if affiliations:
        return unique_preserve(affiliations)

    raw = clean_text(str(item.get("affiliationsInfoCn", ""))) or clean_text(str(item.get("affiliationsInfoEn", "")))
    if not raw:
        return []
    return unique_preserve(
        [
            normalize_affiliation_text(part)
            for part in re.split(r"\s*;;\s*", raw)
            if normalize_affiliation_text(part)
        ]
    )


def extract_tpldata_categories(item: dict[str, Any]) -> list[str]:
    categories: list[str] = []
    for keyword in item.get("keywords") or []:
        if not isinstance(keyword, dict):
            continue
        value = clean_text(str(keyword.get("keywordCn", ""))) or clean_text(str(keyword.get("keywordEn", "")))
        if value:
            categories.append(value)
    for field_name in ("categoryNameCn", "categoryNameEn"):
        value = clean_text(str(item.get(field_name, "")))
        if value:
            categories.append(value)
    return unique_preserve(categories)


def build_record_landing_url(source: SourceSpec, doi: str, article_id: str) -> str:
    if doi and source.article_url_templates:
        for template in source.article_url_templates:
            rendered = render_url_template(template, doi)
            if rendered:
                return rendered

    base = source.url or source.api_endpoint
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme and parsed.netloc:
        if doi:
            return urllib.parse.urlunsplit(
                (parsed.scheme, parsed.netloc, f"/cn/article/doi/{urllib.parse.quote(doi, safe='/-._;():')}", "", "")
            )
        if article_id:
            return urllib.parse.urlunsplit(
                (parsed.scheme, parsed.netloc, f"/cn/article/id/{urllib.parse.quote(article_id)}", "", "")
            )
    return base


def extract_tpldata_record(item: dict[str, Any], source: SourceSpec) -> dict[str, Any]:
    doi = clean_text(str(item.get("doi", "")))
    article_id = clean_text(str(item.get("id", "")))
    release_progress = item.get("releaseProgress") if isinstance(item.get("releaseProgress"), dict) else {}
    published_at = (
        parse_datetime_value((release_progress or {}).get("maxLastReleaseTime"))
        or parse_datetime_value((release_progress or {}).get("lastReleaseTime"))
        or parse_datetime_value(item.get("publishDate"))
        or parse_datetime_value(item.get("onlineDate"))
    )
    journal = item.get("journal") if isinstance(item.get("journal"), dict) else {}
    collection = (
        clean_text(str(journal.get("titleCn", "")))
        or clean_text(str(journal.get("titleEn", "")))
        or source.collection
    )

    return {
        "source_id": source.id,
        "source_label": source.label,
        "publisher": source.publisher,
        "collection": collection,
        "title": clean_text(str(item.get("titleCn", ""))) or clean_text(str(item.get("titleEn", ""))),
        "abstract": clean_text(str(item.get("abstractinfoCn", ""))) or clean_text(str(item.get("abstractinfoEn", ""))),
        "authors": extract_tpldata_authors(item),
        "affiliations": extract_tpldata_affiliations(item),
        "doi": doi,
        "arxiv_id": "",
        "categories": extract_tpldata_categories(item),
        "keywords": [],
        "landing_url": build_record_landing_url(source, doi, article_id),
        "pdf_url": "",
        "published_at": published_at,
        "notes": "discovered-via-tpl-data",
    }


def extract_published_at_from_html(html_text: str) -> str:
    visible_text = clean_text(html_text)
    patterns = [
        r"(?:网络首发时间|网络出版日期|在线公开时间|在线发布时间|网络发布日期|最新更新时间|更新时间)[:：]?\s*([0-9]{4}-[0-9]{2}-[0-9]{2}(?:\s+[0-9]{2}:[0-9]{2}:[0-9]{2})?)",
        r"(?:published online|online published|published at)[:：]?\s*([0-9]{4}-[0-9]{2}-[0-9]{2}(?:\s+[0-9]{2}:[0-9]{2}:[0-9]{2})?)",
    ]
    for pattern in patterns:
        match = re.search(pattern, visible_text, re.IGNORECASE)
        if not match:
            continue
        parsed = parse_datetime(match.group(1))
        if parsed:
            return parsed
    return ""


def extract_html_abstract(html_text: str) -> str:
    patterns = [
        r'(?is)<input[^>]+id=["\']abstract_text["\'][^>]+value=["\'](.*?)["\']',
        r'(?is)<div[^>]+class=["\'][^"\']*abstract[^"\']*["\'][^>]*>(.*?)</div>',
        r'(?is)<section[^>]+class=["\'][^"\']*abstract[^"\']*["\'][^>]*>(.*?)</section>',
        r'(?is)<span[^>]+id=["\']ChDivSummary["\'][^>]*>(.*?)</span>',
        r'(?is)<span[^>]+class=["\'][^"\']*abstract-text[^"\']*["\'][^>]*>(.*?)</span>',
        r'(?is)<h[1-6][^>]*>\s*(?:摘要|abstract)\s*</h[1-6]>\s*(?:<[^>]+>\s*)*<p[^>]*>(.*?)</p>',
    ]
    for pattern in patterns:
        match = re.search(pattern, html_text)
        if not match:
            continue
        abstract = clean_text(match.group(1))
        if abstract:
            return abstract
    return ""


def extract_ieee_xplore_metadata(page_url: str, html_text: str) -> dict[str, Any]:
    match = re.search(r"(?s)xplGlobal\.document\.metadata\s*=\s*(\{.*?\});", html_text or "")
    if not match:
        return {}
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}

    authors: list[str] = []
    affiliations: list[str] = []
    for author in payload.get("authors") or []:
        if not isinstance(author, dict):
            continue
        name = clean_text(str(author.get("name", "")))
        if name:
            authors.append(name)
        for affiliation in author.get("affiliation") or []:
            text = clean_text(str(affiliation))
            if text:
                affiliations.append(text)

    categories: list[str] = []
    for keyword_group in payload.get("keywords") or []:
        if not isinstance(keyword_group, dict):
            continue
        for keyword in keyword_group.get("kwd") or []:
            text = clean_text(str(keyword))
            if text:
                categories.append(text)

    published_at = ""
    for key in ("insertDate", "displayPublicationDate", "dateOfInsertion", "publicationDate"):
        published_at = parse_datetime(str(payload.get(key, "")))
        if published_at:
            break

    pdf_url = clean_text(str(payload.get("pdfUrl", ""))) or clean_text(str(payload.get("pdfPath", "")))
    if pdf_url:
        pdf_url = urllib.parse.urljoin(page_url, pdf_url)

    return {
        "title": clean_text(str(payload.get("title", ""))) or clean_text(str(payload.get("displayDocTitle", ""))),
        "abstract": clean_text(str(payload.get("abstract", ""))),
        "authors": unique_preserve(authors),
        "affiliations": unique_preserve(affiliations),
        "doi": clean_text(str(payload.get("doi", ""))),
        "categories": unique_preserve(categories),
        "collection": clean_text(str(payload.get("publicationTitle", ""))) or clean_text(str(payload.get("displayPublicationTitle", ""))),
        "pdf_url": pdf_url,
        "published_at": published_at,
    }


def extract_article_metadata(page_url: str, html_text: str, source: SourceSpec) -> dict[str, Any]:
    document = collect_document(html_text)
    meta_values = meta_map(document)
    ieee_metadata = {}
    if "ieeexplore.ieee.org" in urllib.parse.urlsplit(page_url).netloc:
        ieee_metadata = extract_ieee_xplore_metadata(page_url, html_text)
    title = first_meta(
        meta_values,
        "citation_title",
        "dc.title",
        "dcterms.title",
        "og:title",
    ) or document.title_text or ieee_metadata.get("title", "")
    abstract = first_meta(
        meta_values,
        "citation_abstract",
        "citation_description",
        "dcterms.abstract",
        "og:description",
        "dc.description",
        "dcterms.description",
        "description",
    )
    if ieee_metadata.get("abstract") and len(clean_text(str(ieee_metadata.get("abstract", "")))) > len(abstract):
        abstract = clean_text(str(ieee_metadata.get("abstract", "")))
    if not abstract:
        abstract = extract_html_abstract(html_text)
    journal_title = first_meta(
        meta_values,
        "citation_journal_title",
        "prism.publicationname",
        "dc.source",
        "og:site_name",
    ) or clean_text(str(ieee_metadata.get("collection", "")))
    published_at = first_meta(
        meta_values,
        "citation_publication_date",
        "citation_online_date",
        "citation_date",
        "dc.date",
        "dcterms.date",
        "prism.publicationdate",
        "article:published_time",
    )
    doi = first_meta(meta_values, "citation_doi", "dc.identifier")
    authors = meta_values.get("citation_author", [])
    if not authors:
        author_value = first_meta(meta_values, "dc.creator", "dcterms.creator", "author")
        authors = split_keywords(author_value)
    authors = unique_preserve(authors + clean_list(ieee_metadata.get("authors")))
    affiliations = meta_values.get("citation_author_institution", [])
    if not affiliations:
        affiliations = split_keywords(first_meta(meta_values, "citation_author_institution"))
    if not affiliations:
        affiliations = extract_affiliations_from_html(html_text)
    affiliations = unique_preserve(affiliations + clean_list(ieee_metadata.get("affiliations")))
    categories = split_keywords(
        first_meta(
            meta_values,
            "citation_keywords",
            "dc.keywords",
            "dcterms.subject",
            "keywords",
        )
    )
    if not categories:
        categories = extract_keywords_from_html(html_text)
    categories = unique_preserve(categories + clean_list(ieee_metadata.get("categories")))
    pdf_meta_url = first_meta(meta_values, "citation_pdf_url")
    if pdf_meta_url:
        pdf_url = urllib.parse.urljoin(page_url, pdf_meta_url)
    else:
        pdf_url = guess_pdf_url(document, page_url)
    if not pdf_url:
        pdf_url = clean_text(str(ieee_metadata.get("pdf_url", "")))

    if not doi:
        doi = extract_doi(html_text)
    if not doi:
        doi = clean_text(str(ieee_metadata.get("doi", "")))

    published_at_value = parse_datetime(published_at)
    if not published_at_value:
        published_at_value = extract_published_at_from_html(html_text)
    if not published_at_value:
        published_at_value = clean_text(str(ieee_metadata.get("published_at", "")))

    return {
        "source_id": source.id,
        "source_label": source.label,
        "publisher": source.publisher,
        "collection": source.collection or journal_title,
        "title": title,
        "abstract": abstract,
        "authors": authors,
        "affiliations": affiliations,
        "doi": doi,
        "arxiv_id": "",
        "categories": categories,
        "keywords": [],
        "landing_url": page_url,
        "pdf_url": pdf_url,
        "published_at": published_at_value,
        "notes": "",
    }


def merge_records(primary: dict[str, Any], secondary: dict[str, Any]) -> dict[str, Any]:
    merged = dict(primary)
    for key, value in secondary.items():
        if key in {"authors", "affiliations", "categories", "keywords"}:
            existing = clean_list(primary.get(key))
            incoming = clean_list(value)
            merged[key] = unique_preserve(existing + incoming)
            continue
        if key == "abstract":
            existing_text = clean_text(str(primary.get("abstract", "")))
            incoming_text = clean_text(str(value))
            merged[key] = incoming_text if len(incoming_text) > len(existing_text) else existing_text
            continue
        if key == "notes":
            merged[key] = clean_text(" ".join(part for part in [primary.get("notes", ""), value] if clean_text(str(part))))
            continue
        if not merged.get(key) and value:
            merged[key] = value
    return merged


def unique_preserve(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = value.casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(value)
    return result


def normalize_candidate_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    filtered_query = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_")
        and key.lower() not in {"from", "ref", "source", "spm", "share", "tracking"}
    ]
    normalized_query = urllib.parse.urlencode(filtered_query, doseq=True)
    return urllib.parse.urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path,
            normalized_query,
            "",
        )
    )


def candidate_priority(url: str, text: str) -> int:
    candidate = f"{url} {text}".casefold()
    score = 0
    if any(hint in candidate for hint in ARTICLE_URL_HINTS):
        score += 120
    if re.search(r"\b10\.\d{4,9}/[-._;()/:a-z0-9]+\b", candidate):
        score += 90
    if "/document/" in candidate:
        score += 80
    if "/article/" in candidate:
        score += 40
    if len(clean_text(text)) >= 8:
        score += 15
    if clean_text(text).casefold() in UTILITY_LINK_TEXT:
        score -= 60
    if any(hint in candidate for hint in UTILITY_URL_HINTS):
        score -= 40
    return score


def extract_candidate_links(
    page_url: str,
    html_text: str,
    include_patterns: list[str],
    exclude_patterns: list[str],
) -> list[str]:
    document = collect_document(html_text)
    defaults = ["/article/", "/document/", "/science/article/", "/content/pdf/", "doi.org/10.", "paper"]
    include_terms = [item.lower() for item in (include_patterns or defaults)]
    exclude_terms = [item.lower() for item in exclude_patterns]
    ranked: dict[str, tuple[int, int, str]] = {}

    for index, anchor in enumerate(document.anchors):
        href = anchor.get("href", "")
        text = anchor.get("text", "")
        if not href:
            continue
        if "{{" in href or "}}" in href or "{%" in href:
            continue
        absolute = urllib.parse.urljoin(page_url, href)
        candidate_text = f"{absolute} {text}".lower()
        if include_terms and not any(term in candidate_text for term in include_terms):
            continue
        if exclude_terms and any(term in candidate_text for term in exclude_terms):
            continue
        parsed = urllib.parse.urlparse(absolute)
        if parsed.scheme not in {"http", "https", "file"}:
            continue
        normalized = normalize_candidate_url(absolute)
        score = candidate_priority(absolute, text)
        existing = ranked.get(normalized)
        if existing is None or score > existing[0]:
            ranked[normalized] = (score, index, normalized)
    ordered = sorted(ranked.values(), key=lambda item: (-item[0], item[1], item[2]))
    return [item[2] for item in ordered]


def choose_article_url(candidate_url: str, source: SourceSpec) -> str:
    doi = extract_doi(candidate_url)
    if not doi:
        return candidate_url
    for template in source.article_url_templates:
        rendered = render_url_template(template, doi)
        if rendered:
            return rendered
    return candidate_url


def needs_enrichment(record: dict[str, Any]) -> bool:
    return not (
        clean_text(str(record.get("abstract", "")))
        and clean_list(record.get("affiliations"))
        and clean_text(str(record.get("published_at", "")))
    )


def enrich_record_from_templates(
    record: dict[str, Any],
    runtime: RuntimePaths,
    source: SourceSpec,
) -> dict[str, Any]:
    doi = clean_text(str(record.get("doi", "")))
    if not doi or not source.enrichment_url_templates or not needs_enrichment(record):
        return record

    enriched = dict(record)
    for template in source.enrichment_url_templates:
        url = render_url_template(template, doi)
        if not url:
            continue
        try:
            html_text, final_url = fetch_text(url, runtime, source)
        except urllib.error.URLError as exc:
            note = clean_text(str(enriched.get("notes", "")))
            enriched["notes"] = clean_text(f"{note} enrichment-fetch-error: {exc}")
            continue
        secondary = extract_article_metadata(final_url, html_text, source)
        secondary["notes"] = clean_text(f"enriched-from: {urllib.parse.urlsplit(final_url).netloc}")
        enriched = merge_records(enriched, secondary)
        if not needs_enrichment(enriched):
            break
    return enriched


def discover_from_html_list(runtime: RuntimePaths, source: SourceSpec) -> list[dict[str, Any]]:
    page_text, page_url = fetch_text(resolve_url(runtime.config_path.parent, source.url), runtime, source)
    article_urls = extract_candidate_links(page_url, page_text, source.include_url_patterns, source.exclude_url_patterns)
    records: list[dict[str, Any]] = []
    for article_url in article_urls[: source.max_items]:
        fetch_url = choose_article_url(article_url, source)
        try:
            article_text, final_url = fetch_text(fetch_url, runtime, source)
        except urllib.error.URLError as exc:
            records.append(
                {
                    "source_id": source.id,
                    "source_label": source.label,
                    "publisher": source.publisher,
                    "collection": source.collection,
                    "title": "",
                    "abstract": "",
                    "authors": [],
                    "affiliations": [],
                    "doi": "",
                    "arxiv_id": "",
                    "categories": [],
                    "keywords": [],
                    "landing_url": fetch_url,
                    "pdf_url": "",
                    "published_at": "",
                    "notes": f"article-fetch-error: {exc}",
                }
            )
            continue
        record = extract_article_metadata(final_url, article_text, source)
        if not record.get("doi"):
            record["doi"] = extract_doi(article_url)
        record = enrich_record_from_templates(record, runtime, source)
        if record.get("title"):
            records.append(record)
    return records


def discover_from_tpldata(runtime: RuntimePaths, source: SourceSpec) -> list[dict[str, Any]]:
    endpoint = clean_text(source.api_endpoint) or resolve_url(runtime.config_path.parent, source.url)
    payload = dict(source.api_payload)
    payload.setdefault("max", source.max_items)
    payload.setdefault("currentpage", 1)

    records: list[dict[str, Any]] = []
    current_page = int(payload.get("currentpage") or 1)
    total_page = current_page

    while len(records) < source.max_items and current_page <= total_page:
        payload["currentpage"] = current_page
        response, _ = fetch_json_post(endpoint, payload, runtime, source)
        page_records, discovered_total_page = extract_tpldata_records(response)
        if discovered_total_page:
            total_page = discovered_total_page
        if not page_records:
            break
        records.extend(extract_tpldata_record(item, source) for item in page_records)
        current_page += 1

    return records[: source.max_items]


def parse_rss_feed(feed_text: str, source: SourceSpec) -> list[dict[str, Any]]:
    root = ET.fromstring(feed_text)
    records: list[dict[str, Any]] = []
    if local_name(root.tag) in {"feed"}:
        entries = [node for node in root if local_name(node.tag) == "entry"]
        for entry in entries:
            title = ""
            summary = ""
            link = ""
            published_at = ""
            authors: list[str] = []
            categories: list[str] = []
            pdf_url = ""
            identifier = ""
            for child in entry:
                tag = local_name(child.tag)
                if tag == "title":
                    title = clean_text("".join(child.itertext()))
                elif tag in {"summary", "content"} and not summary:
                    summary = clean_text("".join(child.itertext()))
                elif tag in {"published", "updated"} and not published_at:
                    published_at = parse_datetime("".join(child.itertext()))
                elif tag == "id" and not identifier:
                    identifier = clean_text("".join(child.itertext()))
                elif tag == "author":
                    author_name = ""
                    for author_child in child:
                        if local_name(author_child.tag) == "name":
                            author_name = clean_text("".join(author_child.itertext()))
                            break
                    if author_name:
                        authors.append(author_name)
                elif tag == "category":
                    term = clean_text(child.attrib.get("term", "") or "".join(child.itertext()))
                    if term:
                        categories.append(term)
                elif tag == "link":
                    href = child.attrib.get("href", "").strip()
                    rel = child.attrib.get("rel", "alternate").strip().lower()
                    title_attr = child.attrib.get("title", "").strip().lower()
                    if rel == "alternate" and href and not link:
                        link = href
                    if (title_attr == "pdf" or rel == "related") and href.endswith(".pdf"):
                        pdf_url = href
            records.append(
                {
                    "source_id": source.id,
                    "source_label": source.label,
                    "publisher": source.publisher,
                    "collection": source.collection,
                    "title": title,
                    "abstract": summary,
                    "authors": unique_preserve(authors),
                    "affiliations": [],
                    "doi": "",
                    "arxiv_id": extract_arxiv_id(identifier) if "arxiv.org" in identifier else "",
                    "categories": unique_preserve(categories),
                    "keywords": [],
                    "landing_url": link or identifier,
                    "pdf_url": pdf_url,
                    "published_at": published_at,
                    "notes": "",
                }
            )
        return records

    items = [node for node in root.iter() if local_name(node.tag) == "item"]
    for item in items:
        title = child_text(item, "title")
        link = child_text(item, "link")
        description = child_text(item, "description")
        published_at = parse_datetime(child_text(item, "pubdate", "date"))
        categories = [clean_text("".join(node.itertext())) for node in item if local_name(node.tag) == "category"]
        authors = []
        for node in item:
            tag = local_name(node.tag)
            if tag in {"creator", "author"}:
                authors.append(clean_text("".join(node.itertext())))
        pdf_url = ""
        for node in item:
            if local_name(node.tag) != "enclosure":
                continue
            href = node.attrib.get("url", "").strip()
            content_type = node.attrib.get("type", "").lower()
            if href and ("pdf" in href.lower() or "pdf" in content_type):
                pdf_url = href
                break
        records.append(
            {
                "source_id": source.id,
                "source_label": source.label,
                "publisher": source.publisher,
                "collection": source.collection,
                "title": title,
                "abstract": description,
                "authors": unique_preserve([author for author in authors if author]),
                "affiliations": [],
                "doi": "",
                "arxiv_id": "",
                "categories": unique_preserve([category for category in categories if category]),
                "keywords": [],
                "landing_url": link,
                "pdf_url": pdf_url,
                "published_at": published_at,
                "notes": "",
            }
        )
    return records


def child_text(node: ET.Element, *names: str) -> str:
    lower_names = {name.lower() for name in names}
    for child in node:
        if local_name(child.tag) in lower_names:
            return clean_text("".join(child.itertext()))
    return ""


def discover_from_rss(runtime: RuntimePaths, source: SourceSpec) -> list[dict[str, Any]]:
    feed_text, _ = fetch_text(resolve_url(runtime.config_path.parent, source.url), runtime, source)
    records = parse_rss_feed(feed_text, source)
    if not source.follow_article_pages:
        return records[: source.max_items]

    enriched: list[dict[str, Any]] = []
    for record in records[: source.max_items]:
        landing_url = record.get("landing_url", "")
        if not landing_url:
            enriched.append(record)
            continue
        try:
            article_text, final_url = fetch_text(landing_url, runtime, source)
        except urllib.error.URLError:
            enriched.append(record)
            continue
        page_record = extract_article_metadata(final_url, article_text, source)
        enriched.append(merge_records(record, page_record))
    return enriched


def build_arxiv_query(categories: list[str]) -> str:
    if categories:
        return " OR ".join(f"cat:{category}" for category in categories)
    return "cat:cs.RO OR cat:cs.AI OR cat:cs.HC OR cat:cs.MA"


def extract_arxiv_id(identifier: str) -> str:
    parsed = urllib.parse.urlparse(identifier)
    path = parsed.path.rstrip("/")
    if not path:
        return ""
    return path.rsplit("/", 1)[-1]


def discover_from_arxiv(runtime: RuntimePaths, source: SourceSpec) -> list[dict[str, Any]]:
    base_url = resolve_url(runtime.config_path.parent, source.url)
    params = {
        "search_query": build_arxiv_query(source.categories),
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "start": "0",
        "max_results": str(source.max_items),
    }
    request_url = f"{base_url}?{urllib.parse.urlencode(params)}"
    feed_text, _ = fetch_text(request_url, runtime, source)
    return parse_rss_feed(feed_text, source)


def first_text(value: Any) -> str:
    if isinstance(value, list):
        for entry in value:
            text = clean_text(str(entry))
            if text:
                return text
        return ""
    return clean_text(str(value))


def crossref_date_parts_to_iso(date_parts: Any) -> str:
    if not isinstance(date_parts, list) or not date_parts:
        return ""
    parts = date_parts[0] if isinstance(date_parts[0], list) else date_parts
    if not isinstance(parts, list) or not parts:
        return ""
    try:
        year = int(parts[0])
        month = int(parts[1]) if len(parts) >= 2 else 1
        day = int(parts[2]) if len(parts) >= 3 else 1
        value = datetime(year, month, day, tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return ""
    return isoformat_utc(value)


def crossref_published_at(item: dict[str, Any]) -> str:
    for key in ("published-online", "published-print", "published", "issued", "created"):
        raw_value = item.get(key)
        if not isinstance(raw_value, dict):
            continue
        published_at = crossref_date_parts_to_iso(raw_value.get("date-parts"))
        if published_at:
            return published_at
    return ""


def extract_crossref_record(item: dict[str, Any], source: SourceSpec) -> dict[str, Any]:
    authors: list[str] = []
    affiliations: list[str] = []
    for author in item.get("author", []):
        if not isinstance(author, dict):
            continue
        given = clean_text(str(author.get("given", "")))
        family = clean_text(str(author.get("family", "")))
        full_name = clean_text(f"{given} {family}") or clean_text(str(author.get("name", "")))
        if full_name:
            authors.append(full_name)
        for affiliation in author.get("affiliation", []):
            if not isinstance(affiliation, dict):
                continue
            name = clean_text(str(affiliation.get("name", "")))
            if name:
                affiliations.append(name)

    landing_url = clean_text(
        str((((item.get("resource") or {}).get("primary") or {}).get("URL", "")))
    ) or clean_text(str(item.get("URL", "")))
    pdf_url = ""
    for link in item.get("link", []):
        if not isinstance(link, dict):
            continue
        link_url = clean_text(str(link.get("URL", "")))
        content_type = clean_text(str(link.get("content-type", ""))).lower()
        if link_url and ("pdf" in link_url.lower() or "pdf" in content_type):
            pdf_url = link_url
            break

    return {
        "source_id": source.id,
        "source_label": source.label,
        "publisher": source.publisher,
        "collection": first_text(item.get("container-title")) or source.collection,
        "title": first_text(item.get("title")),
        "abstract": clean_text(str(item.get("abstract", ""))),
        "authors": unique_preserve(authors),
        "affiliations": unique_preserve(affiliations),
        "doi": clean_text(str(item.get("DOI", ""))),
        "arxiv_id": "",
        "categories": unique_preserve(clean_list(item.get("subject"))),
        "keywords": [],
        "landing_url": landing_url,
        "pdf_url": pdf_url,
        "published_at": crossref_published_at(item),
        "notes": "discovered-via-crossref",
    }


def discover_from_crossref_journal(runtime: RuntimePaths, source: SourceSpec) -> list[dict[str, Any]]:
    if not source.journal_issn:
        raise ValueError(f"crossref-journal source missing journal_issn: {source.id}")
    request_url = (
        f"https://api.crossref.org/journals/{urllib.parse.quote(source.journal_issn, safe='')}/works"
        f"?sort=published&order=desc&rows={source.max_items}"
    )
    payload_text, _ = fetch_text(request_url, runtime, source)
    payload = json.loads(payload_text)
    items = ((payload.get("message") or {}).get("items") or [])
    records = [
        extract_crossref_record(item, source)
        for item in items
        if isinstance(item, dict)
    ]
    if not source.follow_article_pages:
        return records

    enriched: list[dict[str, Any]] = []
    for record in records:
        landing_url = clean_text(str(record.get("landing_url", "")))
        if not landing_url:
            enriched.append(record)
            continue
        try:
            article_text, final_url = fetch_text(landing_url, runtime, source)
        except urllib.error.URLError as exc:
            failed = dict(record)
            note = clean_text(str(failed.get("notes", "")))
            failed["notes"] = clean_text(f"{note} article-fetch-error: {exc}")
            enriched.append(failed)
            continue
        page_record = extract_article_metadata(final_url, article_text, source)
        enriched.append(merge_records(record, page_record))
    return enriched


def compute_dedupe_key(record: dict[str, Any]) -> str:
    doi = clean_text(str(record.get("doi", ""))).lower()
    if doi:
        return f"doi:{doi}"
    arxiv_id = clean_text(str(record.get("arxiv_id", ""))).lower()
    if arxiv_id:
        return f"arxiv:{arxiv_id}"
    landing_url = clean_text(str(record.get("landing_url", "")))
    if landing_url:
        return f"url:{landing_url}"
    title = clean_text(str(record.get("title", ""))).lower()
    title_hash = hashlib.sha1(title.encode("utf-8")).hexdigest()[:20]
    return f"title:{title_hash}"


def compute_paper_id(dedupe_key: str) -> str:
    return hashlib.sha1(dedupe_key.encode("utf-8")).hexdigest()[:16]


def record_slug(record: dict[str, Any]) -> str:
    title = clean_text(str(record.get("title", "")))
    if title:
        return slugify(title, fallback=compute_paper_id(compute_dedupe_key(record)))
    return compute_paper_id(compute_dedupe_key(record))


def matches_keywords(record: dict[str, Any], keyword_terms: list[str], category_terms: list[str]) -> list[str]:
    haystack_parts = [
        clean_text(str(record.get("title", ""))),
        clean_text(str(record.get("abstract", ""))),
        clean_text(str(record.get("collection", ""))),
        clean_text(str(record.get("source_label", ""))),
    ]
    haystack_parts.extend(clean_list(record.get("categories")))
    haystack = " ".join(haystack_parts).casefold()
    matched: list[str] = []
    for term in keyword_terms:
        variants = unique_preserve([term] + KEYWORD_ALIASES.get(term.casefold(), []))
        if any(keyword_variant_matches(haystack, variant) for variant in variants):
            matched.append(term)
    categories_lower = {item.casefold() for item in clean_list(record.get("categories"))}
    for category in category_terms:
        if category.casefold() in categories_lower or category.casefold() in haystack:
            matched.append(category)
    return unique_preserve(matched)


def keyword_variant_matches(haystack: str, variant: str) -> bool:
    needle = clean_text(variant).casefold()
    if not needle:
        return False
    if not is_ascii_search_term(needle):
        return needle in haystack
    pattern = rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])"
    return re.search(pattern, haystack) is not None


def is_ascii_search_term(value: str) -> bool:
    return bool(value) and all(char.isascii() for char in value)


def should_translate_summary(record: dict[str, Any], runtime: RuntimePaths) -> bool:
    if clean_text(str(record.get("zh_summary", ""))):
        return False
    abstract = clean_text(str(record.get("abstract", "")))
    if not abstract:
        return False
    if contains_cjk_text(abstract) and not is_probably_english_text(abstract):
        return True
    if not runtime.translation.enabled:
        return False
    if runtime.translation.only_translate_english and not is_probably_english_text(abstract):
        return False
    return True


def should_translate_title(record: dict[str, Any], runtime: RuntimePaths) -> bool:
    if clean_text(str(record.get("zh_title", ""))):
        return False
    title = clean_text(str(record.get("title", "")))
    if not title:
        return False
    if contains_cjk_text(title) and not is_probably_english_title(title):
        return True
    if not runtime.translation.enabled:
        return False
    if runtime.translation.only_translate_english and not is_probably_english_title(title):
        return False
    return True


def maybe_translate_title(record: dict[str, Any], runtime: RuntimePaths) -> dict[str, Any]:
    if clean_text(str(record.get("zh_title", ""))):
        return record
    title = clean_text(str(record.get("title", "")))
    if not title:
        return record
    if contains_cjk_text(title) and not is_probably_english_title(title):
        translated = dict(record)
        translated["notes"] = strip_translation_error_notes(
            str(translated.get("notes", "")),
            remove_title_errors=True,
        )
        translated["zh_title"] = title
        return translated
    if not should_translate_title(record, runtime):
        return record
    translated = dict(record)
    try:
        translated["notes"] = strip_translation_error_notes(
            str(translated.get("notes", "")),
            remove_title_errors=True,
        )
        translated["zh_title"] = translate_title_with_llm(translated, runtime)
    except Exception as exc:
        note = strip_translation_error_notes(
            str(translated.get("notes", "")),
            remove_title_errors=True,
        )
        translated["notes"] = clean_text(f"{note} llm-title-translation-error: {exc}")
    return translated


def maybe_translate_summary(record: dict[str, Any], runtime: RuntimePaths) -> dict[str, Any]:
    if clean_text(str(record.get("zh_summary", ""))):
        return record
    abstract = clean_text(str(record.get("abstract", "")))
    if not abstract:
        return record
    if contains_cjk_text(abstract) and not is_probably_english_text(abstract):
        translated = dict(record)
        translated["notes"] = strip_translation_error_notes(
            str(translated.get("notes", "")),
            remove_summary_errors=True,
        )
        translated["zh_summary"] = abstract
        return translated
    if not should_translate_summary(record, runtime):
        return record
    translated = dict(record)
    try:
        translated["notes"] = strip_translation_error_notes(
            str(translated.get("notes", "")),
            remove_summary_errors=True,
        )
        translated["zh_summary"] = translate_summary_with_llm(translated, runtime)
    except Exception as exc:
        note = strip_translation_error_notes(
            str(translated.get("notes", "")),
            remove_summary_errors=True,
        )
        translated["notes"] = clean_text(f"{note} llm-translation-error: {exc}")
    return translated


def maybe_translate_record(record: dict[str, Any], runtime: RuntimePaths) -> dict[str, Any]:
    translated = maybe_translate_title(record, runtime)
    translated = maybe_translate_summary(translated, runtime)
    return translated


def within_since_window(record: dict[str, Any], since_days: int, reference_time: datetime) -> bool:
    if since_days <= 0:
        return True
    published = parse_date_safe(str(record.get("published_at", "")))
    if not published:
        return True
    cutoff = reference_time - timedelta(days=since_days)
    return published >= cutoff


def determine_status(record: dict[str, Any], workflow_mode: str = "full") -> str:
    if workflow_mode == "abstract-only":
        if clean_text(str(record.get("title", ""))) and clean_text(str(record.get("abstract", ""))):
            return "ready"
        if clean_text(str(record.get("title", ""))):
            return "needs_review"
        return "needs_review"
    has_summary = bool(clean_text(str(record.get("zh_summary", ""))))
    has_affiliations = bool(clean_list(record.get("affiliations")))
    if has_summary and has_affiliations:
        return "ready"
    if clean_text(str(record.get("title", ""))) and clean_text(str(record.get("abstract", ""))):
        return "pending_enrichment"
    return "needs_review"


def maybe_download_pdf(record: dict[str, Any], runtime: RuntimePaths, source: SourceSpec) -> dict[str, Any]:
    pdf_url = clean_text(str(record.get("pdf_url", "")))
    if not pdf_url:
        return record
    if source.pdf_policy == "restricted" and not has_auth_material(source, runtime):
        note = clean_text(str(record.get("notes", "")))
        record["notes"] = clean_text(
            f"{note} pdf-download-skipped: restricted source without cookies or auth headers"
        )
        return record
    filename = f"{record.get('paper_id', 'paper')}-{record.get('slug', 'paper')[:80]}.pdf"
    target_path = runtime.pdf_dir / filename
    try:
        download_file(pdf_url, target_path, runtime, source)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
        note = clean_text(str(record.get("notes", "")))
        record["notes"] = clean_text(f"{note} pdf-download-error: {exc}")
        return record
    record["pdf_path"] = str(target_path)
    return record


def prepare_record(
    raw_record: dict[str, Any],
    source: SourceSpec,
    keyword_terms: list[str],
    category_terms: list[str],
    runtime: RuntimePaths,
    reference_time: datetime,
) -> dict[str, Any] | None:
    title = clean_text(str(raw_record.get("title", "")))
    landing_url = clean_text(str(raw_record.get("landing_url", "")))
    if not title and not landing_url:
        return None

    record: dict[str, Any] = {
        "source_id": source.id,
        "source_label": source.label,
        "publisher": clean_text(str(raw_record.get("publisher", source.publisher))),
        "collection": clean_text(str(raw_record.get("collection", source.collection))),
        "title": title,
        "zh_title": clean_text(str(raw_record.get("zh_title", ""))),
        "abstract": clean_text(str(raw_record.get("abstract", ""))),
        "zh_summary": clean_text(str(raw_record.get("zh_summary", ""))),
        "authors": unique_preserve(clean_list(raw_record.get("authors"))),
        "affiliations": unique_preserve(clean_list(raw_record.get("affiliations"))),
        "doi": clean_text(str(raw_record.get("doi", ""))),
        "arxiv_id": clean_text(str(raw_record.get("arxiv_id", ""))),
        "categories": unique_preserve(clean_list(raw_record.get("categories"))),
        "keywords": [],
        "landing_url": landing_url,
        "pdf_url": clean_text(str(raw_record.get("pdf_url", ""))),
        "pdf_path": clean_text(str(raw_record.get("pdf_path", ""))),
        "published_at": clean_text(str(raw_record.get("published_at", ""))),
        "notes": clean_text(str(raw_record.get("notes", ""))),
    }
    if record["published_at"]:
        record["published_at"] = parse_datetime(record["published_at"])

    matched_keywords = matches_keywords(record, keyword_terms, category_terms)
    if keyword_terms or category_terms:
        if not matched_keywords:
            return None
    record["keywords"] = matched_keywords
    if not within_since_window(record, runtime.since_days, reference_time):
        return None

    dedupe_key = compute_dedupe_key(record)
    record["dedupe_key"] = dedupe_key
    record["paper_id"] = compute_paper_id(dedupe_key)
    record["slug"] = record_slug(record)
    timestamp = isoformat_utc(reference_time)
    record["discovered_at"] = timestamp
    record["updated_at"] = timestamp
    record["status"] = determine_status(record, runtime.workflow_mode)
    return record


def maybe_mark_existing_record_filtered(
    connection: sqlite3.Connection,
    raw_record: dict[str, Any],
    source: SourceSpec,
    keyword_terms: list[str],
    category_terms: list[str],
    reference_time: datetime,
) -> bool:
    title = clean_text(str(raw_record.get("title", "")))
    landing_url = clean_text(str(raw_record.get("landing_url", "")))
    doi = clean_text(str(raw_record.get("doi", "")))
    arxiv_id = clean_text(str(raw_record.get("arxiv_id", "")))
    if not title and not landing_url and not doi and not arxiv_id:
        return False

    probe_record: dict[str, Any] = {
        "source_id": source.id,
        "source_label": clean_text(str(raw_record.get("source_label", source.label))),
        "collection": clean_text(str(raw_record.get("collection", source.collection))),
        "title": title,
        "abstract": clean_text(str(raw_record.get("abstract", ""))),
        "categories": unique_preserve(clean_list(raw_record.get("categories"))),
        "doi": doi,
        "arxiv_id": arxiv_id,
        "landing_url": landing_url,
    }
    if matches_keywords(probe_record, keyword_terms, category_terms):
        return False

    dedupe_key = compute_dedupe_key(probe_record)
    existing = get_existing_row(connection, dedupe_key)
    if existing is None or existing["source_id"] != source.id:
        return False

    note = clean_text(str(existing["notes"] or ""))
    filtered_note = clean_text(f"{note} filtered-out-on-refresh")
    connection.execute(
        "UPDATE papers SET keywords_json = ?, status = ?, updated_at = ?, notes = ? WHERE paper_id = ?",
        (
            json_text([]),
            "filtered",
            isoformat_utc(reference_time),
            filtered_note,
            existing["paper_id"],
        ),
    )
    return True


def get_existing_row(connection: sqlite3.Connection, dedupe_key: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM papers WHERE dedupe_key = ?",
        (dedupe_key,),
    ).fetchone()


def upsert_record(connection: sqlite3.Connection, record: dict[str, Any], workflow_mode: str = "full") -> bool:
    existing = get_existing_row(connection, record["dedupe_key"])
    is_new = existing is None
    if existing is not None:
        if not record.get("title"):
            record["title"] = existing["title"] or ""
        if not record.get("zh_title"):
            record["zh_title"] = existing["zh_title"] or ""
        existing_abstract = clean_text(existing["abstract"] or "")
        incoming_abstract = clean_text(str(record.get("abstract", "")))
        if len(existing_abstract) > len(incoming_abstract):
            record["abstract"] = existing_abstract
        if not record.get("zh_summary"):
            record["zh_summary"] = existing["zh_summary"] or ""
        if not clean_list(record.get("authors")):
            record["authors"] = decode_json_list(existing["authors_json"])
        if not clean_list(record.get("affiliations")):
            record["affiliations"] = decode_json_list(existing["affiliations_json"])
        if not record.get("doi"):
            record["doi"] = existing["doi"] or ""
        if not clean_list(record.get("categories")):
            record["categories"] = decode_json_list(existing["categories_json"])
        if not clean_list(record.get("keywords")):
            record["keywords"] = decode_json_list(existing["keywords_json"])
        if not record.get("landing_url"):
            record["landing_url"] = existing["landing_url"] or ""
        if not record.get("pdf_url"):
            record["pdf_url"] = existing["pdf_url"] or ""
        if not record.get("pdf_path"):
            record["pdf_path"] = existing["pdf_path"] or ""
        if not record.get("published_at"):
            record["published_at"] = existing["published_at"] or ""
        if existing["notes"] and record.get("notes"):
            record["notes"] = clean_text(f"{existing['notes']} {record['notes']}")
        elif existing["notes"] and not record.get("notes"):
            record["notes"] = existing["notes"]
        record["discovered_at"] = existing["discovered_at"]
        record["status"] = determine_status(record, workflow_mode)

    params = {
        "paper_id": record["paper_id"],
        "dedupe_key": record["dedupe_key"],
        "slug": record["slug"],
        "source_id": record["source_id"],
        "source_label": record["source_label"],
        "publisher": record.get("publisher", ""),
        "collection": record.get("collection", ""),
        "title": record.get("title", ""),
        "zh_title": record.get("zh_title", ""),
        "abstract": record.get("abstract", ""),
        "zh_summary": record.get("zh_summary", ""),
        "authors_json": json_text(clean_list(record.get("authors"))),
        "affiliations_json": json_text(clean_list(record.get("affiliations"))),
        "doi": record.get("doi", ""),
        "arxiv_id": record.get("arxiv_id", ""),
        "categories_json": json_text(clean_list(record.get("categories"))),
        "keywords_json": json_text(clean_list(record.get("keywords"))),
        "landing_url": record.get("landing_url", ""),
        "pdf_url": record.get("pdf_url", ""),
        "pdf_path": record.get("pdf_path", ""),
        "published_at": record.get("published_at", ""),
        "discovered_at": record.get("discovered_at", ""),
        "updated_at": record.get("updated_at", ""),
        "status": record.get("status", "pending_enrichment"),
        "notes": record.get("notes", ""),
    }
    if existing is None:
        connection.execute(
            """
            INSERT INTO papers (
                paper_id, dedupe_key, slug, source_id, source_label, publisher, collection,
                title, zh_title, abstract, zh_summary, authors_json, affiliations_json, doi, arxiv_id,
                categories_json, keywords_json, landing_url, pdf_url, pdf_path, published_at,
                discovered_at, updated_at, status, notes
            ) VALUES (
                :paper_id, :dedupe_key, :slug, :source_id, :source_label, :publisher, :collection,
                :title, :zh_title, :abstract, :zh_summary, :authors_json, :affiliations_json, :doi, :arxiv_id,
                :categories_json, :keywords_json, :landing_url, :pdf_url, :pdf_path, :published_at,
                :discovered_at, :updated_at, :status, :notes
            )
            """,
            params,
        )
    else:
        connection.execute(
            """
            UPDATE papers
            SET
                slug = :slug,
                source_id = :source_id,
                source_label = :source_label,
                publisher = :publisher,
                collection = :collection,
                title = :title,
                zh_title = :zh_title,
                abstract = :abstract,
                zh_summary = :zh_summary,
                authors_json = :authors_json,
                affiliations_json = :affiliations_json,
                doi = :doi,
                arxiv_id = :arxiv_id,
                categories_json = :categories_json,
                keywords_json = :keywords_json,
                landing_url = :landing_url,
                pdf_url = :pdf_url,
                pdf_path = :pdf_path,
                published_at = :published_at,
                updated_at = :updated_at,
                status = :status,
                notes = :notes
            WHERE dedupe_key = :dedupe_key
            """,
            params,
        )
    return is_new


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def discover_records(
    runtime: RuntimePaths,
    keyword_terms: list[str],
    category_terms: list[str],
    sources: list[SourceSpec],
    skip_downloads: bool,
) -> dict[str, Any]:
    if runtime.translation.enabled:
        runtime.translation.validate()
    reference_time = now_utc()
    run_id = reference_time.strftime("%Y%m%dT%H%M%SZ")
    connection = connect_database(runtime)
    new_records: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    source_stats: dict[str, dict[str, Any]] = {}

    try:
        for source in sources:
            if not source.enabled:
                continue
            stats = {"fetched": 0, "accepted": 0, "new": 0, "filtered": 0, "errors": []}
            source_stats[source.id] = stats
            try:
                if source.kind == "rss":
                    raw_records = discover_from_rss(runtime, source)
                elif source.kind == "html-list":
                    raw_records = discover_from_html_list(runtime, source)
                elif source.kind == "arxiv-api":
                    raw_records = discover_from_arxiv(runtime, source)
                elif source.kind == "crossref-journal":
                    raw_records = discover_from_crossref_journal(runtime, source)
                elif source.kind == "tpl-data":
                    raw_records = discover_from_tpldata(runtime, source)
                else:
                    raise ValueError(f"unsupported source kind: {source.kind}")
            except Exception as exc:
                stats["errors"].append(str(exc))
                continue

            stats["fetched"] = len(raw_records)
            for raw_record in raw_records:
                prepared = prepare_record(
                    raw_record,
                    source,
                    keyword_terms,
                    category_terms,
                    runtime,
                    reference_time,
                )
                if prepared is None:
                    if maybe_mark_existing_record_filtered(
                        connection,
                        raw_record,
                        source,
                        keyword_terms,
                        category_terms,
                        reference_time,
                    ):
                        stats["filtered"] += 1
                    continue
                if prepared["dedupe_key"] in seen_keys:
                    continue
                seen_keys.add(prepared["dedupe_key"])
                existing_row = get_existing_row(connection, prepared["dedupe_key"])
                if existing_row is not None:
                    if not prepared.get("zh_title"):
                        prepared["zh_title"] = existing_row["zh_title"] or ""
                    if not prepared.get("zh_summary"):
                        prepared["zh_summary"] = existing_row["zh_summary"] or ""
                prepared = maybe_translate_record(prepared, runtime)
                prepared["status"] = determine_status(prepared, runtime.workflow_mode)
                if runtime.download_pdfs and not skip_downloads:
                    prepared = maybe_download_pdf(prepared, runtime, source)
                    prepared["status"] = determine_status(prepared, runtime.workflow_mode)
                is_new = upsert_record(connection, prepared, runtime.workflow_mode)
                stats["accepted"] += 1
                if is_new:
                    stats["new"] += 1
                    if runtime.workflow_mode != "abstract-only" and prepared["status"] != "ready":
                        new_records.append(
                            {
                                "paper_id": prepared["paper_id"],
                                "dedupe_key": prepared["dedupe_key"],
                                "title": prepared["title"],
                                "zh_title": prepared.get("zh_title", ""),
                                "abstract": prepared["abstract"],
                                "authors": prepared["authors"],
                                "affiliations": prepared["affiliations"],
                                "source_label": prepared["source_label"],
                                "publisher": prepared["publisher"],
                                "collection": prepared["collection"],
                                "categories": prepared["categories"],
                                "keywords": prepared["keywords"],
                                "landing_url": prepared["landing_url"],
                                "pdf_url": prepared["pdf_url"],
                                "published_at": prepared["published_at"],
                                "zh_summary": prepared.get("zh_summary", ""),
                                "notes": prepared.get("notes", ""),
                            }
                        )
            connection.commit()
    finally:
        connection.close()

    queue_path = runtime.staging_dir / f"enrichment-{run_id}.jsonl"
    write_jsonl(queue_path, new_records)
    summary = {
        "run_id": run_id,
        "workflow_mode": runtime.workflow_mode,
        "queue_path": str(queue_path),
        "new_papers": sum(source["new"] for source in source_stats.values()),
        "queue_records": len(new_records),
        "new_records": len(new_records),
        "sources": source_stats,
    }
    summary_path = runtime.staging_dir / f"discover-summary-{run_id}.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    summary["summary_path"] = str(summary_path)
    return summary


def locate_record(connection: sqlite3.Connection, payload: dict[str, Any]) -> sqlite3.Row | None:
    selectors = [
        ("dedupe_key", payload.get("dedupe_key")),
        ("paper_id", payload.get("paper_id")),
        ("landing_url", payload.get("landing_url")),
        ("title", payload.get("title")),
    ]
    for column, value in selectors:
        normalized = clean_text(str(value or ""))
        if not normalized:
            continue
        query = f"SELECT * FROM papers WHERE {column} = ? LIMIT 1"
        row = connection.execute(query, (normalized,)).fetchone()
        if row is not None:
            return row
    return None


def finalize_records(runtime: RuntimePaths, input_path: Path) -> dict[str, Any]:
    connection = connect_database(runtime)
    updated = 0
    skipped = 0
    errors: list[str] = []
    try:
        with input_path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as exc:
                    errors.append(f"line {line_number}: {exc}")
                    continue
                row = locate_record(connection, payload)
                if row is None:
                    skipped += 1
                    continue
                zh_title = clean_text(str(payload.get("zh_title", ""))) or row["zh_title"] or ""
                zh_summary = clean_text(str(payload.get("zh_summary", ""))) or row["zh_summary"] or ""
                affiliations = clean_list(payload.get("affiliations"))
                if not affiliations:
                    affiliations = decode_json_list(row["affiliations_json"])
                notes = clean_text(
                    " ".join(
                        part
                        for part in [
                            row["notes"] or "",
                            clean_text(str(payload.get("notes", ""))),
                        ]
                        if clean_text(str(part))
                    )
                )
                updated_at = isoformat_utc(now_utc())
                status = determine_status(
                    {
                        "title": row["title"],
                        "zh_title": zh_title,
                        "abstract": row["abstract"],
                        "zh_summary": zh_summary,
                        "affiliations": affiliations,
                    },
                    runtime.workflow_mode,
                )
                connection.execute(
                    """
                    UPDATE papers
                    SET zh_title = ?, zh_summary = ?, affiliations_json = ?, notes = ?, updated_at = ?, status = ?
                    WHERE paper_id = ?
                    """,
                    (
                        zh_title,
                        zh_summary,
                        json_text(affiliations),
                        notes,
                        updated_at,
                        status,
                        row["paper_id"],
                    ),
                )
                updated += 1
        connection.commit()
    finally:
        connection.close()
    return {"updated": updated, "skipped": skipped, "errors": errors}


def row_to_runtime_record(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "paper_id": row["paper_id"],
        "dedupe_key": row["dedupe_key"],
        "source_id": row["source_id"],
        "source_label": row["source_label"],
        "publisher": row["publisher"] or "",
        "collection": row["collection"] or "",
        "title": row["title"] or "",
        "zh_title": row["zh_title"] or "",
        "abstract": row["abstract"] or "",
        "zh_summary": row["zh_summary"] or "",
        "authors": decode_json_list(row["authors_json"] or "[]"),
        "affiliations": decode_json_list(row["affiliations_json"] or "[]"),
        "doi": row["doi"] or "",
        "arxiv_id": row["arxiv_id"] or "",
        "categories": decode_json_list(row["categories_json"] or "[]"),
        "keywords": decode_json_list(row["keywords_json"] or "[]"),
        "landing_url": row["landing_url"] or "",
        "pdf_url": row["pdf_url"] or "",
        "pdf_path": row["pdf_path"] or "",
        "published_at": row["published_at"] or "",
        "discovered_at": row["discovered_at"] or "",
        "updated_at": row["updated_at"] or "",
        "status": row["status"] or "",
        "notes": row["notes"] or "",
    }


def translate_missing_summaries(
    runtime: RuntimePaths,
    limit: int = 50,
    source_id: str = "",
    titles_only: bool = False,
    summaries_only: bool = False,
) -> dict[str, Any]:
    runtime.translation.validate()
    if not runtime.translation.enabled:
        raise ValueError("llm_translation is not enabled in the current config")
    if titles_only and summaries_only:
        raise ValueError("titles_only and summaries_only cannot both be enabled")

    connection = connect_database(runtime)
    translated_title_count = 0
    translated_summary_count = 0
    skipped = 0
    try:
        if titles_only:
            query = """
            SELECT * FROM papers
            WHERE COALESCE(title, '') <> ''
              AND COALESCE(zh_title, '') = ''
            """
        elif summaries_only:
            query = """
            SELECT * FROM papers
            WHERE COALESCE(abstract, '') <> ''
              AND COALESCE(zh_summary, '') = ''
            """
        else:
            query = """
            SELECT * FROM papers
            WHERE COALESCE(title, '') <> ''
              AND (
                COALESCE(zh_title, '') = ''
                OR (
                  COALESCE(abstract, '') <> ''
                  AND COALESCE(zh_summary, '') = ''
                )
              )
            """
        params: list[Any] = []
        normalized_source_id = clean_text(source_id)
        if normalized_source_id:
            query += " AND source_id = ?"
            params.append(normalized_source_id)
        query += " ORDER BY COALESCE(published_at, discovered_at) DESC, updated_at DESC"
        if limit > 0:
            query += " LIMIT ?"
            params.append(int(limit))

        rows = connection.execute(query, params).fetchall()
        for row in rows:
            record = row_to_runtime_record(row)
            should_title = should_translate_title(record, runtime) if not summaries_only else False
            should_summary = should_translate_summary(record, runtime) if not titles_only else False
            if not should_title and not should_summary:
                skipped += 1
                continue
            translated = maybe_translate_record(record, runtime)
            zh_title = clean_text(str(translated.get("zh_title", "")))
            zh_summary = clean_text(str(translated.get("zh_summary", "")))
            notes = clean_text(str(translated.get("notes", "")))
            title_succeeded = should_title and bool(zh_title)
            summary_succeeded = should_summary and bool(zh_summary)
            if title_succeeded:
                translated_title_count += 1
            if summary_succeeded:
                translated_summary_count += 1
            if not title_succeeded and not summary_succeeded:
                skipped += 1
                if notes != (row["notes"] or ""):
                    connection.execute(
                        "UPDATE papers SET notes = ?, updated_at = ? WHERE paper_id = ?",
                        (notes, isoformat_utc(now_utc()), row["paper_id"]),
                    )
                continue
            status = determine_status(
                {
                    "title": row["title"],
                    "zh_title": zh_title,
                    "abstract": row["abstract"],
                    "zh_summary": zh_summary,
                    "affiliations": decode_json_list(row["affiliations_json"] or "[]"),
                },
                runtime.workflow_mode,
            )
            connection.execute(
                "UPDATE papers SET zh_title = ?, zh_summary = ?, notes = ?, updated_at = ?, status = ? WHERE paper_id = ?",
                (
                    zh_title,
                    zh_summary,
                    notes,
                    isoformat_utc(now_utc()),
                    status,
                    row["paper_id"],
                ),
            )
        connection.commit()
    finally:
        connection.close()
    return {
        "translated": translated_title_count + translated_summary_count,
        "translated_titles": translated_title_count,
        "translated_summaries": translated_summary_count,
        "skipped": skipped,
        "source_id": normalized_source_id,
        "limit": limit,
        "titles_only": titles_only,
        "summaries_only": summaries_only,
    }


def site_href(path_value: str, site_dir: Path) -> str:
    if not path_value:
        return ""
    path = Path(path_value)
    if path.exists():
        try:
            return path.relative_to(site_dir).as_posix()
        except ValueError:
            return path.as_uri()
    return path_value


def row_to_catalog_entry(row: sqlite3.Row, site_dir: Path) -> dict[str, Any]:
    return {
        "paper_id": row["paper_id"],
        "dedupe_key": row["dedupe_key"],
        "slug": row["slug"],
        "source_id": row["source_id"],
        "source_label": row["source_label"],
        "publisher": row["publisher"] or "",
        "collection": row["collection"] or "",
        "title": row["title"] or "",
        "zh_title": row["zh_title"] or "",
        "abstract": row["abstract"] or "",
        "zh_summary": row["zh_summary"] or "",
        "authors": decode_json_list(row["authors_json"] or "[]"),
        "affiliations": decode_json_list(row["affiliations_json"] or "[]"),
        "doi": row["doi"] or "",
        "arxiv_id": row["arxiv_id"] or "",
        "categories": decode_json_list(row["categories_json"] or "[]"),
        "keywords": decode_json_list(row["keywords_json"] or "[]"),
        "landing_url": row["landing_url"] or "",
        "pdf_url": row["pdf_url"] or "",
        "pdf_path": row["pdf_path"] or "",
        "pdf_href": site_href(row["pdf_path"] or "", site_dir),
        "published_at": row["published_at"] or "",
        "discovered_at": row["discovered_at"] or "",
        "updated_at": row["updated_at"] or "",
        "status": row["status"] or "",
        "notes": row["notes"] or "",
    }


def copy_site_assets(runtime: RuntimePaths) -> None:
    runtime.site_dir.mkdir(parents=True, exist_ok=True)
    if not runtime.assets_dir.exists():
        raise FileNotFoundError(f"site assets not found: {runtime.assets_dir}")
    for asset in runtime.assets_dir.iterdir():
        target = runtime.site_dir / asset.name
        if asset.is_dir():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(asset, target)
        else:
            shutil.copy2(asset, target)


def build_site(runtime: RuntimePaths) -> dict[str, Any]:
    connection = connect_database(runtime)
    try:
        rows = connection.execute(
            """
            SELECT *
            FROM papers
            WHERE COALESCE(status, '') != 'filtered'
            ORDER BY COALESCE(published_at, discovered_at) DESC, updated_at DESC
            """
        ).fetchall()
    finally:
        connection.close()

    papers = [row_to_catalog_entry(row, runtime.site_dir) for row in rows]
    stats = {
        "paper_count": len(papers),
        "ready_count": sum(1 for paper in papers if paper["status"] == "ready"),
        "source_count": len({paper["source_id"] for paper in papers}),
        "updated_at": isoformat_utc(now_utc()),
    }

    copy_site_assets(runtime)

    catalog_json_path = runtime.site_dir / "catalog.json"
    catalog_js_path = runtime.site_dir / "catalog.js"
    csv_path = runtime.site_dir / "papers.csv"
    catalog_json_path.write_text(json.dumps(papers, ensure_ascii=False, indent=2), encoding="utf-8")
    catalog_js_path.write_text(
        "window.__HERMES_PAPERS__ = "
        + json.dumps(papers, ensure_ascii=False)
        + ";\nwindow.__HERMES_STATS__ = "
        + json.dumps(stats, ensure_ascii=False)
        + ";\n",
        encoding="utf-8",
    )

    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "paper_id",
                "title",
                "zh_title",
                "source_label",
                "publisher",
                "collection",
                "published_at",
                "status",
                "doi",
                "arxiv_id",
                "landing_url",
                "pdf_url",
                "pdf_path",
                "authors",
                "affiliations",
                "keywords",
                "categories",
                "zh_summary",
                "abstract",
                "notes",
            ],
        )
        writer.writeheader()
        for paper in papers:
            writer.writerow(
                {
                    "paper_id": paper["paper_id"],
                    "title": paper["title"],
                    "zh_title": paper["zh_title"],
                    "source_label": paper["source_label"],
                    "publisher": paper["publisher"],
                    "collection": paper["collection"],
                    "published_at": paper["published_at"],
                    "status": paper["status"],
                    "doi": paper["doi"],
                    "arxiv_id": paper["arxiv_id"],
                    "landing_url": paper["landing_url"],
                    "pdf_url": paper["pdf_url"],
                    "pdf_path": paper["pdf_path"],
                    "authors": "; ".join(paper["authors"]),
                    "affiliations": "; ".join(paper["affiliations"]),
                    "keywords": "; ".join(paper["keywords"]),
                    "categories": "; ".join(paper["categories"]),
                    "zh_summary": paper["zh_summary"],
                    "abstract": paper["abstract"],
                    "notes": paper["notes"],
                }
            )
    return {
        "site_dir": str(runtime.site_dir),
        "catalog_json": str(catalog_json_path),
        "catalog_js": str(catalog_js_path),
        "papers_csv": str(csv_path),
        "paper_count": len(papers),
    }


def run_bootstrap(runtime: RuntimePaths) -> dict[str, Any]:
    connection = connect_database(runtime)
    connection.close()
    return {
        "database_path": str(runtime.database_path),
        "pdf_dir": str(runtime.pdf_dir),
        "site_dir": str(runtime.site_dir),
        "staging_dir": str(runtime.staging_dir),
    }


def run_pipeline(runtime: RuntimePaths, keyword_terms: list[str], category_terms: list[str], sources: list[SourceSpec]) -> dict[str, Any]:
    bootstrap_result = run_bootstrap(runtime)
    discover_result = discover_records(runtime, keyword_terms, category_terms, sources, skip_downloads=False)
    site_result = build_site(runtime)
    return {
        "bootstrap": bootstrap_result,
        "discover": discover_result,
        "build_site": site_result,
    }


def pipeline_refresh_once(
    runtime: RuntimePaths,
    keyword_terms: list[str],
    category_terms: list[str],
    sources: list[SourceSpec],
) -> dict[str, Any]:
    discover_result = discover_records(runtime, keyword_terms, category_terms, sources, skip_downloads=False)
    site_result = build_site(runtime)
    return {
        "discover": discover_result,
        "build_site": site_result,
    }


def service_url(host: str, port: int) -> str:
    display_host = host
    if ":" in host and not host.startswith("["):
        display_host = f"[{host}]"
    return f"http://{display_host}:{port}/"


def serve_site(
    runtime: RuntimePaths,
    keyword_terms: list[str],
    category_terms: list[str],
    sources: list[SourceSpec],
    host: str,
    port: int,
    refresh_interval_minutes: int,
    daily_refresh_times: list[str],
    initial_refresh: bool,
) -> None:
    run_bootstrap(runtime)
    refresh_lock = threading.Lock()
    stop_event = threading.Event()

    def refresh_once(trigger: str) -> None:
        started_at = isoformat_utc(now_utc())
        try:
            with refresh_lock:
                result = pipeline_refresh_once(runtime, keyword_terms, category_terms, sources)
            print(
                json.dumps(
                    {
                        "event": "refresh",
                        "trigger": trigger,
                        "started_at": started_at,
                        "finished_at": isoformat_utc(now_utc()),
                        "new_papers": result["discover"]["new_papers"],
                        "paper_count": result["build_site"]["paper_count"],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "event": "refresh_error",
                        "trigger": trigger,
                        "started_at": started_at,
                        "finished_at": isoformat_utc(now_utc()),
                        "error": str(exc),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    if initial_refresh:
        refresh_once("initial")
    elif not (runtime.site_dir / "catalog.js").exists():
        build_site(runtime)

    def refresh_loop() -> None:
        if daily_refresh_times:
            while not stop_event.is_set():
                next_run_at = next_daily_refresh_time(daily_refresh_times)
                wait_seconds = max((next_run_at - datetime.now().astimezone()).total_seconds(), 1)
                if stop_event.wait(wait_seconds):
                    break
                refresh_once(f"scheduled:{next_run_at.strftime('%H:%M')}")
            return
        if refresh_interval_minutes <= 0:
            return
        wait_seconds = max(refresh_interval_minutes, 1) * 60
        while not stop_event.wait(wait_seconds):
            refresh_once("scheduled")

    refresh_thread = threading.Thread(target=refresh_loop, name="hermes-refresh", daemon=True)
    refresh_thread.start()

    class SiteHandler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=str(runtime.site_dir), **kwargs)

        def end_headers(self) -> None:
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

    server = http.server.ThreadingHTTPServer((host, port), SiteHandler)
    server.daemon_threads = True
    print(
        json.dumps(
            {
                "event": "serving",
                "url": service_url(host, port),
                "site_dir": str(runtime.site_dir),
                "refresh_interval_minutes": refresh_interval_minutes,
                "daily_refresh_times": daily_refresh_times,
                "next_refresh_at": (
                    next_daily_refresh_time(daily_refresh_times).isoformat(timespec="seconds")
                    if daily_refresh_times
                    else (
                        (datetime.now().astimezone() + timedelta(minutes=max(refresh_interval_minutes, 1)))
                        .replace(microsecond=0)
                        .isoformat()
                        if refresh_interval_minutes > 0
                        else ""
                    )
                ),
                "initial_refresh": initial_refresh,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        server.shutdown()
        server.server_close()
        refresh_thread.join(timeout=1)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Hermes paper monitor pipeline")
    parser.add_argument(
        "--config",
        default="references/default-config.toml",
        help="Path to the TOML configuration file",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("bootstrap", help="Create the database and output directories")

    discover_parser = subparsers.add_parser("discover", help="Fetch and stage new papers")
    discover_parser.add_argument(
        "--skip-downloads",
        action="store_true",
        help="Skip PDF download even if the config enables it",
    )

    finalize_parser = subparsers.add_parser("finalize", help="Import enriched JSONL data")
    finalize_parser.add_argument("--input", required=True, help="JSONL file with zh_summary and affiliations")

    translate_parser = subparsers.add_parser(
        "translate-missing",
        help="Backfill missing Chinese titles and summaries with the configured LLM API",
    )
    translate_parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum number of records to translate in one run; use 0 for no limit",
    )
    translate_parser.add_argument(
        "--source-id",
        default="",
        help="Only translate papers from the specified source id",
    )
    translate_parser.add_argument(
        "--titles-only",
        action="store_true",
        help="Only backfill missing Chinese titles",
    )
    translate_parser.add_argument(
        "--summaries-only",
        action="store_true",
        help="Only backfill missing Chinese summaries",
    )

    serve_parser = subparsers.add_parser(
        "serve",
        help="Serve the local reading site over HTTP and refresh papers on a schedule",
    )
    serve_parser.add_argument(
        "--host",
        default="",
        help="Bind host; defaults to the [service] config or 127.0.0.1",
    )
    serve_parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="Bind port; defaults to the [service] config or 8765",
    )
    serve_parser.add_argument(
        "--refresh-minutes",
        type=int,
        default=-1,
        help="Scheduled refresh interval in minutes; use 0 to disable periodic refresh",
    )
    serve_parser.add_argument(
        "--refresh-at",
        action="append",
        default=[],
        help="Local daily refresh time in HH:MM; pass multiple times or comma-separated values",
    )
    serve_parser.add_argument(
        "--skip-initial-refresh",
        action="store_true",
        help="Start the HTTP service without an initial discover/build refresh",
    )

    subparsers.add_parser("build-site", help="Render the local static reading site")
    subparsers.add_parser("run", help="Run bootstrap, discover, and build-site")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    config_path = Path(args.config).expanduser().resolve()
    runtime, keyword_terms, category_terms, sources = load_config(config_path)

    if args.command == "bootstrap":
        result = run_bootstrap(runtime)
    elif args.command == "discover":
        result = discover_records(runtime, keyword_terms, category_terms, sources, args.skip_downloads)
    elif args.command == "finalize":
        result = finalize_records(runtime, Path(args.input).expanduser().resolve())
    elif args.command == "translate-missing":
        result = translate_missing_summaries(
            runtime,
            args.limit,
            args.source_id,
            args.titles_only,
            args.summaries_only,
        )
    elif args.command == "serve":
        host = clean_text(args.host) or runtime.service.host
        port = int(args.port) if int(args.port) > 0 else runtime.service.port
        refresh_minutes = (
            int(args.refresh_minutes)
            if int(args.refresh_minutes) >= 0
            else runtime.service.refresh_interval_minutes
        )
        refresh_times = (
            normalize_refresh_times(args.refresh_at)
            if args.refresh_at
            else runtime.service.daily_refresh_times
        )
        initial_refresh = runtime.service.initial_refresh and not args.skip_initial_refresh
        serve_site(
            runtime,
            keyword_terms,
            category_terms,
            sources,
            host,
            port,
            refresh_minutes,
            refresh_times,
            initial_refresh,
        )
        return 0
    elif args.command == "build-site":
        result = build_site(runtime)
    elif args.command == "run":
        result = run_pipeline(runtime, keyword_terms, category_terms, sources)
    else:
        raise ValueError(f"unknown command: {args.command}")

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

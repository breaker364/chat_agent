from __future__ import annotations

import base64
import json
import os
import re
import time
from datetime import datetime
from http import HTTPStatus
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from langchain_core.tools import tool
from langchain_mcp_adapters.sessions import StdioConnection
from langchain_mcp_adapters.tools import load_mcp_tools

_ALLOWED_ROOT: str | None = None
_SEARCH_CACHE: dict[str, dict[str, Any]] = {}
_FETCH_CACHE: dict[str, dict[str, Any]] = {}

_SEARCH_CACHE_TTL_SECONDS = 900
_FETCH_CACHE_TTL_SECONDS = 900
_SEARCH_TIMEOUT_SECONDS = 12
_FETCH_TIMEOUT_SECONDS = 15
_SEARCH_PREVIEW_TIMEOUT_SECONDS = 5
_SEARCH_RESULT_FETCH_COUNT = 2
_MAX_BING_RESULTS = 12
_MAX_FETCH_REDIRECTS = 10
_MAX_SEARCH_VARIANTS = 6

_BING_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
}

_TIME_KEYWORDS = ("今天", "今日", "今年", "本周", "最新", "最近", "刚刚", "current", "latest", "today")
_VOLATILE_KEYWORDS = ("价格", "售价", "多少钱", "股价", "汇率", "天气", "排名", "积分", "standings", "price", "weather")
_HISTORY_KEYWORDS = ("历史", "回顾", "archive", "历年", "百科", "wiki")
_NOISE_KEYWORDS = ("字典", "dictionary", "moba", "英雄联盟", "拼音", "部首", "baike")

_OFFICIAL_DOMAINS = {
    "formula1.com",
    "weather.com.cn",
    "nmc.cn",
    "nio.cn",
    "nio.com",
    "hfut.edu.cn",
    "xiaomi.com",
    "mi.com",
}
_VERTICAL_DOMAINS = {
    "motorsport.com",
    "crash.net",
    "autohome.com.cn",
    "58che.com",
    "cheshi.com",
}
_TECH_MEDIA_DOMAINS = {
    "espn.com",
    "sports.cctv.com",
    "sportingnews.com",
    "si.com",
    "zol.com.cn",
    "ithome.com",
    "finance.sina.com.cn",
}
_AGGREGATOR_DOMAINS = {
    "sohu.com",
    "163.com",
    "qq.com",
    "toutiao.com",
    "bilibili.com",
    "wikipedia.org",
    "baike.baidu.com",
}


def set_allowed_root(path: str | Path) -> None:
    global _ALLOWED_ROOT
    _ALLOWED_ROOT = str(Path(path).resolve())


def _ensure_allowed(path: str) -> Path:
    raw = (path or "").strip() or "."
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate.resolve()
    root_str = _ALLOWED_ROOT or os.getcwd()
    root = Path(root_str).resolve()
    target = (root / candidate).resolve()
    if not str(target).startswith(str(root)):
        raise PermissionError(f"Access denied: {target} is outside {root}")
    return target


def _validate_public_url(url: str) -> bool:
    try:
        parsed = urlparse((url or "").strip())
    except Exception:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    if not parsed.netloc:
        return False
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return False
    if hostname in {"localhost", "127.0.0.1", "::1"}:
        return False
    if hostname.endswith(".local"):
        return False
    return True


def _upgrade_url_if_needed(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme == "http":
        return parsed._replace(scheme="https").geturl()
    return url


def _is_permitted_redirect(source_url: str, redirect_url: str) -> bool:
    try:
        source = urlparse(source_url)
        target = urlparse(redirect_url)
    except Exception:
        return False
    source_host = re.sub(r"^www\.", "", (source.hostname or "").lower())
    target_host = re.sub(r"^www\.", "", (target.hostname or "").lower())
    if not source_host or not target_host:
        return False
    if source_host == target_host:
        return True
    if target_host.endswith("." + source_host) or source_host.endswith("." + target_host):
        return True
    return False


@tool
def list_directory(path: str) -> str:
    """List files and folders in a directory. Provide a relative path from the workspace root."""
    if not path or path.strip() == "":
        path = "."
    try:
        target = _ensure_allowed(path)
    except PermissionError as exc:
        return str(exc)
    if not target.is_dir():
        return f"Not a directory: {target}"
    items: list[str] = []
    for entry in sorted(target.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
        suffix = "/" if entry.is_dir() else ""
        items.append(f"{entry.name}{suffix}")
    return "\n".join(items) if items else "(empty directory)"


@tool
def read_file(path: str) -> str:
    """Read the contents of a text file. Provide a relative path from the workspace root."""
    try:
        target = _ensure_allowed(path)
    except PermissionError as exc:
        return str(exc)
    if not target.is_file():
        return f"File not found: {target}"
    try:
        raw = target.read_bytes()
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return f"[Binary file: {target.name}, size={len(raw)} bytes]"


@tool
def get_file_info(path: str) -> str:
    """Get metadata about a file or directory (size, modified time, type)."""
    try:
        target = _ensure_allowed(path)
    except PermissionError as exc:
        return str(exc)
    if not target.exists():
        return f"Not found: {target}"
    stat = target.stat()
    kind = "directory" if target.is_dir() else "file"
    return "\n".join(
        [
            f"Name: {target.name}",
            f"Path: {target}",
            f"Type: {kind}",
            f"Size: {stat.st_size:,} bytes",
            f"Modified: {stat.st_mtime}",
        ]
    )


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).lower()


def _extract_ascii_terms(text: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"[a-z0-9]+", _normalize_text(text))))


def _extract_chinese_terms(text: str) -> list[str]:
    chunks = re.findall(r"[\u4e00-\u9fff]{2,}", text)
    return list(dict.fromkeys(chunks))


def _current_year() -> int:
    return datetime.now().year


def _extract_years(text: str) -> list[int]:
    years = [int(y) for y in re.findall(r"\b(20\d{2})\b", text)]
    return list(dict.fromkeys(years))


def _resolve_expected_year(query: str) -> int | None:
    years = _extract_years(query)
    if years:
        return max(years)
    current_year = _current_year()
    if any(token in query for token in ("今年", "本赛季", "本年度", "今天", "今日", "最新")):
        return current_year
    return None


def _detect_language(query: str) -> str:
    if re.search(r"[\u4e00-\u9fff]", query):
        return "zh"
    return "en"


def _detect_query_type(query: str) -> str:
    lowered = _normalize_text(query)
    if "天气" in query or "weather" in lowered:
        return "weather"
    if any(token in query for token in ("排名", "积分榜", "积分", "榜单")) or "standings" in lowered:
        return "ranking"
    if any(token in query for token in ("尺寸", "价格", "重量", "参数", "配置")) or any(token in lowered for token in ("spec", "price", "weight", "dimensions")):
        return "product_specs"
    if any(token in query for token in ("校长", "现任", "院长", "董事长", "ceo", "president")):
        return "people_role"
    if any(token in query for token in ("地址", "位置", "在哪", "工厂")) or any(token in lowered for token in ("address", "location", "factory")):
        return "location"
    return "generic"


def _contains_any(query: str, tokens: list[str]) -> bool:
    return any(token in query for token in tokens)


def _detect_query_type_v2(query: str) -> str:
    lowered = _normalize_text(query)
    weather_tokens = ["\u5929\u6c14"]
    ranking_tokens = ["\u6392\u540d", "\u79ef\u5206", "\u699c\u5355", "\u79ef\u5206\u699c"]
    product_tokens = ["\u5c3a\u5bf8", "\u4ef7\u683c", "\u91cd\u91cf", "\u53c2\u6570", "\u914d\u7f6e"]
    people_tokens = ["\u603b\u88c1", "\u8463\u4e8b\u957f", "\u6821\u957f", "\u9662\u957f", "\u6bd5\u4e1a\u9662\u6821", "\u5b66\u5386", "\u4e2a\u4eba\u7b80\u4ecb"]
    location_tokens = ["\u5730\u5740", "\u4f4d\u7f6e", "\u5728\u54ea", "\u5de5\u5382"]

    if _contains_any(query, weather_tokens) or "weather" in lowered:
        return "weather"
    if _contains_any(query, ranking_tokens) or "standings" in lowered:
        return "ranking"
    if _contains_any(query, product_tokens) or any(token in lowered for token in ("spec", "price", "weight", "dimensions")):
        return "product_specs"
    if _contains_any(query, people_tokens) or any(token in lowered for token in ("ceo", "president", "biography", "alma mater")):
        return "people_role"
    if _contains_any(query, location_tokens) or any(token in lowered for token in ("address", "location", "factory")):
        return "location"
    return _detect_query_type(query)


def _extract_entities(query: str) -> list[str]:
    chinese_terms = _extract_chinese_terms(query)
    ascii_terms = _extract_ascii_terms(query)
    entities: list[str] = []
    for term in chinese_terms:
        if term not in _TIME_KEYWORDS and term not in _VOLATILE_KEYWORDS:
            entities.append(term)
    for term in ascii_terms:
        if term not in {"the", "and", "for", "with"}:
            entities.append(term)
    return list(dict.fromkeys(entities))[:8]


def _extract_person_role_terms(query: str) -> list[str]:
    role_terms: list[str] = []
    pairs = [
        ("总裁", "总裁"),
        ("董事长", "董事长"),
        ("CEO", "CEO"),
        ("ceo", "CEO"),
        ("创始人", "创始人"),
        ("校长", "校长"),
        ("毕业院校", "毕业院校"),
        ("毕业于", "毕业院校"),
        ("学历", "学历"),
        ("个人简介", "个人简介"),
    ]
    for token, normalized in pairs:
        if token in query:
            role_terms.append(normalized)
    return list(dict.fromkeys(role_terms))


def _extract_person_role_terms_v2(query: str) -> list[str]:
    pairs = [
        ("\u603b\u88c1", "\u603b\u88c1"),
        ("\u8463\u4e8b\u957f", "\u8463\u4e8b\u957f"),
        ("CEO", "CEO"),
        ("ceo", "CEO"),
        ("\u521b\u59cb\u4eba", "\u521b\u59cb\u4eba"),
        ("\u6821\u957f", "\u6821\u957f"),
        ("\u6bd5\u4e1a\u9662\u6821", "\u6bd5\u4e1a\u9662\u6821"),
        ("\u6bd5\u4e1a\u4e8e", "\u6bd5\u4e1a\u9662\u6821"),
        ("\u5b66\u5386", "\u5b66\u5386"),
        ("\u4e2a\u4eba\u7b80\u4ecb", "\u4e2a\u4eba\u7b80\u4ecb"),
    ]
    role_terms: list[str] = []
    for token, normalized in pairs:
        if token in query:
            role_terms.append(normalized)
    return list(dict.fromkeys(role_terms))


def _extract_primary_subject(query: str) -> str:
    entities = _extract_entities(query)
    if entities:
        return entities[0]
    return query.strip()


def _extract_subject_candidates(query: str) -> list[str]:
    lowered = _normalize_text(query)
    subjects: list[str] = []
    if "\u851a\u6765" in query or "nio" in lowered:
        subjects.append("\u851a\u6765")
        subjects.append("NIO")
    if "\u5c0f\u7c73" in query or "xiaomi" in lowered:
        subjects.append("\u5c0f\u7c73\u96c6\u56e2")
        subjects.append("Xiaomi")
    entities = _extract_entities(query)
    for entity in entities:
        if entity not in subjects:
            subjects.append(entity)
    return list(dict.fromkeys(subjects))[:4]


def _extract_person_names(query: str) -> list[str]:
    company_terms = {
        "\u851a\u6765",
        "\u851a\u6765\u6c7d\u8f66",
        "\u5c0f\u7c73",
        "\u5c0f\u7c73\u96c6\u56e2",
        "NIO",
        "Xiaomi",
    }
    role_terms = {
        "\u603b\u88c1",
        "\u8463\u4e8b\u957f",
        "\u521b\u59cb\u4eba",
        "\u6bd5\u4e1a\u5b66\u6821",
        "\u6bd5\u4e1a\u9662\u6821",
        "\u5b66\u5386",
        "\u4e2a\u4eba\u7b80\u4ecb",
        "CEO",
        "ceo",
    }
    names: list[str] = []
    for token in re.findall(r"[\u4e00-\u9fff]{2,4}", query):
        if token in company_terms or token in role_terms:
            continue
        if any(term in token for term in ["\u6bd5\u4e1a", "\u5b66\u6821", "\u9662\u6821", "\u7b80\u4ecb", "\u5b66\u5386"]):
            continue
        names.append(token)
    return list(dict.fromkeys(names))[:3]


def _extract_keywords_for_query_v2(query: str, query_type: str, expected_year: int | None) -> list[str]:
    keywords = _extract_entities(query)
    lowered = _normalize_text(query)

    if query_type == "ranking" and "f1" in lowered:
        keywords = ["F1", "driver", "standings", "points", "leader"]
    elif query_type == "weather":
        city_terms = [term for term in _extract_chinese_terms(query) if "澶╂皵" not in term]
        city = city_terms[0] if city_terms else query.replace("澶╂皵", "").strip()
        keywords = [city, "浠婂ぉ澶╂皵"]
    elif query_type == "product_specs":
        keys: list[str] = []
        if "nio" in lowered or "钄氭潵" in query:
            keys.append("钄氭潵")
        if "xiaomi" in lowered or "灏忕背" in query:
            keys.append("灏忕背")
        model_match = re.search(r"([A-Za-z]{1,4}\d{1,2})", query)
        if model_match:
            keys.append(model_match.group(1).upper())
        for token in ("灏哄", "閲嶉噺", "浠锋牸", "鍙傛暟"):
            if token in query:
                keys.append(token)
        keywords = keys or keywords
    elif query_type == "people_role":
        keys = []
        subject = _extract_primary_subject(query)
        if subject:
            keys.append(subject)
        keys.extend(_extract_person_role_terms_v2(query))
        if "nio" in lowered or "钄氭潵" in query:
            keys.insert(0, "钄氭潵")
        if "xiaomi" in lowered or "灏忕背" in query:
            keys.insert(0, "灏忕背")
        keywords = keys or keywords
    elif query_type == "location":
        keys = []
        if "钄氭潵" in query or "nio" in lowered:
            keys.append("钄氭潵")
        for token in ("鍦板潃", "浣嶇疆"):
            if token in query:
                keys.append(token)
        keywords = keys or keywords

    if expected_year and str(expected_year) not in keywords:
        keywords.insert(0, str(expected_year))
    return list(dict.fromkeys([k for k in keywords if k]))[:10]


def _extract_keywords_for_query_v3(query: str, query_type: str, expected_year: int | None) -> list[str]:
    keywords = _extract_entities(query)
    lowered = _normalize_text(query)
    company_nio = "\u851a\u6765"
    company_xiaomi = "\u5c0f\u7c73"

    if query_type == "ranking" and "f1" in lowered:
        keywords = ["F1", "driver", "standings", "points", "leader"]
    elif query_type == "people_role":
        keys = []
        subject = _extract_primary_subject(query)
        person_names = _extract_person_names(query)
        if subject:
            keys.append(subject)
        keys.extend(person_names)
        keys.extend(_extract_person_role_terms_v2(query))
        if "nio" in lowered or company_nio in query:
            keys.insert(0, company_nio)
        if "xiaomi" in lowered or company_xiaomi in query:
            keys.insert(0, company_xiaomi)
        keywords = keys or keywords
    elif query_type == "product_specs":
        keys = []
        if "nio" in lowered or company_nio in query:
            keys.append(company_nio)
        if "xiaomi" in lowered or company_xiaomi in query:
            keys.append(company_xiaomi)
        for token in ["\u5c3a\u5bf8", "\u91cd\u91cf", "\u4ef7\u683c", "\u53c2\u6570", "\u914d\u7f6e"]:
            if token in query:
                keys.append(token)
        keywords = keys or keywords
    elif query_type == "location":
        keys = []
        if "nio" in lowered or company_nio in query:
            keys.append(company_nio)
        for token in ["\u5730\u5740", "\u4f4d\u7f6e"]:
            if token in query:
                keys.append(token)
        keywords = keys or keywords

    if expected_year and str(expected_year) not in keywords:
        keywords.insert(0, str(expected_year))
    return list(dict.fromkeys([k for k in keywords if k]))[:10]


def _extract_keywords_for_query(query: str, query_type: str, expected_year: int | None) -> list[str]:
    keywords = _extract_entities(query)
    if query_type == "ranking":
        if "f1" in _normalize_text(query):
            keywords = ["F1", "driver", "standings", "points", "leader"]
    elif query_type == "weather":
        city_terms = [term for term in _extract_chinese_terms(query) if "天气" not in term]
        city = city_terms[0] if city_terms else query.replace("天气", "").strip()
        keywords = [city, "今天天气"]
    elif query_type == "product_specs":
        keys = []
        if "蔚来" in query or "nio" in _normalize_text(query):
            keys.append("蔚来")
        model_match = re.search(r"([A-Za-z]{1,4}\d{1,2})", query)
        if model_match:
            keys.append(model_match.group(1).upper())
        for token in ("尺寸", "重量", "价格", "参数"):
            if token in query:
                keys.append(token)
        keywords = keys or keywords
    elif query_type == "people_role":
        if "合工大" in query:
            keywords = ["合肥工业大学", "现任校长"]
    elif query_type == "location":
        keys = []
        if "蔚来" in query:
            keys.append("蔚来")
        if "f1工厂" in query.lower() or "f1工厂" in query:
            keys.extend(["F1工厂", "江淮蔚来先进制造基地", "合肥"])
        for token in ("地址", "位置"):
            if token in query:
                keys.append(token)
        keywords = keys or keywords
    if expected_year:
        if str(expected_year) not in keywords:
            keywords.insert(0, str(expected_year))
    return list(dict.fromkeys([k for k in keywords if k]))[:8]


def _build_search_queries(query: str, query_type: str, language: str, expected_year: int | None) -> list[str]:
    keywords = _extract_keywords_for_query(query, query_type, expected_year)
    queries = [query]
    if query_type == "ranking" and "f1" in _normalize_text(query):
        queries.append(f"{expected_year or _current_year()} F1 driver standings points leader")
    elif query_type == "weather":
        city = next((term for term in _extract_chinese_terms(query) if "天气" not in term), "合肥")
        queries.append(f"{city}今天天气")
    elif query_type == "product_specs":
        queries.append(" ".join(keywords))
    elif query_type == "people_role":
        queries.append(" ".join(keywords))
    elif query_type == "location":
        queries.append(" ".join(keywords))
    else:
        queries.append(" ".join(keywords))

    deduped: list[str] = []
    seen: set[str] = set()
    for item in queries:
        normalized = _normalize_text(item)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(item)
    return deduped[:_MAX_SEARCH_VARIANTS]


def _build_search_queries_v2(query: str, query_type: str, language: str, expected_year: int | None) -> list[str]:
    keywords = _extract_keywords_for_query_v2(query, query_type, expected_year)
    lowered = _normalize_text(query)
    queries = [query]

    if query_type == "people_role":
        subject = _extract_primary_subject(query)
        role_terms = _extract_person_role_terms_v2(query)
        if subject:
            queries.append(f"{subject} {' '.join(role_terms)}")
        if subject and role_terms:
            queries.append(f"{subject} {' '.join(role_terms)} 简历")
            queries.append(f"{subject} {' '.join(role_terms)} 教育经历")
        if "xiaomi" in lowered or "灏忕背" in query:
            queries.append("小米集团 总裁 毕业院校")
            queries.append("小米集团 CEO 教育经历")
        if "nio" in lowered or "钄氭潵" in query:
            queries.append("蔚来汽车 总裁 毕业院校")
            queries.append("蔚来汽车 CEO 教育经历")
    elif query_type == "product_specs":
        queries.append(" ".join(keywords))
        subject = _extract_primary_subject(query)
        if subject:
            queries.append(f"{subject} 参数 配置")
    elif query_type == "ranking" and "f1" in lowered:
        queries.append(f"{expected_year or _current_year()} F1 driver standings points leader")
    elif query_type == "weather":
        city = next((term for term in _extract_chinese_terms(query) if "澶╂皵" not in term), "鍚堣偉")
        queries.append(f"{city}浠婂ぉ澶╂皵")
    else:
        queries.append(" ".join(keywords))

    deduped: list[str] = []
    seen: set[str] = set()
    for item in queries:
        normalized = _normalize_text(item)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(item)
    return deduped[:_MAX_SEARCH_VARIANTS]


def _build_search_queries_v3(query: str, query_type: str, language: str, expected_year: int | None) -> list[str]:
    keywords = _extract_keywords_for_query_v3(query, query_type, expected_year)
    lowered = _normalize_text(query)
    company_nio = "\u851a\u6765"
    company_xiaomi = "\u5c0f\u7c73"
    queries = [query]

    if query_type == "people_role":
        subject = _extract_primary_subject(query)
        subject_candidates = _extract_subject_candidates(query)
        person_names = _extract_person_names(query)
        role_terms = _extract_person_role_terms_v2(query)
        prioritized_queries: list[str] = []
        for candidate in subject_candidates[:3]:
            if person_names and role_terms:
                for person_name in person_names:
                    prioritized_queries.append(f"{candidate} {person_name} {' '.join(role_terms)}")
                    prioritized_queries.append(f"{person_name} {candidate} {' '.join(role_terms)}")
            elif role_terms:
                prioritized_queries.append(f"{candidate} {' '.join(role_terms)}")
                prioritized_queries.append(f"{candidate} {' '.join(role_terms)} \u6bd5\u4e1a\u9662\u6821")
            else:
                prioritized_queries.append(f"{candidate} \u603b\u88c1 \u6bd5\u4e1a\u9662\u6821")
        if subject:
            prioritized_queries.append(f"{subject} {' '.join(role_terms)}")
        if subject and role_terms:
            prioritized_queries.append(f"{subject} {' '.join(role_terms)} \u7b80\u5386")
            prioritized_queries.append(f"{subject} {' '.join(role_terms)} \u6559\u80b2\u7ecf\u5386")
        for person_name in person_names[:2]:
            prioritized_queries.append(f"{person_name} \u7b80\u5386")
            for candidate in subject_candidates[:2]:
                prioritized_queries.append(f"{person_name} {candidate}")
                prioritized_queries.append(f"{person_name} {candidate} site:baike.baidu.com")
                prioritized_queries.append(f"{person_name} {candidate} site:wikipedia.org")
        if "xiaomi" in lowered or company_xiaomi in query:
            prioritized_queries.append(f"{company_xiaomi}\u96c6\u56e2 \u603b\u88c1 \u6bd5\u4e1a\u9662\u6821")
            prioritized_queries.append(f"{company_xiaomi}\u96c6\u56e2 CEO \u6559\u80b2\u7ecf\u5386")
        if "nio" in lowered or company_nio in query:
            prioritized_queries.append(f"{company_nio} \u603b\u88c1 \u6bd5\u4e1a\u9662\u6821")
            prioritized_queries.append(f"{company_nio} CEO \u6559\u80b2\u7ecf\u5386")
        queries = prioritized_queries + [query]
    elif query_type == "product_specs":
        queries.append(" ".join(keywords))
    elif query_type == "ranking" and "f1" in lowered:
        queries.append(f"{expected_year or _current_year()} F1 driver standings points leader")
    elif query_type == "weather":
        queries.append(" ".join(keywords))
    else:
        queries.append(" ".join(keywords))

    deduped: list[str] = []
    seen: set[str] = set()
    for item in queries:
        normalized = _normalize_text(item)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(item)
    return deduped[:_MAX_SEARCH_VARIANTS]


def _infer_preferred_domains(query: str, query_type: str) -> list[str]:
    lowered = _normalize_text(query)
    if query_type == "weather":
        return ["weather.com.cn", "nmc.cn"]
    if query_type == "ranking" and "f1" in lowered:
        return ["formula1.com", "motorsport.com", "crash.net", "espn.com"]
    if query_type == "product_specs" and ("蔚来" in query or "nio" in lowered):
        return ["nio.cn", "nio.com", "autohome.com.cn", "58che.com"]
    if query_type == "people_role" and ("合工大" in query or "合肥工业大学" in query):
        return ["hfut.edu.cn"]
    if query_type == "location" and "蔚来" in query:
        return ["nio.cn", "nio.com", "autohome.com.cn", "58che.com"]
    return []


def _infer_preferred_domains_v2(query: str, query_type: str) -> list[str]:
    domains = list(_infer_preferred_domains(query, query_type))
    lowered = _normalize_text(query)
    if query_type == "people_role":
        if "xiaomi" in lowered or "\u5c0f\u7c73" in query:
            domains.extend(["xiaomi.com", "mi.com", "baike.baidu.com"])
        if "nio" in lowered or "\u851a\u6765" in query:
            domains.extend(["nio.cn", "nio.com", "baike.baidu.com"])
    return list(dict.fromkeys(domains))


def _should_search(query: str, expected_year: int | None) -> bool:
    if any(token in query for token in _TIME_KEYWORDS):
        return True
    if expected_year and expected_year > 2024:
        return True
    if any(token in query for token in _VOLATILE_KEYWORDS):
        return True
    return True


def _decode_html_entities(value: str) -> str:
    return unescape(value or "")


def _resolve_bing_url(raw_url: str) -> str | None:
    if not raw_url or raw_url.startswith("/") or raw_url.startswith("#"):
        return None
    match = re.search(r"[?&]u=([a-zA-Z0-9+/_=-]+)", raw_url)
    if match:
        encoded = match.group(1)
        if len(encoded) >= 3:
            payload = encoded[2:]
            padded = payload.replace("-", "+").replace("_", "/")
            while len(padded) % 4:
                padded += "="
            try:
                decoded = base64.b64decode(padded).decode("utf-8")
                if decoded.startswith("http"):
                    return decoded
            except Exception:
                pass
    if "bing.com" not in raw_url:
        return raw_url
    return None


def _extract_bing_snippet(block: str) -> str:
    patterns = [
        r'<p[^>]*class="b_lineclamp[^"]*"[^>]*>([\s\S]*?)</p>',
        r'<div[^>]*class="b_caption[^"]*"[^>]*>[\s\S]*?<p[^>]*>([\s\S]*?)</p>',
        r'<div[^>]*class="b_caption[^"]*"[^>]*>([\s\S]*?)</div>',
    ]
    for pattern in patterns:
        match = re.search(pattern, block, re.IGNORECASE)
        if match:
            text = re.sub(r"<[^>]+>", "", match.group(1)).strip()
            if text:
                return _decode_html_entities(text)
    return ""


def _search_with_bing(query: str, count: int) -> list[dict[str, str]]:
    response = requests.get(
        "https://www.bing.com/search",
        params={"q": query, "setmkt": "zh-CN", "setlang": "zh-Hans"},
        headers=_BING_HEADERS,
        timeout=_SEARCH_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    html = response.text

    results: list[dict[str, str]] = []
    for match in re.finditer(r'<li\s+class="b_algo"[^>]*>([\s\S]*?)</li>', html, re.IGNORECASE):
        block = match.group(1)
        link_match = re.search(
            r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>([\s\S]*?)</a>',
            block,
            re.IGNORECASE,
        )
        if not link_match:
            continue
        url = _resolve_bing_url(_decode_html_entities(link_match.group(1)))
        if not url:
            continue
        title = _decode_html_entities(re.sub(r"<[^>]+>", "", link_match.group(2)).strip())
        snippet = _extract_bing_snippet(block)
        results.append({"title": title, "url": url, "snippet": snippet[:400]})
        if len(results) >= max(count * 2, _MAX_BING_RESULTS):
            break
    return results


def _apply_domain_filters(
    results: list[dict[str, str]],
    allowed_domains: list[str] | None,
    blocked_domains: list[str] | None,
) -> list[dict[str, str]]:
    filtered: list[dict[str, str]] = []
    normalized_allowed = [re.sub(r"^www\.", "", d.lower()) for d in (allowed_domains or [])]
    normalized_blocked = [re.sub(r"^www\.", "", d.lower()) for d in (blocked_domains or [])]
    for item in results:
        try:
            hostname = re.sub(r"^www\.", "", (urlparse(item["url"]).hostname or "").lower())
        except Exception:
            continue
        if normalized_allowed and not any(hostname == d or hostname.endswith("." + d) for d in normalized_allowed):
            continue
        if normalized_blocked and any(hostname == d or hostname.endswith("." + d) for d in normalized_blocked):
            continue
        filtered.append(item)
    return filtered


def _extract_domain(url: str) -> str:
    hostname = (urlparse(url).hostname or "").lower()
    return re.sub(r"^www\.", "", hostname)


def _score_credibility(url: str) -> tuple[float, str]:
    domain = _extract_domain(url)
    if domain in _OFFICIAL_DOMAINS or domain.endswith(".gov.cn") or domain.endswith(".edu.cn"):
        return 0.95, "tier1_official"
    if domain in _VERTICAL_DOMAINS:
        return 0.85, "tier2_vertical"
    if domain in _TECH_MEDIA_DOMAINS:
        return 0.70, "tier2_media"
    if domain in _AGGREGATOR_DOMAINS:
        return 0.50, "tier3_aggregator"
    return 0.30, "tier3_unknown"


def _check_timeliness(title: str, url: str, expected_year: int | None) -> tuple[bool, str]:
    haystack = f"{title} {url}".lower()
    if any(token in haystack for token in _HISTORY_KEYWORDS):
        return False, "history_keyword"
    if expected_year is None:
        return True, "no_expected_year"
    years = _extract_years(f"{title} {url}")
    if years and max(years) < expected_year:
        return False, f"older_year:{max(years)}"
    if expected_year >= 2025 and "2024" in haystack and str(expected_year) not in haystack:
        return False, "older_season"
    return True, "pass"


def _score_relevance(query: str, title: str, snippet: str, url: str, query_terms: list[str], preferred_domains: list[str]) -> float:
    haystack = _normalize_text(" ".join([title, snippet, url]))
    raw_text = " ".join([title, snippet, url])
    score = 0.0
    for term in query_terms:
        if re.search(r"[\u4e00-\u9fff]", term):
            if term in raw_text:
                score += 3.0
        elif _normalize_text(term) in haystack:
            score += 2.0
    domain = _extract_domain(url)
    if preferred_domains and any(domain == d or domain.endswith("." + d) for d in preferred_domains):
        score += 2.0
    if any(noise in raw_text for noise in _NOISE_KEYWORDS):
        score -= 8.0
    return score


def _is_low_quality_result(title: str, snippet: str, url: str) -> bool:
    text = _normalize_text(" ".join([title, snippet, url]))
    if not title.strip():
        return True
    if "video-recommend" in urlparse(url).path.lower():
        return True
    if any(token in text for token in ("related searches", "dictionary", "captcha")):
        return True
    return False


def _need_webfetch(query_type: str, results: list[dict[str, Any]]) -> bool:
    if not results:
        return False
    if query_type in {"weather", "product_specs", "people_role", "location"}:
        return True
    if query_type == "ranking":
        text = " ".join(item.get("title", "") + " " + item.get("snippet", "") for item in results[:2])
        return not bool(re.search(r"\b\d{1,4}\b", text))
    return False


def _has_people_role_signal(query: str, title: str, snippet: str, url: str) -> bool:
    haystack = f"{title} {snippet} {url}".lower()
    person_names = _extract_person_names(query)
    tokens = [
        "\u603b\u88c1",
        "\u8463\u4e8b\u957f",
        "\u4e2a\u4eba\u7b80\u4ecb",
        "\u7b80\u5386",
        "\u5b66\u5386",
        "\u6bd5\u4e1a",
        "\u6559\u80b2",
        "ceo",
        "president",
        "biography",
        "education",
    ]
    if any(token.lower() in haystack for token in tokens):
        return True
    if person_names and any(name.lower() in haystack for name in person_names):
        if "baike.baidu.com" in haystack or "wikipedia.org" in haystack or "/item/" in haystack:
            return True
    return False


def _evaluate_results(
    query: str,
    query_type: str,
    raw_results: list[dict[str, str]],
    expected_year: int | None,
    query_terms: list[str],
    preferred_domains: list[str],
    count: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    accepted: list[dict[str, Any]] = []
    tier_counts = {"tier1": 0, "tier2": 0, "tier3": 0}
    supporting_domains: set[str] = set()
    rejected: list[dict[str, Any]] = []

    for item in raw_results:
        title = item.get("title", "")
        snippet = item.get("snippet", "")
        url = item.get("url", "")
        if _is_low_quality_result(title, snippet, url):
            rejected.append({"url": url, "reason": "low_quality"})
            continue
        if query_type == "people_role" and not _has_people_role_signal(query, title, snippet, url):
            rejected.append({"url": url, "reason": "missing_people_role_signal"})
            continue

        credibility, tier = _score_credibility(url)
        timely, timely_reason = _check_timeliness(title, url, expected_year)
        if not timely:
            rejected.append({"url": url, "reason": timely_reason})
            continue

        relevance = _score_relevance(query, title, snippet, url, query_terms, preferred_domains)
        if relevance <= 0:
            rejected.append({"url": url, "reason": "low_relevance"})
            continue

        if tier.startswith("tier1"):
            tier_counts["tier1"] += 1
        elif tier.startswith("tier2"):
            tier_counts["tier2"] += 1
        else:
            tier_counts["tier3"] += 1

        domain = _extract_domain(url)
        supporting_domains.add(domain)
        accepted.append(
            {
                "title": title,
                "url": url,
                "snippet": snippet,
                "credibility_score": credibility,
                "credibility_tier": tier,
                "timeliness": timely_reason,
                "relevance_score": relevance,
                "_domain": domain,
            }
        )

    accepted.sort(
        key=lambda item: (
            item["credibility_score"],
            item["relevance_score"],
            1 if item["_domain"] in preferred_domains else 0,
        ),
        reverse=True,
    )

    deduped: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    seen_domains: set[str] = set()
    for item in accepted:
        if item["url"] in seen_urls:
            continue
        if item["credibility_tier"].startswith("tier3") and (tier_counts["tier1"] > 0 or tier_counts["tier2"] > 1):
            continue
        if item["_domain"] in seen_domains and len(deduped) >= 1:
            continue
        seen_urls.add(item["url"])
        seen_domains.add(item["_domain"])
        item.pop("_domain", None)
        deduped.append(item)
        if len(deduped) >= count:
            break

    evaluation = {
        "tier_counts": tier_counts,
        "supporting_domain_count": len(supporting_domains),
        "cross_validation": "multi_source" if len(supporting_domains) >= 2 else "single_source",
        "rejected_count": len(rejected),
        "rejected_examples": rejected[:5],
    }
    return deduped, evaluation


def _assess_confidence(
    query_type: str,
    results: list[dict[str, Any]],
    evaluation: dict[str, Any] | None,
) -> dict[str, Any]:
    if not results:
        return {
            "level": "low",
            "score": 0.1,
            "reason": "no_results",
            "sufficient": False,
        }

    evaluation = evaluation or {}
    domain_count = int(evaluation.get("supporting_domain_count", 0))
    tier_counts = evaluation.get("tier_counts", {})
    tier1_count = int(tier_counts.get("tier1", 0))
    tier2_count = int(tier_counts.get("tier2", 0))
    top_score = float(results[0].get("credibility_score", 0.0))
    fetched_count = sum(1 for item in results[:3] if item.get("page_preview"))

    score = min(1.0, 0.2 + top_score * 0.3 + min(domain_count, 3) * 0.15 + min(fetched_count, 2) * 0.1)
    if tier1_count:
        score += 0.1
    elif tier2_count >= 2:
        score += 0.05
    score = min(1.0, score)

    if query_type == "people_role":
        sufficient = (domain_count >= 2 and (tier1_count >= 1 or tier2_count >= 2) and fetched_count >= 1)
    elif query_type in {"product_specs", "weather", "ranking", "location"}:
        sufficient = domain_count >= 2 or tier1_count >= 1
    else:
        sufficient = score >= 0.7

    if sufficient and score >= 0.82:
        level = "high"
    elif sufficient or score >= 0.6:
        level = "medium"
    else:
        level = "low"

    reason_parts = [
        f"domains={domain_count}",
        f"tier1={tier1_count}",
        f"tier2={tier2_count}",
        f"fetched={fetched_count}",
    ]
    return {
        "level": level,
        "score": round(score, 2),
        "reason": ", ".join(reason_parts),
        "sufficient": sufficient,
    }


def _merge_evaluated_results(results: list[dict[str, Any]], count: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not results:
        return [], {
            "tier_counts": {"tier1": 0, "tier2": 0, "tier3": 0},
            "supporting_domain_count": 0,
            "cross_validation": "single_source",
            "rejected_count": 0,
            "rejected_examples": [],
        }

    ranked = sorted(
        results,
        key=lambda item: (
            float(item.get("credibility_score", 0.0)),
            float(item.get("relevance_score", 0.0)),
        ),
        reverse=True,
    )
    merged: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    domains: set[str] = set()
    tier_counts = {"tier1": 0, "tier2": 0, "tier3": 0}

    for item in ranked:
        url = str(item.get("url", ""))
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        merged.append(item)
        domain = _extract_domain(url)
        domains.add(domain)
        tier = str(item.get("credibility_tier", ""))
        if tier.startswith("tier1"):
            tier_counts["tier1"] += 1
        elif tier.startswith("tier2"):
            tier_counts["tier2"] += 1
        else:
            tier_counts["tier3"] += 1
        if len(merged) >= count:
            break

    evaluation = {
        "tier_counts": tier_counts,
        "supporting_domain_count": len(domains),
        "cross_validation": "multi_source" if len(domains) >= 2 else "single_source",
        "rejected_count": 0,
        "rejected_examples": [],
    }
    return merged, evaluation


def _select_candidate_results(raw_results: list[dict[str, str]], count: int) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for item in raw_results:
        title = item.get("title", "")
        snippet = item.get("snippet", "")
        url = item.get("url", "")
        if not url or url in seen_urls:
            continue
        if _is_low_quality_result(title, snippet, url):
            continue
        seen_urls.add(url)
        candidates.append(
            {
                "title": title,
                "url": url,
                "snippet": snippet,
                "credibility_score": _score_credibility(url)[0],
            }
        )
        if len(candidates) >= count:
            break
    return candidates


def _fetch_preview(url: str) -> str:
    try:
        response = _fetch_url_with_permitted_redirects(url, timeout_seconds=_SEARCH_PREVIEW_TIMEOUT_SECONDS)
        if isinstance(response, dict):
            return ""
        response.raise_for_status()
    except Exception:
        return ""
    response.encoding = response.apparent_encoding or response.encoding
    _, text = _html_to_text(response.text)
    return text[:1200]


def _fetch_url_with_permitted_redirects(
    url: str,
    timeout_seconds: int = _FETCH_TIMEOUT_SECONDS,
) -> requests.Response | dict[str, Any]:
    current_url = _upgrade_url_if_needed(url)
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    }
    for _ in range(_MAX_FETCH_REDIRECTS + 1):
        response = requests.get(
            current_url,
            headers=headers,
            timeout=timeout_seconds,
            allow_redirects=False,
        )
        if response.status_code not in {301, 302, 307, 308}:
            return response
        redirect_location = response.headers.get("Location")
        if not redirect_location:
            break
        redirect_url = urljoin(current_url, redirect_location)
        if _is_permitted_redirect(current_url, redirect_url):
            current_url = redirect_url
            continue
        return {
            "type": "redirect",
            "original_url": current_url,
            "redirect_url": redirect_url,
            "status_code": response.status_code,
        }
    raise requests.TooManyRedirects(f"Too many redirects while fetching {url}")


def _html_to_text(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    return title, text


def _summarize_search_results(query: str, results: list[dict[str, Any]], need_webfetch: bool) -> str:
    if not results:
        return f"No reliable search results were found for: {query}"

    best = results[0]
    best_title = best.get("title", "").strip()
    best_snippet = best.get("snippet", "").strip()
    best_preview = best.get("page_preview", "").strip()
    summary_parts: list[str] = []

    if best_snippet:
        summary_parts.append(best_snippet)
    elif best_preview:
        summary_parts.append(best_preview[:220])
    elif best_title:
        summary_parts.append(best_title)

    if len(results) > 1:
        source_names = ", ".join(_extract_domain(item.get("url", "")) for item in results[:3] if item.get("url"))
        if source_names:
            summary_parts.append(f"Cross-checked sources: {source_names}.")

    if need_webfetch:
        summary_parts.append("This summary is based on search snippets and may benefit from page fetch for full details.")

    return " ".join(part for part in summary_parts if part).strip()


def _build_search_diagnostics(
    query_type: str,
    results: list[dict[str, Any]],
    evaluation: dict[str, Any] | None,
    confidence: dict[str, Any],
) -> tuple[list[str], list[str]]:
    insufficiencies: list[str] = []
    next_steps: list[str] = []
    evaluation = evaluation or {}

    if not results:
        insufficiencies.append("no_reliable_results")
        next_steps.append("broaden_or_rephrase_query")
        return insufficiencies, next_steps

    if evaluation.get("supporting_domain_count", 0) < 2:
        insufficiencies.append("insufficient_cross_validation")
        next_steps.append("search_additional_independent_domains")
    if query_type == "people_role" and not any(item.get("page_preview") for item in results[:3]):
        insufficiencies.append("missing_page_level_evidence")
        next_steps.append("fetch_biography_or_profile_pages")
    if confidence.get("level") == "low":
        insufficiencies.append("low_confidence")
        next_steps.append("retry_with_narrower_entity_and_role_terms")

    return list(dict.fromkeys(insufficiencies)), list(dict.fromkeys(next_steps))


@tool
def web_search(
    query: str,
    count: int = 8,
    allowed_domains: list[str] | None = None,
    blocked_domains: list[str] | None = None,
) -> str:
    """Search the web with query construction, source grading, timeliness filtering, and cross-source evaluation."""
    normalized_query = _normalize_text(query)
    cache_key = json.dumps(
        {
            "query": normalized_query,
            "count": count,
            "allowed_domains": allowed_domains or [],
            "blocked_domains": blocked_domains or [],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    now = time.time()
    cached = _SEARCH_CACHE.get(cache_key)
    if cached and now - cached["ts"] < _SEARCH_CACHE_TTL_SECONDS:
        return json.dumps(cached["payload"], ensure_ascii=False, indent=2)

    expected_year = _resolve_expected_year(query)
    language = _detect_language(query)
    query_type = _detect_query_type_v2(query)
    query_terms = _extract_keywords_for_query_v3(query, query_type, expected_year)
    preferred_domains = list(dict.fromkeys((allowed_domains or []) + _infer_preferred_domains_v2(query, query_type)))
    search_variants = _build_search_queries_v3(query, query_type, language, expected_year)
    should_search = _should_search(query, expected_year)

    started_at = time.time()
    final_results: list[dict[str, Any]] = []
    aggregated_results: list[dict[str, Any]] = []
    candidate_results: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    overall_evaluation: dict[str, Any] | None = None
    engine = "bing-html"
    backend_note: str | None = None

    if should_search:
        for variant in search_variants:
            raw_search = _search_with_bing(variant, min(max(count, 3), 10))
            raw_results = _apply_domain_filters(
                raw_search,
                allowed_domains or preferred_domains or None,
                blocked_domains,
            )
            if not raw_results and not allowed_domains:
                raw_results = _apply_domain_filters(raw_search, None, blocked_domains)
            for candidate in _select_candidate_results(raw_results, count):
                if all(existing.get("url") != candidate.get("url") for existing in candidate_results):
                    candidate_results.append(candidate)
            evaluated, evaluation = _evaluate_results(
                variant,
                query_type,
                raw_results,
                expected_year,
                query_terms,
                preferred_domains,
                count,
            )
            attempts.append(
                {
                    "query": variant,
                    "raw_count": len(raw_results),
                    "filtered_count": len(evaluated),
                    "evaluation": evaluation,
                }
            )
            if evaluated:
                aggregated_results.extend(evaluated)
                final_results, overall_evaluation = _merge_evaluated_results(aggregated_results, count)
                if _assess_confidence(query_type, final_results, overall_evaluation).get("sufficient"):
                    break
            elif overall_evaluation is None:
                overall_evaluation = evaluation

    if aggregated_results:
        final_results, overall_evaluation = _merge_evaluated_results(aggregated_results, count)

    if query_type == "people_role" and not final_results:
        person_names = _extract_person_names(query)
        subject_candidates = _extract_subject_candidates(query)
        fallback_domains = ["baike.baidu.com", "wikipedia.org"]
        for person_name in person_names[:2]:
            for subject in subject_candidates[:2]:
                for variant in [
                    f"{person_name} {subject} site:baike.baidu.com",
                    f"{person_name} {subject} site:wikipedia.org",
                ]:
                    raw_search = _search_with_bing(variant, min(max(count, 3), 10))
                    raw_results = _apply_domain_filters(raw_search, fallback_domains, blocked_domains)
                    for candidate in _select_candidate_results(raw_results, count):
                        if all(existing.get("url") != candidate.get("url") for existing in candidate_results):
                            candidate_results.append(candidate)
                    evaluated, evaluation = _evaluate_results(
                        variant,
                        query_type,
                        raw_results,
                        expected_year,
                        query_terms,
                        fallback_domains,
                        count,
                    )
                    attempts.append(
                        {
                            "query": variant,
                            "raw_count": len(raw_results),
                            "filtered_count": len(evaluated),
                            "evaluation": evaluation,
                        }
                    )
                    if evaluated:
                        aggregated_results.extend(evaluated)
        if aggregated_results:
            final_results, overall_evaluation = _merge_evaluated_results(aggregated_results, count)

    need_webfetch = _need_webfetch(query_type, final_results)
    if need_webfetch or query_type == "people_role":
        for item in final_results[:_SEARCH_RESULT_FETCH_COUNT]:
            preview = _fetch_preview(item["url"])
            if preview:
                item["page_preview"] = preview

    confidence = _assess_confidence(query_type, final_results, overall_evaluation)
    insufficiencies, next_steps = _build_search_diagnostics(query_type, final_results, overall_evaluation, confidence)
    summary = _summarize_search_results(query, final_results, need_webfetch)

    payload: dict[str, Any] = {
        "query": query,
        "engine": engine,
        "duration_seconds": round(time.time() - started_at, 3),
        "result_count": len(final_results),
        "results": final_results,
        "candidate_results": candidate_results[:5],
        "summary": summary,
        "confidence": confidence,
        "attempts": attempts,
        "query_profile": {
            "language": language,
            "query_type": query_type,
            "expected_year": expected_year,
            "should_search": should_search,
            "query_terms": query_terms,
            "preferred_domains": preferred_domains,
            "search_variants": search_variants,
        },
        "need_webfetch": need_webfetch,
        "insufficiencies": insufficiencies,
        "recommended_next_steps": next_steps,
    }
    if overall_evaluation:
        payload["evaluation"] = overall_evaluation
    if allowed_domains:
        payload["allowed_domains"] = allowed_domains
    if blocked_domains:
        payload["blocked_domains"] = blocked_domains
    if not final_results:
        payload["note"] = "No sufficiently trustworthy and timely search results were found for the topic."
        if preferred_domains and not allowed_domains:
            backend_note = "Preferred domains were prioritized during ranking, but none yielded a trustworthy answer."
    if backend_note:
        payload["backend_note"] = backend_note

    _SEARCH_CACHE[cache_key] = {"ts": now, "payload": payload}
    return json.dumps(payload, ensure_ascii=False, indent=2)


@tool
def web_fetch(url: str, prompt: str = "") -> str:
    """Fetch a webpage with safe redirect handling and return cleaned title plus content excerpt."""
    if not _validate_public_url(url):
        return json.dumps({"url": url, "error": "Invalid URL."}, ensure_ascii=False, indent=2)

    cache_key = json.dumps({"url": url, "prompt": prompt}, ensure_ascii=False, sort_keys=True)
    cached = _FETCH_CACHE.get(cache_key)
    now = time.time()
    if cached and now - cached["ts"] < _FETCH_CACHE_TTL_SECONDS:
        return json.dumps(cached["payload"], ensure_ascii=False, indent=2)

    started_at = time.time()
    try:
        response = _fetch_url_with_permitted_redirects(url)
        if isinstance(response, dict):
            payload = {
                "url": url,
                "code": response["status_code"],
                "code_text": HTTPStatus(response["status_code"]).phrase,
                "result": (
                    "REDIRECT DETECTED: The URL redirects to a different host.\n"
                    f'Original URL: {response["original_url"]}\n'
                    f'Redirect URL: {response["redirect_url"]}\n'
                    "Please call web_fetch again with the redirect URL if you want that page."
                ),
                "duration_seconds": round(time.time() - started_at, 3),
                "prompt": prompt,
            }
            _FETCH_CACHE[cache_key] = {"ts": now, "payload": payload}
            return json.dumps(payload, ensure_ascii=False, indent=2)
        response.raise_for_status()
    except requests.Timeout:
        return json.dumps(
            {"url": url, "error": f"Timed out after {_FETCH_TIMEOUT_SECONDS}s while fetching the page."},
            ensure_ascii=False,
            indent=2,
        )
    except Exception as exc:
        return json.dumps({"url": url, "error": str(exc)}, ensure_ascii=False, indent=2)

    response.encoding = response.apparent_encoding or response.encoding
    title, text = _html_to_text(response.text)
    payload = {
        "url": response.url,
        "title": title or response.url,
        "code": response.status_code,
        "code_text": response.reason,
        "bytes": len(response.content),
        "content_preview": text[:4000],
        "prompt": prompt,
        "duration_seconds": round(time.time() - started_at, 3),
    }
    _FETCH_CACHE[cache_key] = {"ts": now, "payload": payload}
    return json.dumps(payload, ensure_ascii=False, indent=2)


fetch_webpage = web_fetch


async def load_12306_tools(
    env: dict[str, str] | None = None,
    cwd: str | None = None,
) -> list[Any]:
    """Start the 12306-mcp server and return the LangChain tools."""
    connection = StdioConnection(
        transport="stdio",
        command="npx",
        args=["-y", "12306-mcp"],
        cwd=cwd or os.getcwd(),
        encoding="utf-8",
        encoding_error_handler="replace",
        env=env or dict(os.environ),
    )
    return await load_mcp_tools(session=None, connection=connection)


_FILE_TOOLS: list[Any] = [list_directory, read_file, get_file_info]
_SEARCH_TOOLS: list[Any] = [web_search, web_fetch]


async def get_all_tools(
    workspace_dir: str | Path | None = None,
) -> list[Any]:
    """Return the complete tool list: local search, file ops, and 12306 tools."""
    if workspace_dir is not None:
        set_allowed_root(workspace_dir)
    tools = list(_FILE_TOOLS) + list(_SEARCH_TOOLS)
    try:
        ticket_tools = await load_12306_tools(cwd=str(workspace_dir or os.getcwd()))
        tools.extend(ticket_tools)
    except Exception as exc:
        print(f"[WARN] 12306 MCP tools unavailable: {exc}")
    return tools

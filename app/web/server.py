import asyncio
import base64
import datetime
import json
import logging
import mimetypes
import os
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from app.integrations.api_clients import (
    fanqie_api,
    fanqie_cover_url,
    lanren_api,
    netease_ting_api,
    qidian_api,
    qingting_api,
    kuwo_api,
    ximalaya_api,
    yunting_api,
    search_platform_metadata,
    ypshuo_author_by_title,
    ypshuo_author_candidates,
)
from app.core.config import CATEGORY_MAP, FFMPEG_PATH, FFPROBE_PATH, NETWORK_VERIFY_SSL, get_platform_cookies, get_platform_options, set_platform_cookies
from app.integrations.network_utils import clean_html_tags, extract_bytedance_snowflake_year, fetch_share_page_html, get_safe_session, parse_fanqie_share_html, parse_qidian_share_html
from app.processing.processor import load_process_params, load_operation_snapshot, process_audio_books, restore_operation_snapshot
from app.processing.audio_core import batch_get_audio_info, find_cover, get_audio_list, get_image_resolution
from app.processing.metadata_helpers import build_output_folder_name


APP_TITLE = "AudioMeta Nexus"
DEFAULT_PORT = 8787
RESOURCE_DIR = Path(__file__).resolve().parents[2]
CONTAINER_CONFIG_PATH = Path("/config/process_params.json")
LOCAL_CONFIG_PATH = RESOURCE_DIR / "docker/config/process_params.json"
CONTAINER_DATA_PATH = Path("/data")
LOCAL_DATA_PATH = RESOURCE_DIR / "docker/data"
ICON_PATH = RESOURCE_DIR / "icon.ico"
FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48" width="48" height="48">
  <defs>
    <linearGradient id="nexus-bg" x1="8" y1="6" x2="40" y2="44" gradientUnits="userSpaceOnUse">
      <stop stop-color="#4c8dff"/>
      <stop offset="1" stop-color="#2f64d6"/>
    </linearGradient>
  </defs>
  <rect width="48" height="48" rx="13" fill="url(#nexus-bg)"/>
  <path d="M10.5 15.2c4.4-1.8 8.6-1.1 13.5 1.9 4.9-3 9.1-3.7 13.5-1.9v18.2c-4.7-1.7-9-1-13.5 2-4.5-3-8.8-3.7-13.5-2V15.2Z" fill="none" stroke="#fff" stroke-width="2.4" stroke-linejoin="round"/>
  <path d="M24 17.2v18.1" stroke="#fff" stroke-width="2.2" stroke-linecap="round"/>
  <path d="M28.2 25.1c1.4-4.1 2.8 4.1 4.2 0s2.8 4.1 4.2 0" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
</svg>"""
DESKTOP_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
WEB_AUTH_TOKEN = os.environ.get("AUDIOMETA_WEB_TOKEN", "").strip()


def _tag_blacklist_storage_path():
    env_config = (os.environ.get("PROCESS_CONFIG") or "").strip()
    if env_config:
        return Path(env_config).parent / "tag_blacklist.txt"
    if Path("/config").is_dir():
        return Path("/config") / "tag_blacklist.txt"
    return RESOURCE_DIR / "tag_blacklist.txt"


TAG_BLACKLIST_PATH = _tag_blacklist_storage_path()


def _migrate_tag_blacklist_if_needed():
    source = RESOURCE_DIR / "tag_blacklist.txt"
    if TAG_BLACKLIST_PATH == source or not source.exists() or TAG_BLACKLIST_PATH.exists():
        return
    try:
        TAG_BLACKLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
        TAG_BLACKLIST_PATH.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    except Exception:
        pass


_migrate_tag_blacklist_if_needed()

API_SOURCES = ("喜马拉雅", "番茄畅听", "懒人听书", "起点听书", "酷我听书", "网易云听书", "云听fm", "蜻蜓fm")
LINK_PLATFORMS = ("起点听书", "番茄畅听")
TARGET_FORMATS = ("原格式保留", "MP3", "M4A", "FLAC", "OPUS")
BITRATE_OPTIONS = ("自动检测", "64k", "96k", "128k", "192k", "256k", "320k")
FINISHED_OPTIONS = ("完结", "连载")

def default_config_path():
    env_path = os.environ.get("PROCESS_CONFIG")
    if env_path:
        return Path(env_path)
    if CONTAINER_CONFIG_PATH.parent.exists():
        return CONTAINER_CONFIG_PATH
    return LOCAL_CONFIG_PATH


def default_input_folder():
    env_path = os.environ.get("INPUT_FOLDER")
    if env_path:
        return env_path
    if CONTAINER_DATA_PATH.exists():
        return str(CONTAINER_DATA_PATH)
    return str(LOCAL_DATA_PATH)


def data_root():
    root = Path(os.environ.get("DATA_ROOT") or default_input_folder())
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


DEFAULT_PARAMS = {
    "input_folder": "",
    "api_source": "喜马拉雅",
    "api_id": "",
    "link_platform": "起点听书",
    "link_url": "",
    "title": "",
    "subtitle": "",
    "author": "",
    "anchor": "",
    "category": "",
    "platform": "",
    "year": "",
    "target_format": "原格式保留",
    "bitrate": "自动检测",
    "finished": "",
    "check_codec": True,
    "rename_ext": True,
    "debug": True,
    "manual_cover_path": "",
    "manual_desc": "",
    "series_name": "",
    "series_number": "",
    "album_tags": [],
    "team": "RL",
    "fetched_metadata": {},
    "metadata_fields": {"title": True, "subtitle": True, "author": True, "anchor": True, "description": True, "series": True, "tags": True, "cover": True},
}

def normalize_params(params):
    normalized = dict(DEFAULT_PARAMS)
    normalized.update(params or {})
    if isinstance(normalized.get("album_tags"), str):
        normalized["album_tags"] = split_list(normalized["album_tags"])
    if normalized.get("bitrate") == "auto":
        normalized["bitrate"] = "自动检测"
    normalized["check_codec"] = True
    normalized["rename_ext"] = True
    normalized["debug"] = True
    normalized["dry_run"] = bool(normalized.get("dry_run", False))
    normalized["conflict_strategy"] = str(normalized.get("conflict_strategy") or "suffix")
    fields = normalized.get("metadata_fields")
    normalized["metadata_fields"] = dict(DEFAULT_PARAMS["metadata_fields"] if not isinstance(fields, dict) else fields)
    if isinstance(fields, dict): normalized["metadata_fields"].update({k: bool(v) for k, v in fields.items()})
    return normalized


def build_processing_preview(params):
    params = normalize_params(params)
    folder = Path(params.get("input_folder") or "")
    if not folder.is_dir():
        raise ValueError(f"音频目录不存在：{folder}")
    files, formats = get_audio_list(str(folder))
    format_part = params.get("target_format") if params.get("target_format") != "原格式保留" else "&".join(sorted(formats))
    bitrate_part = params.get("bitrate") or "自动检测"
    output_name = build_output_folder_name(
        params.get("title", ""), params.get("author", ""), params.get("anchor", ""),
        params.get("finished", ""), params.get("year", ""), format_part or "未知格式",
        bitrate_part, params.get("team", "RL"),
    )
    output_path = folder.parent / output_name
    normalized = {
        "input_folder": str(folder), "audio_count": len(files),
        "formats": sorted(formats), "output_name": output_name,
        "output_path": str(output_path), "output_exists": output_path.exists(),
        "conflict_strategy": params.get("conflict_strategy", "suffix"),
    }
    return normalized

def build_quality_report(params):
    params = normalize_params(params)
    folder = Path(params.get("input_folder") or "")
    if not folder.is_dir(): raise ValueError(f"音频目录不存在：{folder}")
    files, formats = get_audio_list(str(folder))
    info = batch_get_audio_info(files, logging.getLogger("audiometa-quality")) if files else {}
    issues = []
    for path, item in info.items():
        bitrate = str(item.get("bitrate", ""))
        match = re.search(r"(\d+)", bitrate)
        if match and int(match.group(1)) < 64: issues.append({"file": path, "type": "low_bitrate", "value": bitrate})
        if not item.get("duration"): issues.append({"file": path, "type": "duration_missing"})
    cover = find_cover(str(folder), params.get("api_id") or None, params.get("api_source"), logging.getLogger("audiometa-quality"), params.get("manual_cover_path", ""))
    cover_pixels = get_image_resolution(cover) if cover else 0
    if cover and not cover_pixels: issues.append({"type": "cover_invalid"})
    if cover_pixels and cover_pixels < 500 * 500: issues.append({"type": "cover_low_resolution", "pixels": cover_pixels})
    return {"audio_count": len(files), "formats": sorted(formats), "issues": issues, "cover_found": bool(cover), "cover_size": len(cover) if cover else 0, "cover_pixels": cover_pixels}


def split_list(value):
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [part.strip() for part in re.split(r"[,\n]+", str(value or "")) if part.strip()]


def first_value(data, *keys):
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return ""


def extract_year(value):
    text = str(value or "")
    if text.isdigit() and len(text) >= 10:
        try:
            return datetime.datetime.fromtimestamp(int(text[:10])).strftime("%Y")
        except Exception:
            return text[:4]
    match = re.search(r"(19|20)\d{2}", text)
    return match.group(0) if match else ""


def extract_year_string(data):
    for key in ("releaseDate", "year", "create_time", "publish_time", "update_time", "createTime", "publishTime"):
        value = data.get(key)
        if value:
            return extract_year(value)
    return ""


def split_names_text(text):
    if not text:
        return []
    return [part.strip() for part in re.split(r"[,&\s]+", str(text).strip()) if part.strip()]


def merge_unique_names(existing_names, incoming_text):
    merged = list(existing_names or [])
    for name in split_names_text(incoming_text):
        if name not in merged:
            merged.append(name)
    return merged


def load_tag_blacklist_patterns():
    patterns = []
    if not TAG_BLACKLIST_PATH.exists():
        return patterns
    try:
        for line in TAG_BLACKLIST_PATH.read_text(encoding="utf-8-sig").splitlines():
            pattern = line.strip()
            if pattern and not pattern.startswith("#"):
                patterns.append(pattern)
    except Exception:
        return []
    return patterns


def save_tag_blacklist_patterns(patterns):
    cleaned = []
    for pattern in patterns or []:
        pattern = str(pattern or "").strip()
        if pattern and pattern not in cleaned:
            cleaned.append(pattern)
    TAG_BLACKLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    TAG_BLACKLIST_PATH.write_text("\n".join(cleaned) + ("\n" if cleaned else ""), encoding="utf-8")
    return cleaned


def is_tag_blacklisted(tag, blacklist_patterns=None):
    tag_text = str(tag or "").strip()
    if not tag_text:
        return True
    for pattern in blacklist_patterns if blacklist_patterns is not None else load_tag_blacklist_patterns():
        try:
            if re.search(pattern, tag_text, re.IGNORECASE):
                return True
        except re.error:
            if pattern.lower() in tag_text.lower():
                return True
    return False


def collect_candidate_tags(data):
    tags = []
    if data.get("category"):
        tags.extend([t.strip() for t in re.split(r"[,，\s]+", str(data.get("category")).strip()) if t.strip()])
    if data.get("finished"):
        tags.append(data.get("finished"))
    tags_raw = data.get("tags") or data.get("tag_list") or []
    if isinstance(tags_raw, list):
        for item in tags_raw:
            if isinstance(item, dict):
                tags.append(str(item.get("name") or item.get("tagName") or item.get("tag_name") or "").strip())
            else:
                tags.append(str(item).strip())
    elif isinstance(tags_raw, str):
        tags.extend([t.strip() for t in re.split(r"[,，\s]+", tags_raw) if t.strip()])
    return [tag for tag in tags if tag]


def _ximalaya_dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _ximalaya_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _ximalaya_dicts(child)


def extract_ximalaya_release_year(payload):
    """Extract the album year without using episode/update timestamps."""
    if not isinstance(payload, dict):
        return ""

    preferred_keys = (
        "releaseDate", "release_date", "publishDate", "publish_date",
        "publishTime", "publish_time", "year",
    )
    created_keys = (
        "createdAt", "created_at", "createDate", "create_date",
        "createTime", "create_time", "createAt",
    )
    album_keys = {
        "albumId", "album_id", "albumTitle", "album_title", "albumName", "album_name",
    }

    containers = [payload]
    for key in ("albumPageMainInfo", "albumInfo", "album", "detail"):
        value = payload.get(key)
        if isinstance(value, dict):
            containers.append(value)
    data = payload.get("data")
    if isinstance(data, dict):
        containers.append(data)
        for key in ("albumPageMainInfo", "albumInfo", "album", "detail"):
            value = data.get(key)
            if isinstance(value, dict):
                containers.append(value)

    def first_year(mappings, keys):
        for mapping in mappings:
            for key in keys:
                year = extract_year(mapping.get(key))
                if year:
                    return year
        return ""

    # Explicit publication fields are authoritative wherever they occur.
    year = first_year(containers, preferred_keys)
    if year:
        return year

    # created_at is valid only on an album-shaped object. Do not descend into
    # last_uptrack/tracks, whose timestamps describe individual episodes.
    album_mappings = [mapping for mapping in _ximalaya_dicts(payload) if album_keys.intersection(mapping)]
    return first_year(album_mappings, created_keys)


def collect_tags_and_year_from_payload(payload):
    tags_set = set()
    release_date = ""

    def add_tag(value):
        value = str(value or "").strip()
        if 1 < len(value) <= 12 and "http" not in value:
            tags_set.add(value)

    def extract_from_dict(data):
        nonlocal release_date
        if not isinstance(data, dict):
            return
        for key in ("createDate", "createdAt", "updateDate", "publishTime", "createTime", "year", "publish_time"):
            value = data.get(key)
            if not value:
                continue
            year = extract_year(value)
            if year and (not release_date or year > release_date):
                release_date = year
        for key, value in data.items():
            if key in (
                "tagName", "labelName", "categoryName", "categoryTitle", "displayName",
                "keyword", "name", "displayTags", "albumTags", "tags", "tag_list",
                "newShowTags", "newShowTags2", "newShowTags3", "newShowTags4",
                "showTags", "showTags2", "showTags3", "showTagList", "categoryShowTags",
                "metaDataTags", "metadataValues", "albumMetaValueInfos", "relativeTags",
                "metadataValueName", "metadataName", "showName",
            ):
                if isinstance(value, str):
                    for tag in re.split(r"[,，\s|]+", value):
                        add_tag(tag)
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict):
                            add_tag(first_value(item, "tagName", "labelName", "displayName", "metadataValueName", "showName", "name", "title", "text", "value"))
                            extract_from_dict(item)
                        else:
                            add_tag(item)
                elif isinstance(value, dict):
                    add_tag(first_value(value, "tagName", "labelName", "displayName", "metadataValueName", "showName", "name", "title", "text", "value"))
                    extract_from_dict(value)
            elif isinstance(value, dict):
                extract_from_dict(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        extract_from_dict(item)

    extract_from_dict(payload)
    return list(tags_set), release_date


def collect_ximalaya_app_tags(payload):
    """Collect only tags belonging to the current album in APP detail payloads."""
    tags = []
    tag_keys = (
        "showTagList", "newShowTags", "newShowTags2", "newShowTags3", "newShowTags4",
        "showTags", "showTags2", "showTags3", "categoryShowTags", "metaDataTags",
        "metadataValues", "albumMetaValueInfos", "relativeTags",
    )
    name_keys = (
        "tagName", "labelName", "displayName", "metadataValueName", "showName",
        "name", "title", "text", "value",
    )

    def add(value):
        for part in re.split(r"[,，|]+", str(value or "")):
            part = part.strip()
            if 1 < len(part) <= 20 and "http" not in part and part not in tags:
                tags.append(part)

    def collect(value):
        if isinstance(value, str):
            add(value)
        elif isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, dict):
            name = first_value(value, *name_keys)
            if name:
                add(name)
            for key in tag_keys:
                if key in value:
                    collect(value[key])

    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    roots = [data]
    if isinstance(data, dict):
        roots.extend(
            value for value in (
                data.get("detail"),
                data.get("album"),
                data.get("albumDetailInfo"),
                (data.get("albumDetailInfo") or {}).get("albumInfo")
                if isinstance(data.get("albumDetailInfo"), dict) else None,
            )
            if isinstance(value, dict)
        )
    for root in roots:
        for key in tag_keys:
            if key in root:
                collect(root[key])
    return tags


def normalize_cover_url(data):
    cover = first_value(data, "bestCover", "cover", "pic", "thumb_url", "audio_thumb_uri", "coverUrl")
    if cover.startswith("//"):
        cover = "https:" + cover
    return cover


def resolve_category_id(category_text):
    category_text = str(category_text or "").strip()
    if not category_text:
        return ""
    if category_text in CATEGORY_MAP:
        return category_text
    reverse = {name: key for key, name in CATEGORY_MAP.items()}
    if category_text in reverse:
        return reverse[category_text]
    for key, name in CATEGORY_MAP.items():
        if category_text in name or name in category_text:
            return key
    return ""


def normalize_ximalaya_payload(raw):
    info = raw.get("albumPageMainInfo", raw or {})
    title = first_value(info, "albumTitle", "title", "name")
    subtitle = first_value(info, "customTitle", "subtitle", "shortIntro")
    anchor = first_value(info, "anchorName", "nickname")
    if not anchor and isinstance(raw.get("anchorInfo"), dict):
        anchor = first_value(raw["anchorInfo"], "anchorName", "nickname")
    payload_tags, _ = collect_tags_and_year_from_payload(raw)
    category = first_value(info, "categoryTitle", "categoryName")
    if category and category not in payload_tags:
        payload_tags.append(category)
    normalized = {
        "title": title,
        "subtitle": subtitle,
        "author": "",
        "announcer": anchor,
        "artist": anchor,
        "desc": first_value(info, "detailRichIntro", "intro"),
        "cover": first_value(info, "cover", "coverUrlLarge", "coverUrlMiddle"),
        "releaseDate": extract_ximalaya_release_year(raw),
        "category": category,
        "tags": payload_tags,
        "_ximalaya_raw": raw,
    }
    return normalized


def extract_advanced_info(album_id, api_source):
    tags_list = []
    release_date = ""
    session = get_safe_session()

    def add_tags(values):
        for tag in values or []:
            if tag and tag not in tags_list:
                tags_list.append(tag)

    def merge_payload(payload):
        nonlocal release_date
        tags, year = collect_tags_and_year_from_payload(payload)
        if api_source == "喜马拉雅":
            tags = [tag for tag in tags if tag not in {"其他", "喜马拉雅", "喜马拉雅听书", "喜马拉雅电台", "喜马拉雅好声音", "网络电台", "个人电台", "音频"}]
            year = extract_ximalaya_release_year(payload)
        add_tags(tags)
        if year and (not release_date or year > release_date):
            release_date = year

    if api_source == "喜马拉雅":
        numeric_id = re.search(r"\d+", str(album_id or ""))
        album_id = numeric_id.group(0) if numeric_id else str(album_id).strip()
        app_cookie = os.environ.get("XIMALAYA_COOKIE", "").strip()
        app_headers = {"User-Agent": "ting_10.0.0(Android,14)", "Referer": "https://mobile.ximalaya.com/"}
        if app_cookie:
            app_headers["Cookie"] = app_cookie
        urls = [
            (f"https://mobile.ximalaya.com/mobile-album/album/detail?albumId={album_id}&device=android", app_headers),
            (f"https://m.ximalaya.com/m-revision/page/album/v2/queryAlbumPage/{album_id}?albumCounts=track", {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X)"}),
        ]
        for url, headers in urls:
            try:
                response = session.get(url, headers=headers, timeout=5)
                if response.status_code == 200:
                    payload = response.json()
                    add_tags(collect_ximalaya_app_tags(payload))
                    _, year = collect_tags_and_year_from_payload(payload)
                    if year and (not release_date or year > release_date):
                        release_date = year
            except Exception:
                pass
    elif api_source == "懒人听书":
        try:
            response = session.get(
                f"https://m.lrts.me/book/{album_id}",
                headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X)"},
                timeout=5,
            )
            if response.status_code == 200:
                for match in re.findall(r'<div[^>]*class="[^"]*tag[^"]*"[^>]*>([^<]+)</div>', response.text, re.IGNORECASE):
                    tag = match.strip()
                    if 1 < len(tag) <= 12:
                        add_tags([tag])
        except Exception:
            pass
    return tags_list, release_date


def merge_advanced_fetch_data(data, adv_tags, adv_year, prefer_year=False):
    merged = dict(data or {})
    if adv_tags:
        existing_tags = merged.get("tags", [])
        if isinstance(existing_tags, str):
            existing_tags = [existing_tags]
        merged["tags"] = list(dict.fromkeys(list(existing_tags or []) + list(adv_tags)))
    if adv_year and (prefer_year or not merged.get("releaseDate")):
        merged["releaseDate"] = adv_year
    return merged


def normalize_metadata(data, platform=""):
    data = dict(data or {})
    title = first_value(data, "title", "name", "album", "book_name")
    author = first_value(data, "author", "writer")
    anchor = first_value(data, "announcer", "artist", "anchor", "reader", "narrator")
    category_text = first_value(data, "category", "category_name", "categoryName")
    desc = clean_html_tags(first_value(data, "desc", "info", "description"))
    tags = []
    blacklist_patterns = load_tag_blacklist_patterns()
    for tag in collect_candidate_tags(data):
        if not is_tag_blacklisted(tag, blacklist_patterns) and tag not in tags:
            tags.append(tag)
    return {
        "title": title,
        "subtitle": first_value(data, "subtitle"),
        "author": ", ".join(merge_unique_names([], author)),
        "anchor": ", ".join(merge_unique_names([], anchor)),
        "authors": merge_unique_names([], author),
        "anchors": merge_unique_names([], anchor),
        "desc": desc,
        "cover_url": normalize_cover_url(data),
        "year": extract_year_string(data),
        "finished": first_value(data, "finished"),
        "category": resolve_category_id(category_text),
        "category_text": category_text,
        "tags": tags,
        "platform": platform or data.get("_platform") or "",
        "raw": data,
    }
    if not normalized["author"] and normalized["title"]:
        match = ypshuo_author_by_title(normalized["title"])
        if match.get("author"):
            normalized["author"] = match["author"]
            normalized["authors"] = [match["author"]]
            normalized["raw"] = dict(normalized["raw"] or {})
            normalized["raw"]["ypshuo_author_match"] = match
    return normalized



def _merge_fanqie_search_result(raw, search_result):
    raw = dict(raw or {})
    if not isinstance(search_result, dict):
        return raw
    field_map = (
        ("title", "title"),
        ("name", "title"),
        ("album", "title"),
        ("author", "author"),
        ("announcer", "narrator"),
        ("artist", "narrator"),
        ("cover", "cover"),
        ("bestCover", "cover"),
        ("pic", "cover"),
        ("desc", "desc"),
        ("info", "desc"),
        ("category", "category"),
        ("finished", "finished"),
        ("chapter_count", "chapter_count"),
        ("releaseDate", "release_date"),
    )
    for target, source_key in field_map:
        if search_result.get(source_key):
            raw[target] = search_result[source_key]
    tags = list(raw.get("tags") or [])
    for tag in search_result.get("tags") or []:
        tag = str(tag or "").strip()
        if tag and tag not in tags:
            tags.append(tag)
    if tags:
        raw["tags"] = tags
    return raw


def fetch_api_metadata(api_source, api_id, search_result=None):
    api_source = (api_source or "").strip()
    api_id = (api_id or "").strip()
    if not api_id:
        raise ValueError("请先填写平台专辑 ID")
    if api_source in ("番茄畅听", "起点听书") and re.match(r"^https?://", api_id, re.IGNORECASE):
        return fetch_link_metadata(api_id, api_source)
    if api_source == "喜马拉雅":
        raw = ximalaya_api("album", api_id)
        data = normalize_ximalaya_payload(raw)
        # APP labels are a separate list and must be fetched even when web tags exist.
        adv_tags, adv_year = extract_advanced_info(api_id, api_source)
        data = merge_advanced_fetch_data(data, adv_tags, adv_year, prefer_year=True)
        return normalize_metadata(data, api_source)
    if api_source == "懒人听书":
        data = lanren_api(api_id)
        if not data.get("tags") or not data.get("releaseDate"):
            adv_tags, adv_year = extract_advanced_info(api_id, api_source)
            data = merge_advanced_fetch_data(data, adv_tags, adv_year)
        return normalize_metadata(data, api_source)
    if api_source == "酷我听书":
        return normalize_metadata(kuwo_api(api_id), api_source)
    if api_source == "番茄畅听":
        try:
            raw = fanqie_api(api_id)
        except Exception:
            if not search_result or not search_result.get("title"):
                raise
            raw = {}
        raw = _merge_fanqie_search_result(raw, search_result)
        return normalize_metadata(raw, api_source)
    if api_source == "起点听书":
        cookie = get_platform_cookies().get("qidian", "")
        return normalize_metadata(qidian_api(api_id, cookie_str=cookie), api_source)
    if api_source == "网易云听书":
        return normalize_metadata(netease_ting_api(api_id), api_source)
    if api_source == "云听fm":
        return normalize_metadata(yunting_api(api_id), api_source)
    if api_source == "蜻蜓fm":
        return normalize_metadata(qingting_api(api_id), api_source)
    raise ValueError(f"暂不支持的平台：{api_source}")

def fanqie_origin_from_url(url):
    parsed = urlparse((url or "").strip())
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return "https://m.changdunovel.com"


def parse_fanqie_get_info_response(data):
    if not data or data.get("code") != 0:
        return {}
    inner = data.get("data") or {}
    api_book = inner.get("api_book_info")
    if not isinstance(api_book, dict):
        return {}
    title = (api_book.get("book_name") or api_book.get("title") or "").strip()
    if not title:
        return {}
    tags_raw = api_book.get("tags") or api_book.get("tag_list") or api_book.get("labels") or ""
    tags = _fanqie_tags(tags_raw)
    creation_status = api_book.get("creation_status")
    finished = "完结" if creation_status is not None and str(creation_status) == "0" else "连载" if creation_status is not None else ""
    category = (api_book.get("category_info") or api_book.get("genre") or "").strip()
    if not category and tags:
        category = tags[0]
    return {
        "title": title,
        "name": title,
        "author": (api_book.get("author") or "").strip(),
        "cover": _fanqie_cover(api_book),
        "desc": (api_book.get("abstract") or "").strip(),
        "category": category,
        "finished": finished,
        "tags": tags,
        "releaseDate": _fanqie_release_year(api_book),
    }


def parse_fanqie_audio_detail_response(data):
    if not data or data.get("code") != 0:
        return {}
    inner = data.get("data") or {}
    if not isinstance(inner, dict):
        return {}
    title = (inner.get("book_name") or inner.get("original_book_name") or "").strip()
    if not title:
        return {}
    tags_raw = inner.get("tags") or inner.get("pure_category_tags") or inner.get("tag_list") or inner.get("labels") or ""
    tags = _fanqie_tags(tags_raw)
    category = (inner.get("category") or "").strip()
    if not category and tags:
        category = tags[0]
    creation_status = inner.get("creation_status")
    finished = "完结" if creation_status is not None and str(creation_status) == "0" else "连载" if creation_status is not None else ""
    return {
        "title": title,
        "name": title,
        "author": (inner.get("author") or "").strip(),
        "cover": _fanqie_cover(inner),
        "desc": (inner.get("abstract") or inner.get("book_abstract_v2") or "").strip(),
        "category": category,
        "finished": finished,
        "tags": tags,
        "releaseDate": _fanqie_release_year(inner),
    }


def _fanqie_tags(value):
    if isinstance(value, str):
        values = re.split(r"[,，、|\\n]", value)
    elif isinstance(value, (list, tuple)):
        values = value
    else:
        values = []
    result = []
    for item in values:
        if isinstance(item, dict):
            item = item.get("name") or item.get("tag_name") or item.get("tagName") or item.get("value") or ""
        item = str(item).strip()
        if item and item not in result:
            result.append(item)
    return result


def _fanqie_cover(data):
    return fanqie_cover_url(data)


def _fanqie_release_year(data):
    for key in ("publish_time", "published_time", "first_publish_time", "release_time", "create_time", "created_at", "update_time"):
        value = data.get(key)
        if value in (None, "", 0, "0"):
            continue
        text = str(value)
        match = re.search(r"(19|20)\\d{2}", text)
        if match:
            return match.group(0)
        try:
            timestamp = float(value)
            if timestamp > 1000000000000:
                timestamp /= 1000
            if 0 < timestamp < 4102444800:
                return time.strftime("%Y", time.localtime(timestamp))
        except (TypeError, ValueError, OverflowError):
            pass
    return ""


def extract_fanqie_api_urls(html):
    html = (html or "").replace("\\/", "/").replace("&amp;", "&")
    urls = []
    for match in re.findall(r'https?://[^"\'<>\s]+(?:share/audio/detail|audio/detail/v1|get_info)[^"\'<>\s]*', html):
        url = match
        url = unquote(url)
        if url not in urls:
            urls.append(url)
    return urls


def fetch_fanqie_api_metadata_from_share_html(html, share_url):
    session = get_safe_session()
    origin = fanqie_origin_from_url(share_url)
    headers = {
        "Accept": "application/json",
        "Referer": origin + "/",
        "Origin": origin,
        "User-Agent": DESKTOP_UA,
    }
    for api_url in extract_fanqie_api_urls(html):
        try:
            response = session.get(api_url, headers=headers, timeout=10)
            if response.status_code != 200:
                continue
            payload = response.json()
            parsed = parse_fanqie_audio_detail_response(payload) if "audio/detail" in api_url else parse_fanqie_get_info_response(payload)
            if parsed:
                return parsed
        except Exception:
            continue
    return {}


def merge_fanqie_metadata(base, extra):
    """Merge partial metadata without letting an empty fallback erase valid fields."""
    result = dict(base or {})
    for key, value in (extra or {}).items():
        if value not in (None, "", [], {}):
            if key == "tags":
                result[key] = list(dict.fromkeys((result.get(key) or []) + list(value)))
            elif result.get(key) in (None, "", [], {}):
                result[key] = value
    return result


def fanqie_metadata_incomplete(data):
    data = data or {}
    return any(not data.get(key) for key in ("title", "author", "cover", "desc", "tags", "finished"))


def run_async_task(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def fetch_fanqie_rendered_metadata_async(share_url):
    try:
        from playwright.async_api import async_playwright
    except Exception as exc:
        raise RuntimeError("Docker 镜像缺少 Playwright/Chromium，无法渲染番茄畅听分享页") from exc

    origin = fanqie_origin_from_url(share_url)
    js_get_api_url = r"""
    (function(){
      try {
        var list = performance.getEntriesByType('resource') || [];
        var audioDetail = '', getInfo = '';
        for (var i = list.length - 1; i >= 0; i--) {
          var url = (list[i].name || list[i].url || '') + '';
          if (url.indexOf('share/audio/detail') !== -1 || url.indexOf('audio/detail/v1') !== -1) audioDetail = url;
          if (url.indexOf('get_info') !== -1) getInfo = url;
        }
        return audioDetail || getInfo || '';
      } catch(e) { return ''; }
    })();
    """
    js_get_dom_cover = r"""
    (function(){
      var cover = '';
      var imgEl = document.querySelector('.book-meta-new-img');
      if (imgEl && imgEl.src) cover = imgEl.src;
      if (!cover) {
        var og = document.querySelector('meta[name="og:image"], meta[property="og:image"]');
        if (og && og.getAttribute('content')) cover = og.getAttribute('content');
      }
      return cover;
    })();
    """
    js_click_expand = r"""
    (function(){
      var els = document.querySelectorAll('span, div, a, p');
      for (var i = 0; i < els.length; i++) {
        var txt = (els[i].textContent || '').trim();
        if (txt === '\u5c55\u5f00' || txt === '\u5c55\u5f00\u5168\u90e8') {
          try { els[i].dispatchEvent(new TouchEvent('touchstart', {bubbles: true})); } catch(e) {}
          try { els[i].dispatchEvent(new TouchEvent('touchend', {bubbles: true})); } catch(e) {}
          try { els[i].click(); } catch(e) {}
        }
      }
    })();
    """
    js_fallback = r"""
    (function(){
      var title = '', author = '', cover = '', desc = '', category = '', finished = '', tags = [], releaseDate = '';
      var titleEl = document.querySelector('.book-meta-new-info-title');
      if (titleEl) title = (titleEl.innerText || titleEl.textContent || '').trim();
      var authorEl = document.querySelector('.book-meta-new-info-desc-author');
      if (authorEl) author = (authorEl.innerText || authorEl.textContent || '').trim();
      if (!author) {
        var authorAlt = document.querySelector('[class*="author"], [class*="Author"]');
        if (authorAlt) author = (authorAlt.innerText || authorAlt.textContent || '').trim();
      }
      var imgEl = document.querySelector('.book-meta-new-img');
      if (imgEl && imgEl.src) cover = imgEl.src;

      var descEl = document.querySelector('.book-introduction-desc') || document.querySelector('.text-expand.book-introduction-desc');
      if (descEl) {
        desc = (descEl.innerText || descEl.textContent || '').trim();
        desc = desc.replace(/\u5c55\u5f00\u5168\u90e8$/, '').replace(/\u5c55\u5f00$/, '').replace(/\u6536\u8d77$/, '').trim();
      }

      try {
        var scripts = document.querySelectorAll('script');
        var regex = /"(?:abstract|description|intro|content)"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"/g;
        for (var i = 0; i < scripts.length; i++) {
          var txt = scripts[i].innerHTML || '';
          var match;
          while ((match = regex.exec(txt)) !== null) {
            var val = match[1];
            val = val.replace(/\\u([0-9a-fA-F]{4})/g, function(m, g) { return String.fromCharCode(parseInt(g, 16)); });
            val = val.replace(/\\n/g, '\n').replace(/\\r/g, '').replace(/\\"/g, '"').replace(/\\\\/g, '\\');
            var shortPrefix = desc.substring(0, 8).replace(/\s/g, '');
            if (shortPrefix.length > 0 && val.replace(/\s/g, '').indexOf(shortPrefix) !== -1 && val.length > desc.length) {
              desc = val.trim();
            }
          }
        }
      } catch(e) {}

      var descLabels = document.querySelectorAll('.book-meta-new-info-item-desc');
      for (var i = 0; i < descLabels.length; i++) {
        if ((descLabels[i].textContent || '').trim() === '\u66f4\u65b0\u72b6\u6001') {
          var parent = descLabels[i].parentElement;
          if (parent) {
            var textEl = parent.querySelector('.book-meta-new-info-item-text');
            if (textEl) finished = (textEl.textContent || '').trim();
          }
          break;
        }
      }
      var tagEls = document.querySelectorAll('.book-introduction-title-tag-text');
      if (tagEls.length) tags = [].map.call(tagEls, function(n){ return (n.textContent || '').trim(); }).filter(Boolean);
      if (!tags.length) {
        var tagAlt = document.querySelectorAll('[class*="tag"], [class*="Tag"], [class*="label"], [class*="Label"]');
        tags = [].map.call(tagAlt, function(n){ return (n.textContent || '').trim(); }).filter(function(v){ return v && v.length < 30; }).slice(0, 20);
      }
      if (tags.length) category = tags[0];

      var allText = document.body ? (document.body.innerText || '') : '';
      var yearMatch = allText.match(/(?:出版|发布|上线|创建)[^\d]{0,8}((?:19|20)\d{2})/);
      if (yearMatch) releaseDate = yearMatch[1];

      if (!title) { var ogTitle = document.querySelector('meta[name="og:title"], meta[property="og:title"]'); if (ogTitle && ogTitle.getAttribute('content')) title = ogTitle.getAttribute('content'); }
      if (!cover) { var ogImage = document.querySelector('meta[name="og:image"], meta[property="og:image"]'); if (ogImage && ogImage.getAttribute('content')) cover = ogImage.getAttribute('content'); }
      if (title === '\u756a\u8304\u7545\u542c') title = '';
      return { title: title, author: author, cover: cover, desc: desc, category: category, finished: finished, tags: tags, releaseDate: releaseDate };
    })();
    """

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            page = await browser.new_page(user_agent=DESKTOP_UA, viewport={"width": 900, "height": 700})
            captured_responses = []
            page.on("response", lambda response: captured_responses.append(response) if any(token in response.url for token in ("share/audio/detail", "audio/detail/v1", "share/get_info", "playerapi/share")) else None)
            await page.goto(share_url, wait_until="domcontentloaded", timeout=30000)
            dom_cover = ""
            captured = ""
            for _ in range(10):
                if not dom_cover:
                    dom_cover = await page.evaluate(js_get_dom_cover)
                if not captured:
                    captured = await page.evaluate(js_get_api_url)
                if captured:
                    break
                await page.wait_for_timeout(1000)

            # 优先解析浏览器实际收到的 JSON 响应，避免重新请求时因签名、Cookie 或设备参数失效而只剩标题。
            for response in reversed(captured_responses):
                try:
                    payload = await response.json()
                    response_url = response.url
                    parsed = parse_fanqie_audio_detail_response(payload) if "audio/detail" in response_url else parse_fanqie_get_info_response(payload)
                    if parsed:
                        if dom_cover and not parsed.get("cover"):
                            parsed["cover"] = dom_cover
                        return parsed
                except Exception:
                    continue

            if captured and isinstance(captured, str):
                headers = {
                    "Accept": "application/json",
                    "Referer": origin + "/",
                    "Origin": origin,
                    "User-Agent": DESKTOP_UA,
                }
                response = get_safe_session().get(captured, timeout=12, headers=headers)
                if response.status_code == 200:
                    payload = response.json()
                    parsed = parse_fanqie_audio_detail_response(payload) if "audio/detail" in captured else parse_fanqie_get_info_response(payload)
                    if parsed:
                        if dom_cover and isinstance(dom_cover, str):
                            parsed["cover"] = dom_cover
                        return parsed

            await page.evaluate(js_click_expand)
            await page.wait_for_timeout(1200)
            fallback = await page.evaluate(js_fallback)
            if fallback and isinstance(fallback, dict):
                if dom_cover and not fallback.get("cover"):
                    fallback["cover"] = dom_cover
                return fallback
        finally:
            await browser.close()
    return {}


def fetch_fanqie_rendered_metadata(share_url):
    return run_async_task(fetch_fanqie_rendered_metadata_async(share_url))


def fetch_link_metadata(url, platform):
    url = (url or "").strip()
    platform = (platform or "起点听书").strip()
    if not url:
        raise ValueError("请先填写分享链接")
    html = ""
    data = {}
    try:
        html = fetch_share_page_html(url, timeout=15)
    except Exception as exc:
        _debug_log(f"[番茄分享页] 普通请求失败，转浏览器渲染: {exc}")
    if platform == "起点听书":
        data = parse_qidian_share_html(html, url) if html else {}
    else:
        if html:
            data = parse_fanqie_share_html(html, url)
            static_api_data = fetch_fanqie_api_metadata_from_share_html(html, url)
            data = merge_fanqie_metadata(data, static_api_data)
        # A title-only SSR result is not success. Render the page to fill all remaining fields.
        if fanqie_metadata_incomplete(data):
            rendered_data = fetch_fanqie_rendered_metadata(url)
            data = merge_fanqie_metadata(data, rendered_data)
        if data and not data.get("title"):
            data = {}
    if not data:
        if platform == "番茄畅听":
            raise ValueError("未能从番茄链接解析到专辑信息。番茄分享页经常需要浏览器渲染，请确认链接有效后重试。")
        raise ValueError("未能从链接中解析到专辑信息")
    if platform == "番茄畅听" and not data.get("releaseDate"):
        query = parse_qs(urlparse(url).query)
        book_id = (query.get("book_id") or query.get("bookId") or [""])[0]
        data["releaseDate"] = extract_bytedance_snowflake_year(book_id)
    if platform == "番茄畅听" and data.get("cover"):
        high_res_cover = fanqie_cover_url({"audio_thumb_url_hd": data.get("cover"), "thumb_url": data.get("cover")})
        if high_res_cover:
            data["cover"] = data["bestCover"] = data["pic"] = high_res_cover
    data["_platform"] = platform
    return normalize_metadata(data, platform)

def load_params():
    path = default_config_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        return save_params(DEFAULT_PARAMS)
    with path.open("r", encoding="utf-8-sig") as f:
        loaded = json.load(f)
    return normalize_params(loaded)


def save_params(params):
    path = default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = normalize_params(params)
    with path.open("w", encoding="utf-8") as f:
        json.dump(normalized, f, ensure_ascii=False, indent=2)
    return normalized


def validate_params(params):
    required = {
        "input_folder": "音频目录",
        "title": "专辑标题",
        "author": "原著作者",
        "anchor": "演播艺术家",
        "category": "专辑分类",
        "platform": "发布平台",
        "year": "发布年份",
        "finished": "专辑状态",
    }
    missing = [label for key, label in required.items() if not str(params.get(key, "")).strip()]
    if missing:
        raise ValueError("请补全：" + "、".join(missing))
    if not Path(params["input_folder"]).is_dir():
        raise ValueError(f"音频目录不存在：{params['input_folder']}")


class WebLogHandler(logging.Handler):
    def __init__(self, state):
        super().__init__()
        self.state = state

    def emit(self, record):
        self.state.add_log(self.format(record), record.levelname.lower())


class ProgressBridge:
    def __init__(self, state):
        self.state = state
        self.last_log = ""

    def __call__(self, percent, message):
        try:
            value = max(0, min(100, float(percent)))
        except Exception:
            value = 0
        text = message or ""
        self.state.set_progress(value, text)
        if text:
            log_text = f"⏳ {int(round(value))}% · {text}"
            if log_text != self.last_log:
                self.state.add_log(log_text, "info")
                self.last_log = log_text


class AppState:
    def __init__(self):
        self.lock = threading.RLock()
        self.logs = []
        self.log_seq = 0
        self.log_epoch = 0
        self.progress = 0
        self.message = "等待就绪"
        self.running = False
        self.started_at = ""
        self.finished_at = ""
        self.result = None
        self.error = ""
        self.thread = None
        self.stop_event = threading.Event()
        self.failed_items = []
        self.queue = []
        self.current_task_id = ""

    def add_log(self, message, level="info"):
        with self.lock:
            text = str(message)
            if len(text) > 8000:
                text = text[:8000] + "…（日志内容过长，已截断）"
            self.log_seq += 1
            self.logs.append({"id": self.log_seq, "level": level, "message": text})
            self.logs = self.logs[-2000:]

    def set_progress(self, progress, message):
        with self.lock:
            self.progress = progress
            if message:
                self.message = message

    def snapshot(self, logs_after=None, log_epoch=None, include_logs=True):
        with self.lock:
            logs_reset = False
            if not include_logs:
                logs = []
            elif logs_after is None:
                logs = list(self.logs)
                logs_reset = True
            else:
                first_log_id = self.logs[0]["id"] if self.logs else self.log_seq + 1
                logs_reset = (
                    log_epoch != self.log_epoch
                    or logs_after > self.log_seq
                    or logs_after < first_log_id - 1
                )
                logs = list(self.logs) if logs_reset else [item for item in self.logs if item["id"] > logs_after]
            return {
                "running": self.running,
                "progress": self.progress,
                "message": self.message,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "result": self.result,
                "error": self.error,
                "logs": logs,
                "log_seq": self.log_seq,
                "log_epoch": self.log_epoch,
                "logs_reset": logs_reset,
                "failed_items": list(self.failed_items),
                "queue": list(self.queue),
                "current_task_id": self.current_task_id,
            }

    def reset_for_run(self, clear_logs=True):
        with self.lock:
            if clear_logs:
                self.logs = []
                self.log_epoch += 1
            self.progress = 0
            self.message = "׼ʼ"
            self.running = True
            self.started_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.finished_at = ""
            self.result = None
            self.error = ""
            self.failed_items = []
            self.stop_event = threading.Event()


STATE = AppState()


def build_logger():
    logger = logging.getLogger("audiometa-nexus-web")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    handler = WebLogHandler(STATE)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    return logger


def run_one(params, task_id=""):
    logger = build_logger()

    def record_failed(file_path, error_msg):
        with STATE.lock:
            STATE.failed_items.append({"file": file_path, "error": error_msg})
        STATE.add_log(f"ʧܣ{file_path} | {error_msg}", "error")

    try:
        params = normalize_params(params)
        validate_params(params)
        if params.get("dry_run"):
            preview = build_processing_preview(params)
            with STATE.lock:
                STATE.result = {"dry_run": True, "preview": preview}
                STATE.progress = 100
                STATE.message = "模拟预览完成"
            STATE.add_log(f"模拟运行：发现 {preview['audio_count']} 个音频文件，输出目录：{preview['output_name']}", "info")
            return STATE.result
        STATE.add_log(f"{params.get('title') or Path(params['input_folder']).name}", "info")
        result = process_audio_books(
            params,
            logger,
            progress_callback=ProgressBridge(STATE),
            failed_audios_callback=record_failed,
            stop_event=STATE.stop_event,
        )
        with STATE.lock:
            STATE.result = result
            if result and result.get("error"):
                STATE.error = result.get("error", "")
                STATE.message = "处理失败"
            elif STATE.stop_event.is_set():
                STATE.message = "已停止"
            else:
                STATE.progress = 100
                STATE.message = "处理完成"
        return result or {}
    except Exception as exc:
        with STATE.lock:
            STATE.error = str(exc)
            STATE.message = "处理失败"
        STATE.add_log(f"处理失败：{exc}", "error")
        return {"error": str(exc)}


def run_single_task(params):
    STATE.reset_for_run(clear_logs=True)
    try:
        run_one(params)
    finally:
        with STATE.lock:
            STATE.running = False
            STATE.current_task_id = ""
            STATE.finished_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def run_queue():
    STATE.reset_for_run(clear_logs=True)
    try:
        while True:
            with STATE.lock:
                if STATE.stop_event.is_set():
                    break
                next_task = next((item for item in STATE.queue if item["status"] in ("pending", "failed")), None)
                if not next_task:
                    break
                next_task["status"] = "processing"
                next_task["error"] = ""
                STATE.current_task_id = next_task["id"]
                STATE.progress = 0
                STATE.message = "正在处理队列任务"
            result = run_one(next_task["params"], next_task["id"])
            with STATE.lock:
                if STATE.stop_event.is_set():
                    next_task["status"] = "stopped"
                    break
                if result and result.get("error"):
                    next_task["status"] = "failed"
                    next_task["error"] = result.get("error", "处理失败")
                else:
                    next_task["status"] = "done"
        with STATE.lock:
            if STATE.stop_event.is_set():
                STATE.message = "已停止"
            elif not STATE.error:
                STATE.message = "队列处理完成"
                STATE.progress = 100
    finally:
        with STATE.lock:
            STATE.running = False
            STATE.current_task_id = ""
            STATE.finished_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def list_directories(path_text):
    root = data_root()
    current = Path(path_text or root).resolve()
    if os.path.commonpath([str(current), str(root)]) != str(root):
        current = root
    if not current.exists() or not current.is_dir():
        current = root
    dirs = []
    for item in sorted(current.iterdir(), key=lambda p: p.name.lower()):
        if item.is_dir() and not item.name.startswith("."):
            has_audio = any(child.suffix.lower() in {".mp3", ".m4a", ".flac", ".ogg", ".wav", ".aac", ".alac", ".wma"} for child in item.iterdir() if child.is_file())
            dirs.append({"name": item.name, "path": str(item), "has_audio": has_audio})
    parent = str(current.parent) if current != root and os.path.commonpath([str(current.parent), str(root)]) == str(root) else ""
    return {"root": str(root), "current": str(current), "parent": parent, "dirs": dirs}


def read_folder_desc(folder):
    desc_path = Path(folder) / "desc.txt"
    if not desc_path.exists():
        return None
    return desc_path.read_text(encoding="utf-8-sig")


def find_folder_cover(folder):
    for name in ("cover.jpg", "cover.png", "封面.jpg", "封面.png", "cover.jpeg", "cover.webp"):
        candidate = Path(folder) / name
        if candidate.exists() and candidate.is_file():
            return str(candidate)
    return ""


def resolve_cover_for_folder(folder, cover_path):
    cover_text = str(cover_path or "").strip()
    if cover_text.startswith(("http://", "https://")):
        return cover_text
    folder_path = Path(folder or "")
    if cover_text:
        candidate = Path(cover_text)
        if candidate.exists() and candidate.is_file():
            return str(candidate)
        basename = Path(cover_text.replace("\\", "/")).name
        if basename:
            local_candidate = folder_path / basename
            if local_candidate.exists() and local_candidate.is_file():
                return str(local_candidate)
    return find_folder_cover(folder_path)


def load_folder_config(folder):
    folder_path = Path(folder or "").resolve()
    if not folder_path.exists() or not folder_path.is_dir():
        raise ValueError("请选择有效的音频目录")
    params = load_process_params(str(folder_path))
    if not params:
        return {"found": False, "params": normalize_params({"input_folder": str(folder_path)}), "message": "未找到 process_params.json"}
    params = normalize_params(params)
    params["input_folder"] = str(folder_path)
    desc = read_folder_desc(folder_path)
    if desc is not None:
        params["manual_desc"] = desc
    elif params.get("clean_desc") and not params.get("manual_desc"):
        params["manual_desc"] = params.get("clean_desc", "")
    cover = resolve_cover_for_folder(folder_path, params.get("manual_cover_path"))
    if cover:
        params["manual_cover_path"] = cover
    return {"found": True, "params": params, "message": "已加载专辑目录配置"}


def json_response(handler, payload, status=200):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
    handler.send_header("Pragma", "no-cache")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def text_response(handler, body, content_type="text/html; charset=utf-8", status=200):
    data = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def file_response(handler, path):
    file_path = Path(path or "").resolve()
    if not file_path.exists() or not file_path.is_file():
        return text_response(handler, "Not Found", "text/plain; charset=utf-8", 404)
    allowed_roots = [data_root(), default_config_path().parent.resolve()]
    if not any(os.path.commonpath([str(file_path), str(root)]) == str(root) for root in allowed_roots):
        return text_response(handler, "Forbidden", "text/plain; charset=utf-8", 403)
    content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
    data = file_path.read_bytes()
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def remote_cover_response(handler, url):
    target = str(url or "").strip()
    if not target.startswith(("https://bookcover.yuewen.com/", "https://img2.kuwo.cn/", "https://img3.kuwo.cn/")):
        return text_response(handler, "Forbidden", "text/plain; charset=utf-8", 403)
    try:
        response = get_safe_session().get(target, headers={"Referer": "https://qidian.com/", "User-Agent": "Mozilla/5.0"}, timeout=15)
        if response.status_code != 200 or not response.content:
            return text_response(handler, "Not Found", "text/plain; charset=utf-8", 404)
        handler.send_response(200)
        handler.send_header("Content-Type", response.headers.get("Content-Type", "image/jpeg"))
        handler.send_header("Cache-Control", "public, max-age=3600")
        handler.send_header("Content-Length", str(len(response.content)))
        handler.end_headers()
        handler.wfile.write(response.content)
    except Exception:
        return text_response(handler, "Not Found", "text/plain; charset=utf-8", 404)


def authorized(handler):
    if not WEB_AUTH_TOKEN:
        return True
    supplied = handler.headers.get("X-Audiometa-Token", "")
    return supplied == WEB_AUTH_TOKEN


def favicon_response(handler):
    if not ICON_PATH.exists():
        return text_response(handler, "Not Found", "text/plain; charset=utf-8", 404)
    data = ICON_PATH.read_bytes()
    handler.send_response(200)
    handler.send_header("Content-Type", "image/x-icon")
    handler.send_header("Cache-Control", "public, max-age=86400")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def read_json_body(handler):
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length <= 0:
        return {}
    return json.loads(handler.rfile.read(length).decode("utf-8"))


def save_uploaded_cover(payload):
    data_url = str(payload.get("data") or "")
    match = re.fullmatch(r"data:(image/(?:jpeg|png|webp|gif));base64,([A-Za-z0-9+/=]+)", data_url, re.IGNORECASE)
    if not match:
        raise ValueError("仅支持 JPG、PNG、WEBP 或 GIF 封面图片")
    raw = base64.b64decode(match.group(2), validate=True)
    if not raw or len(raw) > 12 * 1024 * 1024:
        raise ValueError("封面图片不能为空且不能超过 12 MB")
    extension = {"jpeg": ".jpg", "png": ".png", "webp": ".webp", "gif": ".gif"}[match.group(1).split("/")[-1].lower()]
    upload_dir = default_config_path().parent / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    target = upload_dir / f"audiometa-cover-{uuid.uuid4().hex}{extension}"
    target.write_bytes(raw)
    return str(target)


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "AudioMetaNexus/2.0"

    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        path = urlparse(self.path).path
        query = parse_qs(urlparse(self.path).query)
        if not authorized(self):
            return json_response(self, {"ok": False, "error": "未授权"}, 401)
        try:
            if path == "/":
                return text_response(self, INDEX_HTML)
            if path == "/favicon.svg":
                return text_response(self, FAVICON_SVG, "image/svg+xml; charset=utf-8")
            if path == "/favicon.ico":
                return favicon_response(self)
            if path == "/api/config":
                params = load_params()
                folder = params.get("input_folder")
                if folder and Path(folder).exists():
                    cover = resolve_cover_for_folder(folder, params.get("manual_cover_path"))
                    if cover and not params.get("manual_cover_path"):
                        params["manual_cover_path"] = cover
                return json_response(self, {"ok": True, "params": params, "config_path": str(default_config_path())})
            if path == "/api/config/export":
                return json_response(self, {"ok": True, "params": load_params(), "exported_at": datetime.datetime.now().isoformat()})
            if path == "/api/status":
                try:
                    logs_after = int((query.get("logs_after") or [""])[0])
                    client_log_epoch = int((query.get("log_epoch") or [""])[0])
                except (TypeError, ValueError):
                    logs_after = None
                    client_log_epoch = None
                return json_response(self, {
                    "ok": True,
                    "status": STATE.snapshot(logs_after=logs_after, log_epoch=client_log_epoch),
                })
            if path == "/api/health":
                return json_response(self, {"ok": True, "health": {
                    "ffmpeg": Path(FFMPEG_PATH).exists(),
                    "ffprobe": Path(FFPROBE_PATH).exists(),
                    "ssl_verify": NETWORK_VERIFY_SSL,
                    "config_path": str(default_config_path()),
                    "data_root": str(data_root()),
                }})
            if path == "/api/snapshot":
                folder = (query.get("path") or [""])[0]
                return json_response(self, {"ok": True, "snapshot": load_operation_snapshot(folder)})
            if path == "/api/options":
                return json_response(self, {"ok": True, "options": {
                    "api_sources": API_SOURCES,
                    "link_platforms": LINK_PLATFORMS,
                    "platforms": get_platform_options(),
                    "categories": [{"id": k, "name": v} for k, v in CATEGORY_MAP.items()],
                    "target_formats": TARGET_FORMATS,
                    "bitrates": BITRATE_OPTIONS,
                    "finished": FINISHED_OPTIONS,
                    "data_root": str(data_root()),
                }})
            if path == "/api/browse":
                return json_response(self, {"ok": True, "browser": list_directories((query.get("path") or [""])[0])})
            if path == "/api/folder-config":
                return json_response(self, {"ok": True, **load_folder_config((query.get("path") or [""])[0])})
            if path == "/api/cover":
                return file_response(self, (query.get("path") or [""])[0])
            if path == "/api/remote-cover":
                return remote_cover_response(self, (query.get("url") or [""])[0])
            if path == "/api/cookies":
                return json_response(self, {"ok": True, "cookies": get_platform_cookies()})
            if path == "/api/tag-blacklist":
                return json_response(self, {"ok": True, "patterns": load_tag_blacklist_patterns(), "path": str(TAG_BLACKLIST_PATH)})
        except Exception as exc:
            return json_response(self, {"ok": False, "error": str(exc)}, 500)
        return text_response(self, "Not Found", "text/plain; charset=utf-8", 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if not authorized(self):
            return json_response(self, {"ok": False, "error": "未授权"}, 401)
        try:
            payload = read_json_body(self)
            if path == "/api/config":
                return json_response(self, {"ok": True, "params": save_params(payload.get("params", payload))})
            if path == "/api/cover/upload":
                return json_response(self, {"ok": True, "path": save_uploaded_cover(payload)})
            if path == "/api/config/import":
                imported = payload.get("params", payload)
                if not isinstance(imported, dict):
                    raise ValueError("配置数据格式无效")
                return json_response(self, {"ok": True, "params": save_params(imported)})
            if path == "/api/fetch-metadata":
                meta = fetch_api_metadata(payload.get("api_source"), payload.get("api_id"), payload.get("search_result"))
                return json_response(self, {"ok": True, "metadata": meta})
            if path == "/api/search-metadata":
                page = max(1, int(payload.get("page") or 1))
                results, has_next = search_platform_metadata(payload.get("api_source"), payload.get("keyword"), page=page)
                return json_response(self, {"ok": True, "results": results, "page": page, "has_next": has_next})
            if path == "/api/search-author":
                title = str(payload.get("title") or "").strip()
                if not title:
                    raise ValueError("请先填写专辑标题")
                return json_response(self, {"ok": True, "results": ypshuo_author_candidates(title)})
            if path == "/api/fetch-link":
                meta = fetch_link_metadata(payload.get("url"), payload.get("platform"))
                return json_response(self, {"ok": True, "metadata": meta})
            if path == "/api/folder-config":
                return json_response(self, {"ok": True, **load_folder_config(payload.get("path"))})
            if path == "/api/preview":
                return json_response(self, {"ok": True, "preview": build_processing_preview(payload.get("params", payload))})
            if path == "/api/quality-check":
                return json_response(self, {"ok": True, "report": build_quality_report(payload.get("params", payload))})
            if path == "/api/snapshot/restore":
                folder = str(payload.get("path") or "").strip()
                if not folder:
                    raise ValueError("缺少待恢复目录")
                return json_response(self, {"ok": True, "result": restore_operation_snapshot(folder)})
            if path == "/api/cookies":
                if not set_platform_cookies(payload.get("cookies", payload)):
                    raise RuntimeError("Cookie 保存失败")
                return json_response(self, {"ok": True, "cookies": get_platform_cookies()})
            if path == "/api/tag-blacklist":
                patterns = save_tag_blacklist_patterns(payload.get("patterns", []))
                return json_response(self, {"ok": True, "patterns": patterns, "path": str(TAG_BLACKLIST_PATH)})
            if path == "/api/run":
                with STATE.lock:
                    if STATE.running:
                        return json_response(self, {"ok": False, "error": "任务正在运行"}, 409)
                params = normalize_params(payload.get("params", payload) or load_params())
                thread = threading.Thread(target=run_single_task, args=(params,), daemon=True)
                with STATE.lock:
                    STATE.thread = thread
                thread.start()
                return json_response(self, {"ok": True})
            if path == "/api/stop":
                with STATE.lock:
                    if STATE.running:
                        STATE.stop_event.set()
                        STATE.message = "正在停止"
                        STATE.add_log("⏹️ 已发送停止请求，等待当前步骤收尾...", "warning")
                    for item in STATE.queue:
                        if item["status"] == "pending":
                            item["status"] = "stopped"
                return json_response(self, {"ok": True})
            if path == "/api/queue/add":
                params = normalize_params(payload.get("params", payload))
                item = {
                    "id": uuid.uuid4().hex[:12],
                    "title": params.get("title") or Path(params.get("input_folder", "")).name or "未命名",
                    "author": params.get("author", ""),
                    "anchor": params.get("anchor", ""),
                    "source": params.get("input_folder", ""),
                    "status": "pending",
                    "error": "",
                    "params": params,
                }
                with STATE.lock:
                    STATE.queue.append(item)
                return json_response(self, {"ok": True, "item": item, "status": STATE.snapshot()})
            if path == "/api/queue/add-batch":
                base_params = normalize_params(payload.get("params", {}))
                paths = [str(p).strip() for p in (payload.get("paths") or []) if str(p).strip()]
                if not paths:
                    raise ValueError("未提供批量目录")
                added = []
                with STATE.lock:
                    for folder in paths:
                        params = dict(base_params)
                        params["input_folder"] = folder
                        item = {
                            "id": uuid.uuid4().hex[:12], "title": params.get("title") or Path(folder).name,
                            "author": params.get("author", ""), "anchor": params.get("anchor", ""),
                            "source": folder, "status": "pending", "error": "", "params": params,
                        }
                        STATE.queue.append(item); added.append(item)
                return json_response(self, {"ok": True, "items": added, "status": STATE.snapshot()})
            if path == "/api/queue/update":
                task_id = str(payload.get("id") or "").strip()
                params = normalize_params(payload.get("params", {}))
                if not task_id:
                    return json_response(self, {"ok": False, "error": "缺少任务 ID"}, 400)
                updated = None
                with STATE.lock:
                    for item in STATE.queue:
                        if item["id"] != task_id:
                            continue
                        if item["status"] == "processing":
                            return json_response(self, {"ok": False, "error": "处理中任务不可编辑"}, 409)
                        item.update({
                            "title": params.get("title") or Path(params.get("input_folder", "")).name or "未命名",
                            "author": params.get("author", ""),
                            "anchor": params.get("anchor", ""),
                            "source": params.get("input_folder", ""),
                            "status": "pending",
                            "error": "",
                            "params": params,
                        })
                        updated = item
                        break
                if not updated:
                    return json_response(self, {"ok": False, "error": "未找到选中的任务"}, 404)
                return json_response(self, {"ok": True, "item": updated, "status": STATE.snapshot()})
            if path == "/api/queue/remove":
                ids = set(payload.get("ids") or [])
                with STATE.lock:
                    STATE.queue = [item for item in STATE.queue if item["id"] not in ids or item["status"] == "processing"]
                return json_response(self, {"ok": True, "status": STATE.snapshot()})
            if path == "/api/queue/clear":
                with STATE.lock:
                    STATE.queue = [item for item in STATE.queue if item["status"] == "processing"]
                return json_response(self, {"ok": True, "status": STATE.snapshot()})
            if path == "/api/queue/retry-failed":
                with STATE.lock:
                    for item in STATE.queue:
                        if item["status"] in ("failed", "stopped"):
                            item["status"] = "pending"
                            item["error"] = ""
                return json_response(self, {"ok": True, "status": STATE.snapshot()})
            if path == "/api/queue/start":
                with STATE.lock:
                    if STATE.running:
                        return json_response(self, {"ok": False, "error": "已有任务正在运"}, 409)
                    if not STATE.queue:
                        return json_response(self, {"ok": False, "error": "队列为空"}, 400)
                thread = threading.Thread(target=run_queue, daemon=True)
                with STATE.lock:
                    STATE.thread = thread
                thread.start()
                return json_response(self, {"ok": True})
        except Exception as exc:
            return json_response(self, {"ok": False, "error": str(exc)}, 500)
        return text_response(self, "Not Found", "text/plain; charset=utf-8", 404)



INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN" data-theme="dark">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <link rel="icon" href="/favicon.svg?v=3" type="image/svg+xml" sizes="any" />
  <title>AudioMeta Nexus</title>
  <style>
/* ===================================================================
   AudioMeta Nexus  —  UI v4 全新设计系统
   设计语言：中性基底 + 单一品牌强调色，依赖排版/空间/层级而非特效
   =================================================================== */
*, *::before, *::after { box-sizing: border-box; }
* { margin: 0; padding: 0; }
:root {
  --font-sans: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei UI", "Microsoft YaHei", "Noto Sans SC", "Source Han Sans SC", "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  --font-mono: ui-monospace, "SF Mono", "Cascadia Code", "JetBrains Mono", Consolas, "Noto Sans Mono CJK SC", monospace;
  --fs-11: 11px; --fs-12: 12px; --fs-13: 13px; --fs-14: 14px; --fs-15: 15px;
  --fs-16: 16px; --fs-18: 18px; --fs-22: 22px; --fs-28: 28px;
  --lh-tight: 1.3; --lh-base: 1.6;
  --sp-1: 4px; --sp-2: 8px; --sp-3: 12px; --sp-4: 16px; --sp-5: 20px;
  --sp-6: 24px; --sp-8: 32px; --sp-10: 40px;
  --r-sm: 7px; --r-md: 10px; --r-lg: 14px; --r-xl: 18px; --r-pill: 999px;
  --dur-1: .12s; --dur-2: .2s; --dur-3: .32s;
  --ease: cubic-bezier(.4, 0, .2, 1);
  --topbar-h: 62px;
}
/* ---------- 暗色主题（默认） ---------- */
:root, html[data-theme="dark"] {
  color-scheme: dark;
  --bg: #0d1017;
  --bg-grad: radial-gradient(1200px 600px at 82% -8%, rgba(76,141,255,.10), transparent 60%);
  --surface: #141a23;
  --surface-2: #1a212c;
  --elevated: #202834;
  --overlay: rgba(6, 9, 14, .62);
  --border: #262f3b;
  --border-strong: #35414f;
  --text-1: #e8edf4;
  --text-2: #a4b0c0;
  --text-3: #6c7889;
  --brand: #4c8dff;
  --brand-hover: #69a1ff;
  --brand-press: #3b78ea;
  --brand-ink: #ffffff;
  --brand-soft: rgba(76,141,255,.16);
  --brand-line: rgba(76,141,255,.42);
  --ring: rgba(76,141,255,.45);
  --success: #37b981; --success-soft: rgba(55,185,129,.15);
  --warning: #e0a23c; --warning-soft: rgba(224,162,60,.16);
  --danger: #ef5350; --danger-soft: rgba(239,83,80,.16);
  --shadow-sm: 0 1px 2px rgba(0,0,0,.4);
  --shadow-md: 0 8px 24px rgba(0,0,0,.42);
  --shadow-lg: 0 24px 60px rgba(0,0,0,.55);
}
/* ---------- 素雪（亮） ---------- */
html[data-theme="light"] {
  color-scheme: light;
  --bg: #f4f6fa;
  --bg-grad: radial-gradient(1100px 560px at 84% -10%, rgba(76,141,255,.10), transparent 62%);
  --surface: #ffffff;
  --surface-2: #f2f5fa;
  --elevated: #ffffff;
  --overlay: rgba(28, 38, 54, .34);
  --border: #e2e8f1;
  --border-strong: #cdd7e4;
  --text-1: #182230;
  --text-2: #55627a;
  --text-3: #8b97ab;
  --brand: #2f6bf0; --brand-hover: #4880f5; --brand-press: #245bd6; --brand-ink: #ffffff;
  --brand-soft: rgba(47,107,240,.10); --brand-line: rgba(47,107,240,.30); --ring: rgba(47,107,240,.32);
  --success: #1f9d67; --success-soft: rgba(31,157,103,.12);
  --warning: #c47f16; --warning-soft: rgba(196,127,22,.14);
  --danger: #d93a3f; --danger-soft: rgba(217,58,63,.12);
  --shadow-sm: 0 1px 2px rgba(24,34,48,.08);
  --shadow-md: 0 10px 28px rgba(24,34,48,.10);
  --shadow-lg: 0 24px 60px rgba(24,34,48,.16);
}
/* ---------- 茶白（暖亮） ---------- */
html[data-theme="linen"] {
  color-scheme: light;
  --bg: #f6f2ea;
  --bg-grad: radial-gradient(1100px 560px at 84% -10%, rgba(196,140,64,.12), transparent 62%);
  --surface: #fffdf9;
  --surface-2: #f3ede1;
  --elevated: #fffdf9;
  --overlay: rgba(52, 40, 24, .32);
  --border: #e7ddcc;
  --border-strong: #d6c9b2;
  --text-1: #2a2016;
  --text-2: #6a5c48;
  --text-3: #9c8d76;
  --brand: #b5761e; --brand-hover: #c98a2e; --brand-press: #9c6414; --brand-ink: #ffffff;
  --brand-soft: rgba(181,118,30,.12); --brand-line: rgba(181,118,30,.32); --ring: rgba(181,118,30,.34);
  --success: #4f8a3d; --success-soft: rgba(79,138,61,.14);
  --warning: #c0821e; --warning-soft: rgba(192,130,30,.16);
  --danger: #c1503f; --danger-soft: rgba(193,80,63,.14);
  --shadow-sm: 0 1px 2px rgba(74,56,32,.08);
  --shadow-md: 0 10px 28px rgba(74,56,32,.10);
  --shadow-lg: 0 24px 60px rgba(74,56,32,.16);
}
/* ---------- 青瓷（冷亮） ---------- */
html[data-theme="mint"] {
  color-scheme: light;
  --bg: #eef5f2;
  --bg-grad: radial-gradient(1100px 560px at 84% -10%, rgba(24,158,130,.12), transparent 62%);
  --surface: #ffffff;
  --surface-2: #eaf3ef;
  --elevated: #ffffff;
  --overlay: rgba(18, 46, 40, .32);
  --border: #d7e7e0;
  --border-strong: #bcd6cc;
  --text-1: #12261f;
  --text-2: #4c6b62;
  --text-3: #85a096;
  --brand: #0e9a72; --brand-hover: #17ac82; --brand-press: #0a835f; --brand-ink: #ffffff;
  --brand-soft: rgba(14,154,114,.12); --brand-line: rgba(14,154,114,.30); --ring: rgba(14,154,114,.32);
  --success: #0e9a72; --success-soft: rgba(14,154,114,.13);
  --warning: #bf8218; --warning-soft: rgba(191,130,24,.15);
  --danger: #d1443f; --danger-soft: rgba(209,68,63,.13);
  --shadow-sm: 0 1px 2px rgba(16,48,40,.07);
  --shadow-md: 0 10px 28px rgba(16,48,40,.10);
  --shadow-lg: 0 24px 60px rgba(16,48,40,.15);
}
/* ---------- 胭脂（暖亮） ---------- */
html[data-theme="rose"] {
  color-scheme: light;
  --bg: #f9eff1;
  --bg-grad: radial-gradient(1100px 560px at 84% -10%, rgba(214,71,104,.12), transparent 62%);
  --surface: #fffbfc;
  --surface-2: #f6e8ec;
  --elevated: #fffbfc;
  --overlay: rgba(58, 24, 34, .32);
  --border: #eed7dd;
  --border-strong: #e0bcc6;
  --text-1: #2c161d;
  --text-2: #6d4e57;
  --text-3: #a4818c;
  --brand: #d24169; --brand-hover: #dd5980; --brand-press: #b93257; --brand-ink: #ffffff;
  --brand-soft: rgba(210,65,105,.11); --brand-line: rgba(210,65,105,.30); --ring: rgba(210,65,105,.32);
  --success: #2f9a68; --success-soft: rgba(47,154,104,.13);
  --warning: #c07f1c; --warning-soft: rgba(192,127,28,.15);
  --danger: #d93a3f; --danger-soft: rgba(217,58,63,.13);
  --shadow-sm: 0 1px 2px rgba(58,24,34,.07);
  --shadow-md: 0 10px 28px rgba(58,24,34,.10);
  --shadow-lg: 0 24px 60px rgba(58,24,34,.15);
}
/* ---------- 黛蓝（深） ---------- */
html[data-theme="ocean"] {
  color-scheme: dark;
  --bg: #0a1420;
  --bg-grad: radial-gradient(1200px 600px at 82% -8%, rgba(56,150,224,.14), transparent 60%);
  --surface: #10202f;
  --surface-2: #16293b;
  --elevated: #1c3145;
  --overlay: rgba(4, 12, 20, .64);
  --border: #21384c;
  --border-strong: #2f4c66;
  --text-1: #e2eef8; --text-2: #9db6cc; --text-3: #647e95;
  --brand: #38a7e0; --brand-hover: #55b8ea; --brand-press: #2b90c6; --brand-ink: #041019;
  --brand-soft: rgba(56,167,224,.16); --brand-line: rgba(56,167,224,.42); --ring: rgba(56,167,224,.45);
  --success: #35bd8f; --success-soft: rgba(53,189,143,.16);
  --warning: #e0a83c; --warning-soft: rgba(224,168,60,.16);
  --danger: #ef5f5b; --danger-soft: rgba(239,95,91,.16);
  --shadow-sm: 0 1px 2px rgba(0,0,0,.42);
  --shadow-md: 0 8px 24px rgba(0,0,0,.46);
  --shadow-lg: 0 24px 60px rgba(0,0,0,.58);
}
/* ---------- 暮紫（深） ---------- */
html[data-theme="aurora"] {
  color-scheme: dark;
  --bg: #12101f;
  --bg-grad: radial-gradient(1200px 600px at 82% -8%, rgba(146,109,246,.16), transparent 60%);
  --surface: #1a1730;
  --surface-2: #221d3c;
  --elevated: #2a2447;
  --overlay: rgba(10, 8, 20, .64);
  --border: #2d2748; --border-strong: #3e3663;
  --text-1: #ece8f8; --text-2: #b3aacf; --text-3: #78708f;
  --brand: #9b7dfb; --brand-hover: #ac92fc; --brand-press: #8567ea; --brand-ink: #0d0a19;
  --brand-soft: rgba(155,125,251,.18); --brand-line: rgba(155,125,251,.44); --ring: rgba(155,125,251,.46);
  --success: #46c68f; --success-soft: rgba(70,198,143,.16);
  --warning: #e2ad45; --warning-soft: rgba(226,173,69,.16);
  --danger: #f0625f; --danger-soft: rgba(240,98,95,.16);
  --shadow-sm: 0 1px 2px rgba(0,0,0,.42);
  --shadow-md: 0 8px 24px rgba(0,0,0,.46);
  --shadow-lg: 0 24px 60px rgba(0,0,0,.58);
}
/* ---------- 碧波（深） ---------- */
html[data-theme="jade"] {
  color-scheme: dark;
  --bg: #08150f;
  --bg-grad: radial-gradient(1200px 600px at 82% -8%, rgba(35,180,132,.14), transparent 60%);
  --surface: #0e2018;
  --surface-2: #142a20;
  --elevated: #1a3529;
  --overlay: rgba(2, 12, 8, .64);
  --border: #1f382b; --border-strong: #2c4d3b;
  --text-1: #e0f3ea; --text-2: #9dc0af; --text-3: #628070;
  --brand: #29b785; --brand-hover: #3fc796; --brand-press: #219d72; --brand-ink: #04130d;
  --brand-soft: rgba(41,183,133,.16); --brand-line: rgba(41,183,133,.42); --ring: rgba(41,183,133,.45);
  --success: #29b785; --success-soft: rgba(41,183,133,.16);
  --warning: #d7a53f; --warning-soft: rgba(215,165,63,.16);
  --danger: #e9605c; --danger-soft: rgba(233,96,92,.16);
  --shadow-sm: 0 1px 2px rgba(0,0,0,.42);
  --shadow-md: 0 8px 24px rgba(0,0,0,.46);
  --shadow-lg: 0 24px 60px rgba(0,0,0,.58);
}
/* ---------- 玄灰（深） ---------- */
html[data-theme="graphite"] {
  color-scheme: dark;
  --bg: #101215;
  --bg-grad: radial-gradient(1200px 600px at 82% -8%, rgba(150,160,175,.08), transparent 60%);
  --surface: #17191d;
  --surface-2: #1d2024;
  --elevated: #23262b;
  --overlay: rgba(6, 7, 9, .64);
  --border: #292c31; --border-strong: #393d44;
  --text-1: #e7e9ec; --text-2: #a6acb5; --text-3: #6b7079;
  --brand: #7f8b9c; --brand-hover: #93a0b1; --brand-press: #6c7889; --brand-ink: #0f1114;
  --brand-soft: rgba(127,139,156,.16); --brand-line: rgba(127,139,156,.42); --ring: rgba(127,139,156,.42);
  --success: #4fb98a; --success-soft: rgba(79,185,138,.15);
  --warning: #d3a24a; --warning-soft: rgba(211,162,74,.15);
  --danger: #e06764; --danger-soft: rgba(224,103,100,.15);
  --shadow-sm: 0 1px 2px rgba(0,0,0,.44);
  --shadow-md: 0 8px 24px rgba(0,0,0,.48);
  --shadow-lg: 0 24px 60px rgba(0,0,0,.6);
}
/* ---------- 基础元素 ---------- */
html, body { height: 100%; }
body {
  font-family: var(--font-sans);
  font-size: var(--fs-14);
  line-height: var(--lh-base);
  color: var(--text-1);
  background: var(--bg);
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
  transition: background var(--dur-3) var(--ease), color var(--dur-3) var(--ease);
}
body::before {
  content: "";
  position: fixed;
  inset: 0;
  background: var(--bg-grad);
  pointer-events: none;
  z-index: 0;
}
button, input, select, textarea { font-family: inherit; font-size: inherit; color: inherit; }
button { cursor: pointer; background: none; border: none; }
a { color: var(--brand); text-decoration: none; }
::selection { background: var(--brand-soft); color: var(--text-1); }
.ui-icon { display: inline-block; vertical-align: -2px; flex: none; }
:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; border-radius: var(--r-sm); }

/* ---------- 应用骨架 ---------- */
.app-shell {
  position: relative;
  z-index: 1;
  min-height: 100%;
  display: flex;
  flex-direction: column;
}
.workspace {
  flex: 1;
  display: grid;
  grid-template-columns: minmax(0, 1.05fr) minmax(0, 1fr);
  gap: var(--sp-5);
  align-items: start;
  width: min(1680px, 100%);
  margin: 0 auto;
  padding: var(--sp-5) clamp(var(--sp-4), 3vw, var(--sp-8)) var(--sp-8);
}
.column { display: flex; flex-direction: column; gap: var(--sp-5); min-width: 0; }
.column-right { position: sticky; top: calc(var(--topbar-h) + var(--sp-5)); }

/* ---------- 顶栏 ---------- */
.global-topbar {
  position: sticky;
  top: 0;
  z-index: 40;
  height: var(--topbar-h);
  display: flex;
  align-items: center;
  gap: var(--sp-4);
  padding: 0 clamp(var(--sp-4), 3vw, var(--sp-8));
  background: color-mix(in srgb, var(--surface) 82%, transparent);
  backdrop-filter: saturate(140%) blur(14px);
  border-bottom: 1px solid var(--border);
}
.brand { display: flex; align-items: center; gap: var(--sp-3); min-width: 0; }
.brand-logo {
  width: 38px; height: 38px; border-radius: var(--r-md);
  display: grid; place-items: center; flex: none;
  background: linear-gradient(150deg, var(--brand), color-mix(in srgb, var(--brand) 60%, #7b5cff));
  color: var(--brand-ink);
  box-shadow: var(--shadow-sm);
}
.brand-logo svg { width: 22px; height: 22px; }
.brand-text { display: flex; flex-direction: column; line-height: 1.2; min-width: 0; }
.brand-title { font-size: var(--fs-15); font-weight: 650; letter-spacing: .3px; color: var(--text-1); white-space: nowrap; }
.brand-sub { font-size: var(--fs-11); color: var(--text-3); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.topbar-spacer { flex: 1; }
.topbar-actions { display: flex; align-items: center; gap: var(--sp-2); }
/* ---------- 顶栏状态胶囊 ---------- */
.topbar-status {
  display: flex; align-items: center; gap: var(--sp-3);
  padding: 6px var(--sp-3) 6px 10px;
  border: 1px solid var(--border);
  border-radius: var(--r-pill);
  background: var(--surface-2);
  max-width: 340px; min-width: 0;
}
.state-dot { width: 9px; height: 9px; border-radius: 50%; flex: none; background: var(--text-3); position: relative; }
.state-dot.running { background: var(--brand); box-shadow: 0 0 0 0 var(--ring); animation: dotPulse 1.5s var(--ease) infinite; }
.state-dot.done { background: var(--success); }
.state-dot.failed { background: var(--danger); }
@keyframes dotPulse { 0% { box-shadow: 0 0 0 0 var(--ring); } 70% { box-shadow: 0 0 0 7px transparent; } 100% { box-shadow: 0 0 0 0 transparent; } }
.topbar-status-body { display: flex; flex-direction: column; min-width: 0; line-height: 1.25; }
.topbar-status-body #stateText { font-size: var(--fs-12); font-weight: 600; color: var(--text-1); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 190px; }
.topbar-status-meta { display: flex; align-items: center; gap: 6px; font-size: var(--fs-11); color: var(--text-3); }
.topbar-status-meta b { color: var(--brand); font-weight: 650; }
.mini-track { width: 66px; height: 5px; border-radius: var(--r-pill); background: var(--border); overflow: hidden; flex: none; }
.mini-track > span { display: block; height: 100%; width: 0; border-radius: inherit; background: var(--brand); transition: width var(--dur-3) var(--ease); }

/* ---------- 主题切换簇 ---------- */
.theme-cluster { position: relative; display: flex; align-items: center; }
.icon-button {
  width: 38px; height: 38px; border-radius: var(--r-md);
  display: grid; place-items: center; flex: none;
  color: var(--text-2);
  border: 1px solid var(--border);
  background: var(--surface-2);
  transition: all var(--dur-2) var(--ease);
}
.icon-button:hover { color: var(--text-1); border-color: var(--border-strong); background: var(--elevated); }
.icon-button:active { transform: translateY(1px); }
.icon-button svg { width: 18px; height: 18px; }
.theme-toggle { position: relative; overflow: hidden; }
.theme-symbol { position: absolute; inset: 0; display: grid; place-items: center; pointer-events: none; transition: opacity var(--dur-2) var(--ease), transform var(--dur-2) var(--ease); }
.theme-symbol:first-child { opacity: 0; transform: rotate(-40deg) scale(.6); }
.theme-symbol:last-child { opacity: 1; transform: rotate(0) scale(1); }
html[data-theme="light"] .theme-symbol:first-child,
html[data-theme="linen"] .theme-symbol:first-child,
html[data-theme="mint"] .theme-symbol:first-child,
html[data-theme="rose"] .theme-symbol:first-child { opacity: 1; transform: rotate(0) scale(1); }
html[data-theme="light"] .theme-symbol:last-child,
html[data-theme="linen"] .theme-symbol:last-child,
html[data-theme="mint"] .theme-symbol:last-child,
html[data-theme="rose"] .theme-symbol:last-child { opacity: 0; transform: rotate(40deg) scale(.6); }
/* ---------- 卡片 / 分区 ---------- */
#configForm { display: flex; flex-direction: column; gap: var(--sp-5); min-width: 0; }
.section, .panel-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--r-lg);
  box-shadow: var(--shadow-sm);
}
.section { padding: var(--sp-5); position: relative; }
.section-title {
  display: flex; align-items: center; gap: var(--sp-3);
  margin-bottom: var(--sp-4);
}
.section-icon {
  width: 30px; height: 30px; border-radius: var(--r-sm);
  display: grid; place-items: center; flex: none;
  color: var(--brand);
  background: var(--brand-soft);
}
.section-icon svg { width: 17px; height: 17px; }
.section-title h2 { font-size: var(--fs-15); font-weight: 640; letter-spacing: .2px; }
.section-title .section-hint { font-size: var(--fs-12); color: var(--text-3); font-weight: 400; }
.section-toggle {
  margin-left: auto; width: 28px; height: 28px; border-radius: var(--r-sm);
  display: none; place-items: center; color: var(--text-3);
}
.section-toggle svg { width: 16px; height: 16px; }

/* ---------- 表单 ---------- */
.field-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--sp-4); }
.field { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.field.col-span-2 { grid-column: 1 / -1; }
.field > label, .field-label {
  font-size: var(--fs-12); font-weight: 560; color: var(--text-2);
  display: flex; align-items: center; gap: 6px;
}
.field-label .req { color: var(--danger); font-weight: 700; }
.field-note { font-size: var(--fs-11); color: var(--text-3); }
input[type="text"], input[type="url"], input[type="search"], input:not([type]), textarea {
  width: 100%;
  padding: 10px var(--sp-3);
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  color: var(--text-1);
  transition: border-color var(--dur-2) var(--ease), box-shadow var(--dur-2) var(--ease), background var(--dur-2) var(--ease);
}
input::placeholder, textarea::placeholder { color: var(--text-3); }
input:hover, textarea:hover { border-color: var(--border-strong); }
input:focus, textarea:focus {
  outline: none; border-color: var(--brand);
  box-shadow: 0 0 0 3px var(--ring); background: var(--surface);
}
textarea { resize: vertical; min-height: 92px; line-height: var(--lh-base); }
.field-error, input.field-error, textarea.field-error, .chips.field-error {
  border-color: var(--danger) !important;
  box-shadow: 0 0 0 3px var(--danger-soft) !important;
}
.input-row { display: flex; gap: var(--sp-2); align-items: stretch; }
.input-row > input, .input-row > .custom-select { flex: 1; min-width: 0; }
.hint { font-size: var(--fs-12); color: var(--text-3); }
/* ---------- 按钮系统 ---------- */
.btn, .btn-primary, .btn-secondary, .btn-ghost, .quiet-button, .btn-indigo, .btn-amber, .btn-red, .field-mini-action {
  display: inline-flex; align-items: center; justify-content: center; gap: 7px;
  padding: 9px var(--sp-4);
  font-size: var(--fs-13); font-weight: 560; white-space: nowrap;
  border-radius: var(--r-md);
  border: 1px solid transparent;
  transition: all var(--dur-2) var(--ease);
  user-select: none;
}
.btn svg, .btn-primary svg, .btn-secondary svg, .btn-ghost svg, .quiet-button svg, .btn-indigo svg, .btn-amber svg, .btn-red svg, .field-mini-action svg { width: 16px; height: 16px; }
.btn-primary, .btn-indigo { background: var(--brand); color: var(--brand-ink); box-shadow: var(--shadow-sm); }
.btn-primary:hover, .btn-indigo:hover { background: var(--brand-hover); }
.btn-primary:active, .btn-indigo:active { background: var(--brand-press); transform: translateY(1px); }
.btn-secondary, .quiet-button {
  background: var(--surface-2); color: var(--text-1); border-color: var(--border);
}
.quiet-button:hover, .btn-secondary:hover { border-color: var(--border-strong); background: var(--elevated); }
.quiet-button:active, .btn-secondary:active { transform: translateY(1px); }
.btn-ghost { background: transparent; color: var(--text-2); border-color: transparent; }
.btn-ghost:hover { background: var(--surface-2); color: var(--text-1); }
.btn-amber { background: var(--warning-soft); color: var(--warning); border-color: color-mix(in srgb, var(--warning) 32%, transparent); }
.btn-amber:hover { background: color-mix(in srgb, var(--warning) 20%, transparent); }
.btn-red { background: var(--danger-soft); color: var(--danger); border-color: color-mix(in srgb, var(--danger) 34%, transparent); }
.btn-red:hover { background: color-mix(in srgb, var(--danger) 20%, transparent); }
.btn-red:active, .btn-amber:active { transform: translateY(1px); }
.btn-block { width: 100%; }
.btn-lg { padding: 12px var(--sp-5); font-size: var(--fs-14); font-weight: 600; }
.field-mini-action {
  padding: 0 var(--sp-3); height: auto; align-self: stretch;
  background: var(--surface-2); color: var(--text-2); border-color: var(--border);
  font-size: var(--fs-12);
}
.field-mini-action:hover { color: var(--brand); border-color: var(--brand-line); background: var(--brand-soft); }
button:disabled, .btn:disabled, .btn-primary:disabled, .btn-secondary:disabled, .btn-ghost:disabled, .quiet-button:disabled,
.btn-indigo:disabled, .btn-amber:disabled, .btn-red:disabled, .field-mini-action:disabled {
  opacity: .5; cursor: not-allowed; transform: none !important;
}
button[disabled] .ui-icon { opacity: .7; }
.btn-loading { position: relative; color: transparent !important; pointer-events: none; }
.btn-loading::after {
  content: ""; position: absolute; width: 15px; height: 15px;
  border: 2px solid currentColor; border-top-color: transparent; border-radius: 50%;
  color: var(--brand-ink); animation: spin .7s linear infinite;
}
.quiet-button.btn-loading::after, .btn-secondary.btn-loading::after { color: var(--text-1); }
@keyframes spin { to { transform: rotate(360deg); } }
.button-row { display: flex; flex-wrap: wrap; gap: var(--sp-2); }
/* ---------- 自定义下拉 ---------- */
.custom-select { position: relative; min-width: 0; }
select.custom-select-native {
  position: absolute; opacity: 0; pointer-events: none; width: 1px; height: 1px;
}
.custom-select-trigger {
  width: 100%;
  display: flex; align-items: center; gap: var(--sp-2);
  padding: 10px var(--sp-3);
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  color: var(--text-1);
  text-align: left;
  transition: border-color var(--dur-2) var(--ease), box-shadow var(--dur-2) var(--ease), background var(--dur-2) var(--ease);
}
.custom-select-trigger:hover { border-color: var(--border-strong); }
.custom-select-trigger.open, .custom-select-trigger:focus-visible {
  border-color: var(--brand); box-shadow: 0 0 0 3px var(--ring); background: var(--surface); outline: none;
}
.custom-select-trigger:disabled { opacity: .55; cursor: not-allowed; }
.custom-select-value { flex: 1; min-width: 0; display: flex; align-items: center; gap: 7px; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.custom-select-arrow {
  flex: none; width: 9px; height: 9px; margin-left: auto;
  border-right: 2px solid var(--text-3); border-bottom: 2px solid var(--text-3);
  transform: rotate(45deg) translate(-1px, -2px);
  transition: transform var(--dur-2) var(--ease);
}
.custom-select-trigger.open .custom-select-arrow { transform: rotate(225deg) translate(-1px, -1px); }
.custom-select-popover {
  position: fixed; z-index: 3000;
  background: var(--elevated);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  box-shadow: var(--shadow-lg);
  padding: 6px;
  overflow-y: auto;
  opacity: 0; transform: translateY(-6px); pointer-events: none;
  transition: opacity var(--dur-2) var(--ease), transform var(--dur-2) var(--ease);
}
.custom-select-popover.open { opacity: 1; transform: translateY(0); pointer-events: auto; }
.custom-select-popover[data-side="top"] { transform: translateY(6px); }
.custom-select-popover[data-side="top"].open { transform: translateY(0); }
.custom-select-option {
  width: 100%; display: flex; align-items: center; gap: 8px;
  padding: 9px 10px; border-radius: var(--r-sm);
  color: var(--text-1); text-align: left; font-size: var(--fs-13);
  transition: background var(--dur-1) var(--ease);
}
.custom-select-option:hover, .custom-select-option:focus-visible { background: var(--surface-2); outline: none; }
.custom-select-option.selected { background: var(--brand-soft); color: var(--brand); font-weight: 600; }
.custom-select-option:disabled { color: var(--text-3); }
.platform-logo {
  width: 20px; height: 20px; border-radius: 6px; flex: none;
  display: grid; place-items: center; font-size: var(--fs-11); font-weight: 700;
  color: #fff; background: var(--brand-color, var(--brand));
}
/* ---------- 标签 / 芯片池（统一中性配色，不使用随机彩色） ---------- */
.chips {
  display: flex; flex-wrap: wrap; gap: var(--sp-2); align-items: center;
  padding: 8px;
  min-height: 44px;
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  transition: border-color var(--dur-2) var(--ease), box-shadow var(--dur-2) var(--ease);
}
.chips:focus-within { border-color: var(--brand); box-shadow: 0 0 0 3px var(--ring); }
.chip, .colored-chip, .album-tag-chip {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 4px 6px 4px 11px;
  font-size: var(--fs-12); font-weight: 540; line-height: 1.4;
  color: var(--text-1);
  background: var(--brand-soft);
  border: 1px solid var(--brand-line);
  border-radius: var(--r-pill);
  max-width: 100%;
}
.chip > span:first-child, .album-tag-chip > span:first-child { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.chip > button, .album-tag-chip > button {
  width: 18px; height: 18px; border-radius: 50%; flex: none;
  display: grid; place-items: center; font-size: 13px; line-height: 1;
  color: var(--text-2); background: transparent;
  transition: all var(--dur-1) var(--ease);
}
.chip > button:hover, .album-tag-chip > button:hover { background: var(--danger-soft); color: var(--danger); }
.album-tag-chip { background: var(--surface); border-color: var(--border-strong); }
.chip-input {
  flex: 1; min-width: 90px;
  padding: 4px 6px; font-size: var(--fs-13); color: var(--text-1);
  outline: none; border-radius: var(--r-sm);
}
.chip-input:empty::before { content: attr(data-placeholder); color: var(--text-3); }
.tag-input-row { display: flex; gap: var(--sp-2); margin-top: var(--sp-2); }
.tag-input-row > input { flex: 1; }
.series-pool { display: flex; flex-wrap: wrap; gap: var(--sp-2); }
.series-empty { font-size: var(--fs-12); color: var(--text-3); font-style: normal; }
/* ---------- 封面 / 视觉 ---------- */
.cover-row { display: grid; grid-template-columns: 148px minmax(0, 1fr); gap: var(--sp-4); align-items: start; }
.cover-box {
  position: relative; width: 148px; height: 148px; flex: none;
  border-radius: var(--r-md); overflow: hidden;
  background: var(--surface-2);
  border: 1px solid var(--border);
  display: grid; place-items: center;
}
.cover-box img { width: 100%; height: 100%; object-fit: cover; display: none; }
.cover-box:hover .cover-change-button,
.cover-box:focus-within .cover-change-button { opacity: 1; transform: translateY(0); }
.cover-change-button {
  position: absolute; left: 8px; right: 8px; bottom: 8px;
  min-height: 32px; padding: 6px 10px;
  display: inline-flex; align-items: center; justify-content: center;
  font-size: var(--fs-12); font-weight: 600; white-space: nowrap;
  border: 1px solid color-mix(in srgb, var(--brand) 48%, transparent);
  border-radius: var(--r-sm);
  background: color-mix(in srgb, var(--surface) 82%, transparent);
  color: var(--text-1);
  box-shadow: var(--shadow-md);
  backdrop-filter: blur(10px);
  opacity: 0; transform: translateY(5px);
  transition: opacity var(--dur-2) var(--ease), transform var(--dur-2) var(--ease), background var(--dur-2) var(--ease), border-color var(--dur-2) var(--ease);
}
.cover-change-button:hover { background: var(--brand); border-color: var(--brand); color: var(--brand-ink); }
.cover-change-button:focus-visible { opacity: 1; transform: translateY(0); }
.cover-empty {
  display: flex; flex-direction: column; align-items: center; gap: 6px;
  color: var(--text-3); font-size: var(--fs-12); text-align: center; padding: var(--sp-3);
}
.cover-empty svg { width: 26px; height: 26px; opacity: .7; }
.cover-side { display: flex; flex-direction: column; gap: var(--sp-3); min-width: 0; }
.cover-meta {
  font-size: var(--fs-12); color: var(--text-2); font-family: var(--font-mono);
  padding: 5px 10px; background: var(--surface-2); border: 1px solid var(--border);
  border-radius: var(--r-sm); align-self: center; min-width: 118px; text-align: center;
}
.visual-content-column { display: flex; flex-direction: column; gap: var(--sp-4); }

/* ---------- 链接来源分组 ---------- */
.source-tabs { display: flex; gap: 4px; padding: 4px; background: var(--surface-2); border: 1px solid var(--border); border-radius: var(--r-md); margin-bottom: var(--sp-4); }
.source-tab {
  flex: 1; padding: 7px var(--sp-3); border-radius: var(--r-sm);
  font-size: var(--fs-12); font-weight: 560; color: var(--text-2);
  transition: all var(--dur-2) var(--ease);
}
.source-tab.active { background: var(--surface); color: var(--brand); box-shadow: var(--shadow-sm); }
.source-pane { display: none; }
.source-pane.active { display: block; }
/* ---------- 处理控制台（右列） ---------- */
.run-panel { padding: var(--sp-5); display: flex; flex-direction: column; gap: var(--sp-4); }
.run-panel-head { display: flex; align-items: center; justify-content: space-between; gap: var(--sp-3); }
.run-progress-head { display: flex; align-items: baseline; justify-content: space-between; }
.run-progress-head #percentText { font-size: var(--fs-22); font-weight: 700; color: var(--text-1); font-variant-numeric: tabular-nums; }
.run-progress-head .run-count { font-size: var(--fs-12); color: var(--text-3); }
.progress-track { height: 8px; border-radius: var(--r-pill); background: var(--border); overflow: hidden; }
.progress-track > span { display: block; height: 100%; width: 0; border-radius: inherit; background: linear-gradient(90deg, var(--brand), var(--brand-hover)); transition: width var(--dur-3) var(--ease); }
.run-actions { display: grid; grid-template-columns: 1fr 1fr; gap: var(--sp-2); }
.run-actions .btn-block { grid-column: 1 / -1; }

.queue-console { display: flex; flex-direction: column; min-height: 0; overflow: hidden; }
.tab-bar {
  display: flex; gap: 2px; padding: 6px; overflow-x: auto;
  border-bottom: 1px solid var(--border); background: var(--surface-2);
}
.tab {
  display: inline-flex; align-items: center; gap: 7px;
  padding: 8px var(--sp-3); border-radius: var(--r-sm); white-space: nowrap;
  font-size: var(--fs-13); font-weight: 550; color: var(--text-2);
  transition: all var(--dur-2) var(--ease);
}
.tab svg { width: 15px; height: 15px; }
.tab:hover { color: var(--text-1); background: var(--surface); }
.tab.active { color: var(--brand); background: var(--surface); box-shadow: var(--shadow-sm); }
.tab .tab-badge {
  min-width: 18px; height: 18px; padding: 0 5px; border-radius: var(--r-pill);
  display: grid; place-items: center; font-size: var(--fs-11); font-weight: 650;
  background: var(--border); color: var(--text-2);
}
.tab.active .tab-badge { background: var(--brand-soft); color: var(--brand); }
.tab-panel { display: none; padding: var(--sp-4); }
.tab-panel.active { display: flex; flex-direction: column; gap: var(--sp-3); }
.tab-toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: var(--sp-2); }
.tab-toolbar .spacer { flex: 1; }
/* ---------- 队列表 ---------- */
.queue-actions { display: flex; flex-wrap: wrap; align-items: center; gap: var(--sp-2); }
.queue-actions .selection-hint { font-size: var(--fs-12); color: var(--text-3); margin-left: auto; }
.queue-actions .selection-hint b { color: var(--brand); }
.queue-actions .needs-selection { opacity: .5; pointer-events: none; }
.queue-actions.has-selection .needs-selection { opacity: 1; pointer-events: auto; }
.table-wrap { overflow-x: auto; border: 1px solid var(--border); border-radius: var(--r-md); }
table.data-table { width: 100%; border-collapse: collapse; font-size: var(--fs-13); }
.data-table thead th {
  text-align: left; font-weight: 580; font-size: var(--fs-11); letter-spacing: .4px;
  color: var(--text-3); text-transform: uppercase;
  padding: 10px var(--sp-3); background: var(--surface-2);
  border-bottom: 1px solid var(--border); white-space: nowrap;
}
.data-table tbody td { padding: 10px var(--sp-3); border-bottom: 1px solid var(--border); color: var(--text-1); vertical-align: middle; }
.data-table tbody tr:last-child td { border-bottom: none; }
.data-table tbody tr { transition: background var(--dur-1) var(--ease); }
.data-table tbody tr:hover { background: var(--surface-2); }
.data-table tbody tr.selected { background: var(--brand-soft); }
.queue-check { width: 16px; height: 16px; accent-color: var(--brand); cursor: pointer; }
.queue-platform { display: inline-flex; align-items: center; gap: 7px; }
.queue-platform-icon {
  width: 20px; height: 20px; border-radius: 6px; flex: none;
  display: grid; place-items: center; font-style: normal; font-size: var(--fs-11); font-weight: 700;
  background: var(--surface-2); border: 1px solid var(--border); color: var(--text-2);
}
.queue-progress { display: inline-flex; flex-direction: column; gap: 4px; min-width: 74px; font-variant-numeric: tabular-nums; font-size: var(--fs-12); color: var(--text-2); }
.queue-progress.done { color: var(--success); }
.queue-progress-track { display: block; height: 4px; border-radius: var(--r-pill); background: var(--border); overflow: hidden; }
.queue-progress-track > i { display: block; height: 100%; background: var(--brand); border-radius: inherit; transition: width var(--dur-3) var(--ease); }
.queue-progress.done .queue-progress-track > i { background: var(--success); }
.status-badge {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 3px 9px; border-radius: var(--r-pill);
  font-size: var(--fs-11); font-weight: 600; white-space: nowrap;
  background: var(--border); color: var(--text-2);
}
.status-badge::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.status-badge.pending { background: var(--surface-2); color: var(--text-3); }
.status-badge.processing { background: var(--brand-soft); color: var(--brand); }
.status-badge.done { background: var(--success-soft); color: var(--success); }
.status-badge.failed { background: var(--danger-soft); color: var(--danger); }
.status-badge.stopped { background: var(--warning-soft); color: var(--warning); }
.queue-row-actions { display: inline-flex; gap: 4px; }
.queue-row-actions button {
  width: 28px; height: 28px; border-radius: var(--r-sm); flex: none;
  display: grid; place-items: center; font-size: 14px;
  color: var(--text-2); border: 1px solid var(--border); background: var(--surface-2);
  transition: all var(--dur-1) var(--ease);
}
.queue-row-actions button:hover { color: var(--brand); border-color: var(--brand-line); background: var(--brand-soft); }
/* ---------- 日志控制台 ---------- */
#logFilterBox { display: flex; gap: var(--sp-2); align-items: center; }
#logFilterBox .custom-select, #logFilterBox select { min-width: 130px; }
#logFilterBox > input { flex: 1; }
.log-box {
  height: 340px; overflow-y: auto;
  padding: var(--sp-3);
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  font-family: var(--font-mono); font-size: var(--fs-12); line-height: 1.7;
}
.log-line {
  padding: 3px 10px; border-radius: var(--r-sm);
  display: flex; gap: 8px; word-break: break-word;
  border-left: 2px solid transparent;
}
.log-line + .log-line { margin-top: 2px; }
.log-line.info { color: var(--text-2); }
.log-line.warning { color: var(--warning); background: var(--warning-soft); border-left-color: var(--warning); }
.log-line.error { color: var(--danger); background: var(--danger-soft); border-left-color: var(--danger); }
.log-line.log-truncated, .log-line.log-hint { color: var(--text-3); font-style: italic; justify-content: center; }

/* ---------- 空状态 ---------- */
.empty-state {
  display: flex; flex-direction: column; align-items: center; gap: 6px;
  padding: var(--sp-8) var(--sp-4); text-align: center; color: var(--text-3);
}
.empty-state svg { width: 32px; height: 32px; opacity: .55; margin-bottom: 4px; }
.empty-state strong { display: block; font-size: var(--fs-14); color: var(--text-2); font-weight: 600; }
.empty-state span { font-size: var(--fs-12); color: var(--text-3); }
td .empty-state { padding: var(--sp-6) var(--sp-4); }
/* ---------- 概览指标 ---------- */
.overview-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--sp-3); }
.metric {
  padding: var(--sp-4);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  background: var(--surface-2);
  display: flex; flex-direction: column; gap: 4px;
}
.metric-head { display: flex; align-items: center; justify-content: space-between; font-size: var(--fs-12); color: var(--text-3); font-weight: 550; }
.metric-icon {
  width: 22px; height: 22px; border-radius: 6px; display: grid; place-items: center;
  font-size: var(--fs-12); background: var(--border); color: var(--text-2);
}
.metric b { font-size: var(--fs-28); font-weight: 700; line-height: 1.1; color: var(--text-1); font-variant-numeric: tabular-nums; }
.metric small { font-size: var(--fs-11); color: var(--text-3); }
.metric.primary .metric-icon { background: var(--brand-soft); color: var(--brand); }
.metric.success .metric-icon { background: var(--success-soft); color: var(--success); }
.metric.success b { color: var(--success); }
.metric.danger .metric-icon { background: var(--danger-soft); color: var(--danger); }
.metric.danger b { color: var(--danger); }
.metric.amber .metric-icon { background: var(--warning-soft); color: var(--warning); }
.metric-wide { grid-column: 1 / -1; }
.metric-wide b { font-size: var(--fs-16); font-weight: 640; }
.overview-progress { height: 6px; border-radius: var(--r-pill); background: var(--border); overflow: hidden; margin: 6px 0 2px; }
.overview-progress > span { display: block; height: 100%; border-radius: inherit; background: var(--brand); transition: width var(--dur-3) var(--ease); }
.metric-wide.indigo .overview-progress > span { background: var(--brand); }
.metric-wide.success .overview-progress > span { background: var(--success); }
.metric-wide.danger .overview-progress > span { background: var(--danger); }
.metric-wide.amber .overview-progress > span { background: var(--warning); }
.overview-meta { display: flex; flex-wrap: wrap; gap: var(--sp-2) var(--sp-4); font-size: var(--fs-11); color: var(--text-3); }
/* ---------- 模态框 ---------- */
.modal {
  position: fixed; inset: 0; z-index: 2000;
  display: none; align-items: center; justify-content: center;
  padding: var(--sp-4);
  background: var(--overlay);
  backdrop-filter: blur(3px);
}
.modal.show { display: flex; animation: fadeIn var(--dur-2) var(--ease); }
@keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
.modal-card {
  width: min(560px, 100%); max-height: min(86vh, 760px);
  display: flex; flex-direction: column;
  background: var(--surface);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-xl);
  box-shadow: var(--shadow-lg);
  overflow: hidden;
  animation: modalPop var(--dur-3) var(--ease);
}
.modal-card.modal-wide { width: min(760px, 100%); }
@keyframes modalPop { from { opacity: 0; transform: translateY(12px) scale(.98); } to { opacity: 1; transform: none; } }
.modal-head {
  display: flex; align-items: center; gap: var(--sp-3);
  padding: var(--sp-4) var(--sp-5);
  border-bottom: 1px solid var(--border);
}
.modal-head h3 { font-size: var(--fs-16); font-weight: 640; flex: 1; min-width: 0; }
.modal-head .modal-sub { font-size: var(--fs-12); color: var(--text-3); font-weight: 400; }
.modal-close {
  width: 32px; height: 32px; border-radius: var(--r-sm); flex: none;
  display: grid; place-items: center; color: var(--text-3);
  transition: all var(--dur-2) var(--ease);
}
.modal-close:hover { background: var(--surface-2); color: var(--text-1); }
.modal-body { padding: var(--sp-5); overflow-y: auto; display: flex; flex-direction: column; gap: var(--sp-4); }
.modal-foot {
  display: flex; align-items: center; justify-content: flex-end; gap: var(--sp-2);
  padding: var(--sp-4) var(--sp-5);
  border-top: 1px solid var(--border); background: var(--surface-2);
}
.modal-foot .spacer { flex: 1; }

/* ---------- 目录浏览 ---------- */
.dir-path { font-family: var(--font-mono); font-size: var(--fs-12); color: var(--text-2); padding: 8px var(--sp-3); background: var(--surface-2); border: 1px solid var(--border); border-radius: var(--r-sm); word-break: break-all; }
.dir-list { display: flex; flex-direction: column; gap: 4px; max-height: 46vh; overflow-y: auto; }
.dir-item {
  display: flex; align-items: center; justify-content: space-between; gap: var(--sp-3);
  padding: 10px var(--sp-3); border-radius: var(--r-sm);
  border: 1px solid transparent; cursor: pointer;
  transition: all var(--dur-1) var(--ease);
}
.dir-item:hover { background: var(--surface-2); }
.dir-item.selected { background: var(--brand-soft); border-color: var(--brand-line); }
.dir-item strong { font-weight: 550; font-size: var(--fs-13); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dir-item span { font-size: var(--fs-11); color: var(--text-3); flex: none; }
/* ---------- 设置中心 ---------- */
.settings-group { display: flex; flex-direction: column; gap: var(--sp-3); }
.settings-group-title {
  display: flex; align-items: center; gap: var(--sp-2);
  font-size: var(--fs-12); font-weight: 600; color: var(--text-2);
  text-transform: uppercase; letter-spacing: .5px;
}
.settings-icon { width: 22px; height: 22px; display: grid; place-items: center; color: var(--brand); flex: none; }
.settings-icon svg { width: 16px; height: 16px; }
.settings-group + .settings-group { padding-top: var(--sp-4); border-top: 1px solid var(--border); }
.settings-tools { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: var(--sp-2); }
.settings-tools > button {
  padding: 10px var(--sp-3); border-radius: var(--r-md);
  border: 1px solid var(--border); background: var(--surface-2); color: var(--text-1);
  font-size: var(--fs-13); font-weight: 540; text-align: left;
  transition: all var(--dur-2) var(--ease);
}
.settings-tools > button:hover { border-color: var(--brand-line); background: var(--brand-soft); color: var(--brand); }
.theme-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(96px, 1fr)); gap: var(--sp-2); }
.theme-swatch {
  position: relative; padding: 10px; border-radius: var(--r-md);
  border: 1px solid var(--border); background: var(--surface-2);
  display: flex; flex-direction: column; gap: 8px; align-items: flex-start;
  transition: all var(--dur-2) var(--ease);
}
.theme-swatch:hover { border-color: var(--border-strong); transform: translateY(-1px); }
.theme-swatch.active { border-color: var(--brand); box-shadow: 0 0 0 2px var(--ring); }
.theme-preview { width: 100%; height: 34px; border-radius: var(--r-sm); border: 1px solid var(--border); display: flex; overflow: hidden; }
.theme-preview i { flex: 1; }
.theme-swatch span { font-size: var(--fs-12); font-weight: 550; color: var(--text-1); }
.theme-selected {
  position: absolute; top: 6px; right: 6px; width: 18px; height: 18px; border-radius: 50%;
  display: none; place-items: center; background: var(--brand); color: var(--brand-ink);
}
.theme-selected svg { width: 12px; height: 12px; }
.theme-swatch.active .theme-selected { display: grid; }

/* ---------- UI refinement: quiet, structured product surface ---------- */
.brand-logo {
  background: var(--brand);
  border-radius: 11px;
  box-shadow: 0 6px 16px color-mix(in srgb, var(--brand) 20%, transparent);
}
.global-topbar {
  background: var(--surface);
  box-shadow: 0 1px 0 color-mix(in srgb, var(--border) 65%, transparent);
}
.workspace {
  grid-template-columns: minmax(0, 1.12fr) minmax(420px, .88fr);
  gap: clamp(var(--sp-5), 2vw, var(--sp-8));
}
.section {
  border-color: color-mix(in srgb, var(--border) 88%, var(--text-3));
  box-shadow: 0 2px 8px color-mix(in srgb, var(--bg) 24%, transparent);
}
#configForm > .section:first-child {
  border-color: var(--brand-line);
  box-shadow: 0 8px 24px color-mix(in srgb, var(--brand) 7%, transparent);
}
.section-title { margin-bottom: var(--sp-5); }
.section-title h2 { font-size: var(--fs-16); font-weight: 680; letter-spacing: 0; }
.section-title .section-hint { margin-left: 2px; }
.field > label, .field-label { color: var(--text-1); font-weight: 600; }
.field-note, .field-hint, .hint, .modal-note { line-height: 1.55; }
input[type="text"], input[type="url"], input[type="search"], input:not([type]), textarea,
.custom-select-trigger {
  min-height: 42px;
  background: color-mix(in srgb, var(--surface-2) 86%, var(--surface));
}
textarea { min-height: 104px; }
.btn-primary, .btn-indigo {
  box-shadow: 0 5px 14px color-mix(in srgb, var(--brand) 18%, transparent);
}
.btn-secondary, .quiet-button {
  background: var(--surface);
  border-color: var(--border-strong);
}
.btn-amber, .btn-red {
  background: transparent;
  border-color: color-mix(in srgb, currentColor 34%, var(--border));
}
.run-panel {
  background: color-mix(in srgb, var(--surface) 92%, var(--brand-soft));
  border-color: var(--brand-line);
}
.run-actions #addQueueBtn {
  min-height: 42px;
  background: var(--brand);
  color: var(--brand-ink);
  border-color: var(--brand);
  box-shadow: 0 5px 14px color-mix(in srgb, var(--brand) 18%, transparent);
}
.run-actions #addQueueBtn:hover { background: var(--brand-hover); }
.table-wrap, .log-box { border-color: color-mix(in srgb, var(--border) 88%, var(--text-3)); }
.data-table thead th { text-transform: none; letter-spacing: 0; font-size: var(--fs-12); }
.data-table tbody td { padding-block: 12px; }
.queue-row-actions button { background: var(--surface); }
.settings-tools { display: grid; grid-template-columns: repeat(auto-fit, minmax(168px, 1fr)); }
.settings-tools > button { min-height: 42px; display: flex; align-items: center; }
.theme-grid { grid-template-columns: repeat(auto-fit, minmax(118px, 1fr)); gap: var(--sp-2); }
.theme-swatch { padding: 8px; gap: 6px; background: var(--surface); }
.theme-swatch-preview { height: 24px; }
.theme-swatch span { font-size: var(--fs-12); }
.search-results { width: min(680px, calc(100vw - 32px)); padding: var(--sp-4); }
.search-result {
  display: grid;
  grid-template-columns: 56px minmax(0, 1fr) 80px;
  gap: var(--sp-3);
  align-items: center;
  padding: var(--sp-3) var(--sp-2);
}
.search-result > span:not(.search-result-action) { min-width: 0; }
.search-result-title { font-size: var(--fs-15); }
.search-result-desc { -webkit-line-clamp: 3; }
.search-result-action {
  align-self: center;
  min-height: 44px;
  border-left: 1px solid var(--border);
  border-radius: 0;
  padding-left: var(--sp-3);
}
.search-result-action:hover { color: var(--brand-hover); }
.toast { border-radius: var(--r-md); }

@supports not (background: color-mix(in srgb, white, black)) {
  .global-topbar { background: var(--surface); }
  input[type="text"], input[type="url"], input[type="search"], input:not([type]), textarea,
  .custom-select-trigger { background: var(--surface-2); }
}

/* ---------- 搜索结果对话框 ---------- */
.search-backdrop { position: fixed; inset: 0; z-index: 2500; background: var(--overlay); backdrop-filter: blur(3px); }
.search-results {
  position: fixed; z-index: 2600; top: 50%; left: 50%; transform: translate(-50%, -50%);
  width: min(560px, calc(100vw - 32px)); max-height: 78vh; overflow-y: auto;
  background: var(--surface); border: 1px solid var(--border-strong);
  border-radius: var(--r-xl); box-shadow: var(--shadow-lg); padding: var(--sp-3);
}
.search-dialog-head { display: flex; align-items: center; gap: var(--sp-2); padding: var(--sp-2) var(--sp-2) var(--sp-3); border-bottom: 1px solid var(--border); margin-bottom: var(--sp-2); }
.search-dialog-head strong { font-size: var(--fs-14); flex: 1; }
.search-count { font-size: var(--fs-12); color: var(--text-3); }
.search-dialog-close { width: 28px; height: 28px; border-radius: var(--r-sm); display: grid; place-items: center; color: var(--text-3); }
.search-dialog-close:hover { background: var(--surface-2); color: var(--text-1); }
.search-result {
  width: 100%; display: flex; gap: var(--sp-3); align-items: flex-start;
  padding: var(--sp-3); border-radius: var(--r-md); text-align: left;
  border: 1px solid transparent;
  transition: all var(--dur-1) var(--ease);
}
.search-result + .search-result { margin-top: 4px; }
.search-result:hover { background: var(--surface-2); border-color: var(--border); }
.search-result > img {
  width: 52px; height: 52px; border-radius: var(--r-sm); object-fit: cover; flex: none;
  background: var(--surface-2); border: 1px solid var(--border); font-size: var(--fs-11); color: var(--text-3);
}
.search-result > span { display: flex; flex-direction: column; gap: 3px; min-width: 0; flex: 1; }
.search-result > span:not(.search-result-action) { flex: 1 1 auto; }
.search-result-title { font-size: var(--fs-14); font-weight: 600; color: var(--text-1); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.search-result-meta { font-size: var(--fs-12); color: var(--text-3); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.search-result-desc {
  font-size: var(--fs-12); color: var(--text-2); line-height: 1.5;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}
.search-result-tags { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 2px; }
.search-result-tag { font-size: var(--fs-11); padding: 1px 8px; border-radius: var(--r-pill); background: var(--brand-soft); color: var(--brand); }
.search-result-action {
  display: inline-flex; align-items: center; justify-content: center; gap: 2px;
  align-self: stretch; flex: 0 0 72px; margin-left: auto;
  flex-direction: column; font-size: var(--fs-12); font-weight: 600; color: var(--brand);
}
.search-result-action svg { width: 15px; height: 15px; }
.author-result-avatar {
  width: 44px; height: 44px; border-radius: 50%; flex: none;
  display: grid; place-items: center; font-size: var(--fs-16); font-weight: 700; color: #fff;
}
.search-empty { display: flex; flex-direction: column; align-items: center; gap: 8px; padding: var(--sp-8); color: var(--text-3); }
.search-empty svg { width: 30px; height: 30px; opacity: .6; }
.search-pagination { display: flex; align-items: center; justify-content: space-between; gap: var(--sp-2); padding: var(--sp-3) var(--sp-2) var(--sp-1); margin-top: var(--sp-2); border-top: 1px solid var(--border); }

/* ---------- 提示 Toast ---------- */
.toast {
  position: fixed; z-index: 4000; left: 50%; bottom: 28px; transform: translate(-50%, 20px);
  max-width: min(440px, calc(100vw - 32px));
  padding: 11px var(--sp-5);
  background: var(--elevated); color: var(--text-1);
  border: 1px solid var(--border-strong); border-radius: var(--r-pill);
  box-shadow: var(--shadow-lg); font-size: var(--fs-13); font-weight: 540;
  opacity: 0; pointer-events: none; transition: opacity var(--dur-2) var(--ease), transform var(--dur-2) var(--ease);
}
.toast.show { opacity: 1; transform: translate(-50%, 0); }
/* ---------- 滚动条 ---------- */
* { scrollbar-width: thin; scrollbar-color: var(--border-strong) transparent; }
*::-webkit-scrollbar { width: 10px; height: 10px; }
*::-webkit-scrollbar-thumb { background: var(--border-strong); border-radius: var(--r-pill); border: 2px solid transparent; background-clip: padding-box; }
*::-webkit-scrollbar-thumb:hover { background: var(--text-3); background-clip: padding-box; }
*::-webkit-scrollbar-track { background: transparent; }

/* ---------- 补充组件 ---------- */
.req { color: var(--text-3); font-weight: 400; font-size: var(--fs-12); }
.req-mark { color: var(--danger); font-weight: 600; margin-left: 1px; }
.field-hint { font-size: var(--fs-12); color: var(--text-3); }
.field-row-actions { display: flex; align-items: center; gap: var(--sp-3); flex-wrap: wrap; }
.input-row { display: flex; gap: var(--sp-2); align-items: stretch; }
.input-row > input { flex: 1; min-width: 0; }
.input-row > .btn-secondary { flex: 0 0 auto; min-width: 82px; }
.input-row-source { display: grid; grid-template-columns: minmax(120px, 0.9fr) minmax(0, 1.6fr); gap: var(--sp-2); }
.input-row-actions { margin-top: var(--sp-2); }
.input-row-actions .btn-primary,
.input-row-actions .btn-secondary { flex: 1 1 0; min-width: 0; }
.input-row-actions .btn-secondary {
  color: var(--text-2);
  background: var(--surface-2);
  border-color: var(--border);
}
.input-row-actions .btn-secondary:hover {
  color: var(--brand);
  border-color: var(--brand-line);
  background: var(--brand-soft);
}
.input-row-actions .btn-primary,
.input-row-actions .btn-secondary { min-height: 42px; }
.tag-input { margin-top: var(--sp-2); }
.series-inline { display: flex; gap: var(--sp-2); align-items: flex-start; }
.series-inline .series-pool { flex: 1; min-height: 38px; padding: 7px 8px; border: 1px dashed var(--border-strong); border-radius: var(--r-md); }
.series-inline .btn-secondary {
  flex: 0 0 auto;
  min-height: 38px;
  padding-inline: var(--sp-3);
  color: var(--text-2);
  background: var(--surface-2);
  border-color: var(--border);
}
.series-inline .btn-secondary:hover {
  color: var(--brand);
  background: var(--brand-soft);
  border-color: var(--brand-line);
}
.cover-desc { min-width: 0; }
.cover-desc textarea { min-height: 168px; height: 100%; }
.run-panel-head { align-items: flex-start; }
.run-panel-title { display: flex; flex-direction: column; gap: 2px; }
.run-panel-title strong { font-size: var(--fs-15); font-weight: 640; }
.run-panel-sub { font-size: var(--fs-12); color: var(--text-3); }
.selection-copy { font-size: var(--fs-12); color: var(--text-3); white-space: nowrap; }
.selection-copy b { color: var(--brand); font-weight: 650; }
.btn-block { grid-column: 1 / -1; }
.run-actions #addQueueBtn {
  min-height: 42px;
  color: var(--brand);
  background: var(--brand-soft);
  border-color: var(--brand-line);
  box-shadow: none;
}
.run-actions #addQueueBtn:hover { background: color-mix(in srgb, var(--brand) 20%, transparent); }
.run-actions #clearBtn {
  min-height: 36px;
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  padding-top: var(--sp-3);
}
.panel-heading { font-size: var(--fs-13); font-weight: 620; color: var(--text-1); }
.table-scroll { overflow: auto; max-height: 52vh; border-radius: var(--r-md); }
.data-table .col-check { width: 34px; }
.data-table .col-idx { width: 36px; color: var(--text-3); }
.data-table .col-ops { width: 74px; }
.dir-path { font-family: var(--mono, ui-monospace, monospace); font-size: var(--fs-12); color: var(--text-2); padding: 8px var(--sp-3); background: var(--surface-2); border-radius: var(--r-md); word-break: break-all; }
.dir-list { display: flex; flex-direction: column; gap: 2px; max-height: 46vh; overflow: auto; }
.dir-item { display: flex; align-items: center; gap: var(--sp-2); padding: 9px var(--sp-3); border-radius: var(--r-md); cursor: pointer; color: var(--text-1); font-size: var(--fs-13); border: 1px solid transparent; }
.dir-item:hover { background: var(--surface-2); border-color: var(--border); }
.cookie-input { min-height: 84px; resize: vertical; }
.modal-note { font-size: var(--fs-12); color: var(--text-3); }
.settings-tools { display: flex; flex-wrap: wrap; gap: var(--sp-2); }
.settings-tools .btn-secondary { flex: 0 1 auto; }
.theme-swatch-preview { width: 100%; height: 30px; border-radius: var(--r-sm); border: 1px solid var(--border); display: block; }
.sw-dark { background: linear-gradient(120deg, #0d1420 0 45%, #3f7dff 45% 72%, #1a2333 72%); }
.sw-light { background: linear-gradient(120deg, #f4f6fa 0 45%, #2f6bf0 45% 72%, #e2e8f1 72%); }
.sw-linen { background: linear-gradient(120deg, #f6f2ea 0 45%, #b5761e 45% 72%, #e7ddcc 72%); }
.sw-mint { background: linear-gradient(120deg, #eef5f2 0 45%, #0e9a72 45% 72%, #d7e7e0 72%); }
.sw-rose { background: linear-gradient(120deg, #f9eff1 0 45%, #d24169 45% 72%, #eed7dd 72%); }
.sw-ocean { background: linear-gradient(120deg, #0a1420 0 45%, #38a7e0 45% 72%, #16293b 72%); }
.sw-aurora { background: linear-gradient(120deg, #12101f 0 45%, #926df6 45% 72%, #221d3c 72%); }
.sw-jade { background: linear-gradient(120deg, #0c1a16 0 45%, #2fbf8f 45% 72%, #16302a 72%); }
.sw-graphite { background: linear-gradient(120deg, #16181c 0 45%, #8b93a3 45% 72%, #262a30 72%); }
[hidden] { display: none !important; }

/* ---------- 响应式 ---------- */
@media (max-width: 1180px) {
  .workspace { grid-template-columns: 1fr; }
  .column-right { position: static; top: auto; }
}
@media (max-width: 720px) {
  :root { --topbar-h: 56px; }
  .workspace { padding: var(--sp-3) var(--sp-3) var(--sp-6); gap: var(--sp-3); }
  .global-topbar { gap: var(--sp-2); padding: 0 var(--sp-3); }
  .brand-sub { display: none; }
  .topbar-status { display: none; }
  .field-grid { grid-template-columns: 1fr; }
  .cover-row { grid-template-columns: 112px minmax(0, 1fr); }
  .cover-box { width: 112px; height: 112px; }
  .overview-grid { grid-template-columns: 1fr 1fr; }
  .run-actions { grid-template-columns: 1fr; }
  .section { padding: var(--sp-4); }
  .section-title { align-items: flex-start; }
  .section-title .section-hint { display: none; }
  .search-results { padding: var(--sp-3); }
  .search-result { grid-template-columns: 48px minmax(0, 1fr) 58px; gap: var(--sp-2); }
  .search-result > img { width: 48px; height: 48px; }
  .search-result-title { font-size: var(--fs-14); }
  .search-result-action { padding-left: var(--sp-2); }
  .settings-tools { grid-template-columns: 1fr 1fr; }
  .section-toggle { display: grid; cursor: pointer; }
  .section.mobile-collapsible > :not(.section-title) { display: none; }
  .section.mobile-collapsible.mobile-expanded > :not(.section-title) { display: revert; }
  .section.mobile-collapsible.mobile-expanded > .field-grid { display: grid; }
  .section.mobile-collapsible .section-title { cursor: pointer; margin-bottom: 0; }
  .section.mobile-collapsible.mobile-expanded .section-title { margin-bottom: var(--sp-4); }
  .modal { padding: 0; align-items: flex-end; }
  .modal-card { width: 100%; max-height: 92vh; border-radius: var(--r-xl) var(--r-xl) 0 0; }
  .toast { bottom: 16px; }
}
@media (max-width: 420px) {
  .overview-grid { grid-template-columns: 1fr; }
  .settings-tools { grid-template-columns: 1fr; }
  .search-result { grid-template-columns: 44px minmax(0, 1fr); }
  .search-result > img { width: 44px; height: 44px; }
  .search-result-action { grid-column: 2; justify-self: start; min-height: 30px; border-left: none; padding-left: 0; flex-direction: row; }
  .data-table thead th:nth-child(4), .data-table tbody td:nth-child(4) { display: none; }
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation-duration: .001ms !important; animation-iteration-count: 1 !important; transition-duration: .001ms !important; }
}
  </style>
</head>
<body>
  <div class="app-shell">
    <header class="global-topbar">
      <div class="brand">
        <span class="brand-logo" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 7.8c3-1.2 5.8-.7 9 1.3 3.2-2 6-2.5 9-1.3v10.1c-3.1-1.1-6-.7-9 1.3-3-2-5.9-2.4-9-1.3V7.8Z"/><path d="M12.5 9.1v10.1"/><path d="M14.8 12.9c.9-2.5 1.8 2.5 2.7 0s1.8 2.5 2.7 0"/></svg>
        </span>
        <div class="brand-text">
          <span class="brand-title">AudioMeta Nexus</span>
          <span class="brand-sub">有声书元数据处理台</span>
        </div>
      </div>
      <div class="topbar-spacer"></div>
      <div class="topbar-status" role="status" aria-live="polite">
        <span class="state-dot" id="stateDot"></span>
        <div class="topbar-status-body">
          <span id="stateText">等待就绪</span>
          <div class="topbar-status-meta">
            <b id="percentText">0%</b>
            <span>队列 <b id="queueCountText">0/0</b></span>
          </div>
        </div>
      </div>
      <div class="theme-cluster" aria-label="明暗主题切换">
        <span class="theme-symbol" aria-hidden="true"></span>
        <button type="button" class="icon-button theme-toggle" id="themeToggleBtn" title="切换明暗主题" aria-label="切换明暗主题"></button>
        <span class="theme-symbol" aria-hidden="true"></span>
      </div>
      <button type="button" class="icon-button" id="settingsBtn" title="设置中心" aria-label="打开设置中心"></button>
    </header>

    <main class="workspace">
      <section class="column left" aria-label="元数据编辑区">
        <form id="configForm" autocomplete="off">
          <div class="section">
            <div class="section-title"><span class="section-icon" aria-hidden="true"></span><h2>核心来源</h2><span class="section-hint">选择目录并抓取平台元数据</span></div>
            <div class="field col-span-2">
              <label class="field-label" for="input_folder">音频目录<span class="req">（/data 下的专辑文件夹）</span></label>
              <div class="input-row">
                <input id="input_folder" name="input_folder" placeholder="/data/专辑目录" />
                <button type="button" class="btn-secondary" id="browseBtn">浏览</button>
              </div>
            </div>
            <div class="field col-span-2">
              <label class="field-label" for="api_id">平台专辑 ID / 书名 / 分享链接</label>
              <div class="input-row input-row-source">
                <select name="api_source" aria-label="数据来源平台"></select>
                <input id="api_id" name="api_id" placeholder="输入 ID、书名或分享链接" />
              </div>
              <div class="input-row input-row-actions">
                <button type="button" class="btn-primary" id="fetchBtn">获取元数据</button>
                <button type="button" class="btn-secondary" id="searchTitleBtn">按书名搜索</button>
              </div>
            </div>
            <div id="titleSearchBackdrop" class="search-backdrop" hidden></div>
            <div id="titleSearchResults" class="search-results" role="dialog" aria-modal="true" aria-label="书名搜索结果" hidden></div>
            <div id="authorSearchResults" class="search-results" role="dialog" aria-modal="true" aria-label="作者搜索结果" hidden></div>
          </div>

          <div class="section">
            <div class="section-title"><span class="section-icon" aria-hidden="true"></span><h2>元数据档案</h2><span class="section-hint">标题与创作人员</span></div>
            <div class="field-grid">
              <div class="field"><label class="field-label" for="f_title">专辑标题 <span class="req-mark">*</span></label><input id="f_title" name="title" placeholder="请输入专辑标题" /></div>
              <div class="field"><label class="field-label" for="f_subtitle">副标题</label><input id="f_subtitle" name="subtitle" placeholder="可选" /></div>
            </div>
            <div class="field-grid">
              <div class="field">
                <label class="field-label">原著作者 <span class="req-mark">*</span></label>
                <div class="chips editable" id="authorPool" tabindex="-1"></div>
                <div class="field-row-actions"><button type="button" class="field-mini-action" id="fetchAuthorBtn">获取作者</button><span class="field-hint">回车添加，点击气泡删除</span></div>
                <input type="hidden" name="author" />
              </div>
              <div class="field">
                <label class="field-label">演播艺术家 <span class="req-mark">*</span></label>
                <div class="chips editable" id="anchorPool" tabindex="-1"></div>
                <span class="field-hint">回车添加，点击气泡删除</span>
                <input type="hidden" name="anchor" />
              </div>
            </div>
          </div>

          <div class="section">
            <div class="section-title"><span class="section-icon" aria-hidden="true"></span><h2>规格与归档</h2><span class="section-hint">发布信息、格式与系列</span></div>
            <div class="field-grid">
              <div class="field"><label class="field-label">发布平台 <span class="req-mark">*</span></label><select name="platform" aria-label="发布平台"></select></div>
              <div class="field"><label class="field-label">专辑分类 <span class="req-mark">*</span></label><select name="category" aria-label="专辑分类"></select></div>
              <div class="field"><label class="field-label">专辑状态 <span class="req-mark">*</span></label><select name="finished" aria-label="专辑状态"></select></div>
              <div class="field"><label class="field-label" for="f_year">发布年份 <span class="req-mark">*</span></label><input id="f_year" name="year" placeholder="例如 2024" /></div>
              <div class="field"><label class="field-label">目标格式</label><select name="target_format" aria-label="目标格式"></select></div>
              <div class="field"><label class="field-label">比特率</label><select name="bitrate" aria-label="比特率"></select></div>
            </div>
            <div class="field col-span-2">
              <label class="field-label">制作团队<span class="req">（文件夹后缀）</span></label>
              <div class="chips editable" id="teamPool" tabindex="-1"></div>
              <span class="field-hint">回车添加，点击气泡删除</span>
              <input type="hidden" name="team" />
            </div>
            <div class="field col-span-2">
              <label class="field-label">系列档案<span class="req">（可加入多个系列）</span></label>
              <div class="series-inline">
                <div class="chips series-pool" id="seriesPool"></div>
                <button type="button" class="btn-secondary" id="openSeriesBtn">添加系列</button>
              </div>
              <input type="hidden" name="series_name" />
              <input type="hidden" name="series_number" />
            </div>
            <div class="field col-span-2">
              <label class="field-label" for="tagInput">专辑标签池</label>
              <div class="chips" id="tagPool"></div>
              <input id="tagInput" class="tag-input" placeholder="输入新标签，回车添加" />
            </div>
          </div>

          <div class="section">
            <div class="section-title"><span class="section-icon" aria-hidden="true"></span><h2>视觉与内容</h2><span class="section-hint">封面与内容简介</span></div>
            <div class="cover-row">
              <div class="cover-side">
                <div class="cover-box">
                  <img id="coverImg" alt="专辑封面预览" />
                  <span class="cover-empty" id="coverEmpty">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect width="18" height="18" x="3" y="3" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.1-3.1a2 2 0 0 0-2.8 0L6 21"/></svg>
                    暂无封面
                  </span>
                  <button type="button" class="cover-change-button" id="coverChangeBtn">更换封面</button>
                </div>
                <div class="cover-meta" id="coverMeta">--</div>
                <input type="file" id="coverFileInput" accept="image/jpeg,image/png,image/webp,image/gif" hidden />
                <input type="hidden" name="manual_cover_path" />
              </div>
              <div class="field cover-desc">
                <label class="field-label" for="manual_desc">内容简介</label>
                <textarea id="manual_desc" name="manual_desc" placeholder="填写专辑简介内容..."></textarea>
              </div>
            </div>
          </div>
        </form>
      </section>

      <section class="column column-right" aria-label="任务处理区">
        <div class="section run-panel">
          <div class="run-panel-head">
            <div class="run-panel-title"><strong>处理控制</strong><span class="run-panel-sub">加入队列后开始批量处理</span></div>
            <span class="selection-copy">已选 <b id="selectedCountText">0</b> 项</span>
          </div>
          <div class="progress-track"><span id="progressBar"></span></div>
          <div class="run-actions">
            <button type="button" class="btn-secondary btn-block" id="addQueueBtn">加入队列</button>
            <button type="button" class="btn-primary" id="startQueueBtn">开始处理</button>
            <button type="button" class="btn-red" id="stopBtn">停止</button>
            <button type="button" class="btn-ghost btn-block" id="clearBtn">清空编辑区</button>
          </div>
        </div>

        <div class="section queue-console">
          <div class="tab-bar" role="tablist">
            <button type="button" class="tab active" data-tab="queue" role="tab">任务队列</button>
            <button type="button" class="tab" data-tab="log" role="tab">处理日志</button>
            <button type="button" class="tab" data-tab="failed" role="tab">失败任务</button>
            <button type="button" class="tab" data-tab="overview" role="tab">数据概览</button>
          </div>

          <div class="tab-panel active" id="panel-queue" role="tabpanel">
            <div class="queue-actions tab-toolbar">
              <button type="button" class="btn-secondary needs-selection" id="editQueueBtn">编辑选中</button>
              <button type="button" class="btn-amber needs-selection" id="removeQueueBtn">移除选中</button>
              <button type="button" class="btn-red" id="clearQueueBtn">清空队列</button>
            </div>
            <div class="table-scroll">
              <table class="data-table">
                <thead><tr><th class="col-check"></th><th class="col-idx">#</th><th>专辑标题</th><th>平台</th><th>进度</th><th>状态</th><th class="col-ops">操作</th></tr></thead>
                <tbody id="queueBody"></tbody>
              </table>
            </div>
          </div>

          <div class="tab-panel" id="panel-log" role="tabpanel">
            <div class="tab-toolbar">
              <strong class="panel-heading">实时处理日志</strong>
              <span class="spacer"></span>
              <button type="button" class="btn-ghost" id="clearLogBtn">清空日志</button>
            </div>
            <div class="log-box" id="logBox"></div>
          </div>

          <div class="tab-panel" id="panel-failed" role="tabpanel">
            <div class="table-scroll">
              <table class="data-table"><thead><tr><th>文件</th><th>错误信息</th></tr></thead><tbody id="failedBody"></tbody></table>
            </div>
          </div>

          <div class="tab-panel" id="panel-overview" role="tabpanel">
            <div class="overview-grid" id="overviewBox"></div>
          </div>
        </div>
      </section>
    </main>
  </div>

  <div class="modal" id="dirModal">
    <div class="modal-card">
      <div class="modal-head"><h3>选择专辑目录</h3><button type="button" class="modal-close" id="closeDirBtn" aria-label="关闭"></button></div>
      <div class="modal-body">
        <div class="dir-path" id="dirPath"></div>
        <div class="dir-list" id="dirList"></div>
      </div>
      <div class="modal-foot">
        <button type="button" class="btn-secondary" id="dirUpBtn">返回上级</button>
        <span class="spacer"></span>
        <button type="button" class="btn-primary" id="chooseDirBtn">选择此目录</button>
      </div>
    </div>
  </div>

  <div class="modal" id="cookieModal">
    <div class="modal-card">
      <div class="modal-head"><h3>平台 Cookie</h3><button type="button" class="modal-close" id="closeCookieBtn" aria-label="关闭"></button></div>
      <div class="modal-body">
        <div class="field"><label class="field-label" for="qidianCookie">起点听书 Cookie</label><textarea id="qidianCookie" class="cookie-input"></textarea></div>
        <div class="field"><label class="field-label" for="neteaseCookie">网易云听书 Cookie</label><textarea id="neteaseCookie" class="cookie-input"></textarea></div>
        <div class="field"><label class="field-label" for="kuwoCookie">酷我听书 Cookie<span class="req">（可选）</span></label><textarea id="kuwoCookie" class="cookie-input"></textarea></div>
      </div>
      <div class="modal-foot"><span class="modal-note">Cookie 保存到容器配置目录</span><span class="spacer"></span><button type="button" class="btn-primary" id="saveCookieBtn">保存</button></div>
    </div>
  </div>

  <div class="modal" id="blacklistModal">
    <div class="modal-card">
      <div class="modal-head"><h3>标签黑名单</h3><button type="button" class="modal-close" id="closeBlacklistBtn" aria-label="关闭"></button></div>
      <div class="modal-body">
        <div class="modal-note" id="blacklistPath"></div>
        <div class="chips" id="blacklistPool"></div>
        <input id="blacklistInput" placeholder="输入规则或正则，回车添加" />
      </div>
      <div class="modal-foot"><span class="modal-note">支持正则；点击气泡删除规则</span><span class="spacer"></span><button type="button" class="btn-primary" id="saveBlacklistBtn">保存</button></div>
    </div>
  </div>

  <div class="modal" id="seriesModal">
    <div class="modal-card">
      <div class="modal-head"><h3>添加系列档案</h3><button type="button" class="modal-close" id="closeSeriesBtn" aria-label="关闭"></button></div>
      <div class="modal-body">
        <div class="field"><label class="field-label" for="seriesNameInput">系列名</label><input id="seriesNameInput" placeholder="例如：庆余年" /></div>
        <div class="field"><label class="field-label" for="seriesNumberInput">序号<span class="req">（可选）</span></label><input id="seriesNumberInput" placeholder="例如：1，可留空" /></div>
      </div>
      <div class="modal-foot"><span class="modal-note">同一本书可添加多个系列</span><span class="spacer"></span><button type="button" class="btn-primary" id="saveSeriesBtn">添加系列</button></div>
    </div>
  </div>

  <div class="modal" id="settingsModal">
    <div class="modal-card modal-wide">
      <div class="modal-head"><h3>设置中心<span class="modal-sub">数据源、配置与维护工具</span></h3><button type="button" class="modal-close" id="closeSettingsBtn" aria-label="关闭"></button></div>
      <div class="modal-body settings-body">
        <div class="settings-group">
          <div class="settings-group-title"><span class="settings-icon" aria-hidden="true"></span>外观主题</div>
          <div class="theme-grid" id="themeGrid">
            <button type="button" class="theme-swatch" data-theme-option="dark"><span class="theme-swatch-preview sw-dark"></span><span>墨夜</span><span class="theme-selected" aria-hidden="true"></span></button>
            <button type="button" class="theme-swatch" data-theme-option="light"><span class="theme-swatch-preview sw-light"></span><span>素雪</span><span class="theme-selected" aria-hidden="true"></span></button>
            <button type="button" class="theme-swatch" data-theme-option="linen"><span class="theme-swatch-preview sw-linen"></span><span>茶白</span><span class="theme-selected" aria-hidden="true"></span></button>
            <button type="button" class="theme-swatch" data-theme-option="mint"><span class="theme-swatch-preview sw-mint"></span><span>青瓷</span><span class="theme-selected" aria-hidden="true"></span></button>
            <button type="button" class="theme-swatch" data-theme-option="rose"><span class="theme-swatch-preview sw-rose"></span><span>胭脂</span><span class="theme-selected" aria-hidden="true"></span></button>
            <button type="button" class="theme-swatch" data-theme-option="ocean"><span class="theme-swatch-preview sw-ocean"></span><span>黛蓝</span><span class="theme-selected" aria-hidden="true"></span></button>
            <button type="button" class="theme-swatch" data-theme-option="aurora"><span class="theme-swatch-preview sw-aurora"></span><span>暮紫</span><span class="theme-selected" aria-hidden="true"></span></button>
            <button type="button" class="theme-swatch" data-theme-option="jade"><span class="theme-swatch-preview sw-jade"></span><span>碧波</span><span class="theme-selected" aria-hidden="true"></span></button>
            <button type="button" class="theme-swatch" data-theme-option="graphite"><span class="theme-swatch-preview sw-graphite"></span><span>玄灰</span><span class="theme-selected" aria-hidden="true"></span></button>
          </div>
        </div>

        <div class="settings-group">
          <div class="settings-group-title"><span class="settings-icon" aria-hidden="true"></span>数据源与访问</div>
          <div class="settings-tools">
            <button type="button" class="btn-secondary" id="cookieBtn">平台 Cookie</button>
            <button type="button" class="btn-secondary" id="webTokenBtn">访问令牌</button>
            <button type="button" class="btn-secondary" id="blacklistBtn">标签黑名单</button>
          </div>
        </div>

        <div class="settings-group">
          <div class="settings-group-title"><span class="settings-icon" aria-hidden="true"></span>配置管理</div>
          <div class="settings-tools">
            <button type="button" class="btn-secondary" id="loadConfigBtn">加载配置</button>
            <button type="button" class="btn-secondary" id="saveConfigBtn">保存配置</button>
            <button type="button" class="btn-secondary" id="exportConfigBtn">导出配置</button>
            <button type="button" class="btn-secondary" id="importConfigBtn">导入配置</button>
          </div>
          <button type="button" id="settingsLoadConfigBtn" hidden></button>
          <button type="button" id="settingsSaveConfigBtn" hidden></button>
        </div>

        <div class="settings-group">
          <div class="settings-group-title"><span class="settings-icon" aria-hidden="true"></span>检查与诊断</div>
          <div class="settings-tools">
            <button type="button" class="btn-secondary" id="previewRunBtn">预览处理</button>
            <button type="button" class="btn-secondary" id="healthBtn">健康检查</button>
            <button type="button" class="btn-secondary" id="qualityBtn">质量检查</button>
          </div>
        </div>

        <div class="settings-group">
          <div class="settings-group-title"><span class="settings-icon" aria-hidden="true"></span>任务与恢复</div>
          <div class="settings-tools">
            <button type="button" class="btn-secondary" id="batchImportBtn">批量导入目录</button>
            <button type="button" class="btn-secondary" id="failedBtn">查看失败列表</button>
            <button type="button" class="btn-secondary" id="retryBtn">重试失败任务</button>
            <button type="button" class="btn-secondary" id="restoreSnapshotBtn">撤销目录改名</button>
            <button type="button" class="btn-secondary" id="exportLogBtn">导出运行日志</button>
          </div>
        </div>
      </div>
      <div class="modal-foot"><span class="modal-note">修改平台 Cookie 后写入容器配置目录</span><span class="spacer"></span><button type="button" class="btn-primary" id="doneSettingsBtn">完成</button></div>
    </div>
  </div>

  <div class="toast" id="toast" role="status" aria-live="polite"></div>

  <script>
    /* ── Theme Toggle ──────────────────────────── */
    const _THEME_KEY = 'audiometa-theme-v3-dark-console';
    const _THEMES = Object.freeze({
      dark: '墨夜', light: '素雪', linen: '茶白', mint: '青瓷', rose: '胭脂',
      ocean: '黛蓝',
      aurora: '暮紫', jade: '碧波', graphite: '玄灰',
    });
    const _LIGHT_THEMES = new Set(['light', 'linen', 'mint', 'rose']);
    let _lastLightTheme = 'light';

    function applyTheme(theme) {
      if (!_THEMES[theme]) theme = 'dark';
      if (_LIGHT_THEMES.has(theme)) _lastLightTheme = theme;
      document.documentElement.setAttribute('data-theme', theme);
      const btn = document.getElementById('themeToggleBtn');
      if (btn) {
        btn.textContent = '';
        const isLight = _LIGHT_THEMES.has(theme);
        btn.setAttribute('aria-pressed', isLight ? 'false' : 'true');
        btn.title = isLight ? '切换到墨夜' : `切换到${_THEMES[_lastLightTheme] || '素雪'}`;
      }
      document.querySelectorAll('[data-theme-option]').forEach(option => {
        const active = option.dataset.themeOption === theme;
        option.classList.toggle('active', active);
        option.setAttribute('aria-pressed', active ? 'true' : 'false');
      });
      localStorage.setItem(_THEME_KEY, theme);
    }

    (function initTheme() {
      const saved = localStorage.getItem(_THEME_KEY);
      if (_THEMES[saved]) {
        applyTheme(saved);
      } else {
        applyTheme('dark');
      }
    })();

    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', e => {
      if (!localStorage.getItem(_THEME_KEY)) applyTheme(e.matches ? 'dark' : 'light');
    });

    document.getElementById('themeToggleBtn').addEventListener('click', () => {
      const current = document.documentElement.getAttribute('data-theme');
      const next = _LIGHT_THEMES.has(current) ? 'dark' : _lastLightTheme;
      applyTheme(next);
    });
    document.querySelectorAll('[data-theme-option]').forEach(option => {
      option.addEventListener('click', () => {
        applyTheme(option.dataset.themeOption);
        toast(`已切换到${_THEMES[option.dataset.themeOption]}`);
      });
    });

    const form = document.getElementById('configForm');
    const authorPool = document.getElementById('authorPool');
    const anchorPool = document.getElementById('anchorPool');
    const teamPool = document.getElementById('teamPool');
    const seriesPool = document.getElementById('seriesPool');
    const tagPool = document.getElementById('tagPool');
    const tagInput = document.getElementById('tagInput');
    const logBox = document.getElementById('logBox');
    const seriesModal = document.getElementById('seriesModal');
    const settingsModal = document.getElementById('settingsModal');
    const seriesNameInput = document.getElementById('seriesNameInput');
    const seriesNumberInput = document.getElementById('seriesNumberInput');
    const customSelectPopover = document.createElement('div');
    customSelectPopover.id = 'customSelectPopover';
    customSelectPopover.className = 'custom-select-popover';
    customSelectPopover.setAttribute('role', 'listbox');
    document.body.appendChild(customSelectPopover);
    let customSelectOwner = null;
    let authors = [];
    let anchors = [];
    let teams = ['RL'];
    let seriesList = [];
    let tags = [];
    let blacklistPatterns = [];
    let latestStatus = null;
    let browseCurrent = '';
    let selectedDir = '';
    const selectedQueueIds = new Set();
    let editingQueueId = '';
    const MAX_CLIENT_LOGS = 1200;
    const MAX_RENDERED_LOGS = 600;
    let clientLogs = [];
    let lastLogSeq = 0;
    let currentLogEpoch = -1;
    let lastRenderedLogSeq = -1;
    let lastRenderedLogEpoch = -1;
    let logRenderFrame = 0;
    let lastQueueSignature = '';
    let lastFailedSignature = '';
    let lastOverviewSignature = '';
    const requiredFields = [
      { key: 'input_folder', label: '音频目录', el: () => form.elements.input_folder },
      { key: 'title', label: '专辑标题', el: () => form.elements.title },
      { key: 'author', label: '原著作者', el: () => authorPool },
      { key: 'anchor', label: '演播艺术家', el: () => anchorPool },
      { key: 'category', label: '专辑分类', el: () => form.elements.category },
      { key: 'platform', label: '发布平台', el: () => form.elements.platform },
      { key: 'year', label: '发布年份', el: () => form.elements.year },
      { key: 'finished', label: '专辑状态', el: () => form.elements.finished },
    ];

    function toast(message) {
      const el = document.getElementById('toast');
      el.textContent = message;
      el.classList.add('show');
      setTimeout(() => el.classList.remove('show'), 2200);
    }

    function openSettingsModal() {
      settingsModal.classList.add('show');
      document.getElementById('closeSettingsBtn').focus();
    }

    function closeSettingsModal() {
      settingsModal.classList.remove('show');
    }

    function setButtonBusy(button, busy, text) {
      if (!button) return;
      if (busy) {
        button.dataset.oldHtml = button.innerHTML;
        button.disabled = true;
        button.innerHTML = text || '处理中...';
      } else {
        button.disabled = false;
        if (button.dataset.oldHtml) button.innerHTML = button.dataset.oldHtml;
      }
    }

    function clearValidationErrors() {
      document.querySelectorAll('.field-error').forEach(el => el.classList.remove('field-error'));
    }

    function validateRequired(params = readForm()) {
      clearValidationErrors();
      const missing = [];
      let firstEl = null;
      for (const field of requiredFields) {
        if (String(params[field.key] || '').trim()) continue;
        const el = field.el();
        if (el) {
          const section = el.closest?.('.section');
          if (section) section.classList.add('mobile-expanded');
          el.classList.add('field-error');
          firstEl ||= el._customSelectTrigger || el;
        }
        missing.push(field.label);
      }
      if (!missing.length) return true;
      toast('请补全：' + missing.join('、'));
      if (firstEl) {
        firstEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
        if (typeof firstEl.focus === 'function') setTimeout(() => firstEl.focus(), 180);
      }
      return false;
    }

    function switchTab(name) {
      const btn = document.querySelector(`[data-tab="${name}"]`);
      if (btn) btn.click();
    }

    function initMobileSections() {
      const sections = [...document.querySelectorAll('#configForm > .section')];
      sections.forEach((section, index) => {
        const title = section.querySelector('.section-title');
        if (!title || title.querySelector('.section-toggle')) return;
        section.classList.add('mobile-collapsible');
        if (index < 2) section.classList.add('mobile-expanded');
        const toggle = document.createElement('button');
        toggle.type = 'button';
        toggle.className = 'section-toggle';
        const updateToggle = () => {
          toggle.innerHTML = section.classList.contains('mobile-expanded') ? _UI_ICONS.chevronUp : _UI_ICONS.chevronDown;
        };
        updateToggle();
        title.appendChild(toggle);
        title.addEventListener('click', () => {
          section.classList.toggle('mobile-expanded');
          updateToggle();
        });
      });
    }

    const _UI_ICONS = {
      source: '<svg class="ui-icon" aria-hidden="true" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14"/><path d="m19 12-7 7-7-7"/></svg>',
      metadata: '<svg class="ui-icon" aria-hidden="true" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>',
      archive: '<svg class="ui-icon" aria-hidden="true" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 8v13H3V8"/><path d="M1 3h22v5H1z"/><path d="M10 12h4"/></svg>',
      visual: '<svg class="ui-icon" aria-hidden="true" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect width="18" height="18" x="3" y="3" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.086-3.086a2 2 0 0 0-2.828 0L6 21"/></svg>',
      queue: '<svg class="ui-icon" aria-hidden="true" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M8 6h13M8 12h13M8 18h13"/><path d="M3 6h.01M3 12h.01M3 18h.01"/></svg>',
      log: '<svg class="ui-icon" aria-hidden="true" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="M16 13H8M16 17H8"/></svg>',
      failed: '<svg class="ui-icon" aria-hidden="true" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><path d="M12 9v4M12 17h.01"/></svg>',
      overview: '<svg class="ui-icon" aria-hidden="true" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect width="18" height="18" x="3" y="3" rx="2"/><path d="M3 9h18M9 21V9"/></svg>',
      download: '<svg class="ui-icon" aria-hidden="true" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/></svg>',
      search: '<svg class="ui-icon" aria-hidden="true" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>',
      user: '<svg class="ui-icon" aria-hidden="true" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>',
      plus: '<svg class="ui-icon" aria-hidden="true" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M12 5v14"/></svg>',
      play: '<svg class="ui-icon" aria-hidden="true" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m6 4 14 8-14 8Z"/></svg>',
      stop: '<svg class="ui-icon" aria-hidden="true" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="4" width="16" height="16" rx="2"/></svg>',
      trash: '<svg class="ui-icon" aria-hidden="true" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/><path d="M10 11v6M14 11v6"/></svg>',
      check: '<svg class="ui-icon" aria-hidden="true" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>',
      x: '<svg class="ui-icon" aria-hidden="true" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg>',
      palette: '<svg class="ui-icon" aria-hidden="true" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22a10 10 0 1 1 10-10c0 2.21-1.79 4-4 4h-2.5a2 2 0 0 0-1.42 3.42c.36.36.57.86.57 1.41A2.17 2.17 0 0 1 12.57 22H12Z"/><circle cx="7.5" cy="11.5" r="1.2"/><circle cx="11" cy="7.5" r="1.2"/><circle cx="15.5" cy="9.5" r="1.2"/></svg>',
      database: '<svg class="ui-icon" aria-hidden="true" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/><path d="M3 12c0 1.66 4 3 9 3s9-1.34 9-3"/></svg>',
      sliders: '<svg class="ui-icon" aria-hidden="true" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3"/><path d="M1 14h6M9 8h6M17 16h6"/></svg>',
      activity: '<svg class="ui-icon" aria-hidden="true" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>',
      refresh: '<svg class="ui-icon" aria-hidden="true" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/></svg>',
      gear: '<svg class="ui-icon" aria-hidden="true" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/></svg>',
      sun: '<svg class="ui-icon" aria-hidden="true" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/></svg>',
      moon: '<svg class="ui-icon" aria-hidden="true" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/></svg>',
      up: '<svg class="ui-icon" aria-hidden="true" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m18 15-6-6-6 6"/></svg>',
      chevronUp: '<svg class="ui-icon" aria-hidden="true" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m18 15-6-6-6 6"/></svg>',
      chevronDown: '<svg class="ui-icon" aria-hidden="true" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>',
      chevronLeft: '<svg class="ui-icon" aria-hidden="true" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m15 18-6-6 6-6"/></svg>',
      chevronRight: '<svg class="ui-icon" aria-hidden="true" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m9 18 6-6-6-6"/></svg>',
      arrowRight: '<svg class="ui-icon" aria-hidden="true" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14"/><path d="m12 5 7 7-7 7"/></svg>',
      save: '<svg class="ui-icon" aria-hidden="true" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M15.2 3a2 2 0 0 1 1.4.6l3.8 3.8a2 2 0 0 1 .6 1.4V19a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h10.2z"/><path d="M17 21v-7H7v7M7 3v4h8"/></svg>',
    };

    function setButtonContent(button, iconName, label) {
      if (!button) return;
      button.innerHTML = `${_UI_ICONS[iconName] || ''}<span>${label}</span>`;
    }

    function iconifyTextButton(button, iconName) {
      if (!button) return;
      const label = button.textContent.replace(/^[^\u4e00-\u9fa5A-Za-z0-9]+\s*/, '').trim();
      setButtonContent(button, iconName, label || button.title);
    }

    function installUiIcons() {
      const sectionNames = ['source', 'metadata', 'archive', 'visual'];
      document.querySelectorAll('.section-icon').forEach((el, index) => {
        if (_UI_ICONS[sectionNames[index]]) el.innerHTML = _UI_ICONS[sectionNames[index]];
      });
      const settingsIconNames = ['palette', 'database', 'sliders', 'activity', 'refresh'];
      document.querySelectorAll('.settings-icon').forEach((el, index) => {
        if (_UI_ICONS[settingsIconNames[index]]) el.innerHTML = _UI_ICONS[settingsIconNames[index]];
      });
      document.querySelectorAll('.theme-symbol').forEach((el, index) => {
        el.innerHTML = _UI_ICONS[index === 0 ? 'sun' : 'moon'];
      });
      document.querySelectorAll('.theme-selected').forEach(el => {
        el.innerHTML = _UI_ICONS.check;
      });
      const settingsBtn = document.getElementById('settingsBtn');
      if (settingsBtn) settingsBtn.innerHTML = _UI_ICONS.gear;
      const tabIcons = { queue: 'queue', log: 'log', failed: 'failed', overview: 'overview' };
      document.querySelectorAll('.tab').forEach(btn => {
        if (tabIcons[btn.dataset.tab]) iconifyTextButton(btn, tabIcons[btn.dataset.tab]);
      });
      const buttonIcons = {
        fetchBtn: 'download',
        searchTitleBtn: 'search',
        fetchAuthorBtn: 'user',
        openSeriesBtn: 'plus',
        addQueueBtn: 'plus',
        startQueueBtn: 'play',
        stopBtn: 'stop',
        clearBtn: 'trash',
        clearLogBtn: 'trash',
        editQueueBtn: 'check',
        removeQueueBtn: 'x',
        clearQueueBtn: 'trash',
        closeDirBtn: 'x',
        closeCookieBtn: 'x',
        closeBlacklistBtn: 'x',
        closeSeriesBtn: 'x',
        closeSettingsBtn: 'x',
        dirUpBtn: 'up',
        chooseDirBtn: 'check',
        saveSeriesBtn: 'plus',
        saveCookieBtn: 'save',
        saveBlacklistBtn: 'save',
      };
      for (const [id, icon] of Object.entries(buttonIcons)) {
        iconifyTextButton(document.getElementById(id), icon);
      }
    }

    async function api(path, options = {}) {
      const timeoutMs = options.timeoutMs || 30000;
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), timeoutMs);
      const { timeoutMs: _timeoutMs, ...fetchOptions } = options;
      try {
        const token = localStorage.getItem('audiometa-web-token') || '';
        const res = await fetch(path, { headers: {'Content-Type': 'application/json', ...(token ? {'X-Audiometa-Token': token} : {})}, cache: 'no-store', signal: controller.signal, ...fetchOptions });
        const data = await res.json();
        if (!data.ok) throw new Error(data.error || '请求失败');
        return data;
      } catch (error) {
        if (error.name === 'AbortError') throw new Error('请求超时，请检查网络或稍后重试');
        throw error;
      } finally {
        clearTimeout(timer);
      }
    }

    const _PLATFORM_BRANDS = {
      '喜马拉雅': { mark: '喜', color: '#ff6b35' },
      '番茄畅听': { mark: '番', color: '#ff4757' },
      '懒人听书': { mark: '懒', color: '#21a366' },
      '起点听书': { mark: '起', color: '#3574f0' },
      '酷我听书': { mark: '酷', color: '#00b8d9' },
      '网易云听书': { mark: '网', color: '#d43c33' },
      '云听fm': { mark: '云', color: '#8b5cf6' },
      '蜻蜓fm': { mark: '蜻', color: '#0f9b8e' },
    };

    function platformBrand(text) {
      const name = String(text || '').trim();
      return _PLATFORM_BRANDS[name] || _PLATFORM_BRANDS[name.toLowerCase()] || null;
    }

    function createPlatformLogo(brand) {
      const logo = document.createElement('span');
      logo.className = 'platform-logo';
      logo.textContent = brand.mark;
      logo.style.setProperty('--brand-color', brand.color);
      logo.title = '';
      return logo;
    }

    function platformLogoHtml(text) {
      const brand = platformBrand(text);
      if (!brand) return '';
      return `<span class="platform-logo" style="--brand-color:${brand.color}">${brand.mark}</span>`;
    }

    function renderCustomSelectOptionContent(option, text) {
      option.replaceChildren();
      const brand = platformBrand(text);
      if (brand) option.append(createPlatformLogo(brand));
      option.append(document.createTextNode(text));
    }

    function optionList(select, values, formatter = v => ({value: v, label: v}), placeholder = '') {
      select.innerHTML = '';
      if (placeholder) {
        const empty = document.createElement('option');
        empty.value = '';
        empty.textContent = placeholder;
        select.appendChild(empty);
      }
      values.forEach(v => {
        const item = formatter(v);
        const option = document.createElement('option');
        option.value = item.value;
        option.textContent = item.label;
        select.appendChild(option);
      });
      select._customSelectSync?.();
    }

    function syncCustomSelect(select) {
      const trigger = select?._customSelectTrigger;
      if (!trigger) return;
      const value = trigger.querySelector('.custom-select-value');
      const selected = select.options[select.selectedIndex];
      const selectedText = selected?.textContent || '请选择';
      value.replaceChildren();
      const brand = platformBrand(selectedText);
      if (brand) value.append(createPlatformLogo(brand));
      value.append(document.createTextNode(selectedText));
      trigger.disabled = select.disabled;
      trigger.title = selectedText;
    }

    function syncAllCustomSelects() {
      document.querySelectorAll('select.custom-select-native').forEach(syncCustomSelect);
    }

    function closeCustomSelect({ restoreFocus = false } = {}) {
      if (!customSelectOwner) return;
      const { trigger } = customSelectOwner;
      trigger.classList.remove('open');
      trigger.setAttribute('aria-expanded', 'false');
      customSelectPopover.classList.remove('open');
      customSelectPopover.innerHTML = '';
      customSelectPopover.removeAttribute('data-side');
      customSelectOwner = null;
      if (restoreFocus) trigger.focus();
    }

    function positionCustomSelect() {
      if (!customSelectOwner) return;
      const rect = customSelectOwner.trigger.getBoundingClientRect();
      const viewportPad = 12;
      const gap = 6;
      const width = Math.min(Math.max(rect.width, 220), window.innerWidth - viewportPad * 2);
      customSelectPopover.style.width = `${width}px`;
      customSelectPopover.style.visibility = 'hidden';
      customSelectPopover.classList.add('open');
      const desiredHeight = Math.min(customSelectPopover.scrollHeight, 320);
      const spaceBelow = window.innerHeight - rect.bottom - viewportPad - gap;
      const spaceAbove = rect.top - viewportPad - gap;
      const side = spaceBelow >= Math.min(desiredHeight, 190) || spaceBelow >= spaceAbove ? 'bottom' : 'top';
      const available = side === 'bottom' ? spaceBelow : spaceAbove;
      const height = Math.min(desiredHeight, Math.max(110, available));
      const left = Math.min(Math.max(viewportPad, rect.left), window.innerWidth - width - viewportPad);
      const top = side === 'bottom' ? rect.bottom + gap : rect.top - height - gap;
      customSelectPopover.dataset.side = side;
      customSelectPopover.style.left = `${left}px`;
      customSelectPopover.style.top = `${Math.max(viewportPad, top)}px`;
      customSelectPopover.style.maxHeight = `${height}px`;
      customSelectPopover.style.visibility = '';
    }

    function focusCustomSelectOption(targetIndex) {
      const options = [...customSelectPopover.querySelectorAll('.custom-select-option:not(:disabled)')];
      if (!options.length) return;
      const current = options.indexOf(document.activeElement);
      const selected = options.findIndex(option => option.classList.contains('selected'));
      let index = current >= 0 ? current : Math.max(0, selected);
      if (targetIndex === 'first') index = 0;
      else if (targetIndex === 'last') index = options.length - 1;
      else index = Math.min(options.length - 1, Math.max(0, index + targetIndex));
      options.forEach(option => { option.tabIndex = -1; });
      options[index].tabIndex = 0;
      options[index].focus();
      options[index].scrollIntoView({ block: 'nearest' });
    }

    function openCustomSelect(select, trigger, { focusMenu = false } = {}) {
      if (customSelectOwner?.select === select) {
        closeCustomSelect();
        return;
      }
      closeCustomSelect();
      customSelectOwner = { select, trigger };
      customSelectPopover.innerHTML = '';
      [...select.options].forEach(nativeOption => {
        const option = document.createElement('button');
        option.type = 'button';
        option.className = 'custom-select-option';
        renderCustomSelectOptionContent(option, nativeOption.textContent);
        option.disabled = nativeOption.disabled;
        option.tabIndex = nativeOption.selected ? 0 : -1;
        option.setAttribute('role', 'option');
        option.setAttribute('aria-selected', String(nativeOption.selected));
        if (nativeOption.selected) option.classList.add('selected');
        option.onclick = event => {
          event.stopPropagation();
          select.value = nativeOption.value;
          select.dispatchEvent(new Event('input', { bubbles: true }));
          select.dispatchEvent(new Event('change', { bubbles: true }));
          syncCustomSelect(select);
          closeCustomSelect({ restoreFocus: true });
        };
        customSelectPopover.appendChild(option);
      });
      trigger.classList.add('open');
      trigger.setAttribute('aria-expanded', 'true');
      positionCustomSelect();
      requestAnimationFrame(() => {
        const selected = customSelectPopover.querySelector('.custom-select-option.selected');
        selected?.scrollIntoView({ block: 'nearest' });
        if (focusMenu) (selected || customSelectPopover.querySelector('.custom-select-option:not(:disabled)'))?.focus();
      });
    }

    function enhanceSelect(select) {
      if (select.dataset.customSelect === 'true') return;
      const shell = document.createElement('div');
      shell.className = 'custom-select';
      select.parentNode.insertBefore(shell, select);
      shell.appendChild(select);
      select.dataset.customSelect = 'true';
      select.classList.add('custom-select-native');
      select.tabIndex = -1;

      const trigger = document.createElement('button');
      trigger.type = 'button';
      trigger.className = 'custom-select-trigger';
      trigger.setAttribute('aria-haspopup', 'listbox');
      trigger.setAttribute('aria-expanded', 'false');
      trigger.setAttribute('aria-controls', customSelectPopover.id);
      trigger.innerHTML = '<span class="custom-select-value"></span><span class="custom-select-arrow" aria-hidden="true"></span>';
      shell.appendChild(trigger);
      select._customSelectTrigger = trigger;
      select._customSelectSync = () => syncCustomSelect(select);
      syncCustomSelect(select);

      select.addEventListener('change', () => syncCustomSelect(select));
      new MutationObserver(() => syncCustomSelect(select)).observe(select, {
        childList: true, subtree: true, attributes: true,
      });
      trigger.onclick = event => {
        event.stopPropagation();
        openCustomSelect(select, trigger);
      };
      trigger.onkeydown = event => {
        if (!['ArrowDown', 'ArrowUp', 'Enter', ' '].includes(event.key)) return;
        event.preventDefault();
        if (customSelectOwner?.select !== select) openCustomSelect(select, trigger, { focusMenu: true });
        else if (event.key === 'ArrowDown') focusCustomSelectOption(1);
        else if (event.key === 'ArrowUp') focusCustomSelectOption(-1);
      };
    }

    function initCustomSelects() {
      document.querySelectorAll('select').forEach(enhanceSelect);
    }

    customSelectPopover.addEventListener('keydown', event => {
      if (event.key === 'Escape') {
        event.preventDefault();
        closeCustomSelect({ restoreFocus: true });
      } else if (event.key === 'ArrowDown') {
        event.preventDefault();
        focusCustomSelectOption(1);
      } else if (event.key === 'ArrowUp') {
        event.preventDefault();
        focusCustomSelectOption(-1);
      } else if (event.key === 'Home') {
        event.preventDefault();
        focusCustomSelectOption('first');
      } else if (event.key === 'End') {
        event.preventDefault();
        focusCustomSelectOption('last');
      }
    });
    document.addEventListener('pointerdown', event => {
      if (!customSelectOwner) return;
      if (customSelectPopover.contains(event.target) || customSelectOwner.trigger.contains(event.target)) return;
      closeCustomSelect();
    }, true);
    document.addEventListener('focusin', event => {
      if (!customSelectOwner) return;
      if (customSelectPopover.contains(event.target) || customSelectOwner.trigger.contains(event.target)) return;
      closeCustomSelect();
    });
    document.addEventListener('scroll', event => {
      if (customSelectOwner && !customSelectPopover.contains(event.target)) positionCustomSelect();
    }, true);
    window.addEventListener('resize', () => customSelectOwner && positionCustomSelect());

    async function loadOptions() {
      const { options } = await api('/api/options');
      optionList(form.api_source, options.api_sources);
      optionList(form.platform, options.platforms, v => ({value: v, label: v}), '请选择发布平台');
      optionList(form.category, options.categories, v => ({value: v.id, label: `${v.id} · ${v.name}`}), '请选择专辑分类');
      optionList(form.target_format, options.target_formats);
      optionList(form.bitrate, options.bitrates);
      optionList(form.finished, options.finished, v => ({value: v, label: v}), '请选择专辑状态');
      browseCurrent = options.data_root;
    }

    function splitPeople(text) {
      return String(text || '').split(/[,，、|&\s]+/).map(v => v.trim()).filter(Boolean);
    }

    function splitCsv(text) {
      return String(text || '').split(/[,\n]+/).map(v => v.trim()).filter(Boolean);
    }

    function buildSeriesList(namesText, numbersText) {
      const names = splitCsv(namesText);
      const numbers = splitCsv(numbersText);
      return names.map((name, index) => numbers[index] ? `${name}#${numbers[index]}` : name).filter(Boolean);
    }

    function splitSeriesFields(items) {
      const names = [];
      const numbers = [];
      for (const item of items || []) {
        const [name, number = ''] = String(item).split('#', 2);
        if (name.trim()) {
          names.push(name.trim());
          numbers.push(number.trim());
        }
      }
      return { names, numbers };
    }

    function readForm() {
      const fd = new FormData(form);
      const params = Object.fromEntries(fd.entries());
      const seriesFields = splitSeriesFields(seriesList);
      params.author = authors.join(', ');
      params.anchor = anchors.join(', ');
      params.team = teams[0] || '';
      params.series_name = seriesFields.names.join(', ');
      params.series_number = seriesFields.numbers.some(Boolean) ? seriesFields.numbers.join(', ') : '';
      params.check_codec = true;
      params.rename_ext = true;
      params.debug = true;
      params.album_tags = [...tags];
      params.fetched_metadata = currentRawMetadata || {};
      return params;
    }

    let currentRawMetadata = {};
    function fillForm(params) {
      for (const [key, value] of Object.entries(params || {})) {
        const el = form.elements[key];
        if (!el) continue;
        if (el.type === 'checkbox') el.checked = !!value;
        else el.value = value ?? '';
      }
      authors = splitPeople(params.author || '');
      anchors = splitPeople(params.anchor || '');
      teams = splitPeople(params.team || '').slice(0, 1);
      seriesList = buildSeriesList(params.series_name || '', params.series_number || '');
      form.author.value = '';
      form.anchor.value = '';
      form.team.value = '';
      form.series_name.value = '';
      form.series_number.value = '';
      tags = Array.isArray(params.album_tags) ? params.album_tags : String(params.album_tags || '').split(/[,\n]+/).filter(Boolean);
      currentRawMetadata = params.fetched_metadata || {};
      renderAuthors();
      renderAnchors();
      renderTeams();
      renderSeries();
      renderTags();
      previewCover();
      clearValidationErrors();
      syncAllCustomSelects();
    }

    function renderPeople(pool, values, removeHandler, options = {}) {
      pool.innerHTML = '';
      if (!values.length) {
        if (!options.editable) {
          const empty = document.createElement('span');
          empty.className = 'hint';
          empty.textContent = '暂无';
          pool.appendChild(empty);
          return;
        }
      }
      values.forEach((value, index) => {
        const chip = document.createElement('span');
        chip.className = 'chip colored-chip';
        const hue = (198 + index * 137.508) % 360;
        chip.style.setProperty('--chip-color-a', `hsl(${hue.toFixed(1)} 58% 46%)`);
        chip.style.setProperty('--chip-color-b', `hsl(${((hue + 22) % 360).toFixed(1)} 64% 34%)`);
        chip.style.setProperty('--chip-border', `hsl(${((hue + 10) % 360).toFixed(1)} 52% 58% / .55)`);
        chip.innerHTML = `<span>${value}</span>`;
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.textContent = '×';
        btn.onclick = event => {
          event.stopPropagation();
          if (typeof removeHandler === 'function') removeHandler(index);
        };
        chip.appendChild(btn);
        pool.appendChild(chip);
      });
      if (options.editable) {
        const editor = document.createElement('span');
        editor.className = 'chip-input';
        editor.contentEditable = 'true';
        editor.spellcheck = false;
        editor.dataset.placeholder = values.length ? '' : (options.placeholder || '');
        editor.addEventListener('keydown', event => {
          if (event.key !== 'Enter') return;
          event.preventDefault();
          addPeopleValues(editor.textContent, options.target, options.renderFn);
        });
        editor.addEventListener('paste', event => {
          event.preventDefault();
          const text = (event.clipboardData || window.clipboardData).getData('text');
          document.execCommand('insertText', false, text);
        });
        pool.appendChild(editor);
      }
    }

    function renderAuthors() {
      renderPeople(authorPool, authors, index => {
        authors.splice(index, 1);
        renderAuthors();
      }, { editable: true, target: authors, renderFn: renderAuthors, placeholder: '请输入作者，回车添加' });
      if (authors.length) authorPool.classList.remove('field-error');
    }

    function renderAnchors() {
      renderPeople(anchorPool, anchors, index => {
        anchors.splice(index, 1);
        renderAnchors();
      }, { editable: true, target: anchors, renderFn: renderAnchors, placeholder: '请输入演播者，回车添加' });
      if (anchors.length) anchorPool.classList.remove('field-error');
    }

    function renderTeams() {
      renderPeople(teamPool, teams, index => {
        teams.splice(index, 1);
        renderTeams();
      }, { editable: true, target: teams, renderFn: renderTeams, single: true, placeholder: '请输入制作团队，回车添加' });
    }

    function renderSeries() {
      seriesPool.innerHTML = '';
      if (!seriesList.length) {
        const empty = document.createElement('span');
        empty.className = 'series-empty';
        empty.textContent = '暂无系列档案';
        seriesPool.appendChild(empty);
        return;
      }
      renderPeople(seriesPool, seriesList, index => {
        seriesList.splice(index, 1);
        renderSeries();
      });
    }

    function addPeopleValues(value, target, renderFn) {
      const values = splitPeople(value);
      if (target === teams) target.length = 0;
      for (const value of values) if (!target.includes(value)) target.push(value);
      renderFn();
    }

    function openSeriesModal() {
      seriesNameInput.value = '';
      seriesNumberInput.value = '';
      seriesModal.classList.add('show');
      setTimeout(() => seriesNameInput.focus(), 30);
    }

    function closeSeriesModal() { seriesModal.classList.remove('show'); }

    function addSeriesFromModal() {
      const name = seriesNameInput.value.trim();
      const number = seriesNumberInput.value.trim();
      if (!name) return;
      const value = number ? `${name}#${number}` : name;
      if (!seriesList.includes(value)) seriesList.push(value);
      renderSeries();
      closeSeriesModal();
    }

    function renderTags() {
      tagPool.innerHTML = '';
      if (!tags.length) {
        const empty = document.createElement('span');
        empty.className = 'hint';
        empty.textContent = '暂无标签';
        tagPool.appendChild(empty);
        return;
      }
      tags.forEach((tag, index) => {
        const chip = document.createElement('span');
        chip.className = 'chip album-tag-chip';
        // Golden-angle spacing keeps every visible tag on a distinct hue while
        // preserving a balanced palette as tags are added or removed.
        const hue = (198 + index * 137.508) % 360;
        chip.style.setProperty('--tag-color-a', `hsl(${hue.toFixed(1)} 58% 46%)`);
        chip.style.setProperty('--tag-color-b', `hsl(${((hue + 22) % 360).toFixed(1)} 64% 34%)`);
        chip.style.setProperty('--tag-border', `hsl(${((hue + 10) % 360).toFixed(1)} 52% 58% / .55)`);
        const text = document.createElement('span');
        text.textContent = tag;
        chip.appendChild(text);
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.textContent = '×';
        btn.onclick = () => { tags.splice(index, 1); renderTags(); };
        chip.appendChild(btn);
        tagPool.appendChild(chip);
      });
    }

    tagInput.addEventListener('keydown', e => {
      if (e.key !== 'Enter') return;
      e.preventDefault();
      const value = tagInput.value.trim();
      if (value && !tags.includes(value)) tags.push(value);
      tagInput.value = '';
      renderTags();
    });

    document.getElementById('openSeriesBtn').addEventListener('click', openSeriesModal);
    document.getElementById('closeSeriesBtn').addEventListener('click', closeSeriesModal);
    document.getElementById('saveSeriesBtn').addEventListener('click', addSeriesFromModal);
    seriesModal.addEventListener('click', e => { if (e.target === seriesModal) closeSeriesModal(); });
    settingsModal.addEventListener('click', e => { if (e.target === settingsModal) closeSettingsModal(); });
    [seriesNameInput, seriesNumberInput].forEach(input => {
      input.addEventListener('keydown', e => {
        if (e.key !== 'Enter') return;
        e.preventDefault();
        addSeriesFromModal();
      });
    });

    async function loadConfig({ startup = false } = {}) {
      const data = await api('/api/config');
      const params = {...(data.params || {})};
      if (startup) {
        params.platform = '';
        params.category = '';
        params.finished = '';
        params.year = '';
        params.team = 'RL';
      }
      fillForm(params);
      if (!startup) toast('配置已加载');
    }

    async function saveConfig() {
      const data = await api('/api/config', { method: 'POST', body: JSON.stringify({params: readForm()}) });
      fillForm(data.params);
      toast('配置已保存');
    }

    async function loadFolderConfig(path) {
      if (!path) return;
      const data = await api('/api/folder-config', { method: 'POST', body: JSON.stringify({path}) });
      if (!data.found) return;
      if (!confirm('检测到专辑目录内存在 process_params.json，是否加载该配置文件？')) return;
      if (data.params) fillForm(data.params);
      toast(data.message || '配置已加载');
    }

    function applyMetadata(meta) {
      if (meta.title) form.title.value = meta.title;
      if (meta.subtitle) form.subtitle.value = meta.subtitle;
      if (meta.author) {
        for (const value of splitPeople(meta.author)) if (!authors.includes(value)) authors.push(value);
        renderAuthors();
      }
      if (meta.anchor) {
        for (const value of splitPeople(meta.anchor)) if (!anchors.includes(value)) anchors.push(value);
        renderAnchors();
      }
      if (meta.year) form.year.value = meta.year;
      if (meta.finished) form.finished.value = meta.finished;
      if (meta.category) form.category.value = meta.category;
      if (meta.platform) form.platform.value = meta.platform;
      if (meta.desc) form.manual_desc.value = meta.desc;
      if (meta.cover_url) form.manual_cover_path.value = meta.cover_url;
      for (const tag of meta.tags || []) if (tag && !tags.includes(tag)) tags.push(tag);
      currentRawMetadata = meta.raw || {};
      renderTags();
      previewCover();
      syncAllCustomSelects();
      toast('元数据已应用');
    }

    async function fetchMetadata(searchResult) {
      const btn = document.getElementById('fetchBtn');
      setButtonBusy(btn, true, '获取中...');
      toast('正在获取元数据...');
      try {
        const data = await api('/api/fetch-metadata', { method: 'POST', body: JSON.stringify({api_source: form.api_source.value, api_id: form.api_id.value, search_result: searchResult || null}), timeoutMs: 90000 });
        applyMetadata(data.metadata);
        toast('元数据获取成功');
      } finally {
        setButtonBusy(btn, false);
      }
    }

    async function fetchLink() {
      const btn = document.getElementById('fetchLinkBtn');
      setButtonBusy(btn, true, '请求中...');
      toast('正在请求链接元数据...');
      try {
        const data = await api('/api/fetch-link', { method: 'POST', body: JSON.stringify({platform: form.link_platform.value, url: form.link_url.value}), timeoutMs: 120000 });
        applyMetadata(data.metadata);
        toast('元数据获取成功');
      } finally {
        setButtonBusy(btn, false);
      }
    }

    function applyAuthorCandidate(item) {
      const author = String(item?.author || '').trim();
      if (!author) return;
      const existed = authors.includes(author);
      if (!existed) authors.push(author);
      renderAuthors();
      closeAuthorSearchResults();
      toast(existed ? `作者“${author}”已存在` : `已获取作者：${author}`);
    }

    function renderAuthorSearchResults(results, title) {
      const box = document.getElementById('authorSearchResults');
      const backdrop = document.getElementById('titleSearchBackdrop');
      box.replaceChildren();
      const head = document.createElement('div');
      head.className = 'search-dialog-head';
      head.innerHTML = `<strong>选择原著作者</strong><span class="search-count">${results.length} 位候选</span><button type="button" class="search-dialog-close" aria-label="关闭">${_UI_ICONS.x}</button>`;
      head.querySelector('button').onclick = closeAuthorSearchResults;
      box.appendChild(head);
      results.forEach((item, index) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'search-result author-search-result';
        const avatar = document.createElement('span');
        avatar.className = 'author-result-avatar';
        avatar.textContent = String(item.author || '?').trim().slice(0, 1);
        const hue = (190 + index * 47) % 360;
        avatar.style.background = `linear-gradient(135deg, hsl(${hue} 74% 52%), hsl(${(hue + 26) % 360} 70% 36%))`;
        avatar.style.boxShadow = `0 8px 20px hsl(${hue} 72% 42% / .28)`;
        const body = document.createElement('span');
        const authorName = document.createElement('span');
        authorName.className = 'search-result-title';
        authorName.textContent = item.author || '未知作者';
        const meta = document.createElement('span');
        meta.className = 'search-result-meta';
        const sourceName = item.source === 'youshu' ? '备用书库' : '阅评说';
        meta.textContent = [item.title || title, sourceName].filter(Boolean).join(' · ');
        body.append(authorName, meta);
        const pick = document.createElement('span');
        pick.className = 'search-result-action';
        pick.innerHTML = `${_UI_ICONS.arrowRight}<span>填入</span>`;
        button.append(avatar, body, pick);
        button.onclick = () => applyAuthorCandidate(item);
        box.appendChild(button);
      });
      box.hidden = false;
      backdrop.hidden = false;
    }

    function closeAuthorSearchResults() {
      document.getElementById('authorSearchResults').hidden = true;
      document.getElementById('titleSearchBackdrop').hidden = true;
    }

    async function fetchAuthorByTitle() {
      const btn = document.getElementById('fetchAuthorBtn');
      const title = form.elements.title.value.trim();
      if (!title) return toast('请先填写专辑标题');
      setButtonBusy(btn, true, '查询中...');
      try {
        const data = await api('/api/search-author', {
          method: 'POST',
          body: JSON.stringify({title}),
          timeoutMs: 25000,
        });
        const results = data.results || [];
        if (!results.length) return toast('未找到可信的作者匹配，现有作者未修改');
        if (results.length === 1) return applyAuthorCandidate(results[0]);
        renderAuthorSearchResults(results, title);
      } finally {
        setButtonBusy(btn, false);
      }
    }

    async function addQueueFast() {
      const params = readForm();
      if (!validateRequired(params)) return;
      const btn = document.getElementById('addQueueBtn');
      setButtonBusy(btn, true, editingQueueId ? '更新中...' : '加入中...');
      try {
        const path = editingQueueId ? '/api/queue/update' : '/api/queue/add';
        const body = editingQueueId ? {id: editingQueueId, params} : {params};
        const data = await api(path, { method: 'POST', body: JSON.stringify(body) });
        editingQueueId = '';
        if (data.status) applyStatus(data.status);
        toast(path.endsWith('update') ? '任务已更新' : '任务已加入队列');
      } finally {
        setButtonBusy(btn, false);
        if (!editingQueueId) btn.textContent = '＋ 加入队列';
      }
    }

    function editSelectedQueue() {
      const ids = [...selectedQueueIds];
      if (ids.length !== 1) return toast('请选择一个任务进行编辑');
      const item = (latestStatus?.queue || []).find(item => item.id === ids[0]);
      if (!item) return toast('未找到选中的任务');
      if (item.status === 'processing') return toast('处理中任务不可编辑');
      fillForm(item.params || {});
      editingQueueId = item.id;
      setButtonContent(document.getElementById('addQueueBtn'), 'check', '更新任务');
      document.querySelector('.left').scrollTo({top: 0, behavior: 'smooth'});
      toast('已载入选中任务，可在左侧修改后点击更新任务');
    }

    async function startQueue() {
      toast('正在启动处理...');
      if (!latestStatus || !(latestStatus.queue || []).length) {
        const params = readForm();
        if (!validateRequired(params)) return;
        switchTab('log');
        await api('/api/run', { method: 'POST', body: JSON.stringify({params}) });
      } else {
        switchTab('log');
        await api('/api/queue/start', { method: 'POST', body: '{}' });
      }
      applyStatus({...((latestStatus || {})), running: true, message: '正在启动处理...', progress: latestStatus?.progress || 0});
      await refreshStatus();
    }

    function renderTitleSearchResults(results) {
      const box = document.getElementById('titleSearchResults');
      const backdrop = document.getElementById('titleSearchBackdrop');
      box.replaceChildren();
      const head = document.createElement('div');
      head.className = 'search-dialog-head';
      head.innerHTML = `<strong>选择搜索结果</strong><span class="search-count">${results.length} 条结果</span><button type="button" class="search-dialog-close" aria-label="关闭">${_UI_ICONS.x}</button>`;
      head.querySelector('button').onclick = closeTitleSearchResults;
      box.appendChild(head);
      if (!results.length) {
        const empty = document.createElement('div');
        empty.className = 'search-empty';
        empty.innerHTML = `${_UI_ICONS.search}<span>没有找到匹配专辑</span>`;
        box.appendChild(empty);
        box.hidden = false;
        backdrop.hidden = false;
        return;
      }
      results.forEach(item => {
        const button = document.createElement('button');
        button.type = 'button'; button.className = 'search-result';
        const cover = document.createElement('img');
        const coverUrl = item.cover || '';
        cover.src = /^https:\/\/bookcover\.yuewen\.com\//i.test(coverUrl)
          ? '/api/remote-cover?url=' + encodeURIComponent(coverUrl)
          : coverUrl;
        cover.alt = '';
        cover.onerror = () => { cover.removeAttribute('src'); cover.alt = '暂无封面'; };
        const body = document.createElement('span');
        const title = document.createElement('span');
        title.className = 'search-result-title';
        title.textContent = item.title || '未命名专辑';
        const meta = document.createElement('span');
        meta.className = 'search-result-meta';
        const metaParts = [];
        if (item.author) metaParts.push(item.author);
        if (item.narrator) metaParts.push(item.narrator);
        if (item.id) metaParts.push(`ID ${item.id}`);
        meta.textContent = metaParts.join(' · ');
        body.append(title, meta);
        if (item.desc) {
          const desc = document.createElement('span');
          desc.className = 'search-result-desc';
          desc.textContent = item.desc;
          body.append(desc);
        }
        if (Array.isArray(item.tags) && item.tags.length) {
          const tags = document.createElement('span');
          tags.className = 'search-result-tags';
          item.tags.slice(0, 3).forEach(tag => {
            const chip = document.createElement('span');
            chip.className = 'search-result-tag';
            chip.textContent = tag;
            tags.append(chip);
          });
          body.append(tags);
        }
        const pick = document.createElement('span');
        pick.className = 'search-result-action';
        pick.innerHTML = `${_UI_ICONS.arrowRight}<span>选择</span>`;
        button.append(cover, body, pick);
        button.onclick = async () => {
          const selected = {
            id: item.id,
            title: item.title,
            author: item.author,
            narrator: item.narrator,
            cover: item.cover,
            desc: item.desc,
            tags: Array.isArray(item.tags) ? item.tags : [],
            chapter_count: item.chapter_count || 0,
            finished: item.finished || '',
            category: item.category || '',
            release_date: item.release_date || '',
          };
          form.api_id.value = selected.id;
          closeTitleSearchResults();
          await fetchMetadata(selected);
        };
        box.appendChild(button);
      });
      const pagination = document.createElement('div');
      pagination.className = 'search-pagination';
      pagination.innerHTML = `<button type="button" class="quiet-button" id="searchPrevBtn">${_UI_ICONS.chevronLeft}<span>上一页</span></button><span class="search-result-meta" id="searchPageText"></span><button type="button" class="quiet-button" id="searchNextBtn"><span>下一页</span>${_UI_ICONS.chevronRight}</button>`;
      pagination.querySelector('#searchPrevBtn').disabled = titleSearchPage <= 1;
      pagination.querySelector('#searchNextBtn').disabled = !titleSearchHasNext;
      pagination.querySelector('#searchPageText').textContent = `第 ${titleSearchPage} 页`;
      pagination.querySelector('#searchPrevBtn').onclick = () => loadTitleSearchPage(titleSearchPage - 1);
      pagination.querySelector('#searchNextBtn').onclick = () => loadTitleSearchPage(titleSearchPage + 1);
      box.appendChild(pagination);
      box.hidden = false;
      backdrop.hidden = false;
    }

    let titleSearchPage = 1;
    let titleSearchHasNext = false;
    async function loadTitleSearchPage(page) {
      const data = await api('/api/search-metadata', { method: 'POST', body: JSON.stringify({api_source: form.api_source.value, keyword: form.api_id.value.trim(), page}), timeoutMs: 30000 });
      titleSearchPage = data.page || page;
      titleSearchHasNext = !!data.has_next;
      renderTitleSearchResults(data.results || []);
    }

    function closeTitleSearchResults() {
      document.getElementById('titleSearchResults').hidden = true;
      document.getElementById('titleSearchBackdrop').hidden = true;
    }

    async function searchByTitle() {
      const btn = document.getElementById('searchTitleBtn');
      const keyword = form.api_id.value.trim();
      if (!keyword) return toast('请输入书名');
      setButtonBusy(btn, true, '搜索中...');
      try {
        titleSearchPage = 1;
        const data = await api('/api/search-metadata', { method: 'POST', body: JSON.stringify({api_source: form.api_source.value, keyword, page: 1}), timeoutMs: 30000 });
        titleSearchHasNext = !!data.has_next;
        renderTitleSearchResults(data.results || []);
      } finally { setButtonBusy(btn, false); }
    }

    async function stopTask() {
      toast('正在停止任务...');
      await api('/api/stop', { method: 'POST', body: '{}' });
      applyStatus({...((latestStatus || {})), running: true, message: '正在停止'});
      await refreshStatus();
    }

    async function removeSelectedQueueStable() {
      const ids = [...selectedQueueIds];
      if (!ids.length) return toast('请先选择要移除的任务');
      const data = await api('/api/queue/remove', { method: 'POST', body: JSON.stringify({ids}) });
      selectedQueueIds.clear();
      if (data.status) applyStatus(data.status);
      else await refreshStatus();
    }

    async function clearQueue() {
      toast('正在清空队列...');
      await api('/api/queue/clear', { method: 'POST', body: '{}' });
      await refreshStatus();
    }

    async function retryFailedQueue() {
      await api('/api/queue/retry-failed', { method: 'POST', body: '{}' });
      await refreshStatus();
      toast('失败任务已重置，等待重试');
    }

    async function clearAll() {
      editingQueueId = '';
      setButtonContent(document.getElementById('addQueueBtn'), 'plus', '加入队列');
      form.reset();
      authors = [];
      anchors = [];
      teams = ['RL'];
      seriesList = [];
      tags = [];
      currentRawMetadata = {};
      form.manual_cover_path.value = '';
      renderAuthors();
      renderAnchors();
      renderTeams();
      renderSeries();
      renderTags();
      previewCover();
      clearValidationErrors();
      syncAllCustomSelects();
      toast('已清空左侧编辑区');
    }

    function escapeHtml(value) {
      return String(value ?? '').replace(/[&<>"']/g, character => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
      })[character]);
    }

    function renderQueue(queue) {
      const tbody = document.getElementById('queueBody');
      tbody.innerHTML = '';
      if (!(queue || []).length) {
        const tr = document.createElement('tr');
        tr.innerHTML = '<td colspan="7"><div class="empty-state"><div><strong>任务队列为空</strong><span>填写左侧专辑信息后，可加入队列或直接开始处理。</span></div></div></td>';
        tbody.appendChild(tr);
        selectedQueueIds.clear();
        updateSelectedQueueUi();
        return;
      }
      const validIds = new Set((queue || []).map(item => item.id));
      [...selectedQueueIds].forEach(id => { if (!validIds.has(id)) selectedQueueIds.delete(id); });
      (queue || []).forEach((item, index) => {
        const tr = document.createElement('tr');
        const params = item.params || {};
        const platform = params.platform || params.api_source || '未指定';
        const progress = item.status === 'done' ? 100 : item.status === 'processing' ? Math.round(latestStatus?.progress || 0) : 0;
        const progressText = item.status === 'pending' || item.status === 'stopped' ? '—' : `${progress}%`;
        const platformMark = String(platform).includes('喜马拉雅') ? '听' : String(platform).includes('蜻蜓') ? '蜓' : String(platform).slice(0, 1);
        const platformLogo = platformLogoHtml(platform) || `<i class="queue-platform-icon">${escapeHtml(platformMark)}</i>`;
        const isSelected = selectedQueueIds.has(item.id);
        tr.dataset.queueId = item.id;
        tr.title = item.source || '';
        tr.classList.toggle('selected', isSelected);
        tr.innerHTML = `<td><input type="checkbox" class="queue-check" ${isSelected ? 'checked' : ''} aria-label="选择任务"></td><td>${index + 1}</td><td>${escapeHtml(item.title || '未命名')}</td><td><span class="queue-platform">${platformLogo}${escapeHtml(platform)}</span></td><td><span class="queue-progress ${item.status === 'done' ? 'done' : ''}"><span>${progressText}</span><i class="queue-progress-track"><i style="width:${progress}%"></i></i></span></td><td><span class="status-badge ${item.status || 'pending'}">${escapeHtml(statusText(item.status))}</span></td><td><span class="queue-row-actions"><button type="button" data-action="run" title="开始任务">▷</button><button type="button" data-action="edit" title="编辑任务">⋮</button></span></td>`;
        tbody.appendChild(tr);
        tr.onclick = event => {
          const checkbox = event.target.closest('.queue-check');
          if (checkbox) {
            if (checkbox.checked) selectedQueueIds.add(item.id);
            else selectedQueueIds.delete(item.id);
            tr.classList.toggle('selected', checkbox.checked);
            updateSelectedQueueUi();
            return;
          }
          const action = event.target.closest('[data-action]')?.dataset.action;
          if (action) {
            event.stopPropagation();
            selectedQueueIds.clear();
            selectedQueueIds.add(item.id);
            updateSelectedQueueUi();
            if (action === 'run') startQueue().catch(error => toast(error.message));
            else editSelectedQueue();
            return;
          }
          if (selectedQueueIds.has(item.id)) selectedQueueIds.delete(item.id);
          else selectedQueueIds.add(item.id);
          tr.classList.toggle('selected', selectedQueueIds.has(item.id));
          updateSelectedQueueUi();
        };
      });
      updateSelectedQueueUi();
    }

    function updateSelectedQueueUi() {
      const count = selectedQueueIds.size;
      const countText = document.getElementById('selectedCountText');
      if (countText) countText.textContent = String(count);
      const queueActions = document.querySelector('.queue-actions');
      if (queueActions) queueActions.classList.toggle('has-selection', count > 0);
    }

    function statusText(value) {
      return {pending:'等待中', processing:'处理中', done:'完成', failed:'失败', stopped:'已停止'}[value] || value || '';
    }

    let logFilter = 'all';
    let logKeyword = '';
    function mergeStatusLogs(status) {
      const incoming = Array.isArray(status.logs) ? status.logs : [];
      const serverSeq = Number(status.log_seq);
      const serverEpoch = Number(status.log_epoch);
      const hasIncrementalMetadata = Number.isFinite(serverSeq) && Number.isFinite(serverEpoch);
      if (!hasIncrementalMetadata) {
        const changed = JSON.stringify(clientLogs) !== JSON.stringify(incoming);
        clientLogs = incoming.slice(-MAX_CLIENT_LOGS);
        lastLogSeq = clientLogs.length;
        currentLogEpoch = 0;
        status.logs = clientLogs;
        return changed;
      }

      const reset = !!status.logs_reset || serverEpoch !== currentLogEpoch || serverSeq < lastLogSeq;
      let changed = reset;
      if (reset) clientLogs = [];
      const knownIds = new Set(clientLogs.map(item => Number(item.id)).filter(Number.isFinite));
      for (const item of incoming) {
        const id = Number(item.id);
        if (Number.isFinite(id) && knownIds.has(id)) continue;
        clientLogs.push(item);
        if (Number.isFinite(id)) knownIds.add(id);
        changed = true;
      }
      if (clientLogs.length > MAX_CLIENT_LOGS) {
        clientLogs = clientLogs.slice(-MAX_CLIENT_LOGS);
        changed = true;
      }
      lastLogSeq = serverSeq;
      currentLogEpoch = serverEpoch;
      status.logs = clientLogs;
      return changed;
    }

    function scheduleLogRender(force = false) {
      const logPanelActive = document.getElementById('panel-log').classList.contains('active');
      if (!force && !logPanelActive) return;
      if (!force && lastRenderedLogSeq === lastLogSeq && lastRenderedLogEpoch === currentLogEpoch) return;
      if (logRenderFrame) return;
      logRenderFrame = requestAnimationFrame(() => {
        logRenderFrame = 0;
        renderLogs(clientLogs);
        lastRenderedLogSeq = lastLogSeq;
        lastRenderedLogEpoch = currentLogEpoch;
      });
    }

    function renderLogs(logs) {
      const filteredLogs = (logs || []).filter(item => {
        const levelOk = logFilter === 'all' || (item.level || 'info') === logFilter;
        const keywordOk = !logKeyword || String(item.message || '').toLowerCase().includes(logKeyword.toLowerCase());
        return levelOk && keywordOk;
      });
      const wasNearBottom = logBox.scrollHeight - logBox.scrollTop - logBox.clientHeight < 80;
      const fragment = document.createDocumentFragment();
      if (!filteredLogs.length) {
        const div = document.createElement('div');
        div.className = 'log-line info';
        div.textContent = '等待日志实时显示。';
        fragment.appendChild(div);
        logBox.replaceChildren(fragment);
        return;
      }
      const visibleLogs = filteredLogs.slice(-MAX_RENDERED_LOGS);
      const hiddenCount = filteredLogs.length - visibleLogs.length;
      if (hiddenCount > 0) {
        const notice = document.createElement('div');
        notice.className = 'log-line log-truncated';
        notice.textContent = `为保持界面流畅，已隐藏较早的 ${hiddenCount} 条日志。`;
        fragment.appendChild(notice);
      }
      for (const item of visibleLogs) {
        const div = document.createElement('div');
        div.className = 'log-line ' + (item.level || 'info');
        div.textContent = item.message;
        fragment.appendChild(div);
      }
      logBox.replaceChildren(fragment);
      if (wasNearBottom || lastRenderedLogSeq < 0) logBox.scrollTop = logBox.scrollHeight;
    }

    function renderFailed(items) {
      const tbody = document.getElementById('failedBody');
      tbody.innerHTML = '';
      if (!(items || []).length) {
        const tr = document.createElement('tr');
        tr.innerHTML = '<td colspan="2"><div class="empty-state"><div><strong>暂无异常</strong><span>处理失败的文件会集中显示在这里。</span></div></div></td>';
        tbody.appendChild(tr);
        return;
      }
      (items || []).forEach(item => {
        const tr = document.createElement('tr');
        tr.innerHTML = `<td>${item.file || ''}</td><td>${item.error || ''}</td>`;
        tbody.appendChild(tr);
      });
    }

    function parseStatusTime(value) {
      if (!value) return null;
      const date = new Date(String(value).replace(' ', 'T'));
      return Number.isNaN(date.getTime()) ? null : date;
    }

    function formatDuration(startValue, endValue) {
      const start = parseStatusTime(startValue);
      const end = parseStatusTime(endValue) || (start ? new Date() : null);
      if (!start || !end) return '--';
      const totalSeconds = Math.max(0, Math.floor((end - start) / 1000));
      const hours = Math.floor(totalSeconds / 3600);
      const minutes = Math.floor((totalSeconds % 3600) / 60);
      const seconds = totalSeconds % 60;
      return hours ? `${hours}h ${String(minutes).padStart(2, '0')}m ${String(seconds).padStart(2, '0')}s` : `${minutes}m ${String(seconds).padStart(2, '0')}s`;
    }

    function renderOverview(status) {
      const counts = (status.queue || []).reduce((acc, item) => {
        acc[item.status] = (acc[item.status] || 0) + 1;
        return acc;
      }, {});
      const queueTotal = (status.queue || []).length;
      const doneCount = counts.done || 0;
      const stoppedCount = counts.stopped || 0;
      const failedCount = (status.failed_items || []).length;
      const progress = status.running || status.current_task_id
        ? Math.round(status.progress || 0)
        : queueTotal ? Math.round((doneCount / queueTotal) * 100) : Math.round(status.progress || 0);
      const statusClass = status.running ? 'indigo' : failedCount ? 'danger' : stoppedCount ? 'amber' : doneCount ? 'success' : 'slate';
      document.getElementById('overviewBox').innerHTML = `
        <div class="metric primary">
          <div class="metric-head"><span>队列任务</span><span class="metric-icon">▣</span></div>
          <b>${queueTotal}</b>
          <small>等待、处理与完成任务总数</small>
        </div>
        <div class="metric success">
          <div class="metric-head"><span>已完成</span><span class="metric-icon">✓</span></div>
          <b>${doneCount}</b>
          <small>${queueTotal ? ` ${progress}%` : '暂无任务'}</small>
        </div>
        <div class="metric ${failedCount ? 'danger' : 'slate'}">
          <div class="metric-head"><span>异常文件</span><span class="metric-icon">!</span></div>
          <b>${failedCount}</b>
          <small>${failedCount ? '请查看异常列表' : '当前没有异常'}</small>
        </div>
        <div class="metric metric-wide ${statusClass}">
          <div class="metric-head"><span>当前状态</span><span class="metric-icon">●</span></div>
          <b>${status.message || '等待就绪'}</b>
          <div class="overview-progress"><span style="width:${Math.max(0, Math.min(100, progress || 0))}%"></span></div>
          <div class="overview-meta">
            <span>进度 ${Math.max(0, Math.min(100, progress || 0))}%</span>
            <span>${status.started_at || '--'}</span>
            <span>结束 ${status.finished_at || '--'}</span>
            <span>总耗时 ${formatDuration(status.started_at, status.finished_at)}</span>
          </div>
        </div>
      `;
    }

    function applyStatus(s) {
      const logsChanged = mergeStatusLogs(s);
      latestStatus = s;
      document.getElementById('stateText').textContent = s.message || (s.running ? '处理中' : '等待就绪');
      document.getElementById('percentText').textContent = Math.round(s.progress || 0) + '%';
      document.getElementById('progressBar').style.width = (s.progress || 0) + '%';
      const totalQueueItems = (s.queue || []).length;
      const finishedQueueItems = (s.queue || []).filter(item => item.status === 'done').length;
      const queueCountText = document.getElementById('queueCountText');
      if (queueCountText) queueCountText.textContent = `${finishedQueueItems}/${totalQueueItems}`;
      document.getElementById('startQueueBtn').disabled = !!s.running;
      document.getElementById('stopBtn').disabled = !s.running;
      const dot = document.getElementById('stateDot');
      if (dot) {
        const hasFailed = (s.failed_items || []).length > 0;
        const allDone = !s.running && (s.queue || []).length > 0 && (s.queue || []).every(i => i.status === 'done');
        dot.className = 'state-dot' + (s.running ? ' running' : hasFailed ? ' failed' : allDone ? ' done' : '');
      }
      const queueSignature = JSON.stringify(s.queue || []);
      if (queueSignature !== lastQueueSignature) {
        renderQueue(s.queue);
        lastQueueSignature = queueSignature;
      }
      if (logsChanged) scheduleLogRender();
      const failedSignature = JSON.stringify(s.failed_items || []);
      if (failedSignature !== lastFailedSignature) {
        renderFailed(s.failed_items);
        lastFailedSignature = failedSignature;
      }
      const overviewSignature = JSON.stringify([
        queueSignature, s.running, s.progress, s.message,
        s.started_at, s.finished_at, s.error,
      ]);
      if (overviewSignature !== lastOverviewSignature) {
        renderOverview(s);
        lastOverviewSignature = overviewSignature;
      }
    }

    let statusRefreshInFlight = false;
    let statusPollTimer = 0;
    async function refreshStatus() {
      if (statusRefreshInFlight) return;
      statusRefreshInFlight = true;
      try {
        const query = new URLSearchParams({
          logs_after: String(lastLogSeq),
          log_epoch: String(currentLogEpoch),
        });
        const data = await api('/api/status?' + query.toString(), { timeoutMs: 10000 });
        applyStatus(data.status);
      } finally {
        statusRefreshInFlight = false;
      }
    }

    function scheduleStatusPoll() {
      clearTimeout(statusPollTimer);
      const delay = latestStatus?.running ? 1500 : 4000;
      statusPollTimer = setTimeout(async () => {
        try {
          await refreshStatus();
        } catch (_error) {
          // A temporary status failure should not stop future refreshes.
        } finally {
          scheduleStatusPoll();
        }
      }, delay);
    }

    function previewCover() {
      const path = form.manual_cover_path.value.trim();
      const img = document.getElementById('coverImg');
      const empty = document.getElementById('coverEmpty');
      const meta = document.getElementById('coverMeta');
      if (!path) {
        img.removeAttribute('src');
        img.style.display = 'none';
        empty.style.display = 'block';
        meta.textContent = '--';
        return;
      }
      const isRemote = /^https?:\/\//i.test(path);
      const src = isRemote ? path : '/api/cover?path=' + encodeURIComponent(path);
      meta.textContent = '正在读取封面...';
      img.onload = () => { meta.textContent = `${img.naturalWidth} × ${img.naturalHeight}`; };
      img.onerror = () => { img.style.display = 'none'; empty.style.display = 'block'; meta.textContent = '封面预览失败'; };
      img.src = src + (src.includes('?') ? '&' : '?') + 't=' + Date.now();
      img.style.display = 'block';
      empty.style.display = 'none';
    }

    async function openDirModal(path) {
      const data = await api('/api/browse?path=' + encodeURIComponent(path || browseCurrent || ''));
      const b = data.browser;
      browseCurrent = b.current;
      selectedDir = b.current;
      document.getElementById('dirPath').textContent = b.current;
      document.getElementById('dirUpBtn').disabled = !b.parent;
      document.getElementById('dirUpBtn').onclick = () => openDirModal(b.parent);
      const list = document.getElementById('dirList');
      list.innerHTML = '';
      b.dirs.forEach(dir => {
        const item = document.createElement('div');
        item.className = 'dir-item';
        item.innerHTML = `<strong>${dir.name}</strong><span>${dir.has_audio ? '🎵 音频' : '📁 目录'}</span>`;
        item.onmousedown = event => {
          if (event.detail > 1) event.preventDefault();
        };
        item.onclick = () => {
          selectedDir = dir.path;
          [...list.children].forEach(x => x.classList.remove('selected'));
          item.classList.add('selected');
        };
        item.ondblclick = event => {
          event.preventDefault();
          event.stopPropagation();
          window.getSelection()?.removeAllRanges();
          openDirModal(dir.path).catch(error => toast(error.message));
        };
        list.appendChild(item);
      });
      document.getElementById('dirModal').classList.add('show');
    }

    async function openCookieModal() {
      const data = await api('/api/cookies');
      document.getElementById('qidianCookie').value = data.cookies.qidian || '';
      document.getElementById('neteaseCookie').value = data.cookies.netease || '';
      document.getElementById('kuwoCookie').value = data.cookies.kuwo || '';
      document.getElementById('cookieModal').classList.add('show');
    }

    async function saveCookies() {
      await api('/api/cookies', { method: 'POST', body: JSON.stringify({cookies: {qidian: document.getElementById('qidianCookie').value, netease: document.getElementById('neteaseCookie').value, kuwo: document.getElementById('kuwoCookie').value}}) });
      document.getElementById('cookieModal').classList.remove('show');
      toast('Cookie 已保存');
    }

    function renderBlacklistPatterns() {
      const pool = document.getElementById('blacklistPool');
      pool.innerHTML = '';
      if (!blacklistPatterns.length) {
        const empty = document.createElement('span');
        empty.className = 'hint';
        empty.textContent = '暂无黑名单规则';
        pool.appendChild(empty);
        return;
      }
      blacklistPatterns.forEach((pattern, index) => {
        const chip = document.createElement('span');
        chip.className = 'chip colored-chip';
        const hue = (198 + index * 137.508) % 360;
        chip.style.setProperty('--chip-color-a', `hsl(${hue.toFixed(1)} 58% 46%)`);
        chip.style.setProperty('--chip-color-b', `hsl(${((hue + 22) % 360).toFixed(1)} 64% 34%)`);
        chip.style.setProperty('--chip-border', `hsl(${((hue + 10) % 360).toFixed(1)} 52% 58% / .55)`);
        chip.innerHTML = `<span>${pattern}</span>`;
        chip.title = '点击删除';
        chip.onclick = () => {
          blacklistPatterns.splice(index, 1);
          renderBlacklistPatterns();
        };
        pool.appendChild(chip);
      });
    }

    async function openBlacklistModal() {
      const data = await api('/api/tag-blacklist');
      document.getElementById('blacklistPath').textContent = `文件: ${data.path || 'tag_blacklist.txt'}`;
      blacklistPatterns = [...(data.patterns || [])];
      document.getElementById('blacklistInput').value = '';
      renderBlacklistPatterns();
      document.getElementById('blacklistModal').classList.add('show');
    }

    function addBlacklistPatternFromInput() {
      const input = document.getElementById('blacklistInput');
      const value = input.value.trim();
      if (!value) return;
      if (!blacklistPatterns.includes(value)) blacklistPatterns.push(value);
      input.value = '';
      renderBlacklistPatterns();
    }

    async function saveBlacklistPatterns() {
      const data = await api('/api/tag-blacklist', { method: 'POST', body: JSON.stringify({patterns: blacklistPatterns}) });
      blacklistPatterns = [...(data.patterns || [])];
      renderBlacklistPatterns();
      toast('标签黑名单已保存');
    }

    function exportLogs() {
      const text = (latestStatus?.logs || []).map(x => x.message).join('\n');
      const blob = new Blob([text], {type: 'text/plain;charset=utf-8'});
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'audiometa-nexus-log.txt';
      a.click();
      URL.revokeObjectURL(a.href);
    }

    async function previewRun() {
      const params = readForm();
      if (!validateRequired(params)) return;
      const data = await api('/api/preview', {method: 'POST', body: JSON.stringify({params})});
      const p = data.preview;
      toast(`预览：${p.audio_count} 个音频；输出目录：${p.output_name}${p.output_exists ? '（已存在）' : ''}`);
    }

    async function uploadCoverFromComputer(file) {
      if (!file) return;
      if (file.size > 12 * 1024 * 1024) return toast('封面图片不能超过 12 MB');
      const reader = new FileReader();
      reader.onload = async () => {
        try {
          const result = await api('/api/cover/upload', {
            method: 'POST',
            body: JSON.stringify({ name: file.name, data: reader.result }),
            timeoutMs: 30000,
          });
          form.manual_cover_path.value = result.path || '';
          previewCover();
          toast('封面上传成功');
        } catch (error) {
          toast(error.message || '封面上传失败');
        }
      };
      reader.onerror = () => toast('无法读取封面文件');
      reader.readAsDataURL(file);
    }

    async function showHealth() {
      const data = await api('/api/health');
      const h = data.health;
      toast(`健康检查：FFmpeg ${h.ffmpeg ? '正常' : '缺失'}，FFprobe ${h.ffprobe ? '正常' : '缺失'}，SSL ${h.ssl_verify ? '已校验' : '未校验'}`);
    }

    function setWebToken() {
      const current = localStorage.getItem('audiometa-web-token') || '';
      const token = prompt('请输入 Web 访问令牌；留空表示清除令牌', current);
      if (token === null) return;
      if (token.trim()) localStorage.setItem('audiometa-web-token', token.trim());
      else localStorage.removeItem('audiometa-web-token');
      toast('访问令牌已更新');
    }

    async function qualityCheck() {
      const params = readForm();
      if (!params.input_folder) return toast('请先填写音频目录');
      const data = await api('/api/quality-check', {method: 'POST', body: JSON.stringify({params})});
      const r = data.report;
      const details = r.issues.slice(0, 8).map(x => `${x.file ? x.file.split(/[\\/]/).pop() + ': ' : ''}${x.type}`).join('\n');
      alert(`音频：${r.audio_count} 个\n格式：${r.formats.join(', ') || '--'}\n问题：${r.issues.length} 个\n封面：${r.cover_found ? `${r.cover_pixels || 0} 像素` : '缺失'}${details ? `\n\n详情：\n${details}` : ''}`);
    }

    function downloadConfig() {
      api('/api/config/export').then(data => {
        const blob = new Blob([JSON.stringify(data.params, null, 2)], {type: 'application/json;charset=utf-8'});
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = 'audiometa-config.json';
        a.click();
        URL.revokeObjectURL(a.href);
      }).catch(e => toast(e.message));
    }

    function importConfig() {
      const input = document.createElement('input');
      input.type = 'file';
      input.accept = '.json,application/json';
      input.onchange = async () => {
        const file = input.files && input.files[0];
        if (!file) return;
        try {
          const params = JSON.parse(await file.text());
          const data = await api('/api/config/import', {method: 'POST', body: JSON.stringify({params})});
          fillForm(data.params);
          toast('配置已导入');
        } catch (e) { toast(e.message); }
      };
      input.click();
    }

    async function restoreSnapshot() {
      const folder = String(form.input_folder.value || '').trim();
      if (!folder) return toast('请先填写音频目录');
      const data = await api('/api/snapshot?path=' + encodeURIComponent(folder));
      if (!data.snapshot || !data.snapshot.input_folder) return toast('当前目录没有可用快照');
      if (!confirm('仅恢复处理前目录名称，不会删除音频或元数据。是否继续？')) return;
      const result = await api('/api/snapshot/restore', {method: 'POST', body: JSON.stringify({path: folder})});
      if (result.result && result.result.path) form.input_folder.value = result.result.path;
      toast('目录名称已恢复');
    }

    const toolbox = document.getElementById('settingsModal');
    if (toolbox) {
      const makeToolButton = (id, text, handler) => {
        let button = document.getElementById(id);
        if (!button) {
          button = document.createElement('button');
          button.type = 'button'; button.id = id; button.textContent = text;
          toolbox.appendChild(button);
        }
        button.onclick = handler;
      };
      makeToolButton('previewRunBtn', '预览处理', () => previewRun().catch(e => toast(e.message)));
      makeToolButton('healthBtn', '健康检查', () => showHealth().catch(e => toast(e.message)));
      makeToolButton('webTokenBtn', '设置访问令牌', setWebToken);
      makeToolButton('qualityBtn', '质量检查', () => qualityCheck().catch(e => toast(e.message)));
      makeToolButton('exportConfigBtn', '导出配置', downloadConfig);
      makeToolButton('importConfigBtn', '导入配置', importConfig);
      makeToolButton('restoreSnapshotBtn', '撤销目录改名', () => restoreSnapshot().catch(e => toast(e.message)));
      makeToolButton('batchImportBtn', '批量导入目录', () => {
        const input = document.createElement('input');
        input.type = 'file'; input.multiple = true; input.webkitdirectory = true;
        input.onchange = async () => {
          const folders = [...new Set([...input.files].map(file => {
            const rel = file.webkitRelativePath || '';
            return rel.split('/')[0];
          }).filter(Boolean))];
          const root = String(form.input_folder.value || '').trim();
          const paths = folders.map(name => root ? root.replace(/[\\/]$/, '') + '/' + name : name);
          if (!paths.length) return toast('未选择目录');
          const data = await api('/api/queue/add-batch', {method: 'POST', body: JSON.stringify({paths, params: readForm()})});
          if (data.status) applyStatus(data.status);
          toast(`已加入 ${paths.length} 个目录`);
        };
        input.click();
      });
    }
    const logPanel = document.getElementById('panel-log');
    if (logPanel && !document.getElementById('logFilterBox')) {
      const filterBox = document.createElement('div');
      filterBox.id = 'logFilterBox';
      filterBox.className = 'inline';
      filterBox.style.marginBottom = '8px';
      filterBox.innerHTML = '<select id="logLevelFilter"><option value="all">全部级别</option><option value="info">信息</option><option value="warning">警告</option><option value="error">错误</option></select><input id="logKeywordFilter" placeholder="筛选日志关键词" />';
      logPanel.insertBefore(filterBox, logPanel.firstChild);
      document.getElementById('logLevelFilter').onchange = e => { logFilter = e.target.value; scheduleLogRender(true); };
      document.getElementById('logKeywordFilter').oninput = e => { logKeyword = e.target.value.trim(); scheduleLogRender(true); };
    }

    initMobileSections();
    installUiIcons();
    document.querySelectorAll('.tab').forEach(btn => btn.onclick = () => {
      document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
      btn.classList.add('active');
      document.querySelectorAll('.queue-console > .tab-panel').forEach(x => x.classList.remove('active'));
      document.getElementById('panel-' + btn.dataset.tab).classList.add('active');
      if (btn.dataset.tab === 'log') scheduleLogRender(true);
    });
    form.addEventListener('input', e => e.target.classList.remove('field-error'));
    form.addEventListener('change', e => e.target.classList.remove('field-error'));
    document.getElementById('browseBtn').onclick = () => openDirModal(form.input_folder.value).catch(e => toast(e.message));
    form.input_folder.addEventListener('change', () => loadFolderConfig(form.input_folder.value).catch(e => toast(e.message)));
    document.getElementById('closeDirBtn').onclick = () => document.getElementById('dirModal').classList.remove('show');
    document.getElementById('chooseDirBtn').onclick = () => {
      form.input_folder.value = selectedDir;
      document.getElementById('dirModal').classList.remove('show');
      loadFolderConfig(selectedDir).catch(e => toast(e.message));
    };
    document.getElementById('saveConfigBtn').onclick = () => saveConfig().catch(e => toast(e.message));
    document.getElementById('loadConfigBtn').onclick = () => loadConfig().catch(e => toast(e.message));
    document.getElementById('settingsBtn').onclick = openSettingsModal;
    document.getElementById('closeSettingsBtn').onclick = closeSettingsModal;
    document.getElementById('doneSettingsBtn').onclick = closeSettingsModal;
    document.getElementById('settingsLoadConfigBtn').onclick = () => loadConfig().catch(e => toast(e.message));
    document.getElementById('settingsSaveConfigBtn').onclick = () => saveConfig().catch(e => toast(e.message));
    document.getElementById('fetchBtn').onclick = () => fetchMetadata().catch(e => toast(e.message));
    document.getElementById('fetchAuthorBtn').onclick = () => fetchAuthorByTitle().catch(e => toast(e.message));
    document.getElementById('searchTitleBtn').onclick = () => searchByTitle().catch(e => toast(e.message));
    document.getElementById('titleSearchBackdrop').onclick = () => {
      closeTitleSearchResults();
      closeAuthorSearchResults();
    };
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape') {
        closeTitleSearchResults();
        closeAuthorSearchResults();
        closeSettingsModal();
      }
    });
    document.getElementById('addQueueBtn').onclick = () => addQueueFast().catch(e => toast(e.message));
    document.getElementById('startQueueBtn').onclick = () => startQueue().catch(e => toast(e.message));
    document.getElementById('stopBtn').onclick = () => stopTask().catch(e => toast(e.message));
    document.getElementById('removeQueueBtn').onclick = () => removeSelectedQueueStable().catch(e => toast(e.message));
    document.getElementById('editQueueBtn').onclick = () => editSelectedQueue();
    document.getElementById('clearQueueBtn').onclick = () => clearQueue().catch(e => toast(e.message));
    document.getElementById('coverChangeBtn').onclick = () => document.getElementById('coverFileInput').click();
    document.getElementById('coverFileInput').addEventListener('change', event => {
      uploadCoverFromComputer(event.target.files?.[0]);
      event.target.value = '';
    });
    document.getElementById('clearBtn').onclick = () => clearAll().catch(e => toast(e.message));
    document.getElementById('failedBtn').onclick = () => document.querySelector('[data-tab="failed"]').click();
    document.getElementById('retryBtn').onclick = () => retryFailedQueue().catch(e => toast(e.message));
    document.getElementById('cookieBtn').onclick = () => {
      closeSettingsModal();
      openCookieModal().catch(e => toast(e.message));
    };
    document.getElementById('blacklistBtn').onclick = () => {
      closeSettingsModal();
      openBlacklistModal().catch(e => toast(e.message));
    };
    document.getElementById('exportLogBtn').onclick = exportLogs;
    document.getElementById('clearLogBtn').onclick = () => {
      clientLogs = [];
      lastRenderedLogSeq = lastLogSeq;
      lastRenderedLogEpoch = currentLogEpoch;
      renderLogs([]);
      toast('已清空当前日志视图');
    };
    document.getElementById('closeCookieBtn').onclick = () => document.getElementById('cookieModal').classList.remove('show');
    document.getElementById('saveCookieBtn').onclick = () => saveCookies().catch(e => toast(e.message));
    document.getElementById('closeBlacklistBtn').onclick = () => document.getElementById('blacklistModal').classList.remove('show');
    document.getElementById('saveBlacklistBtn').onclick = () => saveBlacklistPatterns().catch(e => toast(e.message));
    document.getElementById('blacklistInput').addEventListener('keydown', e => {
      if (e.key !== 'Enter') return;
      e.preventDefault();
      addBlacklistPatternFromInput();
    });

    (async function init() {
      await loadOptions();
      initCustomSelects();
      await loadConfig({ startup: true });
      await refreshStatus();
      scheduleStatusPoll();
    })().catch(e => toast(e.message));
  </script>
</body>
</html>"""


def main():
    host = os.environ.get("WEB_HOST", "0.0.0.0")
    port = int(os.environ.get("WEB_PORT", DEFAULT_PORT))
    server = ThreadingHTTPServer((host, port), RequestHandler)
    print(f"{APP_TITLE} 已启动：http://{host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

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

from api_clients import (
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
from config import CATEGORY_MAP, FFMPEG_PATH, FFPROBE_PATH, NETWORK_VERIFY_SSL, get_platform_cookies, get_platform_options, set_platform_cookies
from network_utils import clean_html_tags, extract_bytedance_snowflake_year, fetch_share_page_html, get_safe_session, parse_fanqie_share_html, parse_qidian_share_html
from processor import load_process_params, load_operation_snapshot, process_audio_books, restore_operation_snapshot
from audio_core import batch_get_audio_info, find_cover, get_audio_list, get_image_resolution
from metadata_helpers import build_output_folder_name


APP_TITLE = "声境元枢 AudioMeta Nexus"
DEFAULT_PORT = 8787
CONTAINER_CONFIG_PATH = Path("/config/process_params.json")
LOCAL_CONFIG_PATH = Path("docker/config/process_params.json")
CONTAINER_DATA_PATH = Path("/data")
LOCAL_DATA_PATH = Path("docker/data")
RESOURCE_DIR = Path(__file__).resolve().parent
ICON_PATH = RESOURCE_DIR / "icon.ico"
FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48" width="48" height="48">
  <g fill="none" stroke-linecap="round" stroke-linejoin="round" opacity="0.92">
    <path d="M24 6c8.7 0 15.8 5.2 18.9 12.5-5.9-3.3-12.7-3.2-17.7.3-4.8 3.4-7 9.1-5.6 14.4C12.4 31.4 7 25 7 17.4 11.4 10.4 17 6 24 6Z" stroke="#7464f6" stroke-width="3.5"/>
    <path d="M40.8 20.9c4.3 7.5 2.9 16.2-2.2 22-1.1-6.7-4.6-12.5-10.2-14.8-5.4-2.2-11.4-.8-15.3 3.1-1.9-7.2 1-14.9 7.5-18.7 8.3-.2 16.7 2.2 20.2 8.4Z" stroke="#8a77ff" stroke-width="3.5"/>
    <path d="M35.9 40.8c-4.4 7.5-12.8 10.8-20.5 9.3 5.7-3.8 9.1-9.6 8.4-15.6-.7-5.8-4.7-10.3-10-11.7 5.3-5.3 13.4-6.5 19.9-2.7 4.1 7.2 5.7 15.2 2.2 20.7Z" stroke="#5e78ec" stroke-width="3.5"/>
  </g>
  <circle cx="24" cy="24" r="4.2" fill="#a493ff"/>
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
  <link rel="icon" href="/favicon.svg?v=2" type="image/svg+xml" sizes="any" />
  <title>声境元枢 · AudioMeta Nexus</title>
  <style>
    /* ══════════════════════════════════════════════
       DESIGN TOKENS  —  Dark Theme (default)
       ══════════════════════════════════════════════ */
    :root,
    html[data-theme="dark"] {
      --bg:           #050a14;
      --surface:      #0b1220;
      --surface-2:    #101a2c;
      --surface-3:    #172238;
      --glass:        rgba(255,255,255,.035);
      --border:       rgba(255,255,255,.07);
      --border-med:   rgba(255,255,255,.13);
      --border-strong:rgba(255,255,255,.22);
      --text:         #e8edf8;
      --text-2:       #8b99b8;
      --text-3:       #4e5f7a;
      --primary:      #6f7df7;
      --primary-light:#8f9cff;
      --primary-glow: rgba(111,125,247,.32);
      --primary-bg:   rgba(111,125,247,.13);
      --success:      #10b981;
      --success-glow: rgba(16,185,129,.28);
      --success-bg:   rgba(16,185,129,.12);
      --warning:      #f59e0b;
      --warning-glow: rgba(245,158,11,.28);
      --warning-bg:   rgba(245,158,11,.12);
      --danger:       #ef4444;
      --danger-glow:  rgba(239,68,68,.28);
      --danger-bg:    rgba(239,68,68,.12);
      --cyan:         #22d3ee;
      --input-bg:     #060912;
      --log-bg:       #060913;
      --log-text:     #cbd5e1;
      --scrollbar:    rgba(255,255,255,.1);
      --scrollbar-h:  rgba(255,255,255,.18);
      --shadow-sm:    0 2px 8px rgba(0,0,0,.4);
      --shadow:       0 8px 28px rgba(0,0,0,.45);
      --shadow-lg:    0 24px 56px rgba(0,0,0,.62);
      --select-arrow: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'%3E%3Cpath d='M1 1l5 5 5-5' stroke='%235a6882' stroke-width='1.6' fill='none' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
      --grad-body-a:  rgba(99,102,241,.13);
      --grad-body-b:  rgba(139,92,246,.09);
      --grad-h1-start:#e8edf8;
      --modal-mask:   rgba(0,0,0,.72);
    }

    /* ══════════════════════════════════════════════
       DESIGN TOKENS  —  Light Theme
       ══════════════════════════════════════════════ */
    html[data-theme="light"] {
      --bg:           #f4f6fa;
      --surface:      #ffffff;
      --surface-2:    #f4f6fb;
      --surface-3:    #e9edf5;
      --glass:        rgba(0,0,0,.024);
      --border:       rgba(0,0,0,.08);
      --border-med:   rgba(0,0,0,.13);
      --border-strong:rgba(0,0,0,.22);
      --text:         #111827;
      --text-2:       #4b5675;
      --text-3:       #8391a8;
      --primary:      #5263d9;
      --primary-light:#6b79e8;
      --primary-glow: rgba(82,99,217,.22);
      --primary-bg:   rgba(82,99,217,.09);
      --success:      #059669;
      --success-glow: rgba(5,150,105,.2);
      --success-bg:   rgba(5,150,105,.09);
      --warning:      #d97706;
      --warning-glow: rgba(217,119,6,.2);
      --warning-bg:   rgba(217,119,6,.09);
      --danger:       #dc2626;
      --danger-glow:  rgba(220,38,38,.2);
      --danger-bg:    rgba(220,38,38,.09);
      --cyan:         #0891b2;
      --input-bg:     #ffffff;
      --log-bg:       #f8fafc;
      --log-text:     #334155;
      --scrollbar:    rgba(0,0,0,.12);
      --scrollbar-h:  rgba(0,0,0,.2);
      --shadow-sm:    0 2px 8px rgba(0,0,0,.1);
      --shadow:       0 8px 28px rgba(0,0,0,.12);
      --shadow-lg:    0 24px 56px rgba(0,0,0,.18);
      --select-arrow: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'%3E%3Cpath d='M1 1l5 5 5-5' stroke='%238895b2' stroke-width='1.6' fill='none' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
      --grad-body-a:  rgba(79,70,229,.06);
      --grad-body-b:  rgba(139,92,246,.04);
      --grad-h1-start:#1a1f3a;
      --modal-mask:   rgba(0,0,0,.42);
    }
    html[data-theme="linen"] {
      color-scheme: light;
      --bg:           #f7f2e9;
      --surface:      #fffcf6;
      --surface-2:    #f8f1e5;
      --surface-3:    #efe4d3;
      --glass:        rgba(120,80,40,.03);
      --border:       #e3d7c6;
      --border-med:   #d3c2a9;
      --border-strong:#b9a486;
      --text:         #2a2118;
      --text-2:       #695741;
      --text-3:       #9d8769;
      --primary:      #b45309;
      --primary-light:#d97706;
      --primary-glow: rgba(180,83,9,.18);
      --primary-bg:   rgba(180,83,9,.09);
      --success:      #15803d;
      --success-glow: rgba(21,128,61,.16);
      --success-bg:   rgba(21,128,61,.08);
      --warning:      #b45309;
      --warning-glow: rgba(180,83,9,.16);
      --warning-bg:   rgba(180,83,9,.08);
      --danger:       #be123c;
      --danger-glow:  rgba(190,18,60,.16);
      --danger-bg:    rgba(190,18,60,.08);
      --cyan:         #0f766e;
      --input-bg:     #fffdf8;
      --log-bg:       #fbf7f0;
      --log-text:     #65523c;
      --scrollbar:    rgba(120,80,40,.15);
      --scrollbar-h:  rgba(120,80,40,.26);
      --shadow-sm:    0 2px 8px rgba(80,55,25,.08);
      --shadow:       0 8px 28px rgba(80,55,25,.12);
      --shadow-lg:    0 24px 56px rgba(80,55,25,.18);
      --select-arrow: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'%3E%3Cpath d='M1 1l5 5 5-5' stroke='%239d8769' stroke-width='1.6' fill='none' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
      --grad-body-a:  rgba(180,83,9,.07);
      --grad-body-b:  rgba(15,118,110,.05);
      --grad-h1-start:#2a2118;
      --modal-mask:   rgba(0,0,0,.42);
    }
    html[data-theme="mint"] {
      color-scheme: light;
      --bg:           #eef5f1;
      --surface:      #ffffff;
      --surface-2:    #f1f8f4;
      --surface-3:    #dcebe3;
      --glass:        rgba(15,118,110,.03);
      --border:       #cfe7db;
      --border-med:   #b6d9c8;
      --border-strong:#91bfab;
      --text:         #123129;
      --text-2:       #3f6a5c;
      --text-3:       #719b8b;
      --primary:      #0f766e;
      --primary-light:#0d9488;
      --primary-glow: rgba(15,118,110,.16);
      --primary-bg:   rgba(15,118,110,.08);
      --success:      #047857;
      --success-glow: rgba(4,120,87,.16);
      --success-bg:   rgba(4,120,87,.08);
      --warning:      #a16207;
      --warning-glow: rgba(161,98,7,.16);
      --warning-bg:   rgba(161,98,7,.08);
      --danger:       #be123c;
      --danger-glow:  rgba(190,18,60,.16);
      --danger-bg:    rgba(190,18,60,.08);
      --cyan:         #0891b2;
      --input-bg:     #ffffff;
      --log-bg:       #f2faf6;
      --log-text:     #41665a;
      --scrollbar:    rgba(15,118,110,.14);
      --scrollbar-h:  rgba(15,118,110,.26);
      --shadow-sm:    0 2px 8px rgba(13,70,60,.08);
      --shadow:       0 8px 28px rgba(13,70,60,.12);
      --shadow-lg:    0 24px 56px rgba(13,70,60,.18);
      --select-arrow: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'%3E%3Cpath d='M1 1l5 5 5-5' stroke='%23719b8b' stroke-width='1.6' fill='none' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
      --grad-body-a:  rgba(15,118,110,.07);
      --grad-body-b:  rgba(8,145,178,.05);
      --grad-h1-start:#123129;
      --modal-mask:   rgba(0,0,0,.42);
    }
    html[data-theme="rose"] {
      color-scheme: light;
      --bg:           #faf3f5;
      --surface:      #fffdfd;
      --surface-2:    #fdf3f5;
      --surface-3:    #f6e3e8;
      --glass:        rgba(190,18,60,.03);
      --border:       #eed7de;
      --border-med:   #dfbcc7;
      --border-strong:#c794a4;
      --text:         #3a1b24;
      --text-2:       #7a4654;
      --text-3:       #ae7888;
      --primary:      #be123c;
      --primary-light:#e11d48;
      --primary-glow: rgba(190,18,60,.15);
      --primary-bg:   rgba(190,18,60,.08);
      --success:      #047857;
      --success-glow: rgba(4,120,87,.16);
      --success-bg:   rgba(4,120,87,.08);
      --warning:      #a16207;
      --warning-glow: rgba(161,98,7,.16);
      --warning-bg:   rgba(161,98,7,.08);
      --danger:       #be123c;
      --danger-glow:  rgba(190,18,60,.16);
      --danger-bg:    rgba(190,18,60,.08);
      --cyan:         #0e7490;
      --input-bg:     #fffdfd;
      --log-bg:       #fdf4f6;
      --log-text:     #75434f;
      --scrollbar:    rgba(190,18,60,.14);
      --scrollbar-h:  rgba(190,18,60,.26);
      --shadow-sm:    0 2px 8px rgba(100,35,50,.08);
      --shadow:       0 8px 28px rgba(100,35,50,.12);
      --shadow-lg:    0 24px 56px rgba(100,35,50,.18);
      --select-arrow: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'%3E%3Cpath d='M1 1l5 5 5-5' stroke='%23ae7888' stroke-width='1.6' fill='none' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
      --grad-body-a:  rgba(190,18,60,.07);
      --grad-body-b:  rgba(14,116,144,.05);
      --grad-h1-start:#3a1b24;
      --modal-mask:   rgba(0,0,0,.42);
    }
    html[data-theme="dark"] { color-scheme: dark; }
    html[data-theme="light"] { color-scheme: light; }

    /* ══════════════════════════════════════════════
       SHARED  —  radius / spacing constants
       ══════════════════════════════════════════════ */
    :root {
      --radius:    14px;
      --radius-sm: 10px;
      --radius-xs: 8px;
    }
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    html, body { width: 100%; height: 100%; }
    html { transition: background .25s ease, color .25s ease; }
    body {
      background:
        radial-gradient(ellipse 90% 55% at 18% -8%, var(--grad-body-a), transparent),
        radial-gradient(ellipse 60% 45% at 82% 108%, var(--grad-body-b), transparent),
        var(--bg);
      color: var(--text);
      font: 14px/1.5 "Inter", "PingFang SC", "Microsoft YaHei UI", system-ui, sans-serif;
      -webkit-font-smoothing: antialiased;
      text-rendering: optimizeLegibility;
      overflow: hidden;
      transition: background .25s ease, color .2s ease;
    }
    ::-webkit-scrollbar { width: 5px; height: 5px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: var(--scrollbar); border-radius: 99px; }
    ::-webkit-scrollbar-thumb:hover { background: var(--scrollbar-h); }
    button, input, select, textarea { font: inherit; }

    /* ── Buttons ──────────────────────────────── */
    button {
      display: inline-flex; align-items: center; justify-content: center; gap: 5px;
      border: 1px solid var(--border-med);
      min-height: 36px; padding: 0 14px;
      background: var(--surface-3);
      color: var(--text);
      font-size: 13px; font-weight: 600;
      cursor: pointer; border-radius: var(--radius-xs);
      transition: background .15s ease, border-color .15s ease, box-shadow .15s ease, transform .12s ease, color .15s ease;
      white-space: nowrap; letter-spacing: .01em;
    }
    button:hover:not(:disabled) {
      /* Only change the colour layer here. Using the `background` shorthand
         resets a coloured button's gradient and makes it look white in the
         light theme. */
      background-color: var(--surface-2); border-color: var(--border-strong);
      transform: translateY(-1px); box-shadow: var(--shadow-sm);
    }
    .btn-primary:hover:not(:disabled), .btn-green:hover:not(:disabled),
    .btn-amber:hover:not(:disabled), .btn-red:hover:not(:disabled),
    .btn-indigo:hover:not(:disabled) { color: #fff; }
    button:active:not(:disabled) { transform: translateY(0); }
    button:disabled { opacity: .38; cursor: not-allowed; }
    .btn-primary {
      background: linear-gradient(135deg, var(--primary), #8b5cf6);
      border-color: var(--primary-glow); color: #fff;
      box-shadow: 0 0 22px var(--primary-glow);
    }
    .btn-primary:hover:not(:disabled) {
      background: linear-gradient(135deg, var(--primary), #8b5cf6);
      box-shadow: 0 4px 28px var(--primary-glow); filter: brightness(1.08);
    }
    .btn-green {
      background: linear-gradient(135deg, var(--success), #047857);
      border-color: var(--success-glow); color: #fff;
      box-shadow: 0 0 18px var(--success-glow);
    }
    .btn-green:hover:not(:disabled) {
      background: linear-gradient(135deg, var(--success), #047857);
      box-shadow: 0 4px 26px var(--success-glow); filter: brightness(1.08);
    }
    .btn-amber {
      background: linear-gradient(135deg, var(--warning), #b45309);
      border-color: var(--warning-glow); color: #fff;
      box-shadow: 0 0 16px var(--warning-glow);
    }
    .btn-amber:hover:not(:disabled) {
      background: linear-gradient(135deg, var(--warning), #b45309);
      box-shadow: 0 4px 24px var(--warning-glow); filter: brightness(1.08);
    }
    .btn-red {
      background: linear-gradient(135deg, var(--danger), #b91c1c);
      border-color: var(--danger-glow); color: #fff;
      box-shadow: 0 0 16px var(--danger-glow);
    }
    .btn-red:hover:not(:disabled) {
      background: linear-gradient(135deg, var(--danger), #b91c1c);
      box-shadow: 0 4px 24px var(--danger-glow); filter: brightness(1.08);
    }
    .btn-indigo {
      background: linear-gradient(135deg, #4f46e5, #7c3aed);
      border-color: rgba(79,70,229,.45); color: #fff;
      box-shadow: 0 0 16px rgba(79,70,229,.28);
    }
    .btn-indigo:hover:not(:disabled) {
      background: linear-gradient(135deg, #4f46e5, #7c3aed);
      box-shadow: 0 4px 24px rgba(79,70,229,.4); filter: brightness(1.08);
    }
    .quiet-button {
      background: var(--glass); border-color: var(--border); color: var(--text-2);
    }
    .quiet-button:hover:not(:disabled) { background: var(--surface-2); color: var(--text); }

    /* ── Theme Toggle ─────────────────────────── */
    .theme-toggle {
      width: 34px; height: 34px; min-height: 34px; padding: 0;
      border-radius: 50%; flex-shrink: 0;
      background: var(--surface-3); border: 1px solid var(--border-med);
      color: var(--text-2); font-size: 15px; cursor: pointer;
      transition: background .2s ease, border-color .2s ease, color .2s ease, transform .15s ease;
    }
    .theme-toggle:hover:not(:disabled) {
      background: var(--primary-bg); border-color: var(--primary);
      color: var(--primary-light); transform: rotate(18deg) translateY(-1px);
    }

    /* ── Layout ──────────────────────────────── */
    .app {
      height: 100vh;
      display: grid;
      grid-template-columns: minmax(640px, 48vw) 1fr;
    }
    .left, .right { min-width: 0; min-height: 0; }
    .left {
      display: flex; flex-direction: column;
      padding: 20px 16px 14px 20px;
      border-right: 1px solid var(--border);
      background: var(--surface);
      overflow: hidden;
      transition: background .25s ease, border-color .25s ease;
    }
    .right { display: flex; flex-direction: column; background: var(--bg); transition: background .25s ease; }

    /* ── Header ──────────────────────────────── */
    .app-header {
      display: grid; grid-template-columns: 1fr auto;
      align-items: start; gap: 12px; margin-bottom: 14px;
    }
    .app-title { display: flex; flex-direction: column; gap: 2px; }
    h1 {
      font-size: 22px; font-weight: 800; letter-spacing: -.02em; line-height: 1.15;
      background: linear-gradient(135deg, var(--grad-h1-start) 30%, var(--primary-light) 100%);
      -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
    }
    .app-subtitle {
      font-size: 11px; color: var(--text-3); font-weight: 500;
      letter-spacing: .05em; text-transform: uppercase;
    }
    .header-right { display: flex; align-items: flex-start; gap: 8px; }
    .status-card { display: flex; flex-direction: column; align-items: flex-end; gap: 2px; }
    .state-row { display: flex; align-items: center; gap: 5px; }
    .state-dot {
      width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0;
      background: var(--text-3); transition: background .3s ease;
    }
    .state-dot.running { background: var(--success); animation: pulse-border 1.4s ease-in-out infinite; }
    .state-dot.done    { background: var(--success); }
    .state-dot.failed  { background: var(--danger); }
    #stateText { font-size: 12px; color: var(--text-2); font-weight: 600; }
    .percent {
      font-size: 26px; font-weight: 900; letter-spacing: -.03em; line-height: 1;
      background: linear-gradient(135deg, var(--primary-light), var(--cyan));
      -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
    }

    /* ── Progress ────────────────────────────── */
    .progress-track {
      height: 3px; margin-bottom: 13px;
      background: var(--border);
      border-radius: 99px; overflow: hidden;
    }
    @keyframes pb-shimmer {
      0%   { background-position: 200% center; }
      100% { background-position: -200% center; }
    }
    .progress-bar {
      width: 0%; height: 100%;
      background: linear-gradient(90deg,
        var(--primary) 0%, var(--cyan) 40%, var(--success) 70%, var(--primary) 100%);
      background-size: 200% auto;
      border-radius: inherit;
      transition: width .35s ease;
      animation: pb-shimmer 3s linear infinite;
    }

    /* ── Hero Actions ─────────────────────────── */
    .hero-actions { display: flex; gap: 10px; margin-bottom: 13px; }
    .ha-process { display: flex; gap: 8px; flex: 3; min-width: 0; }
    .ha-config  {
      display: flex; gap: 8px; flex: 2; min-width: 0;
      border-left: 1px solid var(--border); padding-left: 10px;
    }
    .ha-process button, .ha-config button {
      flex: 1; min-height: 40px; min-width: 0;
      font-size: 12.5px; font-weight: 700; border-radius: var(--radius-sm);
    }

    /* ── Form ─────────────────────────────────── */
    .form-scroll {
      flex: 1; min-height: 0; overflow-y: auto; overflow-x: hidden; padding-right: 6px;
    }
    .section {
      background: var(--surface-2);
      border: 1px solid var(--border);
      border-radius: var(--radius); padding: 15px; margin-bottom: 9px;
      box-shadow: inset 3px 0 0 transparent;
      transition: border-color .2s ease, background .25s ease, box-shadow .2s ease;
    }
    .section:hover {
      border-color: var(--border-med);
      box-shadow: inset 3px 0 0 var(--primary);
    }
    .section-title {
      display: flex; align-items: center; gap: 8px;
      font-size: 13px; font-weight: 700; color: var(--text);
      margin-bottom: 12px; letter-spacing: .01em;
    }
    .section-toggle {
      display: none; margin-left: auto; width: 26px; height: 26px; min-height: 26px;
      padding: 0; border-radius: 50%; font-size: 13px; background: var(--glass);
      color: var(--text-3); border-color: var(--border);
    }
    .section-icon {
      width: 22px; height: 22px; display: grid; place-items: center;
      border-radius: 6px; font-size: 12px; line-height: 0; flex-shrink: 0;
      /* default — overridden per section below */
      background: var(--primary-bg); color: var(--primary-light);
    }
    /* per-section icon palette */
    .section:nth-child(1) .section-icon { background: var(--primary-bg);  color: var(--primary-light); }
    .section:nth-child(2) .section-icon { background: var(--success-bg);  color: var(--success); }
    .section:nth-child(3) .section-icon { background: var(--warning-bg);  color: var(--warning); }
    .section:nth-child(4) .section-icon { background: rgba(34,211,238,.12); color: var(--cyan); }
    .grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
    .grid-3 { grid-template-columns: repeat(3, minmax(0, 1fr)); }
    .span-2 { grid-column: span 2; }
    .span-3 { grid-column: span 3; }
    label {
      display: block; font-size: 11px; font-weight: 700; color: var(--text-3);
      margin-bottom: 6px; text-transform: uppercase; letter-spacing: .05em;
    }
    .field-label-row {
      min-height: 27px; display: flex; align-items: center; justify-content: space-between;
      gap: 8px; margin-bottom: 6px;
    }
    .field-label-row label { min-width: 0; margin-bottom: 0; }
    .field-mini-action {
      flex: 0 0 auto; min-height: 26px; padding: 0 10px;
      border-radius: 99px; border-color: color-mix(in srgb, var(--primary) 30%, var(--border));
      background: color-mix(in srgb, var(--primary) 12%, var(--surface-2));
      color: var(--primary-light); box-shadow: none;
      font-size: 11px; font-weight: 700; letter-spacing: 0;
    }
    .field-mini-action:hover:not(:disabled) {
      background: color-mix(in srgb, var(--primary) 20%, var(--surface-2));
      border-color: var(--primary); color: var(--primary-light); box-shadow: none;
    }
    input, select, textarea {
      width: 100%; border: 1px solid var(--border); outline: none;
      background: var(--input-bg); color: var(--text);
      min-height: 40px; padding: 8px 12px;
      border-radius: var(--radius-xs); font-size: 14px; line-height: 1.4;
      transition: border-color .15s ease, box-shadow .15s ease, background .2s ease, color .2s ease;
    }
    input::placeholder, textarea::placeholder { color: var(--text-3); }
    select {
      appearance: none; cursor: pointer;
      background-image: var(--select-arrow);
      background-repeat: no-repeat; background-position: calc(100% - 10px) center;
      background-color: var(--input-bg);
      padding-right: 30px;
    }
    select option, select optgroup {
      background-color: var(--surface);
      color: var(--text);
    }
    select option:checked {
      background-color: var(--primary);
      color: #fff;
    }
    .custom-select {
      position: relative; width: 100%; min-width: 0;
    }
    .custom-select-native {
      position: absolute !important; width: 1px !important; height: 1px !important;
      min-height: 0 !important; padding: 0 !important; margin: 0 !important;
      opacity: 0; pointer-events: none; overflow: hidden;
    }
    .custom-select-trigger {
      width: 100%; min-height: 40px; padding: 8px 12px;
      justify-content: space-between; gap: 12px;
      background: var(--input-bg); color: var(--text);
      border: 1px solid var(--border); border-radius: var(--radius-xs);
      box-shadow: none; font-size: 14px; font-weight: 500;
      text-align: left; letter-spacing: 0;
    }
    .custom-select-trigger:hover:not(:disabled) {
      background: var(--surface-3); border-color: var(--border-med);
      color: var(--text); transform: none; box-shadow: none;
    }
    .custom-select-trigger:focus-visible, .custom-select-trigger.open {
      outline: none; border-color: var(--primary);
      background: var(--primary-bg);
      box-shadow: 0 0 0 3px color-mix(in srgb, var(--primary) 18%, transparent);
    }
    .custom-select-arrow {
      width: 9px; height: 9px; flex: 0 0 9px;
      border-right: 2px solid var(--text-3); border-bottom: 2px solid var(--text-3);
      transform: rotate(45deg) translateY(-2px);
      transition: transform .18s ease, border-color .18s ease;
    }
    .custom-select-trigger.open .custom-select-arrow {
      border-color: var(--primary-light);
      transform: rotate(225deg) translate(-1px, -1px);
    }
    .custom-select-native.field-error + .custom-select-trigger {
      border-color: var(--danger); background: var(--danger-bg);
      box-shadow: 0 0 0 3px color-mix(in srgb, var(--danger) 18%, transparent);
    }
    .custom-select-popover {
      position: fixed; z-index: 120; display: none;
      padding: 6px; overflow-y: auto; overscroll-behavior: contain;
      background: color-mix(in srgb, var(--surface) 96%, transparent);
      border: 1px solid var(--border-med); border-radius: 12px;
      box-shadow: 0 18px 46px rgba(0,0,0,.38), 0 0 0 1px rgba(255,255,255,.025);
      backdrop-filter: blur(18px); -webkit-backdrop-filter: blur(18px);
      animation: custom-select-in .14s ease-out;
    }
    .custom-select-popover.open { display: block; }
    .custom-select-popover[data-side="top"] { transform-origin: bottom center; }
    .custom-select-popover[data-side="bottom"] { transform-origin: top center; }
    @keyframes custom-select-in {
      from { opacity: 0; transform: translateY(4px) scale(.985); }
      to { opacity: 1; transform: translateY(0) scale(1); }
    }
    .custom-select-option {
      width: 100%; min-height: 38px; padding: 7px 10px;
      justify-content: flex-start; gap: 10px;
      background: transparent; border: 0; border-radius: var(--radius-xs);
      color: var(--text-2); box-shadow: none;
      font-size: 13px; font-weight: 500; text-align: left;
    }
    .custom-select-option:hover:not(:disabled), .custom-select-option:focus-visible {
      outline: none; background: var(--primary-bg); color: var(--text);
      transform: none; box-shadow: none;
    }
    .custom-select-option.selected {
      background: color-mix(in srgb, var(--primary) 18%, transparent);
      color: var(--primary-light); font-weight: 700;
    }
    .custom-select-option::after {
      content: '✓'; margin-left: auto; color: var(--primary-light);
      opacity: 0; transform: scale(.7); transition: opacity .14s ease, transform .14s ease;
    }
    .custom-select-option.selected::after { opacity: 1; transform: scale(1); }
    textarea { min-height: 130px; resize: none; line-height: 1.6; }
    input:focus, select:focus, textarea:focus {
      border-color: var(--primary); background-color: var(--primary-bg);
      box-shadow: 0 0 0 3px color-mix(in srgb, var(--primary) 18%, transparent);
    }
    input.field-error, select.field-error, textarea.field-error, .chips.field-error {
      border-color: var(--danger);
      background-color: var(--danger-bg);
      box-shadow: 0 0 0 3px color-mix(in srgb, var(--danger) 18%, transparent);
    }
    input[type="checkbox"] {
      width: 16px; height: 16px; min-height: 0; padding: 0; margin: 0 6px 0 0;
      vertical-align: -2px; accent-color: var(--primary); box-shadow: none;
    }
    .inline { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 10px; align-items: stretch; }
    .inline > button { min-height: 40px; }
    .field-row { display: grid; grid-template-columns: minmax(120px, .46fr) minmax(0, 1fr); gap: 8px; }
    .source-controls {
      display: grid;
      grid-template-columns: minmax(150px, .9fr) minmax(260px, 1.65fr) repeat(2, minmax(180px, 1fr));
      gap: 10px; align-items: stretch;
    }
    .source-controls > .custom-select,
    .source-controls > input,
    .source-controls > button {
      width: 100%; min-width: 0; min-height: 46px;
    }
    .source-controls > input { padding-left: 14px; padding-right: 14px; }
    .source-controls > button { border-radius: 12px; font-size: 13px; }
    .format-row { display: grid; grid-template-columns: minmax(150px, 1.2fr) minmax(110px, .9fr); gap: 8px; }
    .two-buttons { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
    .checks { display: flex; gap: 16px; align-items: center; flex-wrap: wrap; padding: 6px 0 2px; }
    .checks label { display: inline-flex; align-items: center; margin: 0; color: var(--text-2); font-size: 12px; font-weight: 600; text-transform: none; letter-spacing: 0; }

    /* ── Chips ─────────────────────────────────── */
    .chips {
      min-height: 40px; display: flex; gap: 6px; flex-wrap: wrap;
      align-items: center; align-content: center; background: var(--input-bg);
      padding: 6px 10px; border-radius: var(--radius-xs);
      border: 1px solid var(--border); font-size: 14px;
      transition: border-color .15s ease, box-shadow .15s ease, background .2s ease;
    }
    .chips.editable { cursor: text; }
    .chips.editable:focus-within {
      border-color: var(--primary); background: var(--primary-bg);
      box-shadow: 0 0 0 3px color-mix(in srgb, var(--primary) 18%, transparent);
    }
    .chip-input {
      flex: 1 1 110px; min-width: 90px; min-height: 24px; padding: 1px 0;
      color: var(--text); font-size: 14px; line-height: 1.35;
      font-weight: 500; outline: none; background: transparent; border: none;
    }
    .chip-input:empty::before { content: attr(data-placeholder); color: var(--text-3); font-weight: 400; }
    .chip-actions { display: flex; justify-content: flex-end; margin-top: 7px; }
    .chip-actions button { min-height: 30px; padding: 0 12px; font-size: 12px; }
    .chip {
      display: inline-flex; align-items: center; gap: 4px;
      height: 22px; min-height: 22px; padding: 0 7px 0 9px; color: #fff;
      font-size: 12px; font-weight: 600; line-height: 1;
      border-radius: 99px; max-width: 100%; overflow: hidden;
    }
    .chip span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .chip > button {
      width: 14px; height: 14px; min-height: 0; flex: 0 0 14px;
      padding: 0; background: transparent; border: none;
      color: var(--text-3); font-size: 14px; line-height: 1;
      box-shadow: none !important; transform: none !important;
    }
    .chip > button:hover { color: rgba(255,255,255,.95); background: transparent !important; box-shadow: none !important; }

    /* ── Series ─────────────────────────────────── */
    .series-inline { display: grid; grid-template-columns: 1fr auto; gap: 8px; align-items: stretch; }
    .series-box { min-height: 40px; align-items: center; align-content: center; }
    .series-empty { display: flex; align-items: center; gap: 6px; color: var(--text-3); font-size: 13px; font-weight: 500; }
    .series-inline button { min-height: 40px; padding: 0 14px; font-size: 13px; }
    .team-box { flex-wrap: nowrap; }
    .team-box .chip-input { flex-basis: 60px; min-width: 50px; }

    /* ── Cover ──────────────────────────────────── */
    .cover-row { display: grid; grid-template-columns: 158px 1fr; gap: 14px; align-items: start; }
    .cover-box {
      width: 100%;
      height: 158px; background: var(--surface-3); border: 1px solid var(--border);
      border-radius: var(--radius-sm); display: grid; place-items: center;
      color: var(--text-3); text-align: center; overflow: hidden; font-size: 12px;
      transition: background .25s ease, border-color .25s ease;
      aspect-ratio: 1 / 1;
    }
    .cover-box img { width: 100%; height: 100%; object-fit: cover; display: none; }
    .cover-meta { margin-top: 5px; color: var(--text-3); font-size: 11px; text-align: center; }
    .search-results {
      position: fixed;
      z-index: 1200;
      inset: 50% auto auto 50%;
      transform: translate(-50%, -50%);
      display: grid;
      gap: 10px;
      width: min(840px, calc(100vw - 32px));
      max-height: min(760px, calc(100vh - 56px));
      overflow: auto;
      padding: 0 18px 18px;
      background: color-mix(in srgb, var(--surface) 96%, transparent);
      border: 1px solid var(--border-med);
      border-radius: 20px;
      box-shadow: 0 32px 80px rgba(0,0,0,.36), inset 0 1px 0 color-mix(in srgb, var(--surface) 88%, transparent);
      backdrop-filter: blur(22px);
    }
    .search-dialog-head {
      position: sticky;
      top: 0;
      z-index: 1;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin: 0 -18px 10px;
      padding: 18px 22px 14px;
      background: linear-gradient(180deg, color-mix(in srgb, var(--surface) 96%, transparent), color-mix(in srgb, var(--surface-2) 90%, transparent));
      border-bottom: 1px solid var(--border);
      backdrop-filter: blur(16px);
    }
    .search-dialog-head strong { color: var(--text); font-size: 17px; }
    .search-count {
      color: var(--primary-light);
      background: var(--primary-bg);
      border: 1px solid color-mix(in srgb, var(--primary) 24%, transparent);
      padding: 3px 9px;
      border-radius: 99px;
      font-size: 11px;
      font-weight: 700;
      white-space: nowrap;
    }
    .search-dialog-close {
      width: 32px;
      height: 32px;
      min-height: 32px;
      border: 0;
      border-radius: 50%;
      color: var(--text-2);
      background: var(--surface-2);
      cursor: pointer;
      transition: background-color .15s ease, color .15s ease, box-shadow .15s ease;
    }
    .search-dialog-close:hover {
      color: var(--text);
      background: var(--primary-bg);
      box-shadow: 0 0 0 3px color-mix(in srgb, var(--primary) 18%, transparent);
    }
    .search-dialog-close .ui-icon { width: 16px; height: 16px; }
    .search-results[hidden] { display: none; }
    .search-results-backdrop {
      position: fixed;
      z-index: 1199;
      inset: 0;
      background: rgba(0,0,0,.58);
      backdrop-filter: blur(6px);
    }
    .search-result {
      display: grid;
      grid-template-columns: 72px minmax(0, 1fr) auto;
      gap: 14px;
      align-items: center;
      width: 100%;
      padding: 14px;
      text-align: left;
      color: var(--text);
      background: linear-gradient(135deg, var(--surface-2), var(--surface));
      border: 1px solid var(--border);
      border-radius: 14px;
      cursor: pointer;
      box-shadow: inset 0 1px 0 color-mix(in srgb, var(--surface) 88%, transparent);
      transition: border-color .18s, transform .18s, background .18s, box-shadow .18s;
    }
    .search-result:hover {
      border-color: var(--primary);
      background: linear-gradient(135deg, var(--primary-bg), var(--surface-2));
      transform: translateY(-1px);
      box-shadow: 0 10px 24px color-mix(in srgb, var(--primary) 16%, transparent);
    }
    .search-result img {
      width: 72px;
      height: 72px;
      object-fit: cover;
      border-radius: 12px;
      background: var(--surface-3);
      border: 1px solid var(--border);
      box-shadow: 0 6px 16px rgba(0,0,0,.14);
    }
    .search-result-title {
      display: block;
      overflow: hidden;
      font-weight: 700;
      line-height: 1.45;
      white-space: normal;
      overflow-wrap: anywhere;
    }
    .search-result-meta {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 4px 10px;
      margin-top: 4px;
      color: var(--text-3);
      font-size: 12px;
    }
    .search-result-desc {
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
      margin-top: 7px;
      color: var(--text-2);
      font-size: 12px;
      line-height: 1.55;
    }
    .search-result-tags {
      display: flex;
      flex-wrap: wrap;
      gap: 5px;
      margin-top: 8px;
    }
    .search-result-tag {
      padding: 2px 7px;
      border-radius: 99px;
      background: var(--surface-3);
      color: var(--text-2);
      border: 1px solid var(--border);
      font-size: 10px;
      font-weight: 600;
    }
    .search-result-action {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      padding: 7px 12px;
      border-radius: 99px;
      background: var(--primary-bg);
      color: var(--primary-light);
      border: 1px solid color-mix(in srgb, var(--primary) 26%, transparent);
      font-size: 12px;
      font-weight: 700;
      white-space: nowrap;
    }
    .search-result-action .ui-icon { width: 14px; height: 14px; }
    .author-search-result { grid-template-columns: 52px minmax(0, 1fr) auto; }
    .author-result-avatar {
      width: 52px;
      height: 52px;
      display: grid;
      place-items: center;
      border-radius: 16px;
      color: #fff;
      font-size: 20px;
      font-weight: 800;
      box-shadow: 0 8px 20px rgba(0,0,0,.16);
    }
    .search-empty {
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 9px;
      padding: 44px 20px;
      color: var(--text-3);
      font-size: 13px;
      font-weight: 600;
    }
    .search-empty .ui-icon { width: 22px; height: 22px; opacity: .75; }
    .search-pagination {
      position: sticky;
      bottom: -18px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
      margin: 2px -18px -18px;
      padding: 12px 18px;
      background: color-mix(in srgb, var(--surface) 92%, transparent);
      border-top: 1px solid var(--border);
      backdrop-filter: blur(14px);
    }
    .search-pagination button {
      min-height: 34px;
      padding: 0 12px;
      border-radius: 9px;
      font-size: 12px;
    }
    .source-controls .btn-primary { box-shadow: 0 8px 20px color-mix(in srgb, var(--primary) 20%, transparent); }
    .source-controls .quiet-button { background: color-mix(in srgb, var(--surface-2) 82%, var(--primary) 18%); border-color: color-mix(in srgb, var(--primary) 20%, var(--border)); }
    .source-controls .quiet-button:hover { background: var(--primary-bg); border-color: color-mix(in srgb, var(--primary) 48%, var(--border)); }
    .cover-actions { display:flex; gap:8px; flex:0 0 auto; }
    .cover-actions button { min-width:112px; min-height:44px; }
    @media (max-width: 720px) {
      .cover-actions { width:100%; }
      .cover-actions button { flex:1; }
    }

    /* ── Toolbox ──────────────────────────────── */
    .toolbox {
      display: flex; flex-wrap: wrap; gap: 6px; align-items: center;
      padding-top: 10px; border-top: 1px solid var(--border); margin-top: 8px;
    }
    .toolbox strong {
      font-size: 11px; font-weight: 700; color: var(--text-3);
      text-transform: uppercase; letter-spacing: .06em; white-space: nowrap;
    }
    .toolbox button {
      flex: 1 1 96px; min-width: 0; padding: 0 8px;
      min-height: 30px; font-size: 12px; font-weight: 600;
    }
    .toolbox-sep {
      width: 1px; height: 20px; background: var(--border-med);
      flex-shrink: 0; margin: 0 2px;
    }

    /* ── Right Panel ─────────────────────────── */
    .tabs {
      display: flex; height: 48px;
      background: var(--surface); border-bottom: 1px solid var(--border);
      padding: 8px 12px 0; gap: 2px; flex-shrink: 0;
      transition: background .25s ease, border-color .25s ease;
    }
    .tab {
      border: 0; padding: 0 16px; min-height: 40px;
      background: transparent; color: var(--text-3);
      font-size: 13px; font-weight: 600; box-shadow: none;
      border-radius: var(--radius-xs) var(--radius-xs) 0 0;
      border-bottom: 2px solid transparent;
      transition: color .15s ease, border-color .15s ease, background .15s ease;
    }
    .tab:hover:not(:disabled) {
      transform: none; box-shadow: none; color: var(--text-2); background: var(--glass);
    }
    .tab.active { background: var(--primary-bg); color: var(--primary-light); border-bottom-color: var(--primary); }
    .tab-panel { flex: 1; min-height: 0; display: none; flex-direction: column; padding: 14px; }
    .tab-panel.active { display: flex; }

    /* ── Queue ──────────────────────────────── */
    .queue-actions { display: flex; gap: 8px; margin-bottom: 10px; flex-wrap: wrap; }
    .queue-actions button { min-height: 34px; font-size: 12px; }
    .table-wrap {
      flex: 1; min-height: 0; overflow: auto;
      border: 1px solid var(--border); background: var(--surface);
      border-radius: var(--radius-sm);
      transition: background .25s ease, border-color .25s ease;
    }
    table { width: 100%; min-width: 820px; border-collapse: separate; border-spacing: 0; }
    th {
      padding: 10px 12px; border-bottom: 1px solid var(--border);
      text-align: left; white-space: nowrap;
      color: var(--text-3); font-size: 11px; font-weight: 700;
      text-transform: uppercase; letter-spacing: .06em;
      background: var(--surface-2); position: sticky; top: 0; z-index: 1;
      transition: background .25s ease, border-color .25s ease;
    }
    td {
      padding: 10px 12px; border-bottom: 1px solid var(--border);
      text-align: left; white-space: nowrap; font-size: 13px; color: var(--text-2);
      transition: background .15s ease, color .15s ease;
    }
    tbody tr:nth-child(even) td { background: var(--glass); }
    tbody tr:hover td { background: var(--primary-bg) !important; color: var(--text); }

    /* ── Log ──────────────────────────────────── */
    .log {
      flex: 1; min-height: 0; overflow: auto; padding: 14px 16px;
      background: var(--log-bg); color: var(--log-text);
      font: 12.5px/1.7 "JetBrains Mono", "Fira Code", Consolas, monospace;
      white-space: pre-wrap; border-radius: var(--radius-sm);
      border: 1px solid var(--border);
      transition: background .25s ease, color .25s ease, border-color .25s ease;
    }
    html[data-theme="dark"] .log-line.error   { color: #fca5a5; }
    html[data-theme="dark"] .log-line.warning { color: #fde68a; }
    html[data-theme="dark"] .log-line.info    { color: #93c5fd; }
    html[data-theme="light"] .log-line.error   { color: #b91c1c; }
    html[data-theme="light"] .log-line.warning { color: #92400e; }
    html[data-theme="light"] .log-line.info    { color: #1d4ed8; }
    .log-line.log-truncated {
      margin-bottom: 8px; padding: 7px 10px;
      color: var(--text-3); background: var(--surface-2);
      border: 1px solid var(--border); border-radius: var(--radius-xs);
      font-family: "Inter", "PingFang SC", sans-serif;
    }

    /* ── Overview ──────────────────────────────── */
    .overview {
      display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px; align-content: start;
    }
    .metric {
      position: relative; overflow: hidden;
      background: var(--surface);
      border: 1px solid var(--border); padding: 16px; border-radius: var(--radius);
      min-height: 128px; transition: border-color .2s ease, transform .2s ease, background .25s ease;
    }
    .metric:hover { border-color: var(--border-med); transform: translateY(-1px); }
    .metric::before {
      content: ''; position: absolute; top: 0; left: 0; right: 0;
      height: 2px; background: var(--metric-accent, var(--primary)); opacity: .8;
    }
    .metric-head {
      display: flex; align-items: center; justify-content: space-between;
      color: var(--text-3); font-size: 11px; font-weight: 700;
      text-transform: uppercase; letter-spacing: .06em;
    }
    .metric-icon {
      width: 30px; height: 30px; display: grid; place-items: center;
      border-radius: 8px; font-size: 14px;
      background: var(--metric-bg, var(--primary-bg));
      color: var(--metric-color, var(--primary-light));
    }
    .metric b {
      display: block; margin-top: 14px;
      font-size: clamp(24px, 2.8vw, 36px); line-height: 1.1;
      color: var(--text); letter-spacing: -.02em;
    }
    .metric small { display: block; margin-top: 6px; color: var(--text-3); font-size: 11px; font-weight: 600; }
    .metric.primary { --metric-accent: var(--primary); --metric-bg: var(--primary-bg); --metric-color: var(--primary-light); }
    .metric.success { --metric-accent: var(--success); --metric-bg: var(--success-bg); --metric-color: var(--success); }
    .metric.danger  { --metric-accent: var(--danger);  --metric-bg: var(--danger-bg);  --metric-color: var(--danger); }
    .metric.indigo  { --metric-accent: #7c3aed; --metric-bg: rgba(124,58,237,.12); --metric-color: #a78bfa; }
    .metric.amber   { --metric-accent: var(--warning);  --metric-bg: var(--warning-bg); --metric-color: var(--warning); }
    .metric.slate   { --metric-accent: #64748b; --metric-bg: rgba(100,116,139,.1); --metric-color: #94a3b8; }
    .metric-wide { grid-column: span 3; min-height: 108px; }
    .overview-progress {
      height: 6px; margin-top: 14px; overflow: hidden;
      border-radius: 99px; background: var(--border);
    }
    .overview-progress span {
      display: block; height: 100%; width: 0%; border-radius: inherit;
      background: linear-gradient(90deg, var(--primary), var(--cyan), var(--success));
      transition: width .4s ease;
    }
    .overview-meta {
      display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px;
      color: var(--text-3); font-size: 11px; font-weight: 600;
    }
    .overview-meta span {
      padding: 3px 8px; border-radius: 99px;
      background: var(--glass); border: 1px solid var(--border);
    }

    /* ── Status Badges ────────────────────────── */
    .status-badge {
      display: inline-flex; align-items: center;
      min-height: 20px; padding: 0 8px;
      border-radius: 99px; font-size: 11px; font-weight: 700; letter-spacing: .03em;
    }
    .status-badge::before { content: '●'; margin-right: 5px; font-size: 7px; }
    .status-badge.pending    { background: var(--glass); color: var(--text-2); border: 1px solid var(--border); }
    .status-badge.processing { background: var(--primary-bg); color: var(--primary-light); border: 1px solid color-mix(in srgb, var(--primary) 30%, transparent); animation: pulse-border 1.8s ease-in-out infinite; }
    .status-badge.processing::before { color: var(--primary-light); }
    .status-badge.done       { background: var(--success-bg); color: var(--success); border: 1px solid color-mix(in srgb, var(--success) 30%, transparent); }
    .status-badge.done::before { color: var(--success); }
    .status-badge.failed     { background: var(--danger-bg); color: var(--danger); border: 1px solid color-mix(in srgb, var(--danger) 30%, transparent); }
    .status-badge.failed::before { color: var(--danger); }
    .status-badge.stopped    { background: var(--warning-bg); color: var(--warning); border: 1px solid color-mix(in srgb, var(--warning) 30%, transparent); }
    .status-badge.stopped::before { color: var(--warning); }
    @keyframes pulse-border { 0%,100% { opacity:1; } 50% { opacity:.6; } }

    /* ── Empty State ──────────────────────────── */
    .empty-state {
      height: 100%; min-height: 260px; display: grid;
      place-items: center; text-align: center;
    }
    .empty-state strong { display: block; color: var(--text-2); margin-bottom: 6px; font-size: 14px; }
    .empty-state span { color: var(--text-3); font-size: 13px; }

    /* ── Modals ──────────────────────────────── */
    .modal-mask {
      position: fixed; inset: 0; display: none;
      align-items: center; justify-content: center;
      background: var(--modal-mask); backdrop-filter: blur(8px);
      z-index: 20; padding: 20px;
      transition: background .25s ease;
    }
    .modal-mask.show { display: flex; }
    .modal {
      width: min(720px, 100%); max-height: 85vh;
      display: flex; flex-direction: column;
      background: var(--surface); border: 1px solid var(--border-med);
      box-shadow: var(--shadow-lg); border-radius: var(--radius); overflow: hidden;
      transition: background .25s ease, border-color .25s ease;
    }
    .modal.compact { width: min(440px, 100%); }
    .modal-head {
      padding: 16px 18px; display: flex; justify-content: space-between;
      align-items: center; border-bottom: 1px solid var(--border);
      background: var(--surface-2); flex-shrink: 0;
      transition: background .25s ease, border-color .25s ease;
    }
    .modal-head strong { font-size: 15px; font-weight: 700; }
    .modal-head button { min-height: 30px; font-size: 12px; }
    .modal-foot {
      padding: 14px 18px; display: flex; justify-content: space-between;
      align-items: center; border-top: 1px solid var(--border); gap: 10px; flex-shrink: 0;
      transition: border-color .25s ease;
    }
    .modal-foot button { min-height: 34px; font-size: 13px; }
    .modal-body { padding: 16px 18px; overflow: auto; flex: 1; }
    .hint { color: var(--text-3); font-size: 12px; font-weight: 600; }
    .dir-list { padding: 8px; overflow: auto; max-height: 400px; }
    .dir-item {
      display: grid; grid-template-columns: 1fr auto; gap: 8px;
      padding: 10px 12px; border: 1px solid var(--border); margin-bottom: 6px;
      background: var(--surface-3); cursor: pointer; border-radius: var(--radius-xs);
      transition: border-color .14s ease, background .14s ease, color .14s ease; color: var(--text-2);
      -webkit-user-select: none; user-select: none;
    }
    .dir-item:hover { border-color: var(--border-med); background: var(--surface-2); color: var(--text); }
    .dir-item.selected { border-color: var(--primary); background: var(--primary-bg); color: var(--text); }
    .dir-item strong { font-size: 13px; font-weight: 600; }
    .dir-item span { font-size: 11px; color: var(--text-3); }

    /* ── Toast ────────────────────────────────── */
    .toast {
      position: fixed; right: 20px; bottom: 20px; padding: 10px 16px 10px 14px;
      background: var(--surface-3); border: 1px solid var(--border-med);
      border-left: 3px solid var(--primary);
      color: var(--text); font-size: 13px; font-weight: 600;
      display: none; z-index: 30; border-radius: var(--radius-xs);
      box-shadow: var(--shadow-lg); max-width: 320px;
      transition: background .25s ease, color .25s ease, border-color .25s ease;
    }
    .toast.show { display: block; animation: slideIn .18s ease; }
    @keyframes slideIn { from { opacity:0; transform:translateY(8px); } to { opacity:1; transform:translateY(0); } }

    /* ── Responsive ───────────────────────────── */
    @media (max-width: 1320px) {
      .app { grid-template-columns: minmax(560px, 47vw) 1fr; }
      .ha-process { flex: 3; }
      .ha-config  { flex: 2; }
      .toolbox button { flex-basis: 90px; }
      .grid-3 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .grid-3 .span-3 { grid-column: span 2; }
    }
    @media (max-width: 1500px) and (min-width: 901px) {
      .source-controls { grid-template-columns: minmax(160px, .78fr) minmax(0, 1.22fr); }
      .source-controls > button { min-height: 42px; }
      .source-controls > button:nth-of-type(1) { grid-column: 1; }
      .source-controls > button:nth-of-type(2) { grid-column: 2; }
    }
    @media (min-width: 1181px) and (max-height: 880px) {
      h1 { font-size: 19px; }
      .percent { font-size: 22px; }
      .progress-track { margin-bottom: 10px; }
      .app-header { margin-bottom: 10px; }
      .hero-actions { margin-bottom: 10px; gap: 7px; }
      .ha-process button, .ha-config button { min-height: 36px; font-size: 12px; padding: 0 10px; }
      .section { padding: 11px; margin-bottom: 7px; }
      .section-title { margin-bottom: 9px; font-size: 12.5px; }
      input, select, .custom-select-trigger { min-height: 36px; font-size: 13px; padding: 7px 10px; }
      textarea { min-height: 106px; }
      .chips { min-height: 36px; font-size: 13px; }
      .chip-input { font-size: 13px; }
      .series-inline button, .series-box, .team-box { min-height: 36px; }
      .cover-row { grid-template-columns: 138px 1fr; gap: 11px; }
      .cover-box { height: 138px; }
      .toolbox { padding-top: 8px; margin-top: 6px; gap: 5px; }
      .toolbox button { min-height: 29px; font-size: 11.5px; }
    }
    @media (min-width: 1181px) and (max-height: 740px) {
      textarea { min-height: 86px; }
      .cover-row { grid-template-columns: 118px 1fr; }
      .cover-box { height: 118px; }
      .section { padding: 9px; margin-bottom: 6px; }
    }
    @media (max-width: 1180px) {
      body { overflow: auto; }
      .app { height: auto; min-height: 100vh; grid-template-columns: 1fr; }
      .left { border-right: 0; border-bottom: 1px solid var(--border); overflow: visible; }
      .form-scroll { overflow: visible; padding-right: 0; }
      .right { min-height: 680px; }
      .tabs { overflow-x: auto; overflow-y: hidden; }
      .tab { flex: 0 0 auto; }
    }
    @media (max-width: 900px) {
      .hero-actions { flex-wrap: wrap; }
      .ha-process, .ha-config { flex: 1 1 100%; }
      .ha-config { border-left: none; padding-left: 0; border-top: 1px solid var(--border); padding-top: 8px; }
      .grid, .grid-3 { grid-template-columns: 1fr; }
      .source-controls { grid-template-columns: minmax(140px, .85fr) minmax(0, 1.55fr); }
      .source-controls > button { min-height: 42px; }
      .source-controls > button:nth-of-type(1) { grid-column: 1; }
      .source-controls > button:nth-of-type(2) { grid-column: 2; }
      .span-2, .span-3, .grid-3 .span-3 { grid-column: span 1; }
      .cover-row { grid-template-columns: 148px 1fr; }
      .overview { grid-template-columns: repeat(2, 1fr); }
      .metric-wide { grid-column: span 2; }
      .queue-actions { flex-wrap: wrap; }
    }
    @media (max-width: 640px) {
      body { font-size: 13px; padding-bottom: calc(214px + env(safe-area-inset-bottom)); }
      .left { padding: 14px 12px 12px; }
      .app-header { grid-template-columns: 1fr; }
      .header-right { flex-direction: row; align-items: center; }
      .status-card { flex-direction: row; align-items: center; justify-content: space-between; }
      .hero-actions {
        position: fixed; left: 0; right: 0; bottom: 0; z-index: 40;
        display: grid; gap: 7px; padding: 9px 10px calc(9px + env(safe-area-inset-bottom));
        margin: 0; background: color-mix(in srgb, var(--surface) 94%, transparent);
        border-top: 1px solid var(--border); box-shadow: 0 -14px 36px rgba(0,0,0,.28);
        backdrop-filter: blur(14px);
      }
      .ha-process, .ha-config { flex: none; display: grid; gap: 7px; }
      .ha-process { grid-template-columns: repeat(3, minmax(0, 1fr)); }
      .ha-config { grid-template-columns: repeat(2, minmax(0, 1fr)); border: 0; padding: 0; }
      .ha-process button, .ha-config button { min-height: 38px; font-size: 12px; }
      .source-controls { grid-template-columns: 1fr; gap: 7px; }
      .source-controls > button:nth-of-type(1), .source-controls > button:nth-of-type(2) { grid-column: auto; }
      .source-controls > input, .source-controls > button, .source-controls > .custom-select { min-height: 40px; }
      .tabs {
        position: fixed; left: 0; right: 0; bottom: calc(92px + env(safe-area-inset-bottom)); z-index: 39;
        height: auto; padding: 7px 8px; gap: 5px; overflow-x: auto; border-top: 1px solid var(--border);
        border-bottom: 1px solid var(--border); box-shadow: 0 8px 22px rgba(0,0,0,.16);
      }
      .tab {
        min-height: 36px; padding: 0 10px; border-radius: var(--radius-xs);
        border: 1px solid transparent; border-bottom-width: 1px; font-size: 12px;
      }
      .tab.active { border-color: color-mix(in srgb, var(--primary) 42%, transparent); }
      .tab-panel { padding: 10px; }
      #panel-log { min-height: 55vh; }
      .log { min-height: 55vh; max-height: 62vh; }
      .field-row, .format-row, .series-inline, .inline { grid-template-columns: 1fr; }
      .action-stack { grid-template-columns: 1fr 1fr; }
      .inline button { width: 100%; }
      input, select, textarea, .custom-select-trigger { min-height: 42px; font-size: 15px; }
      .cover-row { grid-template-columns: 1fr; }
      .cover-box { width: min(100%, 220px); height: auto; aspect-ratio: 1; margin: 0 auto; }
      .cover-meta { margin-bottom: 6px; }
      .chips { min-height: 42px; gap: 7px; padding: 7px 10px; }
      .chip { height: 28px; min-height: 28px; padding: 0 9px 0 11px; font-size: 13px; }
      .chip > button { width: 18px; height: 18px; flex-basis: 18px; font-size: 16px; }
      .chip-input { min-height: 28px; font-size: 15px; }
      .section.mobile-collapsible .section-title { cursor: pointer; margin-bottom: 0; }
      .section.mobile-collapsible:not(.mobile-expanded) > :not(.section-title) { display: none; }
      .section.mobile-collapsible.mobile-expanded .section-title { margin-bottom: 12px; }
      .section-toggle { display: inline-flex; }
      .section.mobile-expanded .section-toggle { color: var(--primary-light); background: var(--primary-bg); }
      .overview { grid-template-columns: 1fr; }
      .metric-wide { grid-column: span 1; }
      .modal { width: calc(100vw - 20px); max-height: calc(100vh - 24px); }
      .modal-head, .modal-foot { flex-direction: column; align-items: stretch; }
      .modal-head button, .modal-foot button { width: 100%; }
      .toast {
        left: 10px; right: 10px; bottom: calc(154px + env(safe-area-inset-bottom));
        max-width: none;
      }
      .queue-actions { display: grid; grid-template-columns: 1fr 1fr; gap: 7px; }
      .queue-actions button { width: 100%; min-height: 36px; }
      .queue-actions button:last-child { grid-column: span 2; }
      .table-wrap { border: 0; background: transparent; overflow: visible; }
      table, tbody, tr, td { display: block; width: 100%; min-width: 0; }
      table { border-spacing: 0; }
      thead { display: none; }
      tbody tr {
        position: relative; margin-bottom: 9px; padding: 11px 12px 11px 42px;
        border: 1px solid var(--border); border-radius: var(--radius-sm);
        background: var(--surface); box-shadow: 0 8px 20px rgba(0,0,0,.12);
      }
      tbody tr:hover td, tbody tr:nth-child(even) td { background: transparent !important; }
      tbody td {
        padding: 2px 0; border: 0; background: transparent !important;
        white-space: normal; font-size: 12.5px; color: var(--text-2);
      }
      tbody td:nth-child(1) {
        position: absolute; left: 12px; top: 12px; width: 22px;
      }
      tbody td:nth-child(2) { display: none; }
      tbody td:nth-child(3) {
        font-size: 14px; font-weight: 800; color: var(--text); padding-right: 80px;
      }
      tbody td:nth-child(4)::before { content: '作者：'; color: var(--text-3); font-weight: 700; }
      tbody td:nth-child(5)::before { content: '演播：'; color: var(--text-3); font-weight: 700; }
      tbody td:nth-child(6) {
        margin-top: 4px; color: var(--text-3); word-break: break-all;
      }
      tbody td:nth-child(6)::before { content: '目录：'; font-weight: 700; }
      tbody td:nth-child(7) {
        position: absolute; top: 10px; right: 10px; width: auto;
      }
      tbody td[colspan] {
        position: static; padding: 0; width: 100%;
      }
    }

    /* ══════════════════════════════════════════════
       2026 WORKSPACE REDESIGN
       ══════════════════════════════════════════════ */
    .app {
      height: 100vh;
      grid-template-columns: minmax(620px, 54%) minmax(0, 46%);
      grid-template-rows: 72px minmax(0, 1fr);
      background: var(--bg);
    }
    .global-topbar {
      grid-column: 1 / -1; grid-row: 1;
      display: flex; align-items: center; justify-content: space-between; gap: 24px;
      padding: 0 18px 0 20px;
      background: color-mix(in srgb, var(--surface) 94%, transparent);
      border-bottom: 1px solid var(--border-med);
      box-shadow: 0 10px 34px rgba(0,0,0,.18);
      backdrop-filter: blur(18px); -webkit-backdrop-filter: blur(18px);
      z-index: 5;
    }
    .topbar-brand, .brand-line, .topbar-controls, .topbar-config-actions,
    .status-card, .status-copy { display: flex; align-items: center; }
    .topbar-brand { min-width: 0; gap: 12px; }
    .brand-mark {
      position: relative; width: 38px; height: 38px; flex: 0 0 38px;
      display: grid; place-items: center; border-radius: 13px;
      background: linear-gradient(145deg, #818cf8, #4f46e5 55%, #7c3aed);
      box-shadow: 0 8px 24px var(--primary-glow);
      transform: rotate(45deg);
    }
    .brand-mark::before, .brand-mark::after, .brand-mark span {
      content: ''; position: absolute; border: 2px solid rgba(255,255,255,.9);
      border-radius: 50%; transform: rotate(-45deg);
    }
    .brand-mark::before { width: 23px; height: 15px; border-left-color: transparent; }
    .brand-mark::after { width: 15px; height: 23px; border-top-color: transparent; opacity: .72; }
    .brand-mark span { width: 7px; height: 7px; background: #fff; border: 0; }
    .app-title { min-width: 0; gap: 1px; }
    .brand-line { min-width: 0; gap: 12px; }
    .brand-line h1 { flex: 0 0 auto; font-size: 20px; }
    .brand-divider { width: 1px; height: 20px; flex: 0 0 1px; background: var(--border-med); }
    .brand-line .app-subtitle {
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
      font-size: 11px; text-transform: none; letter-spacing: .035em;
    }
    .brand-caption {
      color: var(--text-3); font-size: 9px; font-weight: 700;
      letter-spacing: .13em; text-transform: uppercase;
    }
    .topbar-controls { flex: 0 0 auto; gap: 9px; }
    .topbar-config-actions { gap: 6px; padding-right: 9px; border-right: 1px solid var(--border); }
    .topbar-config-actions button { min-height: 32px; padding: 0 10px; font-size: 11.5px; }
    .topbar-icon-button {
      width: 34px; height: 34px; min-height: 34px; padding: 0;
      border-radius: 50%; background: var(--surface-3); color: var(--text-2);
    }
    .topbar-icon-button:hover:not(:disabled) {
      background: var(--primary-bg); border-color: var(--primary); color: var(--primary-light);
    }
    .global-topbar .status-card {
      min-width: 210px; flex-direction: row; justify-content: flex-end; gap: 12px;
      padding-left: 12px; border-left: 1px solid var(--border);
    }
    .status-copy { min-width: 128px; flex-direction: column; align-items: stretch; gap: 6px; }
    .global-topbar .state-row { justify-content: flex-end; }
    .header-progress {
      width: 100%; height: 4px; overflow: hidden;
      border-radius: 99px; background: var(--border);
    }
    .global-topbar .percent { min-width: 48px; font-size: 23px; text-align: right; }

    .left {
      grid-column: 1; grid-row: 2;
      padding: 14px 10px 14px 16px;
      background: var(--bg); border-right: 0; overflow: hidden;
    }
    .workspace-heading {
      min-height: 42px; display: flex; align-items: center; justify-content: space-between; gap: 12px;
      padding: 0 5px 10px 2px; flex: 0 0 auto;
    }
    .workspace-heading > div { min-width: 0; display: flex; align-items: baseline; gap: 9px; }
    .workspace-heading strong { color: var(--text); font-size: 13px; }
    .workspace-heading span { color: var(--text-3); font-size: 10.5px; font-weight: 600; }
    .workspace-clear {
      min-height: 28px; padding: 0 10px; border-color: transparent;
      background: transparent; color: var(--text-3); font-size: 11px; box-shadow: none;
    }
    .workspace-clear:hover:not(:disabled) {
      transform: none; box-shadow: none; color: var(--danger);
      border-color: color-mix(in srgb, var(--danger) 25%, transparent); background: var(--danger-bg);
    }
    .form-scroll { padding: 0 5px 18px 0; }
    .section {
      margin-bottom: 10px; padding: 15px;
      background: linear-gradient(145deg, color-mix(in srgb, var(--surface-2) 95%, transparent), var(--surface));
      border-color: var(--border-med); border-radius: 13px;
      box-shadow: 0 8px 30px rgba(0,0,0,.13), inset 0 1px 0 rgba(255,255,255,.025);
    }
    .section:hover { border-color: color-mix(in srgb, var(--primary) 28%, var(--border-med)); box-shadow: 0 10px 34px rgba(0,0,0,.16), inset 0 1px 0 rgba(255,255,255,.035); }
    .section-title { margin-bottom: 13px; font-size: 13.5px; }
    .section-icon { width: 25px; height: 25px; border-radius: 8px; }
    label { margin-bottom: 5px; font-size: 10.5px; text-transform: none; letter-spacing: .025em; }
    input, select, textarea, .custom-select-trigger, .chips { border-color: var(--border-med); }
    input, select, .custom-select-trigger { min-height: 42px; }
    .chips { min-height: 42px; }
    .grid { gap: 11px; }
    .grid-3 { grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 11px; }
    .source-controls { gap: 8px; grid-template-columns: minmax(132px, .82fr) minmax(210px, 1.45fr) repeat(2, minmax(135px, 1fr)); }
    .source-controls > .custom-select, .source-controls > input, .source-controls > button { min-height: 42px; }
    .source-controls > button { font-size: 12px; border-radius: 9px; }
    .field-label-row { min-height: 26px; }
    .field-mini-action { min-height: 24px; font-size: 10.5px; }
    .series-inline button { min-height: 42px; }
    .cover-row { grid-template-columns: 150px minmax(0, 1fr); }
    .cover-box { height: 150px; }

    .right {
      grid-column: 2; grid-row: 2;
      min-height: 0; padding: 14px 16px 14px 8px; gap: 10px;
      background: var(--bg); overflow: hidden;
    }
    .tabs {
      height: 48px; padding: 7px 8px 0; flex: 0 0 48px;
      border: 1px solid var(--border-med); border-radius: 13px 13px 0 0;
      background: var(--surface); box-shadow: 0 8px 28px rgba(0,0,0,.12);
    }
    .tab { min-height: 40px; padding: 0 13px; font-size: 12px; }
    .tab.active { background: transparent; }
    .tab-panel {
      padding: 12px; border: 1px solid var(--border-med); border-top: 0;
      border-radius: 0 0 13px 13px; background: var(--surface);
      box-shadow: 0 12px 34px rgba(0,0,0,.15);
    }
    .queue-actions { margin-bottom: 9px; }
    .queue-actions button { min-height: 31px; }
    .table-wrap { border-color: var(--border-med); }
    th { padding: 9px 10px; font-size: 10px; }
    td { padding: 9px 10px; font-size: 12px; }
    .log { border-color: var(--border-med); border-radius: 10px; }
    .right-commandbar {
      min-height: 56px; flex: 0 0 56px;
      display: flex; align-items: center; justify-content: space-between; gap: 12px;
      padding: 8px 10px 8px 13px;
      border: 1px solid var(--border-med); border-radius: 12px;
      background: var(--surface); box-shadow: 0 10px 28px rgba(0,0,0,.14);
    }
    .selection-copy { display: flex; align-items: center; gap: 7px; color: var(--text-3); font-size: 11px; font-weight: 700; }
    .selection-copy .state-dot { background: var(--primary-light); }
    .right-commandbar .ha-process { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 7px; flex: 1; min-width: 0; }
    .right-commandbar .ha-process button { min-height: 38px; padding: 0 12px; font-size: 12px; border-radius: 9px; }

    .settings-mask { z-index: 1300; }
    #cookieModal, #blacklistModal { z-index: 1320; }
    .settings-modal { width: min(940px, 100%); max-height: min(820px, calc(100vh - 40px)); }
    .settings-head > div { display: flex; flex-direction: column; gap: 2px; }
    .settings-head > div > span { color: var(--text-3); font-size: 11px; font-weight: 600; }
    .settings-body { padding: 18px; background: var(--bg); }
    .settings-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
    .settings-card {
      padding: 14px; border: 1px solid var(--border-med); border-radius: 12px;
      background: var(--surface); box-shadow: 0 8px 24px rgba(0,0,0,.12);
    }
    .settings-card-head { display: flex; align-items: center; gap: 10px; margin-bottom: 13px; }
    .settings-card-head > div { min-width: 0; display: flex; flex-direction: column; gap: 1px; }
    .settings-card-head strong { color: var(--text); font-size: 13px; }
    .settings-card-head span:not(.settings-icon) { color: var(--text-3); font-size: 10.5px; font-weight: 600; }
    .settings-icon {
      width: 30px; height: 30px; flex: 0 0 30px; display: grid; place-items: center;
      border-radius: 9px; color: var(--primary-light); background: var(--primary-bg);
    }
    .settings-actions { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 7px; }
    .settings-actions button {
      min-height: 36px; min-width: 0; padding: 0 10px;
      justify-content: flex-start; background: var(--surface-2); color: var(--text-2);
      border-color: var(--border); font-size: 11.5px; box-shadow: none;
    }
    .settings-actions button:hover:not(:disabled) { transform: none; box-shadow: none; color: var(--text); background: var(--primary-bg); border-color: color-mix(in srgb, var(--primary) 35%, var(--border)); }
    .settings-danger {
      display: flex; align-items: center; justify-content: space-between; gap: 16px;
      margin-top: 12px; padding: 13px 14px;
      border: 1px solid color-mix(in srgb, var(--danger) 28%, var(--border));
      border-radius: 12px; background: var(--danger-bg);
    }
    .settings-danger > div { display: flex; flex-direction: column; gap: 2px; }
    .settings-danger strong { color: var(--text); font-size: 12.5px; }
    .settings-danger span { color: var(--text-3); font-size: 10.5px; }
    .settings-danger button { min-height: 34px; }
    .settings-foot { background: var(--surface); }

    @media (max-width: 1500px) and (min-width: 901px) {
      .source-controls { grid-template-columns: minmax(140px, .78fr) minmax(0, 1.22fr); }
      .source-controls > button:nth-of-type(1) { grid-column: 1; }
      .source-controls > button:nth-of-type(2) { grid-column: 2; }
    }
    @media (max-width: 1180px) {
      body { overflow: auto; }
      .app { height: auto; min-height: 100vh; grid-template-columns: 1fr; grid-template-rows: auto auto minmax(680px, auto); }
      .global-topbar { grid-column: 1; grid-row: 1; min-height: 72px; }
      .left { grid-column: 1; grid-row: 2; padding: 14px 16px; overflow: visible; }
      .right { grid-column: 1; grid-row: 3; min-height: 680px; padding: 10px 16px 16px; overflow: visible; }
      .form-scroll { overflow: visible; }
      .grid-3 { grid-template-columns: repeat(3, minmax(0, 1fr)); }
    }
    @media (max-width: 760px) {
      body { padding-bottom: 0; }
      .global-topbar { align-items: flex-start; flex-direction: column; gap: 10px; padding: 13px 14px; }
      .topbar-brand { width: 100%; }
      .brand-caption { display: none; }
      .brand-divider, .brand-line .app-subtitle { display: none; }
      .topbar-controls { width: 100%; flex-wrap: wrap; }
      .topbar-config-actions { flex: 1; }
      .topbar-config-actions button { flex: 1; }
      .global-topbar .status-card { min-width: 170px; margin-left: auto; }
      .workspace-heading > div { flex-direction: column; gap: 0; }
      .source-controls { grid-template-columns: 1fr; }
      .source-controls > button:nth-of-type(1), .source-controls > button:nth-of-type(2) { grid-column: auto; }
      .grid, .grid-3 { grid-template-columns: 1fr; }
      .span-2, .span-3, .grid-3 .span-3 { grid-column: span 1; }
      .cover-row { grid-template-columns: 1fr; }
      .tabs { position: static; flex: 0 0 auto; height: auto; border-radius: 12px; }
      .tab-panel { border-top: 1px solid var(--border-med); border-radius: 12px; }
      .right-commandbar { align-items: stretch; flex-direction: column; min-height: 0; }
      .right-commandbar .ha-process { width: 100%; flex: 0 0 auto; grid-template-columns: repeat(3, minmax(0, 1fr)); }
      .settings-grid { grid-template-columns: 1fr; }
      .settings-danger { align-items: stretch; flex-direction: column; }
      .settings-danger button { width: 100%; }
    }
    @media (max-width: 480px) {
      .global-topbar .status-card { width: 100%; min-width: 0; padding: 8px 0 0; border-left: 0; border-top: 1px solid var(--border); }
      .right-commandbar .ha-process { grid-template-columns: 1fr; }
      .settings-actions { grid-template-columns: 1fr; }
    }

    /* ══════════════════════════════════════════════
       DARK CONSOLE — PREVIEW MATCH
       ══════════════════════════════════════════════ */
    html[data-theme="dark"] {
      --bg: #050a14;
      --surface: #0b1220;
      --surface-2: #101a2c;
      --surface-3: #172238;
      --glass: rgba(151,169,201,.045);
      --border: #1d2c44;
      --border-med: #2a3d5a;
      --border-strong: #425a7e;
      --text: #eef2f8;
      --text-2: #a8b4c8;
      --text-3: #67758d;
      --primary: #6f7df7;
      --primary-light: #8f9cff;
      --primary-glow: rgba(111,125,247,.24);
      --primary-bg: rgba(111,125,247,.13);
      --success: #38d68c;
      --success-bg: rgba(29,186,116,.13);
      --danger: #f14343;
      --danger-bg: rgba(241,67,67,.12);
      --warning: #f2b84b;
      --input-bg: #071321;
      --log-bg: #061221;
      --log-text: #b8c3d8;
    }
    html[data-theme="ocean"] {
      color-scheme: dark;
      --bg: #04161f;
      --surface: #08232e;
      --surface-2: #0b2c39;
      --surface-3: #123947;
      --glass: rgba(122,196,208,.055);
      --border: #173d4e;
      --border-med: #245468;
      --border-strong: #3a768c;
      --text: #e8f4f6;
      --text-2: #9cbbc4;
      --text-3: #5e7f8b;
      --primary: #158a9e;
      --primary-light: #54c3d0;
      --primary-glow: rgba(21,138,158,.24);
      --primary-bg: rgba(21,138,158,.14);
      --input-bg: #061b25;
      --log-bg: #051923;
      --log-text: #b0c9ce;
    }
    html[data-theme="aurora"] {
      color-scheme: dark;
      --bg: #140f1e;
      --surface: #1e172c;
      --surface-2: #281e39;
      --surface-3: #352747;
      --glass: rgba(207,166,222,.05);
      --border: #3d2c50;
      --border-med: #503b67;
      --border-strong: #6f5489;
      --text: #f3eef8;
      --text-2: #c1afce;
      --text-3: #7d6a8e;
      --primary: #8f5fbf;
      --primary-light: #c396de;
      --primary-glow: rgba(143,95,191,.24);
      --primary-bg: rgba(143,95,191,.14);
      --input-bg: #160f20;
      --log-bg: #140d1d;
      --log-text: #c9b9cf;
    }
    html[data-theme="jade"] {
      color-scheme: dark;
      --bg: #071511;
      --surface: #0d2119;
      --surface-2: #122b22;
      --surface-3: #1a382c;
      --glass: rgba(157,201,179,.05);
      --border: #203c31;
      --border-med: #315547;
      --border-strong: #4b7664;
      --text: #eef6f1;
      --text-2: #abc1b4;
      --text-3: #698276;
      --primary: #3f9277;
      --primary-light: #74bfa2;
      --primary-glow: rgba(63,146,119,.24);
      --primary-bg: rgba(63,146,119,.14);
      --input-bg: #091b14;
      --log-bg: #081912;
      --log-text: #b5c9bd;
    }
    html[data-theme="graphite"] {
      color-scheme: dark;
      --bg: #0f1217;
      --surface: #171b21;
      --surface-2: #1e242c;
      --surface-3: #29313b;
      --glass: rgba(188,199,215,.045);
      --border: #303944;
      --border-med: #414c5a;
      --border-strong: #5d6a7b;
      --text: #f0f2f5;
      --text-2: #b4bbc5;
      --text-3: #7b8490;
      --primary: #66778f;
      --primary-light: #9aabbe;
      --primary-glow: rgba(102,119,143,.24);
      --primary-bg: rgba(102,119,143,.15);
      --input-bg: #16191d;
      --log-bg: #15181c;
      --log-text: #c0c5cc;
    }
    html[data-theme="dark"] body {
      background:
        radial-gradient(900px 520px at 13% 9%, rgba(35,74,119,.13), transparent 68%),
        radial-gradient(760px 520px at 82% 104%, rgba(59,46,133,.10), transparent 72%),
        var(--bg);
    }
    .app {
      height: 100vh;
      display: grid;
      grid-template-columns: minmax(650px, 1.03fr) minmax(610px, .97fr);
      grid-template-rows: 74px minmax(0, 1fr);
      gap: 10px;
      padding: 0 16px 16px;
      background: transparent;
    }
    .global-topbar {
      grid-column: 1 / -1;
      grid-row: 1;
      height: 74px;
      margin: 0 -16px;
      padding: 0 22px;
      background: rgba(3,11,22,.96);
      border: 0;
      border-bottom: 1px solid #17283c;
      box-shadow: 0 8px 28px rgba(0,0,0,.22);
      backdrop-filter: blur(18px);
    }
    .topbar-brand { gap: 13px; }
    .brand-mark {
      width: 47px;
      height: 47px;
      flex: 0 0 47px;
      padding: 2px;
      border-radius: 0;
      background: transparent;
      box-shadow: none;
      transform: none;
    }
    .brand-mark::before, .brand-mark::after, .brand-mark span { display: none; }
    .brand-mark svg { width: 100%; height: 100%; overflow: visible; }
    .brand-mark svg path {
      fill: none;
      stroke: #7464f6;
      stroke-width: 3.5;
      stroke-linecap: round;
      stroke-linejoin: round;
      opacity: .92;
    }
    .brand-mark svg path:nth-child(2) { stroke: #8a77ff; }
    .brand-mark svg path:nth-child(3) { stroke: #5e78ec; }
    .brand-mark svg circle { fill: #a493ff; }
    .app-title { display: block; }
    .brand-line { gap: 20px; }
    .brand-line h1 {
      font-size: 28px;
      line-height: 1;
      font-weight: 820;
      letter-spacing: -.045em;
      background: none;
      color: var(--text);
      -webkit-text-fill-color: currentColor;
    }
    .brand-divider { height: 28px; background: #26364d; }
    .brand-line .app-subtitle {
      color: #aeb9ce;
      font-size: 17px;
      font-weight: 500;
      letter-spacing: .015em;
      text-transform: uppercase;
    }
    .brand-caption { display: none; }
    .topbar-controls { gap: 16px; }
    .topbar-config-actions { display: none; }
    .theme-cluster { display: flex; align-items: center; gap: 10px; }
    .theme-symbol { color: #a7b0c3; font-size: 22px; line-height: 1; }
    .theme-toggle {
      position: relative;
      width: 43px;
      height: 22px;
      min-height: 22px;
      padding: 0;
      border: 0;
      border-radius: 99px;
      background: linear-gradient(90deg, #344bb4, #7561ed);
      box-shadow: inset 0 0 0 1px rgba(255,255,255,.05);
      transform: none;
    }
    .theme-toggle::after {
      content: '';
      position: absolute;
      top: 2px;
      left: 22px;
      width: 18px;
      height: 18px;
      border-radius: 50%;
      background: #e7ebf6;
      box-shadow: 0 1px 5px rgba(0,0,0,.45);
      transition: left .18s ease;
    }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .theme-toggle::after { left: 3px; }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .theme-toggle {
      background: linear-gradient(90deg, var(--primary), var(--primary-light));
    }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .theme-toggle:hover:not(:disabled) {
      background: linear-gradient(90deg, var(--primary), var(--primary-light));
      filter: brightness(1.05);
    }
    .theme-toggle:hover:not(:disabled) { transform: none; background: linear-gradient(90deg, #4059c2, #826df3); }
    .topbar-separator { width: 1px; height: 34px; background: #203149; }
    .global-topbar .status-card {
      min-width: 380px;
      display: grid;
      grid-template-columns: auto 36px minmax(120px, 1fr) 38px 7px;
      align-items: center;
      gap: 12px;
      padding: 0;
      border: 0;
    }
    .queue-state-label, .queue-count {
      color: #aeb8cb;
      font-size: 13px;
      font-weight: 500;
      white-space: nowrap;
    }
    .global-topbar .percent {
      min-width: 36px;
      color: #d8deea;
      font-size: 14px;
      font-weight: 650;
      text-align: left;
      background: none;
      -webkit-text-fill-color: currentColor;
    }
    .header-progress { height: 12px; background: #182538; border-radius: 99px; }
    .progress-bar { background: linear-gradient(90deg, #7260ed, #8666ff); animation: none; }
    .global-topbar .state-dot { grid-column: 5; grid-row: 1; }
    .topbar-icon-button {
      width: 38px;
      height: 38px;
      min-height: 38px;
      padding: 0;
      border: 0;
      border-radius: 8px;
      background: transparent;
      color: #b9c2d4;
      font-size: 22px;
      box-shadow: none;
    }

    .left {
      grid-column: 1;
      grid-row: 2;
      padding: 0;
      border: 0;
      background: transparent;
      overflow: hidden;
    }
    .workspace-heading[hidden] { display: none; }
    .form-scroll { padding: 0 6px 14px 0; }
    .section {
      margin: 0 0 10px;
      padding: 16px;
      border: 1px solid #20334c;
      border-radius: 8px;
      background: linear-gradient(135deg, rgba(12,28,47,.98), rgba(8,21,37,.98));
      box-shadow: inset 0 1px 0 rgba(255,255,255,.018);
      transition: border-color .18s ease, background .18s ease;
    }
    .section:hover {
      border-color: #2c4564;
      box-shadow: inset 0 1px 0 rgba(255,255,255,.025);
    }
    .section-title {
      gap: 10px;
      margin-bottom: 15px;
      color: #edf2fa;
      font-size: 16px;
      font-weight: 700;
    }
    .section-icon {
      width: 20px;
      height: 20px;
      border-radius: 0;
      background: transparent !important;
      color: #b9c5d9 !important;
      font-size: 18px;
      line-height: 0;
    }
    label {
      margin-bottom: 6px;
      color: #aeb9cd;
      font-size: 13px;
      font-weight: 600;
      letter-spacing: .005em;
      text-transform: none;
    }
    input, select, textarea, .custom-select-trigger, .chips {
      border-color: #29405d;
      border-radius: 6px;
      background-color: #071422;
      color: #e8edf7;
      box-shadow: inset 0 1px 3px rgba(0,0,0,.18);
    }
    input, select, .custom-select-trigger { min-height: 44px; padding: 9px 13px; font-size: 14px; }
    input::placeholder, textarea::placeholder { color: #65748d; }
    .custom-select-trigger:hover:not(:disabled) { background: #0d1d30; border-color: #395575; }
    .source-section { min-height: 178px; }
    .source-directory { grid-template-columns: minmax(0, 1fr) 112px; gap: 10px; }
    .source-directory button { min-height: 44px; }
    .source-query { margin-top: 12px; }
    .source-controls {
      grid-template-columns: minmax(145px, .82fr) minmax(230px, 1.55fr) minmax(125px, .82fr) minmax(125px, .82fr);
      gap: 10px;
    }
    .source-controls > .custom-select, .source-controls > input, .source-controls > button { min-height: 44px; }
    .source-controls > button { border-radius: 6px; font-size: 13px; }
    .source-controls .btn-primary, .cover-actions .btn-primary {
      background: linear-gradient(135deg, #7659ef, #6a50d8);
      border-color: #8068ef;
      box-shadow: 0 4px 14px rgba(100,73,222,.20);
    }
    .source-controls .quiet-button { background: transparent; border-color: #745ee0; color: #a992ff; }

    .metadata-section { min-height: 229px; }
    .metadata-title-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 24px; margin-bottom: 14px; }
    .metadata-title-field { display: grid; grid-template-columns: 68px minmax(0, 1fr); align-items: center; gap: 10px; }
    .metadata-title-field label { margin: 0; color: #d5dce9; }
    .people-row {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
      margin-top: 11px;
    }
    .people-row .entity-row {
      grid-template-columns: 58px minmax(0, 1fr) auto;
      margin-top: 0;
      gap: 8px;
    }
    .people-row .entity-hint { display: none; }
    .people-row .field-mini-action { min-height: 44px; padding: 0 10px; font-size: 12px; white-space: nowrap; }
    .entity-row {
      display: grid;
      grid-template-columns: 68px minmax(0, 1fr) auto;
      align-items: center;
      gap: 10px;
      margin-top: 11px;
    }
    .entity-row > label { margin: 0; color: #d5dce9; }
    .entity-row > input[type="hidden"] { display: none; }
    .chips { min-height: 44px; padding: 6px 9px; }
    .chip { height: 30px; min-height: 30px; padding: 0 9px 0 12px; border-radius: 5px; background: #1a2a40 !important; }
    .chip > button { color: #b5c0d2; }
    .field-mini-action {
      min-height: 44px;
      padding: 0 14px;
      border-radius: 6px;
      background: transparent;
      border-color: #6f5cce;
      color: #a891ff;
    }
    .entity-hint { width: 142px; color: #65758f; font-size: 11px; text-align: center; }

    .archive-section { min-height: 298px; }
    .archive-main-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 14px 18px;
    }
    .archive-main-grid > div {
      display: grid;
      grid-template-columns: max-content minmax(0, 1fr);
      align-items: center;
      gap: 8px;
    }
    .archive-main-grid label { margin: 0; color: #d5dce9; white-space: nowrap; }
    .archive-extra-grid {
      display: grid;
      grid-template-columns: minmax(170px, .75fr) minmax(0, 1.55fr);
      gap: 12px 16px;
      margin-top: 14px;
    }
    .tag-archive { grid-column: 1 / -1; }
    .series-inline { grid-template-columns: minmax(0, 1fr) auto; }
    .series-inline button { min-height: 44px; border-radius: 6px; }
    #tagPool { min-height: 40px; }
    #tagInput { min-height: 38px; }

    .visual-section { min-height: 304px; }
    .cover-row { grid-template-columns: 192px minmax(0, 1fr); gap: 22px; }
    .cover-preview-column { min-width: 0; }
    .cover-box {
      position: relative;
      width: 192px;
      height: 192px;
      border-radius: 7px;
      border-color: #31465f;
      background: #0c1929;
    }
    .cover-box img { object-fit: cover; }
    .cover-change-button {
      position: absolute;
      left: 50%;
      bottom: 12px;
      z-index: 2;
      opacity: 0;
      pointer-events: none;
      min-height: 36px;
      padding: 0 14px;
      transform: translateX(-50%);
      border-color: rgba(255,255,255,.22);
      background: rgba(5,12,22,.78);
      color: #e8edf7;
      backdrop-filter: blur(8px);
      transition: opacity .15s ease, transform .15s ease, background .15s ease, border-color .15s ease;
    }
    .cover-box:hover .cover-change-button,
    .cover-change-button:focus-visible {
      opacity: 1;
      pointer-events: auto;
    }
    .cover-change-button:hover:not(:disabled) { transform: translateX(-50%); }
    @media (hover: none) {
      .cover-change-button {
        opacity: 1;
        pointer-events: auto;
      }
    }
    .cover-meta { color: #63728b; }
    .visual-content-column { min-width: 0; }
    .visual-content-column textarea { min-height: 220px; height: 220px; padding: 13px 16px; line-height: 1.72; resize: vertical; }

    .right {
      grid-column: 2;
      grid-row: 2;
      display: grid;
      grid-template-rows: minmax(0, 1fr);
      gap: 10px;
      min-height: 0;
      padding: 0;
      background: transparent;
      overflow: hidden;
    }
    .queue-console, .live-log-card {
      min-height: 0;
      overflow: hidden;
      border: 1px solid #1d3048;
      border-radius: 8px;
      background: linear-gradient(145deg, rgba(10,25,43,.98), rgba(6,18,32,.98));
      box-shadow: inset 0 1px 0 rgba(255,255,255,.015);
    }
    .queue-console { display: flex; flex-direction: column; }
    .tabs {
      height: 61px;
      flex: 0 0 61px;
      gap: 10px;
      padding: 0 23px;
      border: 0;
      border-bottom: 1px solid #1d3048;
      border-radius: 0;
      background: transparent;
      box-shadow: none;
    }
    .tab {
      min-height: 61px;
      padding: 0 12px;
      border-radius: 0;
      border-bottom: 2px solid transparent;
      color: #aab5c9;
      font-size: 15px;
      font-weight: 600;
    }
    .tab.active { color: #f0f3fa; border-bottom-color: #8d73ff; background: transparent; }
    .tab:hover:not(:disabled) { background: transparent; color: #eef2f9; }
    .overview-tab { display: none; }
    .queue-console > .tab-panel {
      flex: 1;
      min-height: 0;
      padding: 0 14px;
      border: 0;
      border-radius: 0;
      background: transparent;
      box-shadow: none;
    }
    .queue-console > .tab-panel#panel-log {
      padding: 0;
    }
    .queue-console > .tab-panel#panel-log .log {
      margin: 0;
      border: 0;
      border-radius: 0;
    }
    .queue-actions { display: none; }
    .queue-actions.has-selection {
      position: absolute;
      right: 18px;
      bottom: 76px;
      z-index: 4;
      display: flex;
      margin: 0;
      padding: 7px;
      border: 1px solid #2a405d;
      border-radius: 7px;
      background: rgba(6,18,32,.94);
      box-shadow: 0 12px 28px rgba(0,0,0,.38);
      backdrop-filter: blur(12px);
    }
    .queue-actions.has-selection button { min-height: 32px; box-shadow: none; }
    #panel-queue { position: relative; }
    .table-wrap {
      border: 0;
      border-radius: 0;
      background: transparent;
    }
    .queue-console table { min-width: 670px; }
    .queue-console th {
      height: 49px;
      padding: 0 10px;
      border-bottom: 1px solid #263b55;
      background: rgba(15,31,51,.72);
      color: #8190a9;
      font-size: 12px;
      text-transform: none;
      letter-spacing: 0;
    }
    .queue-console td {
      height: 53px;
      padding: 0 10px;
      border-bottom: 1px solid #1d3048;
      color: #d5dce8;
      font-size: 13px;
    }
    .queue-console tbody tr:nth-child(even) td { background: transparent; }
    .queue-console tbody tr.selected td { background: rgba(114,88,245,.10) !important; }
    .queue-console th:nth-child(1), .queue-console td:nth-child(1) { width: 34px; }
    .queue-console th:nth-child(2), .queue-console td:nth-child(2) { width: 30px; }
    .queue-console th:nth-child(3) { width: 36%; }
    .queue-console th:nth-child(4) { width: 19%; }
    .queue-console th:nth-child(5) { width: 20%; }
    .queue-console th:nth-child(6) { width: 15%; }
    .queue-check {
      width: 16px;
      height: 16px;
      min-width: 16px;
      min-height: 16px;
      margin: 0;
      accent-color: var(--primary);
      cursor: pointer;
    }
    .queue-platform { display: inline-flex; align-items: center; gap: 7px; color: #d3dae7; }
    .queue-platform-icon {
      display: grid;
      place-items: center;
      width: 19px;
      height: 19px;
      border-radius: 5px;
      background: #ff552e;
      color: #fff;
      font-size: 9px;
      font-weight: 800;
    }
    .queue-progress { display: flex; flex-direction: column; gap: 5px; min-width: 90px; }
    .queue-progress > span { color: #cfd6e3; font-size: 11px; }
    .queue-progress-track { display: block; width: 100%; height: 4px; overflow: hidden; border-radius: 99px; background: #223149; }
    .queue-progress-track i { display: block; height: 100%; border-radius: inherit; background: #8d72ff; }
    .queue-progress.done .queue-progress-track i { background: #3ed38a; }
    .queue-row-actions { display: inline-flex; gap: 5px; }
    .queue-row-actions button { width: 28px; height: 28px; min-height: 28px; padding: 0; border: 0; background: transparent; color: #9eacc1; box-shadow: none; }
    .status-badge { min-height: 26px; padding: 0 11px; border: 0 !important; }
    .status-badge::before { display: none; }
    .status-badge.pending { background: #18273b; color: #aab5c8; }
    .status-badge.processing { background: #102e59; color: #6da7ff; animation: none; }
    .status-badge.done { background: #0d3b31; color: #43dc91; }
    .right-commandbar {
      min-height: 68px;
      flex: 0 0 68px;
      margin-top: 0;
      padding: 10px 15px;
      border: 0;
      border-top: 1px solid #1d3048;
      border-radius: 0;
      background: rgba(6,17,30,.66);
      box-shadow: none;
    }
    .selection-copy { color: #8795ac; font-size: 13px; }
    .right-commandbar .ha-process { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; flex: 1; min-width: 0; }
    .right-commandbar .ha-process button { min-width: 0; min-height: 45px; border-radius: 6px; font-size: 13px; }
    #addQueueBtn { background: transparent; border-color: #765fe1; color: #aa94ff; }
    #startQueueBtn { background: rgba(48,87,173,.28); border-color: #4f7ada; color: #7ea8ff; box-shadow: none; }
    #stopBtn { background: rgba(188,29,36,.38); border-color: #ef343d; color: #ffb0b4; box-shadow: none; }

    .live-log-card { display: flex; flex-direction: column; }
    .live-log-head {
      height: 55px;
      flex: 0 0 55px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 14px 0 17px;
      border-bottom: 1px solid #1d3048;
      color: #edf1f8;
    }
    .live-log-head strong { font-size: 14px; }
    .live-log-head button { min-height: 34px; padding: 0 12px; background: transparent; }
    .live-log-card #panel-log, .live-log-card #panel-log.active {
      display: flex;
      flex: 1;
      min-height: 0;
      padding: 9px 10px 10px;
      border: 0;
      border-radius: 0;
      background: transparent;
      box-shadow: none;
    }
    #logFilterBox { display: none !important; }
    .log {
      padding: 10px 12px;
      border-color: #1d3048;
      border-radius: 6px;
      background: #061321;
      color: #aeb9cc;
      font-size: 12px;
      line-height: 1.7;
    }
    html[data-theme="dark"] .log-line.info { color: #66a4ff; }
    html[data-theme="dark"] .log-line.warning { color: #f0c359; }
    html[data-theme="dark"] .log-line.error { color: #ff777d; }

    /* Complete light palette for the redesigned console. */
    html[data-theme="light"] {
      --bg: #f4f6fa;
      --surface: #ffffff;
      --surface-2: #f7f9fc;
      --surface-3: #e9edf5;
      --border: #d8deea;
      --border-med: #c6cfdf;
      --border-strong: #a8b4ca;
      --text: #1a2233;
      --text-2: #4d5b74;
      --text-3: #8391a8;
      --input-bg: #ffffff;
      --log-bg: #f7f9fc;
      --log-text: #46566f;
    }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) body {
      background:
        radial-gradient(900px 520px at 13% 9%, color-mix(in srgb, var(--primary) 8%, transparent), transparent 68%),
        radial-gradient(760px 520px at 82% 104%, color-mix(in srgb, var(--cyan) 7%, transparent), transparent 72%),
        var(--bg);
    }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .brand-mark svg path { stroke: var(--primary); }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .brand-mark svg path:nth-child(2) { stroke: var(--primary-light); }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .brand-mark svg path:nth-child(3) { stroke: color-mix(in srgb, var(--primary) 72%, var(--cyan)); }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .brand-mark svg circle { fill: var(--primary-light); }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .global-topbar {
      background: color-mix(in srgb, var(--surface) 96%, transparent);
      border-bottom-color: var(--border-med);
      box-shadow: var(--shadow);
    }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .brand-divider,
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .topbar-separator { background: var(--border-med); }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .brand-line .app-subtitle,
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .theme-symbol,
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .queue-state-label,
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .queue-count { color: var(--text-2); }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .global-topbar .percent { color: var(--text); }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .header-progress { background: var(--surface-3); }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .topbar-icon-button { color: var(--text-2); }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .topbar-icon-button:hover:not(:disabled) { background: var(--primary-bg); color: var(--primary); }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .section {
      border-color: var(--border-med);
      background: linear-gradient(135deg, color-mix(in srgb, var(--surface) 99%, transparent), color-mix(in srgb, var(--surface-2) 99%, transparent));
      box-shadow: inset 0 1px 0 color-mix(in srgb, var(--surface) 90%, transparent), var(--shadow-sm);
    }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .section:hover { border-color: var(--border-strong); }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .section-title { color: var(--text); }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .section-icon { color: var(--text-2) !important; }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) label { color: var(--text-2); }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) input,
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) select,
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) textarea,
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .custom-select-trigger,
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .chips {
      border-color: var(--border-med);
      background-color: var(--input-bg);
      color: var(--text);
      box-shadow: inset 0 1px 2px color-mix(in srgb, var(--text-3) 8%, transparent);
    }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) input::placeholder,
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) textarea::placeholder { color: var(--text-3); }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .custom-select-trigger:hover:not(:disabled) { background: var(--surface-2); border-color: var(--border-strong); }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .metadata-title-field label,
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .entity-row > label,
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .archive-main-grid label { color: var(--text-2); }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .chip { background: var(--surface-3) !important; color: var(--text); }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .chip > button { color: var(--text-2); }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .entity-hint,
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .cover-meta { color: var(--text-3); }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .archive-extra-grid { border-top-color: var(--border); }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .cover-box { border-color: var(--border-med); background: var(--surface-2); }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .queue-console,
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .live-log-card {
      border-color: var(--border);
      background: linear-gradient(145deg, color-mix(in srgb, var(--surface) 99%, transparent), color-mix(in srgb, var(--surface-2) 99%, transparent));
      box-shadow: var(--shadow);
    }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .tabs,
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .live-log-head { border-bottom-color: var(--border); }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .tab { color: var(--text-2); }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .tab.active { color: var(--primary); border-bottom-color: var(--primary-light); }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .tab:hover:not(:disabled) { color: var(--text); }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .queue-actions.has-selection {
      border-color: var(--border-med);
      background: color-mix(in srgb, var(--surface) 96%, transparent);
      box-shadow: var(--shadow);
    }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .queue-console th {
      border-bottom-color: var(--border-med);
      background: color-mix(in srgb, var(--surface-2) 85%, transparent);
      color: var(--text-3);
    }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .queue-console td { border-bottom-color: var(--border); color: var(--text-2); }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .queue-console tbody tr.selected td { background: var(--primary-bg) !important; }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .queue-platform { color: var(--text-2); }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .queue-progress > span { color: var(--text-2); }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .queue-progress-track { background: var(--surface-3); }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .queue-row-actions button { color: var(--text-2); }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .status-badge.pending { background: var(--surface-3); color: var(--text-2); }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .status-badge.processing { background: var(--primary-bg); color: var(--primary); }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .status-badge.done { background: var(--success-bg); color: var(--success); }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .right-commandbar {
      border-top-color: var(--border);
      background: color-mix(in srgb, var(--surface-2) 82%, transparent);
    }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .selection-copy { color: var(--text-3); }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) #addQueueBtn { background: var(--surface); color: var(--primary); }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) #startQueueBtn { background: var(--primary-bg); border-color: var(--primary); color: var(--primary); }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) #stopBtn { background: var(--danger-bg); border-color: var(--danger); color: var(--danger); }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .live-log-head { color: var(--text); }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .log {
      border-color: var(--border);
      background: var(--log-bg);
      color: var(--log-text);
    }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .log-line.info { color: var(--primary); }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .log-line.warning { color: var(--warning); }
    :is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .log-line.error { color: var(--danger); }

    .appearance-card { grid-column: 1 / -1; }
    .theme-picker { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
    .theme-option {
      position: relative;
      display: grid;
      grid-template-columns: 48px minmax(0, 1fr) 18px;
      align-items: center;
      gap: 10px;
      min-height: 68px;
      padding: 9px 10px;
      border-color: var(--border);
      background: var(--surface-2);
      color: var(--text-2);
      box-shadow: none;
      text-align: left;
      white-space: normal;
    }
    .theme-option:hover:not(:disabled) { transform: none; border-color: var(--border-strong); background: var(--surface-3); box-shadow: none; }
    .theme-option.active { border-color: var(--primary); background: var(--primary-bg); color: var(--text); box-shadow: inset 0 0 0 1px var(--primary-glow); }
    .theme-option-copy { min-width: 0; display: flex; flex-direction: column; gap: 1px; }
    .theme-option-copy strong { color: var(--text); font-size: 12px; line-height: 1.25; }
    .theme-option-copy small { color: var(--text-3); font-size: 10px; font-weight: 600; }
    .theme-selected { color: var(--primary-light); font-size: 13px; opacity: 0; }
    .theme-option.active .theme-selected { opacity: 1; }
    .theme-preview {
      position: relative;
      width: 46px;
      height: 42px;
      display: flex;
      align-items: flex-end;
      gap: 3px;
      padding: 6px;
      overflow: hidden;
      border: 1px solid var(--preview-border);
      border-radius: 7px;
      background: var(--preview-bg);
    }
    .theme-preview::before { content: ''; position: absolute; inset: 6px 6px 15px; border-radius: 3px; background: var(--preview-card); }
    .theme-preview i { position: relative; z-index: 1; width: 8px; height: 5px; border-radius: 3px; background: var(--preview-accent); }
    .theme-preview i:nth-child(2) { width: 12px; opacity: .72; }
    .theme-preview i:nth-child(3) { width: 6px; opacity: .46; }
    .preview-dark { --preview-bg:#050a14; --preview-card:#101a2c; --preview-border:#2a3d5a; --preview-accent:#8f9cff; }
    .preview-light { --preview-bg:#f4f6fa; --preview-card:#fff; --preview-border:#c6cfdf; --preview-accent:#6b79e8; }
    .preview-linen { --preview-bg:#f7f2e9; --preview-card:#fffcf6; --preview-border:#d3c2a9; --preview-accent:#d97706; }
    .preview-mint { --preview-bg:#eef5f1; --preview-card:#fff; --preview-border:#b4d2c3; --preview-accent:#0d9488; }
    .preview-rose { --preview-bg:#faf3f5; --preview-card:#fffdfd; --preview-border:#dfb9c5; --preview-accent:#e11d48; }
    .preview-ocean { --preview-bg:#04161f; --preview-card:#0b2c39; --preview-border:#245468; --preview-accent:#54c3d0; }
    .preview-aurora { --preview-bg:#140f1e; --preview-card:#281e39; --preview-border:#503b67; --preview-accent:#c396de; }
    .preview-jade { --preview-bg:#071511; --preview-card:#122b22; --preview-border:#315547; --preview-accent:#74bfa2; }
    .preview-graphite { --preview-bg:#0f1217; --preview-card:#1e242c; --preview-border:#414c5a; --preview-accent:#9aabbe; }

    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) body {
      background:
        radial-gradient(900px 520px at 13% 9%, color-mix(in srgb, var(--primary) 13%, transparent), transparent 68%),
        radial-gradient(760px 520px at 82% 104%, color-mix(in srgb, var(--primary-light) 8%, transparent), transparent 72%),
        var(--bg);
    }
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .global-topbar {
      background: color-mix(in srgb, var(--bg) 95%, transparent);
      border-bottom-color: var(--border);
    }
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .brand-mark svg path { stroke: var(--primary); }
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .brand-mark svg path:nth-child(2) { stroke: var(--primary-light); }
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .brand-mark svg path:nth-child(3) { stroke: color-mix(in srgb, var(--primary) 72%, #9ab9ff); }
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .brand-mark svg circle { fill: var(--primary-light); }
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .brand-divider,
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .topbar-separator { background: var(--border-med); }
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .brand-line .app-subtitle,
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .theme-symbol,
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .queue-state-label,
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .queue-count { color: var(--text-2); }
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .theme-toggle { background: linear-gradient(90deg, var(--primary), var(--primary-light)); }
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .global-topbar .percent { color: var(--text); }
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .header-progress { background: var(--surface-3); }
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .progress-bar { background: linear-gradient(90deg, var(--primary), var(--primary-light)); }
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .topbar-icon-button { color: var(--text-2); }
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .section {
      border-color: var(--border);
      background: linear-gradient(135deg, color-mix(in srgb, var(--surface-2) 96%, transparent), color-mix(in srgb, var(--surface) 97%, transparent));
    }
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .section:hover { border-color: var(--border-strong); }
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .section-title { color: var(--text); }
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .section-icon { color: var(--text-2) !important; }
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) label { color: var(--text-2); }
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) input,
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) select,
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) textarea,
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .custom-select-trigger,
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .chips { border-color: var(--border-med); background-color: var(--input-bg); color: var(--text); }
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) input::placeholder,
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) textarea::placeholder { color: var(--text-3); }
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .custom-select-trigger:hover:not(:disabled) { background: var(--surface-2); border-color: var(--border-strong); }
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .source-controls .btn-primary,
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .cover-actions .btn-primary { background: linear-gradient(135deg, var(--primary-light), var(--primary)); border-color: var(--primary-light); box-shadow: 0 4px 14px var(--primary-glow); }
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .source-controls .quiet-button,
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .field-mini-action { border-color: var(--primary); color: var(--primary-light); }
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .metadata-title-field label,
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .entity-row > label,
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .archive-main-grid label { color: var(--text-2); }
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .chip { background: var(--surface-3) !important; color: var(--text); }
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .chip > button { color: var(--text-2); }
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .entity-hint,
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .cover-meta { color: var(--text-3); }
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .archive-extra-grid,
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .tabs,
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .live-log-head { border-color: var(--border); }
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .cover-box { border-color: var(--border-med); background: var(--surface-2); }
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .queue-console,
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .live-log-card { border-color: var(--border); background: linear-gradient(145deg, var(--surface-2), var(--surface)); }
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .tab { color: var(--text-2); }
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .tab.active { color: var(--text); border-bottom-color: var(--primary-light); }
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .queue-actions.has-selection { border-color: var(--border-med); background: color-mix(in srgb, var(--surface) 95%, transparent); }
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .queue-console th { border-bottom-color: var(--border-med); background: color-mix(in srgb, var(--surface-2) 78%, transparent); color: var(--text-3); }
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .queue-console td { border-bottom-color: var(--border); color: var(--text-2); }
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .queue-console tbody tr.selected td { background: var(--primary-bg) !important; }
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .queue-platform,
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .queue-progress > span,
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .queue-row-actions button { color: var(--text-2); }
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .queue-progress-track { background: var(--surface-3); }
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .queue-progress-track i { background: var(--primary-light); }
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .status-badge.pending { background: var(--surface-3); color: var(--text-2); }
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .status-badge.processing { background: var(--primary-bg); color: var(--primary-light); }
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .right-commandbar { border-top-color: var(--border); background: color-mix(in srgb, var(--surface) 70%, transparent); }
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .selection-copy { color: var(--text-3); }
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) #addQueueBtn { border-color: var(--primary); color: var(--primary-light); }
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) #startQueueBtn { background: var(--primary-bg); border-color: var(--primary); color: var(--primary-light); }
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .live-log-head { color: var(--text); }
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .log { border-color: var(--border); background: var(--log-bg); color: var(--log-text); }
    :is(html[data-theme="ocean"], html[data-theme="aurora"], html[data-theme="jade"], html[data-theme="graphite"]) .log-line.info { color: var(--primary-light); }

    .chip.colored-chip {
      background: linear-gradient(135deg, var(--chip-color-a), var(--chip-color-b)) !important;
      border: 1px solid var(--chip-border);
      color: #fff;
      box-shadow: 0 3px 10px color-mix(in srgb, var(--chip-color-a) 24%, transparent);
    }
    .chip.colored-chip > button { color: rgba(255,255,255,.76); }
    .chip.colored-chip > button:hover { color: #fff; }

    #tagPool .album-tag-chip {
      background: linear-gradient(135deg, var(--tag-color-a), var(--tag-color-b)) !important;
      border: 1px solid var(--tag-border);
      color: #fff;
      box-shadow: 0 3px 10px color-mix(in srgb, var(--tag-color-a) 24%, transparent);
    }
    #tagPool .album-tag-chip > button { color: rgba(255,255,255,.76); }
    #tagPool .album-tag-chip > button:hover { color: #fff; }

    /* ── Modern UI polish ─────────────────────────── */
    .ui-icon {
      width: 16px;
      height: 16px;
      flex: 0 0 16px;
      fill: none;
      stroke: currentColor;
      stroke-width: 1.8;
      stroke-linecap: round;
      stroke-linejoin: round;
    }
    .section .section-icon {
      width: 26px;
      height: 26px;
      border-radius: 8px;
      color: var(--primary-light) !important;
      background: color-mix(in srgb, var(--primary) 12%, transparent) !important;
      box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--primary) 14%, transparent);
    }
    .section .section-icon .ui-icon {
      width: 17px;
      height: 17px;
      flex-basis: 17px;
    }
    .section-title {
      font-size: 16px;
      color: var(--text);
    }
    .section {
      box-shadow: inset 0 1px 0 color-mix(in srgb, var(--surface) 88%, transparent), 0 14px 34px rgba(0,0,0,.08);
    }
    .section:hover {
      box-shadow: inset 0 1px 0 color-mix(in srgb, var(--surface) 88%, transparent), 0 18px 40px rgba(0,0,0,.12);
    }
    button:focus-visible,
    input:focus-visible,
    select:focus-visible,
    textarea:focus-visible,
    .custom-select-trigger:focus-visible,
    .chips:focus-within {
      outline: none;
      box-shadow: 0 0 0 3px color-mix(in srgb, var(--primary) 22%, transparent);
    }
    .settings-card {
      border-color: color-mix(in srgb, var(--border-med) 88%, transparent);
      background: linear-gradient(145deg, color-mix(in srgb, var(--surface-2) 96%, transparent), var(--surface));
      box-shadow: inset 0 1px 0 color-mix(in srgb, var(--surface) 88%, transparent), 0 10px 28px rgba(0,0,0,.08);
    }
    .settings-card-head strong { font-size: 14px; }
    .settings-icon {
      box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--primary) 16%, transparent);
    }
    .settings-icon .ui-icon,
    .topbar-icon-button .ui-icon {
      width: 17px;
      height: 17px;
      flex-basis: 17px;
    }
    .theme-symbol .ui-icon {
      width: 18px;
      height: 18px;
      flex-basis: 18px;
    }
    .theme-selected .ui-icon {
      width: 13px;
      height: 13px;
      flex-basis: 13px;
    }
    .section-toggle .ui-icon {
      width: 14px;
      height: 14px;
      flex-basis: 14px;
    }
    .settings-actions button {
      border-radius: 8px;
      transition: background-color .15s ease, border-color .15s ease, color .15s ease, box-shadow .15s ease;
    }
    .custom-select-value {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      flex: 1;
      min-width: 0;
    }
    .platform-logo {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 18px;
      height: 18px;
      flex: 0 0 18px;
      border-radius: 6px;
      background: var(--brand-color);
      color: #fff;
      font-size: 10px;
      font-weight: 800;
      line-height: 1;
      box-shadow: 0 3px 9px color-mix(in srgb, var(--brand-color) 32%, transparent);
    }
    .custom-select-option .platform-logo {
      width: 20px;
      height: 20px;
      flex-basis: 20px;
      border-radius: 7px;
      font-size: 11px;
    }
    .modal {
      border-color: color-mix(in srgb, var(--border-med) 86%, transparent);
      box-shadow: 0 26px 70px rgba(0,0,0,.28), inset 0 1px 0 color-mix(in srgb, var(--surface) 88%, transparent);
    }
    .toast {
      z-index: 2000;
    }

    @media (max-width: 1500px) and (min-width: 1181px) {
      .app { grid-template-columns: minmax(620px, 1.03fr) minmax(560px, .97fr); }
      .brand-line h1 { font-size: 24px; }
      .brand-line .app-subtitle { font-size: 14px; }
      .global-topbar .status-card { min-width: 310px; grid-template-columns: auto 34px minmax(90px, 1fr) 34px 7px; gap: 9px; }
      .source-controls { grid-template-columns: minmax(140px, .78fr) minmax(0, 1.22fr); }
      .source-controls > button:nth-of-type(1) { grid-column: 1; }
      .source-controls > button:nth-of-type(2) { grid-column: 2; }
      .right-commandbar .ha-process button { min-width: 108px; }
    }
    @media (max-width: 1180px) {
      body { overflow: auto; }
      .app { height: auto; min-height: 100vh; grid-template-columns: 1fr; grid-template-rows: 74px auto minmax(760px, 100vh); }
      .global-topbar { grid-column: 1; grid-row: 1; }
      .left { grid-column: 1; grid-row: 2; overflow: visible; }
      .right { grid-column: 1; grid-row: 3; min-height: 760px; }
      .form-scroll { overflow: visible; }
      .source-controls { grid-template-columns: minmax(145px, .75fr) minmax(0, 1.35fr) repeat(2, minmax(130px, .8fr)); }
    }
    @media (max-width: 760px) {
      .app { display: flex; flex-direction: column; min-width: 0; padding: 0 10px 12px; }
      .global-topbar { position: static; width: calc(100% + 20px); height: auto; margin: 0 -10px; padding: 12px; flex-direction: column; align-items: stretch; }
      .topbar-brand { width: 100%; }
      .brand-mark { width: 38px; height: 38px; flex-basis: 38px; }
      .brand-line h1 { font-size: 22px; }
      .brand-divider, .brand-line .app-subtitle { display: none; }
      .topbar-controls { width: 100%; justify-content: space-between; gap: 10px; }
      .global-topbar .status-card { min-width: 0; flex: 1; grid-template-columns: auto 32px minmax(55px, 1fr) 34px 7px; gap: 7px; }
      .theme-symbol { display: none; }
      .topbar-separator { display: none; }
      .left, .right { width: 100%; }
      .source-controls, .metadata-title-grid, .people-row, .archive-main-grid, .archive-extra-grid { grid-template-columns: 1fr; }
      .theme-picker { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .source-controls > button:nth-of-type(1), .source-controls > button:nth-of-type(2) { grid-column: auto; }
      .metadata-title-field, .entity-row, .archive-main-grid > div { grid-template-columns: 1fr; }
      .people-row { gap: 8px; }
      .people-row .entity-row { grid-template-columns: 1fr; }
      .people-row .entity-hint { display: block; }
      .entity-row > label, .metadata-title-field label, .archive-main-grid label { margin-bottom: 4px; }
      .entity-hint { display: none; }
      .tag-archive { grid-column: auto; }
      .cover-row { grid-template-columns: 1fr; }
      .cover-box { width: min(100%, 220px); height: 220px; margin: 0 auto; }
      .cover-actions { width: 100%; }
      .cover-actions button { flex: 1; min-width: 0; }
      .right { display: grid; grid-template-rows: minmax(0, 1fr); min-height: 680px; }
      .tabs { overflow-x: auto; }
      .right-commandbar { align-items: stretch; flex-direction: column; min-height: 0; flex-basis: auto; }
      .right-commandbar .ha-process { display: grid; width: 100%; grid-template-columns: 1fr; }
      .right-commandbar .ha-process button { width: 100%; min-width: 0; }
      .queue-console table { min-width: 620px; }
      .settings-grid { grid-template-columns: 1fr; }
    }
    @media (max-width: 430px) {
      .theme-picker { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="app">
    <!-- ── Global Top Bar ─────────────────────────── -->
    <header class="global-topbar">
      <div class="topbar-brand">
        <span class="brand-mark" aria-hidden="true">
          <svg viewBox="0 0 48 48" focusable="false">
            <path d="M24 6c8.7 0 15.8 5.2 18.9 12.5-5.9-3.3-12.7-3.2-17.7.3-4.8 3.4-7 9.1-5.6 14.4C12.4 31.4 7 25 7 17.4 11.4 10.4 17 6 24 6Z"/>
            <path d="M40.8 20.9c4.3 7.5 2.9 16.2-2.2 22-1.1-6.7-4.6-12.5-10.2-14.8-5.4-2.2-11.4-.8-15.3 3.1-1.9-7.2 1-14.9 7.5-18.7 8.3-.2 16.7 2.2 20.2 8.4Z"/>
            <path d="M35.9 40.8c-4.4 7.5-12.8 10.8-20.5 9.3 5.7-3.8 9.1-9.6 8.4-15.6-.7-5.8-4.7-10.3-10-11.7 5.3-5.3 13.4-6.5 19.9-2.7 4.1 7.2 5.7 15.2 2.2 20.7Z"/>
            <circle cx="24" cy="24" r="4.2"/>
          </svg>
        </span>
        <div class="app-title">
          <div class="brand-line"><h1>声境元枢</h1><span class="brand-divider"></span><span class="app-subtitle">AUDIOMETA NEXUS · 有声书元数据处理</span></div>
        </div>
      </div>
      <div class="topbar-controls">
        <div class="topbar-config-actions" aria-hidden="true">
          <button type="button" class="quiet-button" id="loadConfigBtn">加载配置</button>
          <button type="button" class="quiet-button" id="saveConfigBtn">保存配置</button>
        </div>
        <div class="theme-cluster" aria-label="主题切换">
          <span class="theme-symbol">☼</span>
          <button type="button" class="theme-toggle" id="themeToggleBtn" title="切换明暗主题" aria-label="切换明暗主题"></button>
          <span class="theme-symbol">☾</span>
        </div>
        <span class="topbar-separator"></span>
        <div class="status-card">
          <span class="queue-state-label">队列状态</span>
          <span class="percent" id="percentText">0%</span>
          <div class="header-progress"><div class="progress-bar" id="progressBar"></div></div>
          <span class="queue-count" id="queueCountText">0/0</span>
          <span class="state-dot" id="stateDot"></span>
          <span id="stateText" hidden>等待就绪</span>
        </div>
        <button type="button" class="topbar-icon-button" id="settingsBtn" title="打开设置中心" aria-label="打开设置中心">⚙</button>
      </div>
    </header>

    <!-- ── Left Panel ─────────────────────────────── -->
    <section class="left">
      <div class="workspace-heading" hidden>
        <div><strong>元数据工作区</strong><span>配置来源、归档规格与内容信息</span></div>
      </div>

      <form class="form-scroll" id="configForm">
        <div class="section source-section">
          <div class="section-title"><span class="section-icon">↗</span>核心来源</div>
          <label>音频文件夹路径（请选择 /data 下的专辑目录）</label>
          <div class="inline source-directory">
            <input name="input_folder" placeholder="/data/专辑目录" />
            <button type="button" class="quiet-button" id="browseBtn">浏览目录</button>
          </div>
          <div class="source-query">
            <label>平台专辑 ID / 书名 / 分享链接（可选）</label>
            <div class="source-controls">
              <select name="api_source"></select>
              <input name="api_id" placeholder="输入 ID、书名或分享链接 URL" />
              <button type="button" class="btn-primary" id="fetchBtn">⇩ 获取元数据</button>
              <button type="button" class="quiet-button" id="searchTitleBtn">⌕ 按书名搜索</button>
            </div>
          </div>
          <div id="titleSearchBackdrop" class="search-results-backdrop" hidden></div>
          <div id="titleSearchResults" class="search-results" role="dialog" aria-modal="true" hidden></div>
          <div id="authorSearchResults" class="search-results" role="dialog" aria-modal="true" hidden></div>
        </div>

        <div class="section metadata-section">
          <div class="section-title"><span class="section-icon">▤</span>元数据档案</div>
          <div class="metadata-title-grid">
            <div class="metadata-title-field"><label>专辑标题</label><input name="title" placeholder="请输入专辑标题" /></div>
            <div class="metadata-title-field"><label>副标题</label><input name="subtitle" placeholder="请输入副标题（可选）" /></div>
          </div>
          <div class="people-row">
            <div class="entity-row">
              <label>作者</label>
              <div class="chips editable" id="authorPool"></div>
              <button type="button" class="field-mini-action" id="fetchAuthorBtn">♧ 获取作者</button>
              <input type="hidden" name="author" />
            </div>
            <div class="entity-row">
              <label>演播者</label>
              <div class="chips editable" id="anchorPool"></div>
              <span class="entity-hint">输入后按回车添加</span>
              <input type="hidden" name="anchor" />
            </div>
          </div>
        </div>

        <div class="section archive-section">
          <div class="section-title"><span class="section-icon">◇</span>规格与归档</div>
          <div class="archive-main-grid">
            <div><label>发布平台 *</label><select name="platform"></select></div>
            <div><label>专辑分类 *</label><select name="category"></select></div>
            <div><label>专辑状态 *</label><select name="finished"></select></div>
            <div><label>发布年份 *</label><input name="year" placeholder="请选择或填写年份" /></div>
            <div><label>目标格式</label><select name="target_format"></select></div>
            <div><label>比特率</label><select name="bitrate"></select></div>
          </div>
          <div class="archive-extra-grid">
            <div class="team-archive">
              <label>制作团队（文件夹后缀）</label>
              <div class="chips editable team-box" id="teamPool"></div>
              <input type="hidden" name="team" />
            </div>
            <div class="series-archive">
              <label>系列档案（同一本书可加入多个系列）</label>
              <div class="series-inline">
                <div class="chips series-box" id="seriesPool"></div>
                <button type="button" class="quiet-button" id="openSeriesBtn">＋ 添加系列</button>
              </div>
              <input type="hidden" name="series_name" />
              <input type="hidden" name="series_number" />
            </div>
            <div class="tag-archive">
              <label>专辑标签池（回车添加，点击气泡删除）</label>
              <div class="chips" id="tagPool"></div>
              <input id="tagInput" placeholder="输入新标签，按回车添加..." style="margin-top:7px" />
            </div>
          </div>
        </div>

        <div class="section visual-section">
          <div class="section-title"><span class="section-icon">▧</span>视觉与内容</div>
          <div class="cover-row">
            <div class="cover-preview-column">
              <div class="cover-box">
                <img id="coverImg" alt="" />
                <span id="coverEmpty" style="font-size:12px;color:var(--text-3)">暂无封面<br/>1:1</span>
                <button type="button" class="cover-change-button" id="coverChangeBtn">▧ 更换封面</button>
              </div>
              <div class="cover-meta" id="coverMeta">--</div>
            </div>
            <div class="visual-content-column">
              <input type="hidden" name="manual_cover_path" />
              <input type="file" id="coverFileInput" accept="image/jpeg,image/png,image/webp,image/gif" hidden />
              <label>简介</label>
              <textarea name="manual_desc" placeholder="简介内容..."></textarea>
            </div>
          </div>
        </div>
      </form>

    </section>

    <!-- ── Right Panel ─────────────────────────────── -->
    <section class="right">
      <div class="queue-console">
        <div class="tabs">
          <button type="button" class="tab active" data-tab="queue">▣ 任务队列</button>
          <button type="button" class="tab" data-tab="log">▤ 处理日志</button>
          <button type="button" class="tab" data-tab="failed">△ 失败任务</button>
          <button type="button" class="tab overview-tab" data-tab="overview">▥ 数据概览</button>
        </div>

        <div class="tab-panel active" id="panel-queue">
          <div class="queue-actions">
            <button type="button" class="btn-indigo" id="editQueueBtn">✓ 编辑选中任务</button>
            <button type="button" class="btn-amber" id="removeQueueBtn">× 移除选中任务</button>
            <button type="button" class="btn-red" id="clearQueueBtn">清空全部队列</button>
          </div>
          <div class="table-wrap">
            <table>
              <thead><tr><th></th><th>#</th><th>专辑标题</th><th>平台</th><th>进度</th><th>状态</th><th>操作</th></tr></thead>
              <tbody id="queueBody"></tbody>
            </table>
          </div>
        </div>

        <div class="tab-panel" id="panel-overview"><div class="overview" id="overviewBox"></div></div>
        <div class="tab-panel" id="panel-failed">
          <div class="table-wrap">
            <table><thead><tr><th>文件</th><th>错误</th></tr></thead><tbody id="failedBody"></tbody></table>
          </div>
        </div>

        <div class="tab-panel" id="panel-log">
          <div class="live-log-head"><strong>◉ 处理日志（实时）</strong><button type="button" class="quiet-button" id="clearLogBtn">▧ 清空日志</button></div>
          <div class="log" id="logBox"></div>
        </div>

        <div class="right-commandbar">
          <div class="selection-copy"><span>已选 <b id="selectedCountText">0</b> 项</span></div>
          <div class="ha-process">
            <button type="button" class="quiet-button" id="addQueueBtn">＋ 加入队列</button>
            <button type="button" class="btn-indigo" id="startQueueBtn">▷ 开始处理</button>
            <button type="button" class="btn-red" id="stopBtn">□ 停止</button>
            <button type="button" class="quiet-button" id="clearBtn">▧ 清空编辑区</button>
          </div>
        </div>
      </div>
    </section>
  </div>

  <!-- ── Directory Modal ───────────────────────── -->
  <div class="modal-mask" id="dirModal">
    <div class="modal">
      <div class="modal-head"><strong>选择有声书专辑目录</strong><button type="button" id="closeDirBtn">× 关闭</button></div>
      <div style="padding:10px 18px;color:var(--text-3);font-size:12px;border-bottom:1px solid var(--border);font-family:monospace;flex-shrink:0" id="dirPath"></div>
      <div class="dir-list" id="dirList"></div>
      <div class="modal-foot"><button type="button" id="dirUpBtn">↑ 返回上级</button><button type="button" class="btn-primary" id="chooseDirBtn">✓ 选择此目录</button></div>
    </div>
  </div>

  <!-- ── Cookie Modal ──────────────────────────── -->
  <div class="modal-mask" id="cookieModal">
    <div class="modal">
      <div class="modal-head"><strong>设置平台 Cookie</strong><button type="button" id="closeCookieBtn">× 关闭</button></div>
      <div class="modal-body">
        <label>起点听书 Cookie</label><textarea id="qidianCookie" style="min-height:90px;margin-bottom:12px"></textarea>
        <label>网易云听书 Cookie</label><textarea id="neteaseCookie" style="min-height:90px"></textarea>
        <label>酷我听书 Cookie（可选）</label><textarea id="kuwoCookie" style="min-height:90px"></textarea>
      </div>
      <div class="modal-foot"><span class="hint">Cookie 会保存到容器配置目录</span><button type="button" class="btn-primary" id="saveCookieBtn">保存 Cookie</button></div>
    </div>
  </div>

  <!-- ── Blacklist Modal ───────────────────────── -->
  <div class="modal-mask" id="blacklistModal">
    <div class="modal">
      <div class="modal-head"><strong>标签黑名单管理</strong><button type="button" id="closeBlacklistBtn">× 关闭</button></div>
      <div class="modal-body">
        <div class="hint" id="blacklistPath" style="margin-bottom:10px"></div>
        <div class="chips" id="blacklistPool"></div>
        <input id="blacklistInput" placeholder="输入黑名单规则或正则表达式，按回车添加" style="margin-top:10px" />
      </div>
      <div class="modal-foot"><span class="hint">支持正则表达式；点击气泡可删除规则。</span><button type="button" class="btn-primary" id="saveBlacklistBtn">保存黑名单</button></div>
    </div>
  </div>

  <!-- ── Series Modal ─────────────────────────── -->
  <div class="modal-mask" id="seriesModal">
    <div class="modal compact">
      <div class="modal-head"><strong>添加系列档案</strong><button type="button" id="closeSeriesBtn">× 关闭</button></div>
      <div class="modal-body">
        <label>系列名</label><input id="seriesNameInput" placeholder="例如：庆余年" style="margin-bottom:12px" />
        <label>序号（可选）</label><input id="seriesNumberInput" placeholder="例如：1，可留空" />
      </div>
      <div class="modal-foot"><span class="hint">同一本书可以添加多个系列，序号可选。</span><button type="button" class="btn-primary" id="saveSeriesBtn">＋ 添加系列</button></div>
    </div>
  </div>

  <!-- ── Settings Center ───────────────────────── -->
  <div class="modal-mask settings-mask" id="settingsModal">
    <div class="modal settings-modal">
      <div class="modal-head settings-head">
        <div><strong>设置中心</strong><span>数据源、规则、配置与维护工具</span></div>
        <button type="button" id="closeSettingsBtn">× 关闭</button>
      </div>
      <div class="modal-body settings-body">
        <div class="settings-grid">
          <section class="settings-card appearance-card">
            <div class="settings-card-head"><span class="settings-icon">◐</span><div><strong>外观主题</strong><span>选择适合当前环境的工作台配色</span></div></div>
            <div class="theme-picker" id="themePicker">
              <button type="button" class="theme-option" data-theme-option="dark">
                <span class="theme-preview preview-dark"><i></i><i></i><i></i></span><span class="theme-option-copy"><strong>墨夜</strong><small>典雅墨蓝</small></span><span class="theme-selected">✓</span>
              </button>
              <button type="button" class="theme-option" data-theme-option="light">
                <span class="theme-preview preview-light"><i></i><i></i><i></i></span><span class="theme-option-copy"><strong>素雪</strong><small>清润月白</small></span><span class="theme-selected">✓</span>
              </button>
              <button type="button" class="theme-option" data-theme-option="linen">
                <span class="theme-preview preview-linen"><i></i><i></i><i></i></span><span class="theme-option-copy"><strong>茶白</strong><small>温润米茶</small></span><span class="theme-selected">✓</span>
              </button>
              <button type="button" class="theme-option" data-theme-option="mint">
                <span class="theme-preview preview-mint"><i></i><i></i><i></i></span><span class="theme-option-copy"><strong>青瓷</strong><small>含蓄青绿</small></span><span class="theme-selected">✓</span>
              </button>
              <button type="button" class="theme-option" data-theme-option="rose">
                <span class="theme-preview preview-rose"><i></i><i></i><i></i></span><span class="theme-option-copy"><strong>胭脂</strong><small>柔雅绯粉</small></span><span class="theme-selected">✓</span>
              </button>
              <button type="button" class="theme-option" data-theme-option="ocean">
                <span class="theme-preview preview-ocean"><i></i><i></i><i></i></span><span class="theme-option-copy"><strong>黛蓝</strong><small>沉静黛青</small></span><span class="theme-selected">✓</span>
              </button>
              <button type="button" class="theme-option" data-theme-option="aurora">
                <span class="theme-preview preview-aurora"><i></i><i></i><i></i></span><span class="theme-option-copy"><strong>暮紫</strong><small>端庄暮紫</small></span><span class="theme-selected">✓</span>
              </button>
              <button type="button" class="theme-option" data-theme-option="jade">
                <span class="theme-preview preview-jade"><i></i><i></i><i></i></span><span class="theme-option-copy"><strong>碧波</strong><small>自然青碧</small></span><span class="theme-selected">✓</span>
              </button>
              <button type="button" class="theme-option" data-theme-option="graphite">
                <span class="theme-preview preview-graphite"><i></i><i></i><i></i></span><span class="theme-option-copy"><strong>玄灰</strong><small>克制水墨</small></span><span class="theme-selected">✓</span>
              </button>
            </div>
          </section>
          <section class="settings-card">
            <div class="settings-card-head"><span class="settings-icon">◌</span><div><strong>数据源与访问</strong><span>平台凭据和 Web 访问控制</span></div></div>
            <div class="settings-actions">
              <button type="button" id="cookieBtn">平台 Cookie</button>
              <button type="button" id="webTokenBtn">访问令牌</button>
              <button type="button" id="blacklistBtn">标签黑名单</button>
            </div>
          </section>
          <section class="settings-card">
            <div class="settings-card-head"><span class="settings-icon">▣</span><div><strong>配置管理</strong><span>加载、保存、导入与导出</span></div></div>
            <div class="settings-actions">
              <button type="button" id="settingsLoadConfigBtn">加载配置</button>
              <button type="button" id="settingsSaveConfigBtn">保存配置</button>
              <button type="button" id="exportConfigBtn">导出配置</button>
              <button type="button" id="importConfigBtn">导入配置</button>
            </div>
          </section>
          <section class="settings-card">
            <div class="settings-card-head"><span class="settings-icon">◇</span><div><strong>检查与诊断</strong><span>处理预览、运行状态和质量报告</span></div></div>
            <div class="settings-actions">
              <button type="button" id="previewRunBtn">预览处理</button>
              <button type="button" id="healthBtn">健康检查</button>
              <button type="button" id="qualityBtn">质量检查</button>
            </div>
          </section>
          <section class="settings-card">
            <div class="settings-card-head"><span class="settings-icon">↻</span><div><strong>任务与恢复</strong><span>批量导入、失败重试与操作快照</span></div></div>
            <div class="settings-actions">
              <button type="button" id="batchImportBtn">批量导入目录</button>
              <button type="button" id="failedBtn">查看失败列表</button>
              <button type="button" id="retryBtn">重试失败任务</button>
              <button type="button" id="restoreSnapshotBtn">撤销目录改名</button>
              <button type="button" id="exportLogBtn">导出运行日志</button>
            </div>
          </section>
        </div>
      </div>
      <div class="modal-foot settings-foot"><span class="hint">修改平台 Cookie 后会写入容器配置目录。</span><button type="button" class="btn-primary" id="doneSettingsBtn">完成</button></div>
    </div>
  </div>

  <div class="toast" id="toast"></div>

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

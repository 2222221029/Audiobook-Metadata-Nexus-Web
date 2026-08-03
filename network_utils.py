# network_utils.py
import sys
import ssl
import re
import json
import html as html_lib
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context
import urllib3
from config import DEFAULT_DESC, NETWORK_VERIFY_SSL, PLATFORM_VERIFY_SSL

if not NETWORK_VERIFY_SSL:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def _debug_log(msg: str):
    try:
        sys.__stdout__.write(msg + "\n")
        sys.__stdout__.flush()
    except Exception:
        pass

def fix_ssl_context():
    if NETWORK_VERIFY_SSL:
        return
    try:
        ssl._create_default_https_context = ssl._create_unverified_context
    except AttributeError:
        pass

class CustomTLSAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        ctx = create_urllib3_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try: ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
        except AttributeError: pass
        try: ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        except AttributeError: pass
        try: ctx.set_ciphers('DEFAULT@SECLEVEL=1')
        except ssl.SSLError:
            try: ctx.set_ciphers('ALL:@SECLEVEL=1')
            except Exception: pass
        kwargs['ssl_context'] = ctx
        return super(CustomTLSAdapter, self).init_poolmanager(*args, **kwargs)

def get_platform_verify_ssl(platform_key=None, verify_ssl=None):
    if verify_ssl is None:
        if platform_key:
            verify_ssl = PLATFORM_VERIFY_SSL.get(platform_key, PLATFORM_VERIFY_SSL.get("default", NETWORK_VERIFY_SSL))
        else:
            verify_ssl = PLATFORM_VERIFY_SSL.get("default", NETWORK_VERIFY_SSL)
    return verify_ssl

def get_safe_session(verify_ssl=None, platform_key=None):
    verify_ssl = get_platform_verify_ssl(platform_key=platform_key, verify_ssl=verify_ssl)
    session = requests.Session()
    session.verify = verify_ssl
    adapter = HTTPAdapter(max_retries=3) if verify_ssl else CustomTLSAdapter(max_retries=3)
    session.mount('https://', adapter)
    session.mount('http://', adapter)
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive"
    })
    return session

def fetch_share_page_html(url: str, timeout: int = 15, verify_ssl=None, platform_key=None) -> str:
    verify_ssl = get_platform_verify_ssl(platform_key=platform_key, verify_ssl=verify_ssl)
    session = get_safe_session(verify_ssl=verify_ssl)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    resp = session.get(url, headers=headers, timeout=timeout, verify=verify_ssl)
    resp.raise_for_status()
    resp.encoding = resp.encoding or "utf-8"
    return resp.text

def parse_qidian_share_html(html: str, page_url: str = "") -> dict:
    out = {}
    try:
        m = re.search(r"<title>([^<]+)</title>", html, re.IGNORECASE)
        title = (m.group(1).strip() if m else "").split("|")[0].strip() if m else ""
        if title:
            out["title"] = out["name"] = title
        m = re.search(r"url\(?(https://bookcover\.yuewen\.com/qdbimg/[^)\s\"']+)\)?", html)
        cover = m.group(1) if m else ""
        if cover:
            cover = re.sub(r'/180$', '', cover)
            out["cover"] = out["bestCover"] = out["pic"] = cover
            year_match = re.search(r"/(?:19|20)\d{2}[-_/](?:0?[1-9]|1[0-2])[-_/](?:0?[1-9]|[12]\d|3[01])(?:/|$)", cover)
            if year_match:
                out["releaseDate"] = re.search(r"(?:19|20)\d{2}", year_match.group(0)).group(0)
        m = re.search(r'class="[^"]*subtitle-4[^"]*">([^<]+)<', html)
        author = (m.group(1).strip() if m else "").strip()
        if not author:
            m = re.search(r'"AuthorName"[^"]*"[^"]*"\s*,\s*"([^"]+)"', html)
            if not m: m = re.search(r'["\']AuthorName["\'][^"]*,\s*["\']([^"\']+)["\']', html)
            author = m.group(1).strip() if m else ""
        if author:
            out["announcer"] = out["artist"] = author
            out["author"] = ""
        m = re.search(r'class="[^"]*_text_1fxmt_[^"]*"[^>]*>([\s\S]*?)</div>', html)
        desc = ""
        if m:
            desc = clean_html_tags(f"<div>{m.group(1)}</div>")
        if not desc or len(desc) < 20:
            m = re.search(r'"Intro"\s*,\s*(\d+)\s*,\s*"([^"]+)"', html)
            if not m: m = re.search(r'["\']Intro["\'][^"]*["\']([^"\']{50,})["\']', html)
            if m:
                try: desc = m.group(2).replace("\\r\\n", "\n").replace("\\n", "\n").strip()
                except: desc = (m.group(1) if m.lastindex >= 1 else "") or desc
        if desc:
            out["desc"] = out["info"] = desc
        m = re.search(r'caption tc-white-alpha-80[^>]*>([^<]+)<', html)
        if m:
            tag = m.group(1).strip()
            if "完" in tag: out["finished"] = "完结"
            elif tag and tag not in ("简介",): out["category"] = tag
        if not out.get("finished") and re.search(r'"完结"|ActionStatus.*完', html):
            out["finished"] = "完结"
        if not out.get("category") and re.search(r"仙侠|玄幻|都市", html):
            for c in ("仙侠", "玄幻", "都市"):
                if c in html:
                    out["category"] = c
                    break
        return out
    except Exception as e:
        _debug_log(f"[起点分享页解析] 异常: {e}")
        return {}

def parse_fanqie_share_html(html: str, page_url: str = "") -> dict:
    out = {}
    try:
        # 新版 /ug-ssr/pages/book-share 将完整专辑数据放在路由状态中。
        # 直接解析该 JSON 比重放带签名的接口稳定，也不依赖 Chromium。
        router_match = re.search(r'window\._ROUTER_DATA\s*=\s*(\{[\s\S]*?\})\s*;?\s*</script>', html, re.IGNORECASE)
        if router_match:
            try:
                router_data = json.loads(html_lib.unescape(router_match.group(1)))
                page_data = ((((router_data.get("loaderData") or {}).get("book-share_page") or {}).get("pageData")) or {})
                book = page_data.get("api_book_info") or page_data.get("book_info") or {}
                if isinstance(book, dict):
                    title = str(book.get("book_name") or book.get("name") or book.get("title") or "").strip()
                    if title:
                        tags_raw = book.get("tags") or book.get("tag_list") or []
                        if isinstance(tags_raw, str):
                            tags = [part.strip() for part in re.split(r"[,，、|]", tags_raw) if part.strip()]
                        elif isinstance(tags_raw, list):
                            tags = [str(item.get("name") or item.get("tag_name") or "").strip() if isinstance(item, dict) else str(item).strip() for item in tags_raw]
                        else:
                            tags = []
                        cover = (
                            page_data.get("audio_thumb_uri_webp")
                            or book.get("audio_thumb_uri_webp")
                            or book.get("audio_thumb_uri")
                            or book.get("thumb_url")
                            or ""
                        )
                        creation_status = book.get("creation_status")
                        finished = "完结" if str(creation_status) == "0" else "连载" if creation_status is not None else ""
                        category = str(book.get("category_info") or book.get("genre") or (tags[0] if tags else "")).strip()
                        out = {
                            "title": title,
                            "name": title,
                            "album": title,
                            "author": str(book.get("author") or "").strip(),
                            "desc": str(book.get("abstract") or book.get("description") or "").strip(),
                            "info": str(book.get("abstract") or book.get("description") or "").strip(),
                            "cover": str(cover).strip().replace("http://", "https://"),
                            "bestCover": str(cover).strip().replace("http://", "https://"),
                            "pic": str(cover).strip().replace("http://", "https://"),
                            "category": category,
                            "finished": finished,
                            "tags": list(dict.fromkeys(tag for tag in tags if tag)),
                            "chapter_count": book.get("serial_count") or book.get("audio_serial_count") or "",
                            "score": book.get("score") or "",
                            "play_num": book.get("play_num") or "",
                            "releaseDate": str(book.get("create_time") or "")[:4],
                        }
                        return out
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                _debug_log(f"[番茄分享页 ROUTER_DATA] 解析失败，继续旧版兜底: {exc}")

        for pattern in [
            r'<script[^>]*>[\s\S]*?["\'](?:api_book_info|book_name|book_id)["\'][\s\S]*?</script>',
            r'window\.__INITIAL_STATE__\s*=\s*(\{[\s\S]*?\});',
            r'__NUXT__\s*=\s*(\{[\s\S]*?\});',
            r'"book_name"\s*:\s*"([^"]+)"',
            r'"author"\s*:\s*"([^"]+)"',
        ]:
            m = re.search(pattern, html)
            if m:
                if "book_name" in pattern and m.lastindex: out["title"] = out["name"] = m.group(1).strip()
                if "author" in pattern and m.lastindex: out["author"] = m.group(1).strip()
                if out: break
        m = re.search(r'"(https://[^"]*novelfm[^"]*\.(?:jpeg|jpg|png)[^"]*)"', html)
        if m:
            out["cover"] = out["bestCover"] = m.group(1)
        return out
    except Exception as e:
        _debug_log(f"[番茄分享页解析] 异常: {e}")
        return {}

def clean_html_tags(text: str) -> str:
    if not text: return DEFAULT_DESC
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</p>', '\n\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<div.*?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</div>', '\n', text, flags=re.I)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\n\s+', '\n', text)
    text = re.sub(r'\n+', '\n', text).strip()
    text = filter_ad_content(text)
    return text if text else DEFAULT_DESC

def filter_ad_content(text: str) -> str:
    if not text: return ""
    ad_patterns = [
        r'上新福利[\s\S]*?(?=\n\n|$)', r'【评论有礼】[\s\S]*?(?=\n\n|【|$)', r'【播放有礼】[\s\S]*?(?=\n\n|【|$)',
        r'【*购买须知[\s\S]+$', r'订阅\+推荐\+\d+字以上[\s\S]*?(?=\n\n|$)', r'专辑好评满\d+[\s\S]*?(?=\n\n|$)',
        r'播放量破\d+w[\s\S]*?(?=\n\n|$)', r'抽\d+位用户[\s\S]*?(?=\n\n|$)', r'喜马月卡[\s\S]*?(?=\n\n|$)',
        r'深夜小茶馆周边[\s\S]*?(?=\n\n|$)', r'即日起截至[\d月日]+[\s\S]*?(?=\n\n|$)',
    ]
    for pattern in ad_patterns:
        text = re.sub(pattern, '', text, flags=re.DOTALL).strip()
    text = re.sub(r'\n+', '\n', text).strip()
    return text

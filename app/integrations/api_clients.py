# api_clients.py
import os
import re
import json
import time
import uuid
import codecs
import html
from functools import lru_cache
from urllib.parse import urlsplit, urlunsplit
import requests
from app.core.config import get_platform_cookies, FANQIE_SHARE_ID, FANQIE_X_BOGUS, FANQIE_SIGNATURE
from app.integrations.network_utils import get_safe_session, _debug_log, clean_html_tags, extract_bytedance_snowflake_year

def ximalaya_api(endpoint: str, id: str) -> dict:
    id_match = re.search(r"\d+", str(id or ""))
    id = id_match.group(0) if id_match else str(id or "").strip()
    urls = {
        "album": f"https://www.ximalaya.com/revision/album/v1/simple?albumId={id}",
        "anchor": f"https://www.ximalaya.com/revision/user/basic?uid={id}"
    }
    try:
        session = get_safe_session()
        headers = {"Referer": "https://www.ximalaya.com/", "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        cookie = os.environ.get("XIMALAYA_COOKIE", "").strip()
        if cookie:
            headers["Cookie"] = cookie
        resp = session.get(urls[endpoint], headers=headers, timeout=10)
        if resp.status_code != 200: raise Exception(f"API请求失败，状态码：{resp.status_code}")
        return resp.json().get("data", {})
    except Exception as e:
        raise Exception(f"喜马拉雅API请求失败：{str(e)}")

def lanren_api(book_id: str) -> dict:
    url = f"https://m.lrts.me/ajax/getBookDetail?bookId={book_id}"
    try:
        session = get_safe_session()
        resp = session.get(url, headers={"Referer": "https://m.lrts.me/"}, timeout=15)
        if resp.status_code != 200: raise Exception(f"懒人听书API请求失败，状态码：{resp.status_code}")
        data = resp.json()
        if data.get("status", 0) != 0: raise Exception(f"API返回错误：{data.get('msg', '未知错误')}")
        book_info = {}
        if "name" in data and "author" in data: book_info = data
        else:
            core_data = data.get("data", {})
            if isinstance(core_data, dict):
                if "name" in core_data: book_info = core_data
                else:
                    for key, val in core_data.items():
                        if isinstance(val, dict) and "name" in val:
                            book_info = val
                            break
        if not book_info: raise Exception("API返回数据中未找到书籍核心信息")
        title = book_info.get("name") or book_info.get("bookName") or ""
        author = book_info.get("author", "")
        announcer = book_info.get("announcer", "")
        desc = book_info.get("desc", "")
        cover = book_info.get("cover") or book_info.get("bestCover", "")
        category = book_info.get("type", "")
        tags = [category] if category else []
        release_date = get_lanren_year(book_id)
        return {
            "name": title, "title": title, "album": title, "author": author, "announcer": announcer, "artist": announcer,
            "desc": desc, "info": desc, "cover": cover, "bestCover": cover, "pic": cover, "category": category,
            "tags": tags, "releaseDate": release_date
        }
    except Exception as e: raise Exception(f"懒人听书API解析异常：{str(e)}")

def get_lanren_year(book_id: str) -> str:
    try:
        headers = {"Referer": f"https://www.lrts.me/book/{book_id}"}
        session = get_safe_session()
        url_pc_menu = f"https://www.lrts.me/ajax/book/menu?bookId={book_id}&pageNum=1&pageSize=50&sortType=0"
        try:
            resp = session.get(url_pc_menu, headers=headers, timeout=5)
            if resp.status_code == 200:
                matches = re.findall(r'更新时间[:：]?\s*(\d{4})-\d{2}-\d{2}', resp.text)
                if matches: return matches[0]
        except Exception as e:
            _debug_log(f"[懒人听书年份接口] PC 菜单请求失败: {e}")
        url_m_menu = f"https://m.lrts.me/ajax/getBookMenu?bookId={book_id}&pageNum=1&pageSize=50&sortType=0"
        try:
            resp = session.get(url_m_menu, headers=headers, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                items = []
                core_data = data.get("data", {})
                if isinstance(core_data, dict) and "list" in core_data: items = core_data["list"]
                elif isinstance(core_data, list): items = core_data
                elif isinstance(core_data, dict) and "data" in core_data and isinstance(core_data["data"], list): items = core_data["data"]
                if items and len(items) > 0:
                    time_val = items[0].get("createTime") or items[0].get("updateTime")
                    if time_val:
                        if isinstance(time_val, (int, float)):
                            if time_val > 9999999999: time_val /= 1000
                            return str(int(time.strftime("%Y", time.localtime(time_val))))
                        elif isinstance(time_val, str):
                            m = re.search(r'^(\d{4})', time_val)
                            if m: return m.group(1)
        except Exception as e:
            _debug_log(f"[懒人听书年份接口] 移动端菜单请求失败: {e}")
        url_pc_detail = f"https://www.lrts.me/book/{book_id}"
        try:
            resp = session.get(url_pc_detail, headers=headers, timeout=5)
            if resp.status_code == 200:
                matches = re.findall(r'更新时间[:：]?\s*(\d{4})-\d{2}-\d{2}', resp.text)
                if matches: return matches[0]
        except Exception as e:
            _debug_log(f"[懒人听书年份接口] PC 详情请求失败: {e}")
    except Exception as e:
        _debug_log(f"[懒人听书年份接口] 解析失败: {e}")
    return ""

def get_kuwo_album_desc_from_page(album_id: str) -> str:
    url = "https://www.kuwo.cn/album_detail/{}".format(album_id.strip())
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.kuwo.cn/"}
    try:
        session = get_safe_session()
        resp = session.get(url, headers=headers, timeout=15)
        if resp.status_code != 200: return ""
        html = resp.text
    except Exception: return ""
    try:
        nuxt_start = html.find("window.__NUXT__")
        if nuxt_start == -1: nuxt_start = html.find('window["__NUXT__"]')
        if nuxt_start == -1: return ""
        script_end = html.find("</script>", nuxt_start)
        block = html[nuxt_start:script_end if script_end != -1 else len(html)]
        candidates = re.findall(r'"((?:[^"\\]|\\.|\\u[0-9a-fA-F]{4})*)"', block)
        best = ""
        for s in candidates:
            try: u = codecs.decode(s, "unicode_escape")
            except Exception: u = s
            if 50 <= len(u) <= 5000 and ("。" in u or "，" in u):
                if len(u) > len(best): best = u
        if best: return best.strip()
    except: pass
    try:
        m = re.search(r'<p\s+class="intr_txt"[^>]*>\s*<span[^>]*>([^<]+)', html)
        if m:
            desc = m.group(1).strip().replace("...", "").strip()
            if len(desc) >= 10: return desc
    except: pass
    return ""

def get_kuwo_album_info(album_id: str, pn=1, rn=24) -> dict:
    kuwo_cookie = os.environ.get("KUWO_COOKIE", "").strip() or get_platform_cookies().get("kuwo", "").strip()
    cookies = {"Hm_Iuvt_cdb524f42f23cer9b268564v7y735ewrq2324": kuwo_cookie} if kuwo_cookie else {}
    req_id = str(uuid.uuid4()).replace("-", "")
    url = "https://www.kuwo.cn/api/www/album/albumInfo"
    params = {"albumId": album_id, "pn": pn, "rn": rn, "reqId": req_id, "httpsStatus": 1, "plat": "web_www", "from": "", "_": int(time.time() * 1000)}
    headers = {
        "Referer": f"https://www.kuwo.cn/album_detail/{album_id}",
        "User-Agent": "Mozilla/5.0",
        "Secret": "7363e89561110e6cb657c2fb7cedc85451a49cad02a8ce4d6bc236dce7ed52ce0144c917",
    }
    try:
        session = get_safe_session()
        session.cookies.update(cookies)
        resp = session.get(url=url, params=params, headers=headers, timeout=15)
        if resp.status_code == 200:
            res = resp.json()
            if res.get("success") or res.get("code") == 200: return res
    except: pass
    return None

def _normalize_kuwo_cover_url(value: str) -> str:
    cover = str(value or "").strip()
    if not cover:
        return ""
    if not cover.startswith(("http://", "https://")):
        cover = "https://img2.kuwo.cn/star/albumcover/" + cover.lstrip("/")
    cover = cover.replace("http://", "https://")
    return re.sub(r"/albumcover/(?:\d+)/", "/albumcover/5000/", cover)


def _extract_kuwo_year(value) -> str:
    if isinstance(value, dict):
        for key in ("releaseDate", "release_date", "pub", "pubdate", "showtime", "timing_online", "publishTime", "publish_date", "date"):
            match = re.search(r"(?:19|20)\d{2}", str(value.get(key) or ""))
            if match:
                return match.group(0)
        for child in value.values():
            year = _extract_kuwo_year(child)
            if year:
                return year
    elif isinstance(value, list):
        for child in value:
            year = _extract_kuwo_year(child)
            if year:
                return year
    return ""


def _kuwo_search_album_by_id(album_id: str) -> dict:
    wanted = str(album_id or "").strip()
    for endpoint in ("http://search.kuwo.cn/r.s", "https://search.kuwo.cn/r.s"):
        try:
            response = get_safe_session().get(endpoint, params={"pn": 0, "rn": 30, "all": wanted, "ft": "album", "newsearch": 1, "rformat": "json", "encoding": "utf8", "plat": "pc", "pcjson": 1}, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
            if response.status_code != 200:
                continue
            for album in (response.json().get("albumlist") or []):
                if str(album.get("albumid") or album.get("id") or "").strip() != wanted:
                    continue
                cover = _normalize_kuwo_cover_url(album.get("hts_img") or album.get("img") or album.get("pic") or "")
                tags = [tag for tag in (album.get("startype"), album.get("fartist"), album.get("artist")) if tag]
                category = str(album.get("startype") or "").strip()
                finished = "完结" if str(album.get("finished")) in {"1", "true", "True"} else "连载" if album.get("finished") else ""
                return {"album": album.get("name") or album.get("title") or "", "pic": cover, "artist": album.get("artist") or album.get("aartist") or "", "info": album.get("info") or "", "releaseDate": _extract_kuwo_year(album), "tags": list(dict.fromkeys(tags)), "category": category, "finished": finished, "chapter_count": album.get("musiccnt") or ""}
        except Exception:
            continue
    return {}


def kuwo_api(album_id: str) -> dict:
    search_data = _kuwo_search_album_by_id(album_id)
    if search_data:
        return search_data
    # Prefer the plugin's stable album detail endpoint. Some search results
    # from later pages are not accepted by the newer web album endpoint.
    try:
        session = get_safe_session()
        response = session.get(
            "https://datacenter.kuwo.cn/d.c",
            params={"cmd": "query", "ft": "album", "ids": str(album_id).strip(), "resenc": "utf8", "cmkey": "plist_album"},
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.kuwo.cn/"},
            timeout=15,
        )
        if response.status_code == 200:
            albums = response.json()
            album = albums[0] if isinstance(albums, list) and albums else {}
            if isinstance(album, dict) and (album.get("name") or album.get("album")):
                name = album.get("name") or album.get("album") or album.get("title") or ""
                cover = _normalize_kuwo_cover_url(album.get("pic") or album.get("hts_img") or album.get("img") or "")
                artist = album.get("artist") or album.get("aartist") or album.get("author") or ""
                info = album.get("intro") or album.get("info") or album.get("albuminfo") or ""
                release = _extract_kuwo_year(album)
                return {"album": name, "pic": cover, "artist": artist, "info": info, "releaseDate": release}
    except Exception:
        pass
    # Kuwo's search endpoint still returns complete album records even when
    # both detail endpoints reject the request. Resolve the selected ID there.
    try:
        session = get_safe_session()
        response = session.get(
            "http://search.kuwo.cn/r.s",
            params={"pn": 0, "rn": 30, "all": str(album_id).strip(), "ft": "album", "newsearch": 1, "rformat": "json", "encoding": "utf8", "plat": "pc", "pcjson": 1},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        if response.status_code == 200:
            for album in (response.json().get("albumlist") or []):
                if str(album.get("albumid") or album.get("id") or "").strip() != str(album_id).strip():
                    continue
                cover = _normalize_kuwo_cover_url(album.get("hts_img") or album.get("img") or album.get("pic") or "")
                return {
                    "album": album.get("name") or album.get("title") or "",
                    "pic": cover,
                    "artist": album.get("artist") or album.get("aartist") or "",
                    "info": album.get("info") or "",
                    "releaseDate": _extract_kuwo_year(album),
                }
    except Exception:
        pass
    raw = get_kuwo_album_info(album_id, pn=1, rn=1)
    if not raw: raise Exception("酷我听书API请求失败或无数据返回")
    data = raw.get("data") or {}
    album_obj = data.get("album")
    if isinstance(album_obj, dict):
        name = album_obj.get("name") or album_obj.get("album") or album_obj.get("title") or ""
        pic = album_obj.get("pic") or album_obj.get("cover") or album_obj.get("albumpic") or ""
        artist = album_obj.get("artist") or album_obj.get("author") or ""
        info = (album_obj.get("info") or album_obj.get("albuminfo") or album_obj.get("description") or "").strip()
        release_date = _extract_kuwo_year(album_obj)
    else:
        name = data.get("album") or data.get("name") or ""
        pic = data.get("pic") or data.get("cover") or ""
        artist = data.get("artist") or data.get("author") or ""
        info = (data.get("info") or data.get("albuminfo") or data.get("description") or "").strip()
        release_date = _extract_kuwo_year(data)

    if not info and album_id:
        page_desc = get_kuwo_album_desc_from_page(album_id)
        if page_desc: info = page_desc
    if pic: pic = _normalize_kuwo_cover_url(pic)
    return {"album": name, "pic": pic, "artist": artist, "info": info, "releaseDate": release_date}

def parse_novelfm_share_response(data: dict) -> dict:
    if not data or data.get("code") != 0: return {}
    inner = data.get("data") or {}
    api_book = inner.get("api_book_info")
    if not api_book or not isinstance(api_book, dict): return {}
    title = (api_book.get("book_name") or api_book.get("title") or "").strip()
    if not title: return {}
    author = (api_book.get("author") or "").strip()
    cover = api_book.get("audio_thumb_url_hd") or api_book.get("audio_thumb_uri") or api_book.get("audio_thumb_uri_webp") or api_book.get("thumb_url") or ""
    desc = (api_book.get("abstract") or "").strip()
    tags_str = api_book.get("tags") or ""
    tags = [t.strip() for t in tags_str.split(",") if t.strip()] if isinstance(tags_str, str) else []
    creation_status = api_book.get("creation_status")
    finished = "完结" if str(creation_status) == "0" else "连载" if creation_status is not None else ""
    serial_count = api_book.get("serial_count") or ""
    try: chapter_count = int(serial_count) if serial_count else 0
    except: chapter_count = 0
    category = (api_book.get("category_info") or api_book.get("genre") or "").strip()
    if not category and tags: category = tags[0]
    create_time = (api_book.get("create_time") or "").strip()
    release_date = create_time[:4] if len(create_time) >= 4 else ""
    if not re.fullmatch(r"(?:19|20)\d{2}", release_date):
        release_date = extract_bytedance_snowflake_year(api_book.get("book_id") or api_book.get("id"))
    return {"name": title, "title": title, "album": title, "bestCover": cover, "cover": cover, "pic": cover, "author": author, "announcer": "", "artist": "", "desc": desc, "info": desc, "releaseDate": release_date, "category": category, "finished": finished, "tags": tags, "chapter_count": chapter_count}

def _fanqie_get_share_info(book_id: str) -> dict:
    url = "https://api5-sinfonlineb.novelfm.com/novelfm/playerapi/share/get_info/v1/"
    params = {
        "book_id": str(book_id).strip(), "share_info_type": "5", "source_channel": "link", "object_id": "", "msToken": "",
        "device_platform": "android", "os": "android", "aid": "3040", "app_name": "novel_fm", "version_code": "608",
        "device_id": "3942194090368537", "iid": "1109875180222825", "_rticket": str(int(time.time() * 1000)),
    }
    try:
        sid = (FANQIE_SHARE_ID and str(FANQIE_SHARE_ID).strip()) or ""
        xb_cfg = (FANQIE_X_BOGUS and str(FANQIE_X_BOGUS).strip()) or ""
        sig_cfg = (FANQIE_SIGNATURE and str(FANQIE_SIGNATURE).strip()) or ""
        if sid: params["share_id"] = sid
        if not xb_cfg or not sig_cfg:
            try:
                from app.integrations.fanqie_signature import generate_for_share_get_info
                ua = "com.xs.fm/608 (Linux; U; Android 9; zh_CN; 2210132C; Build/PQ3A.190605.07021633;tt-ok/3.12.13.17)"
                xb_gen, sig_gen = generate_for_share_get_info(url, dict(params), ua)
                if xb_gen and not xb_cfg: params["X-Bogus"] = xb_cfg = xb_gen
                if sig_gen and not sig_cfg: params["_signature"] = sig_cfg = sig_gen
            except: pass
        if xb_cfg and "X-Bogus" not in params: params["X-Bogus"] = xb_cfg
        if sig_cfg and "_signature" not in params: params["_signature"] = sig_cfg
    except NameError: pass
    headers = {"User-Agent": "com.xs.fm/608", "Accept": "application/json", "Referer": "https://novelfm.com/"}
    try:
        session = get_safe_session()
        resp = session.get(url, params=params, headers=headers, timeout=12, verify=False)
        if resp.status_code != 200: return {}
        return parse_novelfm_share_response(resp.json())
    except Exception as e: return {}

_FANQIE_APP_UA = "com.xs.fm/608 (Linux; U; Android 9; zh_CN; 2210132C; Build/PQ3A.190605.07021633;tt-ok/3.12.13.17)"
_FANQIE_SEARCH_URL = "https://api5-sinfonlinec.novelfm.com/novelfm/bookmall/search/page/v1/"
_FANQIE_SEARCH_PARAMS = {
    "device_platform": "android",
    "os": "android",
    "aid": "3040",
    "app_name": "novel_fm",
    "version_code": "608",
    "device_id": "3942194090368537",
    "iid": "1109875180222825",
}


def fanqie_cover_url(data):
    data = dict(data or {})
    for key in (
        "audio_thumb_url_hd",
        "audio_thumb_uri",
        "audio_thumb_url",
        "audio_thumb_uri_webp",
        "thumb_url",
        "horiz_thumb_url",
        "cover",
        "cover_url",
        "image_url",
    ):
        value = data.get(key)
        if isinstance(value, dict):
            value = value.get("url") or value.get("uri") or (value.get("url_list") or [""])[0]
        if isinstance(value, list):
            value = value[0] if value else ""
        if value:
            url = str(value).strip()
            if url.startswith("//"):
                url = "https:" + url
            try:
                parsed = urlsplit(url)
                host = parsed.hostname or ""
                if "novelfmpic.com" in host and "-sign." in host:
                    host = host.replace("-sign.", ".", 1)
                    path = parsed.path
                    path = re.sub(
                        r"~tplv-y3bzr8ilui-(?:smart-)?resize:\d+:\d+(?:\.\w+)?",
                        "~tplv-y3bzr8ilui-resize:1080:1080.jpeg",
                        path,
                    )
                    url = urlunsplit(("https", host, path, "", ""))
            except Exception:
                pass
            return url.replace("http://", "https://")
    return ""


def fanqie_release_year(data):
    data = dict(data or {})
    for key in (
        "publish_time",
        "published_time",
        "first_publish_time",
        "release_time",
        "create_time",
        "created_at",
        "update_time",
    ):
        value = data.get(key)
        if value in (None, "", 0, "0"):
            continue
        text = str(value)
        match = re.search(r"(19|20)\d{2}", text)
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


def _merge_fanqie_metadata_entries(base, extra):
    base = dict(base or {})
    extra = dict(extra or {})
    for key in (
        "name",
        "title",
        "album",
        "bestCover",
        "cover",
        "pic",
        "author",
        "announcer",
        "artist",
        "desc",
        "info",
        "releaseDate",
        "category",
        "finished",
        "chapter_count",
    ):
        if not base.get(key) and extra.get(key):
            base[key] = extra[key]
    tags = list(base.get("tags") or [])
    for tag in extra.get("tags") or []:
        tag = str(tag or "").strip()
        if tag and tag not in tags:
            tags.append(tag)
    if tags:
        base["tags"] = tags
    return base


def _fanqie_finished_status(book):
    creation_status = book.get("creation_status")
    if creation_status is not None:
        return "完结" if str(creation_status) == "0" else "连载"
    serial_status = book.get("serial_status")
    if serial_status:
        text = str(serial_status)
        return "完结" if "完" in text else "连载"
    return ""


def _fanqie_parse_search_book(book):
    book = dict(book or {})
    item_id = str(book.get("book_id") or book.get("id") or "").strip()
    title = str(book.get("book_name") or book.get("title") or book.get("name") or "").strip()
    tags = []
    tags_raw = book.get("tags")
    if tags_raw is None:
        tags_raw = book.get("tag_list") or book.get("labels") or []
    if isinstance(tags_raw, list):
        for tag in tags_raw:
            if isinstance(tag, dict):
                name = str(tag.get("tag_name") or tag.get("name") or tag.get("tagName") or tag.get("value") or "").strip()
            else:
                name = str(tag).strip()
            if name and name not in tags:
                tags.append(name)
    elif isinstance(tags_raw, str) and tags_raw:
        for part in re.split(r"[,，;；|]", tags_raw):
            part = part.strip()
            if part and part not in tags:
                tags.append(part)
    tag_name = str(book.get("tag_name") or "").strip()
    if tag_name and tag_name not in tags:
        tags.append(tag_name)
    chapter_count = book.get("chapter_number") or book.get("chapter_count") or book.get("serial_count") or ""
    try:
        chapter_count = int(chapter_count)
    except Exception:
        chapter_count = 0
    cover = fanqie_cover_url(book)
    release_date = fanqie_release_year(book) or extract_bytedance_snowflake_year(book.get("book_id") or book.get("id") or "")
    return {
        "id": item_id,
        "title": title,
        "author": str(book.get("author") or "").strip(),
        "narrator": str(book.get("anchor") or book.get("narrator") or "").strip(),
        "cover": cover,
        "desc": str(book.get("abstract") or book.get("description") or book.get("desc") or "").strip(),
        "tags": tags,
        "category": str(book.get("category") or book.get("category_name") or book.get("categoryName") or "").strip(),
        "finished": _fanqie_finished_status(book),
        "chapter_count": chapter_count,
        "word_count": book.get("word_count") or "",
        "release_date": release_date,
    }


def _fanqie_search_books(keyword: str, page: int = 1, limit: int = 12) -> list:
    keyword = str(keyword or "").strip()
    if not keyword:
        return []
    try:
        limit = max(1, int(limit or 12))
        page = max(1, int(page or 1))
        params = dict(_FANQIE_SEARCH_PARAMS)
        params["_rticket"] = str(int(time.time() * 1000))
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": _FANQIE_APP_UA,
        }
        session = get_safe_session()
        response = session.post(
            _FANQIE_SEARCH_URL,
            params=params,
            headers=headers,
            json={"query": keyword, "limit": limit, "offset": (page - 1) * limit},
            timeout=12,
            verify=False,
        )
        if response.status_code != 200:
            return []
        payload = response.json()
        data = payload.get("data") or {}
        search_data = data.get("search_data") or data.get("searchData") or data.get("books") or []
        if isinstance(search_data, dict):
            search_data = search_data.get("books") or list(search_data.values())
        results = []
        seen = set()

        def add_book(book):
            if not isinstance(book, dict):
                return
            item = _fanqie_parse_search_book(book)
            if not item["id"] or not item["title"] or item["id"] in seen:
                return
            seen.add(item["id"])
            results.append(item)

        for group in search_data:
            if isinstance(group, dict):
                if isinstance(group.get("books"), list):
                    for book in group["books"]:
                        add_book(book)
                elif group.get("book_id") or group.get("id"):
                    add_book(group)
            elif isinstance(group, list):
                for book in group:
                    add_book(book)
        return results[:limit]
    except Exception:
        return []


def _fanqie_search_by_id(book_id: str) -> dict:
    want_id = str(book_id or "").strip()
    if not want_id:
        return {}
    for item in _fanqie_search_books(want_id, page=1, limit=30):
        if item["id"] != want_id:
            continue
        return {
            "title": item["title"],
            "author": item["author"],
            "cover": item["cover"],
            "desc": item["desc"],
            "category": item["category"],
            "finished": item["finished"],
            "tags": item["tags"],
            "announcer": item["narrator"],
            "chapter_count": item["chapter_count"],
            "releaseDate": item["release_date"],
        }
    return {}

def _fanqie_plugin_detail(book_id: str) -> dict:
    session = get_safe_session()
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36", "Accept": "application/json, text/plain, */*", "Referer": "https://fanqienovel.com/"}
    endpoints = [
        ("https://fanqienovel.com/api/reader/book/detail", {"bookId": book_id}),
        ("https://fanqienovel.com/api/book/detail", {"book_id": book_id}),
        ("https://novel.snssdk.com/api/novel/book/detail/v1/", {"book_id": book_id}),
        ("https://api5-normal-lf.fqnovel.com/reading/bookapi/detail/v/", {"book_id": book_id, "aid": "1967", "iid": "0"}),
    ]
    result = {}
    for url, params in endpoints:
        try:
            response = session.get(url, params=params, headers=headers, timeout=15)
            data = response.json().get("data") or {}
            title = str(data.get("book_name") or data.get("title") or data.get("name") or "").strip()
            if not title:
                continue
            tags_value = data.get("tags") or data.get("tag_list") or data.get("labels") or data.get("tag_name") or []
            if isinstance(tags_value, str):
                tags = [part.strip() for part in re.split(r"[,，|]", tags_value) if part.strip()]
            elif isinstance(tags_value, list):
                tags = [str(item.get("name") or item.get("tag_name") or item.get("tagName") or item.get("value") or "").strip() if isinstance(item, dict) else str(item).strip() for item in tags_value]
            else:
                tags = []
            cover = fanqie_cover_url(data)
            release_date = fanqie_release_year(data) or extract_bytedance_snowflake_year(data.get("book_id") or data.get("id") or book_id)
            result = {"name": title, "title": title, "album": title, "bestCover": cover, "cover": cover, "pic": cover, "author": str(data.get("author") or data.get("author_name") or "").strip(), "announcer": str(data.get("anchor") or data.get("narrator") or "").strip(), "artist": str(data.get("anchor") or data.get("narrator") or "").strip(), "desc": str(data.get("abstract") or data.get("description") or data.get("desc") or "").strip(), "info": str(data.get("abstract") or data.get("description") or data.get("desc") or "").strip(), "releaseDate": release_date, "category": str(data.get("category") or data.get("category_name") or "").strip(), "finished": "完结" if str(data.get("creation_status")) == "0" else "连载" if data.get("creation_status") is not None else "", "tags": list(dict.fromkeys(tag for tag in tags if tag))}
            break
        except Exception:
            continue
    if not result:
        raise ValueError("番茄详情接口未返回有效数据")
    try:
        response = session.get("https://fanqienovel.com/api/reader/directory/detail", params={"bookId": book_id}, headers=headers, timeout=15)
        data = response.json().get("data") or {}
        count = 0
        for volume in data.get("chapterListWithVolume") or []:
            count += len(volume) if isinstance(volume, list) else len(volume.get("chapterList") or []) if isinstance(volume, dict) else 0
        result["chapter_count"] = count
    except Exception:
        pass
    return result


def fanqie_api(album_id: str) -> dict:
    try:
        try:
            album_id = str(album_id).strip()
            detail = _fanqie_plugin_detail(album_id)
            if detail.get("title"):
                result = _merge_fanqie_metadata_entries(detail, _fanqie_get_share_info(album_id))
                result = _merge_fanqie_metadata_entries(result, _fanqie_search_by_id(album_id))
                if not result.get("title"):
                    result["title"] = result["name"] = result["album"] = f"书籍ID_{album_id}"
                return result
        except Exception:
            pass
        share_info = _fanqie_get_share_info(album_id)

        session = get_safe_session()
        title, author, cover, desc, announcer, category, finished, tags, chapter_count, release_date = "", "", "", "", "", "", "", [], 0, ""
        detail_urls = [
            ("https://fanqienovel.com/api/reader/book/detail", {"bookId": album_id}),
            ("https://fanqienovel.com/api/book/detail", {"book_id": album_id}),
            ("https://novel.snssdk.com/api/novel/book/detail/v1/", {"book_id": album_id}),
            ("https://api5-normal-lf.fqnovel.com/reading/bookapi/detail/v/", {"book_id": album_id, "aid": "1967", "iid": "1"}),
        ]
        for url, params in detail_urls:
            try:
                resp = session.get(url, params=params, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://fanqienovel.com/"}, timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                    if str(data.get("code")) == "0" or data.get("code") == 0:
                        book_data = data.get("data") or {}
                        t = (book_data.get("book_name") or book_data.get("title") or book_data.get("name") or "").strip()
                        if t:
                            title, author = t, (book_data.get("author") or book_data.get("author_name") or "").strip() or author
                            cover, desc = fanqie_cover_url(book_data) or cover, (book_data.get("abstract") or book_data.get("desc") or "").strip() or desc
                            category, announcer = (book_data.get("category") or book_data.get("category_name") or "").strip() or category, (book_data.get("anchor") or book_data.get("narrator") or "").strip() or announcer
                            tags_raw = book_data.get("tags")
                            if isinstance(tags_raw, str):
                                parsed_tags = [part.strip() for part in re.split(r"[,，|]", tags_raw) if part.strip()]
                            elif isinstance(tags_raw, list):
                                parsed_tags = [str(item.get("name") or item.get("tag_name") or item.get("tagName") or "").strip() if isinstance(item, dict) else str(item).strip() for item in tags_raw]
                            else:
                                parsed_tags = []
                            for tag in parsed_tags:
                                if tag and tag not in tags:
                                    tags.append(tag)
                            cs = book_data.get("creation_status")
                            if cs is not None and not finished: finished = "完结" if str(cs) == "0" else "连载"
                            release_date = fanqie_release_year(book_data) or extract_bytedance_snowflake_year(book_data.get("book_id") or book_data.get("id") or album_id) or release_date
                            break
            except: pass
        try:
            resp = session.get("https://fanqienovel.com/api/reader/directory/detail", params={"bookId": album_id}, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
            if resp.status_code == 200 and resp.json().get("code") == 0:
                inner = resp.json().get("data") or {}
                if "chapterListWithVolume" in inner:
                    for vol in inner["chapterListWithVolume"]:
                        if isinstance(vol, list): chapter_count += len(vol)
                        elif isinstance(vol, dict) and "chapterList" in vol: chapter_count += len(vol["chapterList"])
        except: pass
        if not title:
            if share_info and share_info.get("name"):
                title = share_info.get("name") or title
                author = share_info.get("author") or author
                cover = share_info.get("cover") or share_info.get("bestCover") or cover
                desc = share_info.get("desc") or share_info.get("info") or desc
                category = share_info.get("category") or category
                finished = share_info.get("finished") or finished
                tags = share_info.get("tags") or tags
                release_date = share_info.get("releaseDate") or release_date
        hit = _fanqie_search_by_id(album_id)
        if hit:
            title = hit.get("title") or title
            author = hit.get("author") or author
            cover = hit.get("cover") or cover
            desc = hit.get("desc") or desc
            category = hit.get("category") or category
            finished = hit.get("finished") or finished
            announcer = hit.get("announcer") or announcer
            chapter_count = hit.get("chapter_count") or chapter_count
            release_date = hit.get("releaseDate") or release_date
            for tag in hit.get("tags", []) or []:
                if tag and tag not in tags:
                    tags.append(tag)
        if not title: title = f"书籍ID_{album_id}"
        if chapter_count and not desc: desc = f"番茄畅听有声书，共{chapter_count}集。"
        return {"name": title, "title": title, "album": title, "bestCover": cover, "cover": cover, "pic": cover, "author": author, "announcer": announcer, "artist": announcer, "desc": desc, "info": desc, "releaseDate": release_date, "category": category, "finished": finished, "tags": tags, "chapter_count": chapter_count}
    except Exception as e: raise Exception(f"番茄畅听API异常：{str(e)}")

def _qidian_getshare(book_id: str, cookie_str: str | None = None) -> dict | None:
    try:
        if cookie_str is None: cookie_str = os.environ.get("QIDIAN_COOKIE", "").strip() or get_platform_cookies().get("qidian", "").strip()
        bid = book_id.strip()
        if not bid: raise Exception("bookId 为空")
        if not cookie_str: raise Exception("未检测到起点 Cookie")
        url = f"https://magev6.if.qidian.com/argus/api/v2/bookdetail/getshare?bookId={bid}&shareType=&shareUserId=&noteContent="
        referer = f"https://magev6.if.qidian.com/h5/share/book?channel=qidianapp&ex1={bid}&bookId={bid}"
        headers = {"Referer": referer, "User-Agent": "Mozilla/5.0", "Cookie": cookie_str, "Accept": "*/*"}
        resp = requests.get(url, headers=headers, timeout=15)
        raw = resp.text if resp.status_code == 200 else None
        if not raw: raise Exception("getshare 无响应")
        data = json.loads(raw)
        if data.get("Result") is not None and str(data.get("Result")) != "0": raise Exception(f"getshare 返回：{data.get('Message', '未知')}")
        book_info = (data.get("Data") or {}).get("BookInfo") if isinstance(data.get("Data"), dict) else data.get("BookInfo") or {}
        title = (book_info.get("BookName") or book_info.get("bookName") or "").strip()
        if not title: raise Exception("无书名数据")
        author = (book_info.get("AuthorName") or book_info.get("authorName") or "").strip()
        desc = (book_info.get("Description") or book_info.get("description") or "").strip()
        bid_cover = str(book_info.get("BookId") or book_info.get("bookId") or bid).strip()
        cover = f"https://bookcover.yuewen.com/qdbimg/349573/{bid_cover}" if bid_cover else ""
        category = (book_info.get("CategoryName") or book_info.get("categoryName") or "").strip()
        action_status = (book_info.get("ActionStatus") or book_info.get("actionStatus") or "").strip()
        finished = "完结" if action_status and "完" in action_status else "连载" if action_status else ""
        return {"name": title, "title": title, "album": title, "bestCover": cover, "cover": cover, "pic": cover, "author": author, "announcer": "", "artist": "", "desc": desc, "info": desc, "releaseDate": "", "category": category, "finished": finished}
    except Exception as e: raise Exception(f"getshare 请求异常：{e}")

def parse_qidian_getshare_json(json_str: str) -> dict | None:
    try:
        raw = (json_str or "").strip()
        if not raw: return None
        data = json.loads(raw)
        if data.get("Result") != 0: return None
        book_info = (data.get("Data") or {}).get("BookInfo") if isinstance(data.get("Data"), dict) else data.get("BookInfo") or {}
        title = (book_info.get("BookName") or book_info.get("bookName") or "").strip()
        if not title: return None
        author = (book_info.get("AuthorName") or book_info.get("authorName") or "").strip()
        desc = (book_info.get("Description") or book_info.get("description") or "").strip()
        bid = str(book_info.get("BookId") or book_info.get("bookId") or "").strip()
        cover = f"https://bookcover.yuewen.com/qdbimg/349573/{bid}" if bid else ""
        category = (book_info.get("CategoryName") or book_info.get("categoryName") or "").strip()
        action_status = (book_info.get("ActionStatus") or book_info.get("actionStatus") or "").strip()
        finished = "完结" if action_status and "完" in action_status else "连载" if action_status else ""
        return {"name": title, "title": title, "album": title, "bestCover": cover, "cover": cover, "pic": cover, "author": author, "announcer": "", "artist": "", "desc": desc, "info": desc, "releaseDate": "", "category": category, "finished": finished}
    except Exception: return None

def _qidian_plugin_detail(book_id: str) -> dict:
    """Use the same public search/detail chain as qidian-scraper-wasm."""
    session = get_safe_session()
    headers = {
        "Platform": "10",
        "AppId": "50",
        "AreaId": "501000",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/132.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://qidian.com/",
    }
    response = session.get(
        f"https://qdcg.qidian.com/api/audio/detail?adid={book_id}&_csrfToken=",
        headers=headers,
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("Result") not in (None, 0, "0"):
        raise ValueError(payload.get("Message") or "起点详情接口返回失败")
    data = payload.get("Data") or {}
    title = str(data.get("AudioName") or data.get("audioName") or "").strip()
    if not title:
        raise ValueError("起点详情接口没有返回书名")
    tags = []
    for item in data.get("Tags") or data.get("tags") or []:
        if isinstance(item, dict):
            tag = str(item.get("TagName") or item.get("tagName") or item.get("Name") or "").strip()
        else:
            tag = str(item).strip()
        if tag and tag not in tags:
            tags.append(tag)
    category = str(data.get("CategoryName") or data.get("categoryName") or "").strip()
    if category and category not in tags:
        tags.append(category)
    cover = str(data.get("CoverUrl") or data.get("coverUrl") or "").strip().replace("http://", "https://")
    release_date = _extract_qidian_cover_year(cover)
    status = str(data.get("ActionStatus") or data.get("actionStatus") or "").strip()
    finished = "完结" if "完结" in status else "连载" if status else ""
    return {
        "name": title, "title": title, "album": title,
        "bestCover": cover, "cover": cover, "pic": cover,
        "author": str(data.get("AuthorName") or data.get("authorName") or "").strip(),
        "announcer": str(data.get("AnchorName") or data.get("anchorName") or "").strip(),
        "artist": str(data.get("AnchorName") or data.get("anchorName") or "").strip(),
        "desc": str(data.get("Intro") or data.get("intro") or data.get("Description") or "").strip(),
        "info": str(data.get("Intro") or data.get("intro") or data.get("Description") or "").strip(),
        "releaseDate": release_date, "category": category, "finished": finished, "tags": tags,
    }


def _extract_qidian_cover_year(cover_url: str) -> str:
    """起点有声封面常把上传日期写入 URL，例如 /coverimg/2022-02-08/xxx.jpg。"""
    match = re.search(r"/(?:19|20)\d{2}[-_/](?:0?[1-9]|1[0-2])[-_/](?:0?[1-9]|[12]\d|3[01])(?:/|$)", str(cover_url or ""))
    return match.group(0).strip("/_").split("-")[0].split("/")[0] if match else ""


def qidian_api(album_id: str, cookie_str: str | None = None) -> dict:
    errors = []
    try:
        return _qidian_plugin_detail(str(album_id).strip())
    except Exception as exc:
        errors.append(str(exc))
    try:
        return _qidian_getshare(album_id, cookie_str=cookie_str)
    except Exception as exc:
        errors.append(str(exc))
    raise Exception(f"起点详情获取失败：{'；'.join(errors)}")

def search_platform_metadata(platform: str, keyword: str, page: int = 1, limit: int = 12) -> tuple[list[dict], bool]:
    """Search book/album titles using the same public endpoints as the plugins."""
    platform = str(platform or "").strip()
    keyword = str(keyword or "").strip()
    if not keyword:
        raise ValueError("请输入书名")
    session = get_safe_session()
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json, text/plain, */*"}
    results = []

    def add(item_id, title, author="", cover="", intro="", tags=None, narrator="", chapter_count=0, finished="", category="", release_date=""):
        item_id, title = str(item_id or "").strip(), str(title or "").strip()
        if not item_id or not title or any(item["id"] == item_id for item in results):
            return
        results.append({
            "id": item_id,
            "title": title,
            "author": str(author or "").strip(),
            "cover": str(cover or "").strip(),
            "desc": str(intro or "").strip(),
            "tags": tags or [],
            "narrator": str(narrator or "").strip(),
            "chapter_count": chapter_count or 0,
            "finished": str(finished or "").strip(),
            "category": str(category or "").strip(),
            "release_date": str(release_date or "").strip(),
        })

    if platform == "喜马拉雅":
        url = "https://www.ximalaya.com/revision/search"
        params = {"core": "album", "spellchecker": "true", "rows": limit, "condition": "relation", "device": "web", "kw": keyword, "page": page}
        data = session.get(url, params=params, headers={**headers, "Referer": "https://www.ximalaya.com/"}, timeout=20).json()
        docs = (((data.get("data") or {}).get("result") or {}).get("response") or {}).get("docs") or []
        for item in docs:
            add(item.get("id"), item.get("title"), item.get("nickname") or item.get("anchorName"), item.get("cover_path"), item.get("intro"), [x.strip() for x in str(item.get("tags") or "").split(",") if x.strip()])
    elif platform == "番茄畅听":
        for item in _fanqie_search_books(keyword, page=page, limit=limit):
            add(
                item["id"],
                item["title"],
                item["author"],
                item["cover"],
                item["desc"],
                item["tags"],
                narrator=item["narrator"],
                chapter_count=item["chapter_count"],
                finished=item["finished"],
                category=item["category"],
                release_date=item["release_date"],
            )
    elif platform == "起点听书":
        url = "https://qdcg.qidian.com/api/search/list"
        data = session.get(url, params={"key": keyword, "pageIndex": page, "pageSize": limit, "site": 3, "model": 1}, headers={**headers, "Platform": "10", "AppId": "50", "AreaId": "501000"}, timeout=20).json()
        for item in ((data.get("Data") or {}).get("items") or []):
            book_id = item.get("bookId")
            cover = f"https://bookcover.yuewen.com/qdbimg/349573/{book_id}" if book_id else ""
            add(book_id, item.get("bookName"), item.get("authorName"), cover, item.get("description"), [item.get("categoryName")] if item.get("categoryName") else [])
    elif platform == "酷我听书":
        data = session.get("http://search.kuwo.cn/r.s", params={"pn": page - 1, "rn": limit, "all": keyword, "ft": "album", "newsearch": 1, "rformat": "json", "encoding": "utf8", "plat": "pc", "pcjson": 1}, headers=headers, timeout=20).json()
        for item in data.get("albumlist") or data.get("abslist") or []:
            add(item.get("albumid") or item.get("id"), item.get("name"), item.get("artist") or item.get("aartist"), item.get("hts_img") or item.get("img"), item.get("info"))
    elif platform == "网易云听书":
        data = session.get("https://music.163.com/api/search/get", params={"s": keyword, "type": 1009, "limit": limit, "offset": (page - 1) * limit}, headers={**headers, "Referer": "https://music.163.com/"}, timeout=20).json()
        for item in ((data.get("result") or {}).get("djRadios") or []):
            dj = (item.get("dj") or {}).get("nickname") if isinstance(item.get("dj"), dict) else ""
            cover = item.get("picUrl") or item.get("pic_url") or ""
            add(item.get("id"), item.get("name"), dj, cover, item.get("desc"), [x for x in (item.get("category"), item.get("secondCategory")) if x])
    elif platform == "懒人听书":
        from urllib.parse import quote
        page_html = session.get(f"https://www.lrts.me/search/book/{page}/{quote(keyword)}" if page > 1 else f"https://www.lrts.me/search/book/{quote(keyword)}", headers={**headers, "Referer": "https://www.lrts.me/"}, timeout=20).text
        for block in re.findall(r'<li class="book-item"[^>]*>([\s\S]*?)</li>', page_html):
            match = re.search(r'<a href="/book/(\d+)"', block)
            title = re.search(r'<a class="book-item-name"[^>]*>([\s\S]*?)</a>', block)
            cover = re.search(r'<img[^>]*src="([^"]+)"', block)
            intro = re.search(r'<p class="book-item-desc weaken">([\s\S]*?)</p>', block)
            if match and title:
                clean = lambda value: html.unescape(re.sub(r"<[^>]+>", " ", value)).strip()
                add(match.group(1), clean(title.group(1)), "", cover.group(1) if cover else "", clean(intro.group(1)) if intro else "")
    elif platform == "蜻蜓fm":
        graphql = '{ searchResultsPage(keyword:"%s", page:%d, include:"channel_ondemand" ) { searchData, numFound } }' % (keyword.replace('"', '\\"'), page)
        data = session.post("https://webbff.qtfm.cn/www", json={"query": graphql}, headers=headers, timeout=20).json()
        page = ((data.get("data") or {}).get("searchResultsPage") or {})
        search_data = page.get("searchData") or []
        if isinstance(search_data, dict): search_data = search_data.get("items") or []
        for item in search_data:
            cover = str(item.get("cover") or "").split("!")[0]
            add(item.get("id"), item.get("title"), item.get("podcaster"), cover, item.get("description"))
    else:
        raise ValueError(f"{platform} 暂未接入书名搜索接口")
    return results[:limit], len(results) >= limit


def _normalize_ypshuo_title(value: str) -> str:
    value = html.unescape(re.sub(r"<[^>]+>", "", str(value or ""))).strip()
    value = re.split(r"[|｜]", value, maxsplit=1)[0]
    value = re.sub(r"[（(](?:有声|演播|播讲|多人|精品|全集|完结|更新)[^）)]*[）)]$", "", value).strip()
    value = re.sub(r"^(?:有声小说|有声书|精品有声剧)[：:·\s]*", "", value)
    return re.sub(r"[\s《》<>·•:：,，。.!！?？_\-—]", "", value).lower()


def _select_ypshuo_author(title: str, candidates: list[dict]) -> dict:
    exact = _matching_ypshuo_authors(title, candidates)
    authors = {item["author"] for item in exact}
    return exact[0] if exact and len(authors) == 1 else {}


def _matching_ypshuo_authors(title: str, candidates: list[dict]) -> list[dict]:
    wanted = _normalize_ypshuo_title(title)
    exact = []
    seen_authors = set()
    for item in candidates:
        author = str(item.get("author") or item.get("author_name") or "").strip()
        item_title = str(item.get("title") or item.get("novel_name") or "").strip()
        author_key = re.sub(r"\s+", "", author).lower()
        if wanted and author and author_key not in seen_authors and _normalize_ypshuo_title(item_title) == wanted:
            exact.append({"title": item_title, "author": author, "id": str(item.get("id") or ""), "source": item.get("source") or "ypshuo"})
            seen_authors.add(author_key)
    return exact


def _parse_youshu_author_candidates(page_html: str) -> list[dict]:
    candidates = []
    blocks = re.split(r'<div\s+class=["\']c_row["\']\s*>', page_html, flags=re.IGNORECASE)[1:]
    if not blocks:
        blocks = [page_html]
    for block in blocks:
        title_match = re.search(r'class=["\']c_subject["\'][\s\S]*?<a[^>]*>([\s\S]*?)</a>', block, re.IGNORECASE)
        author_match = re.search(r'作者\s*[：:]\s*</span>\s*<span[^>]*class=["\']c_value["\'][^>]*>([\s\S]*?)</span>', block, re.IGNORECASE)
        if not author_match:
            author_match = re.search(r'作者\s*[：:][\s\S]{0,300}?<a[^>]*>([^<]+)</a>', block, re.IGNORECASE)
        if title_match and author_match:
            item_id = (re.search(r'/book/(\d+)', block) or ["", ""])[1]
            candidates.append({"id": item_id, "title": html.unescape(re.sub(r"<[^>]+>", "", title_match.group(1))).strip(), "author": html.unescape(re.sub(r"<[^>]+>", "", author_match.group(1))).strip(), "source": "youshu"})
    return candidates


@lru_cache(maxsize=256)
def ypshuo_author_candidates(title: str) -> list[dict]:
    """Return distinct author candidates whose normalized title is an exact match."""
    title = str(title or "").strip()
    if not title:
        return []
    session = get_safe_session()
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json, text/plain, */*", "Referer": "https://m.ypshuo.com/"}
    try:
        response = session.get("https://m.ypshuo.com/api/novel/search", params={"keyword": title, "searchType": 1, "page": 1}, headers=headers, timeout=8)
        payload = response.json()
        books = ((payload.get("data") or {}).get("data") or []) if str(payload.get("code")) == "00" else []
        matches = _matching_ypshuo_authors(title, [{**book, "source": "ypshuo"} for book in books if isinstance(book, dict)])
        if matches:
            return matches
    except Exception as exc:
        _debug_log(f"[阅评说作者补全] 主 API 失败: {exc}")
    try:
        response = session.post("https://www.youshu.me/modules/article/search.php", data={"searchtype": "all", "searchkey": title, "t_btnsearch": ""}, headers={**headers, "Referer": "https://www.youshu.me/"}, timeout=8)
        response.encoding = response.apparent_encoding or response.encoding
        return _matching_ypshuo_authors(title, _parse_youshu_author_candidates(response.text))
    except Exception as exc:
        _debug_log(f"[阅评说作者补全] 备用站点失败: {exc}")
        return []


@lru_cache(maxsize=256)
def ypshuo_author_by_title(title: str) -> dict:
    """Resolve an original novel author only when candidates agree."""
    candidates = ypshuo_author_candidates(str(title or "").strip())
    authors = {item["author"] for item in candidates}
    return candidates[0] if candidates and len(authors) == 1 else {}


def netease_ting_api(album_id: str) -> dict:
    try:
        session = get_safe_session()
        cookie_str = os.environ.get("NETEASE_COOKIE", "").strip() or get_platform_cookies().get("netease", "").strip()
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://music.163.com/", "Accept": "application/json"}
        if cookie_str: headers["Cookie"] = session.headers["Cookie"] = cookie_str
        try:
            from Crypto.Cipher import AES
            from Crypto.Util.Padding import pad
            enc = {"radioId": album_id, "limit": 1, "offset": 0, "asc": True}
            csrf = ""
            if "__csrf=" in cookie_str:
                m = re.search(r"__csrf=([^;]+)", cookie_str)
                if m: csrf = m.group(1).strip()
            enc["csrf_token"] = csrf
            import base64, binascii, random, string
            sk = "".join(random.choices(string.ascii_letters + string.digits, k=16))
            tx = json.dumps(enc)
            p1 = base64.b64encode(AES.new("0CoJUm6Qyw8W8jud".encode(), AES.MODE_CBC, "0102030405060708".encode()).encrypt(pad(tx.encode(), AES.block_size))).decode()
            p2 = base64.b64encode(AES.new(sk.encode(), AES.MODE_CBC, "0102030405060708".encode()).encrypt(pad(p1.encode(), AES.block_size))).decode()
            t = sk[::-1]
            r = pow(int(binascii.hexlify(t.encode()), 16), int("010001", 16), int("00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7b725152b3ab17a876aea8a5aa76d2e417629ec4ee341f56135fccf695280104e0312ecbda92557c93870114af6c9d05c4f7f0c3685b7a46bee255932575cce10b424d813cfe4875d3e82047b97ddef52741d546b8e289dc6935b3ece0462db0a22b8e7", 16))
            body = {"params": p2, "encSecKey": format(r, "x").zfill(256)}
            resp = session.post("https://music.163.com/weapi/dj/program/byradio", data=body, headers={**headers, "Content-Type": "application/x-www-form-urlencoded"}, timeout=15)
            if resp.status_code == 200:
                j = resp.json()
                if j.get("code") == 200 and j.get("programs"):
                    prog = j["programs"][0]
                    radio = prog.get("radio") or {}
                    name = (radio.get("name") or "").strip() or (prog.get("name") or "").strip()
                    cover = radio.get("picUrl") or radio.get("pic") or prog.get("coverUrl") or ""
                    desc = radio.get("desc") or prog.get("description") or ""
                    announcer = (radio.get("dj") or {}).get("nickname") or (prog.get("dj") or {}).get("nickname") or ""
                    if j.get("count", 0) and not desc: desc = f"网易云听书电台，共{j['count']}期节目。"
                    release_date = ""
                    ct = prog.get("createTime") or radio.get("createTime")
                    if ct is not None:
                        s = str(ct)
                        if s.isdigit() and len(s) >= 10:
                            try:
                                from datetime import datetime
                                release_date = datetime.utcfromtimestamp(int(s[:10])).strftime("%Y")
                            except: release_date = s[:4] if len(s) >= 4 else ""
                        elif len(s) >= 4 and s[:4].isdigit(): release_date = s[:4]
                    return {"name": name, "title": name, "album": name, "bestCover": cover, "cover": cover, "pic": cover, "author": "", "announcer": announcer, "artist": announcer, "desc": desc, "info": desc, "releaseDate": release_date}
        except ImportError as e: raise Exception("缺少必要的加密库。请在终端执行: pip install pycryptodome")
        except Exception: pass
        url = "https://music.163.com/api/dj/program/detail"
        resp = session.get(url, params={"id": album_id}, headers=headers, timeout=15)
        if resp.status_code != 200: raise Exception(f"网易云听书请求失败，状态码：{resp.status_code}")
        data = resp.json()
        prog = data.get("program", data.get("data", data)) or {}
        if isinstance(prog, list) and prog: prog = prog[0]
        main = prog.get("mainSong", prog) or prog
        title = (main.get("name") if isinstance(main, dict) else prog.get("name")) or prog.get("title") or ""
        cover = prog.get("coverUrl") or prog.get("picUrl") or prog.get("cover") or prog.get("pic") or ""
        if isinstance(main, dict):
            artist = main.get("artists", [])
            announcer = artist[0].get("name", "") if isinstance(artist, list) and artist else (prog.get("dj", {}) or {}).get("nickname", "")
        else: announcer = (prog.get("dj") or {}).get("nickname") or prog.get("announcer") or prog.get("anchor") or ""
        author = prog.get("author") or prog.get("bookAuthor") or ""
        desc = prog.get("description") or prog.get("desc") or prog.get("intro") or prog.get("info") or ""
        if not title: raise Exception(f"未找到有效专辑数据。请确认 ID 类型 (API返回: {str(data)[:100]}...)")
        release_date = ""
        ct = prog.get("createTime")
        if ct is not None:
            s = str(ct)
            if s.isdigit() and len(s) >= 10:
                try:
                    from datetime import datetime
                    ts = int(s[:10]) if len(s) > 10 else int(s)
                    if ts > 1e9: ts = ts // 1000
                    release_date = datetime.utcfromtimestamp(ts).strftime("%Y")
                except: release_date = s[:4] if len(s) >= 4 else ""
            elif len(s) >= 4 and s[:4].isdigit(): release_date = s[:4]
        return {"name": title, "title": title, "album": title, "bestCover": cover, "cover": cover, "pic": cover, "author": author, "announcer": announcer, "artist": announcer, "desc": desc, "info": desc, "releaseDate": release_date}
    except Exception as e: raise Exception(f"{str(e)}")

def yunting_api(album_id: str) -> dict:
    import hashlib
    import time
    from urllib.parse import urlparse, parse_qs
    
    try:
        aid = album_id.strip()
        # 1. 链接/ID 解析
        if aid.startswith("http"):
            parsed = urlparse(aid)
            qs = parse_qs(parsed.query, keep_blank_values=True)
            for key in ("columnId", "albumId", "id"):
                if key in qs and qs[key]:
                    aid = str(qs[key][0]).strip()
                    break
            else: 
                raise Exception("云听链接中未找到专辑 ID (columnId / albumId / id)")

        # 2. 接口及鉴权配置
        base_url = "https://ytmsout.radio.cn"
        secret = "f0fc4c668392f9f9a447e48584c214ee"
        path = f"/web/appAlbum/detail/{aid}"
        data_params = {"id": aid}

        # 计算签名
        timestamp_ms = str(int(time.time() * 1000))
        params_str = "&".join(f"{k}={data_params[k]}" for k in sorted(data_params.keys()))
        sign_text = f"{params_str}&timestamp={timestamp_ms}&key={secret}"
        sign = hashlib.md5(sign_text.encode("utf-8")).hexdigest().upper()

        headers = {
            "Content-Type": "application/json",
            "equipmentId": "0000",
            "platformCode": "WEB",
            "timestamp": timestamp_ms,
            "sign": sign,
        }

        url = f"{base_url}{path}"
        session = get_safe_session()
        
        # 3. 发起请求
        resp = session.get(url, params=data_params, headers=headers, timeout=15)
        if resp.status_code != 200: 
            raise Exception(f"请求失败状态码：{resp.status_code}")
            
        result = resp.json()
        if result.get("code") != 0: 
            raise Exception(f"云听返回错误：{result.get('message', '未知')}")
            
        data = result.get("data") or {}

        # 4. 提取与组装元数据
        title = (data.get("name") or data.get("title") or data.get("albumName") or "").strip() or f"专辑_{aid}"
        subtitle = (data.get("desSimple") or data.get("subtitle") or "").strip()
        cover = data.get("image") or data.get("cover") or ""
        desc = (data.get("des") or data.get("description") or "").strip()

        total = data.get("singleCount") or data.get("childCount") or data.get("total") or 0
        if total and not desc: 
            desc = f"云听fm 专辑，共{total}集。"

        author, announcer = "", (data.get("ownerNickName") or data.get("anchorName") or "").strip()
        if desc:
            import re
            m_author = re.search(r"作者[：:]\s*([^；;\n]+)", desc)
            if m_author: 
                author = m_author.group(1).strip()

        end_flag = data.get("endFlag")
        finished = "完结" if end_flag == 1 else "连载" if end_flag is not None else ""

        release_date = ""
        val = data.get("publishTime") or data.get("createTime")
        if val is not None:
            try:
                from datetime import datetime
                if isinstance(val, (int, float)) and val >= 1e12: 
                    release_date = str(datetime.fromtimestamp(val / 1000.0).year)
                elif isinstance(val, (int, float)) and val >= 1e9: 
                    release_date = str(datetime.fromtimestamp(val).year)
                else:
                    s = str(val).strip()
                    if len(s) >= 4 and s[:4].isdigit(): 
                        release_date = s[:4]
            except: 
                pass
        if not release_date:
            release_date = _extract_yunting_cover_year(cover)

        category = (data.get("categoryName") or data.get("typeName") or data.get("category") or "").strip()

        return {
            "name": title, "title": title, "album": title, "subtitle": subtitle, 
            "bestCover": cover, "cover": cover, "pic": cover, 
            "author": author, "announcer": announcer, "artist": announcer, 
            "desc": desc, "info": desc, "releaseDate": release_date, 
            "category": category, "finished": finished
        }

    except Exception as e: 
        raise Exception(f"云听fm API异常：{str(e)}")


def _extract_yunting_cover_year(cover_url: str) -> str:
    """云听封面常见路径为 /202211/09/18/xxx.jpg，使用其上传日期作为年份兜底。"""
    from urllib.parse import unquote
    value = unquote(str(cover_url or ""))
    match = re.search(r"/(?:19|20)(\d{2})(?:0[1-9]|1[0-2])/(?:0[1-9]|[12]\d|3[01])/(?:0[1-9]|[12]\d|3[01])(?:/|$)", value)
    if match:
        return ("20" if match.group(0).find("/20") >= 0 else "19") + match.group(1)
    match = re.search(r"/(?:19|20)(\d{2})[-_/](?:0?[1-9]|1[0-2])[-_/](?:0?[1-9]|[12]\d|3[01])(?:/|$)", value)
    return (re.search(r"(?:19|20)\d{2}", match.group(0)).group(0) if match else "")

def qingting_api(album_id: str) -> dict:
    try:
        session = get_safe_session()
        url = f"https://i.qtfm.cn/capi/v3/channel/{album_id.strip()}"
        headers = {"Content-Type": "application/json", "Origin": "https://www.qtfm.cn", "Referer": "https://www.qtfm.cn/", "User-Agent": "Mozilla/5.0"}
        resp = session.get(url, params={"user_id": "null"}, headers=headers, timeout=15)
        if resp.status_code != 200: raise Exception(f"蜻蜓fm 请求失败，状态码：{resp.status_code}")
        result = resp.json()
        if result.get("errorno") != 0: raise Exception(f"蜻蜓fm 返回错误：{result.get('errormsg', '未知')}")
        data = result.get("data") or {}
        title = (data.get("title") or "").strip() or f"专辑_{album_id}"
        cover = data.get("cover") or ""
        author = ""
        podcasters = data.get("podcasters") or []
        if podcasters and isinstance(podcasters[0], dict): author = (podcasters[0].get("nick_name") or podcasters[0].get("nickname") or "").strip()
        desc, total = data.get("description") or "", data.get("program_count", 0)
        if total and not desc: desc = f"蜻蜓fm 专辑，共{total}集。"
        category = (data.get("category_name") or data.get("categoryName") or data.get("category") or "").strip()
        if not category: category = str(data.get("category_id") or data.get("categoryId") or "")
        update_status = str(data.get("update_status") or data.get("updateStatus") or data.get("status") or "").strip()
        finished = "完结" if "完" in update_status or update_status in ("2", "3") else "连载" if "更" in update_status or "连" in update_status or update_status == "1" else ""
        tags_raw = data.get("tags") or data.get("tag_list") or []
        tags = []
        if isinstance(tags_raw, list):
            for t in tags_raw:
                tag = (t.get("name") or t.get("tag_name") or str(t)).strip() if isinstance(t, dict) else str(t).strip()
                if tag and tag not in tags: tags.append(tag)
        elif isinstance(tags_raw, str) and tags_raw: tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
        release_date = ""
        for key in ("create_time", "publish_time", "update_time", "publishTime", "createTime"):
            val = data.get(key)
            if val is not None:
                s = str(val).strip()
                if s.isdigit() and len(s) >= 10:
                    try:
                        from datetime import datetime
                        release_date = datetime.utcfromtimestamp(int(s[:10])).strftime("%Y")
                    except: release_date = s[:4] if len(s) >= 4 else ""
                    break
                elif len(s) >= 4 and s[:4].isdigit():
                    release_date = s[:4]
                    break
        return {"name": title, "title": title, "album": title, "bestCover": cover, "cover": cover, "pic": cover, "author": author, "announcer": author, "artist": author, "desc": desc, "info": desc, "releaseDate": release_date, "category": category, "finished": finished, "tags": tags}
    except Exception as e: raise Exception(f"蜻蜓fm API异常：{str(e)}")

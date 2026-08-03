# api_clients.py
import os
import re
import json
import time
import uuid
import codecs
import html
import requests
from config import get_platform_cookies, FANQIE_SHARE_ID, FANQIE_X_BOGUS, FANQIE_SIGNATURE
from network_utils import get_safe_session, _debug_log, clean_html_tags

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
    kuwo_cookie = os.environ.get("KUWO_COOKIE", "").strip()
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

def kuwo_api(album_id: str) -> dict:
    raw = get_kuwo_album_info(album_id, pn=1, rn=1)
    if not raw: raise Exception("酷我听书API请求失败或无数据返回")
    data = raw.get("data") or {}
    album_obj = data.get("album")
    if isinstance(album_obj, dict):
        name = album_obj.get("name") or album_obj.get("album") or album_obj.get("title") or ""
        pic = album_obj.get("pic") or album_obj.get("cover") or album_obj.get("albumpic") or ""
        artist = album_obj.get("artist") or album_obj.get("author") or ""
        info = (album_obj.get("info") or album_obj.get("albuminfo") or album_obj.get("description") or "").strip()
        release_date = str(album_obj.get("releaseDate") or "").strip()[:4] if album_obj.get("releaseDate") else ""
    else:
        name = data.get("album") or data.get("name") or ""
        pic = data.get("pic") or data.get("cover") or ""
        artist = data.get("artist") or data.get("author") or ""
        info = (data.get("info") or data.get("albuminfo") or data.get("description") or "").strip()
        release_date = str(data.get("releaseDate") or "").strip()[:4] if data.get("releaseDate") else ""

    if not info and album_id:
        page_desc = get_kuwo_album_desc_from_page(album_id)
        if page_desc: info = page_desc
    if pic: pic = re.sub(r'/([1-9])00/', r'/\g<1>000/', pic)
    return {"album": name, "pic": pic, "artist": artist, "info": info, "releaseDate": release_date}

def parse_novelfm_share_response(data: dict) -> dict:
    if not data or data.get("code") != 0: return {}
    inner = data.get("data") or {}
    api_book = inner.get("api_book_info")
    if not api_book or not isinstance(api_book, dict): return {}
    title = (api_book.get("book_name") or api_book.get("title") or "").strip()
    if not title: return {}
    author = (api_book.get("author") or "").strip()
    cover = api_book.get("thumb_url") or api_book.get("audio_thumb_uri") or ""
    desc = (api_book.get("abstract") or "").strip()
    tags_str = api_book.get("tags") or ""
    tags = [t.strip() for t in tags_str.split(",") if t.strip()] if isinstance(tags_str, str) else []
    creation_status = api_book.get("creation_status")
    finished = "完结" if str(creation_status) == "1" else "连载" if creation_status is not None else ""
    serial_count = api_book.get("serial_count") or ""
    try: chapter_count = int(serial_count) if serial_count else 0
    except: chapter_count = 0
    category = (api_book.get("category_info") or api_book.get("genre") or "").strip()
    if not category and tags: category = tags[0]
    create_time = (api_book.get("create_time") or "").strip()
    release_date = create_time[:4] if len(create_time) >= 4 else ""
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
                from fanqie_signature import generate_for_share_get_info
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

def _fanqie_search_by_id(book_id: str) -> dict:
    try:
        params = {"device_platform": "android", "os": "android", "aid": "3040", "app_name": "novel_fm", "version_code": "608", "device_id": "3942194090368537", "iid": "1109875180222825", "_rticket": str(int(time.time() * 1000))}
        url = "https://api5-sinfonlinec.novelfm.com/novelfm/bookmall/search/page/v1/"
        headers = {"Content-Type": "application/json; charset=utf-8", "User-Agent": "com.xs.fm/608"}
        session = get_safe_session()
        resp = session.post(url, params=params, headers=headers, json={"query": str(book_id).strip(), "limit": 30, "offset": 0}, timeout=12, verify=False)
        if resp.status_code != 200: return {}
        search_data = (resp.json().get("data") or {}).get("search_data")
        if not search_data: return {}
        items = search_data if isinstance(search_data, list) else (list(search_data.values()) if isinstance(search_data, dict) else [])
        want_id = str(book_id).strip()
        for book_item in items:
            books = book_item.get("books") if isinstance(book_item, dict) else []
            for book in books:
                if str(book.get("book_id", "")).strip() != want_id: continue
                category = (book.get("category") or book.get("category_name") or "").strip()
                finished = ""
                if book.get("creation_status") is not None: finished = "完结" if str(book.get("creation_status")) == "1" else "连载"
                elif book.get("serial_status"): finished = "完结" if "完" in str(book.get("serial_status")) else "连载"
                tags = []
                tags_raw = book.get("tags") or book.get("tag_list") or book.get("labels") or []
                if isinstance(tags_raw, list):
                    for t in tags_raw:
                        tag = (t.get("name") or t.get("tag_name") or str(t)).strip() if isinstance(t, dict) else str(t).strip()
                        if tag and tag not in tags: tags.append(tag)
                elif isinstance(tags_raw, str) and tags_raw: tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
                return {"title": (book.get("book_name") or "").strip(), "author": (book.get("author") or "").strip(), "cover": book.get("thumb_url") or book.get("cover") or "", "desc": (book.get("abstract") or "").strip(), "category": category, "finished": finished, "tags": tags}
        return {}
    except Exception: return {}

def fanqie_api(album_id: str) -> dict:
    try:
        share_info = _fanqie_get_share_info(album_id)

        session = get_safe_session()
        title, author, cover, desc, announcer, category, finished, tags, chapter_count = "", "", "", "", "", "", "", [], 0
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
                            cover, desc = book_data.get("thumb_url") or book_data.get("cover") or cover, (book_data.get("abstract") or book_data.get("desc") or "").strip() or desc
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
                            if cs is not None and not finished: finished = "完结" if str(cs) == "1" else "连载"
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
        if not title:
            hit = _fanqie_search_by_id(album_id)
            if hit:
                title, author, cover, desc = hit.get("title") or title, hit.get("author") or author, hit.get("cover") or cover, hit.get("desc") or desc
                category, finished, tags = hit.get("category") or category, hit.get("finished") or finished, hit.get("tags", []) or tags
        if not title: title = f"书籍ID_{album_id}"
        if chapter_count and not desc: desc = f"番茄畅听有声书，共{chapter_count}集。"
        return {"name": title, "title": title, "album": title, "bestCover": cover, "cover": cover, "pic": cover, "author": author, "announcer": announcer, "artist": announcer, "desc": desc, "info": desc, "releaseDate": "", "category": category, "finished": finished, "tags": tags}
    except Exception as e: raise Exception(f"番茄畅听API异常：{str(e)}")

def _qidian_getshare(book_id: str, cookie_str: str | None = None) -> dict | None:
    try:
        if cookie_str is None: cookie_str = get_platform_cookies().get("qidian", "").strip()
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
        "releaseDate": "", "category": category, "finished": finished, "tags": tags,
    }


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

    def add(item_id, title, author="", cover="", intro="", tags=None):
        item_id, title = str(item_id or "").strip(), str(title or "").strip()
        if not item_id or not title or any(item["id"] == item_id for item in results):
            return
        results.append({"id": item_id, "title": title, "author": str(author or "").strip(), "cover": str(cover or "").strip(), "desc": str(intro or "").strip(), "tags": tags or []})

    if platform == "喜马拉雅":
        url = "https://www.ximalaya.com/revision/search"
        params = {"core": "album", "spellchecker": "true", "rows": limit, "condition": "relation", "device": "web", "kw": keyword, "page": page}
        data = session.get(url, params=params, headers={**headers, "Referer": "https://www.ximalaya.com/"}, timeout=20).json()
        docs = (((data.get("data") or {}).get("result") or {}).get("response") or {}).get("docs") or []
        for item in docs:
            add(item.get("id"), item.get("title"), item.get("nickname") or item.get("anchorName"), item.get("cover_path"), item.get("intro"), [x.strip() for x in str(item.get("tags") or "").split(",") if x.strip()])
    elif platform == "番茄畅听":
        url = "https://api5-sinfonlinec.novelfm.com/novelfm/bookmall/search/page/v1/"
        params = {"device_platform": "android", "os": "android", "aid": "3040", "app_name": "novel_fm", "version_code": "608", "_rticket": int(time.time() * 1000)}
        data = session.post(url, params=params, json={"query": keyword, "limit": limit, "offset": (page - 1) * limit}, headers=headers, timeout=20).json()
        for group in ((data.get("data") or {}).get("search_data") or []):
            for item in (group.get("books") or []):
                add(item.get("book_id"), item.get("book_name"), item.get("author"), item.get("thumb_url"), item.get("abstract"), str(item.get("tags") or "").split(","))
    elif platform == "起点听书":
        url = "https://qdcg.qidian.com/api/search/list"
        data = session.get(url, params={"key": keyword, "pageIndex": page, "pageSize": limit, "site": 3, "model": 1}, headers={**headers, "Platform": "10", "AppId": "50", "AreaId": "501000"}, timeout=20).json()
        for item in ((data.get("Data") or {}).get("items") or []):
            add(item.get("bookId"), item.get("bookName"), item.get("authorName"), "", item.get("description"), [item.get("categoryName")] if item.get("categoryName") else [])
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


def netease_ting_api(album_id: str) -> dict:
    try:
        session = get_safe_session()
        cookie_str = get_platform_cookies().get("netease", "").strip()
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

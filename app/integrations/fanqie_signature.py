# -*- coding: utf-8 -*-
"""
番茄畅听 share/get_info 签名生成（纯 Python 环境，无需安装 Node）：
- X-Bogus：用 py_mini_racer 在进程内执行 JS 生成（pip install py_mini_racer + 一份 X-Bogus.js）
- _signature：请求头条 jssdk_signature 接口获取（可选）
"""
import os
import subprocess
import urllib.parse

_BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
# 优先 X-Bogus-standalone.js，否则使用 X-Bogus.js（开源或你保存的 JS 需有 sign(query, user_agent)）
_XBOGUS_CANDIDATES = [
    os.path.join(_BASE, "X-Bogus-standalone.js"),
    os.path.join(_BASE, "X-Bogus.js"),
]


def _get_xbogus_js_path():
    for p in _XBOGUS_CANDIDATES:
        if os.path.isfile(p):
            return p
    return ""


_ctx = None


def _get_xbogus_mini_racer(query: str, user_agent: str) -> str:
    """纯 Python 方案：用 py_mini_racer 在进程内执行 JS，无需 Node。需 pip install py_mini_racer。"""
    js_path = _get_xbogus_js_path()
    if not js_path:
        return ""
    global _ctx
    try:
        from py_mini_racer import MiniRacer
        if _ctx is None:
            with open(js_path, "r", encoding="utf-8") as f:
                js_code = f.read()
            _ctx = MiniRacer()
            # 无 Node 环境：注入 module/exports 以支持 module.exports = { sign }，避免 process.versions.node 走 Node 路径
            _ctx.eval(
                "var window = null; var global = this; var self = this; "
                "var process = { env: {} }; "
                "var module = { exports: {} }; var exports = module.exports;"
            )
            _ctx.eval(js_code)
            # 开源版只有 module.exports.sign，挂到全局供 call("sign", ...)
            _ctx.eval("if (module && module.exports && module.exports.sign) this.sign = module.exports.sign;")
        out = _ctx.call("sign", query, user_agent or "")
        return (out or "").strip()
    except Exception:
        return ""


def _get_xbogus_node(query: str, user_agent: str) -> str:
    """备用：用 Node 运行 X-Bogus.js（需本机安装 Node）。"""
    js_path = _get_xbogus_js_path()
    if not js_path:
        return ""
    try:
        code = (
            "const q = process.argv[1]; const ua = process.argv[2] || ''; "
            "const m = require(%r); console.log(m.sign(q, ua));"
        ) % (js_path.replace("\\", "\\\\"),)
        out = subprocess.run(
            ["node", "-e", code, query, user_agent],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=_BASE,
        )
        if out.returncode == 0 and out.stdout:
            return out.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
        pass
    return ""


def _get_xbogus_execjs(query: str, user_agent: str) -> str:
    """备用：PyExecJS（通常依赖 Node）。"""
    js_path = _get_xbogus_js_path()
    if not js_path:
        return ""
    try:
        import execjs
        with open(js_path, "r", encoding="utf-8") as f:
            ctx = execjs.compile(f.read())
        return ctx.call("sign", query, user_agent or "") or ""
    except Exception:
        return ""


def generate_x_bogus(full_url: str, user_agent: str = "") -> str:
    """
    根据完整 URL 和 User-Agent 生成 X-Bogus 参数。
    full_url: 即将请求的完整 URL（含 query，不含 X-Bogus）。
    user_agent: 请求使用的 User-Agent。
    返回 X-Bogus 字符串，失败返回空。
    """
    ua = user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    parsed = urllib.parse.urlparse(full_url)
    query = parsed.query or ""
    # 优先纯 Python 方案（py_mini_racer，无需 Node）
    xb = _get_xbogus_mini_racer(query, ua)
    if not xb:
        xb = _get_xbogus_node(query, ua)
    if not xb:
        xb = _get_xbogus_execjs(query, ua)
    return xb


# 番茄 H5 分享页域名，与前端 window.location 一致，jssdk 可能只对该域名返回有效 signature
FANQIE_SHARE_PAGE_BASE = "https://m.changdunovel.com/ug/pages/book-share"


def build_share_page_url(book_id: str, share_id: str, extra_params: dict = None) -> str:
    """拼出与浏览器地址栏一致的分享页 URL（用于请求 jssdk_signature）。"""
    from urllib.parse import urlencode, unquote
    # share_id 在 config 里可能是 "xxx%3D"，先 unquote 再 urlencode 避免双重编码成 %253D
    raw_share = (share_id or "").strip()
    if raw_share:
        raw_share = unquote(raw_share)
    q = [("book_id", str(book_id).strip()), ("share_id", raw_share), ("source_channel", "link")]
    if extra_params:
        q.extend((k, v) for k, v in extra_params.items() if v is not None and str(v).strip() != "")
    return FANQIE_SHARE_PAGE_BASE + "?" + urlencode(q, safe="")


def generate_signature_from_jssdk(page_url: str, debug: bool = False, cookie: str = "") -> str:
    """
    请求 polaris.zijieapi.com/jssdk_signature/ 获取 _signature（与前端一致）。
    page_url: 分享页完整 URL。
    cookie: 可选，请求时携带的 Cookie 字符串（如从 config.FANQIE_JSSDK_COOKIE 读取）。
    返回 signature 字符串，失败或为空则返回 ""。
    """
    try:
        import requests
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:
            pass
        url = "https://polaris.zijieapi.com/jssdk_signature/"
        # 与前端一致：url 必填；aid/app 等便于服务端识别 novelfm 并可能返回 signature
        params = {
            "url": page_url.split("#")[0],
            "aid": "3040",           # novelfm 的 aid（_signature 文件里 byted_acrawler.init 使用）
            "app_name": "novel_fm",
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Linux; Android 9; Build/PQ3A.190605.07021633) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Mobile Safari/537.36",
            "Referer": "https://m.changdunovel.com/",
            "Accept": "application/json",
        }
        cookie_str = (cookie or "").strip()
        if cookie_str:
            headers["Cookie"] = cookie_str
            if debug:
                print("   [jssdk] 已携带 Cookie 请求")
        session = requests.Session()
        session.verify = False
        r = session.get(url, params=params, headers=headers, timeout=10)
        if debug:
            print(f"   [jssdk] GET {url} params.url={params['url'][:70]}...")
            print(f"   [jssdk] 状态码 {r.status_code}")
        if r.status_code != 200:
            return ""
        data = r.json()
        if debug:
            print(f"   [jssdk] 响应: {data}")
        sig = data.get("signature") or (data.get("data") or {}).get("signature") or ""
        if debug and (data.get("code") == 0) and not (sig or "").strip():
            print("   [jssdk] 返回空 signature（服务端需浏览器 Cookie/会话才签发），请将浏览器同一次请求的 X-Bogus 与 _signature 填到 config 使用。")
        return (sig or "").strip()
    except requests.exceptions.SSLError as e:
        if debug:
            print(f"   [jssdk] SSL 异常（可能被服务端断开或网络/代理干扰）: {e}")
        return ""
    except Exception as e:
        if debug:
            print(f"   [jssdk] 请求异常: {e}")
        return ""


def generate_for_share_get_info(
    base_url: str,
    params: dict,
    user_agent: str = "",
    jssdk_debug: bool = False,
) -> tuple:
    """
    为 share/get_info 请求生成 X-Bogus 和 _signature（若可用）。
    base_url: 如 https://api5-sinfonlineb.novelfm.com/novelfm/playerapi/share/get_info/v1/
    params: 当前请求的 query 参数字典（不含 X-Bogus、_signature），需含 book_id、share_id 用于拼分享页 URL。
    返回 (x_bogus_str, signature_str)，任一失败则为空字符串。
    """
    from urllib.parse import urlencode
    query = urlencode(params, doseq=False, safe="")
    full_url = base_url + "?" + query
    xb = generate_x_bogus(full_url, user_agent)
    # 前端 jssdk 传的是分享页 URL（window.location.href），不是 get_info 的 API URL
    book_id = (params.get("book_id") or "").strip()
    share_id = (params.get("share_id") or "").strip()
    jssdk_cookie = ""
    try:
        from app.core.config import FANQIE_JSSDK_COOKIE
        jssdk_cookie = (FANQIE_JSSDK_COOKIE or "").strip()
    except Exception:
        pass
    if book_id and share_id:
        page_url = build_share_page_url(book_id, share_id)
        sig = generate_signature_from_jssdk(page_url, debug=jssdk_debug, cookie=jssdk_cookie)
    else:
        sig = generate_signature_from_jssdk(full_url, debug=jssdk_debug, cookie=jssdk_cookie)
    return (xb or "", sig or "")

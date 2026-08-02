# -*- coding: utf-8 -*-
"""
内置浏览器：用 pywebview 打开分享页 URL，加载完成后用 JS 提取书籍数据，写入 JSON 文件。
番茄畅听：先在内置浏览器中打开用户粘贴的链接，页面加载后从 performance 取 get_info 请求 URL，再请求该 API 解析并写入 JSON。

用法: python fetch_share_with_browser.py <url> <platform> <output_json_path>
  platform: qidian | fanqie
  output_json_path: 提取结果写入的 JSON 文件路径

依赖: pip install pywebview requests
Windows 推荐安装 WebView2 运行时（通常已预装）。
"""
import sys
import json
import time
import os
from urllib.parse import urlparse


def _parse_get_info_response(data):
    """解析 get_info 响应（与 audio_processor 中番茄 share/get_info 解析一致）。"""
    if not data or data.get("code") != 0:
        return {}
    inner = data.get("data") or {}
    api_book = inner.get("api_book_info")
    if not api_book or not isinstance(api_book, dict):
        return {}
    title = (api_book.get("book_name") or api_book.get("title") or "").strip()
    if not title:
        return {}
    author = (api_book.get("author") or "").strip()
    cover = api_book.get("thumb_url") or api_book.get("audio_thumb_uri") or ""
    desc = (api_book.get("abstract") or "").strip()
    tags_str = api_book.get("tags") or ""
    tags = [t.strip() for t in tags_str.split(",") if t.strip()] if isinstance(tags_str, str) else []
    creation_status = api_book.get("creation_status")
    finished = "完结" if (creation_status is not None and str(creation_status) == "0") else "连载" if creation_status is not None else ""
    category = (api_book.get("category_info") or api_book.get("genre") or "").strip()
    if not category and tags:
        category = tags[0]
    return {"title": title, "author": author, "cover": cover, "desc": desc, "category": category, "finished": finished, "tags": tags}


def _parse_audio_detail_response(data):
    """解析 changdunovel.com/reading/bookapi/share/audio/detail/v1 的响应（正常打开分享页后页面会请求该 API）。"""
    if not data or data.get("code") != 0:
        return {}
    inner = data.get("data") or {}
    if not isinstance(inner, dict):
        return {}
    title = (inner.get("book_name") or inner.get("original_book_name") or "").strip()
    if not title:
        return {}
    author = (inner.get("author") or "").strip()
    desc = (inner.get("abstract") or inner.get("book_abstract_v2") or "").strip()
    cover = (
        inner.get("thumb_url")
        or inner.get("audio_thumb_uri")
        or inner.get("audio_thumb_url_hd")
        or inner.get("horiz_thumb_url")
        or ""
    )
    category = (inner.get("category") or "").strip()
    tags_str = inner.get("tags") or inner.get("pure_category_tags") or ""
    tags = [t.strip() for t in tags_str.split(",") if t.strip()] if isinstance(tags_str, str) else []
    if not category and tags:
        category = tags[0]
    creation_status = inner.get("creation_status")
    if creation_status is not None:
        finished = "完结" if str(creation_status) == "0" else "连载"
    else:
        finished = "连载"
    return {"title": title, "author": author, "cover": cover, "desc": desc, "category": category, "finished": finished, "tags": tags}


def _origin_from_url(url_string):
    """从分享页 URL 解析出 origin（如 https://m.changdunovel.com），用于 Referer/Origin，避免换链接后接口校验失败。"""
    try:
        parsed = urlparse(url_string.strip())
        if parsed.scheme and parsed.netloc:
            return "{}://{}".format(parsed.scheme, parsed.netloc)
    except Exception:
        pass
    return "https://m.changdunovel.com"


def _run_fanqie_auto(share_url, output_path):
    import webview
    import requests
    import time
    import json

    share_url = share_url.strip()
    result = {"title": "", "author": "", "cover": "", "desc": "", "category": "", "finished": "", "tags": []}
    referer_origin = _origin_from_url(share_url)

    # 1. API 拦截脚本
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

    # 2. 封面提取脚本
    js_get_dom_cover = r"""
    (function(){
        var cover = '';
        var imgEl = document.querySelector('.book-meta-new-img');
        if (imgEl && imgEl.src) cover = imgEl.src;
        if (!cover) { 
            var og = document.querySelector('meta[property="og:image"]'); 
            if (og && og.getAttribute('content')) cover = og.getAttribute('content'); 
        }
        return cover;
    })();
    """

    # 3. 终极兜底方案：暴力提取网页文字和底层 JSON 的脚本
    JS_FANQIE_FALLBACK = r"""
    (function(){
        var title = '', author = '', cover = '', desc = '', category = '', finished = '';
        var titleEl = document.querySelector('.book-meta-new-info-title');
        if (titleEl) title = (titleEl.innerText || titleEl.textContent || '').trim();
        var authorEl = document.querySelector('.book-meta-new-info-desc-author');
        if (authorEl) author = (authorEl.innerText || authorEl.textContent || '').trim();
        var imgEl = document.querySelector('.book-meta-new-img');
        if (imgEl && imgEl.src) cover = imgEl.src;

        var descEl = document.querySelector('.book-introduction-desc') || document.querySelector('.text-expand.book-introduction-desc');
        if (descEl) {
            desc = (descEl.innerText || descEl.textContent || '').trim();
            desc = desc.replace(/展开全部$/, '').replace(/展开$/, '').replace(/收起$/, '').trim();
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
            if ((descLabels[i].textContent || '').trim() === '更新状态') {
                var parent = descLabels[i].parentElement;
                if (parent) {
                    var textEl = parent.querySelector('.book-meta-new-info-item-text');
                    if (textEl) finished = (textEl.textContent || '').trim();
                }
                break;
            }
        }
        var tagEls = document.querySelectorAll('.book-introduction-title-tag-text');
        if (tagEls.length) category = [].map.call(tagEls, function(n){ return (n.textContent || '').trim(); }).filter(Boolean).join(' ');

        if (!title) { var og = document.querySelector('meta[property="og:title"]'); if (og && og.getAttribute('content')) title = og.getAttribute('content'); }
        if (!cover) { var og = document.querySelector('meta[property="og:image"]'); if (og && og.getAttribute('content')) cover = og.getAttribute('content'); }
        if (title === '番茄畅听') title = '';
        return { title: title, author: author, cover: cover, desc: desc, category: category, finished: finished };
    })();
    """

    def on_loaded(window):
        try:
            dom_cover = ""
            captured = ""

            # 先给浏览器 6 秒时间尝试高雅地抓取 API
            for _ in range(6):
                time.sleep(1)
                if not dom_cover:
                    dom_cover = window.evaluate_js(js_get_dom_cover)
                if not captured:
                    captured = window.evaluate_js(js_get_api_url)
                if captured:
                    break

            api_success = False
            if captured and isinstance(captured, str):
                is_audio_detail = "audio/detail" in captured
                headers = {
                    "Accept": "application/json",
                    "Referer": referer_origin + "/",
                    "Origin": referer_origin,
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                }
                resp = requests.get(captured, timeout=10, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    parsed = _parse_audio_detail_response(data) if is_audio_detail else _parse_get_info_response(data)
                    if parsed:
                        if dom_cover and isinstance(dom_cover, str):
                            parsed["cover"] = dom_cover
                        result.update(parsed)
                        api_success = True

            # ==== 核心逻辑：如果 API 获取失败，立即在同一个窗口里启动暴力兜底，绝不报错退回 ====
            if not api_success:
                # 模拟触摸点击“展开全部”，让网页把简介吐出来
                window.evaluate_js("""
                    var els = document.querySelectorAll('span, div, a, p');
                    for (var i = 0; i < els.length; i++) {
                        var txt = (els[i].textContent || '').trim();
                        if (txt === '展开' || txt === '展开全部') {
                            try { els[i].click(); } catch(e) {}
                        }
                    }
                """)
                time.sleep(1)
                # 直接强取数据
                fallback_data = window.evaluate_js(JS_FANQIE_FALLBACK)
                if fallback_data and isinstance(fallback_data, dict):
                    result.update({k: (fallback_data.get(k) or "") for k in
                                   ("title", "author", "cover", "desc", "category", "finished")})
                    # 强行补充我们拿到的高清封面
                    if dom_cover and isinstance(dom_cover, str) and not result.get("cover"):
                        result["cover"] = dom_cover
                else:
                    result["_error"] = "彻底提取失败，请确保链接为有效分享页。"

        except Exception as e:
            result["_error"] = str(e)
        finally:
            try:
                window.destroy()
            except:
                pass

    desktop_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    window = webview.create_window("番茄畅听 - 正在获取书籍信息", share_url, width=900, height=700)
    webview.start(on_loaded, window, user_agent=desktop_ua)

    # 无论成功失败，都将结果写入文件，通知主程序
    try:
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
    except Exception as e:
        result["_error"] = result.get("_error") or str(e)
        try:
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


def main():
    if len(sys.argv) < 4:
        sys.stderr.write("Usage: fetch_share_with_browser.py <url> <platform> <output_json_path>\n")
        sys.exit(1)
    url = sys.argv[1].strip()
    platform = (sys.argv[2].strip() or "qidian").lower()
    output_path = sys.argv[3].strip()
    if not url or not output_path:
        sys.stderr.write("url and output_json_path required\n")
        sys.exit(1)

    if platform == "fanqie":
        try:
            import webview
            import requests
        except ImportError:
            sys.stderr.write("pywebview and requests required. pip install pywebview requests\n")
            sys.exit(2)
        _run_fanqie_auto(url, output_path)
        sys.exit(0)

    try:
        import webview
    except ImportError:
        sys.stderr.write("pywebview not installed. Run: pip install pywebview\n")
        sys.exit(2)

    result = {"title": "", "author": "", "cover": "", "desc": "", "category": "", "finished": ""}

    # 起点听书分享页：从 DOM 提取
    JS_QIDIAN = r"""
    (function(){
        var title = document.title ? document.title.split('|')[0].trim() : '';
        var authorEl = document.querySelector('.subtitle-4') || document.querySelector('[class*="subtitle-4"]');
        var author = authorEl ? authorEl.textContent.trim() : '';
        var cover = '';
        var list = document.querySelectorAll('[style*="background-image"]');
        for (var i = 0; i < list.length; i++) {
            var s = list[i].getAttribute('style') || '';
            var m = s.match(/url\(['"]?(https:\/\/bookcover\.yuewen\.com[^'")]+)/);
            if (m) { cover = m[1]; break; }
        }
        var descEl = document.querySelector('[class*="_text_1fxmt"]') || document.querySelector('[class*="_text_"]');
        if (!descEl) { var divs = document.querySelectorAll('div'); for (var j = 0; j < divs.length; j++) { if (divs[j].innerText && divs[j].innerText.length > 100) { descEl = divs[j]; break; } } }
        var desc = descEl ? descEl.innerText.trim() : '';
        var cat = '', fin = '';
        var caps = document.querySelectorAll('.caption');
        for (var k = 0; k < caps.length; k++) {
            var t = caps[k].textContent.trim();
            if (t && t.indexOf('完') >= 0) fin = '完结';
            else if (t && t !== '简介' && !cat) cat = t;
        }
        return { title: title, author: author, cover: cover, desc: desc, category: cat, finished: fin };
    })();
    """

    # 番茄畅听：按分享页渲染后的 DOM 精确选择器提取，外加底层 JSON 透视挖掘
    JS_FANQIE = r"""
        (function(){
            var title = '', author = '', cover = '', desc = '', category = '', finished = '';
            var titleEl = document.querySelector('.book-meta-new-info-title');
            if (titleEl) title = (titleEl.innerText || titleEl.textContent || '').trim();
            var authorEl = document.querySelector('.book-meta-new-info-desc-author');
            if (authorEl) author = (authorEl.innerText || authorEl.textContent || '').trim();
            var imgEl = document.querySelector('.book-meta-new-img');
            if (imgEl && imgEl.src) cover = imgEl.src;

            // 1. 先获取表面上被截断的残缺简介
            var descEl = document.querySelector('.book-introduction-desc') || document.querySelector('.text-expand.book-introduction-desc');
            if (descEl) {
                desc = (descEl.innerText || descEl.textContent || '').trim();
                desc = desc.replace(/展开全部$/, '').replace(/展开$/, '').replace(/收起$/, '').trim();
            }

            // 2. 🚀 X光挖掘核心：遍历网页底层所有的 JS 数据，暴力抓取完整简介
            try {
                var scripts = document.querySelectorAll('script');
                // 正则匹配所有疑似包含完整简介的 JSON 字段
                var regex = /"(?:abstract|description|intro|content)"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"/g;
                for (var i = 0; i < scripts.length; i++) {
                    var txt = scripts[i].innerHTML || '';
                    var match;
                    while ((match = regex.exec(txt)) !== null) {
                        var val = match[1];
                        // 翻译底层的 Unicode 编码 (例如 \u53ea) 和转义字符
                        val = val.replace(/\\u([0-9a-fA-F]{4})/g, function(m, g) { return String.fromCharCode(parseInt(g, 16)); });
                        val = val.replace(/\\n/g, '\n').replace(/\\r/g, '').replace(/\\"/g, '"').replace(/\\\\/g, '\\');

                        // 特征比对：如果挖出来的数据包含残缺简介的前8个字，且长度更长，说明抓到了完整版！
                        var shortPrefix = desc.substring(0, 8).replace(/\s/g, '');
                        if (shortPrefix.length > 0 && val.replace(/\s/g, '').indexOf(shortPrefix) !== -1 && val.length > desc.length) {
                            desc = val.trim();
                        }
                    }
                }
            } catch(e) {}

            var descLabels = document.querySelectorAll('.book-meta-new-info-item-desc');
            for (var i = 0; i < descLabels.length; i++) {
                if ((descLabels[i].textContent || '').trim() === '更新状态') {
                    var parent = descLabels[i].parentElement;
                    if (parent) {
                        var textEl = parent.querySelector('.book-meta-new-info-item-text');
                        if (textEl) finished = (textEl.textContent || '').trim();
                    }
                    break;
                }
            }
            var tagEls = document.querySelectorAll('.book-introduction-title-tag-text');
            if (tagEls.length) category = [].map.call(tagEls, function(n){ return (n.textContent || '').trim(); }).filter(Boolean).join(' ');

            if (!title) { var og = document.querySelector('meta[property="og:title"]'); if (og && og.getAttribute('content')) title = og.getAttribute('content'); }
            if (!cover) { var og = document.querySelector('meta[property="og:image"]'); if (og && og.getAttribute('content')) cover = og.getAttribute('content'); }
            if (title === '番茄畅听') title = '';
            return { title: title, author: author, cover: cover, desc: desc, category: category, finished: finished };
        })();
        """

    js_code = JS_QIDIAN if platform == "qidian" else JS_FANQIE
    load_seconds = 12  # 等待页面渲染（加长等待时间以确保数据加载完毕）

    def on_loaded(window):
        time.sleep(load_seconds)
        try:
            # === 升级版：模拟真实的手机触摸事件，对抗 Vue/React 的事件拦截 ===
            window.evaluate_js("""
                var els = document.querySelectorAll('span, div, a, p');
                for (var i = 0; i < els.length; i++) {
                    var txt = (els[i].textContent || '').trim();
                    if (txt === '展开' || txt === '展开全部') {
                        try { els[i].dispatchEvent(new TouchEvent('touchstart', {bubbles: true})); } catch(e) {}
                        try { els[i].dispatchEvent(new TouchEvent('touchend', {bubbles: true})); } catch(e) {}
                        try { els[i].click(); } catch(e) {}
                    }
                }
            """)
            time.sleep(1.5)
            # ========================================================

            data = window.evaluate_js(js_code)
            if data:
                if isinstance(data, dict):
                    result.update({k: (data.get(k) or "") for k in ("title", "author", "cover", "desc", "category", "finished")})
                elif isinstance(data, str) and data:
                    result["title"] = data
        except Exception as e:
            result["_error"] = str(e)
        try:
            window.destroy()
        except Exception:
            pass

    # 使用桌面版 UA，避免起点/番茄识别为手机端后跳转「正在前往APP」、打开应用等
    desktop_ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    window = webview.create_window("分享页 - 加载中", url, width=900, height=700)
    webview.start(on_loaded, window, user_agent=desktop_ua)

    try:
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
    except Exception as e:
        result["_error"] = result.get("_error") or str(e)
        try:
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
    sys.exit(0)


if __name__ == "__main__":
    main()

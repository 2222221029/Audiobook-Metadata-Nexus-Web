# -*- coding: utf-8 -*-
"""请求 novelfm share/get_info API（使用带 share_id、X-Bogus、_signature 的完整 URL），将响应保存到 响应"""
import json
import os

from network_utils import get_safe_session

# 你提供的完整 URL（含签名，可能有时效）
FULL_URL = (
    "https://api5-sinfonlineb.novelfm.com/novelfm/playerapi/share/get_info/v1/"
    "?book_id=7387309822351264830&share_info_type=5&source_channel=link"
    "&share_id=bXgYgej18j5DoHfjOsk9NOfumLvBERjxnJYpmqmdlWI%3D"
    "&object_id=&aid=3040&msToken=&X-Bogus=DFSzswVYkjxANnQrC7bBBjTGOLCx"
    "&_signature=_02B4Z6wo00001h52CbgAAIDBQeMCdmiXRQoedg0AAO4O4e"
)

RESPONSE_FILE = os.path.join(os.path.dirname(__file__), "响应")


def main():
    print("正在请求 share/get_info（完整 URL）...")
    try:
        session = get_safe_session(platform_key="fanqie")
        resp = session.get(
            FULL_URL,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                "Accept": "application/json",
                "Referer": "https://novelfm.com/",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        with open(RESPONSE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        print(f"code={data.get('code')}, message={data.get('message', '')}")
        if data.get("code") == 0:
            info = (data.get("data") or {}).get("api_book_info") or {}
            print(f"已抓取: 《{info.get('book_name','')}》 {info.get('author','')}")
        print(f"响应已保存到: {RESPONSE_FILE}")
    except Exception as e:
        print(f"请求失败: {e}")
        raise


if __name__ == "__main__":
    main()

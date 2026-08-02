# config.py
import os
import sys
import json
import platform
import locale
from pathlib import Path

def get_base_dir():
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

def get_resource_dir():
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))

BASE_DIR = get_base_dir()
RESOURCE_DIR = get_resource_dir()

def find_ffmpeg_tools():
    tools = {'ffmpeg': None, 'ffprobe': None}
    exe_ext = '.exe' if sys.platform == 'win32' else ''
    search_paths = [
        BASE_DIR, os.path.join(BASE_DIR, 'ffmpeg'), os.path.join(BASE_DIR, 'bin'),
        os.path.join(BASE_DIR, 'tools'), RESOURCE_DIR,
    ]
    path_env = os.environ.get('PATH', '')
    path_entries = [p for p in path_env.split(os.pathsep) if p] if path_env else []
    if path_entries: search_paths.extend(path_entries)
    recursive_roots = {os.path.abspath(p) for p in search_paths[:5]}
    search_paths = list(dict.fromkeys([p for p in search_paths if p and os.path.exists(p)]))

    for search_path in search_paths:
        possible_files = [
            f'ffmpeg{exe_ext}', 'ffmpeg.exe', 'ffmpeg',
            f'ffprobe{exe_ext}', 'ffprobe.exe', 'ffprobe'
        ]
        for file in possible_files:
            file_path = os.path.join(search_path, file)
            if os.path.isfile(file_path) and os.access(file_path, os.X_OK):
                if 'ffmpeg' in file.lower(): tools['ffmpeg'] = file_path
                elif 'ffprobe' in file.lower(): tools['ffprobe'] = file_path
        if all(tools.values()): break
        try:
            if os.path.abspath(search_path) not in recursive_roots:
                continue
            for root, dirs, files in os.walk(search_path, followlinks=True):
                for file in files:
                    file_lower = file.lower()
                    if file_lower.startswith('ffmpeg') and (file_lower.endswith('.exe') or exe_ext == ''):
                        tools['ffmpeg'] = os.path.join(root, file)
                    elif file_lower.startswith('ffprobe') and (file_lower.endswith('.exe') or exe_ext == ''):
                        tools['ffprobe'] = os.path.join(root, file)
                if all(tools.values()): break
            if all(tools.values()): break
        except (PermissionError, OSError): continue
    return tools

ffmpeg_tools = find_ffmpeg_tools()
FFMPEG_PATH = ffmpeg_tools.get('ffmpeg') or os.path.join(BASE_DIR, "ffmpeg.exe")
FFPROBE_PATH = ffmpeg_tools.get('ffprobe') or os.path.join(BASE_DIR, "ffprobe.exe")

def check_ffmpeg_paths():
    return {
        'ffmpeg_found': os.path.exists(FFMPEG_PATH),
        'ffprobe_found': os.path.exists(FFPROBE_PATH),
        'ffmpeg_path': FFMPEG_PATH,
        'ffprobe_path': FFPROBE_PATH,
        'base_dir': BASE_DIR
    }

class ThemeConfig:
    PRIMARY_COLOR, PRIMARY_LIGHT, PRIMARY_DARK = "#4F46E5", "#6366F1", "#4338CA"
    SUCCESS_COLOR, WARNING_COLOR, ERROR_COLOR, INFO_COLOR = "#10B981", "#F59E0B", "#EF4444", "#3B82F6"
    BG_MAIN, BG_CARD, BG_LOG = "#F3F4F6", "#FFFFFF", "#F9FAFB"
    TEXT_MAIN, TEXT_SECONDARY, TEXT_LIGHT = "#111827", "#6B7280", "#FFFFFF"
    BORDER_COLOR, ROUND_CORNER = "#E5E7EB", 5
    system = platform.system()
    if system == "Windows": FONT_FAMILY = "Microsoft YaHei UI"
    elif system == "Darwin": FONT_FAMILY = "PingFang SC"
    else: FONT_FAMILY = "WenQuanYi Micro Hei"

CATEGORY_MAP = {
    "410": "相声评书", "411": "电台节目", "412": "玄幻奇幻", "413": "历史军事",
    "414": "武侠仙侠", "415": "都市娱乐", "416": "科幻末日", "417": "灵异悬疑",
    "418": "游戏竞技", "419": "轻小说", "420": "二次元", "421": "言情小说",
    "422": "文学出版", "401": "其他"
}
PLATFORM_LIST = ["喜马拉雅", "番茄畅听", "懒人听书", "起点听书", "酷我听书", "网易云听书", "云听fm", "蜻蜓fm", "荔枝fm", "猫耳fm"]
DEFAULT_DESC = "暂无简介信息"

_cpu_count = os.cpu_count()
MAX_WORKERS = max(1, (_cpu_count or 1) - 1)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__)) if __file__ else os.getcwd()

FANQIE_SHARE_ID = "bXgYgej18j5DoHfjOsk9NOfumLvBERjxnJYpmqmdlWI%3D"
FANQIE_X_BOGUS = ""
FANQIE_SIGNATURE = "_02B4Z6wo00001h52CbgAAIDBQeMCdmiXRQoedg0AAO4O4e"
FANQIE_JSSDK_COOKIE = os.environ.get("FANQIE_JSSDK_COOKIE", "")

XIMALAYA_API_TIMEOUT = 10
NETWORK_VERIFY_SSL = True
PLATFORM_VERIFY_SSL = {
    "default": NETWORK_VERIFY_SSL,
    "ximalaya": NETWORK_VERIFY_SSL,
    "lanren": NETWORK_VERIFY_SSL,
    "kuwo": NETWORK_VERIFY_SSL,
    "fanqie": NETWORK_VERIFY_SSL,
    "qidian": NETWORK_VERIFY_SSL,
    "netease": NETWORK_VERIFY_SSL,
    "yunting": NETWORK_VERIFY_SSL,
    "qingting": NETWORK_VERIFY_SSL,
}
XIMALAYA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Referer": "https://www.ximalaya.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"
}

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".flac", ".ogg", ".wav", ".aac", ".alac", ".wma"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
MAX_FILE_NAME_LENGTH = 200

PROGRESS_UPDATE_INTERVAL = 100
PROGRESS_COLORS = {"low": ThemeConfig.INFO_COLOR, "mid": ThemeConfig.WARNING_COLOR, "high": ThemeConfig.SUCCESS_COLOR}

LOG_FILE = os.path.join(BASE_DIR, "audio_processor.log")
LOG_LEVEL, LOG_FORMAT = "INFO", "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
LOG_MAX_BYTES, LOG_BACKUP_COUNT = 10 * 1024 * 1024, 5

DEFAULT_RENAME_EXT, DEFAULT_CHECK_CODEC, DEFAULT_DEBUG_MODE = True, False, False
DEFAULT_BITRATE, DEFAULT_FINISHED_STATUS = "auto", "完结"
DEFAULT_YEAR, DEFAULT_CATEGORY, DEFAULT_PLATFORM, DEFAULT_TEAM = "2024", "401", "喜马拉雅", "RL"
COVER_SIZE, COVER_QUALITY, TEMP_FILE_RETENTION_HOURS = (500, 500), 85, 24

def check_config():
    status = check_ffmpeg_paths()
    return status['ffmpeg_found'] and status['ffprobe_found']

COOKIE_FILE = os.path.join(BASE_DIR, "platform_cookies.json")
COOKIE_ENCRYPTED_FILE = os.path.join(BASE_DIR, "platform_cookies.enc")
COOKIE_KEYS = ("qidian", "netease")

def get_platform_cookies():
    encrypted_path = os.path.abspath(COOKIE_ENCRYPTED_FILE)
    plain_path = os.path.abspath(COOKIE_FILE)
    try:
        from cookie_store import read_encrypted_cookie_file, read_plain_cookie_file
        encrypted_data = read_encrypted_cookie_file(encrypted_path, COOKIE_KEYS)
        if encrypted_data is not None:
            return encrypted_data
        if os.path.exists(plain_path):
            return read_plain_cookie_file(plain_path, COOKIE_KEYS)
    except Exception as e: pass
    return {"qidian": "", "netease": ""}

def set_platform_cookies(cookies_dict):
    encrypted_path = os.path.abspath(COOKIE_ENCRYPTED_FILE)
    plain_path = os.path.abspath(COOKIE_FILE)
    try:
        from cookie_store import normalize_cookie_data, read_encrypted_cookie_file, read_plain_cookie_file, write_encrypted_cookie_file, write_plain_cookie_file
        data = get_platform_cookies()
        for k in COOKIE_KEYS:
            if k in cookies_dict: data[k] = (cookies_dict.get(k) or "").strip()
        data = normalize_cookie_data(data, COOKIE_KEYS)
        if write_encrypted_cookie_file(encrypted_path, data):
            return True
        return write_plain_cookie_file(plain_path, data)
    except Exception: return False

def get_system_encoding() -> str:
    try: return locale.getencoding()
    except (AttributeError, IndexError): return locale.getlocale()[1] or "gbk"
SYSTEM_ENCODING = get_system_encoding()

def get_audio_extensions(): return list(AUDIO_EXTENSIONS)
def get_image_extensions(): return list(IMAGE_EXTENSIONS)
def get_category_options(): return list(CATEGORY_MAP.items())
def get_platform_options(): return PLATFORM_LIST

if __name__ == "__main__":
    if check_config(): print("✅ 配置文件检查通过")
    else: print("⚠️ 配置文件存在警告，FFmpeg工具未找到")

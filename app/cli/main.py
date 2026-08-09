import argparse
import json
import logging
import os
import sys
import threading
from pathlib import Path

from app.processing.processor import process_audio_books


DEFAULT_CONFIG_PATH = "/config/process_params.json"
DEFAULT_INPUT_FOLDER = "/data"


class ConsoleProgress:
    def __init__(self):
        self.last_percent = None

    def __call__(self, percent, message):
        try:
            percent_text = f"{float(percent):5.1f}%"
        except Exception:
            percent_text = "  0.0%"
        if percent_text != self.last_percent or message:
            print(f"[progress] {percent_text} {message}", flush=True)
        self.last_percent = percent_text


def build_logger(verbose=False):
    logger = logging.getLogger("audiometa-nexus-cli")
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    return logger


def load_params(path):
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")
    with config_path.open("r", encoding="utf-8-sig") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("配置文件根节点必须是 JSON 对象")
    return data


def normalize_params(params, input_folder=None):
    normalized = dict(params)
    if input_folder:
        normalized["input_folder"] = input_folder
    normalized.setdefault("input_folder", DEFAULT_INPUT_FOLDER)
    normalized.setdefault("api_source", "喜马拉雅")
    normalized.setdefault("api_id", "")
    normalized.setdefault("subtitle", "")
    normalized.setdefault("target_format", "原格式保留")
    normalized.setdefault("bitrate", "auto")
    normalized.setdefault("check_codec", True)
    normalized.setdefault("rename_ext", True)
    normalized.setdefault("debug", True)
    normalized["check_codec"] = True
    normalized["rename_ext"] = True
    normalized["debug"] = True
    normalized.setdefault("manual_cover_path", "")
    normalized.setdefault("manual_desc", "")
    normalized.setdefault("series_name", "")
    normalized.setdefault("series_number", "")
    normalized.setdefault("album_tags", [])
    normalized.setdefault("team", "RL")
    normalized.setdefault("fetched_metadata", {})
    return normalized


def validate_params(params):
    required = ("input_folder", "title", "author", "anchor", "category", "platform", "year", "finished")
    missing = [key for key in required if not str(params.get(key, "")).strip()]
    if missing:
        raise ValueError("配置缺少必填字段: " + ", ".join(missing))
    input_folder = params["input_folder"]
    if not os.path.isdir(input_folder):
        raise FileNotFoundError(f"音频目录不存在或不是文件夹: {input_folder}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="AudioMeta Nexus Docker 批处理入口")
    parser.add_argument("--config", default=os.environ.get("PROCESS_CONFIG", DEFAULT_CONFIG_PATH), help="process_params.json 路径")
    parser.add_argument("--input-folder", default=os.environ.get("INPUT_FOLDER"), help="覆盖配置中的音频目录")
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    args = parser.parse_args(argv)

    logger = build_logger(args.verbose)
    try:
        params = normalize_params(load_params(args.config), args.input_folder)
        validate_params(params)
        failed_items = []

        def record_failed(file_path, error_msg):
            failed_items.append({"file": file_path, "error": error_msg})
            logger.error(f"处理失败: {file_path} | {error_msg}")

        result = process_audio_books(
            params,
            logger,
            progress_callback=ConsoleProgress(),
            failed_audios_callback=record_failed,
            stop_event=threading.Event(),
        )
        if result is None:
            logger.error("批处理未完成，请检查音频目录、ffmpeg 和配置参数。")
            return 2
        if result and result.get("error"):
            return 2
        if failed_items:
            return 1
        return 0
    except Exception as exc:
        logger.error(f"Docker 批处理启动失败: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

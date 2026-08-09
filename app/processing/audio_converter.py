# audio_converter.py
import os
import subprocess
import tempfile
import uuid
import shutil
from app.core.config import FFMPEG_PATH, SYSTEM_ENCODING

# 格式对应的编解码器映射
CODEC_MAP = {
    "MP3": "libmp3lame",
    "M4A": "aac",
    "AAC": "aac",
    "OPUS": "libopus",
    "FLAC": "flac",
    "WAV": "pcm_s16le"
}

AUTO_BITRATE_VALUES = {"", "auto", "自动检测", "跳过升频", "原码率", "保持原码率"}

def is_auto_bitrate(value: str) -> bool:
    return (value or "").strip().lower() in {v.lower() for v in AUTO_BITRATE_VALUES}

def normalize_conversion_options(input_file: str, target_format: str, target_bitrate: str) -> tuple:
    target_format = (target_format or "原格式保留").strip()
    target_bitrate = (target_bitrate or "auto").strip()

    ext = ".m4a" if target_format == "AAC" else f".{target_format.lower()}"
    if target_format == "原格式保留":
        ext = os.path.splitext(input_file)[1].lower()
        target_format = ext.lstrip(".").upper()

    return target_format, target_bitrate, ext

def build_ffmpeg_conversion_command(input_file: str, output_file: str, target_format: str, target_bitrate: str) -> list:
    target_format, target_bitrate, _ = normalize_conversion_options(input_file, target_format, target_bitrate)
    codec = CODEC_MAP.get(target_format, "copy")

    cmd = [FFMPEG_PATH, "-y", "-hide_banner", "-loglevel", "error", "-i", input_file]
    cmd.extend(["-map", "0", "-map_metadata", "0"])
    cmd.extend(["-c:a", codec])

    if target_format not in ["FLAC", "WAV"] and not is_auto_bitrate(target_bitrate):
        bitrate_val = target_bitrate.lower().replace("kbps", "k")
        if "k" not in bitrate_val:
            bitrate_val += "k"

        if codec == "libmp3lame":
            cmd.extend([
                "-b:a", bitrate_val,
                "-minrate", bitrate_val,
                "-maxrate", bitrate_val,
                "-bufsize", bitrate_val,
                "-ar", "44100",
                "-ac", "2"
            ])
        elif codec == "aac":
            cmd.extend([
                "-b:a", bitrate_val,
                "-ar", "44100",
                "-ac", "2"
            ])

    if target_format in ["MP3", "FLAC"]:
        cmd.extend(["-c:v", "copy", "-id3v2_version", "3"])
    elif target_format in ["M4A", "AAC"]:
        cmd.extend(["-c:v", "copy", "-disposition:v", "attached_pic"])

    cmd.append(output_file)
    return cmd

def convert_audio_format_and_bitrate(input_file: str, target_format: str, target_bitrate: str, logger=None) -> tuple:
    """
    高质量音频转换引擎，支持 MP3/WAV/FLAC/M4A/AAC 互转及码率调整，并保留完整元数据。
    返回: (成功布尔值, 最终文件路径, 错误信息)
    """
    target_format = (target_format or "原格式保留").strip()
    target_bitrate = (target_bitrate or "auto").strip()

    if target_format == "原格式保留" and is_auto_bitrate(target_bitrate):
        return True, input_file, "无需转换"

    ext = ".m4a" if target_format == "AAC" else f".{target_format.lower()}"
    if target_format == "原格式保留":
        ext = os.path.splitext(input_file)[1].lower()
        target_format = ext.lstrip(".").upper()

    output_file = os.path.join(
        os.path.dirname(input_file), 
        f"_cvt_{uuid.uuid4().hex[:8]}{ext}"
    )

    cmd = build_ffmpeg_conversion_command(input_file, output_file, target_format, target_bitrate)

    try:
        startupinfo = None
        if os.name == 'nt':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
            
        result = subprocess.run(cmd, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, 
                                encoding=SYSTEM_ENCODING, errors="ignore", timeout=300, startupinfo=startupinfo)
        
        if result.returncode == 0 and os.path.exists(output_file):
            return True, output_file, None
        else:
            if os.path.exists(output_file): os.remove(output_file)
            return False, input_file, result.stderr or "未知转换错误"
    except Exception as e:
        if os.path.exists(output_file): os.remove(output_file)
        return False, input_file, str(e)

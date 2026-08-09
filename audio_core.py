# audio_core.py
import os
import sys
import struct
import json
import tempfile
import subprocess
import time
import uuid
import shutil
import re
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor
from PIL import Image
from config import FFMPEG_PATH, FFPROBE_PATH, BASE_DIR, MAX_WORKERS, SYSTEM_ENCODING
from network_utils import get_safe_session, clean_html_tags
from api_clients import (ximalaya_api, lanren_api, kuwo_api, fanqie_api, qidian_api, netease_ting_api, yunting_api, qingting_api)

def replace_file_safely(source_path: str, target_path: str) -> None:
    if not os.path.exists(source_path) or os.path.getsize(source_path) <= 0:
        raise FileNotFoundError(f"Replacement source is missing or empty: {source_path}")

    backup_path = None
    if os.path.exists(target_path):
        backup_path = f"{target_path}.bak_{uuid.uuid4().hex}"
        os.replace(target_path, backup_path)

    try:
        os.replace(source_path, target_path)
    except Exception:
        if backup_path and os.path.exists(backup_path) and not os.path.exists(target_path):
            os.replace(backup_path, target_path)
        raise
    else:
        if backup_path and os.path.exists(backup_path):
            os.remove(backup_path)

def check_ffmpeg_tools(logger) -> bool:
    ffmpeg_exists, ffprobe_exists = os.path.exists(FFMPEG_PATH), os.path.exists(FFPROBE_PATH)
    if ffmpeg_exists and ffprobe_exists:
        startupinfo = None
        if sys.platform == "win32":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
        try:
            # Containerized installs can take a little longer to cold-start (for
            # example when the binary and its shared libraries are on a mounted
            # layer). Keep this as a bounded probe, but avoid rejecting a healthy
            # tool merely because the five-second desktop timeout was too strict.
            ffmpeg_result = subprocess.run([FFMPEG_PATH, "-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False, timeout=20, startupinfo=startupinfo)
            ffprobe_result = subprocess.run([FFPROBE_PATH, "-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False, timeout=20, startupinfo=startupinfo)
            if ffmpeg_result.returncode == 0 and ffprobe_result.returncode == 0:
                logger.info(f"✅ FFmpeg工具验证通过\n   FFmpeg路径: {FFMPEG_PATH}\n   ffprobe路径: {FFPROBE_PATH}")
                ffmpeg_output = ffmpeg_result.stdout.decode('utf-8', errors='ignore')
                if ffmpeg_output: logger.info(f"   FFmpeg版本: {(ffmpeg_output.split('\n')[0] if '\n' in ffmpeg_output else ffmpeg_output[:50]).strip()}")
                return True
        except (subprocess.TimeoutExpired, FileNotFoundError, PermissionError) as e:
            logger.warning(f"⚠️ FFmpeg工具测试失败: {str(e)}")
    logger.error("❌ FFmpeg工具检查失败")
    return False

def get_wav_bitrate(file_path: str) -> tuple:
    try:
        with open(file_path, 'rb') as f:
            riff_header = f.read(12)
            if len(riff_header) < 12 or riff_header[:4] != b'RIFF' or riff_header[8:12] != b'WAVE': return False, "0kbps", "WAV", 300.0
            while True:
                chunk_header = f.read(8)
                if len(chunk_header) < 8: break
                chunk_id, chunk_size = chunk_header[:4], struct.unpack('<I', chunk_header[4:8])[0]
                if chunk_id == b'fmt ':
                    fmt_data = f.read(min(chunk_size, 16))
                    if len(fmt_data) >= 16:
                        audio_format = struct.unpack('<H', fmt_data[0:2])[0]
                        num_channels = struct.unpack('<H', fmt_data[2:4])[0]
                        sample_rate = struct.unpack('<I', fmt_data[4:8])[0]
                        byte_rate = struct.unpack('<I', fmt_data[8:12])[0]
                        bits_per_sample = struct.unpack('<H', fmt_data[14:16])[0]
                        bitrate_bps = byte_rate * 8
                        codec = {1: "PCM", 3: "IEEE_FLOAT", 6: "ALAW", 7: "ULAW"}.get(audio_format, f"WAV_{audio_format}")
                        f.seek(12)
                        while True:
                            data_header = f.read(8)
                            if len(data_header) < 8: break
                            data_id, data_size = data_header[:4], struct.unpack('<I', data_header[4:8])[0]
                            if data_id == b'data':
                                if byte_rate > 0: duration = data_size / byte_rate
                                elif sample_rate > 0 and num_channels > 0 and bits_per_sample > 0: duration = data_size / (sample_rate * num_channels * (bits_per_sample / 8))
                                else: duration = 300.0
                                return True, f"{int(bitrate_bps / 1000)}kbps" if bitrate_bps > 0 else "0kbps", codec, duration
                            f.seek(data_size, 1)
                        return True, f"{int(bitrate_bps / 1000)}kbps" if bitrate_bps > 0 else "0kbps", codec, 300.0
                    break
                else: f.seek(chunk_size, 1)
            return False, "0kbps", "WAV", 300.0
    except Exception: return False, "0kbps", "WAV", 300.0

def batch_get_audio_info(audio_list: list, logger, progress_callback=None) -> dict:
    audio_info = {}
    total_files = len(audio_list)
    logger.info(f"📊 开始获取音频信息（共{total_files}个文件）...")
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_list = [(executor.submit(get_single_audio_info, file), file) for file in audio_list]
        for i, (future, file_path) in enumerate(future_list, 1):
            try:
                audio_info[file_path] = future.result()
                if progress_callback and i % 5 == 0: progress_callback((i / total_files) * 100, f"获取音频信息：{i}/{total_files}")
                if i % 20 == 0: logger.info(f"📊 获取音频信息进度：{i}/{total_files} ({(i / total_files) * 100:.1f}%)")
            except Exception as e:
                logger.warning(f"⚠️ 获取{os.path.basename(file_path)}信息失败：{str(e)}")
                audio_info[file_path] = {"codec": "AAC", "bitrate": "0kbps", "duration": 300.0}
    logger.info(f"✅ 音频信息获取完成（成功{len(audio_info)}/{total_files}）")
    return audio_info

def get_single_audio_info(file_path: str) -> dict:
    file_ext = os.path.splitext(file_path)[1].lower()
    if file_ext == '.wav':
        success, bitrate_str, codec, duration = get_wav_bitrate(file_path)
        if success: return {"codec": codec, "bitrate": bitrate_str, "duration": duration}
        else: return {"codec": "WAV", "bitrate": "0kbps", "duration": 300.0}
    try:
        import mutagen
        audio = mutagen.File(file_path)
        if audio is not None and audio.info is not None:
            duration = float(audio.info.length)
            bitrate_bps = getattr(audio.info, 'bitrate', 0)
            codec = {'.mp3': 'MP3', '.m4a': 'AAC', '.flac': 'FLAC', '.aac': 'AAC', '.ogg': 'OGG'}.get(file_ext, 'AAC')
            return {"codec": codec, "bitrate": f"{int(bitrate_bps / 1000)}kbps" if bitrate_bps > 0 else "128kbps", "duration": duration}
    except Exception: pass

    if not os.path.exists(FFPROBE_PATH): raise Exception(f"ffprobe工具不存在: {FFPROBE_PATH}")
    startupinfo = None
    if sys.platform == "win32":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
    try:
        cmd = [FFPROBE_PATH, "-v", "quiet", "-select_streams", "a:0", "-show_entries", "stream=codec_name,bit_rate:format=duration,bit_rate", "-of", "json", file_path]
        result = subprocess.run(cmd, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding=SYSTEM_ENCODING, errors="ignore", timeout=30, startupinfo=startupinfo)
        if result.returncode != 0: raise Exception(f"ffprobe执行失败: {result.stderr[:200] if result.stderr else '未知'}")
        data = json.loads(result.stdout)
        streams = data.get("streams", [{}])[0]
        format_data = data.get("format", {})
        codec = streams.get("codec_name", "aac").upper()
        duration = float(format_data.get("duration", 300.0))
        bitrate_bps = int(streams["bit_rate"]) if streams.get("bit_rate") and streams["bit_rate"].isdigit() else int(format_data["bit_rate"]) if format_data.get("bit_rate") and format_data["bit_rate"].isdigit() else int((os.path.getsize(file_path) * 8) / duration) if duration > 0 else 0
        return {"codec": codec, "bitrate": f"{int(bitrate_bps / 1000)}kbps" if bitrate_bps > 0 else "0kbps", "duration": duration}
    except Exception as e:
        try:
            file_size = os.path.getsize(file_path)
            codec = {'.mp3': 'MP3', '.m4a': 'AAC', '.flac': 'FLAC', '.aac': 'AAC', '.ogg': 'OGG', '.wav': 'WAV'}.get(file_ext, 'AAC')
            bitrate_bps = int((file_size * 8) / 600.0)
            return {"codec": codec, "bitrate": f"{int(bitrate_bps / 1000)}kbps" if bitrate_bps > 0 else "128kbps", "duration": 600.0}
        except Exception: return {"codec": "AAC", "bitrate": "128kbps", "duration": 300.0}

def get_audio_list(folder: str) -> (list, set):
    audio_exts = {"mp3", "m4a", "flac", "ogg", "wav", "aac", "alac", "wma"}
    files, found_formats = [], set()
    for root, _, files_in_dir in os.walk(folder):
        for file in files_in_dir:
            ext = os.path.splitext(file)[1].lower().lstrip(".")
            if ext in audio_exts:
                files.append(os.path.join(root, file))
                found_formats.add(ext.upper())
    return files, found_formats

def calculate_bitrate_range(audio_info: dict, found_formats: set) -> str:
    bitrate_values, wav_files = [], []
    standard_formats = {"MP3", "M4A", "FLAC", "WAV", "AAC", "OGG", "ALAC", "WMA", "OPUS"}
    normalized_formats = set()
    for fmt in found_formats:
        fmt_upper = fmt.upper()
        if fmt_upper in ["MPEG", "MP3"]: normalized_formats.add("MP3")
        elif fmt_upper in ["M4A", "MP4", "AAC"]: normalized_formats.add("M4A")
        elif fmt_upper in ["FLAC", "WAV", "WAVE", "OGG", "VORBIS", "ALAC", "WMA", "OPUS"]: normalized_formats.add(fmt_upper if fmt_upper in standard_formats else ("WAV" if fmt_upper == "WAVE" else "OGG"))
        else: normalized_formats.add(fmt_upper)
    format_str = "&".join(sorted(normalized_formats or {fmt.upper() for fmt in found_formats})) if found_formats else "未知格式"
    for file_path, info in audio_info.items():
        bitrate_str = info.get("bitrate", "0kbps")
        match = re.search(r'(\d+)', bitrate_str)
        if match and int(match.group(1)) > 0: bitrate_values.append(int(match.group(1)))
        else: wav_files.append(os.path.basename(file_path))
    if not bitrate_values: return f"{format_str}@0kbps" if wav_files and len(wav_files) == len(audio_info) else f"{format_str}@未知码率"
    min_br, max_br = min(bitrate_values), max(bitrate_values)
    wav_note = "(本专辑存在无法识别的音频)" if wav_files else ""
    return f"{format_str}@{min_br}kbps{wav_note}" if min_br == max_br else f"{format_str}@{min_br}~{max_br}kbps{wav_note}"

def create_metadata_file(tags: dict) -> str:
    filtered_tags = {}
    exclude_patterns = [tag.lower().rstrip(':') for tag in ["ENCODEDBY:", "PODCASTDESC:", "DESC:", "LANG:", "ENCODER:", "PUBLISHER:", "©LAN", "©PUB"]]
    for key, value in tags.items():
        if not any(p in key.lower() for p in exclude_patterns) and value:
            if key.lower() == "comment": filtered_tags[key] = tags.get("DESCRIPTION", clean_html_tags(""))
            elif key.lower() == "encoder": filtered_tags[key] = tags.get("ENCODING", "AAC")
            elif key.lower() == "track": filtered_tags[key] = tags.get("track", f"1/{tags.get('TRACKTOTAL', '54')}")
            else: filtered_tags[key] = value
    if "comment" not in filtered_tags: filtered_tags["comment"] = filtered_tags.get("DESCRIPTION", clean_html_tags(""))
    if "encoder" not in filtered_tags: filtered_tags["encoder"] = filtered_tags.get("ENCODING", "AAC")
    if "track" not in filtered_tags: filtered_tags["track"] = f"1/{filtered_tags.get('TRACKTOTAL', '54')}"
    if "subtitle" in tags and tags["subtitle"]: filtered_tags["subtitle"] = tags["subtitle"]
    if "grouping" in tags and tags["grouping"]: filtered_tags["grouping"] = tags["grouping"]
    temp_fd, temp_path = tempfile.mkstemp(suffix=".txt", prefix="ffmeta_", text=True)
    os.close(temp_fd)
    with open(temp_path, "w", encoding="utf-8") as f:
        f.write(";FFMETADATA1\n")
        for key, value in filtered_tags.items(): f.write(f"{key}={value.replace(';', '\\;').replace('#', '\\#').replace(chr(10), '\\n')}\n")
    return temp_path

def write_tags_and_cover(audio_path: str, tags: dict, cover_data: bytes = None, logger=None) -> bool:
    audio_ext = os.path.splitext(audio_path)[1].lower()
    desc_value = tags.get("DESCRIPTION", clean_html_tags(tags.get("comment", "")))
    try:
        import mutagen
        success = False
        if audio_ext == '.mp3':
            # 引入了缺少的 TCOM 模块
            from mutagen.id3 import ID3, TIT2, TPE1, TPE2, TALB, TCON, TDRC, TRCK, COMM, APIC, TCOM
            from mutagen.mp3 import MP3
            audio = MP3(audio_path)
            if audio.tags is None: audio.add_tags()
            audio.tags.add(TIT2(encoding=3, text=tags.get("title", "")))
            audio.tags.add(TPE1(encoding=3, text=tags.get("artist", "")))
            audio.tags.add(TPE2(encoding=3, text=tags.get("album_artist", "")))
            # 关键修复：写入作曲家（演播）
            audio.tags.add(TCOM(encoding=3, text=tags.get("composer", "")))
            audio.tags.add(TALB(encoding=3, text=tags.get("album", "")))
            audio.tags.add(TCON(encoding=3, text=tags.get("genre", "")))
            audio.tags.add(TDRC(encoding=3, text=tags.get("date", "")))
            audio.tags.add(TRCK(encoding=3, text=tags.get("track", "1/1")))
            audio.tags.add(COMM(encoding=3, lang='chi', desc='', text=desc_value))
            if cover_data: audio.tags.add(APIC(encoding=3, mime='image/jpeg', type=3, desc='Cover', data=cover_data))
            audio.save()
            success = True
        elif audio_ext == '.m4a':
            from mutagen.mp4 import MP4, MP4Cover
            audio = MP4(audio_path)
            if audio.tags is None: audio.add_tags()
            audio.tags['\xa9nam'] = tags.get("title", "")
            audio.tags['\xa9ART'] = tags.get("artist", "")
            audio.tags['aART'] = tags.get("album_artist", "")
            # 关键修复：写入作曲家（M4A 的内部代码为 ©wrt）
            audio.tags['\xa9wrt'] = tags.get("composer", "")
            audio.tags['\xa9alb'] = tags.get("album", "")
            audio.tags['\xa9gen'] = tags.get("genre", "")
            audio.tags['\xa9day'] = tags.get("date", "")
            audio.tags['\xa9cmt'] = desc_value
            try: audio.tags['trkn'] = [tuple(map(int, tags.get("track", "1/1").split('/')))]
            except: pass
            if cover_data: audio.tags['covr'] = [MP4Cover(cover_data, imageformat=MP4Cover.FORMAT_JPEG)]
            audio.save()
            success = True
        elif audio_ext == '.flac':
            from mutagen.flac import FLAC, Picture
            audio = FLAC(audio_path)
            audio['title'] = tags.get("title", "")
            audio['artist'] = tags.get("artist", "")
            audio['albumartist'] = tags.get("album_artist", "")
            # 关键修复：写入作曲家
            audio['composer'] = tags.get("composer", "")
            audio['album'] = tags.get("album", "")
            audio['genre'] = tags.get("genre", "")
            audio['date'] = tags.get("date", "")
            audio['tracknumber'] = tags.get("track", "1/1").split('/')[0]
            audio['tracktotal'] = tags.get("track", "1/1").split('/')[1] if '/' in tags.get("track", "1/1") else ""
            audio['description'] = desc_value
            if cover_data:
                pic = Picture()
                pic.type, pic.mime, pic.desc, pic.data = 3, "image/jpeg", "Cover", cover_data
                audio.clear_pictures()
                audio.add_picture(pic)
            audio.save()
            success = True
        if success: return True
    except ImportError: pass
    except Exception as e:
        if logger: logger.warning(f"⚠️ Mutagen 写入异常: {str(e)}")

    if not os.path.exists(FFMPEG_PATH): return False
    temp_audio, temp_cover, meta_file = os.path.join(os.path.dirname(audio_path), f"_temp_{uuid.uuid4().hex}{os.path.splitext(audio_path)[1]}"), None, None
    audio_format = audio_ext.lstrip(".")
    try:
        meta_file = create_metadata_file(tags)
        cmd = [FFMPEG_PATH, "-y", "-hide_banner", "-loglevel", "error", "-i", audio_path, "-i", meta_file]
        if cover_data:
            temp_cover = os.path.join(tempfile.gettempdir(), f"_temp_cover_{int(time.time() * 1000)}_{os.getpid()}.jpg")
            with open(temp_cover, "wb") as f: f.write(cover_data)
            cmd.extend(["-i", temp_cover])
            if audio_format in ("mp3", "flac"):
                cmd.extend(["-map", "2:v", "-c:v", "mjpeg", "-metadata:s:v", 'title="Album cover"', "-metadata:s:v", 'comment="Cover (front)"'])
                if audio_format == "mp3": cmd.insert(-4, "-id3v2_version"); cmd.insert(-3, "3")
            elif audio_format == "m4a": cmd.extend(["-map", "2:v", "-c:v", "mjpeg", "-disposition:v", "attached_pic", "-metadata:s:v", 'title="Album cover"', "-metadata:s:v", 'comment="Cover (front)"'])
        cmd.extend(["-map", "0:a", "-map_metadata", "1", "-c:a", "copy"])
        if tags.get("publisher", ""): cmd.extend(["-metadata", f"publisher={tags['publisher']}"])
        if tags.get("language", "chi"): cmd.extend(["-metadata", f"language={tags.get('language', 'chi')}"])
        if tags.get("subtitle", ""): cmd.extend(["-metadata", f"subtitle={tags['subtitle']}"])
        if tags.get("grouping", ""): cmd.extend(["-metadata", f"grouping={tags['grouping']}"])
        cmd.extend(["-metadata:s:a", "language=chi", "-metadata", f"comment={desc_value}", "-metadata", f"encoder={tags.get('ENCODING', 'AAC')}", "-metadata", f"track={tags.get('track', '1/1')}", temp_audio])
        startupinfo = None
        if sys.platform == "win32":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
        result = subprocess.run(cmd, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding=SYSTEM_ENCODING, errors="ignore", timeout=120, startupinfo=startupinfo)
        if result.returncode != 0: return False
        if os.path.exists(temp_audio) and os.path.getsize(temp_audio) > 0:
            replace_file_safely(temp_audio, audio_path)
            return True
        return False
    except Exception: return False
    finally:
        for file in [temp_audio, temp_cover, meta_file]:
            if file and os.path.exists(file):
                try: os.remove(file)
                except: pass

def convert_cover(cover_data: bytes) -> bytes:
    try:
        with Image.open(BytesIO(cover_data)) as img:
            if img.mode in ("RGBA", "P", "L"): img = img.convert("RGB")
            output = BytesIO()
            img.save(output, format="JPEG", quality=95)
            return output.getvalue()
    except Exception: return cover_data

def save_cover_to_folder(cover_data: bytes, folder: str, logger) -> None:
    try:
        with open(os.path.join(folder, "cover.jpg"), "wb") as f: f.write(cover_data)
        if logger: logger.info(f"✅ 封面已保存：{os.path.join(folder, 'cover.jpg')}")
    except Exception as e:
        if logger: logger.error(f"❌ 保存封面失败：{str(e)}")

def load_manual_cover(cover_path: str) -> bytes:
    if not cover_path or not os.path.exists(cover_path): return None
    try:
        with open(cover_path, "rb") as f: return convert_cover(f.read())
    except: return None

def get_image_resolution(img_bytes: bytes) -> int:
    try:
        with Image.open(BytesIO(img_bytes)) as img: return img.width * img.height
    except: return 0

def find_cover(folder: str, api_id: str = None, api_source: str = "喜马拉雅", logger=None, manual_cover_path: str = None) -> bytes:
    manual_cover = load_manual_cover(manual_cover_path)
    if manual_cover:
        if logger: logger.info(f"✅ 使用手动选择的封面 (绝对优先级)")
        save_cover_to_folder(manual_cover, folder, logger)
        return manual_cover

    local_cover_data, local_cover_path = None, None
    for candidate in [os.path.join(folder, f"{name}.{ext}") for name in ["cover", "封面", "album", "albumart", "artwork"] for ext in ["jpg", "jpeg", "png", "bmp", "webp"]]:
        if os.path.exists(candidate):
            try:
                with open(candidate, "rb") as f: local_cover_data = convert_cover(f.read())
                local_cover_path = candidate
                break
            except: pass

    api_cover_data = None
    if api_id:
        if logger: logger.info(f"🌐 尝试从{api_source}抓取封面并与本地对比...")
        try:
            cover_url, album_data = None, {}
            if api_source == "喜马拉雅": cover_url = ximalaya_api("album", api_id).get("albumPageMainInfo", {}).get("cover")
            elif api_source == "懒人听书": cover_url = lanren_api(api_id).get("bestCover")
            elif api_source == "酷我听书": cover_url = kuwo_api(api_id).get("pic")
            elif api_source == "番茄畅听": cover_url = fanqie_api(api_id).get("cover") or fanqie_api(api_id).get("pic") or fanqie_api(api_id).get("bestCover")
            elif api_source == "起点听书": cover_url = qidian_api(api_id).get("cover") or qidian_api(api_id).get("pic") or qidian_api(api_id).get("bestCover")
            elif api_source == "网易云听书": cover_url = netease_ting_api(api_id).get("cover") or netease_ting_api(api_id).get("pic") or netease_ting_api(api_id).get("bestCover")
            elif api_source == "云听fm": cover_url = yunting_api(api_id).get("cover") or yunting_api(api_id).get("pic") or yunting_api(api_id).get("bestCover")
            elif api_source == "蜻蜓fm": cover_url = qingting_api(api_id).get("cover") or qingting_api(api_id).get("pic") or qingting_api(api_id).get("bestCover")
            if cover_url:
                cover_url = f"https:{cover_url}" if cover_url.startswith("//") else cover_url
                if not cover_url.startswith("http"): cover_url = f"https://{cover_url}" if not cover_url.startswith("/") else f"https://m.lrts.me{cover_url}"
                resp = get_safe_session().get(cover_url, timeout=8)
                if resp.status_code == 200: api_cover_data = convert_cover(resp.content)
        except Exception as e:
            if logger: logger.warning(f"⚠️ {api_source}封面获取失败：{str(e)}")

    best_cover_data = None
    if local_cover_data and api_cover_data:
        res_local, res_api = get_image_resolution(local_cover_data), get_image_resolution(api_cover_data)
        if res_api > res_local:
            if logger: logger.info(f"✅ API封面分辨率 ({res_api}) > 本地 ({res_local})，覆盖本地封面")
            best_cover_data = api_cover_data
        else:
            if logger: logger.info(f"✅ 本地封面分辨率 ({res_local}) >= API ({res_api})，使用本地封面")
            best_cover_data = local_cover_data
    elif local_cover_data:
        if logger: logger.info(f"✅ 找到文件夹内封面：{os.path.basename(local_cover_path)}")
        best_cover_data = local_cover_data
    elif api_cover_data:
        if logger: logger.info(f"✅ 从{api_source}抓取封面成功")
        best_cover_data = api_cover_data

    if best_cover_data:
        save_cover_to_folder(best_cover_data, folder, logger)
        return best_cover_data
    if logger: logger.warning("⚠️ 未找到封面，跳过嵌入")
    return None

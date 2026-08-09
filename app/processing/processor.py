# processor.py - AudioMeta Nexus
import os
import sys
import json
import time
import shutil
import traceback
from concurrent.futures import ThreadPoolExecutor
import re
import uuid
from app.core.config import BASE_DIR, CATEGORY_MAP, DEFAULT_DESC, MAX_WORKERS
from app.integrations.network_utils import fix_ssl_context, clean_html_tags
from app.integrations.api_clients import ximalaya_api, lanren_api, kuwo_api, fanqie_api, qidian_api, netease_ting_api, yunting_api, qingting_api
from app.processing.audio_core import check_ffmpeg_tools, get_audio_list, batch_get_audio_info, calculate_bitrate_range, find_cover, write_tags_and_cover, replace_file_safely
from app.processing.metadata_helpers import build_output_folder_name, join_people_for_tag, split_people

def replace_converted_audio(original_path: str, converted_path: str) -> str:
    new_ext = os.path.splitext(converted_path)[1]
    target_path = os.path.splitext(original_path)[0] + new_ext

    if os.path.abspath(target_path) == os.path.abspath(original_path):
        replace_file_safely(converted_path, original_path)
        return original_path

    stamp = f"{time.strftime('%Y%m%d%H%M%S')}_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    original_backup = f"{original_path}.bak_{stamp}"
    target_backup = f"{target_path}.bak_{stamp}" if os.path.exists(target_path) else None

    os.replace(original_path, original_backup)
    try:
        if target_backup:
            os.replace(target_path, target_backup)
        os.replace(converted_path, target_path)
    except Exception:
        if os.path.exists(target_path):
            try: os.remove(target_path)
            except: pass
        if target_backup and os.path.exists(target_backup):
            os.replace(target_backup, target_path)
        if os.path.exists(original_backup) and not os.path.exists(original_path):
            os.replace(original_backup, original_path)
        raise

    for backup in (original_backup, target_backup):
        if backup and os.path.exists(backup):
            os.remove(backup)
    return target_path

def save_desc_file(folder_path: str, description: str, source: str, logger=None) -> bool:
    try:
        with open(os.path.join(folder_path, "desc.txt"), 'w', encoding='utf-8') as f: f.write(description.strip())
        if logger:
            source_text = {'API': '网络API获取', 'Manual': '手动输入', 'Default': '默认描述'}.get(source, '未知来源')
            logger.info(f"✅ 简介已保存到desc.txt (来源: {source_text})\n📄 简介字数: {len(description)}")
        return True
    except Exception as e:
        if logger: logger.error(f"❌ 保存desc.txt文件失败: {str(e)}")
        return False

def save_reader_file(folder_path: str, anchor: str, logger=None) -> bool:
    try:
        with open(os.path.join(folder_path, "reader.txt"), 'w', encoding='utf-8') as f: f.write(anchor.strip())
        if logger: logger.info(f"✅ 演播艺术家已保存到reader.txt")
        return True
    except Exception as e:
        if logger: logger.error(f"❌ 保存reader.txt文件失败: {str(e)}")
        return False

def save_process_params(folder_path: str, params: dict, clean_desc: str, desc_source: str, logger=None) -> bool:
    try:
        params_to_save = params.copy()
        params_to_save.update({'clean_desc': clean_desc, 'desc_source': desc_source, '_saved_at': time.strftime("%Y-%m-%d %H:%M:%S"), '_tool_version': "AudioMeta Nexus"})
        with open(os.path.join(folder_path, "process_params.json"), 'w', encoding='utf-8') as f:
            json.dump(params_to_save, f, ensure_ascii=False, indent=2, default=str)
        if logger: logger.info(f"✅ 处理参数已保存到: process_params.json\n📋 保存的参数包括简介 (来源: {desc_source})")
        return True
    except Exception as e:
        if logger: logger.error(f"❌ 保存处理参数失败: {str(e)}")
        return False


def resolve_output_folder_path(input_folder: str, new_folder_name: str, conflict_strategy: str = "suffix") -> tuple:
    new_folder_path = os.path.join(os.path.dirname(input_folder), new_folder_name)
    if os.path.abspath(new_folder_path) == os.path.abspath(input_folder):
        return new_folder_path, False
    if conflict_strategy == "suffix":
        base_candidate = new_folder_path
        suffix_index = 1
        used_suffix = False
        while os.path.exists(new_folder_path):
            used_suffix = True
            new_folder_path = f"{base_candidate} ({suffix_index})"
            suffix_index += 1
        return new_folder_path, used_suffix
    return new_folder_path, os.path.exists(new_folder_path)


def save_operation_snapshot(folder_path: str, params: dict, audio_files: list, logger=None) -> bool:
    """保存处理前快照，便于失败恢复和后续撤销功能使用。"""
    try:
        snapshot = {
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "input_folder": os.path.abspath(folder_path),
            "params": dict(params or {}),
            "files": [],
        }
        for file_path in audio_files:
            try:
                stat = os.stat(file_path)
                snapshot["files"].append({
                    "path": os.path.abspath(file_path),
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                })
            except OSError:
                snapshot["files"].append({"path": os.path.abspath(file_path)})
        with open(os.path.join(folder_path, ".audiometa_snapshot.json"), "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2, default=str)
        if logger:
            logger.info(f"处理前快照已保存：{len(snapshot['files'])} 个音频文件")
        return True
    except Exception as e:
        if logger: logger.warning(f"处理前快照保存失败：{e}")
        return False

def load_process_params(folder_path: str, logger=None) -> dict:
    try:
        config_file_path = os.path.join(folder_path, "process_params.json")
        if not os.path.exists(config_file_path): return {}
        with open(config_file_path, 'r', encoding='utf-8') as f: params = json.load(f)
        if logger:
            logger.info(f"✅ 从配置文件加载处理参数")
            if '_saved_at' in params: logger.info(f"⏰ 参数保存时间: {params['_saved_at']}")
            if 'desc_source' in params: 
                source_txt = {'API': '网络API获取', 'Manual': '手动输入', 'Default': '默认描述'}.get(params['desc_source'], '未知来源')
                logger.info(f"📄 简介来源: {source_txt}")
        return params
    except Exception as e:
        if logger: logger.error(f"❌ 加载处理参数失败: {str(e)}")
        return {}

def load_operation_snapshot(folder_path: str) -> dict:
    snapshot_path = os.path.join(folder_path, ".audiometa_snapshot.json")
    if not os.path.exists(snapshot_path):
        return {}
    try:
        with open(snapshot_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}

def restore_operation_snapshot(folder_path: str, logger=None) -> dict:
    """安全恢复处理前目录名称，不覆盖已经存在的目标目录。"""
    current_path = os.path.abspath(folder_path)
    snapshot = load_operation_snapshot(current_path)
    original_path = os.path.abspath(str(snapshot.get("input_folder") or ""))
    if not original_path:
        raise ValueError("未找到有效的处理前快照")
    if original_path == current_path:
        return {"restored": False, "reason": "目录名称未发生变化", "path": current_path}
    if os.path.exists(original_path):
        raise FileExistsError(f"原目录已存在，为避免覆盖无法恢复：{original_path}")
    os.rename(current_path, original_path)
    if logger:
        logger.info(f"已恢复处理前目录名称：{original_path}")
    return {"restored": True, "from": current_path, "path": original_path}

def _split_series_values(value) -> list:
    return [part.strip() for part in re.split(r"[,，\n]+", str(value or "")) if part.strip()]

def _is_auto_bitrate(value: str) -> bool:
    return (value or "").strip().lower() in {"", "auto", "自动检测", "跳过升频", "原码率", "保持原码率"}

def build_series_items(series_name: str = None, series_number: str = None) -> list:
    names = _split_series_values(series_name)
    numbers = _split_series_values(series_number)
    items = []
    for index, name in enumerate(names):
        number = numbers[index] if index < len(numbers) else ""
        items.append(f"{name}#{number}" if number else name)
    if not items and numbers:
        items.extend(f"\u7cfb\u5217#{number}" for number in numbers)
    return list(dict.fromkeys(items))

def build_series_grouping(series_name: str = None, series_number: str = None) -> str:
    return "; ".join(build_series_items(series_name, series_number))

def build_series_metadata(series_name: str = None, series_number: str = None) -> list:
    return [item.replace("#", " #") for item in build_series_items(series_name, series_number)]

def _extract_prefetched_desc_and_subtitle(fetched_metadata: dict, album_subtitle: str):
    metadata = fetched_metadata or {}
    raw_desc = metadata.get("desc") or metadata.get("info") or ""
    desc = clean_html_tags(raw_desc) if raw_desc else ""
    subtitle = album_subtitle or (metadata.get("subtitle") or "").strip()
    return desc, subtitle

def _fetch_desc_and_subtitle_from_api(api_source: str, api_id: str, album_subtitle: str, logger=None):
    album_desc = ""
    final_album_subtitle = album_subtitle
    if api_source == "喜马拉雅":
        album_data = ximalaya_api("album", api_id)
        if album_data:
            final_album_subtitle = album_subtitle or album_data.get("albumPageMainInfo", {}).get("customTitle") or album_data.get("albumPageMainInfo", {}).get("subtitle") or album_subtitle
            raw_desc = album_data.get("albumPageMainInfo", {}).get("detailRichIntro", "")
            if raw_desc:
                album_desc = clean_html_tags(raw_desc)
    elif api_source == "懒人听书":
        raw_desc = lanren_api(api_id).get("desc", "")
        if raw_desc:
            album_desc = clean_html_tags(raw_desc)
    elif api_source == "酷我听书":
        raw_desc = kuwo_api(api_id).get("info", "")
        if raw_desc:
            album_desc = clean_html_tags(raw_desc)
    elif api_source == "番茄畅听":
        raw_desc = fanqie_api(api_id).get("desc", "")
        if raw_desc:
            album_desc = clean_html_tags(raw_desc)
    elif api_source == "起点听书":
        raw_desc = qidian_api(api_id).get("desc", "")
        if raw_desc:
            album_desc = clean_html_tags(raw_desc)
    elif api_source == "网易云听书":
        raw_desc = netease_ting_api(api_id).get("desc", "")
        if raw_desc:
            album_desc = clean_html_tags(raw_desc)
    elif api_source == "云听fm":
        raw_desc = yunting_api(api_id).get("desc", "")
        if raw_desc:
            album_desc = clean_html_tags(raw_desc)
    elif api_source == "蜻蜓fm":
        raw_desc = qingting_api(api_id).get("desc", "")
        if raw_desc:
            album_desc = clean_html_tags(raw_desc)
    if album_desc and logger:
        logger.info("✅ 从网络API提取简介内容")
    return album_desc, final_album_subtitle

def process_single_audio(args):
    try:
        audio_file, idx, total_tracks, album_title, album_subtitle, author, anchor, clean_desc, final_year, category_name, platform, cover_data, audio_info, series_name, series_number, logger, target_format, target_bitrate = args
        
        file_name = os.path.basename(audio_file)
        current_format = audio_info[audio_file]["codec"].upper()
        current_bitrate = audio_info[audio_file]["bitrate"]
        
        final_file = audio_file
        skip_conversion = False
        
        if _is_auto_bitrate(target_bitrate) and target_format == "原格式保留":
            skip_conversion = True
        elif target_format == current_format and target_bitrate == current_bitrate:
            if logger: logger.info(f"⏭️ 码率格式一致，自动跳过转换: {file_name}")
            skip_conversion = True
            
        if not skip_conversion:
            try:
                from app.processing.audio_converter import convert_audio_format_and_bitrate
                if logger: logger.info(f"🔁 转换音频：{file_name} -> {target_format} / {target_bitrate}")
                success, temp_final_file, err = convert_audio_format_and_bitrate(audio_file, target_format, target_bitrate, logger)
                if not success:
                    return (audio_file, False, idx, f"引擎转换失败: {err}")
                
                if temp_final_file != audio_file and os.path.exists(temp_final_file):
                    final_file = replace_converted_audio(audio_file, temp_final_file)
            except ImportError:
                if logger: logger.warning("⚠️ 未找到 audio_converter 模块，跳过转换")
                final_file = audio_file
        
        codec = target_format if target_format != "原格式保留" else audio_info[audio_file]["codec"]
        grouping_value = build_series_grouping(series_name, series_number)
        
        # 🎵 核心修复：将原有的逗号(中英均可)彻底转换为分号(;) 以符合音频标签底层多艺术家分割标准
        tag_author = join_people_for_tag(author)
        tag_anchor = join_people_for_tag(anchor)
        
        tags = {
            "title": os.path.splitext(os.path.basename(final_file))[0], 
            "album": album_title, 
            "artist": tag_author, 
            "album_artist": tag_author, 
            "composer": tag_anchor,
            "genre": f"Audiobook;有声书;{category_name}", 
            "date": final_year, 
            "track": f"{idx}/{total_tracks}",
            "copyright": f"{platform} {final_year} © {tag_author.replace(';', ' & ')} - 版权所有" if platform else f"{final_year} © {tag_author.replace(';', ' & ')} - 版权所有",
            "comment": clean_desc, 
            "publisher": platform, 
            "language": "chi", 
            "DESCRIPTION": clean_desc, 
            "ENCODING": codec, 
            "TRACKTOTAL": str(total_tracks)
        }
        if album_subtitle: tags["subtitle"] = album_subtitle
        if grouping_value: tags["grouping"] = grouping_value
        
        success = write_tags_and_cover(final_file, tags, cover_data, None)
        return (final_file, success, idx, None)
    except Exception as e: return (args[0], False, args[1], f"{type(e).__name__}: {str(e)}")

def batch_process_audio_parallel(audio_list: list, album_title: str, album_subtitle: str, author: str, anchor: str, clean_desc: str, final_year: str, category_name: str, platform: str, cover_data: bytes = None, audio_info: dict = None, series_name: str = None, series_number: str = None, logger=None, progress_callback=None, failed_audios_callback=None, stop_event=None, retry_set: set = None, target_format: str = "原格式保留", target_bitrate: str = "自动检测") -> tuple:
    success_count, fail_count, total_tracks = 0, 0, len(audio_list)
    tasks_to_run = [(idx, f) for idx, f in enumerate(audio_list, 1) if (not retry_set or os.path.basename(f) in retry_set)]
    total_tasks = len(tasks_to_run)
    if total_tasks == 0:
        if logger: logger.warning("⚠️ 没有找到需要处理的音频文件")
        return 0, 0, audio_list
        
    if logger: logger.info(f"⚡ 开始批量处理音频文件（共{total_tracks}个）...")
    updated_audio_list = []
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_list = []
        for idx, audio_file in tasks_to_run:
            if stop_event and stop_event.is_set(): break
            future_list.append((executor.submit(process_single_audio, (audio_file, idx, total_tracks, album_title, album_subtitle, author, anchor, clean_desc, final_year, category_name, platform, cover_data, audio_info, series_name, series_number, logger, target_format, target_bitrate)), audio_file, idx))
            
        for i, (future, audio_file, idx) in enumerate(future_list, 1):
            if stop_event and stop_event.is_set():
                for pending_future, _, _ in future_list[i - 1:]:
                    pending_future.cancel()
                break
            try:
                result = future.result(timeout=180)
                final_file, success = result[0], result[1]
                error_msg = result[3] if len(result) == 4 else "处理返回结果异常"
                updated_audio_list.append((idx, final_file))
                
                if progress_callback and i % 5 == 0: progress_callback((i / total_tasks) * 100, f"处理音频：{i}/{total_tasks}")
                if success:
                    success_count += 1
                    if logger: logger.info(f"✅ 处理成功（{idx}/{total_tracks}）：{os.path.basename(final_file)}")
                else:
                    fail_count += 1
                    if logger: logger.error(f"❌ 处理失败（{idx}/{total_tracks}）：{os.path.basename(final_file)} | {error_msg}")
                    if failed_audios_callback: failed_audios_callback(final_file, error_msg)
            except Exception as e:
                fail_count += 1
                if logger: logger.error(f"❌ 处理异常（{idx}/{total_tracks}）：{os.path.basename(audio_file)} | {type(e).__name__}: {str(e)}")
                if failed_audios_callback: failed_audios_callback(audio_file, str(e))
                
    if stop_event and stop_event.is_set():
        if logger: logger.warning(f"⏹️ 已收到停止请求，音频处理阶段中断。本轮已完成：{success_count}，失败：{fail_count}")
    elif logger:
        logger.info(f"✅ 音频处理阶段完成！本轮处理：{total_tasks} 个 | 成功：{success_count}，失败：{fail_count}")
    
    updated_audio_list.sort(key=lambda x: x[0])
    final_list = [x[1] for x in updated_audio_list]
    for i in range(len(audio_list)):
        if not any(item[0] == i + 1 for item in updated_audio_list):
            final_list.insert(i, audio_list[i])
            
    return success_count, fail_count, final_list

def generate_metadata_files(folder: str, audio_list: list, album_title: str, album_subtitle: str, author: str, anchor: str, final_year: str, category_name: str, platform: str, clean_desc: str, audio_info: dict, series_name: str = None, series_number: str = None, tags_list: list = None, logger=None) -> None:
    try:
        genre_str = f"Audiobook;有声书;{category_name}"
        chapters, total_duration = [], 0.0
        for idx, audio_file in enumerate(audio_list):
            orig_key = next((k for k in audio_info.keys() if os.path.splitext(os.path.basename(k))[0] == os.path.splitext(os.path.basename(audio_file))[0]), audio_file)
            duration = audio_info.get(orig_key, {}).get("duration", 0.0)
            chapter_title = os.path.splitext(os.path.basename(audio_file))[0]
            if "-" in chapter_title and len(chapter_title.split("-")[0]) == 5: chapter_title = "-".join(chapter_title.split("-")[1:])
            chapters.append({"id": idx, "start": total_duration, "end": total_duration + duration, "title": chapter_title})
            total_duration += duration
            
        series_list = build_series_metadata(series_name, series_number)
        grouping_value = build_series_grouping(series_name, series_number)
        tags_array = tags_list if tags_list else []
        
        # 智能分离多人列表，供 JSON 与 ABS 元数据格式使用
        author_list = split_people(author)
        anchor_list = split_people(anchor)
        
        metadata_json = {
            "tags": tags_array, "chapters": chapters, "title": album_title, "subtitle": album_subtitle if album_subtitle else None,
            "authors": author_list, "narrators": anchor_list, "series": series_list, "genres": genre_str.split(";"),
            "publishedYear": final_year, "publisher": platform, "description": clean_desc, "language": "中文", "explicit": False, "abridged": False
        }
        with open(os.path.join(folder, "metadata.json"), "w", encoding="utf-8") as f: 
            json.dump(metadata_json, f, ensure_ascii=False, indent=2)
        
        abs_content = f""";ABMETADATA2\n#audiobookshelf\nmedia=book\ntags={",".join(tags_array)}\ntitle={album_title}\nsubtitle={album_subtitle if album_subtitle else 'null'}\nauthors={",".join(author_list)}\nnarrators={",".join(anchor_list)}\nseries={grouping_value if grouping_value else 'null'}\ngenres={genre_str}\npublishedYear={final_year}\npublisher={platform if platform else '未知平台'}\nlanguage=中文\nexplicit=false\nabridged=false\ncopyright={f"{platform} {final_year} © {author.replace(',', '&').replace('，', '&')} - 版权所有" if platform else f"{final_year} © {author.replace(',', '&').replace('，', '&')} - 版权所有"}\ntotalDuration={round(total_duration, 2)}\n\n[DESCRIPTION]\n{clean_desc}\n\n"""
        with open(os.path.join(folder, "metadata.abs"), "w", encoding="utf-8") as f:
            f.write(abs_content)
            for ch in chapters: f.write(f"[CHAPTER]\nstart={ch['start']:.6f}\nend={ch['end']:.6f}\ntitle={ch['title']}\n\n")
            
        if logger: 
            logger.info("✅ metadata文件生成完成！")
            if album_subtitle: logger.info(f"✅ 副标题已写入metadata：{album_subtitle}")
            if tags_array: logger.info(f"✅ 专辑标签已写入metadata：{', '.join(tags_array)}")
    except Exception as e:
        if logger: logger.error(f"❌ 生成metadata失败：{str(e)}")

def process_audio_books(params: dict, logger, progress_callback=None, failed_audios_callback=None, stop_event=None, retry_files: list = None, folder_renamed_callback=None, modal_callback=None):
    try:
        if sys.platform == "win32": os.environ["PYTHONUNBUFFERED"] = "1"
        fields = params.get("metadata_fields") or {}
        if not fields.get("title", True): params["title"] = ""
        if not fields.get("subtitle", True): params["subtitle"] = ""
        if not fields.get("author", True): params["author"] = ""
        if not fields.get("anchor", True): params["anchor"] = ""
        if not fields.get("description", True): params["manual_desc"] = ""
        if not fields.get("series", True): params["series_name"] = params["series_number"] = ""
        if not fields.get("tags", True): params["album_tags"] = []
        if not fields.get("cover", True): params["manual_cover_path"] = ""
        input_folder, api_source, api_id = params["input_folder"].strip(), params.get("api_source", "喜马拉雅").strip(), params.get("api_id", "").strip()
        author, anchor, album_title, album_subtitle = params["author"].strip(), params["anchor"].strip(), params["title"].strip(), params.get("subtitle", "").strip()
        category_id, platform, manual_year, target_bitrate, finished = params["category"].strip(), params["platform"].strip(), params["year"].strip(), params.get("bitrate", "自动检测").strip(), params["finished"].strip()
        target_format = params.get("target_format", "原格式保留").strip()
        rename_ext, manual_cover_path, manual_desc = params["rename_ext"], params["manual_cover_path"].strip(), params["manual_desc"].strip()
        series_name, series_number, album_tags = params.get("series_name", "").strip(), params.get("series_number", "").strip(), params.get("album_tags", [])
        fetched_metadata = params.get("fetched_metadata") or {}
        retry_set = set(os.path.basename(f) for f in retry_files) if retry_files else None

        if progress_callback: progress_callback(0, "开始处理...")
        
        logger.info("=" * 60)
        logger.info("🚀 开始有声书批量处理流程" + (" (重试模式)" if retry_set else ""))
        logger.info(f"📂 音频文件夹路径：{input_folder}")
        logger.info(f"📁 程序目录：{BASE_DIR}")
        logger.info("=" * 60)
        
        fix_ssl_context()
        if not check_ffmpeg_tools(logger): return progress_callback(0, "FFmpeg工具验证失败") if progress_callback else None
        if not os.path.exists(input_folder): return progress_callback(0, "音频文件夹路径不存在") if progress_callback else None

        if progress_callback: progress_callback(10, "扫描音频文件...")
        audio_list, found_formats = get_audio_list(input_folder)
        save_operation_snapshot(input_folder, params, audio_list, logger)
        if not audio_list: return progress_callback(0, "未找到音频文件") if progress_callback else None
        
        format_part_init = "&".join(sorted([fmt.upper() for fmt in found_formats])) if found_formats else "未知格式"
        logger.info(f"✅ 找到 {len(audio_list)} 个音频文件，格式：{format_part_init}")

        if progress_callback: progress_callback(20, "获取音频信息...")
        audio_info = batch_get_audio_info(audio_list, logger, lambda p, m: progress_callback(20 + (p * 0.2), m) if progress_callback else None)
        
        low_bitrate_files = []
        import re
        for f_path, info in audio_info.items():
            br_str = info.get("bitrate", "0kbps")
            match = re.search(r'(\d+)', br_str)
            if match and int(match.group(1)) < 64:
                low_bitrate_files.append({"file": f_path, "bitrate": br_str})
                
        if low_bitrate_files and modal_callback:
            logger.warning(f"⚠️ 预检发现 {len(low_bitrate_files)} 个低质量/未知码率文件")
            decision = modal_callback(low_bitrate_files, len(audio_list))
            if not decision[0]: # decision return format from ui.py is (confirmed, bitrate)
                if progress_callback: progress_callback(0, "用户取消了处理任务")
                if stop_event: stop_event.set()
                return
            if decision[1] and decision[1] != "跳过升频":
                target_bitrate = decision[1]
                params["bitrate"] = target_bitrate
        elif low_bitrate_files:
            logger.warning(f"⚠️ 预检发现 {len(low_bitrate_files)} 个低质量/未知码率文件")
            for item in low_bitrate_files[:20]:
                logger.warning(f"   - {os.path.basename(item['file'])}: {item['bitrate']}")
            if len(low_bitrate_files) > 20:
                logger.warning(f"   - 其余 {len(low_bitrate_files) - 20} 个低码率文件已省略")
            if _is_auto_bitrate(target_bitrate):
                logger.warning("当前码率为自动检测，仅记录低码率预警；如需升频请在目标格式与码率处选择明确码率。")
            else:
                logger.info(f"已选择目标码率 {target_bitrate}，后续转换阶段将按该码率处理。")

        if rename_ext and target_format == "原格式保留":
            logger.info("📝 开始规范音频扩展名...")
            for i, file in enumerate(audio_list):
                if stop_event and stop_event.is_set(): break
                if audio_info[file]["codec"].lower() == "aac" and os.path.splitext(file)[1].lower() != ".m4a":
                    new_file = os.path.splitext(file)[0] + ".m4a"
                    try:
                        shutil.move(file, new_file)
                        audio_list[i] = new_file
                        audio_info[new_file] = audio_info.pop(file)
                        if retry_set and os.path.basename(file) in retry_set:
                            retry_set.remove(os.path.basename(file))
                            retry_set.add(os.path.basename(new_file))
                    except Exception as rename_error:
                        logger.warning(f"⚠️ 规范扩展名失败，已跳过：{os.path.basename(file)} | {rename_error}")

        if stop_event and stop_event.is_set(): return
        if progress_callback: progress_callback(45, "获取专辑信息...")
        album_desc, desc_source, final_album_subtitle, auto_year = "", "Unknown", album_subtitle, None
        
        if manual_desc: 
            album_desc, desc_source = manual_desc, "Manual"
            logger.info("✅ 使用手动输入的简介内容")
        elif fetched_metadata:
            album_desc, final_album_subtitle = _extract_prefetched_desc_and_subtitle(fetched_metadata, album_subtitle)
            if album_desc:
                desc_source = "Prefetched"
                logger.info("✅ 复用已抓取的元数据简介内容")
        elif api_id:
            try:
                album_desc, final_album_subtitle = _fetch_desc_and_subtitle_from_api(api_source, api_id, album_subtitle, logger)
                if album_desc:
                    desc_source = "API"
            except Exception as api_error:
                logger.warning(f"⚠️ 获取网络简介失败，将回退到默认简介：{api_error}")
            
        if not album_desc: album_desc, desc_source = DEFAULT_DESC, "Default"

        clean_desc, final_year, category_name = clean_html_tags(album_desc), manual_year, CATEGORY_MAP.get(category_id, "未知分类")
        if os.path.exists(input_folder): save_desc_file(input_folder, clean_desc, desc_source, logger)

        if final_album_subtitle: logger.info(f"✅ 最终使用的副标题：{final_album_subtitle}")
        logger.info(f"📚 专辑分类：{category_name}")

        if progress_callback: progress_callback(55, "处理封面图片...")
        cover_data = find_cover(input_folder, api_id if api_id else None, api_source, logger, manual_cover_path)
        try: 
            audio_list.sort(key=lambda s: [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', os.path.basename(s))])
            logger.info("✅ 音频文件已按名称完成自然排序")
        except Exception as sort_error:
            logger.warning(f"⚠️ 音频自然排序失败，保留原顺序：{sort_error}")

        if stop_event and stop_event.is_set(): return
        if progress_callback: progress_callback(60, "开始批量处理音频文件...")
        
        success_count, fail_count, audio_list = batch_process_audio_parallel(audio_list, album_title, final_album_subtitle, author, anchor, clean_desc, final_year, category_name, platform, cover_data, audio_info, series_name, series_number, logger, lambda p, m: progress_callback(60 + (p * 0.35), m) if progress_callback else None, failed_audios_callback, stop_event, retry_set, target_format, target_bitrate)

        if stop_event and stop_event.is_set(): return
        if progress_callback: progress_callback(95, "生成metadata文件...")
        generate_metadata_files(input_folder, audio_list, album_title, final_album_subtitle, author, anchor, final_year, category_name, platform, clean_desc, audio_info, series_name, series_number, album_tags, logger)

        try:
            save_process_params(input_folder, params, clean_desc, desc_source, logger)
        except Exception as save_params_error:
            logger.warning(f"⚠️ 保存处理参数失败：{save_params_error}")
        try:
            save_reader_file(input_folder, anchor, logger)
        except Exception as save_reader_error:
            logger.warning(f"⚠️ 保存 reader.txt 失败：{save_reader_error}")

        if stop_event and stop_event.is_set(): return
        if progress_callback: progress_callback(98, "重命名文件夹...")
        try:
            bitrate_range = calculate_bitrate_range(audio_info, found_formats)
            format_part = target_format if target_format != "原格式保留" else ("&".join(sorted([fmt.upper() for fmt in found_formats])) if found_formats else "未知格式")
            bitrate_part = target_bitrate if target_bitrate not in ["自动检测", "auto"] else (bitrate_range.split("@")[1] if "@" in bitrate_range else "未知码率")
            new_folder_name = build_output_folder_name(
                album_title,
                author,
                anchor,
                finished,
                final_year,
                format_part,
                bitrate_part,
                params.get("team", "RL"),
            )
            new_folder_path, used_suffix = resolve_output_folder_path(
                input_folder,
                new_folder_name,
                params.get("conflict_strategy", "suffix"),
            )
            if os.path.abspath(new_folder_path) == os.path.abspath(input_folder):
                logger.info("输出目录名称未变化，跳过重命名")
            else:
                if used_suffix:
                    logger.warning(f"输出目录已存在，自动使用备用目录名：{os.path.basename(new_folder_path)}")
                if os.path.exists(input_folder) and os.path.exists(new_folder_path):
                    logger.error(f"输出目录已存在，跳过重命名以避免覆盖：{new_folder_path}")
                elif os.path.exists(input_folder):
                    logger.info(f"📁 文件夹重命名为: {new_folder_name}")
                    shutil.move(input_folder, new_folder_path)
                    input_folder = new_folder_path
                    if folder_renamed_callback: folder_renamed_callback(new_folder_path)
                    if os.path.exists(new_folder_path):
                        logger.info(f"✅ 文件夹重命名完成：{new_folder_name}")
                        save_desc_file(new_folder_path, clean_desc, desc_source, logger)
                        save_reader_file(new_folder_path, anchor, logger)
        except Exception as e: logger.error(f"❌ 文件夹重命名失败：{e}")

        # ==== 生成最终的汇总报告块 ====
        total_duration_sec = sum(info.get("duration", 0.0) for info in audio_info.values())
        m, s = divmod(total_duration_sec, 60)
        h, m = divmod(m, 60)
        duration_str = f"{int(h):02d}:{int(m):02d}:{int(s):02d}"
        
        cover_size_kb = len(cover_data) // 1024 if cover_data else 0
        cover_str = f"已嵌入 (大小: {cover_size_kb} KB)" if cover_data else "未嵌入"
        source_text = {'API': '网络API获取', 'Prefetched': '已抓取元数据复用', 'Manual': '手动输入', 'Default': '默认描述'}.get(desc_source, '未知来源')

        summary = f"""
============================================================
🎉 有声书批量处理流程全部完成！
📊 本轮处理统计：处理{len(audio_list)}个 (成功{success_count}个 / 失败{fail_count}个) | 专辑总计{len(audio_list)}个
⚡ 并行线程数：{MAX_WORKERS} | 📅 年份：{final_year} | 📚 专辑分类：{category_name}
📖 副标题：{final_album_subtitle if final_album_subtitle else '无'}
🏷 专辑标签：{", ".join(album_tags) if album_tags else '无'}
📄 专辑简介：desc.txt ({source_text})
📁 音频文件夹路径：{input_folder}
============================================================

================================================================================
📋 写入音频的元数据信息汇总（仅显示一次）
================================================================================
💽 专辑标题: {album_title}
📖 副标题: {final_album_subtitle if final_album_subtitle else '无'}
👤 原著作者: {author}
🎙 演播艺术家: {anchor}
📅 年份: {final_year}
🏷 专辑分类: {category_name}
🏷 专辑标签: {", ".join(album_tags) if album_tags else '无'}
🌐 发布平台: {platform}
📊 状态: {finished}
🔊 码率（kbps）: {format_part} - {bitrate_part}
🎵 音频编码: {format_part}
📁 文件数量: {len(audio_list)}个
⏱ 总时长: {duration_str}
🖼 封面图片路径: {cover_str}
📄 专辑简介来源: {source_text}
📝 专辑简介字数: {len(clean_desc)}个字符
📁 生成的元数据文件:
  • metadata.json - 完整的元数据信息
  • metadata.abs - Audiobookshelf兼容格式
  • desc.txt - 简明的专辑简介
  • reader.txt - 演播艺术家名称
================================================================================
✅ 所有元数据信息已成功写入！
================================================================================
"""
        logger.info(summary.strip("\n"))
        if progress_callback: progress_callback(100, f"处理完成！成功{success_count}个，失败{fail_count}个")
        return {
            "success_count": success_count,
            "fail_count": fail_count,
            "audio_list": audio_list,
            "input_folder": input_folder,
            "desc_source": desc_source,
        }
    except Exception as e:
        logger.error(f"❌ 处理流程异常终止：{traceback.format_exc()}")
        if progress_callback: progress_callback(0, f"处理失败：{str(e)}")
        return {
            "success_count": 0,
            "fail_count": 0,
            "audio_list": [],
            "input_folder": params.get("input_folder", ""),
            "desc_source": "Error",
            "error": str(e),
        }

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.integrations.api_clients import _fanqie_parse_search_book, _fanqie_search_books, _matching_ypshuo_authors, fanqie_api, fanqie_cover_url, fanqie_release_year, search_platform_metadata
from app.processing.metadata_helpers import build_output_folder_name
from app.processing.processor import load_operation_snapshot, resolve_output_folder_path, restore_operation_snapshot, save_operation_snapshot
from app.web.server import (
    _tag_blacklist_storage_path,
    AppState,
    FAVICON_SVG,
    INDEX_HTML,
    collect_tags_and_year_from_payload,
    collect_ximalaya_app_tags,
    extract_ximalaya_release_year,
    fetch_api_metadata,
    fetch_link_metadata,
    load_folder_config,
    load_tag_blacklist_patterns,
    normalize_ximalaya_payload,
    save_tag_blacklist_patterns,
)

DOCKER_WEB_SOURCE = (Path(__file__).resolve().parents[1] / "app" / "web" / "server.py").read_text(encoding="utf-8")


class RegressionTests(unittest.TestCase):
    def test_required_metadata_fields_start_empty(self):
        self.assertIn('"category": ""', DOCKER_WEB_SOURCE)
        self.assertIn('"platform": ""', DOCKER_WEB_SOURCE)
        self.assertIn('"year": ""', DOCKER_WEB_SOURCE)
        self.assertIn('"finished": ""', DOCKER_WEB_SOURCE)
        self.assertIn('"team": "RL"', DOCKER_WEB_SOURCE)
        self.assertIn("'请选择发布平台'", INDEX_HTML)
        self.assertIn("'请选择专辑分类'", INDEX_HTML)
        self.assertIn("'请选择专辑状态'", INDEX_HTML)

    def test_ximalaya_release_year_ignores_track_and_update_timestamps(self):
        payload = {
            "albumPageMainInfo": {
                "albumId": 123,
                "albumTitle": "历史专辑",
                "created_at": 1502787165000,
                "updated_at": 1780000000000,
            },
            "data": {
                "tracks": [
                    {"trackId": 1, "created_at": 1780000000000, "updated_at": 1780000000000}
                ]
            },
        }
        self.assertEqual(extract_ximalaya_release_year(payload), "2017")
        self.assertEqual(normalize_ximalaya_payload(payload)["releaseDate"], "2017")

    def test_status_logs_are_returned_incrementally(self):
        state = AppState()
        state.add_log("one")
        state.add_log("two")
        first = state.snapshot(logs_after=0, log_epoch=0)
        self.assertEqual([item["message"] for item in first["logs"]], ["one", "two"])
        self.assertEqual(first["log_seq"], 2)
        self.assertFalse(first["logs_reset"])

        state.add_log("three")
        delta = state.snapshot(logs_after=2, log_epoch=0)
        self.assertEqual([item["message"] for item in delta["logs"]], ["three"])

        state.reset_for_run(clear_logs=True)
        reset = state.snapshot(logs_after=3, log_epoch=0)
        self.assertTrue(reset["logs_reset"])
        self.assertEqual(reset["logs"], [])

    def test_log_view_uses_bounded_on_demand_rendering(self):
        self.assertIn("const MAX_CLIENT_LOGS = 1200", INDEX_HTML)
        self.assertIn("const MAX_RENDERED_LOGS = 600", INDEX_HTML)
        self.assertIn("document.createDocumentFragment()", INDEX_HTML)
        self.assertIn("logBox.replaceChildren(fragment)", INDEX_HTML)
        self.assertIn("logs_after: String(lastLogSeq)", INDEX_HTML)
        self.assertIn("if (logsChanged) scheduleLogRender()", INDEX_HTML)
        self.assertNotIn("logSize !== lastLogSize || document.getElementById('panel-log').classList.contains('active')", INDEX_HTML)

    def test_native_selects_are_enhanced_with_custom_popovers(self):
        self.assertIn("custom-select-trigger", INDEX_HTML)
        self.assertIn("custom-select-popover", INDEX_HTML)
        self.assertIn("function initCustomSelects()", INDEX_HTML)
        self.assertIn("await loadOptions();\n      initCustomSelects();", INDEX_HTML)
        self.assertIn("event.key === 'Escape'", INDEX_HTML)
        self.assertIn("aria-selected", INDEX_HTML)

    def test_directory_picker_double_click_does_not_select_text(self):
        self.assertIn("-webkit-user-select: none; user-select: none", INDEX_HTML)
        self.assertIn("if (event.detail > 1) event.preventDefault()", INDEX_HTML)
        self.assertIn("window.getSelection()?.removeAllRanges()", INDEX_HTML)

    def test_dark_select_options_have_readable_native_colours(self):
        self.assertIn('html[data-theme="dark"] { color-scheme: dark; }', INDEX_HTML)
        self.assertIn("select option, select optgroup", INDEX_HTML)
        self.assertIn("background-color: var(--surface)", INDEX_HTML)
        self.assertIn("select option:checked", INDEX_HTML)

    def test_coloured_button_hover_keeps_its_gradient(self):
        self.assertIn("background-color: var(--surface-2)", INDEX_HTML)
        for class_name in ("btn-primary", "btn-green", "btn-amber", "btn-red", "btn-indigo"):
            selector = f".{class_name}:hover:not(:disabled)"
            start = INDEX_HTML.rindex(selector)
            rule = INDEX_HTML[start:INDEX_HTML.index("}", start)]
            self.assertIn("background: linear-gradient", rule, class_name)

    def test_author_lookup_button_and_dialog_are_present(self):
        self.assertIn('id="fetchAuthorBtn"', INDEX_HTML)
        self.assertIn('id="authorSearchResults"', INDEX_HTML)
        self.assertIn("function fetchAuthorByTitle()", INDEX_HTML)
        self.assertIn("'/api/search-author'", INDEX_HTML)
        self.assertIn(".search-result-desc", INDEX_HTML)
        self.assertIn(".search-result-tags", INDEX_HTML)
        self.assertIn(".search-result-action", INDEX_HTML)
        self.assertIn('class="search-count"', INDEX_HTML)

    def test_source_controls_share_one_aligned_grid(self):
        self.assertIn('class="source-controls"', INDEX_HTML)
        self.assertIn(".source-controls > input,", INDEX_HTML)
        self.assertIn("width: 100%; min-width: 0; min-height: 46px;", INDEX_HTML)
        self.assertIn("@media (max-width: 1500px) and (min-width: 901px)", INDEX_HTML)
        self.assertIn(".source-controls > button:nth-of-type(2) { grid-column: 2; }", INDEX_HTML)
        self.assertNotIn('class="source-action"', INDEX_HTML)

    def test_workspace_redesign_keeps_all_required_sections(self):
        for marker in (
            'class="global-topbar"', 'id="settingsBtn"', 'class="right-commandbar"',
            'name="input_folder"', 'id="teamPool"', 'id="seriesPool"', 'id="tagPool"',
        ):
            self.assertIn(marker, INDEX_HTML)

    def test_settings_center_exposes_existing_maintenance_tools(self):
        for element_id in (
            "settingsModal", "cookieBtn", "webTokenBtn", "blacklistBtn",
            "exportConfigBtn", "importConfigBtn", "previewRunBtn", "healthBtn",
            "qualityBtn", "batchImportBtn", "restoreSnapshotBtn", "exportLogBtn",
        ):
            self.assertIn(f'id="{element_id}"', INDEX_HTML)

    def test_redesigned_html_has_no_duplicate_static_ids(self):
        static_ids = re.findall(r'id="([^"]+)"', INDEX_HTML)
        duplicates = sorted({element_id for element_id in static_ids if static_ids.count(element_id) > 1})
        self.assertEqual(duplicates, [])

    def test_dark_console_matches_preview_structure(self):
        for marker in (
            'class="theme-cluster"', 'id="queueCountText"', 'class="queue-console"',
            'id="panel-log"', 'id="selectedCountText"', 'id="clearLogBtn"',
            'class="metadata-title-grid"', 'class="archive-main-grid"',
        ):
            self.assertIn(marker, INDEX_HTML)
        self.assertIn("DARK CONSOLE — PREVIEW MATCH", INDEX_HTML)
        self.assertIn("grid-template-columns: minmax(650px, 1.03fr) minmax(610px, .97fr)", INDEX_HTML)
        self.assertIn("applyTheme('dark')", INDEX_HTML)

    def test_preview_layout_switches_queue_and_log_panels(self):
        self.assertIn('.queue-console > .tab-panel#panel-log', INDEX_HTML)
        self.assertIn("document.querySelectorAll('.queue-console > .tab-panel')", INDEX_HTML)
        self.assertNotIn("document.querySelectorAll('.tab-panel').forEach(x => x.classList.remove('active'))", INDEX_HTML)

    def test_archive_parameter_labels_do_not_wrap(self):
        self.assertIn("grid-template-columns: max-content minmax(0, 1fr)", INDEX_HTML)
        self.assertIn(".archive-main-grid label { margin: 0; color: #d5dce9; white-space: nowrap; }", INDEX_HTML)

    def test_redesigned_console_has_a_complete_light_palette(self):
        for selector in (
            ':is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) body',
            ':is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .global-topbar',
            ':is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .section',
            ':is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .queue-console,',
            ':is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .queue-console th',
            ':is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .right-commandbar',
            ':is(html[data-theme="light"], html[data-theme="linen"], html[data-theme="mint"], html[data-theme="rose"]) .log',
        ):
            self.assertIn(selector, INDEX_HTML)
        self.assertIn("--bg: #f4f6fa", INDEX_HTML)

    def test_settings_center_exposes_nine_curated_themes(self):
        for theme, label in (
            ("dark", "墨夜"),
            ("light", "素雪"),
            ("linen", "茶白"),
            ("mint", "青瓷"),
            ("rose", "胭脂"),
            ("ocean", "黛蓝"),
            ("aurora", "暮紫"),
            ("jade", "碧波"),
            ("graphite", "玄灰"),
        ):
            self.assertIn(f'data-theme-option="{theme}"', INDEX_HTML)
            self.assertIn(label, INDEX_HTML)
        for theme in ("linen", "mint", "rose", "ocean", "aurora", "jade", "graphite"):
            self.assertIn(f'html[data-theme="{theme}"]', INDEX_HTML)
        self.assertIn("const _THEMES = Object.freeze", INDEX_HTML)
        self.assertIn("const _LIGHT_THEMES = new Set(['light', 'linen', 'mint', 'rose'])", INDEX_HTML)
        self.assertIn("option.dataset.themeOption === theme", INDEX_HTML)

    def test_album_tag_pool_uses_distinct_colour_variables(self):
        self.assertIn("chip.className = 'chip album-tag-chip'", INDEX_HTML)
        self.assertIn("index * 137.508", INDEX_HTML)
        self.assertIn("chip.style.setProperty('--tag-color-a'", INDEX_HTML)
        self.assertIn("chip.style.setProperty('--tag-color-b'", INDEX_HTML)
        self.assertIn("text.textContent = tag", INDEX_HTML)
        self.assertIn("#tagPool .album-tag-chip", INDEX_HTML)
        self.assertNotIn("chip.innerHTML = `<span>${tag}</span>`", INDEX_HTML)

    def test_people_and_other_chip_pools_use_colored_chips(self):
        self.assertIn("chip.className = 'chip colored-chip'", INDEX_HTML)
        self.assertIn("chip.style.setProperty('--chip-color-a'", INDEX_HTML)
        self.assertIn("chip.style.setProperty('--chip-color-b'", INDEX_HTML)
        self.assertIn("chip.style.setProperty('--chip-border'", INDEX_HTML)
        self.assertIn(".chip.colored-chip", INDEX_HTML)
        self.assertIn("const hue = (198 + index * 137.508) % 360", INDEX_HTML)
        for pool_id in ("authorPool", "anchorPool", "teamPool", "seriesPool", "blacklistPool"):
            self.assertIn(pool_id, INDEX_HTML)

    def test_custom_select_supports_platform_logos(self):
        self.assertIn("const _PLATFORM_BRANDS =", INDEX_HTML)
        self.assertIn("function createPlatformLogo", INDEX_HTML)
        self.assertIn(".platform-logo", INDEX_HTML)
        self.assertIn(".custom-select-value", INDEX_HTML)

    def test_clear_edit_area_is_grouped_with_queue_buttons(self):
        self.assertIn('id="clearBtn"', INDEX_HTML)
        self.assertNotIn('id="settingsClearBtn"', INDEX_HTML)
        self.assertIn('id="addQueueBtn"', INDEX_HTML)
        self.assertIn('id="startQueueBtn"', INDEX_HTML)
        self.assertIn('id="stopBtn"', INDEX_HTML)

    def test_cover_controls_are_cleaned_up_and_hover_only(self):
        self.assertNotIn('id="uploadCoverBtn"', INDEX_HTML)
        self.assertNotIn('id="previewCoverBtn"', INDEX_HTML)
        self.assertIn(".cover-box:hover .cover-change-button", INDEX_HTML)

    def test_ui_icons_use_consistent_svg_system(self):
        self.assertIn("function installUiIcons()", INDEX_HTML)
        self.assertIn("const _UI_ICONS =", INDEX_HTML)
        self.assertIn(".ui-icon", INDEX_HTML)
        self.assertIn("settingsIconNames", INDEX_HTML)
        self.assertIn(".settings-icon .ui-icon", INDEX_HTML)
        self.assertIn(".theme-symbol .ui-icon", INDEX_HTML)

    def test_browser_favicon_matches_ui_brand(self):
        self.assertIn('href="/favicon.svg?v=2"', INDEX_HTML)
        self.assertIn('d="M24 6c8.7 0 15.8 5.2 18.9 12.5-5.9-3.3-12.7-3.2-17.7.3-4.8 3.4-7 9.1-5.6 14.4C12.4 31.4 7 25 7 17.4 11.4 10.4 17 6 24 6Z"', FAVICON_SVG)

    def test_author_and_anchor_pools_share_one_row(self):
        self.assertIn('class="people-row"', INDEX_HTML)
        self.assertIn('id="authorPool"', INDEX_HTML)
        self.assertIn('id="anchorPool"', INDEX_HTML)
        self.assertIn(".people-row .entity-row", INDEX_HTML)
        self.assertIn("placeholder: '请输入作者，回车添加'", INDEX_HTML)
        self.assertIn("placeholder: '请输入演播者，回车添加'", INDEX_HTML)
        self.assertIn("editor.dataset.placeholder = values.length ? '' : (options.placeholder || '')", INDEX_HTML)
        self.assertIn("placeholder: '请输入制作团队，回车添加'", INDEX_HTML)

    def test_clear_edit_area_clears_cover_and_cover_is_square(self):
        self.assertIn("form.manual_cover_path.value = ''", INDEX_HTML)
        self.assertIn("img.removeAttribute('src')", INDEX_HTML)
        self.assertIn("height: 192px", INDEX_HTML)
        self.assertIn("aspect-ratio: 1 / 1;", INDEX_HTML)
        self.assertIn(".cover-box img { object-fit: cover; }", INDEX_HTML)

    def test_queue_has_checkboxes_and_platform_logos(self):
        self.assertIn('type="checkbox" class="queue-check"', INDEX_HTML)
        self.assertIn('colspan="7"', INDEX_HTML)
        self.assertIn("platformLogoHtml(platform)", INDEX_HTML)

    def test_settings_toast_results_are_above_modal(self):
        self.assertIn(".toast {\n      z-index: 2000;", INDEX_HTML)

    def test_folder_config_restores_saved_cover(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "processed"
            folder.mkdir()
            (folder / "process_params.json").write_text(json.dumps({"title": "书"}), encoding="utf-8")
            cover = folder / "cover.jpg"
            cover.write_bytes(b"cover")
            result = load_folder_config(str(folder))
            self.assertTrue(result["found"])
            self.assertEqual(Path(result["params"]["manual_cover_path"]).resolve(), cover.resolve())

    def test_output_folder_does_not_suffix_processed_input_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            folder = parent / "书 - 作者 - 演播 - 完结 - 2024 - MP3 128k -RL"
            folder.mkdir()
            path, used_suffix = resolve_output_folder_path(str(folder), folder.name, "suffix")
            self.assertEqual(Path(path).resolve(), folder.resolve())
            self.assertFalse(used_suffix)
            path, used_suffix = resolve_output_folder_path(str(folder), "新目录", "suffix")
            self.assertEqual(Path(path).resolve(), (parent / "新目录").resolve())
            self.assertFalse(used_suffix)
            conflict = parent / "同名目录"
            conflict.mkdir()
            path, used_suffix = resolve_output_folder_path(str(folder), conflict.name, "suffix")
            self.assertEqual(Path(path).resolve(), (parent / "同名目录 (1)").resolve())
            self.assertTrue(used_suffix)

    def test_ypshuo_author_candidates_require_exact_title_and_are_distinct(self):
        candidates = [
            {"id": "1", "novel_name": "我不是戏神", "author_name": "三九音域"},
            {"id": "2", "novel_name": "我不是戏神", "author_name": "三九音域"},
            {"id": "3", "novel_name": "我不是戏神", "author_name": "另一作者"},
            {"id": "4", "novel_name": "我不是戏神前传", "author_name": "错误作者"},
        ]
        matches = _matching_ypshuo_authors("我不是戏神", candidates)
        self.assertEqual([item["author"] for item in matches], ["三九音域", "另一作者"])

    def test_ximalaya_app_show_tags_are_collected(self):
        payload = {
            "data": {
                "newShowTags": [{"title": "影视原著"}, {"displayName": "历史小说"}],
                "albumMetaValueInfos": [{"metadataValueName": "出版物"}],
            }
        }
        tags, _ = collect_tags_and_year_from_payload(payload)
        self.assertTrue({"影视原著", "历史小说", "出版物"}.issubset(set(tags)))

    def test_ximalaya_app_tags_ignore_recommendations_and_comments(self):
        payload = {
            "data": {
                "detail": {"showTagList": [{"tagName": "灵异"}, {"tagName": "探险"}]},
                "recommendAlbums": [{"categoryName": "儿童"}],
                "comments": {"list": [{"albumCommentTags": "大大的赞,喜欢听"}]},
            }
        }
        self.assertEqual(collect_ximalaya_app_tags(payload), ["灵异", "探险"])

    @patch("app.web.server.extract_advanced_info", return_value=(["移动端标签"], ""))
    @patch("app.web.server.ximalaya_api")
    def test_ximalaya_app_tags_are_fetched_even_when_web_tags_exist(self, api_mock, advanced_mock):
        api_mock.return_value = {
            "albumPageMainInfo": {
                "albumTitle": "测试专辑", "categoryTitle": "有声书",
                "tags": ["网页标签一", "网页标签二"],
            }
        }
        metadata = fetch_api_metadata("喜马拉雅", "123")
        advanced_mock.assert_called_once()
        self.assertIn("移动端标签", metadata["tags"])

    def test_output_folder_name_is_deterministic(self):
        value = build_output_folder_name("书名", "作者", "主播", "完结", "2024", "MP3", "128k", "RL")
        self.assertEqual(value, "书名 - 作者 - 演播主播 - 完结 - 2024 - MP3 128k -RL")

    def test_snapshot_round_trip(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / "book"
            folder.mkdir()
            audio = folder / "01.mp3"
            audio.write_bytes(b"audio")
            self.assertTrue(save_operation_snapshot(str(folder), {"title": "book"}, [str(audio)]))
            snapshot = load_operation_snapshot(str(folder))
            self.assertEqual(snapshot["input_folder"], str(folder.resolve()))
            self.assertEqual(snapshot["files"][0]["path"], str(audio.resolve()))

    def test_snapshot_restore_never_overwrites_existing_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = root / "original"
            renamed = root / "renamed"
            original.mkdir()
            renamed.mkdir()
            (renamed / ".audiometa_snapshot.json").write_text(json.dumps({"input_folder": str(original)}), encoding="utf-8")
            with self.assertRaises(FileExistsError):
                restore_operation_snapshot(str(renamed))
            self.assertTrue(original.exists())
            self.assertTrue(renamed.exists())

    def test_snapshot_restore_moves_only_when_target_is_free(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = root / "original"
            renamed = root / "renamed"
            renamed.mkdir()
            (renamed / ".audiometa_snapshot.json").write_text(json.dumps({"input_folder": str(original)}), encoding="utf-8")
            result = restore_operation_snapshot(str(renamed))
            self.assertTrue(result["restored"])
            self.assertTrue(original.exists())
            self.assertFalse(renamed.exists())

    def test_fanqie_search_result_parser_matches_plugin_fields(self):
        item = _fanqie_parse_search_book({
            "book_id": "7123456789012345678",
            "book_name": "\u6d4b\u8bd5\u4e66\u540d",
            "author": "\u4f5c\u8005",
            "anchor": "\u6f14\u64ad",
            "audio_thumb_uri": "http://p6-novelfm-sign.novelfmpic.com/novel-pic/p2od1966be4cc0083581d73b36119c66464~tplv-y3bzr8ilui-smart-resize:220:220.jpeg?lk3s=b132c119&scene=search_v2&x-expires=1786437088&x-signature=fake",
            "thumb_url": "http://p6-novelfm-sign.novelfmpic.com/novel-pic/p2od1966be4cc0083581d73b36119c66464~tplv-y3bzr8ilui-resize:220:0.jpeg?lk3s=b132c119&scene=search_v2&x-expires=1786437088&x-signature=fake",
            "abstract": "\u7b80\u4ecb",
            "tags": [{"tag_name": "\u7384\u5e7b"}, "\u90fd\u5e02"],
            "tag_name": "\u60ac\u7591",
            "creation_status": 0,
            "create_time": "2022-05-06",
            "chapter_number": 88,
            "category": "\u6709\u58f0\u5c0f\u8bf4",
        })
        self.assertEqual(item["id"], "7123456789012345678")
        self.assertEqual(item["title"], "\u6d4b\u8bd5\u4e66\u540d")
        self.assertEqual(item["narrator"], "\u6f14\u64ad")
        self.assertEqual(item["tags"], ["\u7384\u5e7b", "\u90fd\u5e02", "\u60ac\u7591"])
        self.assertEqual(item["finished"], "\u5b8c\u7ed3")
        self.assertEqual(item["chapter_count"], 88)
        self.assertEqual(item["cover"], "https://p6-novelfm.novelfmpic.com/novel-pic/p2od1966be4cc0083581d73b36119c66464~tplv-y3bzr8ilui-resize:1080:1080.jpeg")
        self.assertEqual(item["release_date"], "2022")

    def test_fanqie_link_metadata_helpers(self):
        data = {
            "audio_thumb_url_hd": "http://hd.example.com/cover.jpg",
            "thumb_url": "http://thumb.example.com/cover.jpg",
            "create_time": 1641330000000,
        }
        self.assertEqual(fanqie_cover_url(data), "https://hd.example.com/cover.jpg")
        self.assertEqual(fanqie_release_year(data), "2022")

    @patch("app.web.server.fetch_fanqie_rendered_metadata", return_value={})
    @patch("app.web.server.fetch_fanqie_api_metadata_from_share_html", return_value={})
    @patch("app.web.server.parse_fanqie_share_html")
    @patch("app.web.server.fetch_share_page_html", return_value="<html></html>")
    def test_fanqie_link_cover_is_upgraded_to_hd(self, html_mock, parse_mock, api_mock, render_mock):
        signed_cover = (
            "http://p6-novelfm-sign.novelfmpic.com/novel-pic/"
            "p2od1966be4cc0083581d73b36119c66464"
            "~tplv-y3bzr8ilui-smart-resize:220:220.jpeg?lk3s=b132c119&x-signature=fake"
        )
        parse_mock.return_value = {
            "title": "\u94fe\u63a5\u6807\u9898",
            "author": "\u4f5c\u8005",
            "cover": signed_cover,
            "bestCover": signed_cover,
            "pic": signed_cover,
        }
        meta = fetch_link_metadata(
            "https://m.changdunovel.com/ug/pages/book-share?book_id=123",
            "\u756a\u8304\u7545\u542c",
        )
        self.assertEqual(
            meta["cover_url"],
            "https://p6-novelfm.novelfmpic.com/novel-pic/"
            "p2od1966be4cc0083581d73b36119c66464"
            "~tplv-y3bzr8ilui-resize:1080:1080.jpeg",
        )

    def test_tag_blacklist_uses_persistent_config_path(self):
        with patch.dict("os.environ", {"PROCESS_CONFIG": "/config/process_params.json"}):
            path = _tag_blacklist_storage_path()
        self.assertEqual(str(path).replace("\\", "/"), "/config/tag_blacklist.txt")

    def test_tag_blacklist_save_load_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tag_blacklist.txt"
            with patch("app.web.server.TAG_BLACKLIST_PATH", path):
                save_tag_blacklist_patterns(["\u5e7f\u544a", "\u5f15\u6d41"])
                self.assertEqual(load_tag_blacklist_patterns(), ["\u5e7f\u544a", "\u5f15\u6d41"])

    def test_tag_blacklist_persists_more_than_four_patterns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tag_blacklist.txt"
            patterns = [f"规则{i}" for i in range(1, 13)]
            with patch("app.web.server.TAG_BLACKLIST_PATH", path):
                saved = save_tag_blacklist_patterns(patterns)
                self.assertEqual(saved, patterns)
                self.assertEqual(load_tag_blacklist_patterns(), patterns)

    @patch("app.integrations.api_clients._fanqie_search_by_id")
    @patch("app.integrations.api_clients._fanqie_get_share_info")
    @patch("app.integrations.api_clients._fanqie_plugin_detail")
    def test_fanqie_id_only_merges_detail_share_and_search(self, detail_mock, share_mock, search_mock):
        detail_mock.return_value = {
            "title": "\u8be6\u60c5\u6807\u9898",
            "cover": "https://detail.example.com/cover.jpg",
            "releaseDate": "2019",
        }
        share_mock.return_value = {
            "name": "\u5206\u4eab\u6807\u9898",
            "desc": "\u5206\u4eab\u7b80\u4ecb",
            "tags": ["\u5206\u4eab\u6807\u7b7e"],
        }
        search_mock.return_value = {
            "title": "\u641c\u7d22\u6807\u9898",
            "desc": "\u641c\u7d22\u7b80\u4ecb",
            "announcer": "\u641c\u7d22\u6f14\u64ad",
            "chapter_count": 12,
            "releaseDate": "2022",
            "tags": ["\u641c\u7d22\u6807\u7b7e"],
        }
        raw = fanqie_api("123")
        self.assertEqual(raw["title"], "\u8be6\u60c5\u6807\u9898")
        self.assertEqual(raw["cover"], "https://detail.example.com/cover.jpg")
        self.assertEqual(raw["desc"], "\u5206\u4eab\u7b80\u4ecb")
        self.assertEqual(raw["announcer"], "\u641c\u7d22\u6f14\u64ad")
        self.assertEqual(raw["chapter_count"], 12)
        self.assertEqual(raw["releaseDate"], "2019")
        self.assertIn("\u5206\u4eab\u6807\u7b7e", raw["tags"])
        self.assertIn("\u641c\u7d22\u6807\u7b7e", raw["tags"])

    def test_fanqie_search_request_uses_plugin_params(self):
        with patch("app.integrations.api_clients.get_safe_session") as get_session:
            response = get_session.return_value.post.return_value
            response.status_code = 200
            response.json.return_value = {
                "data": {"search_data": [{"books": [{"book_id": "1", "book_name": "Book"}]}]}
            }
            items = _fanqie_search_books("demo", page=2, limit=15)
            self.assertEqual([item["id"] for item in items], ["1"])
            call_args = get_session.return_value.post.call_args
            kwargs = call_args.kwargs
            self.assertEqual(call_args.args[0], "https://api5-sinfonlinec.novelfm.com/novelfm/bookmall/search/page/v1/")
            self.assertEqual(kwargs["json"], {"query": "demo", "limit": 15, "offset": 15})
            self.assertEqual(
                kwargs["headers"]["User-Agent"],
                "com.xs.fm/608 (Linux; U; Android 9; zh_CN; 2210132C; Build/PQ3A.190605.07021633;tt-ok/3.12.13.17)",
            )
            self.assertIn("device_id", kwargs["params"])
            self.assertIn("iid", kwargs["params"])
            self.assertIn("_rticket", kwargs["params"])

    @patch("app.integrations.api_clients._fanqie_search_books")
    def test_fanqie_search_platform_metadata_keeps_plugin_fields(self, search_mock):
        search_mock.return_value = [{
            "id": "1",
            "title": "\u641c\u7d22\u6807\u9898",
            "author": "\u4f5c\u8005",
            "narrator": "\u6f14\u64ad",
            "cover": "https://example.com/cover.jpg",
            "desc": "\u7b80\u4ecb",
            "tags": ["\u6807\u7b7e"],
            "chapter_count": 12,
            "finished": "\u5b8c\u7ed3",
            "category": "\u6709\u58f0\u5c0f\u8bf4",
            "release_date": "2024",
        }]
        results, has_next = search_platform_metadata("\u756a\u8304\u7545\u542c", "\u641c\u7d22\u6807\u9898")
        self.assertEqual(results[0]["narrator"], "\u6f14\u64ad")
        self.assertEqual(results[0]["chapter_count"], 12)
        self.assertEqual(results[0]["finished"], "\u5b8c\u7ed3")
        self.assertEqual(results[0]["release_date"], "2024")

    @patch("app.web.server.fanqie_api")
    def test_fetch_fanqie_metadata_uses_selected_search_result(self, fanqie_mock):
        fanqie_mock.return_value = {
            "title": "\u540e\u53f0\u6807\u9898",
            "desc": "\u540e\u53f0\u7b80\u4ecb",
            "cover": "https://backend.example/cover.jpg",
        }
        meta = fetch_api_metadata("\u756a\u8304\u7545\u542c", "123", {
            "title": "\u641c\u7d22\u6807\u9898",
            "author": "\u641c\u7d22\u4f5c\u8005",
            "narrator": "\u641c\u7d22\u6f14\u64ad",
            "cover": "https://example.com/cover.jpg",
            "desc": "\u641c\u7d22\u7b80\u4ecb",
            "tags": ["\u6807\u7b7e"],
            "finished": "\u5b8c\u7ed3",
            "category": "\u6709\u58f0\u5c0f\u8bf4",
            "release_date": "2023",
        })
        self.assertEqual(meta["title"], "\u641c\u7d22\u6807\u9898")
        self.assertEqual(meta["desc"], "\u641c\u7d22\u7b80\u4ecb")
        self.assertEqual(meta["cover_url"], "https://example.com/cover.jpg")
        self.assertEqual(meta["author"], "\u641c\u7d22\u4f5c\u8005")
        self.assertEqual(meta["anchor"], "\u641c\u7d22\u6f14\u64ad")
        self.assertIn("\u6807\u7b7e", meta["tags"])
        self.assertEqual(meta["year"], "2023")

    def test_fanqie_click_flow_passes_selected_search_result(self):
        self.assertIn("async function fetchMetadata(searchResult)", INDEX_HTML)
        self.assertIn("search_result: searchResult || null", INDEX_HTML)
        self.assertIn("const selected = {", INDEX_HTML)
        self.assertIn("await fetchMetadata(selected);", INDEX_HTML)


if __name__ == "__main__":
    unittest.main()

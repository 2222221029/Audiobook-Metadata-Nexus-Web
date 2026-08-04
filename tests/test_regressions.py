import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from api_clients import _matching_ypshuo_authors
from metadata_helpers import build_output_folder_name
from processor import load_operation_snapshot, restore_operation_snapshot, save_operation_snapshot
from docker_web import (
    AppState,
    INDEX_HTML,
    collect_tags_and_year_from_payload,
    collect_ximalaya_app_tags,
    extract_ximalaya_release_year,
    fetch_api_metadata,
    normalize_ximalaya_payload,
)

DOCKER_WEB_SOURCE = (Path(__file__).resolve().parents[1] / "docker_web.py").read_text(encoding="utf-8")


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
            'class="live-log-card"', 'id="selectedCountText"', 'id="clearLogBtn"',
            'class="metadata-title-grid"', 'class="archive-main-grid"',
        ):
            self.assertIn(marker, INDEX_HTML)
        self.assertIn("DARK CONSOLE — PREVIEW MATCH", INDEX_HTML)
        self.assertIn("grid-template-columns: minmax(650px, 1.03fr) minmax(610px, .97fr)", INDEX_HTML)
        self.assertIn("applyTheme('dark')", INDEX_HTML)

    def test_preview_layout_keeps_live_log_visible_with_queue(self):
        self.assertIn('.live-log-card #panel-log, .live-log-card #panel-log.active', INDEX_HTML)
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
        self.assertIn("--bg: #edf1f7", INDEX_HTML)

    def test_settings_center_exposes_nine_curated_themes(self):
        for theme, label in (
            ("dark", "曜石深色"),
            ("light", "云雾浅色"),
            ("linen", "暖砂浅色"),
            ("mint", "薄荷浅色"),
            ("rose", "蔷薇浅色"),
            ("ocean", "深海蓝"),
            ("aurora", "极光紫"),
            ("jade", "松石青"),
            ("graphite", "钛金灰"),
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
        for pool_id in ("authorPool", "anchorPool", "teamPool", "seriesPool", "blacklistPool"):
            self.assertIn(pool_id, INDEX_HTML)

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

    @patch("docker_web.extract_advanced_info", return_value=(["移动端标签"], ""))
    @patch("docker_web.ximalaya_api")
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


if __name__ == "__main__":
    unittest.main()

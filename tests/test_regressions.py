import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from metadata_helpers import build_output_folder_name
from processor import load_operation_snapshot, restore_operation_snapshot, save_operation_snapshot
from docker_web import INDEX_HTML, collect_tags_and_year_from_payload, collect_ximalaya_app_tags, fetch_api_metadata


class RegressionTests(unittest.TestCase):
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

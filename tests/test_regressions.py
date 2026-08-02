import json
import tempfile
import unittest
from pathlib import Path

from metadata_helpers import build_output_folder_name
from processor import load_operation_snapshot, restore_operation_snapshot, save_operation_snapshot
from docker_web import collect_tags_and_year_from_payload, merge_ximalaya_inferred_tags, normalize_ximalaya_payload


class RegressionTests(unittest.TestCase):
    def test_ximalaya_app_show_tags_are_collected(self):
        payload = {
            "data": {
                "newShowTags": [{"title": "影视原著"}, {"displayName": "历史小说"}],
                "albumMetaValueInfos": [{"metadataValueName": "出版物"}],
            }
        }
        tags, _ = collect_tags_and_year_from_payload(payload)
        self.assertTrue({"影视原著", "历史小说", "出版物"}.issubset(set(tags)))

    def test_ximalaya_hidden_app_labels_have_high_confidence_fallback(self):
        raw = {
            "albumPageMainInfo": {
                "albumTitle": "大明王朝1566丨历史正剧",
                "categoryId": 1001,
                "categoryTitle": "有声图书",
                "isPaid": True,
                "sellingPoint": {"sp_feeling": "影视级后期"},
            }
        }
        data = normalize_ximalaya_payload(raw)
        tags = merge_ximalaya_inferred_tags(data, raw)
        self.assertTrue({"有声图书", "影视原著", "历史小说", "出版物"}.issubset(set(tags)))

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

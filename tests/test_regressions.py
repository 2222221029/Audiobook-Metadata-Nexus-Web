import json
import tempfile
import unittest
from pathlib import Path

from metadata_helpers import build_output_folder_name
from processor import load_operation_snapshot, restore_operation_snapshot, save_operation_snapshot


class RegressionTests(unittest.TestCase):
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

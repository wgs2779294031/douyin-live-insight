"""Synthetic checks for the local catalog builder, without publisher state."""
import contextlib
from datetime import datetime, timedelta
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.local_data import build_index


class BuildIndexTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "shared"
        self.root.mkdir()
        self.output = self.base / "staged-index.jsonl"

    def partition(self, day="2026-11-06", tag="示例", count=1, filename=None):
        directory = self.root / "数据" / day
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (filename or tag + ".jsonl")
        beginning = datetime.fromisoformat(day).replace(tzinfo=build_index.BEIJING)
        counts = dict.fromkeys(build_index.STREAMS, 0)
        counts["speech"] = count
        footer = {
            "stream": "export_info", "schema": 1, "platform": "douyin", "tag": tag,
            "counts": counts, "from": beginning.isoformat(),
            "until_exclusive": (beginning + timedelta(days=1)).isoformat(),
            "source_present": {key: bool(value) for key, value in counts.items()},
            "window_is_session": False,
        }
        rows = [{"stream": "speech", "ts": beginning.timestamp() + offset,
                 "platform": "douyin", "tag": tag} for offset in range(count)]
        path.write_bytes(b"".join(
            (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
            for row in [*rows, footer]))
        return path

    def build(self):
        with contextlib.redirect_stdout(io.StringIO()):
            build_index.build(self.root, self.output)

    def test_only_nested_records_enter_local_index(self):
        path = self.partition()
        old_layout = self.root / path.parent.name
        old_layout.mkdir()
        (old_layout / path.name).write_bytes(path.read_bytes())
        self.build()
        rows = [json.loads(line) for line in self.output.read_bytes().splitlines()]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["path"], "数据/2026-11-06/示例.jsonl")
        self.assertEqual(row["reading_guide"], "索引使用说明.md")
        self.assertEqual(row["records"], 1)
        self.assertEqual(row["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual((self.root / row["path"]).read_bytes(), path.read_bytes())

    def test_bad_footer_keeps_previous_staging_output(self):
        path = self.partition()
        self.build()
        previous = self.output.read_bytes()
        path.write_bytes(path.read_bytes().replace(b'"speech":1', b'"speech":2', 1))
        with self.assertRaisesRegex(ValueError, "row-count mismatch"):
            self.build()
        self.assertEqual(self.output.read_bytes(), previous)

    def test_overlapping_export_windows_are_rejected(self):
        self.partition()
        self.partition(filename="overlap.jsonl")
        with self.assertRaisesRegex(ValueError, "Overlapping export windows"):
            self.build()
        self.assertFalse(self.output.exists())

    def test_inventory_changes_during_inspection_keep_previous_output(self):
        self.partition()
        self.build()
        previous = self.output.read_bytes()
        original_inspect = build_index.inspect

        def inspect_then_add(root, path):
            result = original_inspect(root, path)
            self.partition(day="2026-11-05")
            return result

        with patch.object(build_index, "inspect", side_effect=inspect_then_add):
            with self.assertRaisesRegex(ValueError, "inventory changed"):
                self.build()
        self.assertEqual(self.output.read_bytes(), previous)


if __name__ == "__main__":
    unittest.main()

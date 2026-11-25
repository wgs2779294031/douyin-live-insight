from datetime import date, datetime
import json
from pathlib import Path
import tempfile
import unittest

from scripts.local_data.export_records import BEIJING, publish


class ExportChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.dest = self.root / "share"
        self.day = date(2026, 11, 5)
        self.tag = "示例-主播"
        self.ts = datetime(2026, 11, 5, 14, 27, tzinfo=BEIJING).timestamp()

    def write(self, suffix, rows):
        path = self.data / suffix
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        return path

    def publish(self, **kw):
        return publish(self.data, self.dest, self.tag, self.day, **kw)

    def test_cross_stream_window_field_masks_and_repeat(self):
        self.write(f"watch_{self.tag}.jsonl", [
            {"ts": self.ts-86400, "is_live": True, "web_rid": "1"},
            {"ts": self.ts, "is_live": False, "error": "DO_NOT_COPY", "api_msg": "DO_NOT_COPY"},
            {"ts": self.ts + 2, "is_live": False, "live_status": 0},
            {"ts": self.ts + 3, "is_live": True, "web_rid": "19", "room_id_str": "19"},
        ])
        self.write(f"stats_douyin_{self.tag}_{self.day}.jsonl", [
            {"ts": self.ts, "type": "room_user_seq", "online": 0, "total_pv": None,
             "ranks": [{"user_id": "DO_NOT_COPY"}]},
        ])
        self.write(f"events_douyin_{self.tag}_{self.day}.jsonl", [
            {"ts": self.ts, "type": "raw_event", "method": "WebcastExample", "payload_hex": "DO_NOT_COPY"},
            {"ts": self.ts, "type": "lottery_draw_result", "candidate_num": 9, "extra_json": "DO_NOT_COPY",
             "winners_blob_head_hex":"DO_NOT_COPY"},
            {"ts": self.ts, "type":"teamfight", "sides":[{"score":4, "user_id":"DO_NOT_COPY"}]},
        ])
        self.write(f"danmu_douyin_{self.tag}_{self.day}.jsonl", [
            {"ts": self.ts, "type":"danmaku", "text":"原问 13812345678", "uname":"DO_NOT_COPY"},
        ])
        self.write(f"speech_{self.tag}_{self.day}.jsonl", [
            {"ts": self.ts, "seg":"seg_0007", "off":4.5, "dur":1, "text":"完整句子 https://test.invalid/DO_NOT_COPY。"},
        ])
        self.write(f"room_meta_douyin_{self.tag}.json", [{"fetched_at":self.ts+86400, "title":"DO_NOT_COPY"}])
        self.write(f"portrait_{self.tag}_{self.day}.jsonl", [{"private":"DO_NOT_COPY"}])
        receipt = self.publish(start="14:27:00", end="14:27:03")
        output = next(self.dest.rglob("*.jsonl"))
        all_text = output.read_text(encoding="utf-8")
        self.assertNotIn("DO_NOT_COPY",all_text)
        self.assertNotIn("13812345678",all_text)
        rows=list(map(json.loads,all_text.splitlines()))
        self.assertEqual([r["state"] for r in rows if r["stream"]=="watch"],["unknown","offline"])
        self.assertEqual(receipt["counts"],{"watch":2,"room_meta":0,"stats":1,"danmu":1,"events":3,"speech":1})
        self.assertIn("完整句子",all_text)
        self.assertIn('"score":4',all_text)
        self.assertFalse(self.publish(start="14:27:00", end="14:27:03")["changed"])

    def test_live_tail_and_failed_replacement(self):
        name=f"danmu_douyin_{self.tag}_{self.day}.jsonl"
        p=self.write(name,[{"ts":self.ts,"type":"danmaku","text":"一"}])
        with p.open("ab") as f: f.write(b'{"broken":')
        receipt=self.publish()
        self.assertEqual(receipt["counts"]["danmu"],1)
        target=next(self.dest.rglob("*.jsonl"))
        before=target.read_bytes()
        with p.open("ab") as f: f.write(b'\n')
        with self.assertRaisesRegex(ValueError, "invalid source"):
            self.publish()
        self.assertEqual(before,target.read_bytes())
        self.assertEqual(list(self.root.glob(".nas-export-*")),[])

    def test_adjacent_date_and_empty_window(self):
        self.write(f"speech_{self.tag}_2026-11-04.jsonl",[
            {"ts":self.ts,"tag":self.tag,"text":"next calendar day","seg":"seg_0001","off":3}])
        self.assertEqual(self.publish()["counts"]["speech"],1)
        with self.assertRaisesRegex(ValueError, "No business rows"):
            self.publish(start="01:00:00",end="02:00:00")
        self.assertEqual(len(list(self.dest.rglob("*.jsonl"))),1)


if __name__ == "__main__":
    unittest.main()

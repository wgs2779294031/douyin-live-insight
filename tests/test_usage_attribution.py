"""Automatic metrics keep material date separate from actual SDK send date."""
import json

from test_llm_lifecycle import sandbox, snapshot, write_gate, eventually


def test_realtime_worker_attaches_input_day_and_room_without_rewriting_send_time(sandbox):
    runtime, path, data = sandbox.runtime(enabled=True, room_llm=True)
    write_gate(data)
    runtime.start()
    eventually(lambda: runtime.llm_status()["state"] == "waiting_input")
    runtime._push_llm(snapshot("generated old input"))  # Source snapshot ts=1.
    eventually(lambda: len(sandbox.calls) == 1)
    def records():
        return [json.loads(line) for file in data.glob("llm_metrics_*.jsonl")
                for line in file.read_text(encoding="utf-8").splitlines()]
    eventually(lambda: bool(records()))
    record = records()[0]
    assert record["room_tag"] == "synthetic" and record["purpose"] == "realtime"
    assert record["data_date"] == "1970-01-01"
    assert record["sdk_attempt_usage"][0]["ts"] > 1_000_000_000
    assert record["metrics_version"] == 2

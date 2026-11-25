# -*- coding: utf-8 -*-
"""事后批量画像测试：tmp 构造弹幕 → stub Router → 断言同构落盘/幂等/脱敏。

零真实 API 调用；不触碰真实 data/。
"""
import json
import hashlib
import copy
import os
import socket
from types import ModuleType, SimpleNamespace
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import portrait_backfill as pb  # noqa: E402

TAG = "测试主播A"
DATE = "2026-10-10"
T0 = 1791630000

_FAKE_PORTRAIT = json.dumps({
    "interest_topics": ["考研院校"],
    "question_topics": [{"topic": "复试线", "count": 2, "examples": ["复试多少分"]}],
    "sentiment": {"positive": 0.6, "neutral": 0.4, "negative": 0.0},
    "consumption_intent": {"level": "medium", "keywords": ["资料"]},
    "activity_layers": {"high": [], "mid": [], "low": []},
    "suggestions": ["多讲复试线"],
}, ensure_ascii=False)


class _StubRouter:
    """替身 Router：恒返回固定 JSON 文本，记录调用次数。"""

    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail
        self.seen_payloads = []

    def chat(self, system, user, temperature=0.7, timeout_override=None,
             purpose=""):
        self.calls += 1
        self.seen_payloads.append(user)
        return None if self.fail else _FAKE_PORTRAIT


def _mk_svc():
    return pb.PortraitService(_StubRouter())


def _mk_data(tmp_path, n_windows=3):
    """构造 n_windows 个画像窗的弹幕（每窗 2 条，间隔 130s）。"""
    d = tmp_path
    rows = []
    for w in range(n_windows):
        base = T0 + w * 130
        rows.append({"ts": base, "type": "danmaku", "text": f"问题{w}a",
                     "room_id": "r1", "user_id": "u-secret", "uname": "用户A"})
        rows.append({"ts": base + 5, "type": "like", "room_id": "r1"})
    (d / f"danmu_douyin_{TAG}_{DATE}.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
        encoding="utf-8")
    return d


def test_backfill_writes_same_shape_as_realtime(tmp_path):
    """落盘行结构与实时版同构：ts/room_tag/portrait（normalize 齐全字段）。"""
    d = _mk_data(tmp_path)
    r = pb.backfill_tag(d, TAG, DATE, _mk_svc(), every=120, min_danmaku=1)
    assert r["status"] == "done" and r["ok"] == r["windows"] == 2  # 3 窗首窗只记起点
    out = d / f"portrait_{TAG}_{DATE}.jsonl"
    rows = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2
    for row in rows:
        assert set(row) == {"ts", "room_tag", "portrait"}
        p = row["portrait"]
        for k in ("portrait_ts", "interest_topics", "question_topics",
                  "consumption_intent", "activity_layers", "sentiment",
                  "suggestions"):
            assert k in p, k
        assert isinstance(p["question_topics"], list)


def test_backfill_scrubs_user_id(tmp_path):
    """匿名化红线：user_id 与值不得出现在产出文件中。"""
    d = _mk_data(tmp_path)
    svc = pb.PortraitService(_RouterCapture(_FAKE_PORTRAIT))
    pb.backfill_tag(d, TAG, DATE, svc, every=120, min_danmaku=1)
    out = (d / f"portrait_{TAG}_{DATE}.jsonl").read_text(encoding="utf-8")
    assert "user_id" not in out and "u-secret" not in out


class _RouterCapture(_StubRouter):
    def chat(self, system, user, temperature=0.7, timeout_override=None,
             purpose=""):
        self.calls += 1
        self.payload = user
        return _FAKE_PORTRAIT


def test_backfill_idempotent_skip_and_force(tmp_path):
    """已有画像文件默认跳过；--force 追加重做（看板读末行自然覆盖）。"""
    d = _mk_data(tmp_path)
    r1 = pb.backfill_tag(d, TAG, DATE, _mk_svc(), every=120, min_danmaku=1)
    assert r1["status"] == "done"
    out = d / f"portrait_{TAG}_{DATE}.jsonl"
    n1 = len(out.read_text(encoding="utf-8").splitlines())
    r2 = pb.backfill_tag(d, TAG, DATE, _mk_svc(), every=120, min_danmaku=1)
    assert r2["status"] == "skipped"
    assert len(out.read_text(encoding="utf-8").splitlines()) == n1
    r3 = pb.backfill_tag(d, TAG, DATE, _mk_svc(), every=120, min_danmaku=1,
                         force=True)
    assert r3["status"] == "done"
    assert len(out.read_text(encoding="utf-8").splitlines()) == n1 * 2


def test_backfill_respects_max_calls(tmp_path):
    """单日调用封顶：max_calls=1 时即使有 2 个画像窗也只调 1 次。"""
    d = _mk_data(tmp_path)
    svc = _mk_svc()
    pb.backfill_tag(d, TAG, DATE, svc, every=120, min_danmaku=1, max_calls=1)
    assert svc.client.calls == 1


def test_backfill_skips_empty_windows(tmp_path):
    """冷场窗口弹幕数 < min_danmaku 的点跳过（对齐实时空窗熔断，0 成本）。"""
    d = tmp_path
    # 只有 like 无 danmaku：冷场窗口 count=0，全部熔断
    rows = [{"ts": T0 + i * 130, "type": "like", "room_id": "r1"}
            for i in range(3)]
    (d / f"danmu_douyin_{TAG}_{DATE}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    svc = _mk_svc()
    r = pb.backfill_tag(d, TAG, DATE, svc, every=120, min_danmaku=1)
    assert r["status"] == "done" and r["ok"] == 0 and svc.client.calls == 0


def test_backfill_no_danmu(tmp_path):
    """无弹幕文件 → no_danmu，不产文件。"""
    r = pb.backfill_tag(tmp_path, TAG, DATE, _mk_svc())
    assert r["status"] == "no_danmu"
    assert not (tmp_path / f"portrait_{TAG}_{DATE}.jsonl").exists()


def test_input_snapshot_records_exact_bytes_and_does_not_follow_appends(tmp_path):
    _mk_data(tmp_path)
    path = tmp_path / f"danmu_douyin_{TAG}_{DATE}.jsonl"
    original = path.read_bytes() + b'\n{"ts":true}\nnot-json\n'
    path.write_bytes(original)
    records, captured = pb._snapshot_input(tmp_path, TAG, DATE)
    path.write_bytes(original + b'\n{"ts":9999999999,"type":"danmaku","text":"late"}\n')
    assert captured["sha256"] == hashlib.sha256(original).hexdigest()
    assert captured["bytes"] == len(original) and captured["records"] == 6
    assert captured["invalid_records"] == 2
    assert captured["ts_min"] == T0 and captured["ts_max"] == T0 + 265
    assert len(list(pb._replay_windows(records, 120))) == 2


def test_all_dates_ignores_other_tags_with_same_prefix_and_invalid_dates(tmp_path):
    for name in (f"danmu_douyin_{TAG}_{DATE}.jsonl", f"danmu_douyin_{TAG}_other_{DATE}.jsonl",
                 f"danmu_douyin_{TAG}_2026-99-99.jsonl"):
        (tmp_path / name).write_text("", encoding="utf-8")
    assert pb._dates_for_tag(tmp_path, TAG) == [DATE]


class _Run:
    def __init__(self, path, task_id="synthetic-task-a", **parameters):
        self.path = path
        self._record = {"task_id": task_id, "tag": TAG, "data_date": DATE,
                        "parameters": {"every": 120, "min_danmaku": 1, "max_calls": 40, **parameters},
                        "control_epoch": [0, 0, 0], "state": "running", "status": "running",
                        "dispatch_settled": True}
        self.stop_reason = ""
        self.fail_marker = self.fail_checkpoint = self.fail_finish = False
        self.dispatches, self.progresses, self.finishes = [], [], []
    @property
    def record(self):
        return copy.deepcopy(self._record)
    def guard(self):
        import llm_client
        if self.stop_reason:
            return False
        current = llm_client.application_control(str(self.path), str(self.path.parent),
                                                  tag=TAG, purpose="review")
        if not current["allowed"]:
            self.stop_reason = current["reason"]
        elif current["epoch"] != self._record["control_epoch"]:
            self.stop_reason = "control_changed"
        return not self.stop_reason
    def before_send(self, provider, model):
        self.dispatches.append((provider, model))
        self._record["dispatch_settled"] = False
        if self.fail_marker:
            self.stop_reason = "task_state_unavailable"
            raise OSError("synthetic-private-marker-error")
        return True
    def progress(self, **fields):
        self.progresses.append(copy.deepcopy(fields))
        if self.fail_checkpoint and fields.get("dispatch_settled") and self.dispatches:
            raise OSError("synthetic-private-checkpoint-error")
        self._record.update(copy.deepcopy(fields))
    def finish(self, state, *, reason="", **fields):
        self.finishes.append((state, reason))
        if self.fail_finish:
            self.fail_finish = False
            raise OSError("synthetic-private-finish-error")
        self._record.update(copy.deepcopy(fields), state=state, status=state, reason=reason)
        return self.public()
    def public(self):
        return self.record


@pytest.fixture
def worker_env(monkeypatch, tmp_path):
    import ssl
    import client
    import llm_client
    def denied(*args, **kwargs):
        raise AssertionError("network forbidden in portrait worker tests")
    monkeypatch.setattr(socket, "socket", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    calls, instances = [], []
    env = SimpleNamespace(calls=calls, instances=instances, hook=None, content=_FAKE_PORTRAIT)
    class SDK:
        def __init__(self, api_key, **kwargs):
            assert kwargs["max_retries"] == 0
            self.key, self.closed = api_key, False
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
            instances.append(self)
        def create(self, **kwargs):
            calls.append({"key": self.key, **kwargs})
            if env.hook:
                env.hook()
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=env.content))],
                                   usage=SimpleNamespace(prompt_tokens=2, completion_tokens=1))
        def close(self):
            self.closed = True
    module = ModuleType("openai")
    module.OpenAI = SDK
    monkeypatch.setitem(sys.modules, "openai", module)
    monkeypatch.setattr(llm_client, "_RETRY_BACKOFF_BASE", 0)
    llm_client.invalidate_gate_cache()
    path = tmp_path / "config.json"
    def configure(key="synthetic-worker-key", budget=0):
        path.write_text(json.dumps({"llm": {"enabled": True, "active_provider": "synthetic",
            "daily_token_budget": budget, "providers": {"synthetic": {
                "api_key": key, "base_url": "https://example.invalid/v1", "model": "synthetic-model",
                "protocol": "openai_compatible"}}}}), encoding="utf-8")
    configure()
    env.path, env.configure = path, configure
    yield env
    assert all(item.closed for item in instances)


def test_managed_run_identity_exact_snapshot_metrics_and_append(tmp_path, worker_env):
    _mk_data(tmp_path)
    old = b'{"portrait":{"historical":true}}\n'
    output = tmp_path / f"portrait_{TAG}_{DATE}.jsonl"
    output.write_bytes(old)
    original = (tmp_path / f"danmu_douyin_{TAG}_{DATE}.jsonl").read_bytes()
    run = _Run(worker_env.path)
    result = pb.execute_task(run, tmp_path, str(worker_env.path))
    assert result["state"] == "succeeded" and result["counts"]["ok"] == 2
    assert len(worker_env.calls) == len(run.dispatches) == 2
    assert result["input_snapshot"]["sha256"] == hashlib.sha256(original).hexdigest()
    assert output.read_bytes().startswith(old)
    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()][1:]
    assert all(row["task_id"] == run.record["task_id"] and row["room_tag"] == TAG
               and row["purpose"] == "review" for row in rows)
    ref = result["output_ref"]
    assert ref["byte_start"] == len(old) and ref["byte_end"] == output.stat().st_size and ref["rows"] == 2
    metrics = list(tmp_path.glob("llm_metrics_*.jsonl"))
    records = [json.loads(line) for line in metrics[0].read_text(encoding="utf-8").splitlines()]
    assert all(row["task_id"] == run.record["task_id"] and row["purpose"] == "review" and row["room_tag"] == TAG for row in records)


def test_managed_snapshot_does_not_pick_up_new_data_during_model_call(tmp_path, worker_env):
    _mk_data(tmp_path)
    source = tmp_path / f"danmu_douyin_{TAG}_{DATE}.jsonl"
    initial = source.read_bytes()
    def append():
        if len(worker_env.calls) == 1:
            with source.open("a", encoding="utf-8") as stream:
                stream.write('\n' + json.dumps({"ts": T0 + 999, "type": "danmaku", "text": "synthetic late"}) + '\n')
    worker_env.hook = append
    result = pb.execute_task(_Run(worker_env.path), tmp_path, str(worker_env.path))
    assert result["state"] == "succeeded" and len(worker_env.calls) == 2
    assert result["input_snapshot"]["bytes"] == len(initial)
    assert result["input_snapshot"]["sha256"] == hashlib.sha256(initial).hexdigest()


def test_managed_timeout_is_unknown_and_does_not_scan_remaining_windows(tmp_path, worker_env, caplog):
    _mk_data(tmp_path, n_windows=5)
    def timeout():
        raise TimeoutError("synthetic-private-timeout")
    worker_env.hook = timeout
    run = _Run(worker_env.path)
    result = pb.execute_task(run, tmp_path, str(worker_env.path))
    assert result["state"] == "unknown" and result["reason"] == "request_result_unknown"
    assert result["counts"]["calls"] == result["counts"]["unknown"] == 1
    assert result["counts"]["fail"] == 0 and result["counts"]["windows_seen"] == 1
    assert not result["dispatch_settled"] and len(worker_env.calls) == 3
    assert "synthetic-private-timeout" not in caplog.text


def test_managed_timeout_then_pause_stays_unknown_not_ordinary_blocked(tmp_path, worker_env):
    _mk_data(tmp_path, n_windows=5)
    def timeout_and_pause():
        (tmp_path / "llm_runtime.json").write_text(json.dumps({"enabled": False, "pause_generation": 1}), encoding="utf-8")
        raise TimeoutError("synthetic timeout during pause")
    worker_env.hook = timeout_and_pause
    result = pb.execute_task(_Run(worker_env.path), tmp_path, str(worker_env.path))
    assert result["state"] == "unknown" and result["reason"] == "request_result_unknown"
    assert result["counts"]["unknown"] == 1 and result["counts"]["fail"] == 0
    assert not result["dispatch_settled"] and len(worker_env.calls) == 1


def test_managed_known_response_invalid_portrait_is_failure_not_unknown(tmp_path, worker_env):
    _mk_data(tmp_path)
    worker_env.content = "synthetic invalid JSON"
    result = pb.execute_task(_Run(worker_env.path), tmp_path, str(worker_env.path))
    assert result["state"] == "failed" and result["counts"]["fail"] == 2
    assert result["counts"]["unknown"] == 0 and result["dispatch_settled"]
    assert len(worker_env.calls) == 2


def test_managed_call_limit_is_partial_with_exact_processed_scope(tmp_path, worker_env):
    _mk_data(tmp_path, n_windows=5)
    result = pb.execute_task(_Run(worker_env.path, max_calls=1), tmp_path, str(worker_env.path))
    assert result["state"] == "partial" and result["reason"] == "call_limit"
    assert result["counts"]["windows_total"] == 4 and result["counts"]["windows_seen"] == 1
    assert result["counts"]["ok"] == result["counts"]["calls"] == len(worker_env.calls) == 1
    assert result["processed"]["processed_ts_end"] == T0 + 130


@pytest.mark.parametrize("pause", ["global", "review", "quick_resume", "key"])
def test_managed_issued_success_can_land_then_task_stops_remaining_windows(tmp_path, worker_env, pause):
    _mk_data(tmp_path, n_windows=5)
    def stop():
        if pause == "key":
            worker_env.configure("")
        else:
            gate = {"enabled": pause != "global", "pause_generation": int(pause in ("global", "quick_resume"))}
            if pause == "review":
                gate.update(purposes={"review": False}, purpose_pause_generation={"review": 1})
            (tmp_path / "llm_runtime.json").write_text(json.dumps(gate), encoding="utf-8")
    worker_env.hook = stop
    result = pb.execute_task(_Run(worker_env.path), tmp_path, str(worker_env.path))
    assert result["state"] == "blocked" and result["counts"]["ok"] == 1
    assert result["counts"]["calls"] == 1 and result["counts"]["fail"] == 0
    assert len(worker_env.calls) == 1 and result["output_ref"]["rows"] == 1


def test_managed_manual_tag_off_does_not_block_but_budget_does(tmp_path, worker_env):
    import llm_client
    _mk_data(tmp_path)
    (tmp_path / "llm_runtime.json").write_text(json.dumps({"enabled": True, "tags_off": [TAG]}), encoding="utf-8")
    result = pb.execute_task(_Run(worker_env.path), tmp_path, str(worker_env.path))
    assert result["state"] == "succeeded" and len(worker_env.calls) == 2
    worker_env.configure(budget=1)
    llm_client.DailyTokenBudget(1, str(tmp_path)).add(1, 0)
    result = pb.execute_task(_Run(worker_env.path, task_id="synthetic-budget-task"), tmp_path, str(worker_env.path))
    assert result["state"] == "blocked" and result["reason"] == "daily_budget_exhausted"
    assert result["counts"]["fail"] == 0 and len(worker_env.calls) == 2


@pytest.mark.parametrize("failure", ["marker", "checkpoint", "output", "finish"])
def test_managed_persistence_failures_are_unknown_and_do_not_resume(tmp_path, worker_env, monkeypatch, failure):
    _mk_data(tmp_path, n_windows=5)
    run = _Run(worker_env.path)
    if failure == "marker":
        run.fail_marker = True
    elif failure == "checkpoint":
        run.fail_checkpoint = True
    elif failure == "finish":
        run.fail_finish = True
    else:
        def denied(*args):
            raise OSError("synthetic-private-output-error")
        monkeypatch.setattr(pb, "_append_task_output", denied)
    result = pb.execute_task(run, tmp_path, str(worker_env.path))
    assert result["state"] == "unknown"
    assert len(worker_env.calls) == (0 if failure == "marker" else 4 if failure == "finish" else 1)
    if failure != "finish":
        assert not result["dispatch_settled"]


def test_standard_cli_uses_store_reuse_and_explicit_force_new_run(tmp_path, worker_env, capsys):
    _mk_data(tmp_path)
    argv = ["--tag", TAG, "--date", DATE, "--config", str(worker_env.path), "--data-dir", str(tmp_path)]
    assert pb.main(argv) == 0
    first = json.loads(capsys.readouterr().out.splitlines()[-1])
    output = tmp_path / f"portrait_{TAG}_{DATE}.jsonl"
    old = output.read_bytes()
    source = tmp_path / f"danmu_douyin_{TAG}_{DATE}.jsonl"
    with source.open("a", encoding="utf-8") as stream:
        stream.write('\n' + json.dumps({"ts": T0 + 1, "type": "danmaku", "text": "synthetic late append"}) + '\n')
    assert pb.main(argv) == 0
    reused = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert reused["reused"] and reused["task_id"] == first["task_id"] and len(worker_env.calls) == 2
    assert reused["task"]["input"]["sha256"] == first["task"]["input"]["sha256"]
    assert pb.main(argv + ["--force"]) == 0
    forced = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert forced["task_id"] != first["task_id"] and len(worker_env.calls) == 4
    assert forced["task"]["input"]["sha256"] != first["task"]["input"]["sha256"]
    assert output.read_bytes().startswith(old)


def test_standard_cli_unknown_run_requery_does_not_issue_again(tmp_path, worker_env, capsys):
    _mk_data(tmp_path, n_windows=5)
    def timeout():
        raise TimeoutError("synthetic result unknown")
    worker_env.hook = timeout
    argv = ["--tag", TAG, "--date", DATE, "--config", str(worker_env.path), "--data-dir", str(tmp_path)]
    assert pb.main(argv) == 2
    first = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert first["status"] == "unknown" and len(worker_env.calls) == 3
    assert pb.main(argv) == 2
    reused = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert reused["reused"] and reused["task_id"] == first["task_id"]
    assert len(worker_env.calls) == 3


def test_child_cli_only_claims_and_does_not_reserve_or_accept_scope_overrides(tmp_path, worker_env, monkeypatch, capsys):
    calls = []
    run = _Run(worker_env.path)
    class Store:
        def __init__(self, **kwargs):
            pass
        def claim(self, task_id, **kwargs):
            calls.append((task_id, kwargs))
            return run
        def reserve_inline(self, *args, **kwargs):
            raise AssertionError("child must not reserve")
    module = ModuleType("backfill_tasks")
    module.TaskStore = Store
    monkeypatch.setitem(sys.modules, "backfill_tasks", module)
    monkeypatch.setattr(pb, "execute_task", lambda *args: {"state": "succeeded", "task_id": run.record["task_id"]})
    args = ["--task-id", "synthetic-child", "--config", str(worker_env.path), "--data-dir", str(tmp_path)]
    assert pb.main(args) == 0 and calls == [("synthetic-child", {"timeout": 10})]
    with pytest.raises(SystemExit):
        pb.main(args + ["--tag", TAG])


@pytest.mark.parametrize("stages", [
    "known_timeout", "timeout_known", "timeout_success", "known_known", "known_success_pause",
])
def test_borrow_stage_observation_preserves_real_store_lease_and_force_boundary(tmp_path, worker_env, stages):
    import llm_client
    import backfill_tasks as tasks
    _mk_data(tmp_path, n_windows=5)
    providers = {name: {"api_key": "synthetic-stage-key-" + name,
                        "base_url": "https://example.invalid/v1", "model": "synthetic-stage-model-" + name}
                 for name in ("primary", "a", "b")}
    worker_env.path.write_text(json.dumps({"llm": {"enabled": True, "active_provider": "primary",
        "daily_token_budget": 1, "providers": providers}}), encoding="utf-8")
    llm_client.DailyTokenBudget(1, str(tmp_path)).add(1, 0)
    class Rejected(RuntimeError):
        status_code = 401
    def hook():
        on_a = worker_env.calls[-1]["key"] == providers["a"]["api_key"]
        if (on_a and stages.startswith("known_")) or (not on_a and stages.endswith("_known")):
            raise Rejected("synthetic known response")
        if (on_a and stages.startswith("timeout_")) or (not on_a and stages.endswith("_timeout")):
            raise TimeoutError("synthetic unknown result")
        if stages == "known_success_pause":
            (tmp_path / "llm_runtime.json").write_text(json.dumps({
                "enabled": False, "pause_generation": 1}), encoding="utf-8")
    worker_env.hook = hook
    store = tasks.TaskStore(config_path=worker_env.path, data_dir=tmp_path)
    _, run = store.reserve_inline(TAG, DATE)
    assert run is not None
    task_id = run.task_id
    result = pb.execute_task(run, tmp_path, str(worker_env.path))
    manifest = json.loads(store._path(task_id).read_text(encoding="utf-8"))
    raw = next(row for row in manifest["runs"] if row["task_id"] == task_id)
    assert tasks._same_identity(raw["owner"], tasks.process_identity(os.getpid()))
    unknown = stages in ("known_timeout", "timeout_known", "timeout_success")
    assert result["state"] == ("unknown" if unknown else "blocked")
    assert raw["counts"]["unknown"] == int(unknown)
    assert raw["counts"]["ok"] == int(stages in ("timeout_success", "known_success_pause"))
    assert raw["counts"]["windows_seen"] == raw["counts"]["calls"] == 1
    assert raw["counts"]["fail"] == 0
    assert raw["dispatch_settled"] is (not unknown)
    assert raw["lease"]["released"] is (not unknown)
    assert raw["dispatch_count"] == len(worker_env.calls) == (4 if unknown else 2)
    if stages in ("timeout_success", "known_success_pause"):
        rows = [json.loads(line) for line in (tmp_path / f"portrait_{TAG}_{DATE}.jsonl").read_text(encoding="utf-8").splitlines()]
        assert len(rows) == 1 and rows[0]["task_id"] == task_id
    if stages != "known_success_pause":
        response, redo = store.reserve_inline(TAG, DATE, force=True, request_id="synthetic-stage-explicit-redo")
        assert (redo is None) is unknown
        assert (response["task_id"] == task_id) is unknown
        if redo is not None:
            redo.finish("blocked", reason="synthetic_cleanup", dispatch_settled=True)


@pytest.mark.parametrize("at", ["marker", "issued_reply", "issued_timeout"])
def test_customer_stop_keeps_known_and_unresolved_dispatches_distinct(tmp_path, worker_env, monkeypatch, at):
    import backfill_tasks as tasks
    _mk_data(tmp_path, n_windows=4)
    root = pathlib.Path(__file__).resolve().parents[1]
    context = {"schema": 1, "root": str(root), "config": str(worker_env.path),
        "data": str(tmp_path), "roster": str(tmp_path / "roster.json"), "dist": str(tmp_path / "dist"),
        "python": sys.executable, "profile": str(tmp_path / "private-profile"),
        "port": 18765, "cdp_port": 19222, "instance_id": "synthetic_manual_stop"}
    context_path, stop_path = tmp_path / "context.json", tmp_path / "stop.json"
    context_path.write_text(json.dumps(context), encoding="utf-8")
    monkeypatch.setenv("DY_CUSTOMER_CONTEXT", str(context_path))
    monkeypatch.setenv("DY_CUSTOMER_STOP", str(stop_path))
    gate = tmp_path / "llm_runtime.json"
    gate.write_text('{"enabled":true,"pause_generation":0}', encoding="utf-8")
    config_before, gate_before = worker_env.path.read_bytes(), gate.read_bytes()
    identity = tasks.process_identity(os.getpid())

    def stop():
        stop_path.write_text(json.dumps({"schema": 1, "action": "stop",
            "instance_id": context["instance_id"], "pid": identity["pid"],
            "created": identity["created"]}), encoding="utf-8")

    store = tasks.TaskStore(config_path=worker_env.path, data_dir=tmp_path)
    _, run = store.reserve_inline(TAG, DATE, request_id="synthetic-customer-stop-first")
    assert run is not None
    if at == "marker":
        before_send = run.before_send
        def marked(*args, **kwargs):
            result = before_send(*args, **kwargs)
            stop()
            return result
        monkeypatch.setattr(run, "before_send", marked)
    else:
        def issued():
            stop()
            if at == "issued_timeout":
                raise TimeoutError("synthetic unresolved reply")
        worker_env.hook = issued

    result = pb.execute_task(run, tmp_path, str(worker_env.path))
    raw = next(row for row in json.loads(store._path(run.task_id).read_text(encoding="utf-8"))["runs"]
               if row["task_id"] == run.task_id)
    output = tmp_path / f"portrait_{TAG}_{DATE}.jsonl"
    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()] if output.exists() else []
    known = at == "issued_reply"
    assert raw["counts"]["windows_total"] == 3
    assert raw["counts"]["calls"] == raw["counts"]["windows_seen"] == raw["dispatch_count"] == 1
    assert len(worker_env.calls) == int(at != "marker")
    assert raw["counts"]["ok"] == len(rows) == int(known)
    assert raw["counts"]["unknown"] == int(not known) and raw["counts"]["fail"] == 0
    assert result["state"] == ("blocked" if known else "unknown")
    assert result["reason"] == ("application_stopping" if known else "request_result_unknown")
    assert raw["dispatch_settled"] is known and raw["lease"]["released"] is known
    assert raw["finished_at"] is not None
    if known:
        assert rows[0]["task_id"] == run.task_id and rows[0]["portrait"]["suggestions"] == ["多讲复试线"]
        ref = raw["output"]
        assert ref["rows"] == 1 and ref["byte_start"] == 0 and ref["byte_end"] == output.stat().st_size

    # A new explicit attempt after stop clears is allowed only when prior dispatch is settled.
    stop_path.unlink()
    response, redo = store.reserve_inline(TAG, DATE, force=True, request_id="synthetic-customer-stop-redo")
    assert (redo is not None) is known
    assert (response["task_id"] != run.task_id) is known
    if redo is not None:
        redo.finish("blocked", reason="synthetic_cleanup", dispatch_settled=True)
    original = next(row for row in json.loads(store._path(run.task_id).read_text(encoding="utf-8"))["runs"]
                    if row["task_id"] == run.task_id)
    assert original == raw and len(worker_env.calls) == int(at != "marker")
    assert worker_env.path.read_bytes() == config_before and gate.read_bytes() == gate_before

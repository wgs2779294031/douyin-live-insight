"""飞书 app 后端 —— 接通性自检（自建应用直写多维表格）。

为什么需要这个脚本
------------------------------------------------
`tests/test_feishu_app.py` 用替身把逻辑跑通，但**测不出飞书那一侧配错了**：
表 ID 抄错、列名少了、应用没被加进这张多维表格、当月配额用完 —— 这些
错误码只有在真发请求时才会露出来，而且往往要等到开播后落盘失败才发现。

本脚本就是**开播前的 30 秒体检**，按「先查配置 → 再验权限 → 再验列 → 最后
真写一行再删掉」的顺序分四步，每一步失败都直接告诉你该去哪儿改什么。

四步分别验什么
------------------------------------------------
[1] 配置解析：backend 是不是 app、必填项齐不齐、**每个房间落到哪张表**
    （多房间下 `table_id_map` 按房间标识选表；单房间只能走兜底 `table_id`）
[2] 凭证权限：拿一次 `tenant_access_token`。这一步过了 = app_id/app_secret 对；
[3] 表与列：读该表的字段列表，和落盘行要写的 31 个字段逐个比对。
    缺列会直接告诉你「差哪几个」（对应错误码 1254045 FieldNameNotFound）；
    列类型不对也会提示（数字列写成文本列 → 1254015）；
[4] 真实写入：往每张表各写 1 条探针记录，**立刻删掉**。
    这一步过了 = 应用已被加进这张多维表格、且有编辑权限。

用法（在项目根目录执行）
------------------------------------------------
    python scripts/check_feishu_app.py              # 只做 [1]~[3]，不写任何数据
    python scripts/check_feishu_app.py --write      # 追加 [4]，写探针后自动删除
    python scripts/check_feishu_app.py --config other.json
    python scripts/check_feishu_app.py --tag 自家-主播A   # 只查某一个房间

★ [4] 的探针行用 `room_id="__probe__"`、`platform="probe"` 作标记，
  万一删除失败，你可以在表里按这两列搜出来手动删掉。

★ 全程只读 + 一写一删；不做任何账号动作，不改表结构。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from feishu_sink import (  # noqa: E402
    FeishuApiError,
    FeishuConfigError,
    FeishuSinkConfig,
    TenantTokenProvider,
    build_row,
    build_record_payload,
    load_feishu_config,
    resolve_table_id,
    row_to_fields,
)

# 飞书多维表格字段类型码（只用得上前两个）
_TYPE_NAMES = {
    1: "多行文本", 2: "数字", 3: "单选", 4: "多选", 5: "日期", 7: "复选框",
    11: "人员", 13: "电话号码", 15: "超链接", 17: "附件", 18: "单向关联",
    19: "查找引用", 20: "公式", 21: "双向关联", 22: "地理位置",
    1001: "创建时间", 1002: "最后更新时间", 1003: "创建人", 1004: "修改人",
    1005: "自动编号",
}

_OK, _BAD, _WARN = "[OK]  ", "[FAIL]", "[WARN]"


# ================================================================ HTTP 原语
def _http(method: str, url: str, headers: Dict[str, str],
          payload: Optional[Dict[str, Any]], timeout: float
          ) -> Tuple[int, Dict[str, Any]]:
    """最小 HTTP 封装，返回 (状态码, 响应体 dict)。"""
    import requests

    resp = requests.request(method, url, headers=headers, json=payload,
                            timeout=timeout)
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 - 网关 5xx 可能返回 HTML
        body = {}
    if not isinstance(body, dict):
        body = {}
    return int(resp.status_code), body


def _auth_headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8"}


def _api_transport(url: str, headers: Dict[str, str],
                   payload: Optional[Dict[str, Any]], timeout: float
                   ) -> Tuple[int, Dict[str, Any]]:
    """POST 版传输，签名与 `feishu_sink._default_api_transport` 一致。

    ★ 必须包一层：`TenantTokenProvider` 是按 4 参位置调用 transport 的
    （它只发 POST），不能直接把带 method 参数的 `_http` 塞进去。
    """
    return _http("POST", url, headers, payload, timeout)


# ================================================================ 房间 → 表
def _planned_targets(cfg_path: str, only_tag: Optional[str] = None
                     ) -> Tuple[List[Tuple[Optional[str], str, str]], bool]:
    """算出「哪个房间 → 写哪张表」。

    ★ 刻意复用 `main._resolve_rooms` —— 房间解析与文件名标识的规则只允许有
    一份实现；自检脚本自己再推一遍，迟早会和真跑起来的选表逻辑对不上。

    :return: ([(room_tag_or_None, 房间显示名, table_id), ...], is_multi)
        room_tag 为 None 表示单房间（legacy）模式，此时 table_id_map 不生效。
    """
    import main as main_mod

    cfg = main_mod.load_config(cfg_path)
    # _resolve_rooms 只用到 args.room / args.config 两项
    args = argparse.Namespace(config=cfg_path, room=None)
    rooms, multi = main_mod._resolve_rooms(cfg, args)
    fcfg = load_feishu_config(cfg_path)

    targets: List[Tuple[Optional[str], str, str]] = []
    if multi:
        for i, room in enumerate(rooms):
            tag = room.file_tag(i)
            if only_tag and tag != only_tag:
                continue
            label = room.label or tag
            targets.append((tag, f"{label}（{room.room_id}）",
                            resolve_table_id(fcfg, tag)))
    else:
        if only_tag:
            print(f"{_WARN} --tag {only_tag} 只对多房间模式有意义；"
                  f"当前配置没有 rooms 数组，忽略该参数")
        rid = getattr(cfg, "web_rid", "") or "(未填)"
        targets.append((None, f"单房间（{rid}）", resolve_table_id(fcfg, None)))
    return targets, multi


def _expected_fields() -> Dict[str, Any]:
    """用一条空快照构造出落盘行，取出「要写哪些列、每列什么类型」。

    这样列清单永远和 build_row 同步，不会因为字段改名而失准。
    """
    row = build_row({}, room_id="probe", platform="probe")
    return row_to_fields(row)


def _expected_type_code(value: Any) -> int:
    if isinstance(value, bool):
        return 7          # 复选框
    if isinstance(value, (int, float)):
        return 2          # 数字
    return 1              # 多行文本


# ================================================================ 各步检查
def step1_config(cfg: FeishuSinkConfig, targets, multi: bool) -> bool:
    print("\n[1] 配置解析")
    print(f"    backend={cfg.backend!r}  enabled={cfg.enabled}  "
          f"模式={'多房间' if multi else '单房间(legacy)'}  "
          f"api_base={cfg.api_base}  timeout={cfg.timeout_seconds}s")

    if str(cfg.backend).strip() != "app":
        print(f"{_BAD} backend 不是 'app'（当前 {cfg.backend!r}）。"
              f"本自检只针对自建应用直写；想验 dry_run/webhook 请用对应的方式。")
        print("      改法：config.json → feishu.backend = \"app\"")
        return False

    if not cfg.enabled:
        print(f"{_WARN} feishu.enabled=false —— 配置能验通，但真正跑采集时"
              f"**不会落盘**。要落盘记得改成 true。")

    try:
        cfg.validate()
    except FeishuConfigError as e:
        print(f"{_BAD} 配置校验失败：{e}")
        return False
    print(f"{_OK} 必填项齐全（app_id / app_secret / app_token / table 已配）")
    print(f"      app_id={_mask(cfg.app_id)}  app_secret={_mask(cfg.app_secret, 0)}  "
          f"app_token={cfg.app_token}  table_id_map={len(cfg.table_id_map)} 项")

    print("    房间 → 数据表 映射：")
    for tag, label, tid in targets:
        print(f"      {label:<28s} → table_id={tid}"
              f"{'   （来自兜底 table_id）' if tag is None else ''}")

    if not cfg.table_id_map:
        print(f"{_WARN} table_id_map 为空 —— 所有房间都会写同一张表。"
              f"多房间下建议一房间一张表（见 README §5.5）")
    elif not multi:
        # 单房间模式下 room_tag 恒为 None，映射永远命中不到 → 静默失效的一类坑
        print(f"{_WARN} 配了 table_id_map，但当前是**单房间模式**（没有 rooms 数组）"
              f"→ 映射不会生效，只会走兜底 table_id={cfg.table_id!r}。"
              f"要么把房间写进 rooms，要么把 table_id 填上。")
    return True


def step2_token(cfg: FeishuSinkConfig
                ) -> Tuple[bool, Optional[TenantTokenProvider], Optional[str]]:
    print("\n[2] 凭证与权限（换取 tenant_access_token）")
    provider = TenantTokenProvider(
        app_id=cfg.app_id, app_secret=cfg.app_secret,
        transport=_api_transport, api_base=cfg.api_base, timeout=cfg.timeout_seconds,
        refresh_margin_seconds=cfg.token_refresh_margin_seconds,
    )
    try:
        token = provider.get()
    except FeishuApiError as e:
        print(f"{_BAD} {e}")
        print("      排查：开发者后台「凭证与基础信息」里的 App ID / App Secret "
              "是否抄全；是否误用了 App Token。")
        return False, None, None
    except Exception as e:  # noqa: BLE001 - 断网 / DNS / 代理等
        print(f"{_BAD} 请求飞书失败：{type(e).__name__}: {e}")
        print(f"      排查：能否访问 {cfg.api_base}（代理 / 防火墙 / DNS）；"
              f"或先用 --config 指向另一份配置。")
        return False, None, None
    print(f"{_OK} 拿到 token：{_mask(token, 4)}（第 {provider.fetch_count} 次获取，"
          f"官方最长有效 2 小时，到期前自动换）")
    return True, provider, token


def step3_fields(cfg: FeishuSinkConfig, token: str, targets) -> bool:
    print("\n[3] 数据表字段核对（表 ID、应用是否已加入、列名与列类型）")
    expected = _expected_fields()
    all_ok = True
    for tag, label, tid in targets:
        print(f"    --- {label} → {tid} ---")
        url = (f"{cfg.api_base.rstrip('/')}/open-apis/bitable/v1/apps/"
               f"{cfg.app_token}/tables/{tid}/fields?page_size=100")
        try:
            status, body = _http("GET", url, _auth_headers(token), None,
                                 cfg.timeout_seconds)
        except Exception as e:  # noqa: BLE001 - 网络层异常也要转成人话
            print(f"{_BAD} 请求失败：{type(e).__name__}: {e}")
            all_ok = False
            continue

        code = body.get("code")
        if status != 200 or code != 0:
            print(f"{_BAD} 读字段失败：HTTP {status} code={code} msg={body.get('msg')!r}")
            all_ok = False
            continue

        items = ((body.get("data") or {}).get("items")) or []
        actual = {str(it.get("field_name")): int(it.get("type") or 0)
                  for it in items if isinstance(it, dict)}
        print(f"      线上已有 {len(actual)} 列")

        missing = [k for k in expected if k not in actual]
        if missing:
            print(f"{_BAD} 缺 {len(missing)} 列：{'、'.join(missing)}")
            print("      照下面这份列清单在表里补列（列名必须逐字符一致）：")
            for k, v in expected.items():
                mark = "缺" if k in missing else "有"
                print(f"        [{mark}] {k:<26s} 应为 {_TYPE_NAMES[_expected_type_code(v)]}")
            all_ok = False
        else:
            print(f"{_OK} {len(expected)} 列全部存在")

        type_bad = []
        for k, v in expected.items():
            if k not in actual:
                continue
            want = _expected_type_code(v)
            got = actual[k]
            if got != want:
                type_bad.append((k, _TYPE_NAMES.get(got, f"type={got}"),
                                 _TYPE_NAMES[want]))
        if type_bad:
            print(f"{_WARN} {len(type_bad)} 列类型不一致（飞书可能报 1254015）：")
            for k, got, want in type_bad[:10]:
                print(f"        {k:<26s} 线上是「{got}」，应为「{want}」")
        elif not missing:
            print(f"{_OK} 列类型与落盘值一致")

        extra = [k for k in actual if k not in expected]
        if extra:
            print(f"      （线上另有 {len(extra)} 列落盘不会写："
                  f"{'、'.join(sorted(extra)[:6])}{' …' if len(extra) > 6 else ''}）")
    return all_ok


def step4_write(cfg: FeishuSinkConfig, provider: TenantTokenProvider, targets) -> bool:
    print("\n[4] 真实写入探针（写完立刻删除）")
    row = build_row({}, room_id="__probe__", platform="probe")
    row["ts"] = int(time.time())          # 带真实时间，便于在表里找到
    row["date"] = time.strftime("%Y-%m-%d")
    all_ok = True
    for tag, label, tid in targets:
        print(f"    --- {label} → {tid} ---")
        base = (f"{cfg.api_base.rstrip('/')}/open-apis/bitable/v1/apps/"
                f"{cfg.app_token}/tables/{tid}/records")
        try:
            token = provider.get()
        except Exception as e:  # noqa: BLE001
            print(f"{_BAD} 取 token 失败：{type(e).__name__}: {e}")
            all_ok = False
            continue

        # 幂等键：探针也带上，防超时重试插重复行
        payload = build_record_payload(row)
        try:
            status, body = _http("POST", base, _auth_headers(token), payload,
                                 cfg.timeout_seconds)
        except Exception as e:  # noqa: BLE001
            print(f"{_BAD} 写入请求异常：{type(e).__name__}: {e}")
            all_ok = False
            continue

        code = body.get("code")
        if status != 200 or code != 0:
            print(f"{_BAD} 写入失败：HTTP {status} code={code} msg={body.get('msg')!r}")
            _explain_write_error(code)
            all_ok = False
            continue

        rec = ((body.get("data") or {}).get("record")) or {}
        record_id = rec.get("record_id")
        print(f"{_OK} 写入成功 record_id={record_id}")

        if not record_id:
            print(f"{_WARN} 响应里没有 record_id，无法自动删除 —— "
                  f"请到表里搜 platform=probe 手动删掉这一行")
            continue

        durl = f"{base}/{record_id}"
        try:
            dstatus, dbody = _http("DELETE", durl, _auth_headers(token), None,
                                   cfg.timeout_seconds)
        except Exception as e:  # noqa: BLE001
            print(f"{_WARN} 删除请求异常：{type(e).__name__}: {e} → "
                  f"请手动删 record_id={record_id}")
            continue
        dcode = dbody.get("code")
        if dstatus == 200 and dcode == 0:
            print(f"{_OK} 探针已删除，表已复原")
        else:
            print(f"{_WARN} 删除失败：HTTP {dstatus} code={dcode} msg={dbody.get('msg')!r}"
                  f" → 请手动删 record_id={record_id}")
    return all_ok


def _explain_write_error(code: Any) -> None:
    hints = {
        1254291: "同一数据表不支持并发写 → 确认没有第二个进程在写同一张表；"
                 "本项目已内置进程级按表串行锁",
        1254290: "触发频控（单表约 50 次/秒）→ 调大 feishu.min_interval_seconds",
        1255040: "飞书侧 504 超时 → 稍后重试；本项目已做退避重试",
        1254004: "WrongTableId → table_id 抄错了（URL 里 table= 后面那段）",
        1254045: "FieldNameNotFound → 上面 [3] 报的缺列的问题",
        1254015: "字段类型不匹配 → 数字列别建成文本列，改列类型或改列名",
        1254302: "Permission denied → 应用没被加进这张多维表格。"
                 "打开多维表格 → 右上「...」→「...更多」→「添加文档应用」→ 搜你的应用名",
        1254304: "Permission denied → 同上；另外确认已开通权限"
                 "「查看、评论、编辑和管理多维表格」",
        99991403: "当月 API 调用量已用尽（基础免费版约 10,000 次/月）→ "
                  "等次月 1 号刷新或升级版本",
        99991663: "tenant_access_token 无效 → 脚本会自动换一次；仍失败则查凭证",
    }
    if isinstance(code, int) and code in hints:
        print(f"      说明：{hints[code]}")


def _mask(s: str, keep: int = 4) -> str:
    s = str(s or "")
    if not s:
        return "(空)"
    if len(s) <= keep:
        return "*" * len(s)
    return s[:keep] + "*" * max(len(s) - keep, 4)


# ================================================================ 入口
def main(cfg_path: str = "config.json", do_write: bool = False,
         only_tag: Optional[str] = None) -> int:
    if not os.path.exists(cfg_path):
        print(f"{_BAD} 找不到配置文件 {cfg_path}")
        return 2

    print("=" * 68)
    print("飞书 app 后端接通性自检")
    print(f"配置文件：{os.path.abspath(cfg_path)}")
    print("=" * 68)

    fcfg = load_feishu_config(cfg_path)
    from config import ConfigError

    try:
        targets, multi = _planned_targets(cfg_path, only_tag)
    except (FeishuConfigError, ConfigError) as e:
        print(f"\n[1] 配置解析\n{_BAD} 房间→表 解析失败：{e}")
        return 1
    if not targets:
        print(f"{_BAD} 没有可检查的房间（--tag {only_tag} 没匹配上？）")
        return 1

    if not step1_config(fcfg, targets, multi):
        return 1

    ok2, provider, token = step2_token(fcfg)
    if not ok2:
        return 1

    ok3 = step3_fields(fcfg, token, targets)

    ok4 = True
    if do_write:
        ok4 = step4_write(fcfg, provider, targets)
    else:
        print("\n[4] 真实写入探针 —— 已跳过（加 --write 才会执行）")

    print("\n" + "=" * 68)
    if ok3 and ok4:
        print("体检通过：配置、凭证、表结构、写入权限都正常。")
        if not do_write:
            print("建议再跑一次 `--write` 确认写入权限（会写 1 行再删掉）。")
        print("接下来：config.json → feishu.enabled = true，即可开播落盘。")
        return 0
    print("体检未通过 —— 按上面 [FAIL] 的提示逐条改，改完重跑本脚本。")
    return 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="飞书 app 后端接通性自检")
    ap.add_argument("--config", default="config.json", help="配置文件路径")
    ap.add_argument("--write", action="store_true",
                    help="真实写入 1 条探针记录并立刻删除（验写入权限）")
    ap.add_argument("--tag", default=None,
                    help="只检查某个房间标识（多房间模式下的 label）")
    a = ap.parse_args()
    sys.exit(main(a.config, a.write, a.tag))

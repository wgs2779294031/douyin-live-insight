# -*- coding: utf-8 -*-
"""从原话指纹生成人设卡「草稿」+ 机械体检（2026-10-28）。

为什么是「草稿」：实测 doubao-lite / deepseek 都能写出 70~80% 可用的卡，但
**稳定犯四类错**（都是机械可查的，不靠人眼）：

1. 把 `fan_call` 塞进 `catchphrases`（占掉一个槽）；
2. `forbidden_topics` 给空 —— 合规闸门直接没了；
3. `interaction_preference` 写成字符串（`load_anchor_profile` 只认 list，字段会被丢掉）；
4. 泛化 `category`（"升学规划指导"），丢掉"专业与就业方向拆解"这层定位。

所以流程是：LLM 出草稿 → 本脚本机械体检 → 人只看 ⚠️ 那几行 → 改名成正式卡。
草稿文件名 `_draft_<tag>.json`，**不会被 llm_service 加载**（它只认 `{tag}.json`）。

用法：
    python scripts/profile_draft.py --tag 框框看升学
    python scripts/profile_draft.py --tag 框框看升学 --provider deepseek
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import profile_digest as pd  # noqa: E402

# 合规兜底：模型给空时必须补上（这些是赛道通用禁区，宁可多不可少）
DEFAULT_FORBIDDEN = ["保过", "包过", "保录取", "保offer", "内推收费", "竞品机构"]

SYSTEM = """你是直播运营，负责把主播的原话指纹整理成一张「人设卡」。

规则：
1. 只输出一个 JSON 对象，不要 markdown、不要解释；
2. 字段：name / category / style / catchphrases / fan_call / audience /
   interaction_preference / forbidden_topics；
3. catchphrases 最多 3 条，**只能从「候选口癖」里选特异性高的**（≥2），
   不要选特异性≈1 的通用词，也不要把 fan_call 塞进 catchphrases；
4. fan_call 只填**一个**称呼；
5. interaction_preference 是字符串数组；
6. 所有字段值加起来不超过 300 个中文字符（硬上限，超了会被系统砍掉）；
7. 只能依据指纹里出现过的内容，不要编。"""


def read_digest(tag: str, path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    m = re.search(r"^### %s\n(.*?)(?=^### |\Z)" % re.escape(tag), text, re.S | re.M)
    return m.group(1) if m else ""


def candidate_tics(digest_text: str) -> list:
    """digest 里「候选口癖」那一行 = 特异性 ≥1.5 的短词白名单。"""
    m = re.search(r"候选口癖.*?\n  (.*)", digest_text)
    if not m:
        return []
    return [x.split("×")[0] for x in m.group(1).split("、") if x]


def check_card(card: dict, digest_text: str) -> "tuple[list, dict]":
    """机械体检：返回 (问题列表, 修好的卡)。问题为空才算过。"""
    import llm_service as L

    issues = []
    card = dict(card or {})
    cps = [str(c).strip() for c in (card.get("catchphrases") or []) if str(c).strip()]
    fan = str(card.get("fan_call") or "").strip()

    if len(cps) > 3:
        issues.append("catchphrases 超过 3 条（注入时只取前 3）：%s" % cps)
    allowed = set(candidate_tics(digest_text))
    bad = [c for c in cps if allowed and c not in allowed]
    if bad:
        issues.append("口癖不在候选里（可能选了通用词/编的）：%s" % bad)
    if fan and fan in cps:
        issues.append("fan_call「%s」同时出现在 catchphrases 里，白发一个槽" % fan)
    if not fan:
        issues.append("fan_call 为空")
    elif len(fan) > 6 or "、" in fan or "，" in fan:
        issues.append("fan_call 应是单个称呼，现在是「%s」（台词校验会永远零命中）" % fan)
    elif fan not in pd.ADDRESS:
        issues.append("fan_call「%s」不在常用称呼表里，确认主播真这么叫" % fan)
    # 称呼词占口癖槽：catchphrases 应该是「他说话的方式」，不是「他怎么叫观众」
    addr_in_cps = [c for c in cps if c in pd.ADDRESS]
    if addr_in_cps:
        issues.append("catchphrases 里放了称呼词 %s（那是 fan_call 的活，白占一个槽）"
                      % addr_in_cps)

    inter = card.get("interaction_preference")
    if inter is not None and not isinstance(inter, list):
        issues.append("interaction_preference 不是数组（会被 load_anchor_profile 丢掉）：%r" % inter)
        card["interaction_preference"] = [str(inter)] if str(inter or "").strip() else []
    forb = card.get("forbidden_topics")
    _PLACEHOLDER = {"未提及", "无", "没有", "暂无", "无明确提及", "无明确", ""}

    def _is_placeholder(x: str) -> bool:
        s = x.strip()
        return s in _PLACEHOLDER or s.startswith(("无明确", "未提及", "暂无"))

    forb_real = [str(x).strip() for x in (forb or []) if str(x).strip()
                 and not _is_placeholder(str(x))] if isinstance(forb, list) else []
    if not forb_real:
        issues.append("forbidden_topics 为空或只有占位词 %r → 已补默认合规清单，请人工确认"
                      % (forb,))
        card["forbidden_topics"] = list(DEFAULT_FORBIDDEN)

    inject, trimmed = L._trim_profile(card)
    if trimmed:
        issues.append("总长超 %d 字，注入时会被裁掉字段：%s" % (L._INJECT_MAX_CHARS, ",".join(trimmed)))
        if "forbidden_topics" in trimmed:
            issues.append("**裁掉的正好是 forbidden_topics → 合规闸门失效**，必须压短 style/audience")
    return issues, card


FACT_FIELDS = ("fan_call", "catchphrases", "forbidden_topics")
"""「事实字段」：只有人/团队知道，模型不该自动改。

fan_call = 他真怎么叫观众；catchphrases = 他真这么说；forbidden_topics = 这个赛道
不能说什么。`--apply` 默认**只允许改描述层**（style/audience/category），动这三个
必须显式 `--force` —— 这就是「半自动」的分界线：日常更新不用人写，但事实不许机器擅改。
"""


def diff_cards(old: dict, new: dict) -> list:
    """返回 [(字段, 旧值, 新值)]，只列有变化的键。"""
    keys = sorted(set(old) | set(new))
    return [(k, old.get(k), new.get(k)) for k in keys if old.get(k) != new.get(k)]


def apply_card(target: Path, card: dict, force: bool = False):
    """把草稿写进正式卡。返回 (ok, 消息, 事实字段变化列表)。"""
    old = {}
    if target.exists():
        try:
            old = json.loads(target.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            old = {}
    fact = [(f, old.get(f), card.get(f)) for f in FACT_FIELDS
            if old.get(f) != card.get(f)]
    if old and fact and not force:
        return False, "事实字段有变化，需 --force", fact
    if old:
        bdir = target.parent / "_backup"
        bdir.mkdir(exist_ok=True)
        stamp = __import__("time").strftime("%Y%m%d-%H%M%S")
        (bdir / ("%s_%s.json" % (target.stem, stamp))).write_text(
            json.dumps(old, ensure_ascii=False, indent=2), encoding="utf-8")
    target.write_text(json.dumps(card, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True, "已写入 %s" % target, fact


def main() -> int:
    ap = argparse.ArgumentParser(description="用系统内 LLM 生成人设卡草稿（带机械体检）")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--provider", help="doubao / deepseek / deepseek_official（默认配置里的 active）")
    ap.add_argument("--digest", default=str(ROOT / "data" / "profile_digest.md"))
    ap.add_argument("--cards", default=str(ROOT / "anchor_profiles"))
    ap.add_argument("--apply", action="store_true",
                    help="体检全过时直接写入正式卡 <tag>.json（覆盖前自动备份到 _backup/）")
    ap.add_argument("--force", action="store_true",
                    help="允许 --apply 改动事实字段（fan_call / catchphrases / forbidden_topics）")
    ap.add_argument("--restart", action="store_true",
                    help="--apply 成功后重启该路的 watch（只 watch，不动录音）让新卡生效")
    args = ap.parse_args()

    digest_text = read_digest(args.tag, Path(args.digest))
    if not digest_text:
        print("digest 里没有 %s，先跑 scripts/profile_digest.py --tag %s" % (args.tag, args.tag),
              file=sys.stderr)
        return 2

    import llm_service as L

    cfg = L.load_llm_config(str(ROOT / "config.json"))
    router = cfg.build_router(provider_override=args.provider or None)
    if router is None:
        print("没有可用的 LLM provider", file=sys.stderr)
        return 2

    out = router.chat(SYSTEM, "主播 tag：%s\n\n原话指纹：\n%s" % (args.tag, digest_text),
                      temperature=0.3, purpose="review")
    if not out:
        print("LLM 没有返回（看 metrics 日志）", file=sys.stderr)
        return 2
    try:
        card = json.loads(out[out.find("{"):out.rfind("}") + 1])
    except Exception as e:  # noqa: BLE001
        print("模型输出不是 JSON：%s\n---\n%s" % (e, out[:500]), file=sys.stderr)
        return 1

    issues, fixed = check_card(card, digest_text)
    draft = Path(args.cards) / ("_draft_%s.json" % args.tag)
    draft.write_text(json.dumps(fixed, ensure_ascii=False, indent=2), encoding="utf-8")

    print("provider=%s model=%s" % (router.active, router.active_model))
    print("草稿：%s（**不会被自动加载**，改名成 %s.json 才生效）" % (draft, args.tag))
    print(json.dumps(fixed, ensure_ascii=False, indent=2))
    print("-" * 50)
    if issues:
        for i in issues:
            print("⚠️  %s" % i)
        print("→ 改完上面几项再改名；退出码 1")
        return 1
    print("✅ 机械体检全过（仍需人看一眼 category/audience 是不是这个主播）")

    if not args.apply:
        return 0

    target = Path(args.cards) / ("%s.json" % args.tag)
    ok, msg, fact = apply_card(target, fixed, force=args.force)
    print(msg)
    if not ok:
        for f, o, n in fact:
            print("   %s：%r → %r" % (f, o, n))
        print("→ 这三项是人定的「事实」，确认要改再加 --force（改了主播会照着念）")
        return 1
    if fact:
        print("（事实字段变化 %d 项，已 --force 覆盖）" % len(fact))

    if args.restart:
        sys.path.insert(0, str(ROOT / "scripts"))
        import anchor_manager as am  # noqa: PLC0415

        import anchor_roster as ar  # noqa: PLC0415
        entry = ar.find(ar.load(), args.tag)
        if not entry:
            print("⚠️ 名册里找不到 %s，卡已写入但没重启，下次开播自己生效" % args.tag)
            return 0
        print(am.restart_anchor(args.tag, entry, kinds=("watch",)))
    else:
        print("→ 没加 --restart：卡已写入，下次开播（进程启动）才生效")
    return 0


if __name__ == "__main__":
    sys.exit(main())

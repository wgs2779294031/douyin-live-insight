# -*- coding: utf-8 -*-
"""监控看板生成器：读 data/ 最近 7 天原始数据 → 生成单文件 dashboard.html。

2026-10-07 立项（替代只认旧无 tag 文件名的 scripts/dashboard.py）：
  - 视图：主播 Tab（二级）→ 日期条（默认最新有数据的一天）+「7 天总览」对比条；
  - 只在下播后由 watch_anchor_live.py 触发（在播/开机自启不触发）；
  - 只读不删：data/ 下任何 jsonl 一个不动，看板只是展示层；
  - 零外部依赖、零常驻进程：数据内嵌 JSON + 原生 JS/CSS/SVG，双击即开；
  - 原子写（tmp + os.replace），六实例并发触发安全；
  - 生成失败只打日志——铁律：监控先活，看板尽力。

体积控制：弹幕明细不进 HTML（只进统计）；stats 在线时序抽稀到每分钟 1 点。
脱敏说明：user_id/uname 已脱敏，「参与人数」用 stats 的 total_pv/online。
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import date as _date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from launch_all import ANCHORS  # noqa: E402  名册单一来源 (名, sec_uid, 中文标签)

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = DATA / "dashboard.html"
DAYS = 7
TYPE_LABELS = {"danmaku": "发言", "enter": "进场", "like": "点赞", "gift": "礼物"}


# ---------------------------------------------------------------- 基础工具
def iter_jsonl(path: Path):
    """容错读 jsonl：坏行/半行直接跳过。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def last_jsonl(path: Path) -> Optional[Dict[str, Any]]:
    """取 jsonl 最后一行（画像/探测记录都是全量快照）。"""
    last = None
    for rec in iter_jsonl(path):
        last = rec
    return last


def dates_for(tag: str) -> List[str]:
    """该主播有数据的日期并集（danmu/portrait/stats 三类文件名里的日期）。"""
    dates = set()
    for prefix in ("danmu_douyin_%s_", "portrait_%s_", "stats_douyin_%s_"):
        for p in DATA.glob((prefix % tag) + "*.jsonl"):
            stem = p.stem
            # 文件名尾部 8 位日期：..._2026-10-07
            tail = stem.rsplit("_", 1)[-1]
            if len(tail) == 10 and tail[4] == "-" and tail[7] == "-":
                dates.add(tail)
    if not dates:
        return []
    today = _date.today()
    floor = (today - timedelta(days=DAYS - 1)).isoformat()
    recent = sorted(d for d in dates if d >= floor)
    return recent[-DAYS:]


def downsample(series: List[List[int]], per_minute: int = 1) -> List[List[int]]:
    """[[ts, v], ...] 抽稀：每 per_minute 秒桶保留最后一个点。"""
    if not series:
        return []
    bucket = int(per_minute) * 60
    out: List[List[int]] = []
    cur_key = None
    cur_val = None
    for ts, v in series:
        key = ts // bucket
        if key != cur_key:
            if cur_key is not None:
                out.append([cur_key * bucket, cur_val])
            cur_key, cur_val = key, v
        else:
            cur_val = v
    if cur_key is not None:
        out.append([cur_key * bucket, cur_val])
    return out


# ---------------------------------------------------------------- 单日加载
def _load_room_meta(tag: str) -> Dict[str, Any]:
    p = DATA / ("room_meta_douyin_%s.json" % tag)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _room_id_of(meta: Dict[str, Any]) -> str:
    return str(meta.get("room_id") or "")


def load_day(tag: str, day: str, meta: Dict[str, Any],
             room_ids: set) -> Optional[Dict[str, Any]]:
    """汇总某主播某天的：弹幕统计 / 人气时序 / 画像 / 冷场。"""
    d = {"danmu": None, "stats": None, "portrait": None, "cold": None}

    # ---- danmu：按 type 计数 + 首末条（时长） ----
    p = DATA / ("danmu_douyin_%s_%s.jsonl" % (tag, day))
    if p.exists():
        counts = {k: 0 for k in ("danmaku", "enter", "like", "gift")}
        first_ts = last_ts = None
        for rec in iter_jsonl(p):
            t = rec.get("type")
            if t in counts:
                counts[t] += 1
            ts = rec.get("ts")
            if isinstance(ts, int):
                if first_ts is None:
                    first_ts = ts
                last_ts = ts
        counts["total"] = sum(counts.values())
        duration_min = None
        if first_ts and last_ts and last_ts >= first_ts:
            duration_min = max(1, round((last_ts - first_ts) / 60))
        d["danmu"] = {"counts": counts, "first_ts": first_ts,
                      "last_ts": last_ts, "duration_min": duration_min}

    # ---- stats：online 抽稀时序 + 峰值 + total_pv ----
    p = DATA / ("stats_douyin_%s_%s.jsonl" % (tag, day))
    if p.exists():
        series: List[List[int]] = []
        peak = 0
        total_pv = None
        for rec in iter_jsonl(p):
            ts = rec.get("ts")
            if rec.get("type") == "room_stats":
                online = rec.get("online")
                if isinstance(online, int) and isinstance(ts, int):
                    series.append([ts, online])
                    peak = max(peak, online)
            elif rec.get("type") == "room_user_seq":
                pv = rec.get("total_pv")
                if isinstance(pv, int):
                    total_pv = max(total_pv or 0, pv)
        d["stats"] = {"online_series": downsample(series),
                      "online_peak": peak, "total_pv": total_pv}

    # ---- portrait：最后一行 = 全量快照 ----
    p = DATA / ("portrait_%s_%s.jsonl" % (tag, day))
    if p.exists():
        last = last_jsonl(p)
        if last and isinstance(last.get("portrait"), dict):
            d["portrait"] = last["portrait"]

    # ---- coldline：按天文件，room_id 区分主播 ----
    p = DATA / ("coldline_%s.jsonl" % day)
    if p.exists() and room_ids:
        count = 0
        last_lines: List[str] = []
        last_ts = -1
        for rec in iter_jsonl(p):
            if str(rec.get("room_id")) not in room_ids:
                continue
            if rec.get("event") == "cold_start":
                count += 1
                ts = rec.get("ts")
                if isinstance(ts, int) and ts >= last_ts:
                    last_ts = ts
                    lines = rec.get("lines")
                    if isinstance(lines, list):
                        last_lines = [str(x) for x in lines[:3]]
        if count:
            d["cold"] = {"count": count, "last_lines": last_lines}

    # 场次标题：room_meta 是「最近一场」覆盖写——只有最新日期才可信
    if d["danmu"]:
        d["meta"] = {"title": meta.get("title"), "web_rid": meta.get("web_rid"),
                     "start_time": meta.get("start_time"),
                     "anchor_name": meta.get("anchor_name")}
    return d if (d["danmu"] or d["stats"] or d["portrait"] or d["cold"]) else None


def load_status(tag: str) -> Dict[str, Any]:
    """watch_<tag>.jsonl 最后一条含 fans/nick 的记录 → 在播状态。"""
    st = {"is_live": None, "nick": None, "fans": None, "title": None}
    p = DATA / ("watch_%s.jsonl" % tag)
    for rec in iter_jsonl(p):
        if rec.get("error"):
            continue
        for k in ("is_live", "nick", "fans", "title"):
            if rec.get(k) is not None:
                st[k] = rec[k]
    return st


def load_llm_days(days: List[str]) -> Dict[str, Any]:
    """llm_budget_<date>.json → {date: {total, budget}}（全局，不分主播）。"""
    out: Dict[str, Any] = {}
    for day in days:
        p = DATA / ("llm_budget_%s.json" % day)
        if p.exists():
            try:
                j = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(j, dict) and "total" in j:
                    out[day] = {"total": j.get("total", 0),
                                "budget": j.get("budget", 0)}
            except (json.JSONDecodeError, OSError):
                continue
    return out


# ---------------------------------------------------------------- 汇总
def build_payload() -> Dict[str, Any]:
    anchors: List[Dict[str, Any]] = []
    all_dates: set = set()
    for _, _, tag in ANCHORS:
        meta = _load_room_meta(tag)
        room_ids = {_room_id_of(meta)} - {""}
        days = dates_for(tag)
        all_dates.update(days)
        days_data: Dict[str, Any] = {}
        for day in days:
            dd = load_day(tag, day, meta, room_ids)
            if dd:
                days_data[day] = dd
        # 2026-10-08：近 7 天无任何采集数据的号不进板（如长期未开播的子号），
        # 谁开播谁出现，不用手动删 tab。
        if not days_data:
            continue
        # 7 天总览（对比条）：有 danmu 数据的天才进
        overview = []
        for day in days:
            dd = days_data.get(day)
            if not dd or not dd.get("danmu"):
                continue
            c = dd["danmu"]["counts"]
            overview.append({
                "date": day,
                "total": c.get("total", 0),
                "danmaku": c.get("danmaku", 0),
                "enter": c.get("enter", 0),
                "like": c.get("like", 0),
                "gift": c.get("gift", 0),
                "duration_min": dd["danmu"].get("duration_min"),
                "online_peak": (dd.get("stats") or {}).get("online_peak") or 0,
                "total_pv": (dd.get("stats") or {}).get("total_pv"),
                "cold_count": (dd.get("cold") or {}).get("count", 0),
            })
        anchors.append({
            "tag": tag,
            "status": load_status(tag),
            "dates": days,
            "overview": overview,
            "days_data": days_data,
        })
    days_sorted = sorted(all_dates)[-DAYS:]
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M"),
        "days": days_sorted,
        "llm": load_llm_days(days_sorted),
        "anchors": anchors,
    }


# ---------------------------------------------------------------- HTML
_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="60">
<title>竞品直播监控看板</title>
<style>
:root { --bg:#f5f6f8; --card:#ffffff; --line:#e3e6ea; --txt:#24292f; --sub:#6a737d;
        --red:#d73a49; --blue:#0366d6; --green:#22863a; }
* { box-sizing:border-box; margin:0; padding:0; }
body { font-family:"Microsoft YaHei",system-ui,sans-serif; background:var(--bg);
       color:var(--txt); font-size:14px; padding:16px; }
.topbar { display:flex; justify-content:space-between; align-items:baseline;
          flex-wrap:wrap; gap:8px; margin-bottom:12px; }
.topbar h1 { font-size:20px; }
.topbar .hint { color:var(--sub); font-size:12px; }
.llmbar { color:var(--sub); font-size:12px; }
.tabs { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:12px; }
.tab { padding:8px 14px; background:var(--card); border:1px solid var(--line);
       border-radius:8px; cursor:pointer; user-select:none; }
.tab.active { border-color:var(--blue); color:var(--blue); font-weight:bold; }
.dot { display:inline-block; width:8px; height:8px; border-radius:50%;
       margin-right:6px; background:#c0c4cc; }
.dot.live { background:var(--red); }
.daybar { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:12px; align-items:center; }
.daybtn { padding:5px 10px; background:var(--card); border:1px solid var(--line);
          border-radius:6px; cursor:pointer; font-size:12px; }
.daybtn.active { background:var(--blue); color:#fff; border-color:var(--blue); }
.panel { background:var(--card); border:1px solid var(--line); border-radius:10px;
         padding:16px; margin-bottom:14px; }
.panel h2 { font-size:15px; margin-bottom:10px; }
.cards { display:flex; flex-wrap:wrap; gap:10px; margin-bottom:14px; }
.kpi { flex:1 1 120px; background:var(--bg); border-radius:8px; padding:10px 12px; }
.kpi .v { font-size:22px; font-weight:bold; }
.kpi .k { color:var(--sub); font-size:12px; margin-top:2px; }
.tagline { color:var(--sub); font-size:12px; margin-bottom:10px; }
table { width:100%; border-collapse:collapse; font-size:13px; }
th,td { border-bottom:1px solid var(--line); padding:6px 8px; text-align:left;
        vertical-align:top; }
th { color:var(--sub); font-weight:normal; white-space:nowrap; }
.chips { display:flex; flex-wrap:wrap; gap:6px; margin:6px 0; }
.chip { background:#eef3fb; color:var(--blue); border-radius:12px;
        padding:2px 10px; font-size:12px; }
.muted { color:var(--sub); }
.empty { color:var(--sub); text-align:center; padding:24px 0; }
.coldline { background:#fff8e6; border-radius:6px; padding:8px 10px;
            margin-top:6px; font-size:13px; }
.coldline li { margin-left:18px; }
.overview table td, .overview table th { text-align:right; }
.overview table td:first-child, .overview table th:first-child { text-align:left; }
</style>
</head>
<body>
<div class="topbar">
  <h1>竞品直播监控看板</h1>
  <span class="hint" id="gen"></span>
  <span class="llmbar" id="llm"></span>
</div>
<div class="tabs" id="tabs"></div>
<div id="content"></div>
<script>
const DATA = __PAYLOAD__;

const esc = s => String(s == null ? "" : s)
  .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
  .replace(/"/g,"&quot;").replace(/'/g,"&#39;");
const fmt = n => (n == null ? "—" : Number(n).toLocaleString("zh-CN"));
const hm = ts => { const d = new Date(ts*1000);
  return d.getHours().toString().padStart(2,"0")+":"+d.getMinutes().toString().padStart(2,"0"); };

let curTab = 0, curDay = null, curMode = "day"; // day | overview

function tabLabel(a) {
  const live = a.status.is_live;
  const fans = a.status.fans != null
    ? ' <span style="font-size:11px;color:#6a737d">'
      + (a.status.fans >= 10000 ? (a.status.fans/10000).toFixed(1) + 'w' : a.status.fans) + '粉</span>'
    : '';
  return '<span class="dot'+(live?' live':'')+'"></span>'+esc(a.tag)+fans;
}

// 60s meta refresh 会重置页面——交互状态存 localStorage，刷新后恢复
const SKEY = "wb_dash_state";
function saveState() {
  try { localStorage.setItem(SKEY, JSON.stringify(
    {t:curTab, d:curDay, m:curMode})); } catch(e) {}
}
function loadState() {
  try { const s = JSON.parse(localStorage.getItem(SKEY) || "null");
    if (s && typeof s.t === "number") return s; } catch(e) {}
  return null;
}

function renderTabs() {
  document.getElementById("tabs").innerHTML = DATA.anchors.map((a,i) =>
    '<div class="tab'+(i===curTab?' active':'')+'" onclick="pick('+i+')">'
    + tabLabel(a) + '</div>').join("");
}
function pick(i) { curTab = i; curMode = "day";
  const a = DATA.anchors[i];
  curDay = a.dates.length ? a.dates[a.dates.length-1] : null;
  saveState(); renderTabs(); renderContent(); }

function lineSVG(series) {
  if (!series || series.length < 2) return '<div class="muted">人气数据不足，无法画折线</div>';
  const W=860,H=160,P=28;
  const xs=series.map(p=>p[0]), ys=series.map(p=>p[1]);
  const x0=xs[0], x1=xs[xs.length-1];
  const y0=0, y1=Math.max(1,Math.max(...ys));
  const px=t=>P+(t-x0)/(Math.max(1,x1-x0))*(W-2*P);
  const py=v=>H-P-(v-y0)/Math.max(1,y1-y0)*(H-2*P);
  // 相邻采样点间隔 > 5 分钟（正常每分钟 1 点）= 场次断档：两场直播之间未采样，
  // 断开不连线，避免画出「人慢慢走光」的假斜线
  const GAP=300;
  const segs=[]; let cur=[series[0]];
  for(let i=1;i<series.length;i++){
    if(series[i][0]-series[i-1][0]>GAP){ segs.push(cur); cur=[]; }
    cur.push(series[i]);
  }
  segs.push(cur);
  const lines=segs.filter(s=>s.length>1).map(s=>
    '<polyline points="'+s.map(p=>px(p[0]).toFixed(1)+","+py(p[1]).toFixed(1)).join(" ")
    +'" fill="none" stroke="#0366d6" stroke-width="2"/>').join("");
  // 断档处标注下播/开播时刻（前段末点=下播，后段首点=开播）
  let marks="";
  for(let i=1;i<segs.length;i++){
    const prev=segs[i-1][segs[i-1].length-1], nxt=segs[i][0];
    if(!prev||!nxt) continue;
    const cx=x=>Math.max(P+22,Math.min(W-P-22,px(x)));
    marks+='<text x="'+cx(prev[0])+'" y="'+(py(prev[1])-8)
      +'" font-size="10" fill="#6a737d" text-anchor="middle">下播 '+hm(prev[0])+'</text>'
      +'<text x="'+cx(nxt[0])+'" y="'+(py(nxt[1])-8)
      +'" font-size="10" fill="#6a737d" text-anchor="middle">开播 '+hm(nxt[0])+'</text>';
  }
  let grid="";
  for(let i=0;i<=2;i++){const v=Math.round(y1*i/2);
    grid+='<line x1="'+P+'" y1="'+py(v)+'" x2="'+(W-P)+'" y2="'+py(v)
      +'" stroke="#e3e6ea"/><text x="4" y="'+(py(v)+4)
      +'" font-size="10" fill="#6a737d">'+v+'</text>';}
  return '<svg viewBox="0 0 '+W+' '+H+'" style="width:100%;height:auto">'
    +grid+lines+marks
    +'<text x="'+P+'" y="'+(H-6)+'" font-size="10" fill="#6a737d">'+hm(x0)+'</text>'
    +'<text x="'+(W-P-30)+'" y="'+(H-6)+'" font-size="10" fill="#6a737d">'+hm(x1)+'</text>'
    +'</svg>';
}

function barSVG(rows, key, label) {
  const vals = rows.map(r=>r[key]||0);
  if (!vals.some(v=>v>0)) return '<span class="muted">无数据</span>';
  const W=860,H=110,P=30,max=Math.max(1,...vals);
  const bw=(W-2*P)/rows.length;
  let s='';
  rows.forEach((r,i)=>{
    const v=r[key]||0, h=(H-2*P)*v/max;
    s+='<rect x="'+(P+i*bw+bw*0.15)+'" y="'+(H-P-h)+'" width="'+(bw*0.7)
      +'" height="'+Math.max(1,h)+'" fill="#0366d6" rx="2"/>'
      +'<text x="'+(P+i*bw+bw/2)+'" y="'+(H-P+12)+'" font-size="9" '
      +'text-anchor="middle" fill="#6a737d">'+r.date.slice(5)+'</text>';
    if(v>0) s+='<text x="'+(P+i*bw+bw/2)+'" y="'+(H-P-h-4)
      +'" font-size="9" text-anchor="middle" fill="#24292f">'
      +(v>=10000?(v/10000).toFixed(1)+'w':v)+'</text>';
  });
  return '<svg viewBox="0 0 '+W+' '+H+'" style="width:100%;height:auto">'
    +'<text x="2" y="12" font-size="11" fill="#6a737d">'+label+'</text>'+s+'</svg>';
}

function portraitBlock(p) {
  if (!p) return '<div class="muted">该日无画像数据</div>';
  const chips = (arr)=> (arr&&arr.length)
    ? '<div class="chips">'+arr.map(x=>'<span class="chip">'+esc(x)+'</span>').join('')+'</div>'
    : '<span class="muted">—</span>';
  const qt = p.question_topics||[];
  qt.sort((a,b)=>(b.count||0)-(a.count||0));
  const rows = qt.slice(0,10).map(q=>
    '<tr><td>'+esc(q.topic)+'</td><td>'+fmt(q.count)+'</td><td>'
    +esc((q.examples||[]).slice(0,2).join(" / "))+'</td></tr>').join('');
  const sent = p.sentiment||{};
  const act = p.activity_layers||{};
  const cons = p.consumption_intent||{};
  return '<div class="panel"><h2>观众画像（LLM 快照）</h2>'
    +'<div class="tagline">兴趣主题</div>'+chips(p.interest_topics)
    +'<div class="tagline" style="margin-top:10px">情绪分布</div>'
    +'<span>好评 '+Math.round((sent.positive||0)*100)+'% · 中性 '
    +Math.round((sent.neutral||0)*100)+'% · 负面 '+Math.round((sent.negative||0)*100)+'%</span>'
    +'<div class="tagline" style="margin-top:10px">消费意向</div>'
    +'<span>'+(cons.level?esc(cons.level):'—')
    +((cons.keywords&&cons.keywords.length)?'：'+esc(cons.keywords.join("、")):'')+'</span>'
    +'<div class="tagline" style="margin-top:10px">活跃分层</div>'
    +'<span>高 '+(act.high==null?'—':act.high)+' / 中 '+(act.mid==null?'—':act.mid)
    +' / 低 '+(act.low==null?'—':act.low)+'</span>'
    +'<div class="tagline" style="margin-top:10px">给主播的建议</div>'
    +((p.suggestions&&p.suggestions.length)
      ? '<ul>'+p.suggestions.map(x=>'<li>'+esc(x)+'</li>').join('')+'</ul>'
      : '<span class="muted">—</span>')
    +'<div class="tagline" style="margin-top:12px">提问聚类 Top10</div>'
    +(rows ? '<table><tr><th>主题</th><th>次数</th><th>示例原句</th></tr>'+rows+'</table>'
           : '<span class="muted">该日未提取到提问主题</span>')
    +'</div>';
}

function coldBlock(c) {
  if (!c) return '<div class="panel"><h2>冷场记录</h2><span class="muted">该日无冷场触发</span></div>';
  return '<div class="panel"><h2>冷场记录</h2><p>触发 <b>'+c.count+'</b> 次</p>'
    +((c.last_lines&&c.last_lines.length)
      ? '<div class="coldline"><b>最近一次冷场台词：</b><ul>'
        +c.last_lines.map(x=>'<li>'+esc(x)+'</li>').join('')+'</ul></div>'
      : '')
    +'</div>';
}

function kpi(v,k) { return '<div class="kpi"><div class="v">'+v+'</div><div class="k">'+k+'</div></div>'; }

function dayView(a, day) {
  const dd = a.days_data[day];
  if (!dd) return '<div class="panel"><div class="empty">'+esc(day)+' 无采集数据（未开播或未采到）</div></div>';
  const dm = dd.danmu||{}, st = dd.stats||{}, c = dm.counts||{};
  const meta = dd.meta||{};
  const title = meta.title ? esc(meta.title) : '<span class="muted">—</span>';
  let html = '<div class="panel"><h2>'+esc(day)+' 场次</h2>'
    +'<div class="tagline">直播标题：'+title
    +(meta.start_time?' · 开播 '+esc(String(meta.start_time)):'')
    +(dm.duration_min?' · 采集时长 '+dm.duration_min+' 分钟':'')+'</div>'
    +'<div class="cards">'
    +kpi(fmt(c.total||0),'弹幕总条数')+kpi(fmt(c.danmaku||0),'发言')
    +kpi(fmt(c.like||0),'点赞')+kpi(fmt(c.gift||0),'礼物')
    +kpi(fmt(st.online_peak||0),'在线峰值')
    +kpi(st.total_pv!=null?fmt(st.total_pv):'—','累计观看')
    +'</div></div>';
  html += '<div class="panel"><h2>在线人数走势（每分钟 1 点）</h2>'
    + lineSVG(st.online_series) + '</div>';
  html += portraitBlock(dd.portrait);
  html += coldBlock(dd.cold);
  return html;
}

function overviewView(a) {
  const rows = a.overview;
  if (!rows || !rows.length)
    return '<div class="panel"><div class="empty">近 7 天无场次数据</div></div>';
  const trs = rows.map(r=>
    '<tr><td>'+esc(r.date)+'</td><td>'+fmt(r.total)+'</td><td>'+fmt(r.danmaku)
    +'</td><td>'+fmt(r.like)+'</td><td>'+fmt(r.gift)+'</td>'
    +'<td>'+(r.duration_min!=null?r.duration_min+' 分':'—')+'</td>'
    +'<td>'+fmt(r.online_peak)+'</td><td>'+(r.total_pv!=null?fmt(r.total_pv):'—')
    +'</td><td>'+fmt(r.cold_count)+'</td></tr>').join('');
  return '<div class="panel overview"><h2>近 7 天场次对比</h2>'
    +'<table><tr><th>日期</th><th>弹幕总数</th><th>发言</th><th>点赞</th><th>礼物</th>'
    +'<th>时长</th><th>在线峰值</th><th>累计观看</th><th>冷场次数</th></tr>'+trs+'</table>'
    +'</div>'
    +'<div class="panel"><h2>弹幕总量对比</h2>'+barSVG(rows,"total","每日弹幕总条数")+'</div>'
    +'<div class="panel"><h2>直播时长对比</h2>'
    +barSVG(rows.map(r=>({...r, dur:r.duration_min||0})),"dur","每日采集时长（分钟）")+'</div>'
    +'<div class="panel"><h2>在线峰值对比</h2>'+barSVG(rows,"online_peak","每日在线人数峰值")+'</div>';
}

function renderContent() {
  const a = DATA.anchors[curTab];
  let html = '';
  const st = a.status;
  html += '<div class="panel"><div class="tagline">当前状态：<b>'
    +(st.is_live===true?'<span style="color:#d73a49">● 直播中</span>'
      :st.is_live===false?'○ 未开播':'— 未知')
    +'</b>'+(st.nick?' · 抖音昵称 '+esc(st.nick):'')
    +(st.fans!=null?' · 粉丝 '+fmt(st.fans):'')+'</div></div>';
  if (curMode === "overview") {
    html += overviewView(a);
  } else if (!a.dates.length) {
    html += '<div class="panel"><div class="empty">近 7 天无数据</div></div>';
  } else {
    html += '<div class="daybar">'
      + a.dates.map(d=>'<div class="daybtn'+(d===curDay?' active':'')
        +'" onclick="pickDay(\\''+d+'\\')">'+d+'</div>').join('')
      + '<div class="daybtn" onclick="pickOverview()">近 7 天总览</div></div>';
    html += dayView(a, curDay);
  }
  document.getElementById("content").innerHTML = html;
}
function pickDay(d) { curDay = d; curMode = "day"; saveState(); renderContent(); }
function pickOverview() { curMode = "overview"; saveState(); renderContent(); }

(function init() {
  document.getElementById("gen").textContent =
    "数据生成于 " + DATA.generated_at + " · 下播后自动更新 · 页面每 60 秒自刷";
  const llm = DATA.llm || {};
  const days = Object.keys(llm).sort();
  if (days.length) {
    const parts = days.map(d=>d.slice(5)+": "+fmt(llm[d].total)
      +(llm[d].budget?" / "+fmt(llm[d].budget):"")+" tok");
    document.getElementById("llm").textContent = "LLM 账（7 天）：" + parts.join(" · ");
  }
  const sv = loadState();
  if (!DATA.anchors.length) {
    renderTabs();
    document.getElementById("content").innerHTML =
      '<div class="panel"><div class="empty">近 7 天无任何主播开播数据</div></div>';
    return;
  }
  if (sv && sv.t < DATA.anchors.length) {
    curTab = sv.t;
    const a = DATA.anchors[curTab];
    curDay = (sv.d && a.dates.indexOf(sv.d) >= 0) ? sv.d
      : (a.dates.length ? a.dates[a.dates.length-1] : null);
    curMode = sv.m === "overview" ? "overview" : "day";
  } else {
    curTab = 0;
    curDay = DATA.anchors[0].dates.length
      ? DATA.anchors[0].dates[DATA.anchors[0].dates.length-1] : null;
  }
  if (curTab >= DATA.anchors.length) curTab = 0;
  renderTabs(); renderContent();
})();
</script>
</body>
</html>
"""


def render_html(payload: Dict[str, Any]) -> str:
    js = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    # 防止数据里的 </script> 提前闭合脚本标签
    js = js.replace("</", "<\\/")
    return _HTML_TEMPLATE.replace("__PAYLOAD__", js)


def _sync_desktop(html: str) -> Optional[Path]:
    """桌面若已有 dashboard.html 副本则同步覆盖（2026-10-08：老大拿它当入口）。

    只在副本已存在时同步，不主动往桌面创建文件。
    """
    try:
        desk = Path(os.environ.get("USERPROFILE", "")) / "Desktop" / "dashboard.html"
        if desk.exists():
            dtmp = desk.with_suffix(".html.tmp")
            dtmp.write_text(html, encoding="utf-8")
            os.replace(dtmp, desk)
            return desk
    except OSError:
        return None
    return None


def build() -> Path:
    payload = build_payload()
    html = render_html(payload)
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".html.tmp")
    tmp.write_text(html, encoding="utf-8")
    os.replace(tmp, OUT)  # 原子覆盖：并发生成/打开中 F5 都安全
    _sync_desktop(html)
    return OUT


def main() -> int:
    try:
        out = build()
        print("[看板] 已生成 %s（%.1f KB）" % (out, out.stat().st_size / 1024))
        return 0
    except Exception as e:  # 看板失败绝不拖垮监控
        print("[看板] 生成失败：%s" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main())

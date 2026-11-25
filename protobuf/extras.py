"""补充 protobuf 消息类：平台会推、但 vendor 的 `github.com/scx567888/live-room-watcher` 快照里没有的部分。

**为什么单独一个文件**：`protobuf/douyin.py` 是第三方生成物（文件头写明 DO NOT EDIT），
直接改它下次重生成就丢；这里集中放**我们自己从真机载荷反解出来的类**，各带证据注释。

**这些类怎么来的（2026-09-30）**
1. 真机探针 `scripts/probe_methods.py --raw-limit` 把「proto 无对应类」的 method 原始
   payload（hex）落盘；
2. `scripts/pb_decode.py` 按 wire format 裸解出字段树；
3. 按解出的字段号/类型写成 betterproto 类（**字段语义是推测，数值是真机真值**）。

⚠️ 郑重提醒：**「proto 里没有类」≠「平台不推」**。实测依据：
- 抖音 web 前端 `client-entry~1.*.js` 里枚举了 **62 个** `Webcast*Message` 名，本仓 proto
  只覆盖 **12 个**（见 `refs/known_methods_web_all.json`）；
- 真机在 5 个房间 10 分钟内直接收到 `WebcastLotteryDrawResultEventMessage`（福袋开奖）
  552B/122B/8288B 等实体载荷，还有 `WebcastLiveEcomGeneralMessage`（电商）等。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

import betterproto

from protobuf.douyin import Common, User


@dataclass(eq=False, repr=False)
class LotteryDrawResultEventMessage(betterproto.Message):
    """福袋 / 抽奖**开奖结果**事件（`WebcastLotteryDrawResultEventMessage`）。

    真机反解样本（2 条，2026-09-30，房间 998047814469 / 42900502874）::

        #1 common   : {method, msg_id, room_id, ...}
        #2 uint64   : 7691244851769871123   ← 活动/抽奖 ID（推测，样本间不同）
        #3 bytes    : 25B / 8189B           ← 名单类二进制（疑为中奖名单，
                                              8189B 那条对应 candidate_num=2105）
        #4 string   : '{"candidate_num":198}' ← JSON 载荷（参与人数等）

    ⚠️ 字段名是**推测**（原名不可知），数值为真机原值；`draw_id` / `winners_blob`
    语义未经二次验证，引用时请说明来源为「真机反解」。
    """

    common: "Common" = betterproto.message_field(1)
    draw_id: int = betterproto.uint64_field(2)
    winners_blob: bytes = betterproto.bytes_field(3)
    extra_json: str = betterproto.string_field(4)


@dataclass(eq=False, repr=False)
class LotteryCondition(betterproto.Message):
    """福袋参与条件的一条 `name=value`（`WebcastLotteryEventNewMessage.#11`）。

    真机样本（房间 60964041263「淡墨荷香」）里 `#11` 是重复项，逐条形如::

        #1 str   = 'participate_type' / 'time' / 'fans_level' / 'lottery_range'
                   / 'gift_info' / 'avg_money' / 'money' / 'get_gift_cnt'
        #2 bytes = 'share' / '600' / 'fans' / …   ← 值（部分可读，部分为二进制）

    ⚠️ 字段名推测：`#1` 是条件名、`#2` 是条件值。`value` 声明为 bytes（真机里
    少数条目的值是非可打印二进制，如 `avg_money`）。
    """

    name: str = betterproto.string_field(1)
    value: bytes = betterproto.bytes_field(2)


@dataclass(eq=False, repr=False)
class LotteryEventNewMessage(betterproto.Message):
    """福袋 / 奖券**事件**（`WebcastLotteryEventNewMessage`）—— 「开始发福袋」这件事本身。

    与 `LotteryDrawResultEventMessage`（**开奖结果**）是两条独立消息。
    真机完整载荷样本（**1311B**，2026-09-30，房间 60964041263「淡墨荷香」）反解::

        #1 common      : {method, msg_id, room_id, create_time}
        #2 uint64      : 7691302424899898158   ← 活动 / lottery ID
        #3 int64       : 1                     ← 状态
        #4 int64       : 1790770980            ← 开始时间（unix 秒）
        #5 int64       : 1790771580            ← 结束时间（unix 秒；与 #4 差 600s = 10 分钟）
        #6 int64       : 1790770981
        #7 string      : 'sslocal://webcast_webview?…revenue_lottery…rule.html…'  ← 规则页
        #8 int64       : 1
        #10 bytes      : 08 09 07 06
        #11 repeated   : 参与条件（participate_type=share / lottery_range=fans / time=600 …）
        #18 string     : '钻石'                 ← ★★ 奖品名
        #24 string     : 一段权益说明 JSON（含「抖音宝石」「抖音支付/月付」）

    ⚠️ 字段名推测（原名不可知）；数值为真机原值。`conditions` 里的键值已核对
    （participate_type / lottery_range / time），但**未穷举**，引用请注明「真机反解」。
    """

    common: "Common" = betterproto.message_field(1)
    activity_id: int = betterproto.uint64_field(2)
    status: int = betterproto.int64_field(3)
    start_time: int = betterproto.int64_field(4)     # unix 秒
    end_time: int = betterproto.int64_field(5)       # unix 秒
    rule_url: str = betterproto.string_field(7)
    conditions: List["LotteryCondition"] = betterproto.message_field(11)
    prize_name: str = betterproto.string_field(18)   # ★ 奖品名（真机样本为「钻石」）


@dataclass(eq=False, repr=False)
class LuckyBoxMessage(betterproto.Message):
    """福袋推送（`WebcastLuckyBoxMessage`）—— 当前在发的福袋体。

    真机完整载荷样本（**3144B**，2026-09-30，房间 60964041263）反解::

        #1 common      : {method, msg_id, room_id}
        #2 int64       : 20                    ← 状态
        #3 uint64      : 7691301864251951922   ← box / 活动 ID
        #4 int64       : 1790770971000         ← 时间戳（毫秒）
        #5 int64       : 300                   ← 时长（秒）
        #7 string      : '钻石红包'             ← ★★ 福袋名
        #12 message    : user（送出者，字段号同 vendor User：id=1/nick_name=3）
        #15 int64      : 180
        #21 int64      : 1009
        #24 string     : '7691301864251951922' ← box_id 的字符串形式（与 #3 同值）
        #25 message    : 模板消息 'webcast_luckybox_cmtmsg_new'（含奖品图/颜色）

    ⚠️ 字段名推测；`#2=20` / `#15` / `#21` 语义未坐实，故**不声明**，betterproto 自动跳过。
    """

    common: "Common" = betterproto.message_field(1)
    status: int = betterproto.int64_field(2)
    box_id: int = betterproto.uint64_field(3)
    ts_ms: int = betterproto.int64_field(4)          # 毫秒
    duration: int = betterproto.int64_field(5)       # 秒
    box_name: str = betterproto.string_field(7)      # ★ 福袋名（真机样本为「钻石红包」）
    box_id_str: str = betterproto.string_field(24)


@dataclass(eq=False, repr=False)
class LuckyBoxRewardMessage(betterproto.Message):
    """福袋**中奖名单**推送（`WebcastLuckyBoxRewardMessage`）。

    真机完整载荷样本（**346B**，2026-09-30，房间 60964041263）反解::

        #1 common   : {method, msg_id, room_id}
        #2 uint64   : 7691299519551982374      ← 奖励 / 活动 ID
        #3 string   : '7691299519551982374'    ← 同上的字符串形式
        #4 string ×14: 中奖用户 UID 列表（如 1868799894103020 / 3472687104271324 …）
                       ← ★★★ 「中奖名单」在此（一条消息可含多名中奖者）

    ⚠️ 字段名推测。这**坐实了「中奖名单可拿」**——此前 `winners_blob`
    （`LotteryDrawResultEventMessage.#3`）语义存疑，这里则是**明文重复 UID**。
    """

    common: "Common" = betterproto.message_field(1)
    reward_id: int = betterproto.uint64_field(2)
    reward_id_str: str = betterproto.string_field(3)
    winner_ids: List[str] = betterproto.string_field(4)   # ★ 中奖用户 UID 列表


@dataclass(eq=False, repr=False)
class LinkMicSendEmojiMessage(betterproto.Message):
    """连麦 / PK 中的表情互动消息（`WebcastLinkMicSendEmojiMessage`）。

    真机样本（**4237B**，2026-09-30，房间 60964041263 —— 该房当时**正在连麦**）::

        #1 common : {method, msg_id, room_id, create_time}
        #2 User   : {id=3126660889060307, nick_name='铁腰', avatar_thumb=…}  ← 连麦对象

    ⭐ 这是**「PK / 连麦到底推不推」的决定性证据**：vendor proto 里**没有**这个类，
    但平台**确实在推**（同批还收到 5 条 `WebcastLinkerContributeMessage`）。
    """

    common: "Common" = betterproto.message_field(1)
    sender: "User" = betterproto.message_field(2)


@dataclass(eq=False, repr=False)
class TeamfightSide(betterproto.Message):
    """团战（PK）里的**一方阵营**：房间 + 比分。

    真机样本（房间 `30474723465` 点唱Club，2026-09-30）::

        #1 uint64 : 7691172785923230515     ← 阵营所在 room_id
        #2 int64  : 31106                   ← ★ 比分（整型真值）
        #3 string : '3.1w'                  ← ★ 比分显示串（≈ 3.1 万）
        #4 string : '3.1w'
    """

    room_id: int = betterproto.uint64_field(1)
    score: int = betterproto.int64_field(2)          # ★ 比分（整型）
    score_str: str = betterproto.string_field(3)     # ★ 显示串（'3.1w'）
    score_str2: str = betterproto.string_field(4)


@dataclass(eq=False, repr=False)
class TeamfightDetail(betterproto.Message):
    """团战（PK）对战详情 —— `LinkmicTeamfightScoreMessage.#2` 与 `LinkmicTeamfightMessage.#3`
    **共用同一结构**（真机比对：两者字段号/含义一致）。

    真机样本::

        #1 uint64 : 7691298873381868590    ← 对战 ID
        #2 string : 同上（字符串形式）
        #3 int64  : 2
        #4 int64  : 2
        #5 int64  : 1790770073             ← 开始（unix 秒）
        #6 int64  : 7200                   ← 时长（秒）
        #8 int64  : 60
        #9 repeated TeamfightSide          ← ★ 两方比分
    """

    battle_id: int = betterproto.uint64_field(1)
    battle_id_str: str = betterproto.string_field(2)
    status: int = betterproto.int64_field(3)
    start_time: int = betterproto.int64_field(5)     # unix 秒
    duration: int = betterproto.int64_field(6)       # 秒
    sides: List["TeamfightSide"] = betterproto.message_field(9)   # ★ 两方


@dataclass(eq=False, repr=False)
class LinkmicTeamfightScoreMessage(betterproto.Message):
    """团战（PK）**比分**推送（`WebcastLinkmicTeamfightScoreMessage`）。

    真机样本 8527B（房间 `30474723465`，该房当时**正在团战**）::

        #1 common : {method, msg_id, room_id}
        #2 TeamfightDetail : 对战详情 + #9 两方比分（真机 31106 vs 12350）

    ⭐ 这是 v3.4 曾缺的「**PK 比分真机样本**」—— vendor proto 里**没有**这个类，
    但平台在团战时**高频推**（90s 收到 10 条比分 + 1 条团战主消息）。
    """

    common: "Common" = betterproto.message_field(1)
    detail: "TeamfightDetail" = betterproto.message_field(2)


@dataclass(eq=False, repr=False)
class LinkmicTeamfightMessage(betterproto.Message):
    """团战（PK）**主消息**（`WebcastLinkmicTeamfightMessage`）。

    真机样本 8816B（房间 `30474723465`）::

        #1 common : {…}
        #2 int64  : 3                      ← 状态
        #3 TeamfightDetail : 与比分消息**同一结构**（真机 32575 vs …）
    """

    common: "Common" = betterproto.message_field(1)
    status: int = betterproto.int64_field(2)
    detail: "TeamfightDetail" = betterproto.message_field(3)


@dataclass(eq=False, repr=False)
class LinkerContributeMessage(betterproto.Message):
    """连麦 / PK **贡献分**消息（`WebcastLinkerContributeMessage`）。

    真机样本（**1366B**，2026-09-30，房间 60964041263）反解::

        #1 common   : {method, msg_id, room_id, create_time}
        #2 int64    : 93079674123      ← 分值（推测：累计 / 总分）
        #3 int64    : 220              ← 本次分值
        #4 repeated : 贡献者（每个元素为 User，真机里只带 id）
        #5 int64    : 1790770948821
        #6 int64    : 12
        #7 string   : '220'            ← 分值字符串形式
        #8 string   : '220'
        #9 message  : 贡献者明细（含 nick_name='铁腰'）

    ⚠️ 字段名推测；`#2/#3` 的「总分 / 本次分」未坐实，引用请注明「真机反解」。
    """

    common: "Common" = betterproto.message_field(1)
    total_score: int = betterproto.int64_field(2)
    score: int = betterproto.int64_field(3)
    contributors: List["User"] = betterproto.message_field(4)
    score_str: str = betterproto.string_field(7)

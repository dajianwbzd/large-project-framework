#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""task_estimate.py — 长时任务估算与容量控制（v5.3.0，项目经理职责的可执行部分）

为什么有这个脚本
----------------
用户会按时间窗提要求：整夜希望"一直跑"，上班时想"半小时看到一个效果"。可**能跑多久取决于
任务本身要吃多少时间和 token**——而这是可以量化、可以复核的，不该靠印象。本脚本把
`skills/项目经理/SKILL.md` §12/§13/§16 的容量纪律变成动作：

1. `--calibrate`  读 `进度/估算实测.csv`（**真账本 11 列**，见 `进度/台账口径.md` §一）→ 偏差系数 + 离散度
2. `--plan`       候选任务 + 时间窗 + token 预算 → 选中序列 / **命中概率** / 排除项及原因
3. `--readiness`  长时编排前的**可实施性关卡**：PASS / CONCERNS / FAIL + 指名修复者
4. `--blockers`   读 `待办/自然人.md` 未闭环项（无人值守序列的**前置地雷检查**）
5. `--record`     卡收口时回填实测值（`卡号/角色/估算O~P(小时)/P(窗口)/实际/偏差/裁尾/登记时间/证据`；
   **没有回填的估算，第二次就没人信**）
6. `--spend` / `--burn-status`  **事中**成本流水（**真账本 10 列**：逐行 `tokens_in/out` +
   `估算成本/币种/依据`，**没有累计列**——累计每次由各行重算）：每轮追加用量，累计触顶即熔断
7. `--guard` / `--contract-init`  **跑批契约与停机闸门**（v5.2.0）：用户声明一次跑批的收工
   条件（起止时刻 / 时长窗 / token 额度 / 需求卡全收口 / 全量测试过），运行循环每轮问一次
   「现在该停了吗」——**停机条件由脚本判定，不靠主控自己记着比对**（同"光有账本没有循环里
   的检查等于没有熔断"）。

硬纪律（代码守卫，不是约定）
----------------------------
- **无人值守模式**（`--unattended`）：**不可逆/对外动作一律排除**（发出、删除已发出副本、
  上线、push、破坏性迁移）；依赖未闭环人工项的卡一律排除——否则整条链停在地雷前空转到天亮。
- **预算熔断**：累计 token 超 `--budget-tokens` 即停，不静默烧完；**事中熔断由主控每轮调
  `--spend` 执行**（本脚本给机制与账本，编排与循环在主控）。有跑批契约时 `--spend` **自动
  取契约里的 `token_budget`**——额度只声明一次，不必每轮重敲（重敲是抄错额度的入口）。
- **没有停机条件的跑批不是跑批**（v5.2.0）：`--guard` 找不到契约、契约里一个条件都没有 →
  **退出码 2 而不是静默放行**——「没有闸门」不允许被读成「没到收工条件」。
- **账本列一律按表头名定位**（v5.3.0）：估算实测是 **11 列**、成本流水是 **10 列**（口径见
  `project-state/进度/台账口径.md`）；旧 6 列 / 旧 10 列账本仍可读可写。**列名不认识就整批记坏行，
  绝不按位置猜**——按位置读真账本会静默错位（`类型←角色`、`估时分钟←估算O(小时)`），比读不出来更危险。
- **「查不到」不许算「已满足」**（v5.2.0）：需求卡/测试卡目录不存在或无卡时，条件记为
  `unknown`（没人查过），**不置 fired**——拿"没人报问题"冒充"没问题"，正是要拦的那种假收工。
- **估算必有依据与置信度**：实测足够（n>=3）走实测校正（置信度高），否则走结构特征基线
  （置信度中/低），**绝不输出无依据的单点数字**；低置信度且无参考类锚 → 标记 `needs_review`。
- **给区间不给单点**：三点估算（乐观 O / 最可能 M / 悲观 P）→ BetaPERT 期望与标准差，
  序列按 σ=√Σσ² 合成，出**「这一夜跑完的概率」**；命中概率低于 `--p-min`（默认 0.80）**裁尾**，
  不放一条"大概率跑不完"的序列出去。
- **估时只用于窗口选卡与预算熔断**，**不得**作为对外交付日期承诺（AI 时代工期预测是噪音）。
- 只出**估算与候选序**；**最终编排在 agent.md 主控**（本脚本不排产、不做技术决策）。

用法
----
    python scripts/task_estimate.py --calibrate <项目根> [--json]
    python scripts/task_estimate.py --blockers  <项目根> [--json]
    python scripts/task_estimate.py --readiness <候选.json> [--window 9] [--unattended] [--json]
    python scripts/task_estimate.py --plan <候选.json> --window 9 [--budget-tokens 300000] \
        [--root <项目根>] [--unattended] [--done card-001,card-002] [--order auto|short|long] \
        [--p-min 0.80] [--write-plan] [--json]
    python scripts/task_estimate.py --record --root <项目根> --card card-011 --type 前端 \
        --est-min 90 --act-min 120 --est-tokens 40000 --act-tokens 52000
    python scripts/task_estimate.py --spend --root <项目根> --card card-011 --role 前端开发 \
        --round 2 --prompt-tokens 30000 --completion-tokens 6000 [--budget-tokens 300000] [--json]
    python scripts/task_estimate.py --burn-status --root <项目根> [--budget-tokens 300000] [--json]
    python scripts/task_estimate.py --contract-init --root <项目根>      # 起一份跑批契约样板
    python scripts/task_estimate.py --guard --root <项目根> [--now "2026-09-16 20:35"] [--json]

跑批契约（project-state/跑批契约.json，一次跑批的收工条件；`--contract-init` 生成样板）：
    {"依据": "用户 2026-09-16：晚上八点开始，跑到十点或烧到 3 亿 token",
     "start_at": "2026-09-16 20:00",   # 开跑时刻（到点前 --guard 判 NOT_STARTED）
     "stop_at":  "2026-09-16 22:00",   # 收工时刻；也可给 "HH:MM"（须配 start_at 作锚）
     "window_hours": null,             # 时长窗；与 start_at 合用时折算为 stop_at
     "token_budget": 300000000,        # token 额度（累计到顶即收工）
     "cards": {"all_done": true, "ids": []},   # 需求卡收工条件
     "test_gate": "all_done",          # 全量测试条件：all_done | none
     "unattended": true,               # 无人值守口径（缺省 true）
     "on_stop": ""}                    # 收工动作（留空即框架标准安全点动作）

候选.json 形如（字段缺省即按默认；`AC`/`契约就绪`/`参考类` 供 `--readiness` 关口判定）：
    [{"id": "card-011", "名称": "登录页接口", "类型": "后端", "优先级": "P0",
      "文件数": 3, "净增行数": 180, "ui": false, "跨角色协商": true,
      "迁移打包": false, "依赖": ["card-009"], "待人工": false, "不可逆": false,
      "AC": "登录失败返回 401 且不泄露用户是否存在", "契约就绪": true,
      "参考类": "已完成的 card-008（同接口+分页）"}]

退出码：0=正常（`--guard` 含「继续跑」）；1=`--readiness` 判定 FAIL；
2=输入错误（候选文件不可解析、契约缺失或无任何停机条件等）；
3=收工条件已满足（`--guard` 判定 STOP，或 `--spend` 累计触顶）；
4=`--guard` 判定 NOT_STARTED（未到开跑时刻）；
5=`--guard` 判定「可以继续，但有条件没查到」（卡目录缺失 / 点名的卡不存在）——**不据此收工，
  也不据此派活**，先补齐数据再判（一个退出码表达不了「继续」和「没查到」两件事，故单列）。
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------- 基线参数
# 这些默认值只是"没有实测时的起点"，会被 --calibrate 的实测中位数覆盖（见 estimate()）。
BASE_MIN = 20.0          # 固定开销：简报封装 + 派发 + 收工落盘
MIN_PER_FILE = 25.0      # 每个落盘文件
MIN_PER_LINE = 1.0 / 8.0  # 每 8 行净增 ≈ 1 分钟
PER_ROUND_TOKENS = 25000  # 每轮子代理派发的上下文规模（默认档）

# 类型/特征系数（>1 更贵）
FACTOR_UI = 1.30          # 含界面：设计走查一路 + 还原返工
FACTOR_CROSS = 1.35       # 跨角色接口协商：等契约、来回改
FACTOR_MIGRATE = 1.50     # 迁移/打包：不可中断 + 环境风险
FACTOR_DOC = 0.80         # 纯文档/盘点类
DOC_TYPES = {"文档", "盘点", "调研", "报告"}

PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}
MIN_SAMPLES = 3           # 同类实测样本下限（少于这个数只作参考，不作为估算依据）
NIGHT_HOURS = 6.0         # 超过此时长默认按无人值守口径提示
LONG_WINDOW_HOURS = 3.0   # 长时窗分档（SKILL §12.2）

# 窗口分档 → (档名, 推荐取向)
WINDOW_TIERS = [
    (0.5, "极短窗", "一次只推一件事，做完就回报"),
    (3.0, "短时窗", "一个批次（3-5 卡同类），批次收口时报一次"),
    (float("inf"), "长时窗", "长链路连续推进，只在节点与异常时留痕"),
]

# 待办/自然人.md 中视为"已闭环"的状态词
CLOSED_WORDS = ("完成", "已闭环", "闭环", "已答复", "已回答", "已办", "关闭", "done", "已解决", "作废")
PLAN_MARK_BEGIN = "<!-- task_estimate:begin -->"
PLAN_MARK_END = "<!-- task_estimate:end -->"

# ---------------------------------------------------------------- 三点估算（v4.6.0）
# 为什么要三点而不是"点估 ± 一个系数"：用户要的不是"大概多久"，而是**"这一夜跑得完吗"**。
# 点估给不出概率，序列一叠加更给不出。三点估算（PERT）能把每项的不确定性合成到序列上，
# 输出「命中时间窗的概率」，低于下限就裁尾——不放一条大概率跑不完的序列出去。
PERT_LAMBDA = 4.0        # 最可能值的权重（经典 BetaPERT λ=4，即 E=(O+4M+P)/6）
P_FINISH_MIN = 0.80      # 序列命中概率下限；低于它按序列尾部裁剪
MIN_HALF_BAND = 0.15     # 最小半带宽（比值）。**零方差是抽样假象不是确定性**——3 条完全一致的
                         # 实测会把区间压成 0，那等于宣称"一定准"，故强制留最小带宽。
# 无实测样本时的基线相对倍数（O/M, P/M）。这是**政策默认值**（非数据推导），故必须
# 在输出里标明来源为"基线倍数"；有实测后由实测比值离散度接管。
BASE_BANDS = {"低": (0.60, 1.90), "中": (0.75, 1.50)}
PROCESS_TIERS = (   # 流程开销分档（借鉴 MetaGPT T-shirt sizing 的用途：估算决定值不值得走全套 SOP）
    (60.0, "直做", "单角色直做，免全套 SOP（对齐 XS/S：估算的用途就是判断值不值得走流程）"),
    (240.0, "常规", "常规流程：PRD/设计按需，不必全上"),
    (float("inf"), "全套", "全套流程 + 建议拆卡（对齐 L/XL：不拆等于把风险压在一张卡上）"),
)

# ---------------------------------------------------------------- 事中成本流水（真账本 10 列，v5.3.0）
# 借鉴 MetaGPT `CostManager` + `Team._check_balance`：**计量在基础设施、熔断在运行循环**。
# 我们的缺口是"只有事后回填"——夜里烧到哪了，天亮才知道。这张流水是**事中**账本。
# 真列（`project-state/进度/台账口径.md` §二）：时间/卡号/角色/模型/tokens_in/tokens_out/估算成本/币种/依据/登记时间
# ——**逐行用量，没有累计列**：累计每次由各行重算，不长出第二份事实源。
BURN_FIELDS = ["时间", "卡号", "角色", "模型", "tokens_in", "tokens_out",
               "估算成本", "币种", "依据", "登记时间"]
# v4.6.0 旧列（含 `累计token`/`累计成本`）：**只读兼容**，新建文件一律用真列。
BURN_FIELDS_LEGACY = ["时间戳", "卡", "角色", "模型", "轮次", "prompt_tokens",
                      "completion_tokens", "累计token", "累计成本", "备注"]

# 可实施性关卡（借鉴 BMAD readiness-gate）
RV_PASS, RV_CONCERNS, RV_FAIL = "PASS", "CONCERNS", "FAIL"

# ---------------------------------------------------------------- 跑批契约与停机闸门（v5.2.0）
# 为什么要有它：v4.6.0 把熔断放进了循环（每轮 `--spend` 查 breaker），但**停机条件只有 token
# 一个维度**，而且额度得每轮重敲；"跑到十点""这批卡跑完""全量测试过"这些条件只能靠主控自己
# 记着比对——**靠人记着的闸门等于没有闸门**（同"光有账本没有循环里的检查等于没有熔断"）。
# 跑批契约把用户一次跑批的要求落成一个文件，`--guard` 每轮问一次「现在该停了吗」，四类条件
# （时间 / token / 需求卡 / 测试卡）全部由脚本判定；**条件写不清或查不到，一律出报错或 unknown，
# 不许静默放行**——假收工比不收工更坏（早上起来以为跑完了）。
CONTRACT_NAME = "跑批契约.json"
GUARD_CONTINUE, GUARD_STOP, GUARD_NOT_STARTED = "CONTINUE", "STOP", "NOT_STARTED"
# 退出码：0/1/2 沿用旧契约（1=关卡 FAIL，2=输入错误），3/4/5 是 v5.2.0 新增的闸门信号。
# 5 单独给「有条件没查到」：**一个数字表达不了"继续跑"和"数据没查到"两件事**——若把它混进 0，
# 编排层就只剩"去报告正文里找 unknown"这条路，那又回到了"靠主控自己记着比对"的老病。
EXIT_STOP, EXIT_NOT_STARTED, EXIT_UNKNOWN = 3, 4, 5
SAFE_POINT_ACTION = ("停在安全点：当前卡收口、落盘、记 `运行日志.md`、留痕报备，"
                     "不静默烧完、不「再跑一轮看看」")
# 契约样板（`--contract-init` 生成）。JSON 不支持注释，故字段说明集中在 `_说明` 一项。
CONTRACT_TEMPLATE = {
    "_说明": ("一次跑批的收工条件（v5.2.0）。**每条条件都是「达到即收工」**，先命中者即停机原因；"
              "不用的条件留空或 null 即「未声明」，未声明不等于已关闭。"
              "时刻写法：`YYYY-MM-DD HH:MM`，或配 start_at 用 `HH:MM`（不给锚点就报错，不猜今天还是明天）。"
              "四类条件：时间（stop_at，或 start_at + window_hours）、token（token_budget）、"
              "需求卡（cards.all_done 或 cards.ids）、全量测试（test_gate=all_done）。"
              "运行循环每轮调 `--guard` 问一次；无契约或一条条件都没有时 `--guard` 报错退出（码 2）"
              "——**「没有闸门」不许被读成「没到收工」**。"),
    "依据": "",
    "start_at": "",
    "stop_at": "",
    "window_hours": None,
    "token_budget": None,
    "cards": {"all_done": False, "ids": []},
    "test_gate": "none",
    "unattended": True,
    "on_stop": "",
}
TEST_GATES = ("none", "all_done")
# status 枚举（需求卡/测试卡同形），用于「卡全收口」判定
CARD_DONE = "done"
STATE_CN = {"pending": "未达到", "fired": "已达成", "unknown": "没查到", "off": "未声明"}


def _reconfigure():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, OSError):
            pass


# ---------------------------------------------------------------- 控制台
def tier_of(hours: float):
    for limit, name, hint in WINDOW_TIERS:
        if hours <= limit:
            return name, hint
    return WINDOW_TIERS[-1][1], WINDOW_TIERS[-1][2]


def fmt_min(m: float) -> str:
    m = float(m)
    if m < 60:
        return f"{m:.0f} 分钟"
    h = m / 60.0
    return f"{h:.1f} 小时（{m:.0f} 分钟）"


def fmt_min_c(m: float) -> str:
    """紧凑写法（表格内用，避免嵌套括号）。"""
    m = float(m)
    return f"{m:.0f}分" if m < 60 else f"{m / 60:.1f}h"


def fmt_tok(t: float) -> str:
    """token 量级渲染。

    **上到「亿」档是 v5.2.0 加的**：用户会按「跑到 3 亿 token」下单，若仍按 k 档渲染就是
    「300000.0k token」——**数字没错但没人这么读**，而契约里的额度是要拿给人核对的那一份。
    """
    t = float(t)
    if t >= 1e8:
        return f"{t / 1e8:.1f}亿 token"
    if t >= 1e6:
        return f"{t / 1e6:.1f}M token"
    if t >= 1000:
        return f"{t / 1000:.1f}k token"
    return f"{t:.0f} token"


# ---------------------------------------------------------------- 实测台账（真账本 11 列，v5.3.0）
# 列定义见 `project-state/进度/台账口径.md` §一（对应验收标准 #40）：
#   卡号/角色/估算O(小时)/估算M(小时)/估算P(小时)/P(窗口)/实际(小时)/偏差/裁尾/登记时间/证据
# v4.5.0 的 6 列（卡号/类型/估时分钟/实时分钟/估token/实token）**只读兼容**：旧账本还能继续追加，
# 但新建账本一律真 11 列——两套口径的差别不是"多了几列"，而是**有没有三点估算与窗口概率**。
CSV_FIELDS = ["卡号", "角色", "估算O(小时)", "估算M(小时)", "估算P(小时)", "P(窗口)",
              "实际(小时)", "偏差", "裁尾", "登记时间", "证据"]
CSV_FIELDS_LEGACY = ["卡号", "类型", "估时分钟", "实时分钟", "估token", "实token"]


def ledger_path(root: Path) -> Path:
    return root / "project-state" / "进度" / "估算实测.csv"


def _num(cell):
    """单元格 → float；空 / `-` / `None` / `未查过` 一律当**没有这个数**（不拿 0 冒充）。"""
    s = (cell or "").strip()
    if s in ("", "-", "None", "未查过", "n/a"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _cell(row: dict, *names) -> str:
    """按列名候选取单元格（先给真列名，再给旧别名）。"""
    for n in names:
        v = (row.get(n) or "").strip()
        if v:
            return v
    return ""


def read_csv_named(path: Path):
    """按**表头名**读 CSV → `(header, [{列名: 单元格}])`。

    为什么不用 `csv.DictReader(..., fieldnames=[...])`：那是**按位置**给列起名——真账本一换列数，
    读出来的数字会静默错位（v5.3.0 前 `read_ledger` 就是这么坏的：11 列真账本被当 6 列旧账本读，
    `估算M(小时)` 落进 `估时分钟`）。表头缺失时返回 `([], [])`，由调用方按坏行计数。
    """
    p = Path(path)
    if not p.exists():
        return [], []
    try:
        text = p.read_text(encoding="utf-8-sig")
    except OSError:
        return [], []
    raw = [[c.strip() for c in r] for r in csv.reader(io.StringIO(text))]
    raw = [r for r in raw if any(r) and not r[0].startswith("#")]
    if not raw:
        return [], []
    header = raw[0]
    rows = []
    for r in raw[1:]:
        if r[0] == header[0]:
            continue                      # 重复表头（多次追加/拼接）跳过
        rows.append(dict(zip(header, r)))
    return header, rows


def ledger_schema(header) -> str:
    if not header:
        return "无表头"
    if "估算M(小时)" in header:
        return "11列真口径"
    if "估时分钟" in header:
        return "6列旧口径"
    return "列名不认识"


def _norm_ledger_row(r: dict, real: bool) -> dict:
    """归一化一行：既有真列名，也保留 `calibrate/estimate` 依赖的旧内部键（下游契约不变）。"""
    card = _cell(r, "卡号")
    if real:
        role = _cell(r, "角色")
        m = _num(_cell(r, "估算M(小时)"))
        act = _num(_cell(r, "实际(小时)"))
        return {
            "卡号": card, "角色": role,
            "估算O(小时)": _num(_cell(r, "估算O(小时)")),
            "估算M(小时)": m, "估算P(小时)": _num(_cell(r, "估算P(小时)")),
            "P(窗口)": _num(_cell(r, "P(窗口)")),
            "实际(小时)": act, "偏差": _num(_cell(r, "偏差")),
            "裁尾": _cell(r, "裁尾"), "登记时间": _cell(r, "登记时间"),
            "证据": _cell(r, "证据"),
            "类型": role or "（未填）",
            "估时分钟": None if m is None else m * 60.0,
            "实时分钟": None if act is None else act * 60.0,
            "估token": None, "实token": None,
        }
    role = _cell(r, "类型", "角色")
    em = _num(_cell(r, "估时分钟"))
    am = _num(_cell(r, "实时分钟"))
    return {
        "卡号": card, "角色": role,
        "估算M(小时)": None if em is None else em / 60.0,
        "实际(小时)": None if am is None else am / 60.0,
        "偏差": None if (em is None or am is None) else (am - em) / 60.0,
        "类型": role or "（未填）",
        "估时分钟": em, "实时分钟": am,
        "估token": _num(_cell(r, "估token")), "实token": _num(_cell(r, "实token")),
    }


def read_ledger(root: Path):
    """读实测台账（真 11 列 / 旧 6 列都认，**全按表头名定位**）。缺文件/坏行不抛异常。"""
    header, raw = read_csv_named(ledger_path(root))
    kind = ledger_schema(header)
    if kind == "列名不认识" or kind == "无表头":
        return [], len(raw)
    real = kind == "11列真口径"
    rows, bad = [], 0
    for r in raw:
        row = _norm_ledger_row(r, real)
        if not row["卡号"]:
            bad += 1
            continue
        rows.append(row)
    return rows, bad


# 账本列名 → CLI/内部键的别名表（真列名与旧列名并存：写哪套由**文件表头**决定）
_ROW_ALIASES = {
    "卡号": ("卡号",), "角色": ("角色", "类型"), "类型": ("类型", "角色"),
    "估算O(小时)": ("估算O(小时)",), "估算M(小时)": ("估算M(小时)", "估算M"),
    "估算P(小时)": ("估算P(小时)", "估算P"), "P(窗口)": ("P(窗口)", "窗口P"),
    "实际(小时)": ("实际(小时)",), "偏差": ("偏差",), "裁尾": ("裁尾",),
    "登记时间": ("登记时间", "记录时间"), "证据": ("证据",),
    "估时分钟": ("估时分钟",), "实时分钟": ("实时分钟",),
    "估token": ("估token",), "实token": ("实token",),
}


def fmt_cell(v) -> str:
    """写进账本的单元格：缺项写 `-`（**不写 0、不写空**——0 会被读成"零耗时"，空会被读成漏读）。"""
    if v is None:
        return "-"
    if isinstance(v, float):
        s = f"{v:.4f}".rstrip("0").rstrip(".")
        return s if s else "-"
    s = str(v).strip()
    return s or "-"


class LedgerSchemaError(Exception):
    """目标账本**已有表头但列名不认识** ⇒ 拒绝写入（v5.3.1）。

    为什么要拒：v5.3.0 起读路径对不认识的列名**整批记坏行**，而写路径只是「按表头逐列取别名、
    取不到写 `-`」——于是同一份烂账本「读不出来」却「写得进去还报 ok:true」。调用方看到 rc=0
    会以为回填成功（D8/V-01 独立复核发现）。写入是**不可逆**动作：宁可 rc=2 让人先确认表头。
    """


def _refuse_write(kind: str, path: Path, header, real, legacy) -> str:
    return (f"{kind}账本表头不认识，拒绝写入（不按位置猜列、不静默写垃圾行）：{path}\n"
            f"  实际表头：{','.join(header)}\n"
            f"  期望其一：{','.join(real)}（真口径）\n"
            f"            或 {','.join(legacy)}（旧口径，只读兼容）\n"
            "  ⇒ 先人工确认表头（迁移或重命名列）；文件为空时本工具会按真口径建表头。")


def record(root: Path, row: dict) -> Path:
    """把一行实测**追加**进台账：列序以**文件自身表头**为准，只追加、绝不重写已有行。

    文件不存在才用真 11 列 `CSV_FIELDS` 建表头；旧 6 列台账继续按旧列写（不会被改写成 11 列）。
    已有表头却**列名不认识**时抛 `LedgerSchemaError`（rc=2 拒写，见该异常的说明）。
    """
    p = ledger_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    header, _ = read_csv_named(p)
    if header and ledger_schema(header) == "列名不认识":
        raise LedgerSchemaError(_refuse_write("估算实测", p, header, CSV_FIELDS, CSV_FIELDS_LEGACY))
    if not header:
        header = list(CSV_FIELDS)
        with p.open("a", encoding="utf-8", newline="") as fh:
            csv.writer(fh).writerow(header)
    vals = []
    for col in header:
        v = None
        for alias in _ROW_ALIASES.get(col, (col,)):
            if row.get(alias) not in (None, ""):
                v = row[alias]
                break
        vals.append(fmt_cell(v))
    with p.open("a", encoding="utf-8", newline="") as fh:
        csv.writer(fh).writerow(vals)
    return p


def calibrate(root: Path):
    """按类型聚合实测：中位数偏差系数（实际/估算）。<MIN_SAMPLES 只作参考。

    真 11 列账本**没有 token 实测** ⇒ `median_act_tokens` / `token_factor` 为 None（**不拿 0 顶替**），
    估算里只走时间校正；旧 6 列账本两样都有。返回里带 `ledger_schema`/`ledger_columns` 供取证。
    """
    header, _raw = read_csv_named(ledger_path(root))
    rows, bad = read_ledger(root)
    by_type: dict = {}
    for r in rows:
        by_type.setdefault(r["类型"], []).append(r)

    out = {"samples_total": len(rows), "bad_rows": bad,
           "ledger_schema": ledger_schema(header), "ledger_columns": header, "types": {}}
    for t, rs in sorted(by_type.items()):
        time_ratios, tok_ratios = [], []
        for r in rs:
            em, am = r["估时分钟"], r["实时分钟"]
            if em and am and em > 0 and am > 0:
                time_ratios.append(am / em)
            et, at = r["估token"], r["实token"]
            if et and at and et > 0 and at > 0:
                tok_ratios.append(at / et)
        act_mins = [r["实时分钟"] for r in rs if r["实时分钟"] is not None]
        act_toks = [r["实token"] for r in rs if r["实token"] is not None]
        n = len(rs)
        out["types"][t] = {
            "n": n,
            "usable": n >= MIN_SAMPLES,
            "median_act_min": statistics.median(act_mins) if act_mins else None,
            "median_act_tokens": statistics.median(act_toks) if act_toks else None,
            "time_factor": round(statistics.median(time_ratios), 3) if time_ratios else None,
            "token_factor": round(statistics.median(tok_ratios), 3) if tok_ratios else None,
            # 离散度（v4.6.0）：三点估算的乐观/悲观端点**由实测比值分位点给出**，
            # 而不是拍一个固定系数——这样"估得准不准"是项目自己长出来的。
            "ratio_lo": round(quantile(time_ratios, 0.10), 3) if time_ratios else None,
            "ratio_hi": round(quantile(time_ratios, 0.90), 3) if time_ratios else None,
            "cards": [r["卡号"] for r in rs],
        }
    out["usable_types"] = sorted(t for t, v in out["types"].items() if v["usable"])
    return out


# ---------------------------------------------------------------- 三点估算核心
def quantile(values, q: float):
    """线性插值分位数（不依赖 statistics.quantiles 对 n<2 的限制）。"""
    vs = sorted(float(v) for v in values)
    if not vs:
        return 0.0
    if len(vs) == 1:
        return vs[0]
    pos = q * (len(vs) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(vs) - 1)
    frac = pos - lo
    return vs[lo] * (1.0 - frac) + vs[hi] * frac


def band_of(info: dict, confidence: str):
    """返回 (o_mult, p_mult, 来源说明)：相对点估 M 的乐观/悲观倍数。

    有可用实测（n>=3）时用**实测比值的 p10/p90 相对中位数**；离散度不足以支撑区间时
    （样本完全一致之类）强制展开到 MIN_HALF_BAND——见常量处注释：零方差是抽样假象。
    """
    n = info.get("n", 0)
    lo, hi, med = info.get("ratio_lo"), info.get("ratio_hi"), info.get("time_factor")
    if n >= MIN_SAMPLES and lo and hi and med and med > 0:
        o, p = lo / med, hi / med
        src = f"实测比值 p10/p90（n={n}）"
        if (p - 1.0) < MIN_HALF_BAND or (1.0 - o) < MIN_HALF_BAND:
            o, p = min(o, 1.0 - MIN_HALF_BAND), max(p, 1.0 + MIN_HALF_BAND)
            src += f"（样本离散不足，端点已按最小带宽 ±{MIN_HALF_BAND:.0%} 展开）"
        return o, p, src
    o, p = BASE_BANDS.get(confidence, BASE_BANDS["低"])
    return o, p, f"基线倍数（{confidence}置信度，非实测推导）"


def pert(o: float, m: float, p: float):
    """BetaPERT：返回 (期望, 标准差, α, β)。用**精确方差**而非教科书 (P−O)/6 近似。

    为什么不用 (P−O)/6：λ=4 时它与 Beta 的精确标准差差约 13%（对称情形下 0.189 vs 0.167），
    **偏小**意味着序列命中概率被系统性算高——恰恰是夜间编排最不能出错的方向。
    （本项的验证方式：scripts/tests/test_task_estimate.py 的蒙特卡洛对拍。）
    """
    span = float(p) - float(o)
    if span <= 0:
        return float(m), 0.0, 1.0, 1.0
    a = 1.0 + PERT_LAMBDA * (m - o) / span
    b = 1.0 + PERT_LAMBDA * (p - m) / span
    mean = o + span * a / (a + b)
    var = (span ** 2) * (a * b) / (((a + b) ** 2) * (a + b + 1.0))
    return mean, math.sqrt(var), a, b


def norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def seq_prob(window_min: float, mean_total: float, sigma_total: float) -> float:
    """序列在窗口内跑完的概率 P(ΣT ≤ W)。σ=0 时退化为确定性判定。"""
    if sigma_total <= 0:
        return 1.0 if mean_total <= window_min else 0.0
    return norm_cdf((window_min - mean_total) / sigma_total)


def process_tier(minutes: float):
    for limit, name, hint in PROCESS_TIERS:
        if minutes <= limit:
            return name, hint
    return PROCESS_TIERS[-1][1], PROCESS_TIERS[-1][2]


# ---------------------------------------------------------------- 估算核心
def base_estimate(task: dict):
    """结构特征基线估算（无实测或实测不足时的起点）。返回 (分钟, token, 依据列表)。"""
    files = max(0, int(task.get("文件数") or 0))
    lines = max(0, int(task.get("净增行数") or 0))
    basis = [f"固定开销 {BASE_MIN:.0f} 分钟（简报+派发+收工落盘）"]
    if files:
        basis.append(f"{files} 个落盘文件 × {MIN_PER_FILE:.0f} 分钟")
    if lines:
        basis.append(f"净增 {lines} 行 ≈ {lines * MIN_PER_LINE:.0f} 分钟")

    minutes = BASE_MIN + files * MIN_PER_FILE + lines * MIN_PER_LINE

    factor = 1.0
    if task.get("迁移打包"):
        factor *= FACTOR_MIGRATE
        basis.append(f"迁移/打包 ×{FACTOR_MIGRATE}")
    if task.get("ui"):
        factor *= FACTOR_UI
        basis.append(f"含界面（多一路设计走查+返工） ×{FACTOR_UI}")
    if task.get("跨角色协商"):
        factor *= FACTOR_CROSS
        basis.append(f"跨角色接口协商 ×{FACTOR_CROSS}")
    if task.get("类型") in DOC_TYPES:
        factor *= FACTOR_DOC
        basis.append(f"文档/盘点类 ×{FACTOR_DOC}")
    minutes *= factor

    # token：按派发轮次算，不按行数算（成本主驱动 = 轮次 × 每轮上下文规模）
    rounds = 2 + math.ceil(files / 3)          # 开发 + 验收
    if task.get("ui"):
        rounds += 1
    if task.get("跨角色协商"):
        rounds += 2
    if task.get("迁移打包"):
        rounds += 1
    tokens = rounds * PER_ROUND_TOKENS
    basis.append(f"{rounds} 轮派发 × {fmt_tok(PER_ROUND_TOKENS)}/轮（成本主驱动是轮次，不是行数）")
    return minutes, float(tokens), basis


def estimate(task: dict, cal: dict):
    """返回 dict：三点估算（O/M/P）→ BetaPERT 期望/标准差 + 依据 + 置信度 + 流程档。

    实测足够（n>=3）时用实测偏差系数校正 M，并用实测比值分位点定 O/P；
    否则用结构特征基线 + 基线倍数（置信度中/低），低置信度且无参考类锚 → `needs_review`。
    """
    m_raw, tokens_raw, basis = base_estimate(task)
    t = task.get("类型")
    info = (cal.get("types") or {}).get(t) or {}
    n = info.get("n", 0)

    m_pt, confidence = m_raw, "低"
    if n >= MIN_SAMPLES:
        tf = info.get("time_factor")
        kf = info.get("token_factor")
        if tf:
            m_pt *= tf
            basis.insert(0, f"实测校正：同类 {n} 张卡估时偏差中位数 ×{tf}（{', '.join(info['cards'])}）")
        if kf:
            tokens_raw *= kf
            basis.insert(0, f"实测校正：同类 token 偏差中位数 ×{kf}")
        confidence = "高"
    elif n > 0:
        confidence = "中"
        basis.insert(0, f"同类实测仅 {n} 张（<{MIN_SAMPLES} 张不作为依据，仅参考 {', '.join(info['cards'])}）")

    o_mult, p_mult, band_src = band_of(info, confidence)
    o, p = m_pt * o_mult, m_pt * p_mult
    mean, sd, alpha, beta = pert(o, m_pt, p)
    tok_scale = (mean / m_pt) if m_pt > 0 else 1.0
    tier, tier_hint = process_tier(mean)
    ref_class = (task.get("参考类") or "").strip()
    basis.append(f"三点：O/M/P = {o:.0f}/{m_pt:.0f}/{p:.0f} 分钟（{band_src}）"
                 f" → 期望 {mean:.0f}、σ {sd:.0f}（BetaPERT 精确方差）")
    if ref_class:
        basis.append(f"参考类锚：{ref_class}")
    needs_review = (confidence == "低" and not ref_class)

    return {
        "id": task.get("id"),
        "名称": task.get("名称", ""),
        "类型": t,
        "优先级": task.get("优先级", "P2"),
        "est_min": round(mean, 1),                      # 序列合成用期望（不是最可能值）
        "est_min_most_likely": round(m_pt, 1),
        "est_min_low": round(o, 1),
        "est_min_high": round(p, 1),
        "sigma_min": round(sd, 2),
        "pert": {"o": round(o, 1), "m": round(m_pt, 1), "p": round(p, 1),
                 "lambda": PERT_LAMBDA, "alpha": round(alpha, 4), "beta": round(beta, 4)},
        "est_tokens": round(tokens_raw * tok_scale),
        "est_tokens_low": round(tokens_raw * o_mult),
        "est_tokens_high": round(tokens_raw * p_mult),
        "confidence": confidence,
        "band_source": band_src,
        "ref_class": ref_class,
        "needs_review": needs_review,
        "process_tier": tier,
        "process_hint": tier_hint,
        "basis": basis,
        "samples": n,
    }


# ---------------------------------------------------------------- 待办解析
def read_blockers(root: Path):
    """读 待办/自然人.md 的未闭环项。表格行为主，缺文件不报错。"""
    p = root / "project-state" / "待办" / "自然人.md"
    items = []
    if not p.exists():
        return items
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return items
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 3:
            continue
        if set("".join(cells)) <= set("-: "):        # 表格分隔行
            continue
        if cells[0] in ("事项",) or "事项" == cells[0]:
            continue
        text_join = " ".join(cells)
        if any(w and w in text_join for w in CLOSED_WORDS):
            continue
        if "状态" in cells[2] or cells[0].startswith("---"):
            continue
        items.append({"事项": cells[0], "为何要自然人": cells[1] if len(cells) > 1 else "",
                      "状态": cells[2] if len(cells) > 2 else "", "原文": text_join})
    return items


# ---------------------------------------------------------------- 编排输入
def build_plan(candidates, window_hours: float, budget_tokens, root: Path,
               unattended: bool, done: set, order: str = "auto",
               p_min: float = P_FINISH_MIN):
    cal = calibrate(root)
    blockers = read_blockers(root)
    blocked_text = " ".join(b["原文"] for b in blockers)

    for c in candidates:
        c["_est"] = estimate(c, cal)

    included, excluded = [], []
    eligible = []
    for c in sorted(candidates, key=lambda x: -x["_est"]["est_min"]):
        cid = c.get("id")
        if cid in done:
            excluded.append({**c["_est"], "reason": "已完成"})
            continue
        if c.get("待人工"):
            excluded.append({**c["_est"], "reason": "需人工拍板（人工依赖，无人值守会空转）"})
            continue
        if unattended and c.get("不可逆"):
            excluded.append({**c["_est"], "reason": "含不可逆/对外动作（无人值守不授权）"})
            continue
        eligible.append(c)

    # 依赖闭合：按 已完成 ∪ 已选中 迭代放开，直到一轮无新增（避免"按估时排序"误判依赖）
    satisfied = set(done)
    progressed = True
    while progressed:
        progressed = False
        for c in eligible:
            if c in included:
                continue
            deps = [d for d in (c.get("依赖") or []) if d]
            unmet = [d for d in deps if d not in satisfied]
            if unmet:
                continue
            included.append(c)
            satisfied.add(c.get("id"))
            progressed = True

    for c in eligible:
        if c in included:
            continue
        deps = [d for d in (c.get("依赖") or []) if d]
        unmet = [d for d in deps if d not in satisfied]
        hit_blk = [d for d in unmet if d in blocked_text]
        why = "依赖未闭环人工项（待办/自然人.md）" if hit_blk else "依赖未满足"
        excluded.append({**c["_est"], "reason": f"{why}：{', '.join(unmet) or '（依赖链不闭合）'}"})

    # 取向：极短/短窗先做小闭环（短的优先）；长时窗做长链路（长的优先）
    if order == "auto":
        order = "long" if window_hours >= LONG_WINDOW_HOURS else "short"
    window_min = window_hours * 60.0
    selected, spill, by_backfill = [], [], []
    used_min, used_tok = 0.0, 0.0
    budget_stop = False

    def fits(e):
        if budget_tokens is not None and (used_tok + e["est_tokens"]) > budget_tokens:
            return "budget"
        if (used_min + e["est_min"]) > window_min:
            return "time"
        return None

    def take(c, via_backfill=False):
        nonlocal used_min, used_tok
        e = c["_est"]
        selected.append(c)
        if via_backfill:
            by_backfill.append(e["id"])
        used_min += e["est_min"]
        used_tok += e["est_tokens"]

    primary_key = (lambda x: (PRIORITY_ORDER.get(x["_est"]["优先级"], 9),
                              x["_est"]["est_min"] if order == "short" else -x["_est"]["est_min"]))
    left = sorted(included, key=primary_key)
    # 第一轮：按窗口取向的主序
    for c in list(left):
        if fits(c["_est"]) is None:
            take(c)
            left.remove(c)
    # 第二轮（回填）：主序放不下的用"短优先"填满剩余窗口——空转是长时窗最大的浪费
    for c in sorted(left, key=lambda x: (PRIORITY_ORDER.get(x["_est"]["优先级"], 9), x["_est"]["est_min"])):
        if fits(c["_est"]) is None:
            take(c, via_backfill=True)

    for c in left:
        why = fits(c["_est"])
        if why == "budget":
            budget_stop = True
            spill.append({**c["_est"], "reason": f"token 预算熔断（已用 {fmt_tok(used_tok)} / 上限 {fmt_tok(budget_tokens)}）"})
        else:
            spill.append({**c["_est"], "reason": f"超出时间窗（已用 {fmt_min(used_min)} / 窗口 {fmt_min(window_min)}）"})

    # 序列必须满足依赖：选中的卡按拓扑序排，同级内再按窗口取向排序
    # （否则会出现"先做依赖方、后做被依赖方"的不可执行序列——估算再好也排不出去）
    def emit_key(x):
        return (PRIORITY_ORDER.get(x["_est"]["优先级"], 9),
                x["_est"]["est_min"] if order == "short" else -x["_est"]["est_min"])

    emitted, pool, ordered_sel = set(done), list(selected), []
    while pool:
        ready = [c for c in pool if all((d in emitted) for d in (c.get("依赖") or []) if d)]
        if not ready:                     # 依赖成环：保留原序交主控裁决，不静默丢卡
            ordered_sel.extend(pool)
            break
        picked = sorted(ready, key=emit_key)[0]
        ordered_sel.append(picked)
        emitted.add(picked.get("id"))
        pool.remove(picked)
    selected = ordered_sel

    # ---- 序列命中概率（σ 按平方和合成）→ 低于下限裁尾（v4.6.0）----
    def seq_stats(sel):
        tot = sum(c["_est"]["est_min"] for c in sel)
        var = sum(c["_est"]["sigma_min"] ** 2 for c in sel)
        return tot, math.sqrt(var)

    tot_min, tot_sigma = seq_stats(selected)
    p_finish = seq_prob(window_min, tot_min, tot_sigma)
    p_finish_before = p_finish
    trimmed = []
    while selected and p_finish < p_min and len(selected) > 1:
        drop = selected.pop()
        trimmed.append({**drop["_est"],
                        "reason": f"命中概率不足（P={p_finish:.0%} < {p_min:.0%}）——**裁尾**："
                                  f"留着它这晚就变成「大概率跑不完」，宁可少排也不虚报"})
        tot_min, tot_sigma = seq_stats(selected)
        p_finish = seq_prob(window_min, tot_min, tot_sigma)
    used_min = sum(c["_est"]["est_min"] for c in selected)
    used_tok = sum(c["_est"]["est_tokens"] for c in selected)

    tier, hint = tier_of(window_hours)
    utilization = (used_min / window_min * 100.0) if window_min else 0.0
    notes = []
    if p_finish < p_min:
        notes.append(f"**唯一剩下的卡命中概率仍不足**（P={p_finish:.0%} < {p_min:.0%}）——"
                     f"要么延长窗口、要么先把这张卡拆小，别把赌注压在一个大估计上。")
    if p_finish_before < p_min and trimmed:
        notes.append(f"原序列命中概率仅 {p_finish_before:.0%}，**已裁掉尾部 {len(trimmed)} 张**使 P 升到 "
                     f"{p_finish:.0%}（裁掉的项列在下方，不静默丢）。")
    review = [c["_est"]["id"] for c in selected if c["_est"].get("needs_review")]
    if review:
        notes.append(f"{len(review)} 张卡是**低置信度且无参考类锚**（{', '.join(review)}）——"
                     f"编排前须人工复核，或给它们补一个「参考类」（同类已做过的卡号）。")
    if utilization < 50.0 and (spill or excluded):
        notes.append(f"窗口利用率仅 {utilization:.0f}%——剩余候选放不进去，原因见下方排除项；"
                     f"可考虑放宽 token 预算、把大卡拆小，或改用较短时间窗只报小闭环。")
    if not selected:
        notes.append("**本窗口排不出任何一张卡**：候选要么需人工、要么含不可逆动作、要么单卡就超窗/超预算。"
                     "长时窗尤其要避免这种情况——用户睡觉前必须看到「这一夜要干什么」。")
    if unattended and blockers:
        notes.append(f"待办/自然人.md 有 {len(blockers)} 项未闭环——依赖它们的卡已排除，"
                     f"但**其余链条仍可能在中途需要用户输入**，编排时预留止损点。")

    # ---- 无人在场时替你做的假设（借鉴 BMAD headless 的 Assumptions 留痕）----
    # 无人值守不是无人授权：夜里没人能拍板，就必须把"替你拍了什么板"逐条留下来。
    assumptions = [
        f"任务时长相互独立，σ 按 √Σσ² 合成——**真实项目里关联返工会让实际方差更大**，"
        f"故 P={p_finish:.0%} 是**乐观口径**，不是保证。",
        "估时只用于**窗口选卡与预算熔断**；不构成对外交付日期承诺（BMAD：AI 时代的工期预测是噪音）。",
    ]
    bands_used = sorted({c["_est"].get("band_source", "") for c in selected if c["_est"].get("band_source")})
    if bands_used:
        assumptions.append("区间端点来源：" + "；".join(b for b in bands_used if b))
    if review:
        assumptions.append(f"以下卡为低置信度且无参考类锚，**我（项目经理）没有替你确认它们能不能做**："
                           f"{', '.join(review)}——编排前需人工复核或补参考类。")
    if unattended:
        assumptions.append(f"无人值守：已替你把**人工依赖卡 {len([e for e in excluded if '人工' in e['reason']])} 张**、"
                           f"**不可逆/对外动作卡 {len([e for e in excluded if '不可逆' in e['reason']])} 张**排除在序列外"
                           f"（明细见排除表）；夜里若出现需拍板的事，按纪律停在安全点并留痕，不硬扛。")
    if not selected:
        assumptions.append("**本窗口排不出任何卡**——这是结论不是失败：宁可报「这一夜排不出」，也不排一条跑不完的。")

    # 流程档（借鉴 MetaGPT TeamLeader 的 T-shirt sizing）：**最重的那张卡决定**这一夜要不要走全套 SOP。
    # 取最重卡而非合计：合计再大也可能是一堆小卡，而一张大卡单独就要求契约/评审/测试全套。
    heaviest = max(selected, key=lambda c: c["_est"]["est_min"], default=None)

    return {
        "window_hours": window_hours,
        "window_tier": tier,
        "tier_hint": hint,
        "process_tier": heaviest["_est"]["process_tier"] if heaviest else None,
        "process_hint": heaviest["_est"]["process_hint"] if heaviest else None,
        "process_basis": (f"由最重卡 {heaviest['_est']['id']}（{fmt_min_c(heaviest['_est']['est_min'])}）决定"
                          if heaviest else "无入选卡"),
        "unattended": bool(unattended),
        "order": order,
        "budget_tokens": budget_tokens,
        "selected": [c["_est"] for c in selected],
        "backfilled": by_backfill,
        "excluded": excluded,
        "spill": spill + trimmed,
        "trimmed": trimmed,
        "notes": notes,
        "assumptions": assumptions,
        "total_est_min": round(used_min, 1),
        "total_est_tokens": round(used_tok),
        "sigma_total": round(tot_sigma, 2),
        "p_finish": round(p_finish, 4),
        "p_finish_before_trim": round(p_finish_before, 4),
        "p_min": p_min,
        "utilization_pct": round(utilization, 1),
        "budget_stop": budget_stop,
        "needs_review": review,
        "blockers": blockers,
        "calibration": {"samples_total": cal["samples_total"], "usable_types": cal["usable_types"]},
    }


# ---------------------------------------------------------------- 可实施性关卡
READINESS_QUESTION = "一个开发在没人可问的情况下，能不能把这些卡做完？"


def readiness_check(candidates, root: Path, unattended: bool, done: set):
    """长时编排前的关卡（借鉴 BMAD readiness-gate）。

    为什么要有关卡：夜间最惨的失败不是跑错，是**跑到一半要问人**。所以在"把这一夜排出去"
    之前先回答一个问题：*一个开发在没人可问的情况下，能不能把这些卡做完？*
    裁决三档（PASS / CONCERNS / FAIL），**每条发现指名修复者**——不指名的发现等于没发现。
    """
    cal = calibrate(root)
    blockers = read_blockers(root)
    ids = {c.get("id") for c in candidates}
    findings, undeclared, review = [], [], []

    for c in candidates:
        cid = c.get("id")
        if cid in done:
            continue
        if "AC" in c:
            if not str(c.get("AC") or "").strip():
                findings.append((RV_FAIL, cid, "AC 显式留空——没人追问时无法判定「做完了」",
                                 "产品经理（补验收标准）"))
        else:
            undeclared.append(f"{cid} · AC")
        if c.get("契约就绪") is False:
            findings.append((RV_FAIL, cid, "契约未就绪（跨角色接口没定）",
                             "架构师 / 接口提供方（先定契约）"))
        elif "契约就绪" not in c:
            undeclared.append(f"{cid} · 契约就绪")
        deps = [d for d in (c.get("依赖") or []) if d]
        unmet = [d for d in deps if d not in ids and d not in done]
        if unmet:
            findings.append((RV_FAIL, cid, f"依赖既不在候选也不在 --done：{', '.join(unmet)}",
                             "主控（补依赖卡或调序）"))
        if c.get("待人工"):
            findings.append((RV_CONCERNS, cid, "需人工拍板——链条会空转在它前面",
                             "用户（先答复）/ 主控（排除出夜间序列）"))
        if unattended and c.get("不可逆"):
            findings.append((RV_CONCERNS, cid, "含不可逆/对外动作——无人值守不授权",
                             "主控（留到用户在侧）"))
        if estimate(c, cal)["needs_review"]:
            review.append(cid)

    # 低置信度+无参考类锚是**一个全局病征**（本类没有实测、也没有可类比的卡），
    # 逐卡刷一行会把真正要看的 FAIL 淹掉——**没人看的报告等于没有报告**。
    if review:
        findings.append((RV_CONCERNS, None,
                         f"{len(review)} 张卡低置信度且无参考类锚（估算事后无法复核）："
                         f"{', '.join(str(x) for x in review)}",
                         "项目经理（补「参考类」锚，或先排一张探索卡）"))

    if cal["samples_total"] == 0:
        findings.append((RV_CONCERNS, None, "估算实测台账为空——本轮估算全走基线倍数（非实测推导）",
                         "项目经理（卡收口时 --record 回填，下次才有据）"))
    if blockers:
        findings.append((RV_CONCERNS, None, f"待办/自然人.md 有 {len(blockers)} 项未闭环",
                         "用户（先清人工依赖）/ 主控（不排依赖它们的卡）"))

    sevs = [f[0] for f in findings]
    verdict = RV_FAIL if RV_FAIL in sevs else (RV_CONCERNS if sevs else RV_PASS)
    return {
        "verdict": verdict,
        "question": READINESS_QUESTION,
        "findings": [{"severity": s, "card": cid, "issue": issue, "fixer": fixer}
                     for s, cid, issue, fixer in findings],
        "undeclared": undeclared,
        "needs_review": review,
        "samples_total": cal["samples_total"],
        "blockers": blockers,
    }


def render_readiness(rv: dict):
    L = ["# 可实施性关卡（长时编排前置）", "",
         f"> 判据问题：**{rv['question']}**", "",
         f"## 裁决：**{rv['verdict']}**", ""]
    if rv["verdict"] == RV_PASS:
        L.append("声明齐备、依赖闭合、无人工卡——可以进长时编排。")
    elif rv["verdict"] == RV_CONCERNS:
        L.append("可以编排，但下面每条都要有人认领（或明确接受风险后再进）。")
    else:
        L.append("**不得进长时编排**——按下方修复者补齐后重跑本关卡。")
    if rv["findings"]:
        L += ["", "| 档 | 卡 | 问题 | 修复者 |", "|---|---|---|---|"]
        for f in rv["findings"]:
            L.append(f"| {f['severity']} | {f['card'] or '（全局）'} | {f['issue']} | {f['fixer']} |")
    if rv["undeclared"]:
        L += ["", "## 未声明项（**未声明 ≠ 已就绪**）", ""]
        L.append("下面这些字段候选清单里没写。没写不等于没问题——只是**没人查过**，"
                 "所以本关卡无法就它们放行：")
        L.append("")
        for u in rv["undeclared"]:
            L.append(f"- {u}")
    if rv["blockers"]:
        L += ["", f"## 未闭环人工依赖 {len(rv['blockers'])} 项（夜间地雷）", ""]
        for b in rv["blockers"]:
            L.append(f"- {b['事项']}（{b['状态']}）")
    L += ["", "---", "",
          "- 三档：**PASS** 可进；**CONCERNS** 逐条认领后可进；**FAIL** 补齐再进。",
          "- 每条发现都有**指名修复者**——不指名的发现等于没发现。",
          "- 「未声明」与「查过且干净」是两回事：报告里不许混为一谈。"]
    return "\n".join(L)


# ---------------------------------------------------------------- 事中成本流水
def burn_path(root: Path) -> Path:
    return root / "project-state" / "进度" / "成本流水.csv"


def burn_schema(header) -> str:
    if not header:
        return "无表头"
    if "tokens_in" in header or "估算成本" in header:
        return "10列真口径"
    if "prompt_tokens" in header or "累计成本" in header:
        return "10列旧口径"
    return "列名不认识"


def _cost_currency(root: Path) -> str:
    """币种取自 `project-state/config.json` 的 `cost_currency`；没配就写 `未查过`（**不猜币种**）。"""
    cfg = root / "project-state" / "config.json"
    if not cfg.exists():
        return "未查过"
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return "未查过"
    v = (data or {}).get("cost_currency")
    return str(v).strip() if v else "未查过"


def read_burn(root: Path):
    """读事中流水（真 10 列 / 旧 10 列都认，**全按表头名定位**）。

    累计一律**由各行重算**：真口径逐行 `tokens_in/out` 求和、逐行 `估算成本` 求和
    （`未查过`→None，不参与）；旧口径取**最后一条有值的累计行**——对累计列求和会重复计
    （v5.3.0 前正是 `tot_cost += c`，把累计值当增量加了 N 遍）。
    """
    header, raw = read_csv_named(burn_path(root))
    kind = burn_schema(header)
    rows, bad = [], 0
    tot_tok, per_cost, cum_last = 0.0, 0.0, None
    any_per_cost = False
    if kind in ("无表头", "列名不认识"):
        return rows, len(raw), 0.0, 0.0, False
    real = kind == "10列真口径"
    for r in raw:
        if real:
            ts, card, role, model = (_cell(r, "时间", "时间戳"), _cell(r, "卡号", "卡"),
                                     _cell(r, "角色"), _cell(r, "模型"))
            pt = _num(_cell(r, "tokens_in", "prompt_tokens"))
            ct = _num(_cell(r, "tokens_out", "completion_tokens"))
            cost = _num(_cell(r, "估算成本"))
            cum = _num(_cell(r, "累计成本"))     # 真口径没有这列 ⇒ None（只为兼容旧行）
            note, rnd = _cell(r, "依据", "备注"), _cell(r, "轮次")
        else:
            ts, card, role, model = (_cell(r, "时间戳", "时间"), _cell(r, "卡", "卡号"),
                                     _cell(r, "角色"), _cell(r, "模型"))
            pt = _num(_cell(r, "prompt_tokens"))
            ct = _num(_cell(r, "completion_tokens"))
            cost = _num(_cell(r, "累计成本"))
            cum = cost
            note, rnd = _cell(r, "备注"), _cell(r, "轮次")
        if pt is None or ct is None:
            bad += 1
            continue
        tot_tok += pt + ct
        if real:
            if cost is not None:
                any_per_cost = True
                per_cost += cost
        elif cum is not None:
            cum_last = cum
        rows.append({"时间戳": ts, "卡": card, "角色": role, "模型": model, "轮次": rnd,
                     "prompt_tokens": int(pt), "completion_tokens": int(ct),
                     "累计成本": cum, "估算成本": cost, "依据": note, "备注": note})
    if real:
        return rows, bad, tot_tok, per_cost, any_per_cost
    return rows, bad, tot_tok, (cum_last or 0.0), cum_last is not None


def cost_of(prompt_tokens, completion_tokens, model, root: Path):
    """按 config.json 的 `cost_table` 计价：{模型: {"prompt": 每千token单价, "completion": ...}}。

    没有表就返回 None（**不猜价**）——价格/倍率是项目自己的数据资产（借鉴 tokencost：
    价格表随数据更新、有测试兜住），不该写死在纪律里。
    """
    cfg = root / "project-state" / "config.json"
    if not cfg.exists() or not model:
        return None
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    table = (data or {}).get("cost_table") or {}
    price = table.get(model) or {}
    pp, pc = _num(str(price.get("prompt", ""))), _num(str(price.get("completion", "")))
    if pp is None and pc is None:
        return None
    return (prompt_tokens * (pp or 0.0) + completion_tokens * (pc or 0.0)) / 1000.0


def spend(root: Path, row: dict, budget_tokens):
    """追加一轮用量：**写哪几列由目标账本现有表头决定**（旧账本继续旧写法，不被改成真列）。

    真口径：`时间/卡号/角色/模型/tokens_in/tokens_out/估算成本/币种/依据/登记时间`（逐行，无累计列）；
    旧口径：沿用 `轮次/累计token/累计成本` 写法。累计值返回前**由各行重算**，不读存档列。
    """
    rows, _, tot_tok, tot_cost, cost_known = read_burn(root)
    pt = int(row["prompt_tokens"])
    ct = int(row["completion_tokens"])
    c = cost_of(pt, ct, row.get("模型"), root)
    new_tok = tot_tok + pt + ct
    new_cost = (tot_cost + c) if c is not None else (tot_cost if cost_known else None)
    p = burn_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ts = row.get("时间戳") or now
    header, _raw = read_csv_named(p)
    if header and burn_schema(header) == "列名不认识":
        raise LedgerSchemaError(_refuse_write("成本流水", p, header, BURN_FIELDS, BURN_FIELDS_LEGACY))
    if not header:
        header = list(BURN_FIELDS)
        with p.open("a", encoding="utf-8", newline="") as fh:
            csv.writer(fh).writerow(header)
    note = row.get("备注") or ""
    card = row.get("卡") or row.get("卡号") or ""
    canon = {
        "时间": ts, "时间戳": ts, "卡": card, "卡号": card,
        "角色": row.get("角色") or "", "模型": row.get("模型") or "",
        "轮次": row.get("轮次") or "",
        "tokens_in": pt, "tokens_out": ct,
        "prompt_tokens": pt, "completion_tokens": ct,
        "估算成本": "未查过" if c is None else f"{c:.6f}",
        "累计token": int(new_tok),
        "累计成本": "" if new_cost is None else f"{new_cost:.6f}",
        "币种": "未查过" if c is None else _cost_currency(root),
        "依据": note, "备注": note, "登记时间": now,
    }
    with p.open("a", encoding="utf-8", newline="") as fh:
        csv.writer(fh).writerow([fmt_cell(canon.get(col)) for col in header])
    breaker = budget_tokens is not None and new_tok >= budget_tokens
    return {
        "ledger": str(p), "schema": burn_schema(header), "row_tokens": pt + ct,
        "round_tokens": pt + ct,
        "cum_tokens": int(new_tok), "cum_cost": None if new_cost is None else round(new_cost, 4),
        "budget_tokens": budget_tokens,
        "remaining_tokens": None if budget_tokens is None else int(budget_tokens - new_tok),
        "used_pct": None if not budget_tokens else round(new_tok / budget_tokens * 100.0, 1),
        "breaker": bool(breaker),
    }


def burn_status(root: Path, budget_tokens):
    header, _raw = read_csv_named(burn_path(root))
    rows, bad, tot_tok, tot_cost, cost_known = read_burn(root)
    by_role, by_model = {}, {}
    for r in rows:
        for bucket, key in ((by_role, r["角色"] or "（未填）"), (by_model, r["模型"] or "（未填）")):
            e = bucket.setdefault(key, {"tokens": 0, "calls": 0})
            e["tokens"] += r["prompt_tokens"] + r["completion_tokens"]
            e["calls"] += 1
    return {
        "rows": len(rows), "bad_rows": bad, "schema": burn_schema(header),
        "cum_tokens": int(tot_tok),
        "cum_cost": round(tot_cost, 4) if cost_known else None,
        "budget_tokens": budget_tokens,
        "remaining_tokens": None if budget_tokens is None else int(budget_tokens - tot_tok),
        "used_pct": None if not budget_tokens else round(tot_tok / budget_tokens * 100.0, 1),
        "breaker": bool(budget_tokens is not None and tot_tok >= budget_tokens),
        "by_role": by_role, "by_model": by_model,
    }


def render_burn(st: dict):
    L = ["# 事中成本流水（project-state/进度/成本流水.csv）", ""]
    if st.get("schema"):
        L.append(f"- 账本口径：**{st['schema']}**")
    L.append(f"- 累计 token：{fmt_tok(st['cum_tokens'])}（{st['rows']} 轮记录，坏行 {st['bad_rows']}）")
    if st["cum_cost"] is not None:
        L.append(f"- 累计成本：{st['cum_cost']:.4f}（按 config.json `cost_table` 计价；无表则不猜价）")
    if st["budget_tokens"] is not None:
        L.append(f"- 预算：{fmt_tok(st['budget_tokens'])}；已用 **{st['used_pct']}%**；"
                 f"剩余 {fmt_tok(st['remaining_tokens'])}")
        L.append(f"- **熔断状态：{'已触顶——按纪律停在安全点（当前卡收口、落盘、记 运行日志.md）' if st['breaker'] else '未触顶'}**")
    else:
        L.append("- 未给 `--budget-tokens`：**夜间序列必须带预算上限**，否则触顶前无人喊停。")
    for title, bucket in (("按角色", st["by_role"]), ("按模型", st["by_model"])):
        if not bucket:
            continue
        L += ["", f"## {title}分桶（谁的轮次在烧钱）", "", "| 对象 | 累计 token | 轮次 | 单轮均值 |", "|---|---|---|---|"]
        for k, v in sorted(bucket.items(), key=lambda x: -x[1]["tokens"]):
            L.append(f"| {k} | {fmt_tok(v['tokens'])} | {v['calls']} | {fmt_tok(v['tokens'] / v['calls'])} |")
    L += ["", "---", "",
          "- 计量在基础设施、熔断在运行循环（借鉴 MetaGPT `CostManager` + `Team._check_balance`）："
          "**每轮由主控调 `--spend` 累加并检查 `breaker`**，触顶即停。",
          "- 真 10 列账本**逐行**记 `tokens_in/tokens_out` 与当轮 `估算成本`（`未查过`＝不猜价），"
          "**没有累计列**：累计值每次由各行**重算**，不信任存档列——避免长出第二份事实源。"]
    return "\n".join(L)


# ---------------------------------------------------------------- 跑批契约与停机闸门（v5.2.0）
def contract_path(root: Path) -> Path:
    return root / "project-state" / CONTRACT_NAME


def _norm_moment(s, anchor=None):
    """把一个时刻写成 datetime；返回 (moment, 错误说明)。

    **不做静默猜测**：只写 `22:00` 而不给日期时，无法判断是今天还是明天——猜错的代价是整夜
    跑飞或压根没跑（定时唤起里"刚过去的一刻"还会静默滚到明天）。所以要么写全日期，
    要么给 `start_at` 当锚点。
    """
    s = str(s or "").strip()
    if not s:
        return None, None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt), None
        except ValueError:
            pass
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            return None, f"时刻非法：{s}"
        if anchor is None:
            return None, (f"`{s}` 只有时刻没有日期——请写完整日期（如 2026-09-16 22:00），"
                          "或同时给 start_at 作锚点；本工具不猜今天还是明天")
        return anchor.replace(hour=hh, minute=mm, second=0, microsecond=0), None
    return None, f"时刻无法解析：{s}（支持 `YYYY-MM-DD HH:MM`，或配 start_at 的 `HH:MM`）"


def load_contract(root: Path):
    """读跑批契约并解析时刻；返回 (契约, 错误说明)。错误说明非空 = 不可用。

    **契约是收工条件的唯一事实源**，这里只解析与折算（`start_at + window_hours → stop_at`、
    跨零点顺延），**不补默认值**——缺什么就是没声明什么。类型写错（如 `test_gate` 拼成
    `all_green`）一律报错：静默当成"关掉这条"会让一次跑批少了半个闸门，而报告上看不出来。
    """
    p = contract_path(root)
    if not p.exists():
        return None, (f"没有跑批契约 {p}——**没有停机条件的跑批不是跑批**；"
                      "先 `--contract-init` 起草，再由用户/主控填收工条件")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        return None, f"跑批契约不可解析（{exc}）：{p}"
    if not isinstance(data, dict):
        return None, f"跑批契约应为 JSON 对象：{p}"
    c = dict(data)

    start, err = _norm_moment(c.get("start_at"))
    if err:
        return None, f"start_at {err}"
    stop, err = _norm_moment(c.get("stop_at"), anchor=start)
    if err:
        return None, f"stop_at {err}"

    wh = c.get("window_hours")
    if wh not in (None, ""):
        try:
            wh = float(wh)
        except (TypeError, ValueError):
            return None, "window_hours 应为数字（小时）"
        if wh <= 0:
            return None, "window_hours 应大于 0"
        if stop is None and start is not None:
            stop = start + timedelta(hours=wh)
    else:
        wh = None
    if start and stop and stop <= start:
        stop = stop + timedelta(days=1)            # 跨零点（20:00 → 次日 02:00）

    budget = c.get("token_budget")
    if budget in (None, ""):
        budget = None
    else:
        try:
            budget = float(budget)
        except (TypeError, ValueError):
            return None, "token_budget 应为数字（token 数）"
        if budget <= 0:
            return None, "token_budget 应大于 0"

    cards_cfg = c.get("cards") or {}
    if not isinstance(cards_cfg, dict):
        return None, "cards 应为对象（如 {\"all_done\": true, \"ids\": []}）"

    gate = c.get("test_gate") or "none"
    if gate not in TEST_GATES:
        return None, f"test_gate 只接受 {' / '.join(TEST_GATES)}（实际 {gate!r}）"

    c["cards"] = cards_cfg
    c["test_gate"] = gate
    c["token_budget"] = budget
    c["_start"], c["_stop"], c["_window_hours"] = start, stop, wh
    return c, ""


def contract_budget(root: Path):
    """从契约取 token 额度（调用方未显式给 `--budget-tokens` 时用）；返回 (额度, 来源)。

    额度只声明一次：每轮重敲既费事，又是抄错额度的入口（少写一位数 = 熔断形同虚设，
    而账本上看不出"你本来想花多少"）。契约本身有效与否由 `--guard` 负责，这里只做查表。
    """
    c, _err = load_contract(root)
    if not c or c.get("token_budget") is None:
        return None, None
    return c["token_budget"], str(contract_path(root))


def contract_window(root: Path):
    """从契约折算时间窗（小时）：优先 `window_hours`，否则由 `start_at`→`stop_at` 得出。"""
    c, _err = load_contract(root)
    if not c:
        return None
    if c.get("_window_hours"):
        return c["_window_hours"]
    if c.get("_start") and c.get("_stop"):
        return (c["_stop"] - c["_start"]).total_seconds() / 3600.0
    return None


def contract_unattended(root: Path):
    """契约声明的无人值守口径（缺省 True：会写契约的跑批基本都是无人值守的）。"""
    c, _err = load_contract(root)
    if not c:
        return False
    return bool(c.get("unattended", True))


def _fm_value(text, key):
    """从 frontmatter 取一个键的值（剥掉行内注释与引号）。取不到返回 None。"""
    lines = text.splitlines()
    if not lines or lines[0].strip().lstrip("\ufeff") != "---":
        return None
    for ln in lines[1:]:
        if ln.strip() == "---":
            break
        if ":" in ln:
            k, v = ln.split(":", 1)
            if k.strip() == key:
                v = v.split("#", 1)[0].strip().strip('"').strip("'")
                return v or None
    return None


def read_card_states(root: Path, sub: str, kind: str, fname: str):
    """读卡卷宗的 status：需求卡/card-NNN/card.md、测试卡/tc-NNN/tc.md。

    返回 (states, note)：states 为 [(id, status)]；**目录不存在或无卡时返回 (None, 原因)**
    ——「没查到」与「查过且都收口」是两件事，调用方必须分开处理（同通用铁律②）。
    兼容卡卷宗（一卡一目录）与旧平铺（`card-001.md`）两种布局。
    """
    base = root / "project-state" / sub
    if not base.is_dir():
        return None, f"project-state/{sub}/ 目录不存在"
    out = []
    for d in sorted(base.glob(f"{kind}-*")):
        if d.is_dir():
            f = d / fname
            if not f.is_file():
                continue
        elif d.suffix == ".md":
            f = d
        else:
            continue
        try:
            text = f.read_text(encoding="utf-8-sig")
        except OSError:
            continue
        out.append((_fm_value(text, "id") or d.stem, _fm_value(text, "status") or ""))
    if not out:
        return None, f"project-state/{sub}/ 下没找到 {kind}- 卡（没查到不等于已收口）"
    return sorted(out), ""


def guard(root: Path, now=None, contract: dict = None):
    """多条件停机闸门：该不该停、为什么停、每条条件的现状。

    判定原则（都来自框架既有铁律，不是这里新发明的）：
    ① **查不到 ≠ 已满足**——卡目录缺失/无卡/契约点名了不存在的卡 → `unknown`，不置 fired；
    ② **没声明 ≠ 已关闭**——契约里没写的条件列为 `off` 并显示出来，免得被读成"不需要"；
    ③ **触顶即停**——任一条件 fired 即 STOP，先命中者即停机原因，不"再跑一轮看看"。
    """
    now = now or datetime.now()
    c = contract or {}
    conds, fired = [], []

    def add(key, label, state, detail):
        conds.append({"key": key, "label": label, "state": state, "detail": detail})
        if state == "fired":
            fired.append({"key": key, "label": label, "detail": detail})

    # ① 收工时刻
    stop, start = c.get("_stop"), c.get("_start")
    if stop:
        left = (stop - now).total_seconds() / 60.0
        if now >= stop:
            add("time", "收工时刻", "fired",
                f"已到 {stop:%Y-%m-%d %H:%M}（超 {abs(left):.0f} 分钟）")
        else:
            add("time", "收工时刻", "pending",
                f"到 {stop:%Y-%m-%d %H:%M} 收工，还剩 {left:.0f} 分钟")
    else:
        add("time", "收工时刻", "off", "契约未声明 stop_at / window_hours")

    # ② token 额度
    budget = c.get("token_budget")
    if budget is not None:
        st_tok = burn_status(root, budget)
        cum = st_tok["cum_tokens"]
        if st_tok["breaker"]:
            add("token", "token 额度", "fired",
                f"累计 {fmt_tok(cum)} 已达额度 {fmt_tok(budget)}（{st_tok['used_pct']}%）")
        else:
            add("token", "token 额度", "pending",
                f"累计 {fmt_tok(cum)} / 额度 {fmt_tok(budget)}（{st_tok['used_pct']}%，"
                f"剩 {fmt_tok(st_tok['remaining_tokens'])}；{st_tok['rows']} 轮记录）")
    else:
        add("token", "token 额度", "off", "契约未声明 token_budget")

    # ③ 需求卡收工条件
    cards_cfg = c.get("cards") or {}
    want = [x for x in (cards_cfg.get("ids") or []) if x]
    if cards_cfg.get("all_done") or want:
        states, note = read_card_states(root, "需求卡", "card", "card.md")
        if states is None:
            add("cards", "需求卡收工条件", "unknown", note)
        else:
            have = {i for i, _ in states}
            missing = [x for x in want if x not in have]
            if missing:
                add("cards", "需求卡收工条件", "unknown",
                    f"契约点名的卡不存在：{'、'.join(missing)}（无法判定，不是已达成）")
            else:
                pick = [(i, s) for i, s in states if not want or i in want]
                open_c = [f"{i}({s or '未标状态'})" for i, s in pick if s != CARD_DONE]
                scope = f"指定 {len(want)} 张" if want else f"全部 {len(pick)} 张"
                if open_c:
                    add("cards", "需求卡收工条件", "pending",
                        f"{scope}中未收口 {len(open_c)} 张：{'、'.join(open_c[:8])}"
                        + ("…" if len(open_c) > 8 else ""))
                else:
                    add("cards", "需求卡收工条件", "fired", f"{scope}全部收口（{CARD_DONE}）")
    else:
        add("cards", "需求卡收工条件", "off", "契约未声明 cards.all_done / cards.ids")

    # ④ 全量测试条件
    if c.get("test_gate") == "all_done":
        states, note = read_card_states(root, "测试卡", "tc", "tc.md")
        if states is None:
            add("test_gate", "全量测试条件", "unknown", note)
        else:
            open_t = [f"{i}({s or '未标状态'})" for i, s in states if s != CARD_DONE]
            if open_t:
                add("test_gate", "全量测试条件", "pending",
                    f"测试卡未收口 {len(open_t)}/{len(states)}：{'、'.join(open_t[:8])}"
                    + ("…" if len(open_t) > 8 else ""))
            else:
                add("test_gate", "全量测试条件", "fired", f"{len(states)} 张测试卡全部收口")
    else:
        add("test_gate", "全量测试条件", "off", "契约未声明 test_gate=all_done")

    # 开跑时刻是**闸门**不是收工条件：未到点就不该动手，但它不构成"可以收工"
    gate = {"declared": bool(start), "reached": True, "detail": "契约未声明 start_at"}
    if start:
        if now < start:
            gate = {"declared": True, "reached": False,
                    "detail": f"未到开跑时刻 {start:%Y-%m-%d %H:%M}"
                              f"（还有 {(start - now).total_seconds() / 60.0:.0f} 分钟）"}
        else:
            gate = {"declared": True, "reached": True,
                    "detail": f"已过开跑时刻 {start:%Y-%m-%d %H:%M}"}

    if fired:
        verdict, reason = GUARD_STOP, fired[0]
    elif start and now < start:
        verdict, reason = GUARD_NOT_STARTED, None
    else:
        verdict, reason = GUARD_CONTINUE, None

    unknown = [x["label"] for x in conds if x["state"] == "unknown"]
    return {
        "root": str(root),
        "contract": str(contract_path(root)),
        "依据": c.get("依据") or "",
        "now": now.strftime("%Y-%m-%d %H:%M:%S"),
        "verdict": verdict,
        "stop_reason": reason,
        "reasons": fired,
        "conditions": conds,
        "declared": len([x for x in conds if x["state"] != "off"]),
        "unknown": unknown,
        # 编排层真正该看的两件事合成一格：**能继续跑** ≠ **可以派活**（`unknown` 时前者成立后者不成立）。
        "dispatch_allowed": verdict == GUARD_CONTINUE and not unknown,
        "start_gate": gate,
        "unattended": bool(c.get("unattended", True)),
        "on_stop": c.get("on_stop") or SAFE_POINT_ACTION,
    }


def render_guard(st: dict):
    L = ["# 跑批停机闸门（project-state/跑批契约.json）", ""]
    head = {"CONTINUE": "可以继续跑", "STOP": "**该收工了**", "NOT_STARTED": "**还没到开跑时刻**"}
    L.append(f"- 判定：**{st['verdict']}** —— {head.get(st['verdict'], '')}")
    L.append(f"- 当前时刻：{st['now']}；开跑闸门：{st['start_gate']['detail']}")
    if st["依据"]:
        L.append(f"- 契约依据：{st['依据']}")
    if st["stop_reason"]:
        L.append(f"- 停机原因：**{st['stop_reason']['label']}**（{st['stop_reason']['detail']}）")
        L.append(f"- 收工动作：{st['on_stop']}")
        if not st["start_gate"].get("reached", True):
            L.append("")
            L.append("> **该跑批还没到开跑时刻，已经有收工条件达成了**——本次跑批**无需开跑**"
                     "（不是「跑到一半被叫停」）。先看清楚是哪条条件已满足：若这条本不该成立"
                     "（比如测试卡是上一批留下的旧状态），**先修数据再跑，别把「没活干」读成「活干完了」**。")
    L += ["", "## 各条件现状（**每条都是「达到即收工」**）", "",
          "| 条件 | 状态 | 现状 |", "|---|---|---|"]
    for c in st["conditions"]:
        L.append(f"| {c['label']} | {STATE_CN.get(c['state'], c['state'])} | {c['detail']} |")
    if st["unknown"]:
        L += ["", "> **有条件「没查到」**（" + "、".join(st["unknown"]) + "）：按通用铁律②，"
              "「没查到」不等于「已满足」——本次**既不据此收工，也不据此派活**，先补齐数据再判。"
              f"**这次退出码是 {EXIT_UNKNOWN}，不是 0**：一个数字表达不了「继续」和「数据没查到」两件事，"
              "所以这条单独有码——编排层照码行事即可，**不必（也不许）去正文里翻字面状态**。"]
    L += ["", "---", "",
          f"- 已声明条件 {st['declared']} 条；`off` 是**未声明**，不等于已关闭（要跑这条路就先写进契约）。",
          "- 判定由本脚本出，**停机执行在主控的循环里**：每轮派发前 `--guard`，STOP 即停在安全点"
          "（当前卡收口、落盘、记 `运行日志.md`、留痕报备）。",
          f"- 退出码：0=继续跑；{EXIT_STOP}=收工条件已满足；{EXIT_NOT_STARTED}=未到开跑时刻；"
          f"{EXIT_UNKNOWN}=有条件没查到（先补数据，不据以收工、不据此派活）；2=契约缺失/无任何条件/解析失败。"]
    return "\n".join(L)


# ---------------------------------------------------------------- 渲染
def render_calibrate(cal: dict):
    L = ["# 估算校准（实测台账：project-state/进度/估算实测.csv）", ""]
    if cal.get("ledger_schema"):
        L.append(f"- 账本口径：**{cal['ledger_schema']}**（{len(cal.get('ledger_columns') or [])} 列）")
    L.append(f"- 实测样本总行数：{cal['samples_total']}（坏行 {cal['bad_rows']}）")
    if not cal["types"]:
        L += ["", "**台账为空**——当前所有估算只能走结构特征基线（置信度低）。",
              "卡收口时用 `--record` 回填实测值，第二次估算才有据可依。"]
        return "\n".join(L)
    L += ["", "| 类型 | 样本 n | 可用（n>=%d） | 实测中位耗时 | 实测中位 token | 估时偏差系数 | token 偏差系数 | 卡号 |"
          % MIN_SAMPLES,
          "|---|---|---|---|---|---|---|---|"]
    for t, v in sorted(cal["types"].items()):
        tf = "-" if v["time_factor"] is None else v["time_factor"]
        kf = "-" if v["token_factor"] is None else v["token_factor"]
        mm = "-" if v["median_act_min"] is None else fmt_min(v["median_act_min"])
        mt = "-" if v["median_act_tokens"] is None else fmt_tok(v["median_act_tokens"])
        L.append(f"| {t} | {v['n']} | {'是' if v['usable'] else '否（仅参考）'} | "
                 f"{mm} | {mt} | "
                 f"{tf} | {kf} | {', '.join(v['cards'])} |")
    L += ["", f"可用于估算（实测校正）的类型：{'、'.join(cal['usable_types']) or '（暂无）'}"]
    return "\n".join(L)


def render_blockers(blockers):
    L = ["# 无人值守前置检查：待办/自然人.md 未闭环项", ""]
    if not blockers:
        L.append("无未闭环人工依赖——夜间序列不存在「停在地雷前空转」的风险。")
        return "\n".join(L)
    L.append(f"**{len(blockers)} 项未闭环**——凡依赖这些事项的卡**不得进夜间序列**：")
    L.append("")
    L.append("| 事项 | 为何要自然人 | 状态 |")
    L.append("|---|---|---|")
    for b in blockers:
        L.append(f"| {b['事项']} | {b['为何要自然人']} | {b['状态']} |")
    return "\n".join(L)


def render_plan(plan: dict):
    L = ["# 长时任务估算（编排输入；最终编排由 agent.md 主控决定）", ""]
    L.append(f"- 时间窗：{fmt_min(plan['window_hours'] * 60)} → **{plan['window_tier']}**（取向：{plan['tier_hint']}）")
    L.append(f"- 模式：{'无人值守' if plan['unattended'] else '有人在侧'}；排序取向：{'短优先' if plan['order'] == 'short' else '长优先'}")
    if plan["budget_tokens"] is not None:
        L.append(f"- token 预算：{fmt_tok(plan['budget_tokens'])}"
                 + ("（**已触发熔断**）" if plan["budget_stop"] else ""))
    L.append(f"- 合计估时：{fmt_min(plan['total_est_min'])}；合计估 token：{fmt_tok(plan['total_est_tokens'])}；"
             f"窗口利用率：{plan['utilization_pct']}%")
    L.append(f"- **本序列在窗口内跑完的概率：P = {plan['p_finish']:.0%}**"
             f"（σ 合计 {plan['sigma_total']:.0f} 分钟；下限 {plan['p_min']:.0%}）——低于下限会**裁尾**，不虚报。")
    if plan.get("process_tier"):
        L.append(f"- 流程档：**{plan['process_tier']}**（{plan['process_hint']}；{plan['process_basis']}）"
                 f"——流程档回答的是「这些卡值不值得走全套 SOP」，不是工期。")
    L.append("")

    L.append("## 建议序列")
    if not plan["selected"]:
        L.append("（无可排项——见下方排除原因）")
    else:
        L.append("| # | 卡 | 类型 | 优先级 | 流程档 | 估时 O/M/P → 期望±σ | 估 token（区间） | 置信度 | 依据 |")
        L.append("|---|---|---|---|---|---|---|---|---|")
        for i, e in enumerate(plan["selected"], 1):
            tag = "（回填）" if e["id"] in (plan.get("backfilled") or []) else ""
            if e.get("needs_review"):
                tag += "（**待复核**）"
            pe = e.get("pert") or {}
            L.append(f"| {i} | {e['id']} {e['名称']}{tag} | {e['类型']} | {e['优先级']} | "
                     f"{e.get('process_tier', '-')} | "
                     f"{fmt_min_c(pe.get('o', e['est_min_low']))}/{fmt_min_c(pe.get('m', e['est_min_most_likely']))}/"
                     f"{fmt_min_c(pe.get('p', e['est_min_high']))} → {fmt_min_c(e['est_min'])}±{e.get('sigma_min', 0):.0f} | "
                     f"{fmt_tok(e['est_tokens'])}（{fmt_tok(e['est_tokens_low'])}–{fmt_tok(e['est_tokens_high'])}） | "
                     f"{e['confidence']} | {'；'.join(e['basis'])} |")
        if plan.get("backfilled"):
            L.append("")
            L.append(f"（标「回填」的 {len(plan['backfilled'])} 张是主序放不下的补充项，用短优先填满剩余窗口——"
                     "**长时窗最大的浪费是空转**。）")

    if plan.get("notes"):
        L.append("")
        for n in plan["notes"]:
            L.append(f"> {n}")

    if plan["excluded"] or plan["spill"]:
        L += ["", "## 未纳入及原因（**不静默丢项**）", "", "| 卡 | 类型 | 原因 |", "|---|---|---|"]
        for e in plan["excluded"] + plan["spill"]:
            L.append(f"| {e['id']} {e['名称']} | {e['类型']} | {e['reason']} |")

    if plan["unattended"] and plan["blockers"]:
        L += ["", f"## 夜间地雷提示（待办/自然人.md 未闭环 {len(plan['blockers'])} 项）", ""]
        for b in plan["blockers"]:
            L.append(f"- {b['事项']}（{b['状态']}）")
        L.append("")
        L.append("**务必先让用户清掉这些项**，否则依赖它们的链条会停在前面空转到天亮。")

    # ---- 无人在场时替你做的假设（必须留痕，v4.6.0）----
    if plan.get("assumptions"):
        L += ["", "## 无人在场时替你做的假设（**必须逐条留痕**）", ""]
        for a in plan["assumptions"]:
            L.append(f"- {a}")

    L += ["", "---", "",
          "- 估算依据：三点估算（O/M/P）+ BetaPERT 精确方差；端点优先取同类实测比值分位点"
          "（n>=%d），否则基线倍数（**非实测推导**，置信度中/低）。" % MIN_SAMPLES,
          "- 命中概率：σ 按 √Σσ² 合成，P = Φ((窗口 − 期望合计)/σ合计)；**低于 %d%% 裁尾**，不虚报。"
          % round(plan.get("p_min", P_FINISH_MIN) * 100),
          "- 无人值守硬约束：排除不可逆/对外动作、排除需人工拍板的卡、带 token 预算熔断、异常停在安全点。",
          "- **事中熔断由主控每轮执行**：`task_estimate.py --spend ...` 累加用量，触顶停在安全点"
          "（本脚本给机制与账本，循环在主控）。",
          "- 实际值请在卡收口时用 `--record` 回填 `进度/估算实测.csv`——**没有回填的估算，第二次就没人信**。"]
    return "\n".join(L)


def write_plan_section(root: Path, body: str) -> Path:
    """把估算表写入 project-state/主控工作计划.md 的标记段（幂等：重跑只替换段内内容）。"""
    p = root / "project-state" / "主控工作计划.md"
    block = f"{PLAN_MARK_BEGIN}\n{body}\n{PLAN_MARK_END}\n"
    old = p.read_text(encoding="utf-8") if p.exists() else "# 主控工作计划\n"
    if PLAN_MARK_BEGIN in old and PLAN_MARK_END in old:
        new = re.sub(re.escape(PLAN_MARK_BEGIN) + r".*?" + re.escape(PLAN_MARK_END),
                     block.rstrip("\n"), old, flags=re.S)
    else:
        new = old.rstrip("\n") + "\n\n## 长时任务估算（v4.5.0，项目经理产出 / 主控编排依据）\n\n" + block
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(new, encoding="utf-8")
    return p


# ---------------------------------------------------------------- CLI
def load_candidates(path: Path):
    """读候选清单；返回 list 或 None（调用方负责报错退出码）。"""
    if not path.exists():
        print(f"错误：候选文件不存在 {path}", file=sys.stderr)
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        print(f"错误：候选文件不可解析（{exc}）", file=sys.stderr)
        return None
    if isinstance(data, dict):
        data = data.get("tasks") or data.get("候选") or []
    if not isinstance(data, list):
        print("错误：候选文件应为数组（或含 tasks 数组的对象）", file=sys.stderr)
        return None
    return data


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="长时任务估算（时间 + token；估算与候选序由项目经理出，编排由 agent.md 决定）")
    ap.add_argument("root_pos", nargs="?", help="项目根（位置参数，等价 --root）")
    ap.add_argument("--root", help="项目根")
    ap.add_argument("--calibrate", action="store_true", help="读估算实测.csv 出校准表")
    ap.add_argument("--blockers", action="store_true", help="列 待办/自然人.md 未闭环项")
    ap.add_argument("--readiness", metavar="候选.json", help="可实施性关卡（长时编排前置）：PASS/CONCERNS/FAIL")
    ap.add_argument("--plan", metavar="候选.json", help="按时间窗出候选序列")
    ap.add_argument("--window", type=float, help="时间窗（小时）")
    ap.add_argument("--p-min", type=float, default=P_FINISH_MIN,
                    help=f"序列命中概率下限（低于则裁尾；默认 {P_FINISH_MIN}）")
    ap.add_argument("--budget-tokens", type=float, default=None, help="token 预算上限（超即熔断）")
    ap.add_argument("--unattended", action="store_true", help="无人值守模式（排除不可逆/对外动作）")
    ap.add_argument("--done", default="", help="已完成的卡号，逗号分隔（依赖判定用）")
    ap.add_argument("--order", choices=["auto", "short", "long"], default="auto")
    ap.add_argument("--write-plan", action="store_true", help="把估算表写入 主控工作计划.md 标记段")
    ap.add_argument("--record", action="store_true", help="回填实测值到 进度/估算实测.csv（真 11 列）")
    ap.add_argument("--card", help="--record/--spend：卡号")
    ap.add_argument("--type", help="--record：任务类型/角色（旧 6 列别名）")
    ap.add_argument("--est-o", type=float, help="--record：估算 O（小时，真 11 列）")
    ap.add_argument("--est-m", type=float, help="--record：估算 M（小时，真 11 列）")
    ap.add_argument("--est-p", type=float, help="--record：估算 P（小时，真 11 列）")
    ap.add_argument("--window-p", type=float, help="--record：P(窗口) 完成概率 0~1")
    ap.add_argument("--act-hours", type=float, help="--record：实际耗时（小时）")
    ap.add_argument("--deviation", type=float, help="--record：偏差（小时；缺省＝实际 − M）")
    ap.add_argument("--trim", default="", help="--record：裁尾（裁掉的验收面，无则留空写 `-`）")
    ap.add_argument("--evidence", default="", help="--record：证据（`路径::锚点` 或作业 id）")
    ap.add_argument("--record-time", help="--record：登记时间（YYYY-MM-DD HH:MM；缺省取本机）")
    ap.add_argument("--est-min", type=float, help="--record：估算分钟（旧 6 列别名，写入时换算成小时）")
    ap.add_argument("--act-min", type=float, help="--record：实际分钟（旧 6 列别名）")
    ap.add_argument("--est-tokens", type=float, help="--record：估算 token（**只写旧 6 列账本**）")
    ap.add_argument("--act-tokens", type=float, help="--record：实际 token（**只写旧 6 列账本**）")
    ap.add_argument("--spend", action="store_true", help="事中成本流水：追加一轮用量并算熔断")
    ap.add_argument("--burn-status", action="store_true", help="事中成本流水：累计/分桶/熔断状态")
    ap.add_argument("--role", help="--spend：角色名")
    ap.add_argument("--model", help="--spend：模型名（配 config.json cost_table 计价）")
    ap.add_argument("--round", dest="round_no", help="--spend：轮次/派发序号")
    ap.add_argument("--prompt-tokens", type=float, help="--spend：本轮输入 token")
    ap.add_argument("--completion-tokens", type=float, help="--spend：本轮输出 token")
    ap.add_argument("--note", default="", help="--spend：备注")
    ap.add_argument("--guard", action="store_true",
                    help="跑批停机闸门：读跑批契约判该不该收工（0=继续/3=收工/4=未到点/2=无契约）")
    ap.add_argument("--contract-init", action="store_true", help="生成跑批契约样板（不覆盖已有）")
    ap.add_argument("--force", action="store_true", help="--contract-init：覆盖已有契约")
    ap.add_argument("--now", default="",
                    help="--guard：指定「当前时刻」（YYYY-MM-DD HH:MM，测试/补判用；缺省取本机时间）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args(argv)

    root = Path(args.root or args.root_pos or ".").resolve()
    done = {x.strip() for x in args.done.split(",") if x.strip()}

    try:
        if args.record:
            if not args.card:
                print("错误：--record 需要 --card（卡号）", file=sys.stderr)
                return 2
            # 真 11 列（小时制）主参数；旧 6 列（分钟制）别名继续可用，写入时按目标表头换算
            m = args.est_m if args.est_m is not None else (
                None if args.est_min is None else args.est_min / 60.0)
            act_h = args.act_hours if args.act_hours is not None else (
                None if args.act_min is None else args.act_min / 60.0)
            role = args.role or args.type or ""
            given = [x for x in (args.est_o, m, args.est_p, args.window_p, act_h,
                                 args.trim, args.evidence, args.est_tokens, args.act_tokens)
                     if x not in (None, "")]
            if not given:
                print("错误：--record 至少要给一项实测"
                      "（--est-o/--est-m/--est-p/--act-hours/--window-p/--trim/--evidence，"
                      "或旧别名 --est-min/--act-min/--est-tokens/--act-tokens）", file=sys.stderr)
                return 2
            dev = args.deviation
            if dev is None and m is not None and act_h is not None:
                dev = round(act_h - m, 4)          # 偏差 = 实际 − M（`进度/台账口径.md` §一）
            row = {
                "卡号": args.card, "角色": role, "类型": role,
                "估算O(小时)": args.est_o, "估算M(小时)": m, "估算P(小时)": args.est_p,
                "P(窗口)": args.window_p, "实际(小时)": act_h, "偏差": dev,
                "裁尾": args.trim, "证据": args.evidence,
                "登记时间": args.record_time or datetime.now().strftime("%Y-%m-%d %H:%M"),
                "估时分钟": args.est_min if args.est_min is not None else (
                    None if m is None else m * 60.0),
                "实时分钟": args.act_min if args.act_min is not None else (
                    None if act_h is None else act_h * 60.0),
                "估token": args.est_tokens, "实token": args.act_tokens,
            }
            try:
                p = record(root, row)
            except LedgerSchemaError as exc:
                print(f"错误：{exc}", file=sys.stderr)
                return 2
            header, _rows = read_csv_named(p)
            kind = ledger_schema(header)
            if kind == "11列真口径" and (args.est_tokens is not None or args.act_tokens is not None):
                print("注意：真 11 列台账没有 token 实测列，--est-tokens/--act-tokens 未写入",
                      file=sys.stderr)
            out = {"ok": True, "ledger": str(p), "schema": kind, "columns": header,
                   "row": {k: fmt_cell(v) for k, v in row.items()}}
            print(json.dumps(out, ensure_ascii=False, indent=2) if args.json
                  else f"已回填实测：{row['卡号']}（{role or '未填角色'}）→ {p}［{kind}］")
            return 0

        if args.blockers:
            items = read_blockers(root)
            if args.json:
                print(json.dumps({"root": str(root), "open_blockers": items},
                                 ensure_ascii=False, indent=2))
            else:
                print(render_blockers(items))
            return 0

        if args.readiness:
            cands = load_candidates(Path(args.readiness))
            if cands is None:
                return 2
            rv = readiness_check(cands, root,
                                 bool(args.unattended or (args.window or 0) >= NIGHT_HOURS
                                      or contract_unattended(root)), done)
            if args.json:
                print(json.dumps(rv, ensure_ascii=False, indent=2))
            else:
                print(render_readiness(rv))
            return 1 if rv["verdict"] == RV_FAIL else 0

        if args.contract_init:
            p = contract_path(root)
            if p.exists() and not args.force:
                print(f"已存在，未覆盖：{p}（确实要重起用 --force——契约里装的是用户声明的收工条件，"
                      "误覆盖等于悄悄换掉一次跑批的判据）")
                return 0
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(CONTRACT_TEMPLATE, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
            out = {"ok": True, "contract": str(p), "keys": list(CONTRACT_TEMPLATE)}
            print(json.dumps(out, ensure_ascii=False, indent=2) if args.json
                  else f"已生成跑批契约样板：{p}\n填好收工条件后，运行循环每轮调 `--guard` 判定。")
            return 0

        if args.guard:
            c, cerr = load_contract(root)
            if cerr:
                print(f"错误：{cerr}", file=sys.stderr)
                return 2
            now = None
            if args.now:
                now, nerr = _norm_moment(args.now)
                if nerr:
                    print(f"错误：--now {nerr}", file=sys.stderr)
                    return 2
            gst = guard(root, now=now, contract=c)
            if gst["declared"] == 0:
                print("错误：跑批契约里一条停机条件都没有（时间 / token / 需求卡 / 测试卡全缺）"
                      "——**「没有闸门」不许被读成「没到收工」**，先补条件再跑",
                      file=sys.stderr)
                return 2
            if args.json:
                print(json.dumps(gst, ensure_ascii=False, indent=2))
            else:
                print(render_guard(gst))
            if gst["verdict"] == GUARD_CONTINUE and gst["unknown"]:
                return EXIT_UNKNOWN
            return {"CONTINUE": 0, "STOP": EXIT_STOP,
                    "NOT_STARTED": EXIT_NOT_STARTED}[gst["verdict"]]

        if args.spend:
            if args.prompt_tokens is None or args.completion_tokens is None:
                print("错误：--spend 需要 --prompt-tokens 与 --completion-tokens", file=sys.stderr)
                return 2
            budget, bsrc = args.budget_tokens, "cli"
            if budget is None:
                budget, cpath = contract_budget(root)
                bsrc = f"契约 {cpath}" if budget is not None else None
            try:
                res = spend(root, {"卡": args.card or "", "角色": args.role or "",
                                   "模型": args.model or "", "轮次": args.round_no or "",
                                   "prompt_tokens": int(args.prompt_tokens),
                                   "completion_tokens": int(args.completion_tokens),
                                   "备注": args.note}, budget)
            except LedgerSchemaError as exc:
                print(f"错误：{exc}", file=sys.stderr)
                return 2
            res["budget_source"] = bsrc
            if args.json:
                print(json.dumps(res, ensure_ascii=False, indent=2))
            else:
                line = f"本轮 {fmt_tok(res['round_tokens'])}；累计 {fmt_tok(res['cum_tokens'])}"
                line += f"［账本 {res.get('schema', '?')}］"
                line += (f"（{res['used_pct']}% / 预算 {fmt_tok(res['budget_tokens'])}"
                         f"{'，取自' + bsrc if bsrc and bsrc != 'cli' else ''}）"
                         if res["budget_tokens"] else "（**未设预算上限**）")
                print(line)
                if res["breaker"]:
                    print("**已触顶：按纪律停在安全点**——当前卡收口、落盘、记 `运行日志.md`，"
                          "不静默烧完、不「再跑一轮看看」。")
            # 触顶是**硬信号**（退出码 3）：额度用完还继续派发，等于前面那条熔断只写在文档里
            return EXIT_STOP if res["breaker"] else 0

        if args.burn_status:
            budget, bsrc = args.budget_tokens, "cli"
            if budget is None:
                budget, cpath = contract_budget(root)
                bsrc = f"契约 {cpath}" if budget is not None else None
            st = burn_status(root, budget)
            st["budget_source"] = bsrc
            if args.json:
                print(json.dumps(st, ensure_ascii=False, indent=2))
            else:
                print(render_burn(st))
            return 0

        if args.plan:
            cp = Path(args.plan)
            candidates = load_candidates(cp)
            if candidates is None:
                return 2
            window = args.window
            if window is None:
                window = contract_window(root)     # v5.2.0：窗口可由跑批契约折算（start_at→stop_at）
                if window is None:
                    print("错误：--plan 需要 --window <小时>（或由跑批契约给出 stop_at、"
                          "start_at+window_hours）", file=sys.stderr)
                    return 2
            budget = args.budget_tokens
            if budget is None:
                budget, _src = contract_budget(root)
            unattended = bool(args.unattended or window >= NIGHT_HOURS
                              or contract_unattended(root))
            plan = build_plan(candidates, window, budget, root,
                              unattended, done, args.order, args.p_min)
            body = render_plan(plan)
            if args.write_plan:
                plan["written_to"] = str(write_plan_section(root, body))
            if args.json:
                print(json.dumps(plan, ensure_ascii=False, indent=2))
            else:
                print(body)
                if plan.get("written_to"):
                    print(f"\n已写入：{plan['written_to']}")
            return 0

        if args.calibrate:
            cal = calibrate(root)
            if args.json:
                print(json.dumps(cal, ensure_ascii=False, indent=2))
            else:
                print(render_calibrate(cal))
            return 0

        ap.print_help()
        return 0
    except OSError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    _reconfigure()
    sys.exit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""retro_calibrate.py — 复盘与角色职责校准（v4.7.0，主控/项目经理的可执行部分）

为什么有这个脚本
----------------
用户要求"随着项目开发优化整个项目组的开发流程 / 判定并优化各角色的项目职责"。但**不是每次
调用 agent 都优化**——那会把"优化"变成噪音；而是在**复盘点**才做。同时，上一轮复盘提出的
**行动项必须核验是否真落地**——"提了就完事"是这类机制最常见的失效方式。

本脚本把这三件事变成动作：
1. `--due`      到点判定：按**收口卡数**（迭代口径）触发，不按墙钟时间
2. `--assemble` 汇总各角色的量化输入（**只汇总事实，判断留给主控**）
3. `--actions`  机制 K：上一轮行动项逐条核落地（**本脚本的核心**）
4. `--record-action` / `--close`  行动项登记与复盘收口

观察窗口径（已定案，不许改）
--------------------------
**按迭代（收口卡数）触发，不按墙钟时间。** 理由：框架已有 `test_card_cadence` 这套迭代节奏
词汇，不该引入第二套；且 agent 框架里墙钟时间不映射工作量——项目闲置两周会触发一次零新数据
的复盘，三天干完 40 张卡又会错过漂移窗口。**时间只作"陈旧提醒"，不作触发器。**

硬纪律（代码守卫，不是约定）
----------------------------
- **没写核验证据 ≠ 已落地**：行动项 `核验证据` 列为空一律判 `unverified`，不许当作默认已完成。
  这是本脚本最不能被改坏的一条守卫（回归测试专门用变异验证钉住它）。
- **未查过 ≠ 查过且干净**：`--assemble` 的某个数据来源文件不存在时，必须明确写"未查过"，
  不许把"没有数据"渲染成"没有异常"。这是本框架通行的一条硬纪律。
- **只汇总事实，不做角色judgment**：`--assemble` 不评判哪个角色好/差、不下"该调整谁"的结论，
  只给量化输入；职责调整的判断留给主控（agent.md）与用户。
- **时间不作触发器**：`--due` 只看收口卡数差值；`stale_days` 只产出"陈旧提醒"，不改 due 真假。

数据文件（新域 project-state/复盘/，运行时按需懒创建）
----------------------------------------------------
- `复盘台账.csv`  列：轮次,日期,迭代标记,里程碑,结论摘要,行动项数,已落地数
  （`迭代标记` = 本轮复盘时的**收口卡数**）
- `行动项.csv`    列：行动项ID,来源轮次,提出日期,类别,行动项,责任人,预期效果,核验证据,核验轮次,状态,逾期,落地情况
  （`类别` ∈ 职责调整/流程优化/工具改进/文档补齐/其他；`状态` ∈ open/done/dropped；
   `逾期` ∈ 是/空，`落地情况` ∈ landed/evidence-missing/unverified/dropped，两列**正交**，
   均由 `--actions` 写回）

两条正交的核验信息（**不许合并成一列**）
-------------------------------------
这是 v4.6.0「没人确认 ≠ 默默通过」铁律的落地：**落地**与**逾期收口**是两件事，混在一列会让
"真落地了但没人在复核时点核收"静默通过。故拆成两列：

- `落地情况` **只表达证据判定**（与逾期无关）：
  1. `状态=dropped`                                        → `dropped`（**不计未落地**，但仍列出：
     让读者看得到"主动放弃"与"忘了做"的区别）
  2. 证据核验通过（路径存在；`路径::关键字` 时关键字也在）  → `landed`
  3. 证据列为空                                            → `unverified`
  4. 证据路径坏 / `路径::关键字` 关键字不在                 → `evidence-missing`
- `逾期` **只表达"到点了还没人在核收"**：`状态=open` **且** `核验轮次 ≤ 当前轮次` → `是`，否则空。
  **与证据判定完全无关**——`landed` + `逾期=是` 是合法且最危险的一类（真落地了，没人核收）。
- `--actions` 退出码 1 ⟺ 存在 `unverified` 或 `evidence-missing` **或 `逾期=是`**（三者任一）。
- `--close` 的 `已落地数` = `落地情况=landed` 的条数，**与 `逾期` 无关**（逾期不污染落地率，
  但单列一个「已落地但逾期未收口」小节让它可见）。

`--close` 幂等判据
------------------
**台账最后一行的 `日期` == 今天** → 判定为"同一天重复收口"，**原地更新那一行**（不追加）；
否则追加 `轮次 = 上一行轮次 + 1` 的新行。用日期而非轮次做判据，是因为重复调用时轮次每次都会
算成"上一行 +1"，只有日期能稳定识别"今天已经收过口"。
更新已有行时：**未传 `--结论`/`--milestone` 一律保留旧值**（不清空），两参数对称处理。

其他口径（避免二次歧义）
------------------------
- `--record-action` 的 `来源轮次` 缺省 = 台账最后一行轮次（无台账则 1）；`提出日期` = 当天；
  默认 `状态=open`、`逾期` 与 `落地情况` 留空。
- `--actions` 时 `行动项.csv` 不存在 → 视为"尚无行动项"（**首轮复盘前本就没有**），退出码 0。
- `--assemble` 两本台账**一律按表头名定位列**（v-next，口径见 `进度/台账口径.md`）：
  真 11 列 `估算实测.csv`（角色 / 估算M(小时) / 实际(小时)）与真 10 列 `成本流水.csv`
  （逐行 `tokens_in/tokens_out` + 当轮 `估算成本`）都认；旧 6 列 / 旧 10 列账本继续按自身表头读。
- `--assemble` 成本口径（**V-02 定案，2026-10-08**）：`估算成本=未查过` 是**正式记法**（"这项没查过"），
  **任何汇总都不得据此反算成本**——即使 `config.json` 里有 `cost_table` 也不反算（与
  `task_estimate --burn-status` 同口径，两工具不再给出互相矛盾的数）。`cost_table` 只在
  账本**完全没有成本列**时兜底计价，且此时 `cost_source="cost_table"`、notes 明写"估算值不是记录值"。
- `--assemble` 成本算法：真 10 列的 `估算成本` 是**当轮值**逐行相加（`未查过` 跳过）；
  旧 10 列的 `累计成本` 是累计值，直接按角色求和会重复计，故**逐行差分**还原；
  **都不可得则该角色成本留空——留空 = 不猜价**。

用法
----
    python scripts/retro_calibrate.py --due <项目根> [--json]
    python scripts/retro_calibrate.py --assemble <项目根> [--json]
    python scripts/retro_calibrate.py --actions <项目根> [--json]
    python scripts/retro_calibrate.py --record-action <项目根> --类别 <c> --行动项 <text> --责任人 <r> \
        [--预期效果 <text>] [--核验证据 <path[;path]>] [--核验轮次 <n>] [--来源轮次 <n>] [--json]
    python scripts/retro_calibrate.py --close <项目根> [--结论 <text>] [--milestone <text>] [--json]

配置（只读 project-state/config.json，缺项即用默认值）
----------------------------------------------------
    "retro_cadence": {"every_n_cards": 20, "stale_days": 30}
读不到该配置项时用默认值 20 / 30 正常工作，不报错（多数项目还没有这一项）。

退出码：0=正常；1=`--actions` 发现 `unverified`/`evidence-missing`/`逾期=是` 的行动项；
2=输入错误（不是框架工作区、缺必需参数等）。
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import re
import statistics
import sys
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------- 常量
DEFAULT_EVERY_N_CARDS = 20   # 默认复盘密度：每 20 张收口卡一轮
DEFAULT_STALE_DAYS = 30      # 默认陈旧阈值（只产提醒，不作触发器）

RETRO_SUBDIR = ("project-state", "复盘")
LEDGER_NAME = "复盘台账.csv"
ACTION_NAME = "行动项.csv"
LEDGER_FIELDS = ["轮次", "日期", "迭代标记", "里程碑", "结论摘要", "行动项数", "已落地数"]
ACTION_FIELDS = ["行动项ID", "来源轮次", "提出日期", "类别", "行动项", "责任人",
                 "预期效果", "核验证据", "核验轮次", "状态", "逾期", "落地情况"]
ACTION_CATEGORIES = ("职责调整", "流程优化", "工具改进", "文档补齐", "其他")
ACTION_STATUSES = ("open", "done", "dropped")
# 落地情况 = **只表达证据判定**（不含逾期）；逾期是与之正交的独立列。
LANDED_STATES = ("landed", "evidence-missing", "unverified", "dropped")
# 触发退出码 1 的两类证据判定 + 逾期（三者任一即 1）
FAIL_STATES = ("unverified", "evidence-missing")

# 三处结构化来源（--assemble）。路径都相对 项目根 的 project-state/进度/。
EST_LEDGER_REL = ("project-state", "进度", "估算实测.csv")
BURN_LEDGER_REL = ("project-state", "进度", "成本流水.csv")
PROGRESS_DIR_REL = ("project-state", "进度")

# 内置 BURN_FIELDS 副本（取不到 sibling 时的兜底，列序与 task_estimate.py 一致）
BURN_FIELDS_FALLBACK = ["时间戳", "卡", "角色", "模型", "轮次", "prompt_tokens",
                        "completion_tokens", "累计token", "累计成本", "备注"]


def _reconfigure():
    """Windows GBK 控制台安全：无法编码的字符替换而非抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, OSError):
            pass


# ---------------------------------------------------------------- 基础工具
def _num(cell):
    """把 CSV 单元格转成 float；空/占位符返回 None（不把空白猜成 0）。"""
    s = (cell or "").strip()
    if s in ("", "-", "None", "none", "NA", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _int(cell, default=0):
    v = _num(cell)
    return int(v) if v is not None else default


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _parse_date(s):
    try:
        return datetime.strptime((s or "").strip(), "%Y-%m-%d")
    except ValueError:
        return None


def _read_csv_dicts(path: Path, fields):
    """按给定列名读 CSV（utf-8-sig 兼容 Excel BOM）。缺文件返回空表，不抛异常。

    **不用裸 split(',')**：行动项/结论摘要里带逗号很常见，裸切会把一行切碎、把其它列写坏。
    **滤掉 `#` 注释行与空行**：否则注释行会被 csv 当表头，整表错位一格（与 task_estimate 口径一致）。
    """
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return []
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if not lines:
        return []
    rows = []
    try:
        for rec in csv.DictReader(lines):
            rows.append({k: (rec.get(k) or "") for k in fields})
    except csv.Error:
        return rows
    return rows


def _write_csv_dicts(path: Path, fields, rows):
    """整表写回（csv 模块负责引号/转义），父目录懒创建。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


def _read_csv_raw(path: Path):
    """按**文件自身表头名**读成 `(header, rows)`，不做任何列名假设。

    给「真口径 / 旧口径列名不同」的两本台账用：上层按别名取值（`_pick`），
    这样真 11 列（角色/估算M(小时)/实际(小时)/…) 与旧 6 列（类型/估时分钟/…）
    都能读，且**列名不认识时不会被硬套成另一个意思**。缺文件返回 `([], [])`，不抛异常。
    """
    if not path.exists():
        return [], []
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return [], []
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if not lines:
        return [], []
    try:
        header = [(h or "").strip() for h in next(csv.reader([lines[0]]))]
    except (csv.Error, StopIteration):
        header = []
    rows = []
    try:
        for rec in csv.DictReader(lines):
            rows.append({(k or "").strip(): (v or "") for k, v in rec.items()})
    except csv.Error:
        pass
    return header, rows


def _pick(rec, *names):
    """按别名依次取第一处非空值：真口径与旧口径列名不同，靠这张别名表兜住。

    找不到返回 None——**不拿 0/空串顶替**，让上层决定「未查过」怎么写。
    """
    for n in names:
        v = rec.get(n)
        if v not in (None, ""):
            return v
    return None


def _burn_fields():
    """成本流水列取 task_estimate.py 的 BURN_FIELDS（同源，避免两处列定义各自漂移）。

    取不到 sibling（沙箱版本、被单独拷贝）时退回内置同序副本，保证本脚本仍能跑。
    **注**：`--assemble` 已改为按文件表头名定位列（真/旧口径都能读），
    本函数保留给「需要知道当前默认列定义」的调用方，不再参与解析。
    """
    try:
        p = Path(__file__).resolve().parent / "task_estimate.py"
        if p.exists():
            spec = importlib.util.spec_from_file_location("_te_burn_fields", p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            got = list(getattr(mod, "BURN_FIELDS", []) or [])
            if got:
                return got
    except Exception:
        pass
    return list(BURN_FIELDS_FALLBACK)


# ---------------------------------------------------------------- 文件路径
def retro_dir(root: Path) -> Path:
    return root.joinpath(*RETRO_SUBDIR)


def ledger_path(root: Path) -> Path:
    return retro_dir(root) / LEDGER_NAME


def action_path(root: Path) -> Path:
    return retro_dir(root) / ACTION_NAME


def require_workspace(root: Path) -> bool:
    """框架工作区的最小判据：project-state/ 目录存在。"""
    return (root / "project-state").is_dir()


# ---------------------------------------------------------------- 配置读取
def load_config(root: Path) -> dict:
    """读 config.json；坏文件/缺文件都当空配置（配置不该阻断复盘）。"""
    p = root / "project-state" / "config.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def retro_cadence(root: Path):
    """返回 (every_n_cards, stale_days)。**缺配置项用默认值，不报错**——很多项目还没有这一项。"""
    cfg = load_config(root)
    rc = cfg.get("retro_cadence")
    every_n, stale = DEFAULT_EVERY_N_CARDS, DEFAULT_STALE_DAYS
    if isinstance(rc, dict):
        e = _num(str(rc.get("every_n_cards", "")))
        s = _num(str(rc.get("stale_days", "")))
        if e is not None and e > 0:
            every_n = int(e)
        if s is not None and s > 0:
            stale = int(s)
    return every_n, stale


# ---------------------------------------------------------------- 迭代口径
def count_closed_cards(root: Path) -> int:
    """当前收口卡数 = project-state/需求卡/card-*/ 的**目录**个数（一卡一目录）。"""
    d = root / "project-state" / "需求卡"
    if not d.is_dir():
        return 0
    return sum(1 for x in d.glob("card-*") if x.is_dir())


def read_ledger(root: Path):
    return _read_csv_dicts(ledger_path(root), LEDGER_FIELDS)


def read_actions(root: Path):
    return _read_csv_dicts(action_path(root), ACTION_FIELDS)


def current_round(root: Path) -> int:
    """当前轮次 = 台账最后一行的轮次 + 1；无台账则 1。"""
    led = read_ledger(root)
    if not led:
        return 1
    return _int(led[-1].get("轮次"), 0) + 1


# ---------------------------------------------------------------- --due
def due_check(root: Path) -> dict:
    """到点判定：**按收口卡数差值**，不按墙钟时间。

    首轮特例：从未复盘过（无台账）且已有卡 → due 为真。否则 due = 当前卡数 - 上次迭代标记 ≥ every_n。
    stale_reminder 只是**提醒**：距上次复盘 > stale_days 且 卡数确有增长（没有新数据的陈旧不是复盘点）。
    """
    every_n, stale_days = retro_cadence(root)
    cur = count_closed_cards(root)
    led = read_ledger(root)
    first = not led
    last_round = _int(led[-1].get("轮次"), 0) if led else 0
    last_iter = _int(led[-1].get("迭代标记"), 0) if led else 0
    last_date = (led[-1].get("日期") or "").strip() if led else ""
    gap = cur - last_iter

    if first and cur > 0:
        due = True
    else:
        due = gap >= every_n
    cards_to_go = 0 if due else max(0, every_n - gap)

    stale_reminder, stale_reason = False, ""
    if first:
        stale_reason = "尚无复盘台账，无『上次复盘日期』可比——首轮由卡数触发，时间不参与判定。"
    else:
        d = _parse_date(last_date)
        if d is None:
            stale_reason = f"上次复盘日期不可解析（{last_date!r}），本项无法判定，不作提醒。"
        else:
            days = (datetime.now() - d).days
            grew = cur > last_iter
            if days > stale_days and grew:
                stale_reminder = True
                stale_reason = (f"距上次复盘 {days} 天 > {stale_days} 天，且本窗口收口卡有增长"
                                f"（{last_iter} → {cur}）——**提醒**：沉淀可能已陈旧，建议尽快复盘。")
            elif days > stale_days and not grew:
                stale_reason = (f"距上次复盘 {days} 天 > {stale_days} 天，但收口卡无增长"
                                f"（{last_iter} → {cur}）——无新数据，**不作提醒**（时间是提醒不是触发器）。")
            else:
                stale_reason = f"距上次复盘 {days} 天，未超 {stale_days} 天阈值。"

    if first and cur > 0:
        why = f"从未复盘且已有 {cur} 张收口卡——**首轮复盘**。"
    elif due:
        why = f"距上次复盘已新增 {gap} 张收口卡，达到密度 {every_n}——到点。"
    else:
        why = f"距上次复盘新增 {gap} 张收口卡，还差 {cards_to_go} 张到点（密度 {every_n}）。"

    return {
        "root": str(root),
        "current_cards": cur,
        "last_round": last_round,
        "last_iteration": last_iter,
        "last_date": last_date,
        "gap": gap,
        "every_n_cards": every_n,
        "stale_days": stale_days,
        "cards_to_go": cards_to_go,
        "due": bool(due),
        "first_retro": bool(first),
        "stale_reminder": bool(stale_reminder),
        "stale_reason": stale_reason,
        "reason": why,
    }


def render_due(d: dict) -> str:
    L = ["# 复盘点判定（--due）——**按收口卡数（迭代口径）触发，不按墙钟时间**", ""]
    L.append(f"- 当前收口卡数：**{d['current_cards']}**（一卡一目录）")
    if d["first_retro"]:
        L.append(f"- 上次迭代标记：**（无台账，从未复盘）**")
    else:
        L.append(f"- 上次迭代标记：**{d['last_iteration']}**（第 {d['last_round']} 轮，日期 {d['last_date'] or '未填'}）")
    L.append(f"- 本轮新增：{d['gap']} 张；密度阈值 every_n_cards={d['every_n_cards']}"
             + (f"；**还差 {d['cards_to_go']} 张到点**" if d["cards_to_go"] else "；**已到点**"))
    L.append(f"- **是否到复盘点（due）：{'是' if d['due'] else '否'}** —— {d['reason']}")
    L.append(f"- 陈旧提醒（stale_days={d['stale_days']}，**只是提醒不是触发器**）："
             f"**{'是' if d['stale_reminder'] else '否'}** —— {d['stale_reason']}")
    L += ["", "---", "",
          "- 判据：`due = 当前收口卡数 − 上次迭代标记 ≥ every_n_cards`；从未复盘且有卡 → 首轮 due 为真。",
          "- 时间不参与 due 判定：闲置两周的零新数据复盘、三天 40 张卡的漂移窗口，都不是时间能表达的。",
          "- 配置来自 project-state/config.json 的 `retro_cadence`；缺项用默认值 20/30。"]
    return "\n".join(L)


# ---------------------------------------------------------------- --assemble
def _price_tokens(model, prompt_tokens, completion_tokens, cost_table):
    """按 config.json cost_table 计价；无表/无该模型返回 None（**不猜价**）。"""
    if not cost_table or not model:
        return None
    price = cost_table.get(model) or {}
    pp, pc = _num(str(price.get("prompt", ""))), _num(str(price.get("completion", "")))
    if pp is None and pc is None:
        return None
    return (prompt_tokens * (pp or 0.0) + completion_tokens * (pc or 0.0)) / 1000.0


def assemble(root: Path) -> dict:
    """汇总各角色的**量化输入**（只汇总事实，不做角色judgment——判断留给主控）。

    三个结构化来源，缺哪个就明确写"未查过"，不许把"没有数据"当"没有异常"。
    """
    est_p = root.joinpath(*EST_LEDGER_REL)
    burn_p = root.joinpath(*BURN_LEDGER_REL)
    prog_d = root.joinpath(*PROGRESS_DIR_REL)
    cfg = load_config(root)
    cost_table = cfg.get("cost_table") or {}

    sources = {
        "进度/估算实测.csv": {"path": str(est_p), "exists": est_p.exists()},
        "进度/成本流水.csv": {"path": str(burn_p), "exists": burn_p.exists()},
        "进度/<角色>.json": {"path": str(prog_d), "exists": prog_d.is_dir()},
    }
    missing = [k for k, v in sources.items() if not v["exists"]]

    # 1) 估算实测：按 角色（旧账本叫「类型」）分组 → 样本数 / 偏差中位数（实时/估时） / 偏差>1.5 卡数
    #    真 11 列是**小时制**（估算M(小时)/实际(小时)），旧 6 列是分钟制（估时分钟/实时分钟）：
    #    这里统一折算成分钟再比（比值与单位无关，但**绝不能把小时当分钟读**）。
    est_by_type, est_rows = {}, 0
    if est_p.exists():
        for rec in _read_csv_raw(est_p)[1]:
            est_rows += 1
            t = (_pick(rec, "角色", "类型") or "").strip() or "（未填）"
            em, am = _num(_pick(rec, "估时分钟")), _num(_pick(rec, "实时分钟"))
            if em is None:
                m_h = _num(_pick(rec, "估算M(小时)"))
                em = None if m_h is None else m_h * 60.0
            if am is None:
                a_h = _num(_pick(rec, "实际(小时)"))
                am = None if a_h is None else a_h * 60.0
            slot = est_by_type.setdefault(t, {"n": 0, "ratios": [], "over_1_5": 0, "cards": []})
            slot["n"] += 1
            slot["cards"].append(_pick(rec, "卡号") or "")
            if em and am and em > 0 and am > 0:
                r = am / em
                slot["ratios"].append(r)
                if r > 1.5:
                    slot["over_1_5"] += 1
    estimate = {}
    for t, s in sorted(est_by_type.items()):
        estimate[t] = {
            "n": s["n"],
            "median_ratio": round(statistics.median(s["ratios"]), 3) if s["ratios"] else None,
            "over_1_5": s["over_1_5"],
            "cards": s["cards"],
        }

    # 2) 成本流水：按 角色 分组 → 轮次数 / token 合计 / 成本合计
    #    两套口径都认（按表头名定位）：
    #    - 真 10 列：`tokens_in/tokens_out` 是**逐行**读数，`估算成本` 是**当轮**成本 → 逐行相加；
    #    - 旧 10 列：`prompt_tokens/completion_tokens` 逐行，`累计成本` 是**累计值** →
    #      直接按角色求和会重复计，故逐行差分还原每行成本。
    #    账本里那一列没有数（真口径 `未查过` / 旧口径缺列）时，退回 cost_table 逐行计价；
    #    两者都不可得就留空——**留空 = 不猜价**。
    burn_present = burn_p.exists()
    burn_header, burn_rows = _read_csv_raw(burn_p) if burn_present else ([], [])
    burn_real = ("估算成本" in burn_header) or ("tokens_in" in burn_header)
    burn_by_role, burn_bad = {}, 0
    for rec in burn_rows:
        role = (_pick(rec, "角色") or "").strip() or "（未填）"
        slot = burn_by_role.setdefault(role, {"rounds": 0, "tokens": 0.0,
                                              "cost": 0.0, "cost_known": False})
        slot["rounds"] += 1
        pt = _num(_pick(rec, "prompt_tokens", "tokens_in"))
        ct = _num(_pick(rec, "completion_tokens", "tokens_out"))
        if pt is None or ct is None:
            burn_bad += 1
            pt, ct = (pt or 0.0), (ct or 0.0)
        slot["tokens"] += pt + ct

    # 成本口径定案（V-02）：**`未查过` 是正式记法**（"这项没查过"），任何汇总都不得据此反算成本；
    # `cost_table` 只在账本**完全没有成本列**时兜底，且结果标 `cost_source="cost_table"`（估算值≠记录值）。
    cost_col = "估算成本" if "估算成本" in burn_header else ("累计成本" if "累计成本" in burn_header else "")
    cost_col_present = bool(cost_col)
    cost_priced_rows = 0
    cost_from_ledger = False
    cum_present = (not burn_real) and any(_num(r.get("累计成本")) is not None for r in burn_rows)
    if cum_present:
        prev = None
        for rec in burn_rows:
            role = (_pick(rec, "角色") or "").strip() or "（未填）"
            cum = _num(rec.get("累计成本"))
            if cum is None:
                continue                      # 该行没成本：不更新 prev，差额归给下一条有值的行
            delta = cum if prev is None else (cum - prev)
            burn_by_role[role]["cost"] += delta
            burn_by_role[role]["cost_known"] = True
            cost_from_ledger = True
            prev = cum
    else:
        for rec in burn_rows:
            role = (_pick(rec, "角色") or "").strip() or "（未填）"
            c = _num(_pick(rec, "估算成本")) if burn_real else None
            from_table = False
            if c is None and not cost_col_present and cost_table:
                pt = _num(_pick(rec, "prompt_tokens", "tokens_in"))
                ct = _num(_pick(rec, "completion_tokens", "tokens_out"))
                c = _price_tokens((_pick(rec, "模型") or "").strip(),
                                  pt or 0.0, ct or 0.0, cost_table)
                if c is not None:
                    cost_priced_rows += 1
                    from_table = True
            if c is not None:
                burn_by_role[role]["cost"] += c
                burn_by_role[role]["cost_known"] = True
                if not from_table:
                    cost_from_ledger = True

    cost_blank_rows = 0
    if cost_col_present:
        for rec in burn_rows:
            if _num(rec.get(cost_col)) is None:
                cost_blank_rows += 1

    # 3) 角色进度摘要 json：只报存在/不存在（**不解析语义**——语义判断留给主控）
    roles = set(burn_by_role.keys())
    if prog_d.is_dir():
        for f in prog_d.glob("*.json"):
            if f.stem != "overall":
                roles.add(f.stem)
    role_rows = []
    for r in sorted(roles):
        jp = prog_d / f"{r}.json"
        exists = jp.exists()
        role_rows.append({
            "role": r,
            "burn_rounds": burn_by_role.get(r, {}).get("rounds", 0),
            "tokens": round(burn_by_role.get(r, {}).get("tokens", 0.0)),
            "cost": (round(burn_by_role[r]["cost"], 6)
                     if burn_by_role.get(r, {}).get("cost_known") else None),
            "progress_json": str(jp),
            "progress_json_exists": exists,
            "progress_json_updated": (datetime.fromtimestamp(jp.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                                      if exists else ""),
        })

    notes = []
    if missing:
        notes.append("缺失来源（**未查过，不等于查过且干净**）：" + "、".join(missing)
                     + "——这些维度的「没有问题」是「没人查过」，不要读成「没有异常」。")
    if burn_present and burn_bad:
        notes.append(f"成本流水有 {burn_bad} 行 token 列不可解析（已按下限 0 计 token，成本另判）。")
    cost_any = any(v["cost_known"] for v in burn_by_role.values())
    if burn_present and cost_priced_rows:
        notes.append(f"成本流水**没有成本列**：{cost_priced_rows} 行按 config.json 的 `cost_table` 兜底计价"
                     "（`cost_source=cost_table`）——**这是估算值，不是记录值**。")
    if burn_present and cost_blank_rows and not cost_priced_rows:
        notes.append(f"成本流水有 {cost_blank_rows} 行成本记 `未查过`/空：**不因 config 里有 `cost_table` 就反算**"
                     "（`未查过` 是正式记法，与 `task_estimate --burn-status` 同口径）；"
                     "要补成本请由记录方走 `--spend` 写当轮值。")
    if burn_present and not cost_any and not cost_table:
        notes.append("成本流水无可用成本列（真口径 `未查过`／旧口径缺累计列），"
                     "且 config.json 无 cost_table——**成本留空不猜价**。")
    est_never = "进度/估算实测.csv" in missing
    if est_never:
        notes.append("估算实测从未查过：本页估算偏差一行不写"
                     "——**没有偏差数据 ≠ 估得准**。")
    if not roles:
        notes.append("无任何角色数据来源（成本流水为空且无角色 json）——本页无角色可汇总。")

    return {
        "root": str(root),
        "sources": sources,
        "missing_sources": missing,
        "estimate_rows": est_rows,
        "estimate_by_type": estimate,
        "burn_by_role": {k: {"rounds": v["rounds"], "tokens": round(v["tokens"]),
                             "cost": round(v["cost"], 6) if v["cost_known"] else None}
                         for k, v in sorted(burn_by_role.items())},
        "burn_bad_rows": burn_bad,
        "cost_source": ("ledger" if cost_from_ledger
                        else ("cost_table" if cost_priced_rows else None)),
        "roles": role_rows,
        "notes": notes,
        "cadence": dict(zip(("every_n_cards", "stale_days"), retro_cadence(root))),
    }


def render_assemble(a: dict) -> str:
    L = ["# 复盘输入汇总（--assemble）——**只汇总事实，不做角色judgment**", ""]
    L.append("判断（谁该改职责、流程哪里该调）留给主控；本页只保证「数据摆全、缺的说缺」。")
    L += ["", "## 来源可用性（**未查过 ≠ 查过且干净**）", "", "| 来源 | 路径 | 状态 |", "|---|---|---|"]
    for k, v in a["sources"].items():
        L.append(f"| {k} | {v['path']} | {'已查' if v['exists'] else '**未查过（文件/目录不存在）**'} |")

    L += ["", "## 估算实测：按类型（偏差 = 实时/估时）", ""]
    if "进度/估算实测.csv" in a["missing_sources"]:
        L.append("> **未查过**：`进度/估算实测.csv` 不存在。这一栏的空白是「没人查过」，"
                 "**不是「估得准」**。")
    elif not a["estimate_by_type"]:
        L.append("台账存在但无可用数据行。")
    else:
        L += ["| 类型 | 样本数 | 偏差中位数（实时/估时） | 偏差>1.5 卡数 | 卡号 |", "|---|---|---|---|---|"]
        for t, v in a["estimate_by_type"].items():
            med = "-" if v["median_ratio"] is None else v["median_ratio"]
            L.append(f"| {t} | {v['n']} | {med} | {v['over_1_5']} | {', '.join(x for x in v['cards'] if x)} |")

    L += ["", "## 成本流水：按角色（token / 成本）", ""]
    if "进度/成本流水.csv" in a["missing_sources"]:
        L.append("> **未查过**：`进度/成本流水.csv` 不存在——本栏无事实可汇总。")
    elif not a["burn_by_role"]:
        L.append("流水存在但无数据行。")
    else:
        L += ["| 角色 | 轮次数 | token 合计 | 成本合计 |", "|---|---|---|---|"]
        for r, v in a["burn_by_role"].items():
            cost = "（留空，不猜价）" if v["cost"] is None else f"{v['cost']:.6f}"
            L.append(f"| {r} | {v['rounds']} | {v['tokens']} | {cost} |")

    L += ["", "## 各角色一行（成本 + 进度摘要 json 存在性）", "", "| 角色 | 轮次 | token | 成本 | 进度摘要 json | 最近更新 |",
          "|---|---|---|---|---|---|"]
    if not a["roles"]:
        L.append("| （无角色数据） | - | - | - | - | - |")
    for r in a["roles"]:
        cost = "留空" if r["cost"] is None else f"{r['cost']:.6f}"
        jj = "存在" if r["progress_json_exists"] else "**不存在**"
        L.append(f"| {r['role']} | {r['burn_rounds']} | {r['tokens']} | {cost} | {jj} | "
                 f"{r['progress_json_updated'] or '-'} |")

    if a["notes"]:
        L.append("")
        for n in a["notes"]:
            L.append(f"> {n}")
    L += ["", "---", "",
          "- 本模式**只汇总事实**：存在性、样本数、偏差中位数、token/成本合计；不评判角色优劣，",
          "  不下「该调整谁」的结论——那一步是主控与用户的判断。",
          "- 缺失来源一律写「未查过」，与「查过且干净」严格区分（框架硬纪律）。"]
    return "\n".join(L)


# ---------------------------------------------------------------- --actions（机制 K）
def _verify_evidence(root: Path, evidence: str):
    """核验 核验证据 列（支持 `;` 分隔多条、`路径::关键字` 断言）。返回 (verdict, 理由)。

    **空证据列一律 unverified**：没写核验证据 ≠ 已落地。这是本脚本最硬的一条守卫。
    """
    raw = (evidence or "").strip()
    if not raw:
        return "unverified", "核验证据列为空——**没写核验证据 ≠ 已落地**"
    fails = []
    for part in raw.split(";"):
        item = part.strip()
        if not item:
            continue
        if "::" in item:
            path_str, _, keyword = item.partition("::")
            path_str, keyword = path_str.strip(), keyword.strip()
        else:
            path_str, keyword = item, ""
        p = Path(path_str)
        full = p if p.is_absolute() else (root / path_str)
        if not full.exists():
            fails.append(f"证据路径不存在：{path_str}")
            continue
        if keyword:
            try:
                text = full.read_text(encoding="utf-8", errors="replace")
            except OSError:
                fails.append(f"证据文件读不出：{path_str}")
                continue
            if keyword not in text:
                fails.append(f"证据在但关键内容不在：{path_str} 不含「{keyword}」")
    if fails:
        return "evidence-missing", "；".join(fails)
    return "landed", f"证据齐备：{raw}"


def actions_check(root: Path, write_back: bool = True) -> dict:
    """逐行核验行动项，把**两列正交信息**写回 行动项.csv：`落地情况`（证据判定）+ `逾期`（到点未收口）。

    判据见模块 docstring：落地情况 ∈ dropped/landed/unverified/evidence-missing；
    逾期 = 是 ⟺ 状态=open 且 核验轮次 ≤ 当前轮次（**与证据判定无关**）。
    存在任一 unverified/evidence-missing **或 逾期=是** → 调用方据此返回退出码 1。
    """
    rows = read_actions(root)
    cur_round = current_round(root)
    counts = {s: 0 for s in LANDED_STATES}   # 证据判定分档
    overdue_n = 0
    items = []
    changed = False

    for r in rows:
        status = (r.get("状态") or "").strip() or "open"
        verify_round = _num(r.get("核验轮次"))

        # ---- 列 1：落地情况 = 只表达证据判定 ----
        if status == "dropped":
            verdict, reason = "dropped", "主动放弃（**不计未落地**，与「忘了做」区分开）"
        else:
            verdict, reason = _verify_evidence(root, r.get("核验证据", ""))
        counts[verdict] += 1

        # ---- 列 2：逾期 = 到点还没人在核收（正交，与证据判定无关）----
        overdue = status == "open" and verify_round is not None and verify_round <= cur_round
        overdue_cell = "是" if overdue else ""
        overdue_reason = (
            f"状态 open 且核验轮次 {int(verify_round)} 已到（当前轮次 {cur_round}）——"
            f"**已到核验时点却无人在核收**（与是否落地无关）") if overdue else ""
        if overdue:
            overdue_n += 1

        if (r.get("落地情况") or "").strip() != verdict:
            r["落地情况"] = verdict
            changed = True
        if (r.get("逾期") or "").strip() != overdue_cell:
            r["逾期"] = overdue_cell
            changed = True
        items.append({
            "行动项ID": r.get("行动项ID", ""),
            "行动项": r.get("行动项", ""),
            "责任人": r.get("责任人", ""),
            "类别": r.get("类别", ""),
            "来源轮次": r.get("来源轮次", ""),
            "核验轮次": r.get("核验轮次", ""),
            "状态": status,
            "逾期": overdue_cell,
            "落地情况": verdict,
            "理由": reason,
            "逾期理由": overdue_reason,
        })

    written = ""
    if write_back and rows:
        _write_csv_dicts(action_path(root), ACTION_FIELDS, rows)
        written = str(action_path(root))

    # 「已落地但逾期未收口」是最容易被忽略的一类：真落地了，却没人按时核收。
    landed_but_overdue = [it["行动项ID"] for it in items
                          if it["落地情况"] == "landed" and it["逾期"] == "是"]
    fail_count = sum(counts[s] for s in FAIL_STATES) + overdue_n
    return {
        "root": str(root),
        "current_round": cur_round,
        "ledger_exists": bool(read_ledger(root)),
        "actions_exists": action_path(root).exists(),
        "counts": counts,
        "overdue_count": overdue_n,
        "landed_but_overdue": landed_but_overdue,
        "fail_count": fail_count,
        "items": items,
        "written": written,
        "changed": changed,
    }


def render_actions(a: dict) -> str:
    L = ["# 行动项核落地（--actions，机制 K）——**提了就完事是最常见的失效方式**", ""]
    if not a["actions_exists"]:
        L.append(f"- 行动项.csv 不存在（{action_path(Path(a['root']))}）——尚无行动项可核"
                 f"（**首轮复盘前本就没有**）。")
    L.append(f"- 当前轮次：{a['current_round']}（台账最后轮次 + 1；无台账为 1）")
    c = a["counts"]
    L.append(f"- 落地情况（**只表达证据判定**）：landed {c['landed']} ／ evidence-missing "
             f"{c['evidence-missing']} ／ unverified {c['unverified']} ／ dropped {c['dropped']}")
    L.append(f"- 逾期（**正交信息：状态=open 且核验轮次已到**）：**{a['overdue_count']}** 条")
    L.append(f"- **问题合计：{a['fail_count']}**（= unverified + evidence-missing + 逾期，两类不计："
             f"landed / dropped）" + ("；**存在即退出码 1**。" if a["fail_count"] else "。"))
    if a["items"]:
        L += ["", "| 行动项ID | 行动项 | 责任人 | 核验轮次 | 逾期 | 落地情况 | 证据判定理由 |",
              "|---|---|---|---|---|---|---|"]
        for it in a["items"]:
            def cell(x):
                return str(x or "").replace("|", "/")
            mark = f"**{cell(it['逾期'])}**" if it["逾期"] else ""
            L.append(f"| {cell(it['行动项ID'])} | {cell(it['行动项'])} | {cell(it['责任人'])} | "
                     f"{cell(it['核验轮次'])} | {mark} | **{cell(it['落地情况'])}** | {cell(it['理由'])} |")
    if a["landed_but_overdue"]:
        L += ["", f"## 已落地但逾期未收口（{len(a['landed_but_overdue'])} 条）——**最容易被忽略的一类**", ""]
        L.append("这些项**证据齐备、确实落地了**，但到了核验轮次还挂在 open、无人在复核时点核收。"
                 "落地率不该被它们污染，但「没人在核收」这件事必须被看见：")
        L.append("")
        for aid in a["landed_but_overdue"]:
            L.append(f"- {aid}（落地情况 landed + 逾期 是）")
    L += ["", "---", "",
          "- 硬纪律：**没写核验证据 ≠ 已落地**（证据列为空一律 unverified）。",
          "- 硬纪律：**未查过 ≠ 查过且干净**（证据路径不存在 = evidence-missing，不是「通过」）。",
          "- 硬纪律：**没人确认 ≠ 默默通过**（落地情况 与 逾期 两列正交：`landed`+`逾期=是` 也算问题，退出码 1）。",
          "- `dropped` 是**主动放弃**，不计问题，但仍列出——让读者看得到它与「忘了做」的区别。",
          "- `落地情况` 与 `逾期` 两列均已写回 行动项.csv（保持其余列不动）。"]
    return "\n".join(L)


# ---------------------------------------------------------------- --record-action
def record_action(root: Path, category: str, text: str, owner: str,
                  expect: str = "", evidence: str = "", verify_round="", source_round=None) -> dict:
    """追加一条行动项。ID 形如 act-001，按现有最大值 +1；台账/文件不存在则懒创建并写表头。"""
    rows = read_actions(root)
    max_n = 0
    for r in rows:
        m = re.match(r"^act-(\d+)$", (r.get("行动项ID") or "").strip())
        if m:
            max_n = max(max_n, int(m.group(1)))
    aid = f"act-{max_n + 1:03d}"
    if source_round is None:
        led = read_ledger(root)
        source_round = _int(led[-1].get("轮次"), 0) if led else 1
        if source_round < 1:
            source_round = 1
    row = {
        "行动项ID": aid, "来源轮次": str(source_round), "提出日期": _today(),
        "类别": category, "行动项": text, "责任人": owner,
        "预期效果": expect or "", "核验证据": evidence or "",
        "核验轮次": "" if verify_round in (None, "") else str(verify_round),
        "状态": "open", "逾期": "", "落地情况": "",
    }
    rows.append(row)
    _write_csv_dicts(action_path(root), ACTION_FIELDS, rows)
    return {"ok": True, "action_id": aid, "path": str(action_path(root)), "row": row}


# ---------------------------------------------------------------- --close
def close_retro(root: Path, conclusion=None, milestone=None) -> dict:
    """收口本轮复盘：写台账一行。

    **幂等判据：台账最后一行 `日期` == 今天 → 原地更新那一行**（不追加、不产生重复轮次）；
    否则追加 `轮次 = 上一行轮次 + 1`。
    `conclusion`/`milestone` 传 `None`（未传参）时**保留旧值**，两参数对称——避免重复收口时
    一次没带 `--结论` 就把上一版结论清空。
    `已落地数` = **证据判定为 `landed` 的条数，与 `逾期` 无关**（逾期不污染落地率）。
    """
    led = read_ledger(root)
    today = _today()
    cur_cards = count_closed_cards(root)
    actions = read_actions(root)
    total = len(actions)
    landed = sum(1 for r in actions if (r.get("落地情况") or "").strip() == "landed")

    if led and (led[-1].get("日期") or "").strip() == today:
        row = led[-1]
        row["迭代标记"] = str(cur_cards)
        row["里程碑"] = row.get("里程碑", "") if milestone is None else milestone
        row["结论摘要"] = row.get("结论摘要", "") if conclusion is None else conclusion
        row["行动项数"] = str(total)
        row["已落地数"] = str(landed)
        updated = True
    else:
        last_round = _int(led[-1].get("轮次"), 0) if led else 0
        row = {"轮次": str(last_round + 1), "日期": today, "迭代标记": str(cur_cards),
               "里程碑": milestone or "", "结论摘要": conclusion or "",
               "行动项数": str(total), "已落地数": str(landed)}
        led.append(row)
        updated = False
    _write_csv_dicts(ledger_path(root), LEDGER_FIELDS, led)
    return {"ok": True, "round": _int(row.get("轮次"), 0), "date": row.get("日期", ""),
            "iteration_mark": _int(row.get("迭代标记"), 0), "milestone": row.get("里程碑", ""),
            "conclusion": row.get("结论摘要", ""), "action_total": total, "landed_total": landed,
            "updated_existing": updated, "path": str(ledger_path(root))}


def render_close(res: dict) -> str:
    verb = "更新当天已有轮次" if res["updated_existing"] else "新开一轮"
    return "\n".join([
        "# 复盘收口（--close）",
        "",
        f"- {verb}：**第 {res['round']} 轮**（日期 {res['date']}）",
        f"- 迭代标记（本轮收口卡数）：**{res['iteration_mark']}**",
        f"- 里程碑：{res['milestone'] or '（未填）'}",
        f"- 结论摘要：{res['conclusion'] or '（未填）'}",
        f"- 行动项数 {res['action_total']} / 已落地数 {res['landed_total']}",
        "",
        "---",
        "",
        "- 幂等判据：台账最后一行日期 == 今天则**原地更新**，不产生重复轮次。",
        f"- 台账：{res['path']}",
    ])


# ---------------------------------------------------------------- CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="复盘与角色职责校准（按收口卡数触发；核验上一轮行动项是否真落地）")
    ap.add_argument("root_pos", nargs="?", help="项目根（位置参数）")
    ap.add_argument("--due", action="store_true", help="是否到复盘点（按收口卡数，不按时间）")
    ap.add_argument("--assemble", action="store_true", help="汇总各角色量化输入（只汇总事实）")
    ap.add_argument("--actions", action="store_true", help="核验行动项落地情况并写回（机制 K）")
    ap.add_argument("--record-action", action="store_true", help="追加一条行动项")
    ap.add_argument("--close", action="store_true", help="收口本轮复盘（写台账）")
    ap.add_argument("--类别", dest="category", help="--record-action：类别（职责调整/流程优化/工具改进/文档补齐/其他）")
    ap.add_argument("--行动项", dest="action_text", help="--record-action：行动项正文")
    ap.add_argument("--责任人", dest="owner", help="--record-action：责任人")
    ap.add_argument("--预期效果", dest="expect", default="", help="--record-action：预期效果")
    ap.add_argument("--核验证据", dest="evidence", default="", help="--record-action：核验证据（多条用 ; 分隔，支持 路径::关键字）")
    ap.add_argument("--核验轮次", dest="verify_round", default=None, help="--record-action：核验轮次（到此轮仍未落地即 overdue）")
    ap.add_argument("--来源轮次", dest="source_round", default=None, help="--record-action：来源轮次")
    ap.add_argument("--结论", dest="conclusion", default=None, help="--close：结论摘要（未传=保留旧值）")
    ap.add_argument("--milestone", dest="milestone", default=None, help="--close：里程碑（未传=保留旧值）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args(argv)

    root = Path(args.root_pos or ".").resolve()

    try:
        if args.record_action:
            missing = [n for n, v in (("--类别", args.category), ("--行动项", args.action_text),
                                      ("--责任人", args.owner)) if not v]
            if missing:
                print(f"错误：--record-action 缺少 {', '.join(missing)}", file=sys.stderr)
                return 2
            if not require_workspace(root):
                print(f"错误：{root} 不是框架工作区（缺 project-state/）", file=sys.stderr)
                return 2
            if args.category not in ACTION_CATEGORIES:
                print(f"错误：--类别 取值非法 {args.category!r}；允许：{'/'.join(ACTION_CATEGORIES)}",
                      file=sys.stderr)
                return 2
            res = record_action(root, args.category, args.action_text, args.owner,
                                args.expect, args.evidence, args.verify_round, args.source_round)
            if args.json:
                print(json.dumps(res, ensure_ascii=False, indent=2))
            else:
                print(f"已登记行动项 {res['action_id']}（{args.category}，责任人 {args.owner}）→ {res['path']}")
            return 0

        # 其余模式都要求是框架工作区
        if not require_workspace(root):
            print(f"错误：{root} 不是框架工作区（缺 project-state/）", file=sys.stderr)
            return 2

        if args.due:
            d = due_check(root)
            print(json.dumps(d, ensure_ascii=False, indent=2) if args.json else render_due(d))
            return 0

        if args.assemble:
            a = assemble(root)
            print(json.dumps(a, ensure_ascii=False, indent=2) if args.json else render_assemble(a))
            return 0

        if args.actions:
            a = actions_check(root)
            print(json.dumps(a, ensure_ascii=False, indent=2) if args.json else render_actions(a))
            return 1 if a["fail_count"] else 0

        if args.close:
            res = close_retro(root, args.conclusion, args.milestone)
            print(json.dumps(res, ensure_ascii=False, indent=2) if args.json else render_close(res))
            return 0

        ap.print_help()
        return 0
    except OSError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    _reconfigure()
    sys.exit(main())

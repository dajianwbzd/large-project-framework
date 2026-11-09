#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""recall.py — 再调用摘要（冷启动唯一读入入口，v5.6.0）

为什么有这个脚本
----------------
真实工程的实测：一个 102 张需求卡的项目，回答"现在什么状态"所需的**有效状态**（卡
frontmatter）只有约 2.8 万 token，却被埋在约 **185 万 token** 的状态面里（`运行日志.md`
7158 行 + `进度/*.json` 1.0MB + 18 份**批次现场对账表** 636KB + 104 份卡全文 988KB）。
实测比例 **1 : 66**。于是每次"开发完成后再调用"，主控都要**现场重新对账**：实测该项目 17 个
主会话共 6042 次工具调用，其中 Bash 3584 次、累计 18.9 小时；冷启动段（前 3 个 turn）吃掉
总输入的 38.2%。而工作区在网络盘上，**单文件读约 100ms、单次目录遍历 10–17 秒**——
"多写几份对账表"这条路必然越走越贵（对账表本身变成下一轮的"来源"，正反馈闭环）。

本脚本把这件事变成**一条命令的确定性输出**：单次遍历、只读必要字节、输出与预算对齐。

权威分层（本脚本的判据来源，不是它自创的）
------------------------------------------
- **卡级状态** 唯一真相源 = `需求卡/*/card.md` 的 frontmatter（`status`/`attempts`/`last_error`）
- **系统级状态** 唯一真相源 = `测试卡/tc-*/覆盖矩阵.md` 与 `发现项.md` 的**机读块**
- **账本**（`进度/overall.json`、`任务清单.md`、`进度/*.json`）= **派生物**：允许存在，
  但对账口径一律**以真相源为准并报差异**，不再手工对账。
  （此前 `skills/项目经理/SKILL.md` 写"overall.json 是项目状态的唯一真相来源"，而
  `agent.md` 写"以卡为准"——两处"唯一真相源"指向不同对象且从未对齐，这正是对账循环的成因。）

机读块格式（纯文本表格，零依赖；`<!-- machine:名字 -->` 围栏）
------------------------------------------------------------
`覆盖矩阵.md`：
    <!-- machine:coverage -->
    dim | result | evidence | finding
    D3  | fail   | 契约/api.yaml | F-04
    D12 | unknown | - | -
    <!-- /machine:coverage -->
  · `result` ∈ pass / fail / partial / not_covered / **unknown**
    — `unknown` = **未回填**（迁移占位或还没判）。它不是"通过"，也不是"失败"，是一条**要露头**的事实。
  · `evidence` = 相对项目根的路径（**会被校验存在性**）｜ `-` ｜ `自述`（报告方自述、不可本机复核）
  · `finding` = `F-01,F-03` 或 `-`

`发现项.md`：
    <!-- machine:findings -->
    id | severity | owner | ref | state | anchor | entry
    F-01 | critical | B | card-031 | fixed | repos/app/src/auth.py:42 | 复跑 test_login_expired
    <!-- /machine:findings -->
  · `owner` ∈ A（归属某张需求卡）/ B（新建修复卡）/ C（暂不改）—— **零无归属**
  · `ref` = `card-NNN` / `tc-NNN` / `cr-NNN` / `忽略决策-*.md` / `-`
  · `state` ∈ open / fixed / accepted
  · `anchor` = **现象在代码里的位置**：`路径` / `路径:行` / `路径#符号`，「未知」也可
  · `entry` = **怎么复现**：一条可复跑的命令 / 触发路径 / 最小用例
    — 二者**不得同时为空**（`anchor` 可以是「未知」，那时 `entry` 必填）。
    — **只有 `anchor` 的路径参与校验**：行号与符号会随无关编辑漂移，不作判据
      （依据见 `01_调研.md` 第五节：aider 的 `line` 只用于渲染，不作索引身份）。
    — 五列块（v5.7.0 之前的格式）= **格式旧**，报一次整块级 `TC-FINDINGS-FORMAT-STALE`
      并**跳过**上面两条锚点检查（旧行内容一条不缺，只是格式老；这两件事必须分开说）。

裁剪纪律（**本脚本最不能被改坏的一条**）
--------------------------------------
超预算时只裁**明细**，**永不裁计数**：每节的标题与计数恒在，明细截断时显式写
`（明细截断，共 N 条，--full 看全）`。把"我没输出"渲染成"没有"，正是本框架踩过三次的
"空与缺混为一谈"。

用法与退出码
------------
    python scripts/recall.py <项目根>              # 人类可读摘要
    python scripts/recall.py <项目根> --json       # 机器可读（供其它脚本消费）
    python scripts/recall.py <项目根> --check      # 门禁模式，只报问题
    python scripts/recall.py <项目根> --write      # 同时落 进度/再调用摘要-<日期>.md
    python scripts/recall.py <项目根> --budget 6000 --full

退出码：`0` 干净 ｜ `1` 有问题（账实差异 / 发现项无归属 / 证据缺失 / 覆盖未回填）｜
`2` 无法检查（项目根或 `project-state/` 不存在 / `config.json` 不可识别）

预算口径：`--budget` 以 **token** 计，内部按 `字节数 / 2.2` 折算（与 01_调研 同一口径，
中文为主时的经验比值），输出头会标注这是估算。
"""

import argparse
import datetime
import json
import os
import re
import sys
import time

# ---------------------------------------------------------------- 常量与判据

CARD_STATUSES = ("todo", "in_progress", "done", "reviewing", "blocked")
NON_TERMINAL = ("in_progress", "reviewing", "blocked")


def normalize_status(s):
    """把状态写法规范化：小写、`-`/空格 → `_`、压掉重复下划线。

    **为什么必须有这一步**：真实工程里 `in-progress`（连字符）出现 8 次。它既不在
    终态集合里、也不在非终态集合里 ⇒ 走状态机的代码**看不见这 8 张卡**：
    断点恢复协议扫"非终态卡"时漏掉它们，盘点时它们不落在任何一格。
    这不是格式洁癖，是**卡凭空消失**。所以写法漂移单独报一类，且报 E。
    """
    if not s:
        return ""
    n = s.strip().lower().replace("-", "_").replace(" ", "_")
    return re.sub(r"_+", "_", n)

COVERAGE_RESULTS = ("pass", "fail", "partial", "not_covered", "unknown")
RESULT_ORDER = ("fail", "partial", "not_covered", "unknown", "pass")
RESULT_MARK = {"fail": "✗", "partial": "~", "not_covered": "·", "unknown": "?", "pass": "✓"}

FINDING_OWNERS = ("A", "B", "C")
FINDING_STATES = ("open", "fixed", "accepted")
SEVERITY_ORDER = ("critical", "required", "nit", "fyi")

# 发现项机读块的两代列集（v5.7.0 起 `anchor` / `entry` **追加在尾部**）。
# 为什么必须是"追加在尾部"：旧五列行在前五格上仍然读得对，缺的只是后两格——
# 于是"这份块格式旧"与"这条发现项没写锚点"**可以分开判**。若把新列插在中间，
# 旧行会被整体错位，那时"格式旧"就再也认不出来，只会表现成一堆字段胡言乱语。
FINDING_COLS = ("id", "severity", "owner", "ref", "state", "anchor", "entry")
FINDING_COLS_LEGACY = FINDING_COLS[:5]

# `anchor` 允许"暂时不知道在哪"，但**不许留空**——此时 `entry`（怎么复现）是强制项。
# 这与 SWE-bench 的入库规则同源：定位信息缺失的条目应当**在建卡时被拦下**，
# 而不是带着一个洞进入执行队列（洞里没有任何东西可查）。
ANCHOR_UNKNOWN = ("未知", "?", "？", "无", "-", "—", "待定位")


def split_anchor(anchor):
    """把 `anchor` 拆成待校验的**路径**（`path:行` / `path#符号` → `path`）。

    两条纪律（判据来自 aider 与真实产物，见 01_调研 §五）：
      · **行号与符号不参与校验**——它们每次无关编辑都会漂移，拿它们当判据
        等于把这条检查变成假阳性发生器；只有**路径**是稳定的。
      · Windows 盘符里的 `:` 不是分隔符（`C:/x/y.py` 的冒号在第 2 个字符），
        所以只剥**末尾**的 `:数字` 与 `#符号`。
    """
    a = (anchor or "").strip().strip("`")
    a = re.sub(r"#.+$", "", a)                 # 剥符号
    a = re.sub(r":\d+\s*$", "", a)             # 剥行号（只剥末尾数字）
    return a.strip()


def anchor_is_checkable(anchor):
    """只有"像裸路径"的锚点才做存在性校验。

    锚点允许写「未知」，也允许写成一句人话（`登录流程那一带`）——后者**不许**
    当路径去查，否则每次调用都会稳定地产生同一批噪声（与 `evidence` 列同一处理，
    见 `analyse` 里证据指针那条注释）。
    """
    a = split_anchor(anchor)
    if not a or a in ANCHOR_UNKNOWN:
        return False
    if re.search(r"\s", a):                    # 含空白 ⇒ 是说明，不是裸路径
        return False
    if a.startswith(("http", "见", "同上")):
        return False
    return ("/" in a) or ("\\" in a) or bool(re.search(r"\.[A-Za-z0-9]{1,6}$", a))

# 受管 JSON 字段散文阈值（M4 账本防散文）。超出即报——历史散文应进 `记忆/`（append-only）。
FIELD_BUDGET = {
    "updated": 400,
    "verdict": 400,
    "notes": 400,
    "project": 200,
}
FIELD_BUDGET_DEFAULT = 1200
# 顶层 `*_batchNN` / `*_batchNN_*` 这类"批次备忘录"键——每次批次都不该新增一个顶层键。
BATCH_KEY_RE = re.compile(r"^(.*_)?batch\w*$", re.IGNORECASE)
BATCH_KEY_TOTAL_BUDGET = 4000
# 递归嵌套信号：把"上一轮述（逐字保留）"层层套进一个字段
NESTED_PROSE_RE = re.compile(r"上一轮.*述（逐字保留）")

LOG_TAIL_LINES = 3
LOG_TAIL_READ_BYTES = 8192  # 只从文件尾读这么多字节，不整份读（运行日志可上万行）

TASK_ITEM_RE = re.compile(r"^(\s*)[-*]\s*\[([ xX])\]\s*(.*)$")
TASK_CARD_RE = re.compile(r"card-\d+")
# 卡号写在**任务行本体**上的写法（取证：武侠门派经营游戏的清单实际写作
# `- [x] **T-01**（card-001 / REQ-1.1｜后端开发）…`——不是模板的缩进子行）。
# 只认「左括号紧跟卡号」这一种，不认散文里的裸 `card-NNN`——否则一段说明
# "（见 card-005 的处置）"会被当成任务挂卡，凭空造出差异。
TASK_CARD_INLINE_RE = re.compile(r"[（(]\s*(card-\d+)")


def parse_task_list(path):
    """解析 `任务清单.md` → `{"tasks": [...], "mapped": n, "ticked": n, ...}`。

    为什么不能只数 `- [x]`：模板里 `## 任务清单` 之外还有 `## 完成标准` 一节，同样用
    `- [ ]` 复选框——纯计数会把「完成标准」的勾选算成任务（本脚本首版就是这么数的）。
    所以：
      ① **优先只在含「任务清单」的标题节内解析**；找不到该标题才退化为全文（不静默当空）；
      ② 任务块靠**块内 `卡：card-NNN` 子行**认定——模板要求每项挂卡，
         没有挂卡的复选框不算任务（多为完成标准/装饰项）；
      ③ 一个任务可挂多张卡，全记下来（模板原文：「一个任务可挂多张卡」）。
    """
    if not path or not os.path.exists(path):
        return None
    lines = read_text(path).splitlines()
    body = lines
    for i, ln in enumerate(lines):
        hm = re.match(r"^(#{1,6})\s*(.*)$", ln)
        if hm and "任务清单" in hm.group(2):
            lvl = len(hm.group(1))
            end = len(lines)
            for j in range(i + 1, len(lines)):
                hm2 = re.match(r"^(#{1,6})\s+", lines[j])
                if hm2 and len(hm2.group(1)) <= lvl:
                    end = j
                    break
            body = lines[i + 1:end]
            break
    tasks = []
    cur = None
    for ln in body:
        m = TASK_ITEM_RE.match(ln)
        if m:
            label = m.group(3).strip()
            cur = {"id": (label.split() or ["?"])[0], "ticked": m.group(2).lower() == "x",
                   "cards": TASK_CARD_INLINE_RE.findall(label), "label": label[:60]}
            tasks.append(cur)
            continue
        # 只认**缩进的子行**挂卡（模板为 4 空格缩进）：否则段末一句"（见 卡：card-005）"
        # 会被误挂到上一个任务头上，凭空造出差异。
        if cur is not None and ln[:1] in (" ", "\t") and ln.strip():
            cm = re.search(r"卡[：:]\s*(.+)", ln)      # 例：`    - 卡：card-001、card-002`
            if cm:
                for c in TASK_CARD_RE.findall(cm.group(1)):
                    if c not in cur["cards"]:          # 行本体已写的卡号**不丢**，两种写法并集
                        cur["cards"].append(c)
    mapped = [t for t in tasks if t["cards"]]
    return {"tasks": tasks, "mapped": len(mapped),
            "ticked": sum(1 for t in tasks if t["ticked"]),
            "ticked_mapped": sum(1 for t in mapped if t["ticked"]),
            "unmapped_ticked": sum(1 for t in tasks if t["ticked"] and not t["cards"])}

# 裁剪优先级：数越大越"该保留"。计数与标题不参与裁剪。
TRIM_PRIORITY = {
    "log_tail": 1,
    "evidence": 2,
    "ledger": 3,
    "cards": 4,
    "systemic": 6,
}
TRIM_STEP = 4  # 每次从最低优先级明细各砍这么多行


# ------------------------------------------------------------------ 基础工具

def parse_frontmatter(text):
    """取首个 `---` 围栏内的 `key: value`；剥掉值里的行内注释与包裹引号。

    与 `guide_audit.py:246` 同口径（本框架统一的行式解析，不引 YAML 依赖）。
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    out = {}
    for ln in lines[1:]:
        if ln.strip() == "---":
            break
        if ":" not in ln:
            continue
        key, _, val = ln.partition(":")
        val = re.split(r"\s+#", val, maxsplit=1)[0].strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        out[key.strip()] = val
    return out


def read_head(path, nbytes=4096):
    """只读文件头 nbytes——frontmatter 在最前，不必付整份 I/O 的代价
    （真实工作区在网络盘上，单文件 open 约 100ms，卡全文最大 47KB）。"""
    try:
        with open(path, "rb") as f:
            return f.read(nbytes).decode("utf-8", "replace")
    except OSError:
        return ""


def read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def read_tail(path, nbytes=LOG_TAIL_READ_BYTES):
    """从文件尾读 nbytes 并返回最后若干行（运行日志可达上万行，不整份读）。"""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > nbytes:
                f.seek(size - nbytes)
            raw = f.read()
    except OSError:
        return []
    text = raw.decode("utf-8", "replace")
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    return lines[-LOG_TAIL_LINES:]


def size_of(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def scan_dirs(path, prefix=None):
    """列出目录下的子目录名（按前缀过滤）。Windows 上 is_dir 走目录项缓存，几乎零代价。"""
    out = []
    try:
        with os.scandir(path) as it:
            for e in it:
                try:
                    if not e.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                if prefix and not e.name.startswith(prefix):
                    continue
                out.append(e.name)
    except OSError:
        pass
    out.sort()
    return out


def scan_files(path, suffix=None):
    out = []
    try:
        with os.scandir(path) as it:
            for e in it:
                try:
                    if not e.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                if suffix and not e.name.endswith(suffix):
                    continue
                out.append(e.name)
    except OSError:
        pass
    out.sort()
    return out


def parse_machine_block(text, name):
    """取出 `<!-- machine:name -->` … `<!-- /machine:name -->` 内的纯文本表格。

    返回 `(rows, found, header)`。rows 为 dict 列表；首行是字段名行。

    找不到围栏 → found=False（不是"空表"——这两件事必须分开，见裁剪纪律同族理由）。
    围栏在而表体空 → found=True, rows=[]（"有块但零条"与"没块"同样是两件事）。

    每行额外带一个 `_ncols`（**该行原始格子数**）。这是 v5.7.0 加的：不足的格子会被
    补空（下面的 `cells += [""]`），补空之后**"旧格式少两列"就和"这两列真的没写"
    长得一模一样**——而这两件事的处置完全不同（前者去迁移格式，后者去补内容）。
    保留原始格子数，才分得开。
    """
    open_re = re.compile(r"<!--\s*machine:%s\s*-->" % re.escape(name))
    close_re = re.compile(r"<!--\s*/machine:%s\s*-->" % re.escape(name))
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if open_re.search(ln):
            start = i + 1
            break
    if start is None:
        return [], False, []
    body = []
    for ln in lines[start:]:
        if close_re.search(ln):
            break
        if ln.strip():
            body.append(ln.strip())
    if not body:
        return [], True, []
    header = [c.strip() for c in body[0].split("|")]
    rows = []
    for ln in body[1:]:
        if re.match(r"^[\s|:\-]+$", ln):  # 分隔行
            continue
        cells = [c.strip() for c in ln.split("|")]
        ncols = len(cells)
        if len(cells) < len(header):
            cells += [""] * (len(header) - len(cells))
        d = dict(zip(header, cells))
        d["_ncols"] = ncols
        rows.append(d)
    return rows, True, header


def strip_meta(rows):
    """去掉行上的内部元键（`_ncols`）——它们只服务解析器，不进 `--json` 契约。"""
    return [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]


def est_tokens(text):
    """token 估算（与 01_调研 同口径）：字节数 / 2.2。"""
    return len(text.encode("utf-8")) / 2.2


def evidence_exists(ps, root, target):
    """证据指针是否存在。`ps` = project-state 绝对路径，`root` = 项目根。

    两个基准都试（真实工程两种写法并存），通配只校验其前一层目录。
    """
    t = target.replace("/", os.sep).replace("\\", os.sep)
    if "*" in t:
        t = t.split("*")[0]
        t = os.path.dirname(t) or "."
    bases = [ps] if os.path.normpath(ps) == os.path.normpath(root) else [ps, root]
    for b in bases:
        if os.path.exists(os.path.join(b, t)):
            return True
    return False


def human(n):
    return "{:,}".format(n)


# ---------------------------------------------------------------- 采集（单次遍历）

def collect(root):
    """定点采集 project-state/ 的**状态面**（不做全树遍历）。

    返回 dict：cards / test_cards / ledger / todos / log_tail / config / paths
    """
    ps = os.path.join(root, "project-state")
    req_dir = os.path.join(ps, "需求卡")
    tc_dir = os.path.join(ps, "测试卡")
    data = {
        "root": root,
        "ps": ps,
        "cards": [],        # 需求卡
        "test_cards": [],   # 测试卡
        "todos": [],
        "log_tail": [],
        "config": {},
        "task_list_done": None,
        "overall": None,
        "overall_err": "",
        "overall_bytes": 0,
        "layouts": {},
    }

    data["config"] = load_config(os.path.join(ps, "config.json"))

    # ---- 定点定位，**不做全树遍历**
    # 实测教训：在局域网盘上 os.walk 整棵 project-state（含 102 张卡各自的 证据/、32 个
    # 进度文件、以及根上若干误建目录）要 **83.6s**；而本脚本真正需要的只有 5 个位置。
    # 每次 scandir 在 Windows 上从目录项缓存取 `is_dir`，几乎零代价。
    card_dirs = scan_dirs(req_dir, "card-")
    card_flat = [f for f in scan_files(req_dir, ".md") if f.startswith("card-")]
    tc_dirs = scan_dirs(tc_dir, "tc-")
    todo_files = scan_files(os.path.join(ps, "待办"), ".md")
    data["task_list_path"] = os.path.join(ps, "任务清单.md")

    data["tc_count_dirs"] = len(tc_dirs)

    # ---- 需求卡：**按 id 归并两种合法布局**
    #   · 卡卷宗（v3.17.0 方案B）：`需求卡/card-NNN/card.md`
    #   · 平铺（早期项目）        ：`需求卡/card-NNN.md`
    # 实测两者可**并存且互补**：某真实工程的 70 张卡是平铺，另有 3 个目录只含 `证据/`
    # （card-056 的 frontmatter 在 `card-056.md`，证据在 `card-056/证据/`）。
    # 只认目录 → 把 70 张卡读成 0 张并反过来把 3 张报成"卡文件缺失"（本脚本首版就这样，
    # 在一个 73 卡工程上打印"卡文件缺失 3"并把 done 数算成 0）。⇒ 按 id 归并，不按载体。
    ids = {}
    for name in card_dirs:
        ids.setdefault(name, {})["dir"] = name
    for name in card_flat:
        ids.setdefault(name[:-3], {})["flat"] = name

    for cid in sorted(ids):
        ent = ids[cid]
        d_path = os.path.join(req_dir, cid, "card.md")
        f_path = os.path.join(req_dir, cid + ".md")
        path, layout = "", "缺卡文件"
        if "dir" in ent:
            fm = parse_frontmatter(read_head(d_path))
            if fm:
                path, layout = d_path, "卷宗"
        if not path and "flat" in ent:
            fm = parse_frontmatter(read_head(f_path))
            if fm:
                path, layout = f_path, "平铺"
        if not path:
            fm = {}
        data["layouts"][layout] = data["layouts"].get(layout, 0) + 1
        rel = os.path.relpath(path, root).replace("\\", "/") if path else ""
        data["cards"].append({
            "id": fm.get("id") or cid,
            "dir": cid,
            "path": rel,
            "layout": layout,
            "status": (fm.get("status") or "").strip(),
            "attempts": fm.get("attempts", ""),
            "last_error": fm.get("last_error", ""),
            # 定位锚点（v5.7.0）：`source` 非空 = 修复卡（由测试卡发现项派生）。
            # `has_*` 判的是**键在不在**，`*` 判的是**值空不空**——这两件事必须分开报，
            # 否则"这张卡还没迁移到新模板"会被说成"这张卡没写锚点"（R8 同族）。
            "source": (fm.get("source") or "").strip(),
            "has_source": "source" in fm,
            "anchor": (fm.get("anchor") or "").strip(),
            "entry": (fm.get("entry") or "").strip(),
            "has_anchor_key": "anchor" in fm,
            "has_entry_key": "entry" in fm,
            "has_md": bool(path),
        })
    data["card_count_dirs"] = len(data["cards"])

    # ---- 测试卡：卡头 + 覆盖矩阵机读块 + 发现项机读块
    # 测试卡在框架里是**一卡一目录**（`测试卡/tc-NNN/`）；这里同时容忍平铺
    # `测试卡/tc-NNN.md`——容忍的方式是**报出来**（`flat=True`），不是静默当卷宗读。
    # 平铺卡没有地方放 覆盖矩阵/发现项，若按卷宗处理会同时报"缺覆盖矩阵""缺发现项"
    # 两个假 ERROR——把"布局不对"说成"内容缺失"，又是一次「空与缺混为一谈」。
    tc_flat = [f for f in scan_files(tc_dir, ".md") if f.startswith("tc-")]
    tc_ids_all = {}
    for name in tc_dirs:
        tc_ids_all.setdefault(name, {})["dir"] = name
    for name in tc_flat:
        tc_ids_all.setdefault(name[:-3], {})["flat"] = name

    for d in sorted(tc_ids_all):
        ent = tc_ids_all[d]
        base = os.path.join(tc_dir, d)
        flat_p = os.path.join(tc_dir, d + ".md")
        is_flat = "dir" not in ent
        tc_md = flat_p if is_flat else os.path.join(base, "tc.md")
        fm = parse_frontmatter(read_head(tc_md))
        cov_p = os.path.join(base, "覆盖矩阵.md")
        fin_p = os.path.join(base, "发现项.md")
        cov_text = "" if is_flat else read_text(cov_p)
        fin_text = "" if is_flat else read_text(fin_p)
        cov_rows, cov_found, cov_header = parse_machine_block(cov_text, "coverage")
        fin_rows, fin_found, fin_header = parse_machine_block(fin_text, "findings")
        data["test_cards"].append({
            "id": fm.get("id") or d,
            "dir": d,
            "base": base,
            "flat": is_flat,
            "path": os.path.relpath(tc_md, root).replace("\\", "/"),
            "status": (fm.get("status") or "").strip(),
            "attempts": fm.get("attempts", ""),
            "last_error": fm.get("last_error", ""),
            "coverage": cov_rows,
            "coverage_found": cov_found,
            "coverage_header": cov_header,
            "has_coverage_file": os.path.exists(os.path.join(base, "覆盖矩阵.md")),
            "findings": fin_rows,
            "findings_found": fin_found,
            "findings_header": fin_header,
            "has_findings_file": os.path.exists(os.path.join(base, "发现项.md")),
        })

    # ---- 待办
    for f in todo_files:
        p = os.path.join(ps, "待办", f)
        txt = read_text(p)
        open_items = [
            ln.strip() for ln in txt.splitlines()
            if re.match(r"^\s*[-*]\s*\[ \]", ln)
        ]
        data["todos"].append({
            "file": f, "path": "project-state/待办/%s" % f,
            "open": len(open_items), "sample": open_items[:5], "bytes": size_of(p),
        })

    # ---- 账本（派生物）
    overall = os.path.join(ps, "进度", "overall.json")
    if os.path.exists(overall):
        data["overall_bytes"] = size_of(overall)
        raw = read_text(overall)
        try:
            data["overall"] = json.loads(raw)
        except (ValueError, TypeError) as e:
            data["overall_err"] = "%s" % e

    # ---- 任务清单：**按块解析**（只数 `- [x]` 会把「完成标准」的勾选算成任务，见 parse_task_list）
    tlp = data.get("task_list_path")
    data["task_list"] = parse_task_list(tlp)

    # ---- 运行日志尾部
    logp = os.path.join(ps, "运行日志.md")
    if os.path.exists(logp):
        data["log_tail"] = read_tail(logp)
    return data


def ledger_card_states(ov):
    """从账本里取出"卡 → 状态"映射，返回 `(schema, {id: status})`。

    为什么要有这一层：两个真实工程的 `进度/overall.json` **schema 完全不同**——
      · 示例工作台B：`{"cards": {"card-001": {"status": "done", ...}, ...}, ...}`
      · 武侠门派经营游戏：`{"phase","milestones","open_items","blockers",...}` —— **没有 `cards` 键**
    本脚本首版直接读 `ov["cards"]`，在第二个工程上得到空映射，于是把 62 张卡全报成
    「有卡无账」——**"账本没这个面"被当成了"账本少了 62 条"**。这是同一类错的第三次
    （前两次：`artifacts or []`、`未回填=0`）。判据：**读不到要说"读不到"，不许说"是 0"**。
    """
    if not isinstance(ov, dict):
        return "none", {}
    c = ov.get("cards")
    if isinstance(c, dict):
        out = {}
        for k, v in c.items():
            if isinstance(v, dict):
                out[str(k)] = str(v.get("status", "")).strip()
            elif isinstance(v, str):
                out[str(k)] = v.strip()
        return "cards-map", out
    if isinstance(c, list):
        out = {}
        for it in c:
            if isinstance(it, dict) and it.get("id"):
                out[str(it["id"])] = str(it.get("status", "")).strip()
        return "cards-list", out
    return "unknown", {}


def load_config(path):
    cfg = {}
    if not os.path.exists(path):
        return cfg
    try:
        cfg = json.loads(read_text(path))
    except (ValueError, TypeError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


# ---------------------------------------------------------------- 分析（对账与核查）

def analyse(data, root, allow_unmigrated=False):
    """产出问题清单。每条 = dict(sev, code, msg, path)。sev ∈ E/W。

    `allow_unmigrated=True`（迁移期）：把"缺机读块"从 E 降为 W 并集中到
    `data["unmigrated"]` 一节——新机制上线时**旧项目必然缺机读块**，若一律判 E，
    门禁会在每个存量项目上恒红，那就没有人再看它了（"永远返回 ERROR 的检查器比没有更坏"）。
    但降级只是**不阻断**，不进上下文——它仍然逐条列出，不会被静默吞掉。
    """
    issues = []
    unmigrated = []

    def add(sev, code, msg, path=""):
        issues.append({"sev": sev, "code": code, "msg": msg, "path": path})

    ps = data["ps"]

    # --- 账实对账：卡 ↔ 账本。**账本没有卡状态面时不冒充对账**
    ov = data["overall"]
    schema, ledger_states = ledger_card_states(ov)
    ledger_ids = set(ledger_states)
    data["ledger_schema"] = schema
    data["ledger_ids"] = sorted(ledger_ids)
    data["ledger_open_items"] = None
    data["ledger_blockers"] = None
    if isinstance(ov, dict):
        for key in ("open_items", "findings"):
            v = ov.get(key)
            if isinstance(v, list):
                data["ledger_open_items"] = len(v)
                break
        v = ov.get("blockers")
        if isinstance(v, list):
            data["ledger_blockers"] = len(v)

    dir_ids = set(c["dir"] for c in data["cards"])
    tc_ids = set(c["dir"] for c in data["test_cards"])

    ledger_ok = schema in ("cards-map", "cards-list")
    if not ledger_ok:
        add("W", "LEDGER-SCHEMA-UNKNOWN",
            "`进度/overall.json` **无卡状态面**（schema=%s）⇒ 账实对账**无法进行**"
            "（此项**不是**「账本少了 N 条」）；卡 frontmatter 仍是唯一可用真相源" % schema,
            "project-state/进度/overall.json")

    missing_in_ledger = []
    missing_dirs = []
    missing_tc_dirs = []
    if ledger_ok:
        missing_in_ledger = sorted(i for i in dir_ids if i not in ledger_ids)
        missing_dirs = sorted(i for i in ledger_ids - dir_ids if not i.startswith("tc-"))
        missing_tc_dirs = sorted(i for i in ledger_ids if i.startswith("tc-") and i not in tc_ids)

    data["missing_in_ledger"] = missing_in_ledger
    data["missing_dirs"] = missing_dirs
    data["missing_tc_dirs"] = missing_tc_dirs

    if missing_in_ledger:
        add("E", "LEDGER-MISSING",
            "有卡无账本条目 %d 条（%s%s）"
            % (len(missing_in_ledger), ", ".join(missing_in_ledger[:6]),
               " …" if len(missing_in_ledger) > 6 else ""),
            "project-state/进度/overall.json")
    if missing_dirs:
        add("E", "LEDGER-ORPHAN",
            "有账无卡 %d 条（%s）" % (len(missing_dirs), ", ".join(missing_dirs[:8])),
            "project-state/进度/overall.json")
    if missing_tc_dirs:
        add("E", "LEDGER-ORPHAN-TC",
            "账本里的测试卡无对应目录 %d 条（%s）" % (len(missing_tc_dirs), ", ".join(missing_tc_dirs)),
            "project-state/进度/overall.json")

    # --- 卡 status ↔ 账本 status（以卡为准）
    drift = []
    for c in data["cards"]:
        ls = ledger_states.get(c["dir"])
        if ls is None:
            continue
        if ls and c["status"] and ls != c["status"]:
            drift.append((c["dir"], c["status"], ls))
    data["status_drift"] = drift
    if drift:
        add("E", "STATUS-DRIFT",
            "卡 status 与账本不一致 %d 条（以卡为准）：%s"
            % (len(drift), "; ".join("%s 卡=%s 账=%s" % d for d in drift[:5])),
            "project-state/进度/overall.json")

    # --- 非终态卡（**按规范化后的状态判定**，否则 `in-progress` 这类写法会整卡漏掉）
    nt = []
    variants = []      # 写法漂移（本意是合法值，写错了）→ E
    extensions = {}    # 集合外扩展（有意为之）→ W，需在 config.json 声明
    declared = set()
    if isinstance(data["config"], dict):
        de = data["config"].get("status_extensions")
        if isinstance(de, list):
            declared = set(str(x).strip() for x in de)

    for c in data["cards"]:
        raw = c["status"]
        n = normalize_status(raw)
        c["status_norm"] = n
        if not n:
            c["status_variant"] = False
            c["status_ext"] = False
            continue
        if n in CARD_STATUSES:
            c["status_variant"] = (raw != n)
            c["status_ext"] = False
            if c["status_variant"]:
                variants.append((c["dir"], raw, n))
            if n in NON_TERMINAL:
                nt.append(c)
        else:
            c["status_variant"] = False
            c["status_ext"] = (n not in declared)
            extensions.setdefault(raw, []).append(c["dir"])

    data["status_variants"] = variants
    data["status_extensions"] = extensions
    data["status_declared_ext"] = sorted(declared)
    for cid, raw, n in variants:
        add("E", "STATUS-VARIANT",
            "%s 状态写作 %r，应为 %r——**状态机会看不见这张卡**"
            "（既不算终态也不算非终态，断点恢复会跳过它）" % (cid, raw, n),
            "project-state/需求卡/" + cid)
    for raw, cards_ in sorted(extensions.items()):
        if raw in declared or normalize_status(raw) in declared:
            continue
        add("W", "STATUS-EXTENSION",
            "状态 %r 不在框架 5 值内（%d 张：%s）——如是刻意扩展，"
            "请在 `config.json` 写 `status_extensions: [\"%s\"]`；否则属笔误"
            % (raw, len(cards_), ", ".join(cards_[:6]), raw),
            "project-state/config.json")

    data["non_terminal"] = sorted(nt, key=lambda c: (c["status_norm"] != "blocked", c["dir"]))
    for c in data["non_terminal"]:
        if not c["status"]:
            add("E", "STATUS-EMPTY", "%s 缺 status" % c["dir"], c["path"])

    # --- 修复卡的定位锚点（v5.7.0）
    #
    # 判据对象：`source` 非空的卡（框架约定：修复卡 = 由测试卡发现项派生，`source: tc-NNN#F-xx`）。
    # 为什么是这一类：普通需求卡的"哪里要改"是**新写的**，写卡时未必知道文件；
    # 而修复卡**已经有一个人在某一处看见过这个现象**——那处的坐标在派发时丢掉，才是真浪费。
    #
    # 三态必须分开（R8 同族）：
    #   · **键都不存在** ⇒ 还没迁移到 v5.7.0 模板 ⇒ 只提示（进 §7），不判 E
    #   · **键在、两值皆空** ⇒ 真缺 ⇒ E（`CARD-NO-ANCHOR`）
    #   · **anchor 写了路径但不存在** ⇒ E（`CARD-ANCHOR-DANGLING`）
    # 首版把前两者合成一条，会让**所有旧工程的全部修复卡**一次性变红——那不是检出，
    # 那是噪声，而噪声最终会让人整条忽略这个检查（v5.5.0「假保护」的同族代价）。
    card_no_anchor = []
    card_dangling_anchor = []
    card_unmigrated = []
    for c in data["cards"]:
        if not c["has_md"] or not c["source"]:
            continue
        if not (c["has_anchor_key"] or c["has_entry_key"]):
            card_unmigrated.append(c["dir"])
            continue
        if not c["anchor"] and not c["entry"]:
            card_no_anchor.append(c["dir"])
        elif c["anchor"] and anchor_is_checkable(c["anchor"]):
            if not evidence_exists(data["ps"], root, split_anchor(c["anchor"])):
                card_dangling_anchor.append((c["dir"], c["anchor"]))
    data["card_no_anchor"] = card_no_anchor
    data["card_dangling_anchor"] = card_dangling_anchor
    data["card_anchor_unmigrated"] = card_unmigrated
    for cid in card_no_anchor:
        add("E", "CARD-NO-ANCHOR",
            "%s 是修复卡（`source` 非空）但 `anchor` 与 `entry` 皆空——"
            "现象是在某一处被看见的，把那处坐标和复现方式写进 frontmatter" % cid,
            "project-state/需求卡/" + cid)
    for cid, anc in card_dangling_anchor:
        add("E", "CARD-ANCHOR-DANGLING",
            "%s 的 `anchor` 路径不存在：%s" % (cid, anc),
            "project-state/需求卡/" + cid)
    if card_unmigrated:
        unmigrated.append(
            "修复卡 %d 张未迁移到 v5.7.0 模板（frontmatter 无 `anchor`/`entry` 键，**不是「值为空」**）：%s"
            % (len(card_unmigrated), ", ".join(card_unmigrated[:8])))

    # --- 任务清单 ↔ 卡状态（v5.6.0）
    # 依据是框架自己的约定，不是本脚本发明的：`任务清单.md` 的「标注规则」写「`[x]` 标记…
    # 与卡片 frontmatter `status: done`、`进度/<角色>.json` 验收结论**三方一致**（主控验收
    # 前置检查核对）」，agent.md 验收前置检查第 3 项同文。**故它是派生状态视图，不是"不参与
    # 对账的规划件"**——本脚本首版把它当规划件、并把"二者不等不算差异"写进了文档，与上述两处
    # 相抵，属**同一类错的第四次**：一条对账线被"看着像没用"的经验判断裁掉了。
    # 裁决方向：**卡 frontmatter 是唯一真相源**，视图按它同步（不是反过来），故漂移报 W
    # （不阻断冷启动，但本批收口前要同步掉）。
    # 边界（写明不查，不装查过）：本项只机核**两方**（卡状态 × 任务清单勾选）；
    # 第三方 `进度/测试工程师.json` 的缺陷结论**不机核**——各工程该文件 schema 未统一，
    # 强行解析只会造出假差异（本轮拒绝了三处"看着能查其实查不准"的扩展）。
    tl = data.get("task_list")
    if tl is None:
        data["task_list_state"] = "missing"
    elif not tl["mapped"]:
        # **视图未启用**：一条任务行都没挂到已知卡 ⇒ 三方一致**无从机核**。
        # 这一支必须与"核过了、没差异"分开说——「核不了」不能当「核过了」。
        data["task_list_state"] = "not-enabled"
    elif not tl["ticked_mapped"]:
        # **视图未维护**：有挂卡任务，但**无一条勾选**（实测：某工程 86 条任务行、勾选 0，
        # 卡却 done 一批）。此时不许按卡逐张报"未勾"——那不是 N 处滞后，是**这个视图没在用**；
        # 逐卡报只会每次都产出同一批假差异（首版「规划件」判断的合理内核就是这个，
        # 错在把它写成了通则，且据此把整条对账线裁掉）。
        # 与「空」和「缺」同理：**"0 条勾选"≠"0 处滞后"**。
        data["task_list_state"] = "not-maintained"
    else:
        data["task_list_state"] = "enabled"
        known = {}
        for c in data["cards"]:
            known[c["dir"]] = c["status_norm"]
            if c["id"]:
                known.setdefault(c["id"], c["status_norm"])
        done_cards = [c["dir"] for c in data["cards"] if c["status_norm"] == "done"]
        referenced = set()
        for t in tl["tasks"]:
            referenced.update(t["cards"])
        ticked_cards = set()
        for t in tl["tasks"]:
            if t["ticked"]:
                ticked_cards.update(t["cards"])
        # **「没挂任务行」不等于「任务行未勾」**（实测打脸：某工程 43 条任务全勾、
        # 却有 35 张 done 卡被报成"未勾"——真相是那些卡在 `任务清单.md` 里**根本没有任务行**，
        # 属该视图的覆盖面之外）。故：
        #   stale  = 有任务行挂它、但那些行都没勾   → 真滞后
        #   untracked = 没有任何任务行挂它          → 覆盖面事实，不是滞后
        stale = [d for d in done_cards if d in referenced and d not in ticked_cards]
        untracked = [d for d in done_cards if d not in referenced]
        data["task_list_untracked"] = untracked
        ahead = []
        for t in tl["tasks"]:
            if not t["ticked"] or not t["cards"]:
                continue
            sts = [known[x] for x in t["cards"] if x in known]
            if sts and all(s in ("todo", "in_progress", "blocked", "") for s in sts):
                ahead.append((t["id"], t["cards"]))
        data["task_list_stale"] = stale
        data["task_list_ahead"] = ahead
        if stale:
            add("W", "TASKLIST-STALE",
                "卡已 done、任务行挂着它却没勾 %d 条（%s%s）——`任务清单.md` 是**派生视图**，"
                "按卡状态同步它（**不是**反过来改卡）"
                % (len(stale), ", ".join(stale[:8]), " …" if len(stale) > 8 else ""),
                "project-state/任务清单.md")
        if ahead:
            add("W", "TASKLIST-AHEAD",
                "任务行已勾但关联卡均未 done %d 条（%s）——勾选超前于卡状态"
                % (len(ahead), "; ".join("%s→%s" % (a, ",".join(cs[:3])) for a, cs in ahead[:5])),
                "project-state/任务清单.md")

    # --- 账本散文（M4）
    prose = []
    if isinstance(ov, dict):
        for k, v in ov.items():
            if not isinstance(v, (str,)):
                continue
            limit = FIELD_BUDGET.get(k, FIELD_BUDGET_DEFAULT)
            n = len(v.encode("utf-8"))
            if n > limit:
                nested = len(NESTED_PROSE_RE.findall(v))
                prose.append((k, n, limit, nested))
    data["prose_fields"] = prose
    for k, n, limit, nested in prose:
        add("E", "LEDGER-PROSE",
            "受管 JSON 字段 `%s` 为 %s 字节（阈值 %s）%s"
            % (k, human(n), human(limit),
               "；含 %d 层『上一轮…述（逐字保留）』递归嵌套" % nested if nested else ""),
            "project-state/进度/overall.json")
    if isinstance(ov, dict):
        batch_keys = [k for k in ov if BATCH_KEY_RE.match(k)]
        batch_bytes = sum(len(json.dumps(ov[k], ensure_ascii=False).encode("utf-8")) for k in batch_keys)
        data["batch_keys"] = batch_keys
        data["batch_bytes"] = batch_bytes
        if batch_keys and batch_bytes > BATCH_KEY_TOTAL_BUDGET:
            add("E", "LEDGER-BATCH-BLOAT",
                "顶层『批次备忘录』键 %d 个、合计 %s 字节（阈值 %s）：%s"
                % (len(batch_keys), human(batch_bytes), human(BATCH_KEY_TOTAL_BUDGET),
                   ", ".join(batch_keys[:6])),
                "project-state/进度/overall.json")
    else:
        data.setdefault("batch_keys", [])
        data.setdefault("batch_bytes", 0)

    # --- 测试卡机读块与内容核查
    cov_summary = []
    for tc in data["test_cards"]:
        st = tc["status"]
        if tc.get("flat"):
            add("E", "TC-FLAT-LAYOUT",
                "%s 是**平铺**文件（`测试卡/%s.md`）⇒ 覆盖矩阵与发现项无处安放，"
                "机读块也不存在。请迁到卷宗布局 `测试卡/%s/`" % (tc["dir"], tc["dir"], tc["dir"]),
                tc["path"])
            cov_summary.append({
                "id": tc["dir"], "status": st, "counts": {r: 0 for r in COVERAGE_RESULTS},
                "fail": [], "unknown": [], "total": 0,
                "findings": {s: 0 for s in FINDING_STATES}, "no_owner": 0,
                "no_anchor": 0, "dangling_anchor": 0, "findings_stale": False,
                "open_findings": [],
                "has_coverage": False, "has_findings": False, "flat": True,
                "coverage_rows": [],
            })
            continue
        if not tc["has_coverage_file"]:
            if st and st != "todo":
                add("E", "TC-NO-COVERAGE", "%s 无 覆盖矩阵.md" % tc["dir"], tc["path"])
        elif not tc["coverage_found"]:
            # 不论哪种模式都记进 `unmigrated`：它是**事实**（这几块读不到），
            # `--allow-unmigrated` 只决定它**阻断与否**。第一版只在迁移模式里填这个集合，
            # 于是普通模式的表头永远印「未迁移 0」——而同一份输出里明明列着 4 处缺块。
            # 又是「空」与「缺」混为一谈，只是这次错在**计数标签**上。
            unmigrated.append("%s 覆盖矩阵.md 缺 `machine:coverage` 机读块（覆盖结论对机器不可读）"
                              % tc["dir"])
            if not allow_unmigrated:
                add("E" if st != "todo" else "W", "TC-NO-MACHINE-BLOCK",
                    "%s 覆盖矩阵.md 缺 `machine:coverage` 机读块（非 todo 卡必填）" % tc["dir"],
                    "project-state/测试卡/%s/覆盖矩阵.md" % tc["dir"])
        if not tc["has_findings_file"]:
            if st and st != "todo":
                add("E", "TC-NO-FINDINGS", "%s 无 发现项.md" % tc["dir"], tc["path"])
        elif not tc["findings_found"]:
            unmigrated.append("%s 发现项.md 缺 `machine:findings` 机读块（归属不可机读）"
                              % tc["dir"])
            if not allow_unmigrated:
                add("E" if st != "todo" else "W", "TC-NO-FINDINGS-BLOCK",
                    "%s 发现项.md 缺 `machine:findings` 机读块（非 todo 卡必填）" % tc["dir"],
                    "project-state/测试卡/%s/发现项.md" % tc["dir"])

        # 覆盖维度
        counts = {r: 0 for r in COVERAGE_RESULTS}
        bad = []
        fail_rows = []
        unknown_rows = []
        by_dim = {}
        for row in tc["coverage"]:
            r = (row.get("result") or "").strip()
            dim = (row.get("dim") or "?").strip()
            by_dim[dim] = row
            if r not in COVERAGE_RESULTS:
                bad.append((dim, r))
                continue
            counts[r] += 1
            if r == "fail":
                fail_rows.append(row)
            elif r == "unknown":
                unknown_rows.append(row)
        for dim, r in bad:
            add("E", "TC-RESULT-INVALID",
                "%s 覆盖维度 %s 的 result=%r 非法（合法：%s）"
                % (tc["dir"], dim, r, "/".join(COVERAGE_RESULTS)),
                "project-state/测试卡/%s/覆盖矩阵.md" % tc["dir"])
        if unknown_rows:
            add("W", "TC-RESULT-UNKNOWN",
                "%s 有 %d 个覆盖维度未回填（result=unknown）：%s"
                % (tc["dir"], len(unknown_rows),
                   ", ".join((r.get("dim") or "?") for r in unknown_rows[:6])),
                "project-state/测试卡/%s/覆盖矩阵.md" % tc["dir"])

        # 发现项
        find_counts = {s: 0 for s in FINDING_STATES}
        no_owner = []
        fids = set()

        # --- 先判**格式**，再判**内容**（R8：跨版本读同一份数据时，这两件事必须分开）
        #
        # 5 列块（v5.7.0 之前的格式）经 `parse_machine_block` 补空后，`anchor`/`entry`
        # 会变成空串——**和"这两列真的没写"完全同形**。如果直接跑内容判定，一份"只是
        # 格式老、内容一条不缺"的块会被逐行判成"无锚点"，并且**每个真实工程都会这样**。
        # 所以：格式不对 ⇒ 报**一次整块级** `TC-FINDINGS-FORMAT-STALE`，进 `unmigrated`
        # （迁移期可降级），并**只跳过 anchor/entry 两条检查**。
        #
        # 为什么不整卡跳过：owner/ref/state 三列在旧格式里**本来就存在且本来就在查**，
        # 连带跳过等于为了修一个假阳性而**弄丢三条真检测力**——那比假阳性更坏。
        fin_header = [h for h in (tc.get("findings_header") or []) if h]
        findings_stale = bool(tc["findings_found"] and fin_header
                              and tuple(fin_header) != FINDING_COLS)
        if findings_stale:
            got = " | ".join(fin_header)
            unmigrated.append(
                "%s 发现项.md 的 `machine:findings` 是**旧列集**（`%s`）⇒ 缺 `anchor`/`entry`"
                "两列，**定位信息不可机读**（这是「格式旧」，不是「这两列没写」）" % (tc["dir"], got))
            if not allow_unmigrated:
                add("E" if st != "todo" else "W", "TC-FINDINGS-FORMAT-STALE",
                    "%s 发现项.md 机读块列集为 `%s`，应为 `%s`——"
                    "把两列**追加到行尾**即可（旧行内容不用动）"
                    % (tc["dir"], got, " | ".join(FINDING_COLS)),
                    "project-state/测试卡/%s/发现项.md" % tc["dir"])

        no_anchor = []
        dangling_anchor = []
        for row in tc["findings"]:
            fid = (row.get("id") or "").strip()
            if fid:
                fids.add(fid)
            owner = (row.get("owner") or "").strip()
            state = (row.get("state") or "").strip()
            if state in find_counts:
                find_counts[state] += 1
            elif state:
                add("E", "TC-FINDING-STATE-INVALID",
                    "%s 发现项 %s 的 state=%r 非法（合法：%s）"
                    % (tc["dir"], fid or "?", state, "/".join(FINDING_STATES)),
                    "project-state/测试卡/%s/发现项.md" % tc["dir"])

            # --- 定位锚点（只在列集是新的时判）
            if not findings_stale:
                anc = (row.get("anchor") or "").strip()
                ent = (row.get("entry") or "").strip()
                if not anc and not ent:
                    no_anchor.append(fid or "?")
                else:
                    # 锚点允许写「未知」——那时 `entry` 就是唯一线索，非空即通过。
                    if anc and anchor_is_checkable(anc):
                        if not evidence_exists(data["ps"], root, split_anchor(anc)):
                            dangling_anchor.append((fid or "?", anc))

            if owner not in FINDING_OWNERS:
                no_owner.append((fid or "?", owner))
                continue
            ref = (row.get("ref") or "").strip()
            if owner in ("A", "B") and (not ref or ref == "-"):
                add("E", "TC-FINDING-NO-REF",
                    "%s 发现项 %s owner=%s 但无 ref（A/B 必须指向具体卡）" % (tc["dir"], fid or "?", owner),
                    "project-state/测试卡/%s/发现项.md" % tc["dir"])
            elif ref and ref != "-":
                if ref.startswith("card-") and ref not in dir_ids:
                    add("E", "TC-REF-DANGLING",
                        "%s 发现项 %s 的 ref=%s 无对应卡目录" % (tc["dir"], fid or "?", ref),
                        "project-state/测试卡/%s/发现项.md" % tc["dir"])
                elif ref.startswith("tc-") and ref not in tc_ids:
                    add("E", "TC-REF-DANGLING",
                        "%s 发现项 %s 的 ref=%s 无对应测试卡目录" % (tc["dir"], fid or "?", ref),
                        "project-state/测试卡/%s/发现项.md" % tc["dir"])
        for fid, owner in no_owner:
            add("E", "TC-FINDING-NO-OWNER",
                "%s 发现项 %s 无合法 owner（%r）——零无归属是收口条件"
                % (tc["dir"], fid, owner),
                "project-state/测试卡/%s/发现项.md" % tc["dir"])
        for fid in no_anchor:
            add("E", "TC-FINDING-NO-ANCHOR",
                "%s 发现项 %s **既无 anchor 也无 entry**——下一个接到它的人只能重新定位一遍。"
                "`anchor` 允许写「未知」，但那时 `entry`（怎么复现）必填"
                % (tc["dir"], fid),
                "project-state/测试卡/%s/发现项.md" % tc["dir"])
        for fid, anc in dangling_anchor:
            add("E", "TC-ANCHOR-DANGLING",
                "%s 发现项 %s 的 anchor 路径不存在：%s（**只有路径参与校验**；"
                "行号/符号会随无关编辑漂移，故不作判据）"
                % (tc["dir"], fid, anc),
                "project-state/测试卡/%s/发现项.md" % tc["dir"])

        # 覆盖行引用的 finding 必须存在
        for row in tc["coverage"]:
            fref = (row.get("finding") or "").strip()
            if not fref or fref == "-":
                continue
            for one in re.split(r"[,，;\s]+", fref):
                one = one.strip()
                if one and one.startswith("F-") and one not in fids:
                    add("E", "TC-FINDING-DANGLING",
                        "%s 覆盖维度 %s 引用 %s，但发现项表里无此 id"
                        % (tc["dir"], (row.get("dim") or "?"), one),
                        "project-state/测试卡/%s/覆盖矩阵.md" % tc["dir"])

        cov_summary.append({
            "id": tc["dir"], "status": st, "counts": counts,
            "fail": fail_rows, "unknown": unknown_rows,
            "total": sum(counts.values()),
            "findings": find_counts, "no_owner": len(no_owner),
            "no_anchor": len(no_anchor), "dangling_anchor": len(dangling_anchor),
            "findings_stale": findings_stale,
            # 未闭环发现项带锚点的明细——**P0 的收益就在这里**：
            # 下一次调用直接从摘要里拿到"改哪里 + 怎么复现"，不必重新定位。
            "open_findings": [r for r in tc["findings"]
                              if (r.get("state") or "").strip() == "open"],
            "has_coverage": tc["coverage_found"], "has_findings": tc["findings_found"],
            "coverage_rows": strip_meta(tc["coverage"]),
        })
    data["cov_summary"] = cov_summary

    # --- 证据指针存在性（覆盖矩阵的 evidence 列）
    #
    # 判据来自**真实工程对拍**，不是想当然：示例工作台B `测试卡/tc-001/*` 里的路径
    # 一律相对 `project-state/` 写（`需求卡/card-031/证据/`、`证据/A-API与契约/D1-*`），
    # 而报告类文件里又会写带前缀的 `project-state/契约/api.yaml`。于是**两个基准都要试**，
    # 两边都落空才报缺失——只认一个基准的版本会把真路径全报成缺口（假阳性）。
    #
    # 另外两类**不许当路径校验**（否则每次调用都产生同一批噪声）：
    #   ① 证据列允许写「命令 + 结果」，如 `grep -cE '^  /' project-state/契约/api.yaml  # 0`
    #      ——命令里的路径是**命令的一部分**，不是指针；
    #   ② 证据根允许用通配（`证据/A-API与契约/D1-*`）——只校验通配符前的目录存在。
    missing_ev = []
    for tc in data["test_cards"]:
        for row in tc["coverage"]:
            ev = (row.get("evidence") or "").strip()
            if not ev or ev in ("-", "自述", "无", "—"):
                continue
            if re.match(r"^(http|见|同上)", ev):
                continue
            if re.search(r"\s", ev):        # 含空白 ⇒ 命令/说明，不是裸指针
                continue
            # 只校验"像路径"的：含 / 或扩展名
            if ("/" not in ev) and not re.search(r"\.[A-Za-z0-9]{1,6}$", ev):
                continue
            target = ev.split("::")[0].strip().strip("`")
            if not evidence_exists(data["ps"], root, target):
                missing_ev.append((tc["dir"], (row.get("dim") or "?"), target))
    data["missing_evidence"] = missing_ev
    for tc_id, dim, target in missing_ev:
        add("E", "EVIDENCE-MISSING",
            "%s 覆盖维度 %s 的证据指针不存在：%s" % (tc_id, dim, target),
            "project-state/测试卡/%s/覆盖矩阵.md" % tc_id)

    # --- 无测试卡
    if not data["test_cards"] and data["card_count_dirs"]:
        add("W", "NO-TEST-CARD",
            "本项目 0 张测试卡 → 系统级状态未知（跨卡链路/数据对账/契约一致/越权穿越无覆盖）",
            "project-state/测试卡/")

    data["issues"] = issues
    data["unmigrated"] = unmigrated
    data["e_count"] = sum(1 for i in issues if i["sev"] == "E")
    data["w_count"] = sum(1 for i in issues if i["sev"] == "W")
    # 回归防线哨兵面（v5.28.0）：用例库基线哨兵集的 pass/fail/unknown——
    # 冷启动一眼看到「修过的问题还有没有防线」（None = 用例库/机读块读不到，无法检查 ≠ 零哨兵）
    data["reg_baseline"] = read_baseline_cases(data["ps"])
    return data


def read_baseline_cases(ps):
    """读用例库机读块，算**基线哨兵集**（v5.28.0）。

    基线哨兵集 = `source` 前缀 ∈ {发现,反馈,回归,探索} 且 `last ≠ retired` 的 CAS
    ——它们是「修过的问题」的防复发哨兵（AC 用例是需求基线，不在此列）。
    返回 None = 读不到（**无法检查 ≠ 没问题**）；否则返回统计 dict。
    """
    path = os.path.join(ps, "代码", "测试", "用例库.md")
    if not os.path.isfile(path):
        return None
    try:
        txt = read_text(path)
    except OSError:
        return None
    rows, found, _header = parse_machine_block(txt, "cases")
    if not found:
        return None
    stats = {"total": 0, "pass": 0, "fail": [], "unknown": [], "skip": 0}
    for r in rows:
        src = (r.get("source") or "").strip()
        last = (r.get("last") or "").strip()
        cid = (r.get("id") or "").strip()
        head = src.split(":", 1)[0] if ":" in src else ""
        if head not in ("发现", "反馈", "回归", "探索"):
            continue
        if last == "retired":
            continue
        stats["total"] += 1
        if last == "pass":
            stats["pass"] += 1
        elif last == "fail":
            stats["fail"].append(cid)
        elif last == "unknown":
            stats["unknown"].append(cid)
        elif last == "skip":
            stats["skip"] += 1
    return stats


# ---------------------------------------------------------------- 渲染（带预算裁剪）

class Emitter:
    """按预算裁剪明细但**永不裁计数**。

    每节 = head（永远保留）+ detail（可裁）。超预算时从**最低优先级**的节开始按
    TRIM_STEP 行递减，被裁的节留下明确的截断标注。
    """

    def __init__(self):
        self.sections = []   # list of dict(key, title, head[], detail[(prio, text)])

    def section(self, key, title):
        self.sections.append({"key": key, "title": title, "items": []})
        return self

    def insert_first(self, key, title, head_lines):
        """把头部插到最前。头部必须在**其余各节都建好之后**才生成——它要报告本次
        输出的实际体量，而那个数只有渲染时才知道（第一版把头部写在最前，结果它把
        "输出 126 token" 印在一个 4000+ token 的报告头上）。"""
        items = [("head", 0, t) for t in head_lines]
        self.sections.insert(0, {"key": key, "title": title, "items": items})
        return self

    def head(self, text):
        self.sections[-1]["items"].append(("head", 0, text))
        return self

    def detail(self, text, prio=None):
        k = self.sections[-1]["key"]
        self.sections[-1]["items"].append(
            ("detail", prio or TRIM_PRIORITY.get(k, 5), text))
        return self

    def _prio_counts(self):
        counts = {}
        for s in self.sections:
            for kind, p, _ in s["items"]:
                if kind == "detail":
                    counts[p] = counts.get(p, 0) + 1
        return counts

    def render(self, budget_tokens, full=False):
        """按**优先级**（不是按节）裁剪，且**保持插入顺序**。

        第二版按"节"裁剪：Section 3 的 head 行先全部输出、detail 行后追加，
        结果 `卡 status ≠ 账本 status 4 条：` 这一行后面跟着的却是 10 行无关内容，
        4 条漂移明细孤零零落在节尾——**指路的话和路分家了**。
        """
        cap = self._prio_counts()
        if not full:
            while est_tokens(self._join(cap)) > budget_tokens:
                live = [p for p in cap if cap[p] > 0]
                if not live:
                    break
                cap[min(live)] = max(0, cap[min(live)] - TRIM_STEP)
        return self._join(cap)

    def _join(self, cap):
        out = []
        for s in self.sections:
            out.append(s["title"])
            used = {}
            total_d = sum(1 for k, _, _ in s["items"] if k == "detail")
            shown_d = 0
            for kind, p, text in s["items"]:
                if kind == "head":
                    out.append(text)
                    continue
                used[p] = used.get(p, 0)
                if used[p] < cap.get(p, 0):
                    out.append(text)
                    used[p] += 1
                    shown_d += 1
            if shown_d < total_d:
                out.append("  …（**本节省略 %d 条明细**，共 %d 条；`--full` 看全。"
                           "注意：被省略的明细不影响上面任何计数）"
                           % (total_d - shown_d, total_d))
            out.append("")
        return "\n".join(out)


def render_report(data, elapsed, budget, full=False, allow_unmigrated=False):
    em = Emitter()
    cfg = data["config"] or {}
    ov = data["overall"]

    # ---- 0 一句话
    n_cards = data["card_count_dirs"]
    n_nt = len(data["non_terminal"])
    n_fail = sum(c["counts"]["fail"] for c in data["cov_summary"])
    n_unk = sum(c["counts"]["unknown"] for c in data["cov_summary"])
    n_nowner = sum(c["no_owner"] for c in data["cov_summary"])
    n_nanchor = sum(c.get("no_anchor", 0) for c in data["cov_summary"])
    n_stale = sum(1 for c in data["cov_summary"] if c.get("findings_stale"))
    n_card_anchor = len(data.get("card_no_anchor", []))
    n_miss = len(data["missing_in_ledger"]) + len(data["missing_dirs"]) + len(data["status_drift"])
    n_ev = len(data["missing_evidence"])

    em.section("one", "## 0 一句话")
    em.head("需求卡 **%d** 张（非终态 **%d**）｜测试卡 **%d** 张｜"
            "系统级未通过 **%d** 项、未回填 **%d** 项｜发现项无归属 **%d** 条、**无锚点 %d 条**｜"
            "账实差异 **%d** 条%s｜证据缺口 **%d** 条｜**严重 %d，提示 %d**"
            % (n_cards, n_nt, len(data["test_cards"]), n_fail, n_unk, n_nowner, n_nanchor, n_miss,
               "（**账本无可对账的卡状态面**）" if data.get("ledger_schema") not in
               ("cards-map", "cards-list") else "",
               n_ev, data["e_count"], data["w_count"]))
    # 旧列集必须单独说一句：它的"无锚点 0"是**读不到**，不是**没有缺**（同 R8）。
    if n_stale:
        em.head("⚠️ 其中 **%d 张测试卡的发现项机读块是旧列集**（5 列，无 `anchor`/`entry`）"
                "⇒ 上句的「无锚点 0 条」对它们是**读不到，不是没有**。见 §7。" % n_stale)
    if n_card_anchor:
        em.head("⚠️ **修复卡 %d 张无 `anchor`/`entry`**——下一个接到它的人只能重新定位一遍。"
                "见 §6。" % n_card_anchor)
    # 「空」与「缺」必须分开说：没有机读块时，"未通过 0 项"是**读不到**，不是**零问题**。
    # 第一版就把它印成 0，读的人会以为系统级是干净的。
    n_covblocks = sum(1 for c in data["cov_summary"] if c["has_coverage"])
    if data["test_cards"] and not n_covblocks:
        em.head("⚠️ **上句的「0 项」实为「读不到」**：%d 张测试卡全部缺机读块 ⇒ "
                "**系统级状态未知**，未通过项数不可知（是未知，不是零）。见 §2/§7。"
                % len(data["test_cards"]))
    elif not data["test_cards"]:
        em.head("⚠️ **本项目 0 张测试卡 ⇒ 系统级状态未知**（无覆盖可读，是未知不是零）。见 §6。")
    verdict = "需处置（见下）" if data["e_count"] else ("有提示" if data["w_count"] else "干净")
    em.head("→ **%s**" % verdict)

    # ---- 1 卡级状态
    dist = {s: 0 for s in CARD_STATUSES}
    extras = {}
    no_status = 0
    for c in data["cards"]:
        n = c.get("status_norm") or ""
        if not n:
            no_status += 1
        elif n in dist:
            dist[n] += 1
        else:
            extras[c["status"]] = extras.get(c["status"], 0) + 1
    em.section("cards", "## 1 卡级状态（真相源：`需求卡/*/card.md` frontmatter）")
    line = "状态分布：" + "｜".join("%s %d" % (s, dist[s]) for s in CARD_STATUSES)
    if extras:
        line += "｜〔集合外〕" + "｜".join("%s %d" % (k, v) for k, v in sorted(extras.items()))
    if no_status:
        line += "｜〔缺 status〕%d" % no_status
    em.head(line)
    em.head("卡片数 %d（卡文件缺失 %d）"
            % (data["card_count_dirs"], sum(1 for c in data["cards"] if not c["has_md"])))
    if data["status_variants"]:
        em.head("- ⚠️ **状态写法漂移 %d 张**（状态机看不见它们）：%s"
                % (len(data["status_variants"]),
                   "; ".join("%s %r→%r" % v for v in data["status_variants"][:8])))
    if data["status_extensions"]:
        for raw, cs_ in sorted(data["status_extensions"].items()):
            tag = "已声明" if raw in data["status_declared_ext"] else "**未声明**"
            em.head("- 〔集合外状态〕%r %d 张（%s）：%s"
                    % (raw, len(cs_), tag, ", ".join(cs_[:8])))
    if data["non_terminal"]:
        em.head("非终态 %d 条（需处置）：" % len(data["non_terminal"]))
        for c in data["non_terminal"]:
            err = (c["last_error"] or "").strip()
            shown = c["status"] + ("（写作 %s）" % c["status"] if c.get("status_variant") else "")
            em.detail("- `%s` **%s**%s%s"
                      % (c["dir"], shown,
                         "  attempts=%s" % c["attempts"] if c["attempts"] else "",
                         "  ✗ %s" % (err[:120] + ("…" if len(err) > 120 else "")) if err else ""),
                      TRIM_PRIORITY["cards"])
    else:
        em.head("非终态 0 条 ✅")

    # ---- 2 系统级状态
    em.section("systemic", "## 2 系统级状态（真相源：`测试卡/tc-*/` 机读块）")
    if not data["test_cards"]:
        em.head("**0 张测试卡 → 系统级状态未知。**"
                "跨卡链路、数据对账、关系完整性、契约全量一致、越权穿越、状态机穿越"
                "这些维度**没有任何一轮覆盖**；历次发现只能散落在 `进度/` 的对账表里。")
    for tc in data["cov_summary"]:
        cnt = tc["counts"]
        em.head("- **%s** status=`%s`｜覆盖 %d 维：%s"
                % (tc["id"], tc["status"] or "（缺）", tc["total"],
                   "｜".join("%s %d" % (r, cnt[r]) for r in RESULT_ORDER if cnt[r]) or "（无机读块）"))
        if tc.get("flat"):
            em.head("    ⚠️ **平铺测试卡**（`测试卡/%s.md`）⇒ 覆盖矩阵/发现项无处安放" % tc["id"])
            continue
        if not tc["has_coverage"]:
            em.head("    ⚠️ 缺 `machine:coverage` 机读块——**未迁移**："
                    "覆盖结论对机器不可读，再调用只能靠人读散文")
        if not tc["has_findings"]:
            em.head("    ⚠️ 缺 `machine:findings` 机读块——**未迁移**")
        for row in tc["fail"]:
            ev = (row.get("evidence") or "-").strip()
            fnd = (row.get("finding") or "-").strip()
            em.detail("    ✗ fail  %-8s %-22s 证据=%s%s"
                      % (row.get("dim", "?"), (row.get("name") or row.get("desc") or "")[:22],
                         ev, "  发现项=%s" % fnd if fnd not in ("-", "") else ""),
                      TRIM_PRIORITY["systemic"])
        for row in tc["unknown"]:
            em.detail("    ? 未回填 %-8s %s" % (row.get("dim", "?"),
                                              (row.get("name") or "")[:30]),
                      TRIM_PRIORITY["systemic"] - 1)
        fc = tc["findings"]
        if sum(fc.values()) or tc["no_owner"]:
            em.head("    发现项 %d 条：%s｜**无归属 %d**%s"
                    % (sum(fc.values()),
                       "｜".join("%s %d" % (s, fc[s]) for s in FINDING_STATES if fc[s]),
                       tc["no_owner"],
                       "｜**无锚点 %d**" % tc["no_anchor"] if tc.get("no_anchor") else ""))
        elif tc["has_findings"]:
            em.head("    发现项 0 条（机读块为空——确认是否真的零发现）")
        # 未闭环发现项的**定位锚点**：这是 P0 的收益面——下一轮不必重新定位。
        # 计数永远印全（`无锚点 N` 在上面那行），这里只裁明细。
        opens = tc.get("open_findings") or []
        if opens and not tc.get("findings_stale"):
            for row in opens:
                anc = (row.get("anchor") or "").strip() or "（未知）"
                ent = (row.get("entry") or "").strip() or "（未写）"
                em.detail("      ⤷ %s [%s] 锚点=`%s` 复现=`%s`"
                          % ((row.get("id") or "?").strip(),
                             (row.get("severity") or "-").strip(), anc[:90], ent[:90]),
                          TRIM_PRIORITY["systemic"] + 1)
        elif opens and tc.get("findings_stale"):
            em.head("      ⤷ 未闭环 %d 条，但机读块是**旧列集** ⇒ 锚点读不到（**不是没有**）" % len(opens))

    # 回归防线（v5.28.0）：基线哨兵集状态——「修过的问题」有没有留下防复发的用例
    rb = data.get("reg_baseline")
    if rb is None:
        em.head("- 回归防线：用例库机读块读不到——哨兵面**无法检查（不是零哨兵）**")
    elif rb["total"]:
        line = "- 回归防线：基线哨兵 %d 条（pass %d）" % (rb["total"], rb["pass"])
        if rb["fail"]:
            line += "｜**fail %d**（旧 bug 复现）：%s" % (len(rb["fail"]), ", ".join(rb["fail"][:6]))
        if rb["unknown"]:
            line += "｜未回填 %d：%s" % (len(rb["unknown"]), ", ".join(rb["unknown"][:6]))
        if rb["skip"]:
            line += "｜skip %d" % rb["skip"]
        em.head(line)
        if rb["fail"]:
            em.detail("      ⤷ 二进宫处置：按线 3 建修复卡 `source: 回归:CAS-NNN`，"
                      "归因「哪个改动把它弄坏的」；全貌跑 "
                      "`python -X utf8 scripts/regression_audit.py <项目根>`",
                      TRIM_PRIORITY["systemic"])
    else:
        em.head("- 回归防线：用例库在、基线哨兵 0 条（还没有修过的问题留哨兵）")

    # ---- 3 账实对账
    em.section("ledger", "## 3 账实对账（派生物 vs 真相源）")
    em.head("账本 = `进度/overall.json`（%s 字节）%s"
            % (human(data["overall_bytes"]),
               "｜⚠️ 不可解析：%s" % data["overall_err"] if data["overall_err"] else ""))
    ledger_ok = data.get("ledger_schema") in ("cards-map", "cards-list")
    if not ledger_ok:
        # 「读不到」≠「是 0」：账本没有卡状态面时**不做对账陈述**，也不把 62 张卡报成缺失。
        em.head("- ⚠️ **账本无卡状态面**（schema=`%s`）⇒ 账实对账**无法进行**。"
                "本节不再输出「有卡无账/有账无卡/状态漂移」——那些数在这里没有意义。" % data.get("ledger_schema"))
        if data.get("ledger_open_items") is not None:
            em.head("- 〔可用〕账本 `open_items`（未闭合项）**%d** 条%s"
                    % (data["ledger_open_items"],
                       "｜`blockers` **%d** 条" % data["ledger_blockers"]
                       if data.get("ledger_blockers") is not None else ""))
        else:
            em.head("- 〔注意〕账本里既无卡状态面也无 `open_items`：卡 frontmatter 是**唯一**状态来源")
    else:
        if data["missing_in_ledger"]:
            em.head("- **有卡无账 %d 条**：%s"
                    % (len(data["missing_in_ledger"]),
                       ", ".join(data["missing_in_ledger"][:10])
                       + (" …" if len(data["missing_in_ledger"]) > 10 else "")))
        else:
            em.head("- 有卡无账 0 条 ✅")
        em.head("- 有账无卡 %d 条%s"
                % (len(data["missing_dirs"]),
                   ("：" + ", ".join(data["missing_dirs"][:10])) if data["missing_dirs"] else " ✅"))
        if data["missing_tc_dirs"]:
            em.head("- 账本测试卡无目录 %d 条：%s"
                    % (len(data["missing_tc_dirs"]), ", ".join(data["missing_tc_dirs"])))
        if data["status_drift"]:
            em.head("- **卡 status ≠ 账本 status %d 条（以卡为准）**：" % len(data["status_drift"]))
            for cid, cs, ls in data["status_drift"][:20]:
                em.detail("    `%s` 卡=%s 账=%s" % (cid, cs, ls), TRIM_PRIORITY["ledger"])
        else:
            em.head("- 卡 status 与账本一致 ✅")
    if data.get("batch_keys"):
        em.head("- 顶层『批次备忘录』键 %d 个、合计 %s 字节：%s"
                % (len(data["batch_keys"]), human(data["batch_bytes"]),
                   ", ".join(data["batch_keys"][:8])))
    for k, n, limit, nested in data["prose_fields"]:
        em.head("- **字段 `%s` 散文 %s 字节**（阈值 %s）%s"
                % (k, human(n), human(limit),
                   "；含 %d 层递归『上一轮…述（逐字保留）』" % nested if nested else ""))
    tl = data.get("task_list")
    tls = data.get("task_list_state")
    if tl is None:
        em.head("- 〔信息〕无 `任务清单.md`：**任务级视图缺失**（是「核不了」，不是「无差异」）")
    elif tls == "not-enabled":
        em.head("- 〔信息〕`任务清单.md` 有 %d 条任务行、勾选 %d 条，但**无一条挂到已知卡**"
                "⇒ 三方一致**无从机核**（是「核不了」，不是「核过了没差异」）；"
                "模板要求每项挂 `卡：card-NNN`，未挂的复选框不算任务"
                % (len(tl["tasks"]), tl["ticked"]))
    elif tls == "not-maintained":
        em.head("- 〔信息〕`任务清单.md` 有 %d 条挂卡任务、**勾选 0 条**⇒ 该视图**未维护**，"
                "逐卡对账**不产生差异**（是「没在用」，不是「没有滞后」）；"
                "卡 frontmatter 仍是唯一真相源" % tl["mapped"])
    else:
        em.head("- 任务清单 ↔ 卡状态（卡为真相源）：任务行 %d 条（挂卡 %d）｜勾选 %d 条｜"
                "滞后（挂卡行未勾）%d 条、超前（已勾卡未 done）%d 条｜"
                "清单未覆盖的 done 卡 %d 张（**不是滞后**，是该视图覆盖面之外）"
                % (len(tl["tasks"]), tl["mapped"], tl["ticked"],
                   len(data.get("task_list_stale") or []), len(data.get("task_list_ahead") or []),
                   len(data.get("task_list_untracked") or [])))

    # ---- 4 证据指针
    em.section("evidence", "## 4 证据指针存在性")
    if data["missing_evidence"]:
        em.head("**缺失 %d 条**（overall 矩阵里 `evidence` 指向的文件不存在）："
                % len(data["missing_evidence"]))
        for tc_id, dim, target in data["missing_evidence"]:
            em.detail("    %s %s → `%s`" % (tc_id, dim, target), TRIM_PRIORITY["evidence"])
    else:
        em.head("缺失 0 条 ✅（无机读块的项目此节无意义——指针尚不可机读）")

    # ---- 5 待办与断点
    em.section("log_tail", "## 5 待办与断点线索")
    tot_open = sum(t["open"] for t in data["todos"])
    em.head("`待办/` 未决 %d 条（%d 个文件）" % (tot_open, len(data["todos"])))
    for t in data["todos"]:
        em.detail("    `%s`：未决 %d 条" % (t["file"], t["open"]), TRIM_PRIORITY["log_tail"])
        for s in t["sample"]:
            em.detail("      %s" % s[:100], TRIM_PRIORITY["log_tail"] - 1)
    em.head("`运行日志.md` 尾部 %d 行（**只读文件尾 %s 字节**，不整份读）"
            % (len(data["log_tail"]), human(LOG_TAIL_READ_BYTES)))
    for ln in data["log_tail"]:
        em.detail("    %s" % ln[:160], TRIM_PRIORITY["log_tail"])

    # ---- 6 建议下一步
    em.section("next", "## 6 建议下一步（按优先级）")
    steps = []
    blk = [c for c in data["non_terminal"] if c["status"] == "blocked"]
    if blk:
        steps.append("`blocked` 卡 %d 张——走 CR / 换方案 / 升级用户，**不自动重开**：%s"
                     % (len(blk), ", ".join(c["dir"] for c in blk[:6])))
    if data["missing_in_ledger"]:
        steps.append("账本落后 %d 张卡——跑 `python scripts/migrate.py <项目根>` 或按卡回填账本；"
                     "**不要手写对账表**（那正是本轮要消掉的开销）"
                     % len(data["missing_in_ledger"]))
    if data["status_drift"]:
        steps.append("卡/账状态漂移 %d 条——**以卡为准**回写账本" % len(data["status_drift"]))
    if n_nowner:
        steps.append("发现项无归属 %d 条——三选一（回退卡 / 新建修复卡 / 提 CR），**零无归属才能收口**"
                     % n_nowner)
    if data.get("card_no_anchor"):
        steps.append("修复卡无锚点 %d 张——frontmatter 补 `anchor`（改哪里，`路径[:行]` 或「未知」）"
                     "+ `entry`（怎么复现，必填）" % len(data["card_no_anchor"]))
    if data.get("card_dangling_anchor"):
        steps.append("修复卡锚点路径不存在 %d 张——锚点写的是**当时**的坐标，"
                     "文件被移动/改名后要更新；**只校验路径，行号漂移不算问题**"
                     % len(data["card_dangling_anchor"]))
    if n_nanchor:
        steps.append("未闭环发现项无锚点 %d 条——发现的人当场留下 `anchor`+`entry` 最省，"
                     "事后补要重新定位一遍" % n_nanchor)
    if n_stale:
        steps.append("测试卡发现项机读块为**旧列集** %d 张——把 `anchor` / `entry` 两列"
                     "**追加到行尾**（旧行内容不用动）" % n_stale)
    if data["missing_evidence"]:
        steps.append("证据指针缺失 %d 条——补齐或把 `evidence` 改标 `自述`（**如实登记**，不许留悬空路径）"
                     % len(data["missing_evidence"]))
    if n_unk:
        steps.append("覆盖维度未回填 %d 条——跑一轮或明确标 `not_covered`，**不许留 unknown 就置 done**" % n_unk)
    if n_fail:
        steps.append("系统级未通过 %d 项——按其在测试卡里的 `finding` 归属逐条闭环" % n_fail)
    rb = data.get("reg_baseline")
    if rb:
        if rb["fail"]:
            steps.append("回归哨兵 fail %d 条（%s）——**旧 bug 复现（二进宫）**：建修复卡 "
                         "`source: 回归:CAS-NNN` 并归因；防线全貌跑 "
                         "`python -X utf8 scripts/regression_audit.py <项目根>`"
                         % (len(rb["fail"]), ", ".join(rb["fail"][:6])))
        if rb["unknown"]:
            steps.append("回归哨兵未回填 %d 条（%s）——跑绿回填 `last`，不许留 unknown 收口"
                         % (len(rb["unknown"]), ", ".join(rb["unknown"][:6])))
    if not data["test_cards"]:
        steps.append("本项目 0 张测试卡——**系统级状态无从得知**；建 `测试卡/tc-001/`，"
                     "把历次散落发现从 `进度/` 的对账表搬进来（这是「从测试卡出发」的前提）")
    if data["prose_fields"] or data.get("batch_bytes", 0) > BATCH_KEY_TOTAL_BUDGET:
        steps.append("账本散文超阈值——**历史移入 `记忆/`（append-only，不参与冷启动读取）**，"
                     "账本只留机器可读的当前值")
    if not steps:
        steps.append("无待处置项；可直接进入事件循环派活")
    for i, s in enumerate(steps, 1):
        em.head("%d. %s" % (i, s))

    # ---- 7 未迁移（两种模式都列：这是**事实**，不是迁移模式专属）
    if data.get("unmigrated"):
        em.section("unmigrated", "## 7 未迁移机读块（**不是「没问题」**）")
        em.head("以下 %d 处**缺机读块** ⇒ 其结论本脚本读不到，**不等于结论为空或通过**。"
                "%s这几处仍需人读散文："
                % (len(data["unmigrated"]),
                   "" if allow_unmigrated else "已按 E 计入上方严重项；"))
        for u in data["unmigrated"]:
            em.detail("    - %s" % u, TRIM_PRIORITY["systemic"])

    # ---- 头部（**最后建**：它要报告本次输出的实际体量）
    body = em.render(budget, full=full)
    est = int(est_tokens(body))
    head_lines = [
        "- 项目根：`%s`" % data["root"],
        "- 运行模式：`%s`｜配置档：`%s`｜schema_version：`%s`"
        % (cfg.get("mode", "（未写，缺省 quick）"),
           cfg.get("profile") or cfg.get("project_profile") or "-",
           cfg.get("schema_version", "（缺）")),
    ]
    if not cfg:
        head_lines.append("- ⚠️ `config.json` 缺失或不可解析")
    head_lines += [
        "- 数据源：**卡 frontmatter（卡级真相源）+ 测试卡机读块（系统级真相源）**；账本仅作对账",
        "- 采集耗时 **%.1fs**｜本次输出约 **%s token**（估算口径 字节/2.2；预算 %s，%s）"
        % (elapsed, human(est), human(budget),
           "未裁剪" if full else "超预算只裁明细、**计数恒全**"),
    ]
    if allow_unmigrated:
        head_lines.append("- 迁移期模式（`--allow-unmigrated`）：缺机读块不阻断，逐条列在 §7")
    em.insert_first("head", "# 再调用摘要 · %s"
                    % (cfg.get("project") or os.path.basename(data["root"])), head_lines)
    return em.render(budget, full=full)


# ---------------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="再调用摘要：冷启动唯一读入入口（单次遍历，输出与预算对齐）")
    ap.add_argument("project_root", help="项目根（含 project-state/；也可直接给 project-state 路径）")
    ap.add_argument("--budget", type=int, default=6000, help="输出 token 预算（估算，默认 6000）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（供其它脚本消费）")
    ap.add_argument("--check", action="store_true", help="门禁模式：只报问题与计数")
    ap.add_argument("--write", action="store_true",
                    help="同时落盘 project-state/进度/再调用摘要-<日期>.md")
    ap.add_argument("--full", action="store_true", help="不裁剪明细")
    ap.add_argument("--allow-unmigrated", action="store_true",
                    help="迁移期：缺机读块降为提示、单列一节，不阻断退出码")
    args = ap.parse_args(argv)

    root = os.path.abspath(args.project_root)
    if os.path.basename(root) == "project-state":
        root = os.path.dirname(root)
    ps = os.path.join(root, "project-state")
    if not os.path.isdir(ps):
        print("✗ 不是项目根（缺 project-state/）：%s" % root, file=sys.stderr)
        print("  提示：给含 project-state/ 的目录，或直接给 project-state 本身。", file=sys.stderr)
        return 2

    t0 = time.time()
    data = collect(root)
    data = analyse(data, root, allow_unmigrated=args.allow_unmigrated)
    elapsed = time.time() - t0

    if not data["config"]:
        cfgp = os.path.join(ps, "config.json")
        if not os.path.exists(cfgp):
            print("✗ 缺 project-state/config.json，无法识别项目：%s" % cfgp, file=sys.stderr)
            print("  先跑 python scripts/migrate.py %s" % root, file=sys.stderr)
            return 2

    if args.json:
        payload = {
            "root": root,
            "elapsed_s": round(elapsed, 2),
            "config": data["config"],
            "counts": {
                "card_dirs": data["card_count_dirs"],
                "tc_dirs": data["tc_count_dirs"],
                "non_terminal": len(data["non_terminal"]),
                "missing_in_ledger": len(data["missing_in_ledger"]),
                "missing_dirs": len(data["missing_dirs"]),
                "status_drift": len(data["status_drift"]),
                "missing_evidence": len(data["missing_evidence"]),
                "findings_no_anchor": sum(c.get("no_anchor", 0) for c in data["cov_summary"]),
                "findings_format_stale": sum(1 for c in data["cov_summary"]
                                             if c.get("findings_stale")),
                "cards_no_anchor": len(data.get("card_no_anchor", [])),
                "cards_anchor_dangling": len(data.get("card_dangling_anchor", [])),
                "errors": data["e_count"],
                "warnings": data["w_count"],
            },
            "card_anchors": {
                "no_anchor": data.get("card_no_anchor", []),
                "dangling": [{"card": c, "anchor": a}
                             for c, a in data.get("card_dangling_anchor", [])],
                "unmigrated_fix_cards": data.get("card_anchor_unmigrated", []),
            },
            "open_findings": [
                {"tc": c["id"], "items": strip_meta(c.get("open_findings") or [])}
                for c in data["cov_summary"] if c.get("open_findings")
            ],
            "non_terminal": [{k: c[k] for k in ("dir", "status", "attempts", "last_error")}
                             for c in data["non_terminal"]],
            "coverage": data["cov_summary"],
            "ledger": {
                "overall_bytes": data["overall_bytes"],
                "missing_in_ledger": data["missing_in_ledger"],
                "missing_dirs": data["missing_dirs"],
                "missing_tc_dirs": data["missing_tc_dirs"],
                "status_drift": [{"card": a, "card_status": b, "ledger_status": c}
                                 for a, b, c in data["status_drift"]],
                "prose_fields": [{"field": k, "bytes": n, "limit": l, "nested": d}
                                 for k, n, l, d in data["prose_fields"]],
                "batch_keys": data.get("batch_keys", []),
                "batch_bytes": data.get("batch_bytes", 0),
                "task_list_done": data.get("task_list_done"),
            },
            "todos": data["todos"],
            "log_tail": data["log_tail"],
            "issues": data["issues"],
            "unmigrated": data.get("unmigrated", []),
        }
        sys.stdout.reconfigure(encoding="utf-8")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif args.check:
        sys.stdout.reconfigure(encoding="utf-8")
        print("再调用体检 · %s（%.1fs）" % (os.path.basename(root), elapsed))
        print("严重 %d｜提示 %d｜未迁移机读块 %d 处%s"
              % (data["e_count"], data["w_count"], len(data.get("unmigrated", [])),
                 "（**已按 E 计入严重**）" if not args.allow_unmigrated
                 and data.get("unmigrated") else ""))
        for i in data["issues"]:
            print("  [%s] %-24s %s" % (i["sev"], i["code"], i["msg"]))
        for u in data.get("unmigrated", []):
            print("  [迁移] %s" % u)
        if not data["issues"] and not data.get("unmigrated"):
            print("  ✅ 无问题")
    else:
        report = render_report(data, elapsed, args.budget, full=args.full,
                               allow_unmigrated=args.allow_unmigrated)
        sys.stdout.reconfigure(encoding="utf-8")
        print(report)
        if args.write:
            out = os.path.join(ps, "进度", "再调用摘要-%s.md" % datetime.date.today().strftime("%Y%m%d"))
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "w", encoding="utf-8", newline="\n") as f:
                f.write(report + "\n")
            print("\n[已写入] %s" % out)

    return 1 if data["e_count"] else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v5.1.3 指导体检（guide_audit）：交叉核对证据链，报告「机制被绕过」的痕迹。

与既有体检工具的分工（**不重复**）：
- `framework_lint.py` 管**框架自身/项目结构是否合规**（编号/引用/注册表/漂移）。
- `path_audit.py` 管**文件在不在该在的地方**。
- `workspace_hygiene.py` 管**垃圾清不清**。
- `guide_audit.py` 管**机制有没有被使用**——需求没建卡就开工、卡没留痕就置 done、
  环境被降级冒充、prod 变更无审批就执行……这些**跨多个文件的痕迹**没有别的工具在看。

判据来源一律是**框架既有条款**（注意事项 / 验收标准 / 需求卡模板 / 契约/环境.md /
变更申请单模板），**不新造标准**——对齐「临时兼岗纪律③：只兼有明确步骤与判据的活」。

每一项的「症状 → 判据出处 → 最小修复动作 → 责任人」写在
`skills/项目经理/指导手册.md`，本脚本只负责**发现与定位**，`--selfcheck` 校验**三方配对**：
脚本 CATALOG ↔ 手册处方 ↔ 回归用例。缺任何一方都是同一个洞——「查出问题没人知道怎么修」
或「改了没人拦得住」，正是指导模式要堵的东西。

**覆盖面声明（v5.1.0）**：本工具只回答「G01–G11 这 11 类**已知**绕过痕迹在不在」。
所以每次输出都带一句「结论只在 G01–G11 内成立」——`报告"这 11 类干净"` 与
`报告"没有问题"` 是两件事，后者会把「从未查过」读成「查过且干净」（通用铁律②）。
**不覆盖**的维度见 `COVERAGE_UNCOVERED`：真实运行环境 / 证据真伪 / 角色产出真伪 /
需求是否合理 / **以及没被想到的绕过手法**。

**v5.1.1（2026-09-20）**：修 `check_g11` 的**发现号形态闸门**（`F-41`）。旧版只按「以 `|`
开头且 ≥6 单元格」认定发现行，于是把台账正文里**任意 6 列数据表**读成发现行——实测一张 CSS
像素实测表产出 5 条 ERROR + 5 条 WARN，编号被读成 `4`/`16`/`32`/`64`/短名 `a.png`，
全部与台账真实内容无关。现要求编号栏形如 `F-<数字>`（或 `FIND-<数字>`）才计入，
被跳过的表格行记 INFO。回归用例 T-K 钉住两面（数据表放过 / 真坏行照抓）。

**v5.1.3（2026-09-21）**：修 `check_g06` 的**审批栏定位方式**（`F-42`）。旧版按
**章节字母**定位（`G`=审批栏 / `H`=执行留痕），而实单允许自增章节——CR-20260920-007/008 与
CR-20260921-009 各自加了章节，审批栏因此落到 `H`，正则零命中、`section_of` 返回空串，
工具报「审批栏未签（在飞）」。**该读数结论碰巧正确、判据路径错误**：三单真签了也会照报「未签」。
现改为**按标题定位**（字母可选），字母错位不再影响读数。回归用例 T-M 钉住两面
（错位单照读 / 真未签仍报 WARN）。

**G11 是自增强回路的探针**：G01–G10 抓「机制被绕过」，G11 抓「抓到的绕过有没有变成
下一个检查点」。靠人或用户（而不是 G01–G10）发现的绕过必须落进
`project-state/记忆/指导发现台账.md`，并归属到**一个真实检查号**或**一张真实变更单**
（`CR-YYYYMMDD-NNN`）；**零归属 = 未收口**（同测试卡纪律②），指向不存在的号 = 假登记。
G11 的诚实边界：它管「发现了有没有记下来」，**管不了「有没有发现」**。

用法：
    python guide_audit.py <项目根>                  # 人类可读报告
    python guide_audit.py <项目根> --json           # 机器可读
    python guide_audit.py <项目根> --check G05      # 只看某一项（可重复）
    python guide_audit.py <项目根> --write          # 另写 project-state/指导体检-<日期>.md
    python guide_audit.py <项目根> --max-mb 5       # 卡卷宗大文件阈值（默认 5MB，模板规定）
    python guide_audit.py <项目根> --force          # 指导模式为 off 时仍跑一次（不改开关）
    python guide_audit.py --selfcheck               # 校验「检查号 ↔ 指导手册处方」配对

**开关（v5.0.0）**：`project-state/config.json` 的 `guidance`（`"on"` / `"off"`，**默认 on**）
是与运行模式（full/rapid）**正交**的开关。为 `off` 时本工具**不跑检查**并明确报告
「本次未做任何检查」——**绝不输出"未发现问题"**：`关掉` 与 `查过且干净` 是两件事
（通用铁律②）。`off` 须在项目计划注明依据；只想跑一次而不改开关用 `--force`。

退出码：0 无 ERROR 级发现（含 guidance=off 的未检查）/ 1 有 ERROR / 2 用法错误或不是框架工作区。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import io
import json
import os
import re
import sys

ERROR = "ERROR"
WARN = "WARN"
INFO = "INFO"
_LEVEL_ORDER = {ERROR: 0, WARN: 1, INFO: 2}

# ---------------------------------------------------------------- 检查目录
# 检查号 → (症状, 最小修复动作, 责任人)。判据出处的长文在 skills/项目经理/指导手册.md。
CATALOG = {
    "G01": ("需求未建卡 / 卡引用悬空",
            "在 需求卡/ 建卡并把 PRD 的 REQ 填进 frontmatter req；悬空引用改为真实卡号或删掉",
            "产品经理（建卡）/ 主控（排产）"),
    "G02": ("契约未定就开工",
            "把卡引用的契约补齐（或先在 契约/ 落一份最小版），再置 in_progress",
            "该卡 assignee / 架构师"),
    "G03": ("置 done 无进度留痕 / 验收记录空壳",
            "回填 进度日志 真实记录（做了什么、命令、结果）；空壳验收重做并留证据",
            "该卡 assignee / 验收路角色"),
    "G04": ("待澄清未清零或需求确认物缺失就开工",
            "清零 clarify_open 并补 需求确认-产品经理.md，再开工",
            "产品经理"),
    "G05": ("环境被降级/混用（prod 与 dev·test 同目标）",
            "恢复三环境独立目标（端口/库名/DATABASE_URL 互不相同）；验收证据改用 test 重跑",
            "运维工程师"),
    "G06": ("变更未审批就执行 / 审批栏空栏",
            "补签审批栏（主控放行；数据配置类加安全审计）；未批的改动回滚或补单",
            "运维工程师 / 主控"),
    "G07": ("证据落错地方（tmp 内疑似证据、卷宗超 5MB）",
            "证据归位到 卡卷宗/证据/；>5MB 大产物留 产物/ 只放指针",
            "项目经理"),
    "G08": ("卡卷宗混入禁入物（代码副本/构建产物）",
            "删卷宗内代码与构建产物，改为 repos/ 下的指针",
            "该卡 assignee"),
    "G09": ("卡无归属人 / 状态或编号非法",
            "补 assignee（须是启用角色）、修正 status 枚举、对齐目录名与 id",
            "主控"),
    "G10": ("有卡无落盘（角色从未产出进度）",
            "该角色补齐 进度/<角色>.json；确未参与则改 assignee",
            "该卡 assignee / 主控"),
    "G11": ("发现未沉淀成检查点 / 归属悬空",
            "登记进 project-state/记忆/指导发现台账.md，并归属到一个**真实存在**的东西："
            "检查号（如 G05）/ 变更单（CR-YYYYMMDD-NNN）/ 检查工具脚本（如 path_audit.py）；"
            "归属写不清就把发现升成一个新检查号（指导手册 §7 三步）",
            "项目经理（登记）/ 主控（升格为检查号）"),
}

# ---------------------------------------------------------------- 覆盖面声明（v5.1.0）
# 本工具**不覆盖**的维度。写在这里不是为了免责，是因为每次输出都必须把它念一遍：
# 「查过且干净」与「从未查过」混为一谈是本框架反复踩的坑（通用铁律②）。
# v5.0.0 堵的是 `guidance=off` 那一层（没跑 ≠ 干净）；v5.1.0 堵的是**检查号覆盖面**这一层
# （11 类干净 ≠ 没有问题）。
COVERAGE_UNCOVERED = (
    ("真实运行环境",
     "G05 只核 契约/环境.md 与卡上的**声明**；真实 DATABASE_URL 在 .env 里、不入库"),
    ("证据真伪",
     "只核对证据的**位置与形状**（在不在卷宗、超没超 5MB），不验内容是否真跑过"),
    ("角色产出真伪",
     "只核对**有没有落盘**（G10），不判断产出质量、是否抄袭、是否 AI 现编"),
    ("需求是否合理",
     "只核对 REQ 有没有建卡闭环（G01/G04），不判断需求本身对不对"),
    ("没被想到的绕过手法",
     "G01–G11 是**已知**绕过法的清单；**新的绕过法不会出现在任何地方**——"
     "G11 只管「发现了有没有记下来」，不管「有没有发现」"),
)

# 指导发现台账（G11）：发现 → 归属 → 升成下一个检查号，是自增强回路的入口。
LEDGER_REL = os.path.join("记忆", "指导发现台账.md")

# 摩擦类型五选一（v5.24.0）。**每类只有一种固定改法**，不许自由发挥——否则台账会退化成情绪本：
#   绕过 → 加检查点 / 放宽条款并给豁免；不合用 → 新增模式（极速修复即此例）；
#   没写清 → 补 L0 摘要或提升加载层；默认值错 → 改模板默认值或加必填项；
#   验收太晚 → 加验收路或加检查器（v5.20/v5.21/v5.22 三版都属此类）。
# 这五类不是想出来的，是从既有 6 次真实升级（FIND-001/002/003/004/005/006）反推出来的。
FRICTION_TYPES = ("绕过", "不合用", "没写清", "默认值错", "验收太晚")
CR_DIR_REL = os.path.join("问题", "变更申请单")
CR_NO_PAT = re.compile(r"CR-\d{8}-\d{3}")
# 发现号形态闸门（F-41）：只认 `F-` / `FIND-` + 数字。台账正文里任何 ≥6 列的**数据表**
# 都会凑出「6 个单元格」，早期版本据此把它读成发现行（实测：一张 CSS 像素表产出 5 条 ERROR，
# 编号被读成 `4`/`16`/`32`/`64`/短名 `a.png`）。发现号形态是台账自己的约定，拿它当闸门
# 既不用识别「哪张表是发现表」，也不会漏掉真发现行。
FIND_NO_PAT = re.compile(r"^[Ff](?:[Ii][Nn][Dd])?-?\d+$")
TOOL_PAT = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\.py\b")
# 注意**不要**在这里加捕获组：findall 只返回组内容，于是 "path_audit.py" 会被抠成
# "path_audit"，再拿去找 scripts/path_audit（无扩展名）→ 必然找不到 → 把**真归属**
# 误报成假登记。这个假阳性是被回归用例 T-A 抓出来的（好行被误伤那条）。

LEGAL_STATUS = {"todo", "in_progress", "done", "reviewing", "blocked"}
CARD_STATUS_STARTED = {"in_progress", "reviewing", "done", "blocked"}

CODE_SUFFIX = {".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs", ".c", ".cc",
               ".cpp", ".h", ".hpp", ".cs", ".rb", ".php", ".sh", ".ps1", ".bat",
               ".cmd", ".vue", ".svelte", ".kt", ".swift", ".m", ".sql"}
ARCHIVE_SUFFIX = {".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".whl",
                  ".jar", ".exe", ".dll", ".so", ".dylib", ".class", ".pyc"}
DOC_SUFFIX = {".pdf", ".docx", ".xlsx", ".pptx"}
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".idea", ".vscode"}
BUILD_DIRS = {"node_modules", "dist", "build", "target", ".venv", "venv", "__pycache__",
              "out", ".next", "coverage"}

# tmp 内出现这些名字 = 疑似证据/卡资料落错了地方（G07）
EVIDENCE_HINTS = ("验收", "证据", "报告", "交付", "截图", "screenshot", "hash", "指纹",
                  "card-", "tc-", "验收报告", "交付物")

REQ_PAT = re.compile(r"REQ-\d+(?:\.\d+)*")
CARD_PAT = re.compile(r"card-(\d+)")
CONTRACT_REF = re.compile(r"契约/[^\s`）)\],，；;：:]+?\.md")
ENV_LINE = re.compile(r"(执行环境|验证环境|运行环境)\s*[:：]\s*[`*]*\s*(dev|本地|development|local)\b",
                      re.I)
PLACEHOLDER = re.compile(r"[<>]")
TIME_PLACEHOLDER = ("YYYY-MM-DD HH:mm", "YYYY-MM-DD", "YYYY/MM/DD", "HH:mm")

FRAMEWORK_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _reconfigure():
    """Windows GBK 控制台遇 ✅/⚠ 等符号会 UnicodeEncodeError，降级为替换而非崩溃。"""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except Exception:
                pass


def read(path):
    with io.open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def rel(root, path):
    try:
        return os.path.relpath(path, root).replace("\\", "/")
    except ValueError:
        return path


class Finding(object):
    def __init__(self, check, level, message, where="", fix="", owner=""):
        self.check = check
        self.level = level
        self.message = message
        self.where = where
        self.fix = fix
        self.owner = owner

    def as_dict(self):
        return {"check": self.check, "level": self.level, "message": self.message,
                "where": self.where, "fix": self.fix, "owner": self.owner}


class Report(object):
    def __init__(self):
        self.findings = []
        self.infos = []

    def add(self, check, level, message, where="", fix="", owner=""):
        self.findings.append(Finding(check, level, message, where, fix, owner))

    def flag(self, check, level, message, where=""):
        symptom, fix, owner = CATALOG.get(check, ("", "", ""))
        self.add(check, level, message, where, fix, owner)

    def error(self, check, message, where=""):
        self.flag(check, ERROR, message, where)

    def warn(self, check, message, where=""):
        self.flag(check, WARN, message, where)

    def note(self, message):
        self.infos.append(message)

    def n(self, level):
        return len([f for f in self.findings if f.level == level])

    @property
    def ok(self):
        return self.n(ERROR) == 0


# ---------------------------------------------------------------- 通用解析


def parse_frontmatter(text):
    """取首个 --- 围栏内的 key: value；值里的行内注释（`  # ...`）剥掉。"""
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
        val = re.split(r"\s+#", val, 1)[0].strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        out[key.strip()] = val
    return out


def as_int(val, default=0):
    try:
        return int(str(val).strip())
    except (TypeError, ValueError):
        return default


# 变更申请单两栏的定位正则（F-42）：**按章节标题定位，字母可有可无**——
# 单证中途插入章节会让模板的 G/H 顺延成 H/I，靠字母定位会读空。
CR_APPROVAL_RE = r"^#{2,4}\s*(?:[A-Z][.、]?\s*)?审批栏"
CR_TRACE_RE = r"^#{2,4}\s*(?:[A-Z][.、]?\s*)?执行留痕"


def has_section(text, title_re):
    """文本里是否**存在**匹配的章节标题（区别于 `section_of` 返回空串）。"""
    return any(re.match(title_re, ln.strip()) for ln in text.splitlines())


def section_of(text, title_re):
    """取 `## <title>` 到下一个 `## ` 之间的正文（`###` 不截断）。"""
    out, on = [], False
    for ln in text.splitlines():
        if not on:
            if re.match(title_re, ln.strip()):
                on = True
            continue
        if re.match(r"^##\s", ln.strip()):
            break
        out.append(ln)
    return "\n".join(out)


def substance(text):
    """正文的「实质字符数」：去掉标题/引用/表格分隔/占位符/时间占位后剩下的字数。

    用来区分「写了」和「只留了个模板骨架」——空壳验收与无留痕置 done 都靠它判定。
    """
    keep = []
    for ln in text.splitlines():
        s = ln.strip()
        if not s or s.startswith(">") or s.startswith("#"):
            continue
        if re.fullmatch(r"[|\-:\s]+", s):
            continue
        keep.append(s)
    joined = "".join(keep)
    for t in TIME_PLACEHOLDER:
        joined = joined.replace(t, "")
    joined = re.sub(r"<[^<>]*>", "", joined)
    joined = joined.replace("xxx", "").replace("XXX", "").replace("待填", "")
    return len(re.sub(r"\s+", "", joined))


def iter_tables(text):
    """按连续 `|` 行切出表格块，逐个 yield（含表头与分隔行）。"""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if lines[i].strip().startswith("|"):
            block, j = [], i
            while j < len(lines) and lines[j].strip().startswith("|"):
                block.append(lines[j])
                j += 1
            yield block
            i = j
        else:
            i += 1


def cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def is_placeholder(val):
    v = (val or "").strip()
    return (not v) or bool(PLACEHOLDER.search(v)) or v in ("-", "待填", "未定", "无")


def profile_letter(cfg):
    """档位首字母（A/B/C）。读不到按 C 处理，但 G05 会因「档位无声明」降级为 WARN。"""
    prof = str(cfg.get("profile", "") or "").strip().upper()
    return prof[0] if prof[:1] in ("A", "B", "C") else ""


def is_quick(cfg):
    return str(cfg.get("mode", "") or "").strip().lower() in ("quick", "fast", "vibe", "快速")


def guidance_state(cfg):
    """指导模式开关（v5.0.0）。缺省 = `on`（默认开）；**只认 `off` 才关**。

    大小写与首尾空白不敏感（`OFF` / ` off ` 一样算关）——写的人意图明确，不该因为
    多敲一个空格就不算数。反过来，取值写成**别的词**（`false`/`0`/`关闭`/拼错）一律按
    `on` 处理——**宁可靠误报提醒人去改配置，也不要因为拼错一个词就静默把整层指导关掉**
    （"关掉"与"查过且干净"是两件事）。

    为什么开关必须在这里被执行：`config.json` 里写着一个没人读的字段，就是
    `注意事项 40` 与 v4.4.0 那条"假保护"教训的同一个坑——**文档承诺了、代码没落**。
    """
    v = str(cfg.get("guidance", "") or "").strip().lower()
    return "off" if v == "off" else "on"


def walk_files(base):
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            yield os.path.join(dirpath, fn)


def discover_cards(ps):
    """卡卷宗：需求卡/card-NNN/card.md。返回 [(dir, path, fm, text)]。"""
    out = []
    base = os.path.join(ps, "需求卡")
    if not os.path.isdir(base):
        return out
    for name in sorted(os.listdir(base)):
        d = os.path.join(base, name)
        md = os.path.join(d, "card.md")
        if os.path.isdir(d) and os.path.isfile(md):
            text = read(md)
            out.append((d, md, parse_frontmatter(text), text))
    return out


def card_dirs(ps):
    base = os.path.join(ps, "需求卡")
    if not os.path.isdir(base):
        return set()
    return {n for n in os.listdir(base) if re.fullmatch(r"card-\d+", n)
            and os.path.isdir(os.path.join(base, n))}


def work_started(root, cfg, cards):
    """是否已有「开工痕迹」：有非 todo 的卡，或 代码区 里已有文件。

    刚初始化的项目与"该建卡却没建"在文件层面长得一样——**没开工就不做诱导性判定**，
    只记 INFO，否则每个新项目第一次体检都会被自己的模板吓一跳（实测：框架仓库自己命中）。
    """
    for _, _, fm, _ in cards:
        if fm.get("status", "") in CARD_STATUS_STARTED:
            return True
    repos = (cfg.get("paths") or {}).get("repos") or ["repos"]
    if isinstance(repos, str):
        repos = [repos]
    for name in repos:
        base = os.path.join(root, str(name).replace("/", os.sep))
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            if filenames:
                return True
    return False


def load_roles(root):
    d = os.path.join(root, "skills")
    if not os.path.isdir(d):
        return set()
    return {n for n in os.listdir(d) if os.path.isdir(os.path.join(d, n))}


# ---------------------------------------------------------------- 各项检查


def check_g01(root, ps, rt):
    """需求未建卡 / 卡引用悬空。"""
    report = rt["report"]
    cards = rt["cards"]
    dirs = rt["card_dirs"]
    started = rt["started"]

    if not dirs:
        if started:
            report.error("G01", "已有开工痕迹（非 todo 的卡或代码区已有文件），但 需求卡/ 下"
                                "一张卡都没有——干活没有卡的承载", rel(root, ps))
        else:
            report.note("G01：尚无需求卡也无开工痕迹（初始化阶段），跳过")
        return

    prd = os.path.join(ps, "契约", "产品需求文档.md")
    if not os.path.isfile(prd):
        report.note("G01 跳过需求覆盖核对：无 契约/产品需求文档.md")
    else:
        reqs = set(REQ_PAT.findall(read(prd)))
        covered = set()
        for _, path, fm, _ in cards:
            covered |= set(REQ_PAT.findall(fm.get("req", "")))
        missing = sorted(reqs - covered)
        if reqs and not covered and started:
            report.error("G01", "PRD 有 %d 条 REQ，但 %d 张卡一张都没承接（= 需求没建卡就开工）"
                         % (len(reqs), len(cards)), rel(root, prd))
        elif missing:
            msg = "%d 条 REQ 无卡承接：%s%s" % (
                len(missing), "、".join(missing[:15]), "（仅列前 15）" if len(missing) > 15 else "")
            if started:
                report.warn("G01", msg, rel(root, prd))
            else:
                report.note("G01（未开工，仅记录）：" + msg)
        else:
            report.note("G01：PRD %d 条 REQ 全部有卡承接" % len(reqs))

    dangling = {}
    for _, path, fm, _ in cards:                       # 卡间依赖：数据，最硬
        for num in CARD_PAT.findall(fm.get("deps", "")):
            if "card-%s" % num not in dirs:
                dangling.setdefault(rel(root, path), set()).add("card-%s" % num)
    tl = os.path.join(ps, "任务清单.md")
    if os.path.isfile(tl):
        for num in CARD_PAT.findall(read(tl)):
            if "card-%s" % num not in dirs:
                dangling.setdefault(rel(root, tl), set()).add("card-%s" % num)
    for where, nums in sorted(dangling.items()):
        report.error("G01", "引用了不存在的卡：%s" % "、".join(sorted(nums)), where)


def check_g02(root, ps, rt):
    """契约未定就开工：已开工的卡引用了不存在的契约。"""
    report = rt["report"]
    quick = rt["quick"]
    n_err = n_warn = 0
    for cdir, path, fm, text in rt["cards"]:
        if fm.get("status", "") not in CARD_STATUS_STARTED:
            continue
        for token in sorted(set(CONTRACT_REF.findall(text))):
            if "xxx" in token or "XXX" in token or PLACEHOLDER.search(token):
                if not quick:
                    report.warn("G02", "契约引用未填实：`%s`" % token, rel(root, path))
                    n_warn += 1
                continue
            target = os.path.join(ps, token.replace("/", os.sep))
            if not os.path.exists(target):
                report.error("G02", "卡已开工但契约不存在：`%s`" % token, rel(root, path))
                n_err += 1
    if not n_err and not n_warn and rt["cards"]:
        report.note("G02：已开工卡引用的契约均存在")


def check_g03(root, ps, rt):
    """置 done 无留痕 / 验收记录空壳。"""
    report = rt["report"]
    hits = 0
    for cdir, path, fm, text in rt["cards"]:
        status = fm.get("status", "")
        if status not in ("done", "reviewing"):
            continue
        body = section_of(text, r"^##\s*进度日志")
        size = substance(body)
        if size < 20:
            report.error("G03", "状态为 %s 但「进度日志」无实质记录（实质 %d 字）："
                                "看不到做了什么、跑了什么命令" % (status, size),
                         rel(root, path))
            hits += 1
        for fn in sorted(os.listdir(cdir)):
            if not (fn.startswith("验收-") and fn.endswith(".md")):
                continue
            vac = substance(read(os.path.join(cdir, fn)))
            if vac < 60:
                report.warn("G03", "验收记录近乎空壳（实质 %d 字）：%s" % (vac, fn),
                            rel(root, os.path.join(cdir, fn)))
                hits += 1
    if not hits and rt["cards"]:
        report.note("G03：已置 done/reviewing 的卡均有进度留痕")


def check_g04(root, ps, rt):
    """待澄清未清零 / 需求确认物缺失。"""
    report = rt["report"]
    for cdir, path, fm, text in rt["cards"]:
        status = fm.get("status", "")
        if status not in CARD_STATUS_STARTED:
            continue
        open_n = as_int(fm.get("clarify_open"), 0)
        if open_n > 0:
            report.error("G04", "clarify_open=%d 未清零却已开工（%s）——判据：待澄清 >0 = 未就绪，"
                                "不得下发开发" % (open_n, status), rel(root, path))
        confirm = fm.get("req_confirm", "")
        confirm_file = os.path.join(cdir, "需求确认-产品经理.md")
        if confirm and not is_placeholder(confirm) and not os.path.isfile(confirm_file):
            report.error("G04", "req_confirm 登记了确认物但文件不存在（假登记）：%s" % confirm,
                         rel(root, path))
        elif not os.path.isfile(confirm_file) and not fm.get("source", "").strip():
            report.warn("G04", "%s 状态但无 需求确认-产品经理.md，也无 source（修复卡）标识"
                        % status, rel(root, path))


def check_g05(root, ps, rt):
    """环境降级：prod 与 dev/test 共用同一目标；验收证据标注 dev 而档位要求 test。"""
    report = rt["report"]
    profile = rt["profile"]
    env_path = os.path.join(ps, "契约", "环境.md")
    if not os.path.isfile(env_path):
        report.note("G05 跳过：无 契约/环境.md，无法判断三环境是否被降级")
        return
    if profile != "C":
        report.note("G05 跳过：档位为 %s（非三环境），不硬套三库条款"
                    % (rt["cfg"].get("profile") or "未声明"))
    else:
        text = read(env_path)
        prod_cols = 0
        for block in iter_tables(text):
            if len(block) < 3:
                continue
            head = [c.lower() for c in cells(block[0])]
            idx = {}
            for key in ("dev", "test", "prod"):
                for i, h in enumerate(head):
                    if key in h:
                        idx[key] = i
                        break
            if len(idx) != 3:
                continue
            prod_cols += 1
            for row in block[2:]:
                rc = cells(row)
                if len(rc) <= idx["prod"]:
                    continue
                name = rc[0]
                pv = rc[idx["prod"]]
                if is_placeholder(pv):
                    continue
                for key in ("dev", "test"):
                    if idx[key] < len(rc) and rc[idx[key]] == pv:
                        report.error(
                            "G05", "prod 与 %s 共用同一目标：「%s」prod=%s（= %s 列 %s）"
                            "——三库强隔离要求端口/库名/DATABASE_URL 互不相同"
                            % (key, name, pv, key, rc[idx[key]]), rel(root, env_path))
            if all(is_placeholder(cells(r)[idx["prod"]])
                   for r in block[2:] if len(cells(r)) > idx["prod"]):
                if rt["started"]:
                    report.warn("G05", "prod 侧全部未声明（%s 表）——当前无法核对三环境是否"
                                       "互不相同（三库强隔离条款要求端口/库名/DATABASE_URL 各自不同）"
                                % cells(block[0])[0], rel(root, env_path))
                else:
                    report.note("G05（未开工，仅记录）：环境.md 的 prod 侧仍是占位未填")
        if prod_cols == 0:
            report.note("G05：环境.md 内未找到含 dev/test/prod 三列的表，无法比对")

    if profile != "C":
        return
    scanned = 0
    for cdir, path, fm, text in rt["cards"]:
        for fn in sorted(os.listdir(cdir)):
            if not (fn.startswith("验收-") and fn.endswith(".md")):
                continue
            p = os.path.join(cdir, fn)
            body = read(p)
            scanned += 1
            m = ENV_LINE.search(body)
            if m:
                report.warn("G05", "验收证据标注执行环境为「%s」而档位为三环境——"
                                   "验收证据只认 test（dev 证据不得顶替）" % m.group(2),
                            rel(root, p))
    tc_base = os.path.join(ps, "测试卡")
    if os.path.isdir(tc_base):
        for name in sorted(os.listdir(tc_base)):
            for fn in ("覆盖矩阵.md", "tc.md"):
                p = os.path.join(tc_base, name, fn)
                if not os.path.isfile(p):
                    continue
                scanned += 1
                m = ENV_LINE.search(read(p))
                if m:
                    report.warn("G05", "测试卡证据标注执行环境为「%s」而档位为三环境"
                                       "（验收证据只认 test）" % m.group(2), rel(root, p))
    if scanned == 0:
        report.note("G05：已开工卡尚无验收/测试卡证据，跳过证据环境核对")


def check_g06(root, ps, rt):
    """变更申请单：审批栏空栏（在飞）或未批准就执行。"""
    report = rt["report"]
    base = os.path.join(ps, "问题", "变更申请单")
    if not os.path.isdir(base):
        report.note("G06 跳过：无 问题/变更申请单/ 目录")
        return
    seen = 0
    for name in sorted(os.listdir(base)):
        if not name.lower().endswith(".md") or "模板" in name or "template" in name.lower():
            continue
        p = os.path.join(base, name)
        if not os.path.isfile(p):
            continue
        seen += 1
        text = read(p)
        approval = section_of(text, CR_APPROVAL_RE)
        trace = section_of(text, CR_TRACE_RE)
        executed = _executed(trace)
        if not has_section(text, CR_APPROVAL_RE):
            # F-42 第二面：「章节不存在」≠「章节存在但没签」，读数必须分形。
            if executed:
                report.error("G06", "变更单据缺「审批栏」章节，但执行留痕已有内容"
                                    "（模板明写「缺任一 → 不得执行」，且无从证明已批准）",
                             rel(root, p))
            else:
                report.warn("G06", "变更单据缺「审批栏」章节（模板 G 段；按章节标题定位、"
                                   "字母可错位，但章节不能没有）——无法判定是否已审批",
                            rel(root, p))
            continue
        signed, timed = _approval_state(approval)
        if not signed and executed:
            report.error("G06", "未批准就执行了：审批栏主控署名空，但执行留痕已有内容"
                                "（模板明写「缺任一 → 不得执行」）", rel(root, p))
        elif not signed:
            report.warn("G06", "变更单据审批栏未签（在飞）", rel(root, p))
        elif not timed:
            report.warn("G06", "审批栏已署名但未记时间（不合格示例 = 空栏）", rel(root, p))
    if seen == 0:
        report.note("G06：问题/变更申请单/ 下暂无实单")


def _approval_state(approval_text):
    """返回 (主控已署名, 署名行有时间)。"""
    signed = timed = False
    for ln in approval_text.splitlines():
        if not ln.strip().startswith("|"):
            continue
        c = cells(ln)
        if len(c) < 4:
            continue
        role = c[0]
        if "主控" not in role:
            continue
        sign = c[2] if len(c) > 2 else ""
        if sign and not PLACEHOLDER.search(sign) and not is_placeholder(sign):
            signed = True
            tm = c[3] if len(c) > 3 else ""
            timed = bool(tm) and not is_placeholder(tm)
    return signed, timed


def _executed(trace_text):
    """执行留痕是否已回填：执行人一行有非占位内容。"""
    for ln in trace_text.splitlines():
        if "执行人" not in ln:
            continue
        if PLACEHOLDER.search(ln):
            return False
        val = ln.split("执行人", 1)[1]
        return substance(val) > 0
    return False


def is_sandbox_mirror(tmp: str, path: str) -> bool:
    """F-32 裁决 A：`path` 是否位于「**声明的沙箱根**」之下。

    沙箱根判据（三条**同时**成立，刻意从严——豁免面越宽，G07 越接近死代码）：
    1. 它在 `tmp/` **下一层**（即 `tmp/<name>`，不递归认更深层的目录，
       避免 `tmp/随便一个目录/...` 也被豁免）；
    2. 目录名以 `_` 开头 —— 这是本框架 `tmp/` 内**隔离根/探针现场**的既有命名习惯；
    3. 目录名**不是** `_YYYYMMDD_*` 形态 —— 那种是**盘点/探针的一次性散落件**（`TEMP_DATED` 纪律），
       它出现在 tmp 根本身就是 G07 要拦的对象，**不得**借豁免洗白。

    命中豁免的条目**仍逐条 INFO 列出**（不静默），以免豁免本身成为新的盲区。
    """
    try:
        rp = os.path.relpath(path, tmp)
    except ValueError:
        return False
    parts = rp.replace("\\", "/").split("/")
    if len(parts) < 2:            # 必须在 tmp 的**子目录**内
        return False
    top = parts[0]
    if not top.startswith("_"):
        return False
    if re.match(r"^_\d{8}[_-]", top):   # `_20260918_login_probe` 之类：一次性散落件，不豁免
        return False
    return True


def check_g07(root, ps, rt, max_mb):
    """证据落错地方：tmp 内疑似证据；卡卷宗超阈值大文件。

    F-32 裁决（`R73`，2026-09-18）：原实现把「tmp 镜像产品产出」与「证据落错地方」
    **判成同一件事**——`EVIDENCE_HINTS` 里的「报告」是 patent-search 这个 skill 的**既有产出名**
    （`data/outputs/patent-search/专利检索报告.md`），隔离沙箱越**忠实**地镜像真实运行，
    越**必然**撞红。于是「隔离沙箱」这一正确取证技术被自己的判据惩罚（惩罚忠实、奖励失真）。

    **裁决取 A：给沙箱镜像加豁免，但不削弱对真·落错地方的检出**——
    - 豁免仅覆盖「**声明的沙箱根**」下的内容：`tmp/` 下一层、名字以 `_` 开头且**不是** `_YYYYMMDD_*`
      命名的目录（即盘点/探针自造的隔离根，如 `tmp/_r72_i_iso`），且其内部命中项**降为 INFO**；
    - `tmp/` 下**其余**位置（含 `tmp/` 根自身、以及 `tmp/_20260918_*.out` 这类散落件）
      **照旧 WARN**——即「真把验收报告落在 tmp 里」仍会被拦；
    - 豁免根内的命中**逐条列出**（不静默），避免“看不见的豁免”变成新的盲区。
    """
    report = rt["report"]
    tmp = rt["tmp"]
    if tmp and os.path.isdir(tmp):
        found = []
        exempt = []
        for dirpath, dirnames, filenames in os.walk(tmp):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for nm in list(dirnames) + filenames:
                if any(h in nm for h in EVIDENCE_HINTS):
                    full = os.path.join(dirpath, nm)
                    (exempt if is_sandbox_mirror(tmp, full) else found).append(rel(root, full))
        if found:
            report.warn("G07", "tmp 内 %d 项疑似证据/卡资料（tmp 纪律：可能是证据落错地方，"
                               "也可能是在飞任务——确认后归位）：%s"
                        % (len(found), "、".join(sorted(found)[:8])))
        if exempt:
            report.note("G07：%d 项位于**声明的沙箱根**内（镜像产品产出名，非落错地方；"
                        "F-32 裁决 A 豁免）：%s" % (len(exempt), "、".join(sorted(exempt)[:8])))
    for cdir, path, fm, text in rt["cards"]:
        for fp in walk_files(cdir):
            try:
                size = os.path.getsize(fp)
            except OSError:
                continue
            if size > max_mb * 1024 * 1024:
                report.warn("G07", "卷宗内 %.1fMB 文件超过 %.0fMB 阈值（大产物应留 产物/ 只放指针）"
                            % (size / 1048576.0, max_mb), rel(root, fp))


def check_g08(root, ps, rt):
    """卡卷宗混入禁入物：代码源文件 / 构建产物 / 压缩包。"""
    report = rt["report"]
    hits = 0
    for cdir, path, fm, text in rt["cards"]:
        # 刻意**不剪 BUILD_DIRS**：剪了就等于把「卷宗混入 node_modules」这条判据变成死代码
        # （回归测试抓过：node_modules 被 SKIP_DIRS 剪掉，检查永远不响）。
        for dirpath, dirnames, filenames in os.walk(cdir):
            dirnames[:] = [d for d in dirnames if d not in {".git", ".idea", ".vscode"}]
            for d in list(dirnames):
                if d in BUILD_DIRS:
                    report.error("G08", "卷宗内含构建产物/依赖目录 `%s`"
                                        "（模板：卷宗只放记录与证据）" % d,
                                 rel(root, os.path.join(dirpath, d)))
                    hits += 1
                    dirnames.remove(d)
            for fn in filenames:
                fp = os.path.join(dirpath, fn)
                suf = os.path.splitext(fn)[1].lower()
                if suf in CODE_SUFFIX:
                    report.error("G08", "卷宗内出现代码源文件 `%s`（代码在 repos/，此处只放指针）"
                                % fn, rel(root, fp))
                    hits += 1
                elif suf in ARCHIVE_SUFFIX:
                    report.error("G08", "卷宗内出现二进制/压缩包 `%s`" % fn, rel(root, fp))
                    hits += 1
                elif suf in DOC_SUFFIX:
                    report.warn("G08", "卷宗内出现交付类文档 `%s`（确认是否该进 交付/）" % fn,
                                rel(root, fp))
                    hits += 1
    if not hits and rt["cards"]:
        report.note("G08：卡卷宗无禁入物")


def check_g09(root, ps, rt):
    """卡无归属人 / 状态非法 / 编号不符。"""
    report = rt["report"]
    roles = rt["roles"]
    for cdir, path, fm, text in rt["cards"]:
        where = rel(root, path)
        status = fm.get("status", "")
        if status not in LEGAL_STATUS:
            report.error("G09", "status 非法：%r（合法值：%s）"
                         % (status, "/".join(sorted(LEGAL_STATUS))), where)
        assignee = fm.get("assignee", "").strip()
        if not assignee:
            report.error("G09", "assignee 为空（无归属人）", where)
        elif roles and assignee not in roles:
            report.error("G09", "assignee「%s」不是启用角色（skills/ 下无此角色）" % assignee, where)
        cid = fm.get("id", "").strip()
        dname = os.path.basename(cdir)
        if cid and cid != dname:
            report.warn("G09", "目录名 %s 与 frontmatter id %s 不一致" % (dname, cid), where)
        if not fm.get("req", "").strip() and not fm.get("source", "").strip():
            report.warn("G09", "req 与 source 都为空（追溯链断：既非需求卡也非修复卡）", where)


def check_g10(root, ps, rt):
    """有卡无落盘：角色有在办卡但从未产出进度。"""
    report = rt["report"]
    prog = os.path.join(ps, "进度")
    if not os.path.isdir(prog):
        report.note("G10 跳过：无 进度/ 目录")
        return
    have = {os.path.splitext(n)[0] for n in os.listdir(prog) if n.endswith(".json")}
    busy = {}
    for cdir, path, fm, text in rt["cards"]:
        if fm.get("status", "") in CARD_STATUS_STARTED and fm.get("assignee"):
            busy.setdefault(fm["assignee"].strip(), []).append(os.path.basename(cdir))
    for role, cards_ in sorted(busy.items()):
        if role not in have:
            report.warn("G10", "角色「%s」有 %d 张在办卡（%s）但 进度/%s.json 不存在"
                               "——角色产出落盘机制：看不到就该怀疑它没真正参与"
                        % (role, len(cards_), "、".join(sorted(cards_)[:5]), role))
    if "overall" not in have and any(
            fm.get("status", "") in ("done", "reviewing") for _, _, fm, _ in rt["cards"]):
        report.warn("G10", "已有卡进入 done/reviewing 但 进度/overall.json 不存在（项目 GO 汇总缺失）")


# ---------------------------------------------------------------- 自增强回路（G11）


def _cr_exists(ps, no):
    """`CR-YYYYMMDD-NNN` 是否真有对应单据（文件名以单号开头且后一位不是数字）。"""
    base = os.path.join(ps, CR_DIR_REL)
    if not os.path.isdir(base):
        return False
    for name in sorted(os.listdir(base)):
        if not name.lower().endswith(".md"):
            continue
        if name.startswith(no) and not name[len(no):len(no) + 1].isdigit():
            return True
    return False


def check_g11(root, ps, rt):
    """发现有没有沉淀成检查点：指导发现台账的归属完整性。

    G01–G10 抓「机制被绕过」，G11 抓**「抓到的绕过有没有变成下一个检查点」**——没有这一条，
    一条新绕过只会停在某个人的记忆里，同一个坑下次照踩。这就是「踩坑即加规则」
    （注意事项 20①）在**指导模式自己身上**的落点，也是指导用户与自增强的接缝。

    判据不新造：沿用测试卡纪律②「发现项零归属 = 未收口」。归属认三类**真实存在**的东西，
    因为"能抓它"的既可能是本脚本的号，也可能是别的工具，还可能是要动结构的变更：
      · 归属栏空 / 只写「-」          → ERROR（零归属）
      · 归属到 CATALOG 里没有的检查号 → ERROR（假登记：登记了，但没有这个检查点）
      · 归属到不存在的变更单          → ERROR（假登记）
      · 归属到 scripts/ 下没有的工具   → ERROR（假登记）
      · 归属栏三类都没有              → ERROR（写了个无法复核的东西）
      · 台账本身不存在                → WARN（回路没入口，不是「没有绕过」；快速模式降为 INFO）
      · 台账在但一行没有              → INFO（**不等于没有绕过**，只说明尚无登记）

    **发现号形态闸门（F-41）**：台账正文里的**任意**管道表都可能凑够 6 列，早期版本只按
    「以 `|` 开头且 ≥6 单元格」判定，于是把 `F-40` 处置节的 CSS 像素实测表读成了 5 条发现
    （编号被读成 `4`/`16`/`32`/`64`/短名 `a.png`），产出 5 条与台账内容完全无关的 ERROR。
    现要求编号栏必须**形如发现号**（`F-` / `FIND-` + 数字）才计入——发现号的形态是台账自己
    的约定，用它当闸门，`指导发现台账.md` 里所有数据表（编号栏是数字或文件名）自然被排除。
    另：**未登记的发现号形态行**（如正文里引用 `| F-99 | … |` 演示）会报 WARN 留痕，
    防止「形态对但根本没登记」的行静默变成盲区。
    """
    report = rt["report"]
    path = os.path.join(ps, LEDGER_REL)
    where = rel(root, path)
    if not os.path.isfile(path):
        msg = ("没有指导发现台账——**自增强回路没有入口**：靠人或用户发现的绕过无处登记，"
               "G01–G11 的清单就永远不会变长"
               "（补建：`python scripts/migrate.py . --only guidance-ledger --apply`；"
               "注意**没有台账 ≠ 没有绕过**，只说明没人登记）")
        if rt.get("quick"):
            # 快速模式的降噪规则（指导手册 §3）：流程类只提醒不拦截。台账缺失是"还没来得及
            # 建"，不是"建了却写了个查不到的号"——后者下面照样报 ERROR。
            report.note("G11：" + msg + "（快速模式：流程类只提醒）")
        else:
            report.warn("G11", msg, where)
        return
    rows = 0
    for ln in read(path).splitlines():
        if not ln.strip().startswith("|"):
            continue
        c = cells(ln)
        if len(c) >= 7:
            # v5.24.0 七列：编号｜发现日期｜触发原话（含会话指针）｜摩擦类型｜能否抓到｜归属｜状态
            no, _date, _quote, friction, catchable, owner, _state = c[:7]
            legacy = False
        elif len(c) >= 6:
            # v5.1.0 六列（老台账，第 3 列是「场景」）——**列数自适应**，老项目不迁移也能跑
            no, _date, _scene, catchable, owner, _state = c[:6]
            friction = ""
            legacy = True
        else:
            continue
        if is_placeholder(no) or "编号" in no or not re.search(r"[0-9A-Za-z]", no):
            continue                      # 表头 / 分隔行 / 未填模板行
        if not FIND_NO_PAT.match(no):
            # 形态不符 ⇒ 不是发现行，是正文里的**数据表**（F-41：6 列宽度实测表曾在此被
            # 整个读成 5 条发现）。这里只留痕不报错——否则「台账里放表格」又挨一记假红。
            report.note("G11：台账里有 %d 列表格行 `%s` 编号栏不是发现号形态（`F-` 数字），"
                        "按数据表跳过（不参与归属校验）" % (len(c), no))
            continue
        rows += 1
        if catchable not in ("是", "否"):
            report.warn("G11", "台账 %s 的「现有检查能否抓到」填的是「%s」，"
                               "既非「是」也非「否」——无法复核" % (no, catchable), where)
        if not legacy:
            ftype = friction.replace("`", "").replace("*", "").strip()
            if not ftype:
                report.warn("G11", "发现 %s 的「摩擦类型」空着——七列台账要求五选一"
                                   "（绕过/不合用/没写清/默认值错/验收太晚）；空着说明它还是"
                                   "草稿、没在复盘里转正" % no, where)
            elif ftype not in FRICTION_TYPES:
                report.warn("G11", "发现 %s 的「摩擦类型」填的是「%s」，不在五选一里"
                                   "（绕过/不合用/没写清/默认值错/验收太晚）——"
                                   "分类错了就没法按类型聚类成该改的条款" % (no, ftype), where)
        if is_placeholder(owner):
            report.error("G11", "发现 %s 零归属：既没指向已有检查号，也没指向变更单"
                                "（零归属 = 未收口。补一个真实检查号；或按"
                                " skills/项目经理/指导手册.md §7 把发现升成新检查号）"
                         % no, where)
            continue
        gids = sorted(set(re.findall(r"\bG\d{2}\b", owner)))
        crs = sorted(set(CR_NO_PAT.findall(owner)))
        tools = sorted(set(TOOL_PAT.findall(owner)))
        bad_g = [g for g in gids if g not in CATALOG]
        bad_cr = [c for c in crs if not _cr_exists(ps, c)]
        bad_tool = [t for t in tools if not os.path.isfile(os.path.join(FRAMEWORK_ROOT, "scripts", t))]
        if bad_g:
            report.error("G11", "发现 %s 归属到**不存在的检查号** %s（假登记：登记了，但脚本里"
                                "没有这个检查点，等于没归属；要么改成真实检查号，要么按手册 §7 "
                                "新增该号并把发现改指向它）" % (no, "、".join(bad_g)), where)
        if bad_cr:
            report.error("G11", "发现 %s 归属到**不存在的变更单** %s（假登记："
                                "`project-state/问题/变更申请单/` 下没有此单）"
                         % (no, "、".join(bad_cr)), where)
        if bad_tool:
            report.error("G11", "发现 %s 归属到**不存在的检查工具** %s（假登记：`scripts/` 下"
                                "没有这个脚本）" % (no, "、".join(bad_tool)), where)
        if not gids and not crs and not tools:
            report.error("G11", "发现 %s 的归属栏里没有可识别的号——要写检查号（如 `G05`）、"
                                "变更单号（`CR-YYYYMMDD-NNN`）或检查工具脚本名"
                                "（如 `path_audit.py`），否则无法复核" % no, where)
    if rows == 0:
        report.note("G11：指导发现台账已就位但没有登记行——**这不等于没有绕过**，"
                    "只说明至今没有「G01–G11 之外」的新发现被记下来")


# ---------------------------------------------------------------- 覆盖面声明（输出）


def coverage_block(ids=None):
    """覆盖声明（文本行）。**每次输出都带**——结论脱离范围就没有意义。"""
    ids = sorted(ids or CATALOG)
    span = ("%s–%s 这 %d 类" % (ids[0], ids[-1], len(ids))) if len(ids) > 1 \
        else ("%s 这 1 类" % ids[0])
    L = ["覆盖面与盲区（**结论只在这个范围内成立，必须跟结论一起读**）：",
         "  覆盖：%s**已知**的「机制被绕过」痕迹。" % span,
         "  **不覆盖**："]
    for dim, why in COVERAGE_UNCOVERED:
        L.append("    · %s —— %s" % (dim, why))
    L.append("  → 「上面这 %d 类干净」成立；「环境没问题」「没有绕过」**不成立**。" % len(ids))
    return L


def coverage_dict(ids=None):
    """覆盖声明（JSON 用），与文本同源，避免两处各说各话。"""
    ids = sorted(ids or CATALOG)
    return {"covered": ids,
            "uncovered": [{"dimension": d, "why": w} for d, w in COVERAGE_UNCOVERED]}


# ---------------------------------------------------------------- 自检配对

MANUAL = os.path.join(FRAMEWORK_ROOT, "skills", "项目经理", "指导手册.md")
REGRESSION = os.path.join(FRAMEWORK_ROOT, "scripts", "tests", "test_guide_audit.py")


def selfcheck():
    """校验**三方配对**：脚本 CATALOG ↔ 指导手册处方 ↔ 回归用例。

    配对（脚本↔手册）只管「查得出问题要有人知道怎么修」；再加上回归这一方，才管得住
    「新增/修改检查号没人拦」——一次 `--selfcheck` 就是指导模式自增强的最小闸门：
    要加 G12，手册得先写处方、用例得先有断言，三方缺一即报。
    """
    print("指导手册配对自检：%s" % MANUAL)
    if not os.path.isfile(MANUAL):
        print("[ERROR] 指导手册不存在——检查号查得出问题，但没人知道怎么修")
        return 1
    text = read(MANUAL)
    ids = sorted(CATALOG)
    # 锚点用**小节标题**（`## GNN`），不用"首次出现"：正文里一句「结论只在 G01–G11 内成立」
    # 就会让 G01 的"首次出现"落在页首，量出 4 个字符 → 误报"处方过短"。
    # 越是想说清覆盖面，越会提到号——所以判据必须是"有没有这一小节"，而不是"哪里提过一次"。
    pos = {}
    for cid in ids:
        m = re.search(r"(?m)^#{2,3}\s*%s\b" % cid, text)
        if m:
            pos[cid] = m.start()
    bad = [cid for cid in ids if cid not in pos]
    if bad:
        print("[ERROR] 指导手册缺这些检查号的处方小节（`## GNN`）：%s" % "、".join(bad))
    order = sorted(pos.items(), key=lambda kv: kv[1])
    thin = []
    for cid, start in order:
        # 到**下一个任意级别的 `## ` 标题**为止（不只是下一个检查号小节）：
        # 否则最后一个检查号会把后面的说明章节也算进自己的"处方长度"，虚胖过关。
        nxt = re.search(r"(?m)^##\s", text[start + 1:])
        end = start + 1 + nxt.start() if nxt else len(text)
        if len(re.sub(r"\s+", "", text[start:end])) < 120:
            thin.append(cid)
    if thin:
        print("[WARN] 这些检查号的处方过短（<120 实质字），疑似占位：%s" % "、".join(thin))
    extra = sorted(set(re.findall(r"\bG\d{2}\b", text)) - set(ids))
    if extra:
        print("[WARN] 指导手册里有脚本未实现的检查号：%s" % "、".join(extra))
    # 第三方：回归用例。判据故意用「弱代理」——只查号有没有在用例里出现过。
    # 为什么敢用 ERROR：**一个新检查号在用例里零出现，一定没有断言**（真有断言必然提及），
    # 这个方向不会误报；弱的是反向（提了一句注释里的号也能过），那由人复核，不在这里冒充。
    if not os.path.isfile(REGRESSION):
        print("[ERROR] 回归用例不存在：%s——新增检查号必须配断言，否则没人拦得住它悄悄失效"
              % REGRESSION)
        return 1
    rtext = read(REGRESSION)
    untested = [cid for cid in ids if not re.search(r"\b%s\b" % cid, rtext)]
    if untested:
        print("[ERROR] 这些检查号在回归用例 %s 里零出现（= 没有断言，改了也没人拦）：%s"
              % (os.path.basename(REGRESSION), "、".join(untested)))
    # 覆盖声明（COVERAGE_UNCOVERED）是唯一事实源，手册 §5 负责解释"为什么"与"真想查怎么办"——
    # 两处一旦漂移，就成了"脚本念的盲区"和"手册教的盲区"不是同一份的假保护。
    missdim = [d for d, _ in COVERAGE_UNCOVERED if d not in text]
    if missdim:
        print("[ERROR] 指导手册没解释这些覆盖盲区（脚本正按 COVERAGE_UNCOVERED 输出）：%s"
              % "、".join(missdim))
    if bad or untested or missdim:
        return 1
    print("三方配对完整：%d 个检查号均有处方+回归提及；%d 条覆盖盲区均有解释%s"
          % (len(ids), len(COVERAGE_UNCOVERED),
             "（含 %d 项处方过短）" % len(thin) if thin else ""))
    return 0


# ---------------------------------------------------------------- 主流程


def build_ctx(root, report):
    ps = os.path.join(root, "project-state")
    cfg = {}
    cfg_path = os.path.join(ps, "config.json")
    if os.path.isfile(cfg_path):
        try:
            cfg = json.loads(read(cfg_path))
        except Exception as exc:           # noqa: BLE001 - 配置坏了要指路，不能崩
            report.note("config.json 解析失败（%s）——路径/档位按默认推断" % exc)
    paths = cfg.get("paths") or {}
    tmp_name = paths.get("tmp") or "tmp"
    cards = discover_cards(ps)
    return {
        "ps": ps, "cfg": cfg, "report": report,
        "cards": cards,
        "card_dirs": card_dirs(ps),
        "roles": load_roles(root),
        "profile": profile_letter(cfg),
        "quick": is_quick(cfg),
        "guidance": guidance_state(cfg),
        "started": work_started(root, cfg, cards),
        "tmp": os.path.join(root, str(tmp_name).replace("/", os.sep)),
    }


def parse_args(argv):
    root = os.getcwd()
    opts = {"json": False, "write": False, "max_mb": 5.0, "checks": [], "selfcheck": False,
            "force": False}
    for a in argv[1:]:
        if a == "--json":
            opts["json"] = True
        elif a == "--write":
            opts["write"] = True
        elif a == "--selfcheck":
            opts["selfcheck"] = True
        elif a == "--force":
            opts["force"] = True
        elif a == "--check":
            opts["_need_check"] = True
        elif a == "--max-mb":
            opts["_need_mb"] = True
        elif a in ("-h", "--help"):
            sys.stdout.write(__doc__)
            return None, 0
        elif a.startswith("-"):
            sys.stderr.write("未知参数：%s\n" % a)
            return None, 2
        elif opts.pop("_need_check", False):
            opts["checks"].append(a.strip().upper())
        elif opts.pop("_need_mb", False):
            try:
                opts["max_mb"] = float(a)
            except ValueError:
                sys.stderr.write("--max-mb 需要数字：%s\n" % a)
                return None, 2
        else:
            root = a
    if opts.pop("_need_check", False) or opts.pop("_need_mb", False):
        sys.stderr.write("--check/--max-mb 后面缺参数\n")
        return None, 2
    return os.path.abspath(root), opts


def main(argv):
    _reconfigure()
    root, opts = parse_args(argv)
    if root is None:
        return opts

    if opts["selfcheck"]:
        return selfcheck()

    ps = os.path.join(root, "project-state")
    if not os.path.isdir(ps):
        sys.stderr.write("不是框架工作区（缺 project-state/）：%s\n" % root)
        return 2

    report = Report()
    ctx = build_ctx(root, report)

    if ctx["guidance"] == "off" and not opts["force"]:
        # **不输出"未发现问题"**——没跑 ≠ 干净。JSON 里 ok=None（既非 true 也非 false），
        # 让消费方无法把"被关掉"误读成"通过了"。
        msg = ("指导模式已关闭（config.json 的 `guidance` = \"off\"）——**本次未做任何检查**，"
               "不得据此认为「没有问题」（「关掉」与「查过且干净」是两件事）。\n"
               "要打开：把 config.json 的 `guidance` 改成 \"on\"；"
               "只想跑一次而不改开关：加 `--force`。\n"
               "关闭须在项目计划注明依据。")
        if opts["json"]:
            sys.stdout.write(json.dumps(
                {"root": root, "guidance": "off", "skipped": "guidance-off", "ok": None,
                 "counts": {ERROR: 0, WARN: 0},
                 # covered 为空是**有意的**：机器消费方若把 ok:None 当通过，
                 # 至少还能看到 "covered": [] —— 一行都没查。
                 "coverage": {"covered": [],
                              "uncovered": [{"dimension": d, "why": w}
                                            for d, w in COVERAGE_UNCOVERED]},
                 "findings": [], "notes": [msg]},
                ensure_ascii=False, indent=2) + "\n")
        else:
            sys.stdout.write("指导体检（v5.1.3）— %s\n%s\n" % (root, msg))
        return 0

    check_g01(root, ps, ctx)
    check_g02(root, ps, ctx)
    check_g03(root, ps, ctx)
    check_g04(root, ps, ctx)
    check_g05(root, ps, ctx)
    check_g06(root, ps, ctx)
    check_g07(root, ps, ctx, opts["max_mb"])
    check_g08(root, ps, ctx)
    check_g09(root, ps, ctx)
    check_g10(root, ps, ctx)
    check_g11(root, ps, ctx)

    findings = report.findings
    if opts["checks"]:
        unknown = [c for c in opts["checks"] if c not in CATALOG]
        if unknown:
            sys.stderr.write("未知检查号：%s（可用：%s）\n"
                             % ("、".join(unknown), "、".join(sorted(CATALOG))))
            return 2
        findings = [f for f in findings if f.check in opts["checks"]]
    n_err = len([f for f in findings if f.level == ERROR])
    n_warn = len([f for f in findings if f.level == WARN])
    # 覆盖声明的范围跟着**实际看的范围**走：`--check G05` 的结论只在 G05 内成立，
    # 这时说"G01–G11 内干净"反而是新的假话。
    scope_ids = sorted(opts["checks"] or CATALOG)

    if opts["json"]:
        out = {"root": root, "ok": n_err == 0,
               "guidance": ctx["guidance"],
               "counts": {ERROR: n_err, WARN: n_warn},
               "coverage": coverage_dict(scope_ids),
               "findings": [f.as_dict() for f in findings], "notes": report.infos}
        sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    else:
        mode = ("快速模式" if ctx["quick"] else "完整模式") + \
               " / 档位 %s" % (ctx["cfg"].get("profile") or "未声明") + \
               " / 指导模式 %s" % ctx["guidance"]
        sys.stdout.write("指导体检（v5.1.3）— %s\n%s / 卡 %d 张\n\n"
                         % (root, mode, len(ctx["cards"])))
        for note in report.infos:
            sys.stdout.write("[INFO] %s\n" % note)
        if report.infos:
            sys.stdout.write("\n")
        if not findings:
            # **不再写光秃秃的「未发现痕迹」**：范围必须同句给出（见 coverage_block）。
            sys.stdout.write("未发现「机制被绕过」的痕迹（范围见下方覆盖声明）。\n")
        for f in sorted(findings, key=lambda x: (_LEVEL_ORDER[x.level], x.check)):
            where = ("（%s）" % f.where) if f.where else ""
            sys.stdout.write("[%s] %s %s｜%s%s\n" % (f.level, f.check, CATALOG[f.check][0],
                                                     f.message, where))
            if f.level == ERROR:
                sys.stdout.write("      处置：%s　责任人：%s\n" % (f.fix, f.owner))
        sys.stdout.write("\n指导体检结论：ERROR %d / WARN %d  —  %s\n"
                         % (n_err, n_warn,
                            "范围内无可拦截问题（**不含**未覆盖维度）" if n_err == 0
                            else "存在 ERROR，按 skills/项目经理/指导手册.md 对应条目处置"))
        sys.stdout.write("\n")
        for line in coverage_block(scope_ids):
            sys.stdout.write("%s\n" % line)

    if opts["write"]:
        path = write_report(root, ctx, findings, n_err, n_warn)
        if not opts["json"]:
            sys.stdout.write("\n已写入：%s\n" % rel(root, path))
    return 0 if n_err == 0 else 1


def write_report(root, ctx, findings, n_err, n_warn):
    ps = ctx["ps"]
    today = _dt.date.today().isoformat()
    path = os.path.join(ps, "指导体检-%s.md" % today)
    L = ["# 指导体检-%s" % today, "",
         "> `scripts/guide_audit.py` 产出（只报告不改动）。处置动作见 "
         "`skills/项目经理/指导手册.md` 对应条目；**改什么、要不要改、由谁改仍归主控与用户**。",
         "", "- 模式：%s / 档位 %s" % ("快速" if ctx["quick"] else "完整",
                                      ctx["cfg"].get("profile") or "未声明"),
         "- 卡数：%d" % len(ctx["cards"]),
         "- 结论：ERROR %d / WARN %d" % (n_err, n_warn), ""]
    if findings:
        L += ["| 级别 | 检查 | 症状 | 发现 | 位置 | 处置 | 责任人 |",
              "|---|---|---|---|---|---|---|"]
        for f in sorted(findings, key=lambda x: (_LEVEL_ORDER[x.level], x.check)):
            L.append("| %s | %s | %s | %s | `%s` | %s | %s |"
                     % (f.level, f.check, CATALOG[f.check][0],
                        f.message.replace("|", "\\|"), f.where, f.fix, f.owner))
    else:
        L.append("未发现「机制被绕过」的痕迹（范围见下方覆盖声明）。")
    L.append("")
    # 落盘的报告是**脱离上下文被人读**的，覆盖声明在这里比在终端上更要紧。
    L += ["## 覆盖面与盲区", "",
          "> 结论**只在下述范围内成立**。这一段不是免责声明——"
          "它区分的是「这 %d 类查过且干净」与「从未查过」（通用铁律②）。" % len(CATALOG), ""]
    for line in coverage_block():
        L.append(line.strip() if line.startswith("  ") else line)
    L.append("")
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(L))
    return path


if __name__ == "__main__":
    sys.exit(main(sys.argv))

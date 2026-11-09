#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""框架自检 / 项目体检（framework_lint.py）——把「手抄登记表必然漂移」变成机器可查。

用法：
    python scripts/framework_lint.py [根目录] [--project|--framework] [--json] [--quiet]

根目录 = 含 `agent.md` + `project-state/config.json` 的目录。
不给参数则取当前目录。退出码：0 = 无 ERROR；1 = 有 ERROR；2 = 用法/工程根错误。

**两档模式（v4.9.0）**——默认自动判别，可显式覆盖：
    框架自查（framework）框架本体仓库，按严格标准查
    项目体检（project）  真实项目实例，`config.json` 的 `project_name` 非空即判为项目
    `--project` / `--framework` 强制指定；判别器与 `git_audit.py` 找真实工作区用的是同一个字段
**为什么要分档（2026-09-15 实测教训）**：框架自检在框架本体跑出 ERROR 0，同一份脚本指向
真实项目却 46 ERROR / 直接崩溃——**工具在框架自己身上是干净的，说明它从来没被真实项目检验过**。
分档是为了让「拿真实项目跑」变成常规动作，而不是每次都要手工解释一堆框架专属要求。

检查项（对应 agent.md 注意事项 27「扩角色同步清单」第 9 条）：
    A 版本一致性       —— agent.md 版本 vs 各角色 SKILL / 声明「随框架同步」的模板文件
    B 编号连续性       —— 验收标准 / 注意事项 / 任务执行纪律 / 完成定义 §9.N 无缺号重号
                          （清单可住在契约文件或内联在 agent.md，两者都认；两处都没有才报错）
    C 引用存在性       —— 文中 `scripts/*.py`、`skills/<角色>/SKILL.md` 引用真的存在
                          （**多仓 + 外部根解析**，v4.9.0：见 build_ref_index）
    D 注册表一致       —— config.json 的 paths 顶层与 layout 八域一一对应；schema_version 必须可识别
    E 单一事实源       —— 协作规范 §4 不得回长出第二份验收清单副本；派发简报三段骨架顺序不得被打乱（v4.8.0）
    F 角色同步完整性   —— 12 角色的 SKILL / 进度 json / 上下文索引登记齐备
    G 交叉引用有效性   —— 「注意事项 N」「验收标准 N」「某 SKILL §N」指向真实存在的条目
    H 登记↔磁盘        —— 上下文索引登记的文件真的在磁盘上（`【运行】` 类除外，索引有而磁盘无 = 假通过）
    I 运行模式声明     —— `mode` 与 `profile` 自洽、`full` 有入口凭据（v5.3.0：默认快速模式）
    J 版本漂移         —— 项目 agent.md 版本 vs 框架源仓库版本（需 config.json 的 `framework_source`）
    K 极速修复         —— `project-state/问题/极速修复台账.md` 的 machine:turbo 机读块自洽性（v5.12.0）
                          （12 列机读块；`basis` = 改动依据，须指向既有需求卡/测试卡或写 `no-card`；
                          `pack` = 面向客户的快速体验包的项目根相对路径，`-` = 本轮未涉及）
    N 工作区地图对账   —— 以 `scripts/workspace_map.py` 为**唯一对账引擎**的严格档（v5.17.0）：登记缺失/
                          契约域未登记/顶层游离件 = ERROR，占位符登记未物化 = WARN；H 组保留为保守 WARN 子集
                          （判据同源不矛盾：N ⊇ H；双引擎各说各话的病从根上避免——对账逻辑只活在 workspace_map 一处）

**为什么需要它**：框架靠"就地追加 + 手工登记副本"长大，副本（验收清单、结构树、上下文索引、
config.paths、版本号）散在多个文件里，改一处漏一处是必然（v3.19.0~v4.4.0 的验收清单副本
静默落后两条线、config.paths 只登记 5 项而 layout 声明 8 项，都是这个根因）。
**能自动查的就不该靠人记**——本脚本是该纪律的执行体。

**为什么 v4.9.0 大改（三条都是从真实项目反推出来的）**：
1. **旧 schema 必须报错而不是崩溃**——游戏项目 `paths.layout` 是字符串（v3.12 时代写法），
   `layout.items()` 直接抛 AttributeError，整个体检连一行报告都出不来。体检工具最该有用的
   场合恰恰是"项目很旧"，此时崩溃等于零价值：报「需迁移」并跳过依赖它的检查，其余照查。
2. **引用解析必须认多仓与外部根**——项目是 `repos/<仓>/…` 的多仓工作区，脚本常在仓内的
   `scripts/`；全局工具（如 `~/.zcode/scripts/glm_vision.py`）与全局 skill 根本不在项目里。
   只按项目根 `scripts/<名>` 查，实测 46 条 ERROR 里 44 条是把真文件判成缺失的假阳性。
3. **要能回答"项目落后框架多少"**——框架在推进、项目在分叉，却没有任何一处机器能说出差距。
"""

import io
import json
import os
import re
import sys

# ---------------------------------------------------------------- 输出与工具

ERROR, WARN, INFO = "ERROR", "WARN", "INFO"
_LEVEL_ORDER = {ERROR: 0, WARN: 1, INFO: 2}

# ---------------------------------------------------------------- 配置 schema
#
# schema_version 与框架版本**解耦**：只在 config.json 的**结构**变了才升（加字段不算，
# 改字段含义/形态才算）。这样「配置怎么解释」有唯一依据，不必靠猜框架版本。
#
#   1 —— v4.9.0 起：顶层 `schema_version` + 可选 `external_roots` / `framework_source`；
#        `paths.layout` 为对象（八域名 → 相对路径），`paths.repos` 为列表（多仓）
#
CONFIG_SCHEMA = "1"
SCHEMA_VERSION_KEY = "schema_version"
SCHEMA_HINT = "跑 `python scripts/migrate.py --dry-run <项目根>` 看迁移计划，再 `--apply` 执行"


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


class Finding(object):
    def __init__(self, check, level, message, where=""):
        self.check = check
        self.level = level
        self.message = message
        self.where = where

    def as_dict(self):
        return {"check": self.check, "level": self.level,
                "message": self.message, "where": self.where}


class Report(object):
    def __init__(self):
        self.findings = []
        self.infos = []          # 只统计不判定的说明（如"共 40 条验收标准"）

    def add(self, check, level, message, where=""):
        self.findings.append(Finding(check, level, message, where))

    def error(self, check, message, where=""):
        self.add(check, ERROR, message, where)

    def warn(self, check, message, where=""):
        self.add(check, WARN, message, where)

    def note(self, check, message):
        self.infos.append({"check": check, "message": message})

    def n(self, level):
        return len([f for f in self.findings if f.level == level])

    @property
    def ok(self):
        return self.n(ERROR) == 0


# ---------------------------------------------------------------- 通用解析

FRONTMATTER_VERSION = re.compile(r'^version:\s*"?([0-9][0-9.]*)"?\s*$', re.M)
SEMVER = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def ver_tuple(text):
    m = SEMVER.search(text or "")
    return tuple(int(x) for x in m.groups()) if m else None


def md_files(root, subdir=None, recursive=True):
    base = os.path.join(root, subdir) if subdir else root
    out = []
    if recursive:
        for cur, _dirs, files in os.walk(base):
            for f in files:
                if f.endswith(".md"):
                    out.append(os.path.join(cur, f))
    else:
        if os.path.isdir(base):
            for f in sorted(os.listdir(base)):
                p = os.path.join(base, f)
                if f.endswith(".md") and os.path.isfile(p):
                    out.append(p)
    return sorted(out)


def rel(root, path):
    try:
        return os.path.relpath(path, root).replace("\\", "/")
    except ValueError:
        return path


def backticked(text):
    """取所有 `...` 行内代码里的引用目标。"""
    return re.findall(r"`([^`\n]+)`", text)


BRACE = re.compile(r"\{([^{}]*)\}")


def expand_braces(token):
    """展开 `scripts/{a,b}.py` 这类简写（递归，支持一个 token 里多处花括号）。

    不展开的代价是**假阳性**：`repos/x/scripts/{generate_manual,generate_pptx}.py` 里三个脚本
    都真实存在，检查器却按单一路径找不到，报出三条"引用了不存在的脚本"（实测于示例工作台B）。
    **被假阳性塞满的检查器，与永远返回 0 ERROR 的检查器一样会被无视**，故按文档常用写法展开再判。
    `{a}` 单元素不算简写，原样返回交由引用检查照常判定。
    """
    m = BRACE.search(token)
    if not m:
        return [token]
    parts = [p.strip() for p in m.group(1).split(",")]
    if len(parts) < 2 or not all(parts):
        return [token]
    out = []
    for one in parts:
        out.extend(expand_braces(token[:m.start()] + one + token[m.end():]))
    return out


def numbered_items(lines, start_idx, end_idx):
    """收集 [start_idx, end_idx) 行内以 `N. ` 开头的列表项编号（按出现顺序）。"""
    nums = []
    for i in range(start_idx, min(end_idx, len(lines))):
        m = re.match(r"^(\d+)\.\s", lines[i])
        if m:
            nums.append(int(m.group(1)))
    return nums


def section_bounds(lines, heading_re, next_re):
    """返回 (标题行号, 正文起, 正文止) —— 找不到返回 None。"""
    start = None
    for i, line in enumerate(lines):
        if re.match(heading_re, line):
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if re.match(next_re, lines[j]):
            end = j
            break
    return start, start + 1, end


def check_continuity(report, check, label, nums, where):
    if not nums:
        report.warn(check, "%s：一条编号项都没解析到（格式变了？）" % label, where)
        return 0
    expect = list(range(1, len(nums) + 1))
    if nums != expect:
        dup = sorted({n for n in nums if nums.count(n) > 1})
        missing = sorted(set(expect) - set(nums))
        detail = []
        if missing:
            detail.append("缺号 %s" % missing)
        if dup:
            detail.append("重号 %s" % dup)
        if not detail:
            detail.append("顺序异常（%s…）" % nums[:8])
        report.error(check, "%s：编号不连续——%s" % (label, "；".join(detail)), where)
    return len(nums)


# ---------------------------------------------------------------- 配置加载（v4.9.0）

def load_config(root, report, check="D注册表"):
    """读 config.json，返回 (cfg, schema_ok)。

    **本函数的存在理由：旧配置必须产出报告，而不是让整个体检崩掉。**
    缺文件/坏 JSON/schema 不认识 → 各报一条 ERROR（含迁移指路）后返回 schema_ok=False，
    由调用方跳过依赖该 schema 的检查；**绝不抛异常**。

    schema_ok=False 时 cfg 仍一并返回（尽力解析）：版本、角色、引用、漂移等检查多数不依赖 schema，
    让它们在旧项目上照样跑完，体检才有"能报出真实缺口"的价值。
    """
    cfg_path = os.path.join(root, "project-state", "config.json")
    if not os.path.isfile(cfg_path):
        report.error(check, "project-state/config.json 不存在", "project-state/config.json")
        return {}, False
    try:
        cfg = json.loads(read(cfg_path))
    except Exception as exc:
        report.error(check, "config.json 不是合法 JSON：%s" % exc, "project-state/config.json")
        return {}, False
    if not isinstance(cfg, dict):
        report.error(check, "config.json 顶层应为对象，实际是 %s" % type(cfg).__name__,
                     "project-state/config.json")
        return {}, False

    sv = cfg.get(SCHEMA_VERSION_KEY)
    if sv is None:
        report.error(check,
                     "config.json 缺 `%s`（旧版配置，无法判断该按哪套结构解释）——%s"
                     % (SCHEMA_VERSION_KEY, SCHEMA_HINT), "project-state/config.json")
        return cfg, False
    if str(sv) != CONFIG_SCHEMA:
        report.error(check,
                     "config.json `%s`=%s 不是本框架支持的版本（支持 %s）——%s"
                     % (SCHEMA_VERSION_KEY, sv, CONFIG_SCHEMA, SCHEMA_HINT),
                     "project-state/config.json")
        return cfg, False
    return cfg, True


def detect_mode(cfg):
    """框架自查 or 项目体检。判别器 = `project_name` 是否非空（与 `git_audit.py` 找真实工作区
    用的是同一字段：框架模板 project_name 为空，真实项目必填）。"""
    pn = str((cfg or {}).get("project_name") or "").strip()
    return "project" if pn else "framework"


# ---------------------------------------------------------------- A 版本一致性

# 「随框架同步/复制」句式声明的是**本文件最后一次随框架更新的版本**，故滞后是正常的（内容没变），
# 只有「超前」和「内容已提到新版本但头部没跟着改」才是错。这比"要求所有文件等于框架版本"可用——
# 后者在真实仓库里永远是红的（11 个角色 SKILL 各自停在改动当次的版本）。
SYNC_PATTERNS = [
    re.compile(r"随大型项目开发框架\s*v(\d+\.\d+\.\d+)"),
    re.compile(r"内容随框架同步至\s*v(\d+\.\d+\.\d+)"),
]
HEADER_VERSION = re.compile(r"^>\s*版本：\s*v(\d+\.\d+\.\d+)")

# 「正文已引用当前框架版本」的扫描窗口（行）。变更注记写在文首，故只看头部这一段；
# 窗口开得比 HEADER_VERSION 的 8 行略宽，容得下"头部 + 紧跟的一段变更说明"。
HEAD_SCAN_LINES = 20


def _version_of(text):
    """取 frontmatter 的 version，或首个 `> 版本：vX` 头部声明。"""
    m = FRONTMATTER_VERSION.search(text)
    if m:
        return m.group(1), "frontmatter"
    head = "\n".join(text.splitlines()[:8])
    m = HEADER_VERSION.search(head)
    if m:
        return m.group(1), "头部"
    return None, None


def check_versions(root, report):
    agent = os.path.join(root, "agent.md")
    if not os.path.isfile(agent):
        report.error("A版本", "agent.md 不存在，无法取框架版本", root)
        return None
    text = read(agent)
    m = re.search(r'^version:\s*"?([0-9][0-9.]*)"?\s*$', text, re.M)
    if not m:
        report.error("A版本", "agent.md frontmatter 无 version 字段", "agent.md")
        return None
    fw = m.group(1)
    fw_t = ver_tuple(fw)
    report.note("A版本", "框架版本 agent.md = v%s" % fw)

    def audit(path, r, declared, kind, lagging_sink):
        """声明版本 declared；只有超前/漏改判错，滞后记 INFO。"""
        d_t = ver_tuple(declared)
        if d_t and fw_t and d_t > fw_t:
            report.error("A版本", "%s 声明 v%s 高于框架版本 v%s（超前/残留）" % (kind, declared, fw), r)
            return
        if declared == fw:
            return
        body = read(path)
        # 正文已引用当前框架版本，头部却停在旧版本 = 漏改版本号（本版本改了内容没升号）。
        # **只看文首的变更注记区**（v4.9.0 收紧）：漏升号的表现形式是"顶部更新说明里写了新版号"，
        # 那一定在头部；而正文中段提到某个版本号，绝大多数是**历史/交叉陈述**
        # （"该机制 v4.8.0 引入""v3.22.0 往本体加了四条"）。此前全篇扫描，实测把这类
        # 诚实注记连续判成 ERROR（同一处两次），**逼着作者删掉准确的版本信息来换取变绿**——
        # 那是本末倒置。滞后本身另有 lagging INFO 兜底，不会因此被藏住。
        head_block = "\n".join(l for l in body.splitlines()[:HEAD_SCAN_LINES]
                               if not l.startswith("> 版本："))
        if fw in head_block:
            report.error("A版本",
                         "%s 停留在 v%s，但文首已引用 v%s——内容随框架改了、版本号漏升"
                         % (kind, declared, fw), r)
            return
        lagging_sink.append("%s = v%s" % (r, declared))

    # A2 各角色 SKILL
    skills_dir = os.path.join(root, "skills")
    lagging = []
    if os.path.isdir(skills_dir):
        for role in sorted(os.listdir(skills_dir)):
            p = os.path.join(skills_dir, role, "SKILL.md")
            if not os.path.isfile(p):
                continue
            sv, _kind = _version_of(read(p))
            if not sv:
                report.error("A版本", "skills/%s/SKILL.md 无 version 字段（frontmatter 与头部都没有）" % role,
                             "skills/%s/SKILL.md" % role)
                continue
            audit(p, "skills/%s/SKILL.md" % role, sv, "SKILL 版本", lagging)

    # A3/A4 project-state 下模板文件
    for p in md_files(root, "project-state"):
        r = rel(root, p)
        head = "\n".join(read(p).splitlines()[:8])
        synced = False
        for pat in SYNC_PATTERNS:
            for hit in pat.findall(head):
                synced = True
                audit(p, r, hit, "「随框架同步」声明", lagging)
        if not synced:
            hv, _kind = _version_of(read(p))
            if hv:
                audit(p, r, hv, "头部版本", lagging)

    if lagging:
        report.note("A版本", "版本滞后于框架的文件（内容未随本次框架变更 → 正常，不必强行升号）：%s"
                    % "；".join(sorted(set(lagging))[:10]) + ("…" if len(set(lagging)) > 10 else ""))
    return fw


# ---------------------------------------------------------------- B 编号连续性
#
# 清单可以住在别的文件里（v4.9.0：agent.md 减重——验收标准/注意事项 已挪入 project-state/，
# agent.md 只留指针）。故这里按「标签 → 实际文件 + 标题」声明；**找不到文件即 ERROR**：
# 指针化之后，"清单文件被删/改名"必须能被机器发现，否则 agent.md 里的指针就成了断链。
AGENT_SECTIONS = [
    # (标签, 相对路径, 标题正则, 下一个同级或更高级标题正则)
    ("验收标准", "project-state/验收标准.md", r"^##\s*验收标准", r"^#{1,2} "),
    ("注意事项", "project-state/注意事项.md", r"^##\s*注意事项", r"^#{1,2} "),
    ("任务执行纪律", "agent.md", r"^## 任务执行纪律", r"^#{1,2} "),
]

# §9 工程治理（9.1~9.5）进 `完成定义.md` 的框架版本。早于此版本的项目完成定义只有 1~8 节，
# 属机制未同步而非格式损坏——见 check_numbering 里的分支。
DOD_S9_SINCE = "3.22.0"


def check_numbering(root, report, fw_version):
    """三份编号清单的连续性。

    **清单住在两处之一**：v4.9.0 起在契约文件（`project-state/验收标准.md` 等），
    更早的项目**内联在 agent.md 正文里**。两处都算"清单在"，只有哪边都没有才是缺陷——
    否则给老项目体检会得到"清单文件不存在"的假阳性，而它的清单好端端躺在 agent.md 里。
    （2026-09-15 实测：这条假阳性同时出现在武林无敌 v3.18 与示例工作台B v3.22 上。）

    `fw_version` 用于把"机制未同步"与"结构被改坏"分开报（同 check_brief_skeleton）。
    """
    totals = {}
    for label, relpath, head, nxt in AGENT_SECTIONS:
        p = os.path.join(root, *relpath.split("/"))
        where = relpath
        if os.path.isfile(p):
            src = p
        else:
            # 回落 agent.md：老结构的清单本体就在那。显式记一句，免得新项目指针落空也被静默放过。
            alt = os.path.join(root, "agent.md")
            alt_lines = read(alt).splitlines() if os.path.isfile(alt) else []
            if section_bounds(alt_lines, head, nxt):
                src, where = alt, "agent.md"
                report.note("B编号", "「%s」清单内联在 agent.md（老结构，未迁到 %s）——能用，"
                            "但它是 agent.md 长期膨胀的主要来源之一" % (label, relpath))
            else:
                report.error("B编号", "「%s」清单既不在 %s、也不在 agent.md——清单丢失或被改名"
                             % (label, relpath), relpath)
                continue
        lines = read(src).splitlines()
        b = section_bounds(lines, head, nxt)
        if not b:
            report.error("B编号", "%s 找不到「%s」小节" % (where, label), where)
            continue
        _s, a, z = b
        nums = numbered_items(lines, a, z)
        n = check_continuity(report, "B编号", "%s %s" % (where, label), nums, where)
        totals[label] = max(nums) if nums else 0
        report.note("B编号", "%s %s = %d 条" % (where, label, n))

    dod = os.path.join(root, "project-state", "完成定义.md")
    if os.path.isfile(dod):
        dod_text = read(dod)
        nums = [int(m.group(1)) for m in
                (re.match(r"^### 9\.(\d+)", l) for l in dod_text.splitlines()) if m]
        if not nums and not re.search(r"(?m)^#{2,3}\s*9[.、\s]", dod_text):
            # §9 工程治理（9.1~9.5）是 v3.22.0 才进完成定义的。老项目的完成定义只有 1~8 节，
            # 这**不是格式坏了，是这一段根本没搬过来**——按"格式变了？"报会把人领到错方向
            # （去找解析 bug，而真正要做的是补段）。
            # 判据用「有没有 §9 这个标题」而不是版本号：实测有项目 agent.md 已声明 v3.22.0、
            # 完成定义却仍停在 1~8 节（**版本号升了、内容没跟**），按版本判会漏掉它。
            report.warn("B编号",
                        "完成定义.md 没有 §9 工程治理段（v%s 起才进完成定义；本项目 agent.md 声明 v%s）"
                        "——属机制未同步，不是解析失败；补段后即可校验 9.1~9.N 编号连续性"
                        % (DOD_S9_SINCE, fw_version or "?"), "project-state/完成定义.md")
        else:
            check_continuity(report, "B编号", "完成定义.md §9.N", nums,
                             "project-state/完成定义.md")
        if nums:
            report.note("B编号", "完成定义.md §9 共 %d 节" % max(nums))
    else:
        report.error("B编号", "完成定义.md 不存在", "project-state/完成定义.md")
    # 供 G 组交叉引用校验用
    return totals


# ---------------------------------------------------------------- C 引用存在性

SCRIPT_REF = re.compile(r"(?:^|/)(scripts/[^/`\s]+?\.py)")
SKILL_REF = re.compile(r"(?:^|/)(skills/([^/\s`]+)/SKILL\.md)")
PLACEHOLDER = re.compile(r"[<>*]")

# 递归建索引时跳过的目录：依赖/缓存/打包产物。`_internal` 是 PyInstaller 打出来的副本，
# 认它等于认可"同一脚本在打包目录里也算存在"，那会把真缺失也放过去。
INDEX_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
                   "_internal", ".idea", ".vscode", "dist", "build"}
INDEX_MAX_DEPTH = 7

# C 组不扫的文件（按 basename 匹配）：只记录"当时引用了什么"的历史台账。
# 理由见 check_references 内的说明——拿今日存在性判历史记录 = 要求改写历史 = 制造假通过。
REF_SKIP_FILES = ("运行日志.md",)

# config.json 未声明 external_roots 时的默认探测位置（见 _external_roots）
DEFAULT_EXTERNAL_ROOTS = ("~/.zcode/skills", "~/.zcode/scripts", "~/.trae-cn/skills")
# 框架自己的根（本脚本住在 `<框架>/scripts/`）。用途只有一个：**当一个脚本引用落空时，
# 去框架里按同名找一遍，把"路径写错"和"文件真不存在"区分开**。
# 为什么需要（实测 2026-09-15，武林无敌）：21 条验收判据写 `scripts/art_lint.py`，
# 而该工具其实住在框架的 `skills/UI设计师/scripts/art_lint.py`——项目开工时没把它复制进去。
# 21 条一模一样的"引用了不存在的脚本"看着像检查器疯了，其实只是一处路径约定没对齐；
# 报错时附上真实位置，"21 个谜团"就变成"一处已定位的错"。
FRAMEWORK_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _canonical(relpath):
    """把工作区里任意深度的 `.../scripts/x.py`、`.../skills/<角色>/SKILL.md` 归一成
    它被引用时惯用的写法（`scripts/x.py` / `skills/<角色>/SKILL.md`）。

    这是多仓解析的关键一步：`repos/verified-assets/designer-demo/scripts/generate_manual.py`
    与文档里写的 `scripts/generate_manual.py` 指的是同一个文件，只有归一后才对得上。
    """
    p = "/" + relpath
    for marker in ("/scripts/", "/skills/"):
        i = p.rfind(marker)
        if i >= 0:
            return p[i + 1:]
    return relpath


def _external_roots(root, cfg, report):
    """外部根 = 不在项目内、但项目文档会引用的工具/技能所在目录（全局 skill、全局脚本）。

    取值优先级：config.json 的 `external_roots`（对象 {"skills": [...], "scripts": [...]}
    或列表）→ 默认位置探测（**探测命中会记 INFO**，避免"悄悄用了某个机器上的路径"）。

    **声明了却不存在**的根会被忽略，但**记一条 INFO**：否则"改了 external_roots 却没生效"
    会表现为假阳性依旧，排查时完全看不出根被悄悄丢了。
    """
    declared = cfg.get("external_roots")
    out = []
    if isinstance(declared, dict):
        for key in ("skills", "scripts"):
            v = declared.get(key)
            for one in (v if isinstance(v, list) else [v] if v else []):
                if isinstance(one, str) and one.strip():
                    out.append(os.path.expanduser(one.strip()))
    elif isinstance(declared, list):
        out.extend(os.path.expanduser(str(x)) for x in declared if isinstance(x, str))
    else:
        for d in DEFAULT_EXTERNAL_ROOTS:
            p = os.path.expanduser(d)
            if os.path.isdir(p):
                out.append(p)
        if out:
            report.note("C引用",
                        "未声明 external_roots，按默认位置解析全局工具（建议在 config.json 显式声明）：%s"
                        % out)
    keep = [p for p in out if os.path.isdir(p)]
    lost = [p for p in out if not os.path.isdir(p)]
    if lost:
        report.note("C引用", "external_roots 里有 %d 个根在本机不存在，已忽略（此机器没装，不算缺陷）：%s"
                    % (len(lost), lost))
    return keep


def repos_of(root, cfg):
    """config.json `paths.repos` 登记的多仓路径（真实项目是**多仓工作区**）。"""
    paths = cfg.get("paths")
    if not isinstance(paths, dict):
        return []
    v = paths.get("repos")
    out = []
    for one in (v if isinstance(v, list) else [v] if v else []):
        if isinstance(one, str) and one.strip():
            out.append(os.path.join(root, one.split("#")[0].strip()))
    return [p for p in out if os.path.isdir(p)]


def build_ref_index(root, cfg, report):
    """建「工作区内到底有哪些脚本/角色 SKILL」的索引（按归一写法存）。

    扫描面 = 项目根 + config.json 登记的每个代码仓 + 外部根。**必须扫多仓**：
    真实项目的脚本住在 `repos/<仓>/…`，只按项目根的 `scripts/<名>` 查会把真文件判成缺失。
    """
    roots = [root] + repos_of(root, cfg) + _external_roots(root, cfg, report)
    idx = set()
    for base in roots:
        if not os.path.isdir(base):
            continue
        # 扫描根本身就是 `skills/`（或 `scripts/`）时，索引键要补回这一段前缀：
        # `~/.zcode/skills` 下的 `元技能生成器/SKILL.md` 与文档里写的
        # `skills/元技能生成器/SKILL.md` 是同一个文件，不补前缀两者永远对不上（实测假阳性）。
        base_name = os.path.basename(os.path.normpath(base)).lower()
        prefix = base_name if base_name in ("skills", "scripts") else ""
        for cur, dirs, files in os.walk(base):
            if os.path.relpath(cur, base).count(os.sep) >= INDEX_MAX_DEPTH:
                dirs[:] = []
                continue
            dirs[:] = [d for d in dirs if d not in INDEX_SKIP_DIRS]
            for f in files:
                if f.endswith(".py") or f == "SKILL.md":
                    rp = os.path.relpath(os.path.join(cur, f), base).replace("\\", "/")
                    key = _canonical(rp)
                    if prefix and not key.startswith(prefix + "/"):
                        key = prefix + "/" + key
                    idx.add(key)
    return idx, [b for b in roots if os.path.isdir(b)]


def check_references(root, report, files, cfg):
    idx, scan_roots = build_ref_index(root, cfg, report)
    seen = set()
    skipped = 0
    hist = 0
    literal = 0
    for p in files:
        r = rel(root, p)
        # **历史台账不追今日存在性**：`运行日志.md` 是 append-only 流水，写的是"当时引用了什么"
        # （如"下载到的 zip 内含 scripts/run.py"）。拿今天的存在性去判历史记录，等于要求改写历史——
        # 那才是真正的假通过（同 migrate_v317 的"文本历史记录不改"）。跳过的份数在汇总里报出来。
        if os.path.basename(p) in REF_SKIP_FILES:
            hist += 1
            continue
        for token in backticked(read(p)):
            # `skills/<角色>/SKILL.md`、`scripts/<脚本>.py` 是占位写法，不是具体引用
            if PLACEHOLDER.search(token):
                skipped += 1
                continue
            for one in expand_braces(token):
                for m in SCRIPT_REF.finditer(one):
                    ref = m.group(1)
                    key = (r, ref)
                    if key in seen:
                        continue
                    seen.add(key)
                    if _canonical(ref) in idx:
                        continue
                    # 索引里没有 ≠ 文件不在：文档会引用**项目外**的真实文件（全局脚本、插件缓存），
                    # 它们不在「项目 + 多仓 + external_roots」三类扫描面里。按字面再判一次——
                    # 把真存在的文件判成缺失，正是检查器最容易被整段无视的那种假阳性。
                    if _literal_ok(root, one):
                        literal += 1
                        continue
                    report.error("C引用",
                                 "引用了不存在的脚本 `%s`%s" % (ref, _where_else(ref, idx)),
                                 r)
                for m in SKILL_REF.finditer(one):
                    ref = m.group(1)
                    key = (r, ref)
                    if key in seen:
                        continue
                    seen.add(key)
                    if _canonical(ref) in idx:
                        continue
                    if _literal_ok(root, one):
                        literal += 1
                        continue
                    # 只有**项目内角色 SKILL 的规范写法**（`skills/<角色>/SKILL.md`）才谈得上
                    # "角色 SKILL 不存在"。带目录前缀的是外部文件的引用，按普通文件缺失报——
                    # 报错信息指错方向，比不报还费时间。
                    if _is_project_role_skill(one, m, ref):
                        report.error("C引用",
                                     "引用了不存在的角色 SKILL `%s`%s" % (ref, _where_else(ref, idx)), r)
                    else:
                        report.error("C引用",
                                     "引用了不存在的文件 `%s`%s"
                                     % (m.group(0).lstrip("/"), _where_else(ref, idx)), r)
    report.note("C引用", "扫描 %d 个文件、%d 处脚本/SKILL 引用（%d 处按字面路径命中、跳过 %d 处占位写法、"
                "%d 份历史台账）；解析面 %d 个根（含多仓与外部根）"
                % (len(files), len(seen), literal, skipped, hist, len(scan_roots)))


def _literal_ok(root, token):
    """token 本身就是一个真实存在的文件？

    依次按「原样 / `~` 展开 / 相对项目根」三种解释去试——文档里的路径三种写法都有。
    只认**文件或目录存在**，不做任何"像不像路径"的猜测：猜错方向就会放过真缺失。
    """
    t = token.strip().strip("`").strip()
    if not t or PLACEHOLDER.search(t) or len(t) > 400:
        return False
    cands = [t, os.path.expanduser(t), os.path.join(root, t)]
    for c in cands:
        try:
            if os.path.exists(c):
                return True
        except (OSError, ValueError):
            continue
    return False


def _where_else(ref, idx):
    """引用落空时，按**同名文件**再找一遍，返回一段可直接贴进报错的提示（找不到就返回空串）。

    两处找，且**只提示不改判**（仍是 ERROR——路径写错就是要改，放过它等于把不可复现的验收判据
    洗成通过）：
      1. 索引里同名不同路径 —— 项目内把工具放错了目录；
      2. 框架自己的 `scripts/` 与 `skills/*/scripts/` —— 工具随框架发，项目开工时没复制/路径没对齐。
    只认**唯一命中**：同名多个文件时不敢断言是哪一个，宁可不说（说错方向比不说更费时间）。
    """
    base = os.path.basename(ref)
    if not base or "/" in base:
        return ""
    hits = sorted(k for k in idx if os.path.basename(k) == base and _canonical(k) != _canonical(ref))
    if len(hits) == 1:
        return "（同名文件在：`%s`——引用路径与它实际所在的位置不一致）" % hits[0]
    if hits:
        return "（同名文件有 %d 处，路径需核对：%s）" % (len(hits), "、".join("`%s`" % h for h in hits[:3]))
    fw = _framework_same_name(base)
    if fw:
        return ("（框架内有同名脚本 `%s`——说明它随框架发、不在项目里：要么开工时没复制进来，"
                "要么文档该写成框架内路径 `%s`）" % (fw, fw))
    return ""


_FW_NAME_CACHE = {}


def _framework_same_name(base):
    """框架 `scripts/` + `skills/*/scripts/` 里同名的 .py（唯一命中才返回相对路径）。"""
    if base in _FW_NAME_CACHE:
        return _FW_NAME_CACHE[base]
    found = []
    for sub in ("scripts", os.path.join("skills")):
        for cur, dirs, files in os.walk(os.path.join(FRAMEWORK_ROOT, sub)):
            dirs[:] = [d for d in dirs if d not in INDEX_SKIP_DIRS]
            if base in files:
                found.append(os.path.relpath(os.path.join(cur, base), FRAMEWORK_ROOT).replace("\\", "/"))
    hit = found[0] if len(found) == 1 else ""
    _FW_NAME_CACHE[base] = hit
    return hit


def _is_project_role_skill(one, m, ref):
    """该匹配是不是「项目内角色 SKILL」的规范写法？

    判据 = `skills/` 之前没有任何目录成分（`^skills/...` 或 `./skills/...`）。
    `~/.zcode/skills/x/SKILL.md`、`plugin_pack/skills/x/SKILL.md` 都带前缀，不属于此类。
    """
    before = one[:m.start(1)].lstrip("./")
    return before == "" and ref.startswith("skills/")


# ---------------------------------------------------------------- D 注册表一致

def check_registry(root, report, cfg, schema_ok):
    if not schema_ok:
        report.note("D注册表", "config.json schema 不可识别，本组跳过（已在上方报错，先迁移再复查）")
        return
    paths = cfg.get("paths")
    if not isinstance(paths, dict):
        report.error("D注册表", "config.json 缺 paths 段（或不是对象）", "project-state/config.json")
        return

    def norm(v):
        if isinstance(v, list):
            return [str(x).split("#")[0].strip() for x in v]
        if isinstance(v, dict):
            return []                      # layout/根文件 这类嵌套结构不是域条目
        return [str(v).split("#")[0].strip()]

    top = {}
    for k, v in paths.items():
        if k.startswith("_") or k in ("layout", "根文件"):
            continue
        for one in norm(v):
            top[one] = k
    layout = paths.get("layout")
    if layout is None:
        report.error("D注册表",
                     "config.json 缺 `paths.layout`（八域名 → 相对路径的对照表）——%s" % SCHEMA_HINT,
                     "project-state/config.json")
        return
    if not isinstance(layout, dict):
        # v3.12 时代把 layout 写成一段说明字符串；直接 .items() 会抛异常、让整个体检崩掉。
        # 报「需迁移」并继续，是本工具 v4.9.0 最重要的一处容错。
        report.error("D注册表",
                     "`paths.layout` 应为对象（八域名 → 相对路径），实际是 %s——旧版写法，%s"
                     % (type(layout).__name__, SCHEMA_HINT), "project-state/config.json")
        return
    lay = {}
    lay_pend = []
    for k, v in layout.items():
        if k.startswith("_"):
            continue
        # `<待填>` 占位（约定同根级白名单）：**工具不猜域映射**，故不参与一一对应比对，
        # 但必须报出来——否则"layout 是对象了、检查却还红着"会让人以为迁移没生效。
        if re.search(r"<[^>]+>", str(v)):
            lay_pend.append(k)
            continue
        for one in norm(v):
            lay[one] = k

    # 多仓：layout 可以只声明伞域 `repos`，而 paths 列到每个仓 `repos/<仓>`。
    # 精确相等会把这种正常登记判成"layout 缺域"，故允许前缀覆盖——**且必须双向**：
    # layout 写伞域 `repos` / paths 列 `repos/workbench`、`repos/skill-platform` 是同一件事的
    # 两种粒度。只认单向时，实测在真实项目上报出"layout 有而 paths 缺的域：['repos']"——
    # 而 paths 里明明逐个列着两个仓，是**假阳性**。
    def covered(one, table):
        if one in table:
            return True
        if any(one.startswith(k.rstrip("/") + "/") for k in table):
            return True                    # 条目是表里某项的子路径
        return any(k.startswith(one.rstrip("/") + "/") for k in table)   # 条目是表里某项的伞域

    only_paths = sorted(x for x in set(top) if not covered(x, lay))
    only_layout = sorted(x for x in set(lay) if not covered(x, top))
    if only_layout:
        # 域声明了却没有对应路径 = 角色按域取路径会解析不到 → 这条是本组存在的理由，必须阻塞。
        report.error("D注册表", "layout 有而 paths 缺的域（路径解析不到）：%s" % only_layout,
                     "project-state/config.json")
    if only_paths:
        # **单向宽松（v4.9.0）**：paths 里多出八域之外的登记项（框架工具 `scripts`、角色技能
        # `skills`、项目自定域如打包现场 `桌面版`）是**正常且被鼓励的**——v3.18.0 根级白名单
        # 恰恰要求根上每一项都登记在册。它们不影响"按域解析得到路径"这个目的，故记 INFO 不阻塞；
        # 域路径写错的情况也不会因此漏掉：那会让对应域的 layout 值在 paths 里找不到，
        # 由上面的 only_layout 报 ERROR。**两条判据的不对称，来自它们各自要防的事不同。**
        report.note("D注册表",
                    "paths 有 %d 项是八域之外的额外登记根（不影响按域解析，属正常）：%s"
                    % (len(only_paths), only_paths))
    if not only_paths and not only_layout and not lay_pend:
        report.note("D注册表", "paths × layout 一一对应，共 %d 域（多仓按前缀归属）" % len(lay))

    if lay_pend:
        report.warn("D注册表",
                    "`paths.layout` 有 %d 项仍是 `<待填>` 占位（工具不猜域映射，需人工补真实相对路径）：%s"
                    % (len(lay_pend), lay_pend), "project-state/config.json")

    whitelist = paths.get("根文件")
    if not isinstance(whitelist, dict):
        whitelist = cfg.get("根文件") if isinstance(cfg.get("根文件"), dict) else {}
    pending = [k for k, v in whitelist.items()
               if not k.startswith("_") and re.search(r"<[^>]+>", str(v))]
    if pending:
        report.warn("D注册表", "根级白名单含未填占位（项目初始化时必须替换为真实路径）：%s" % pending,
                    "project-state/config.json")


# ---------------------------------------------------------------- E 单一事实源

def check_single_source(root, report, agent_lines):
    p = os.path.join(root, "project-state", "协作规范.md")
    if not os.path.isfile(p):
        report.error("E单一源", "协作规范.md 不存在", "project-state/协作规范.md")
        return
    lines = read(p).splitlines()
    b = section_bounds(lines, r"^## 4\.", r"^## ")
    if not b:
        report.error("E单一源", "协作规范.md 找不到 §4（主控验收前置检查）", "project-state/协作规范.md")
        return
    _s, a, z = b
    body = lines[a:z]
    text = "\n".join(body)
    if "唯一事实源" not in text or "agent.md" not in text:
        report.error("E单一源",
                     "§4 未声明「唯一事实源在 agent.md」——去重纪律被破坏（应是指针，不是清单本体）",
                     "project-state/协作规范.md")
    numbered = [l for l in body if re.match(r"^\s*\d+\.\s", l)]
    if numbered:
        report.error("E单一源",
                     "§4 出现 %d 条编号清单项 = 第二份清单本体长回来了（手抄副本必然漂移）"
                     % len(numbered), "project-state/协作规范.md")

    # §4 若声明了「当前 N 项」，必须与 agent.md ⑥ 实际条数一致
    declared = re.search(r"当前\s*(\d+)\s*项", text)
    if declared:
        n_declared = int(declared.group(1))
        b6 = section_bounds(agent_lines, r"^### ⑥ ", r"^#{3,4} ")
        if not b6:
            report.error("E单一源", "agent.md 找不到 ⑥ 小节，无法核对清单条数", "agent.md")
            return
        _s6, a6, z6 = b6
        # 清单本体以「主控验收前置检查（进本步前必须过）：」引导，取其后的编号项
        cut = a6
        for i in range(a6, z6):
            if "主控验收前置检查" in agent_lines[i]:
                cut = i
                break
        n_actual = len(numbered_items(agent_lines, cut, z6))
        if n_actual != n_declared:
            report.error("E单一源",
                         "协作规范 §4 声明「当前 %d 项」，agent.md ⑥ 实为 %d 项——指针里的数字已过期"
                         % (n_declared, n_actual), "project-state/协作规范.md")
        else:
            report.note("E单一源", "验收前置检查清单 %d 项，指针与本体一致" % n_actual)


# ---------------------------------------------------------------- F 角色同步完整性

def role_names(agent_lines):
    """从 agent.md「角色定义」表取角色名（第 1 列）。"""
    b = section_bounds(agent_lines, r"^## 角色定义", r"^#{1,2} ")
    if not b:
        return [], None
    _s, a, z = b
    out = []
    for i in range(a, z):
        m = re.match(r"^\|\s*([^|]+?)\s*\|", agent_lines[i])
        if m:
            name = m.group(1).strip()
            if name and name != "角色" and not set(name) <= set("-: "):
                out.append(name)
    return out, (a, z)


def check_roles(root, report, agent_lines):
    roles, _span = role_names(agent_lines)
    if not roles:
        report.error("F角色", "agent.md 角色定义表解析不到角色", "agent.md")
        return roles
    idx = os.path.join(root, "project-state", "契约", "上下文索引.md")
    idx_text = read(idx) if os.path.isfile(idx) else ""
    if not idx_text:
        report.error("F角色", "上下文索引.md 不存在", "project-state/契约/上下文索引.md")
    for role in roles:
        skill = os.path.join(root, "skills", role, "SKILL.md")
        if not os.path.isfile(skill):
            report.error("F角色", "角色「%s」在角色定义表里，但 skills/%s/SKILL.md 不存在" % (role, role),
                         "skills/%s/SKILL.md" % role)
        prog = os.path.join(root, "project-state", "进度", role + ".json")
        if not os.path.isfile(prog):
            report.error("F角色", "角色「%s」缺进度模板 project-state/进度/%s.json" % (role, role),
                         "project-state/进度/%s.json" % role)
        if idx_text and role not in idx_text:
            report.warn("F角色", "角色「%s」未在 契约/上下文索引.md 出现（产出落点未登记）" % role,
                        "project-state/契约/上下文索引.md")
    # 反向：有 SKILL 目录但不在角色定义表
    skills_dir = os.path.join(root, "skills")
    if os.path.isdir(skills_dir):
        for d in sorted(os.listdir(skills_dir)):
            if os.path.isfile(os.path.join(skills_dir, d, "SKILL.md")) and d not in roles:
                report.error("F角色", "skills/%s 有 SKILL 但不在 agent.md 角色定义表（孤角色）" % d,
                             "skills/%s/SKILL.md" % d)
    report.note("F角色", "角色定义表 %d 个角色" % len(roles))
    return roles


# ---------------------------------------------------------------- E 派发简报骨架顺序（v4.8.0）

# 三段式装配的标记字面量。**位置即语义**：A（跨角色/跨卡共享的那段上下文）必须在最前，
# 因为供应商与服务端网关按**最长公共前缀**命中提示词缓存——第一个随卡变化的字节之前的
# 内容才是可复用的部分。把卡号写进简报第一行 = 第一个 token 就分叉，后面再一致也全量计费。
BRIEF_MARKERS = ("【A 共享块】", "【B 卡块】", "【C 角色块】")

# 该机制自框架哪版引入。用于区分**机制未同步**（老项目，报 WARN + 指路迁移）与
# **结构被改坏**（已是新版还缺/乱序，报 ERROR）——两者都该被看见，但只有后者阻塞。
BRIEF_SKELETON_SINCE = "4.8.0"


def _before(a, b):
    """版本 a 是否早于 b。任一不可解析时返回 False——**拿不准就按"不早于"处理**，
    宁可报 ERROR 也不静默放过（同「未查过 ≠ 查过且干净」的取向）。"""
    ta, tb = ver_tuple(a), ver_tuple(b)
    if not ta or not tb:
        return False
    return ta < tb


def check_brief_skeleton(root, report, agent_lines, fw_version):
    """派发简报骨架：标记缺失、或三段标记**次序交错**，即 ERROR。

    只查 agent.md 一篇文档，不查实际派发的简报（子代理简报是运行期文本，落不到磁盘上）；
    故本检查防的是「有人顺手把卡号挪回第一行 / 把三段拆开重排」这类**顺序漂移**，
    **不构成"缓存确实命中"的证据**——命中须由网关侧 trackCacheUsage 观测。

    **老项目降级为 WARN**：本项目 agent.md 版本早于 v%s 时，"没有三段标记"的意思是
    「这个机制还没同步过来」，不是「有人把它删了」——按 ERROR 报会让**所有 v4.8.0 之前的
    项目永久全红**，而永久全红的检查器等于没有（同「永远返回 0 ERROR 的检查器比没有更坏」）。
    降级不等于放过：WARN 里写明版本差距并指向 `migrate.py`。标记在、但**次序被打乱**的
    一律维持 ERROR——机制在位而排布被改坏，才是本检查真正要拦的那件事。
    """ % BRIEF_SKELETON_SINCE
    text = "\n".join(agent_lines)
    occ = []
    for i, marker in enumerate(BRIEF_MARKERS):
        start = 0
        while True:
            p = text.find(marker, start)
            if p < 0:
                break
            occ.append((p, i))
            start = p + len(marker)
    absent = [BRIEF_MARKERS[i] for i in range(len(BRIEF_MARKERS))
              if not any(o[1] == i for o in occ)]
    if absent:
        msg = ("agent.md 派发简报缺骨架标记 %s——三段式装配纪律被删除或改写"
               "（共享块前置是前缀缓存可命中的唯一前提）" % "、".join(absent))
        if _before(fw_version, BRIEF_SKELETON_SINCE):
            report.warn("E单一源",
                        "%s。**本项目 agent.md 是 v%s，早于该机制引入的 v%s**，属机制未同步"
                        "（不是结构损坏）——跑 `python scripts/migrate.py <项目根>` 看是否在迁移项里"
                        % (msg, fw_version or "?", BRIEF_SKELETON_SINCE),
                        "agent.md")
        else:
            report.error("E单一源", msg, "agent.md")
        return
    occ.sort()
    order = [o[1] for o in occ]
    n = len(BRIEF_MARKERS)
    counts = [sum(1 for o in occ if o[1] == i) for i in range(n)]
    # 三段可以按 A→B→C 成组重复出现多次（骨架在 description / 派发规则 / 验收标准 / 注意事项各写一遍），
    # 故判据是「每 n 个一组、组内次序恒为 A,B,C」，而不是「全文只允许出现一次」。
    grouped_ok = all(order[k] == k % n for k in range(len(order)))
    if len(set(counts)) != 1 or not grouped_ok:
        report.error("E单一源",
                     "agent.md 派发简报骨架顺序被打乱：三段标记应成组出现（A→B→C 为一组，可重复多组），"
                     "实际次序 %s（各段计数 %s）——随卡变化的字节越靠前，跨角色可复用的公共前缀越短，"
                     "缓存命中被打散"
                     % ("→".join("ABC"[i] for i in order),
                        "、".join("ABC[i]=%d" % c for i, c in enumerate(counts))),
                     "agent.md")
        return
    report.note("E单一源",
                "派发简报骨架三段齐备且顺序正确（A 共享块 → B 卡块 → C 角色块，共 %d 组）"
                % counts[0])


# ---------------------------------------------------------------- G 交叉引用有效性

REF_NOTES = re.compile(r"注意事项\s*#?(\d+)")
REF_AC = re.compile(r"验收标准\s*#?(\d+)")
REF_DISC = re.compile(r"任务执行纪律\s*#?(\d+)")
REF_SKILL_SEC = re.compile(r"skills/([^/\s`]+)/SKILL\.md`?\s*§\s*(\d+)")


def check_crossrefs(root, report, files, maxes):
    for p in files:
        text = read(p)
        r = rel(root, p)
        for label, pat, key in (("注意事项", REF_NOTES, "注意事项"),
                                ("验收标准", REF_AC, "验收标准"),
                                ("任务执行纪律", REF_DISC, "任务执行纪律")):
            limit = maxes.get(key)
            if not limit:
                continue
            for hit in pat.finditer(text):
                n = int(hit.group(1))
                if n < 1 or n > limit:
                    report.error("G交叉引用",
                                 "引用了不存在的「%s %d」（该节实际只有 1~%d 条）" % (label, n, limit), r)
        for m in REF_SKILL_SEC.finditer(text):
            role, sec = m.group(1), m.group(2)
            sp = os.path.join(root, "skills", role, "SKILL.md")
            if not os.path.isfile(sp):
                continue      # 已在 C 组报"引用了不存在的 SKILL"
            sm = read(sp)
            if not re.search(r"^#{2,3}\s*%s[\.、\s]" % re.escape(sec), sm, re.M):
                report.error("G交叉引用",
                             "引用了 `skills/%s/SKILL.md` §%s，但该 SKILL 无此小节" % (role, sec), r)


# ---------------------------------------------------------------- H 登记↔磁盘

INDEX_MARKER = re.compile(r"【[^】]*】")


def check_registry_vs_disk(root, report):
    """上下文索引登记的**模板类**条目必须真的在磁盘上。

    【运行】/【运行·按需建】= 运行期生成，开工前不存在属正常 → 跳过；
    含 `<...>`/`*`/`NNN` 的路径是模式不是具体文件 → 跳过；
    其余未标注却找不到的 → WARN（缺文件，或该条其实是运行期产物、只是漏标【运行】）。

    **只把"像路径的首格"当登记条目**（v4.9.0 补）：索引里除文档表外还有别的表——
    角色→落点表（首格是 `产品经理`）、各类对照表（首格是 `角色` 这样的表头）。
    此前一律按路径判，实测在一个真实项目上一次报出 13 条"登记了 `产品经理`、磁盘上找不到"，
    **13 条全假**。判据改为"含 `/`、`\\` 或带扩展名"才认：文档登记一定落到具体文件上，
    没有分隔符也没有扩展名的首格不是路径。**被假阳性塞满的检查器会被整段无视。**
    """
    p = os.path.join(root, "project-state", "契约", "上下文索引.md")
    if not os.path.isfile(p):
        return
    section_dir = ""
    checked = missed = 0
    for line in read(p).splitlines():
        hm = re.match(r"^##\s+([^\s（(]+)", line)
        if hm:
            section_dir = hm.group(1).rstrip("/")
            continue
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 2 or cells[0] in ("文档", "") or set(cells[0]) <= set("-: "):
            continue
        raw = cells[0]
        if INDEX_MARKER.search(raw):
            continue                      # 【运行】类，不要求落在磁盘
        # 去掉“**强调**”“（v3.22.0 补登）”这类注解与反引号，只留路径本身
        cleaned = re.sub(r"（[^）]*）", "", re.sub(r"\*\*", "", raw.replace("`", ""))).strip()
        names = [x.strip() for x in cleaned.split("、")]
        for name in names:
            if not name or PLACEHOLDER.search(name) or "NNN" in name:
                continue
            if not _looks_like_path(name):
                continue
            checked += 1
            cands = [os.path.join(root, "project-state", section_dir, name),
                     os.path.join(root, "project-state", name),
                     os.path.join(root, name)]
            if not any(os.path.exists(c) for c in cands):
                missed += 1
                report.warn("H登记↔磁盘",
                            "上下文索引登记了 `%s`，磁盘上找不到——要么补文件，要么它其实是运行期产物、"
                            "请按图例补 【运行】 标记" % raw,
                            "project-state/契约/上下文索引.md")
    report.note("H登记↔磁盘", "索引模板类条目 %d 条，落空 %d 条" % (checked, missed))


def _looks_like_path(name):
    """首格是不是一个**文件路径**（而非角色名/表头/说明文字）。

    认可：`代码/前端/开发说明.md`（有分隔符）、`配置中心.md`（有扩展名）、`交付/xxx`。
    不认可：`产品经理`、`角色`、`产出落点`——它们既无分隔符也无扩展名。
    """
    if "/" in name or "\\" in name:
        return True
    return bool(re.search(r"\.[A-Za-z0-9]{1,8}$", name))



# ---------------------------------------------------------------- N 工作区地图对账（v5.17.0）

def check_workspace_map_drift(root, report):
    """以 `scripts/workspace_map.py` 为**唯一对账引擎**的登记↔磁盘对账（严格档）。

    与 H 组的分工：H 是保守 WARN 子集（只查"索引登记了、磁盘没有"）；本组由引擎全量驱动——
    error 级漂移（registered_missing / unregistered_contract / loose_file）= lint ERROR，
    warn 级（占位符登记未物化）= WARN。**判据只活在 workspace_map.py 一处**，本组不复制
    对账逻辑（双引擎各说各话 = 两个"唯一真相源"打架，recall.py 文档里记过这个病）。
    地图人读版（工作区地图.md）是运行期产物：未生成只记 INFO 不拦。
    """
    try:
        import workspace_map as wm  # 与本脚本同目录；对账逻辑的唯一存放处
    except ImportError:
        report.error("N地图对账", "对账引擎 scripts/workspace_map.py 不可用（import 失败）——先修引擎，不许跳过检查", "scripts/workspace_map.py")
        return
    index_path = os.path.join(root, "project-state", "契约", "上下文索引.md")
    if not os.path.isfile(index_path):
        return  # H 组已报"缺索引"，这里不重复
    files, dirs = wm.scan_disk(root)
    entries = wm.parse_context_index(index_path)
    ledgers = wm.parse_solution_ledgers(root)
    _, unknown, drift = wm.classify(root, files, dirs, entries, ledgers)
    n_err = n_warn = 0
    for d in drift:
        kind = d["severity"]
        msg = "%s `%s` — %s" % (d["type"], d["path"], d["detail"])
        if kind == "error":
            n_err += 1
            report.error("N地图对账", msg, d["path"])
        else:
            n_warn += 1
            report.warn("N地图对账", msg, d["path"])
    if not os.path.isfile(os.path.join(root, "工作区地图.md")):
        report.note("N地图对账", "工作区地图.md 未生成（运行期产物，不拦）——python -X utf8 scripts/workspace_map.py <项目根> --write")
    report.note("N地图对账", "对账：ERROR %d / WARN %d / unknown %d（engine=workspace_map，【运行】图例语义由引擎承接）"
                % (n_err, n_warn, len(unknown)))


# ---------------------------------------------------------------- J 版本漂移（v4.9.0）

def check_drift(root, report, cfg, fw_version, mode):
    """项目 agent.md 版本 vs 框架源仓库版本——回答"落后多少、要不要迁移"。

    框架是**开工时复制到项目**的，复制即分叉：没有一处机器能说出差距，于是项目可以无声地
    停在旧版本上（2026-09-15 实测：两个真实项目分别停在 v3.22.0 / v3.18.0，而框架已 v4.9.0，
    中间九个版本的成本控制、可实施性关卡、复盘校准等在项目上产出为零）。
    比较对象由 config.json 的 `framework_source` 指定；未指定则跳过（只记 INFO，不强求——
    不是每个项目都值得挂这个指针）。
    """
    src = cfg.get("framework_source")
    if not src or not isinstance(src, str) or not src.strip():
        report.note("J漂移", "未在 config.json 声明 `framework_source`，跳过版本漂移比对")
        return
    src = src.strip()
    if re.search(r"<[^>]+>", src):
        # 与根级白名单同一约定（v4.5.0）：尖括号 = 未填占位。占位不是"指错路"，故只记 INFO。
        report.note("J漂移", "`framework_source` 仍是未填占位（%s），跳过漂移比对——"
                    "填入框架源仓库路径后即可比对" % src)
        return
    src = os.path.expanduser(src)
    if not os.path.isdir(src):
        report.warn("J漂移", "config.json 的 `framework_source` 指向的目录不存在：%s" % src,
                    "project-state/config.json")
        return
    fw_agent = os.path.join(src, "agent.md")
    if not os.path.isfile(fw_agent):
        report.warn("J漂移", "`framework_source` 目录里没有 agent.md：%s" % src,
                    "project-state/config.json")
        return
    m = re.search(r'^version:\s*"?([0-9][0-9.]*)"?\s*$', read(fw_agent), re.M)
    if not m:
        report.warn("J漂移", "`framework_source` 的 agent.md 无 version 字段", "project-state/config.json")
        return
    upstream = m.group(1)
    if mode != "project":
        report.note("J漂移", "框架源仓库版本 = v%s（本仓库即框架本体，无需比对）" % upstream)
        return
    up_t, cur_t = ver_tuple(upstream), ver_tuple(fw_version or "")
    if not cur_t or not up_t:
        return
    if cur_t == up_t:
        report.note("J漂移", "本项目与框架源同版本（v%s）" % upstream)
        return
    if cur_t > up_t:
        report.error("J漂移",
                     "本项目 agent.md v%s **高于** framework_source 的 v%s（来源指错，或项目已分叉自研）"
                     % (fw_version, upstream), "agent.md")
        return
    behind = (up_t[0] - cur_t[0]) * 100 + (up_t[1] - cur_t[1])
    report.warn("J漂移",
                "本项目停在 v%s，框架源已 v%s（落后约 %d 个次版本）——新机制不会自动生效到项目上，"
                "跑 `python scripts/migrate.py --dry-run` 看迁移计划" % (fw_version, upstream, behind),
                "agent.md")
    report.note("J漂移", "版本漂移：项目 v%s ← 框架 v%s；迁移计划见 scripts/migrate.py"
                % (fw_version, upstream))


# ---------------------------------------------------------------- I 运行模式声明一致性（v5.3.0）

# 快速模式的别名（与 `guide_audit.py` 的 is_quick 同源）。**默认档是快速模式**：
# 未声明、空值都按快速模式理解——这正是 v5.3.0 要固定下来的默认。
QUICK_ALIASES = ("quick", "fast", "rapid", "vibe", u"快速")
FULL_ALIASES = ("full", u"完整")

# 「已告知用户 / 用户明确指定」的凭据词。只认两处白名单位置，避免把正文里随手
# 提一句「完整模式」也算成凭据（那正是静默切档最常见的样子）。
_MODE_EVIDENCE = re.compile(u"(用户明确|用户指定|用户要求|用户点名|"
                            u"已告知用户|用户已知情|用户确认)")


def _mode_section(txt):
    """取《主控工作计划》里「运行模式」段的正文；**没有这一段就返回 None**。

    「只在段内找」是刻意的：整份文件里随手写一句「已告知用户」不算入口凭据，
    否则静默切档只要在别处补一句就蒙混过关。找不到段落 = 没有留痕（**不是「找不到就当有」**）。
    """
    m = re.search(u"^#{1,6}\\s*.*运行模式.*$", txt, re.M)
    if not m:
        return None
    seg = txt[m.end():]
    nxt = re.search(u"^#{1,6}\\s", seg, re.M)
    return seg[:nxt.start()] if nxt else seg


def mode_evidence(root):
    """找「切档有凭据」的纸面留痕：主控工作计划的「运行模式」段，或 config.json 里的 `_comment_mode`。

    **判据是纸面凭据，不是「感觉评估过了」**：`mode=full` 本身不是错（切档是一等公民），
    错的是**没有入口凭据**就躺在 full 上。
    """
    hits = []
    plan = os.path.join(root, "project-state", u"主控工作计划.md")
    if os.path.isfile(plan):
        seg = _mode_section(read(plan))
        if seg is not None and (_MODE_EVIDENCE.search(seg) or re.search(u"U[1-7]", seg)):
            hits.append(rel(root, plan))
    cfgp = os.path.join(root, "project-state", "config.json")
    if os.path.isfile(cfgp):
        raw = read(cfgp)
        if re.search(u'"_comment_mode\\s*:\\s*\"[^\"]*(已告知|用户明确|'
                     u'用户指定|入口凭据)', raw):
            hits.append(rel(root, cfgp))
    return hits


def check_mode_declaration(root, report, cfg, mode):
    """I 组：运行模式声明与档位是否自洽。

    **为什么要机器查**：在 v5.3.0 之前，`config.json` 的 `mode` 默认是 `full`、
    指令表又写着「主控判断模式」——**「用户什么都没说」于是被读成了「用户要 12 角色全流程」**。
    用户 2026-09-17 把它拧回来：默认快速模式，完整模式只有两个正当入口。
    旧默认还活在已开工的项目里，靠人记是记不住的。

    **边界**：本组只能核「留在纸面上的入口与依据」，**核不出评估是否真的做过**（同
    `guide_audit.py` 的覆盖面声明）——报告不得把「没报」说成「评估过且干净」。
    """
    m = str((cfg or {}).get("mode", "") or "").strip().lower()
    prof = str((cfg or {}).get("profile", "") or "").strip()

    if m in FULL_ALIASES:
        hits = mode_evidence(root)
        if hits:
            report.note("I运行模式", "`mode` = full 且入口凭据在 %s" % u"、".join(hits))
        else:
            report.error("I运行模式",
                         "`mode` = full 但**找不到入口凭据**（静默切档）——"
                         "v5.3.0 起完整模式只有两个正当入口：① 用户明确指定；"
                         "② 项目经理按 U1~U7 评估为大项目**并已告知用户**。"
                         "请在《主控工作计划》的「运行模式」段补留痕"
                         "（当前档位 + 怎么进来的），或改回 quick",
                         "project-state/config.json")
        if prof.upper().startswith("A") and mode == "project":
            report.note("I运行模式",
                        "完整模式 + profile=A：单环境下跑 12 角色全流程可能是刻意裁剪，仅提醒")
        return

    if m in QUICK_ALIASES:
        if prof.upper().startswith("C"):
            report.error("I运行模式",
                         "「快速模式却按三库要求」：`mode` = %s 但 `profile` = %s（标准三环境）——"
                         "两个字段说的是两件事：`mode` 决定走几个角色，`profile` 决定三库条款执行到哪一档。"
                         "快速模式确认后应把 `profile` 定到 A（单环境）"
                         "（迁移：`python scripts/migrate.py --only mode-explicit --apply`）"
                         % (m, prof), "project-state/config.json")
        elif prof.upper().startswith("B"):
            report.warn("I运行模式",
                        "快速模式 + `profile` = %s（双环境）——不是错误，但请确认是刻意留着的"
                        "（快速模式默认单环境）" % prof, "project-state/config.json")
        return

    if not m:
        report.note("I运行模式",
                    "未声明 `mode`，按 v5.3.0 默认理解为快速模式（建议补上显式声明以免歧义）")
        return

    report.warn("I运行模式",
                "`mode` = %r 不是认识的取值（只认 \"quick\" / \"full\"）——工具不知道该按哪套规则解释"
                % (cfg.get("mode"),), "project-state/config.json")


# ---------------------------------------------------------------- K 极速修复台账自洽（v5.12.0）

# v5.12.0 起「极速修复模式」（Turbo Repair）是**临时叠加档**，不进 config.json 的 `mode`：
# 只在用户点名关键词时启用，不派子代理、不建需求卡/测试卡、只跑 smoke/targeted、轮次间不留关卡。
# 它唯一留下的机器痕迹就是 `project-state/问题/极速修复台账.md` 里的 `machine:turbo` 机读块。
TURBO_LEDGER = os.path.join("project-state", u"问题", u"极速修复台账.md")
TURBO_OPEN = re.compile(r"<!--\s*machine:turbo\s*-->")
TURBO_CLOSE = re.compile(r"<!--\s*/machine:turbo\s*-->")

# **列头逐字比对**（剥首尾空白后）：顺序不许换、不许少列、不许改名。
# 改名与少列都必须变红——少一列意味着那一列**无法校核**（尤其 `exit`/`exit_reason`/`verified`，
# 少了它们就等于把「该退未退」「没独立验证」洗成通过）。
TURBO_COLUMNS = ("id", "at", "source", "basis", "symptom", "files", "depth",
                 "evidence", "pack", "exit", "exit_reason", "verified")

# `basis`（改动依据）认五种形状：既有需求卡、既有测试卡、用例库哨兵（v5.28.0 起 `CAS-NNN`）、
# 以上用 `+` 组合、显式 `no-card`。极速修复不得"把问题直接丢给模型乱改"——改动前必须先拿
# 既有需求卡/测试卡/用例库排查；确实没有对应卡时写 `no-card`（**如实声明**，不是缺陷）。
# 形状不符 = 脏数据（WARN），空值 = 结构性缺口（ERROR，先判空再判形状）。
TURBO_BASIS_CARD = re.compile(
    r"^(?:card-\d{3}|tc-\d{3}|CAS-\d{3})(?:(?:\+)(?:card-\d{3}|tc-\d{3}|CAS-\d{3}))*$")
TURBO_BASIS_NO_CARD = "no-card"

TURBO_ID = re.compile(r"^TRB-\d{3}$")
TURBO_DEPTHS = ("smoke", "targeted")
# `at` / `exit` 的时间格式：`YYYY-MM-DD HH:MM`，T 分隔也认（ISO 写法）。
TURBO_TS = re.compile(r"^\d{4}-\d{2}-\d{2}(?: |T)\d{2}:\d{2}$")


def _turbo_rows(txt):
    """取 machine:turbo 块里的表格数据行；**没有机读块返回 None**（与「块在但没数据行」区分开）。

    表格单元可能被 `|` 分隔后带首尾空格，故一律 strip 后再判；空行、分隔行（`|---|`）跳过。
    """
    mo = TURBO_OPEN.search(txt)
    if not mo:
        return None
    mc = TURBO_CLOSE.search(txt, mo.end())
    body = txt[mo.end():mc.start()] if mc else txt[mo.end():]
    rows = []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not any(cells):
            continue
        if set("".join(cells)) <= set("-: "):
            continue
        rows.append(cells)
    return rows


def check_turbo_ledger(root, report):
    """K 组：极速修复台账（`project-state/问题/极速修复台账.md`）的机器可核自洽性。

    **为什么要机器查**：极速修复模式靠"少做事"换速度——不派子代理、不建卡、只跑冒烟、轮次间不留关卡。
    它的全部约束就是**退出线**：只应在"改一两个文件、症状单一"的小修上叠加，一旦动到 ≥4 个文件或
    同一个症状反复打补丁，就必须退出极速、回正常流程。而这条线**只有台账记得住**：不查台账，
    "极速"就会从临时叠加档悄悄变成常驻档（该建卡的不建、该独立验证的不验、该收口的不收口），
    且事后完全看不出是哪一轮越的线。故本组核的是台账**自己写的东西自不自洽**。

    **12 列的含义**：`id` 台账号、`at` 发生时间、`source` 来源（用户反馈原话）、
    `basis` **改动依据**（改之前有没有查过既有需求卡/测试卡：`card-NNN` / `tc-NNN` /
    `card-NNN+tc-NNN` / `no-card`）、`symptom` 症状、`files` 本轮动到的文件数、
    `depth` 冒烟档位、`evidence` 自检证据路径、`pack` **面向客户的快速体验包**的项目根相对路径
    （`-` = 本轮未涉及交付/体验包）、`exit` 退出时间、`exit_reason` 退出原因、`verified` 是否独立验证。

    **判据**（照 agent.md 的极速修复条款逐条落地，一条不放宽成 WARN）：
    台账不存在 → INFO 跳过（项目从没用过极速 = 完全正常，**不是"没查"**）；
    台账在而无 `machine:turbo` 机读块 → ERROR（写成散文就无法校核）；列头不逐字等于
    `id | at | source | basis | symptom | files | depth | evidence | pack | exit | exit_reason | verified` → ERROR；
    逐行：列数 ≠ 12、`id` 不匹配 `^TRB-\\d{3}$`/重复/乱序、`exit` 与 `exit_reason` 一空一满、
    `depth` ∉ {smoke,targeted}（`full` 说明这轮本不该走极速）、`files` 不是正整数、
    `files` ≥ 4 而 `exit` 空（越过退出线）、同一 `symptom` ≥3 行且末行 `exit` 空（该退未退）、
    `verified` 为空、**`basis` 为空（改之前没有依据）**、**`pack` 为空（要么写路径、要么写 `-`）**
    → 各报 ERROR；`at`/`exit` 时间格式坏、`verified` 取值非 是/否、`basis` 形状不是那四种、
    `evidence` 指向的文件不存在、**`pack` 指向的路径不存在** → 各记 WARN。
    数据行 0 行合法（空台账不算错）。结尾汇总报出依据分布（有卡/无卡）与体验包条数。

    **边界（诚实边界）**：本组只核"台账自己写的东西自不自洽"，**核不出**这一轮到底有没有真派子代理、
    有没有真的只跑冒烟、`files` 是不是真只改了那么几个文件——台账可以写得很齐整而事实并非如此。
    **`basis` 只校形状**：核得出它长得像卡号，核不出那张卡是否真的对应这次问题（填一张无关的真卡，
    本组看不出来）；**`pack` 只校路径存在**：核不出包内容是否真能给客户。
    台账不存在 = 本项目未用过极速修复（INFO），**不等于"没查过"**，报告不得把它读成"干净"。
    """
    p = os.path.join(root, TURBO_LEDGER)
    if not os.path.isfile(p):
        report.note("K极速修复",
                    u"没有 project-state/问题/极速修复台账.md，视为本项目未用过极速修复模式，跳过")
        return
    where = rel(root, p)
    txt = read(p)
    rows = _turbo_rows(txt)
    if rows is None:
        report.error("K极速修复",
                     u"台账存在却没有 `machine:turbo` 机读块——没有机读块就无法校核，"
                     u"等于把台账写成了散文。请补 `<!-- machine:turbo -->` … `<!-- /machine:turbo -->` "
                     u"块（列头逐字为：%s）" % u" | ".join(TURBO_COLUMNS), where)
        return

    if tuple(rows[0]) != TURBO_COLUMNS:
        report.error("K极速修复",
                     u"machine:turbo 块的列头不是约定的 %d 列（顺序不许换、不许少列、不许改名）——"
                     u"应为 `%s`，实际是 `%s`"
                     % (len(TURBO_COLUMNS), u" | ".join(TURBO_COLUMNS), u" | ".join(rows[0])), where)
    data = [r for r in rows[1:] if r != list(TURBO_COLUMNS)]

    ids = []
    symptom_seen = {}
    in_turbo = exited = 0
    depths = {}
    checked = 0
    basis_with_card = basis_no_card = 0
    pack_rows = 0
    for i, cells in enumerate(data, 1):
        line_no = i + 1
        if len(cells) != len(TURBO_COLUMNS):
            report.error("K极速修复",
                         u"第 %d 行有 %d 列（应为 %d 列），该行无法校核：`%s`"
                         % (line_no, len(cells), len(TURBO_COLUMNS), u" | ".join(cells)), where)
            continue
        (rid, at, source, basis, symptom, files, depth,
         evidence, pack, exit_, exit_reason, verified) = cells

        # `basis` 改动依据：**先判空**（结构性缺口 → ERROR），**再判形状**（脏数据 → WARN）。
        # 极速修复的前提是"基于用户反馈 + 正常开发形成的既有需求卡/测试卡排查"，
        # 而不是把现象直接丢给模型乱改；确实无卡可依时必须写 `no-card`（如实声明，不是缺陷）。
        if not basis:
            report.error("K极速修复",
                         u"第 %d 行 `basis` 为空——改之前没有依据：极速修复必须基于用户反馈 + "
                         u"正常开发形成的既有需求卡/测试卡排查，不能把问题直接丢给模型乱改。"
                         u"请填卡号（`card-NNN` / `tc-NNN` / `CAS-NNN`，可 `+` 组合）或写 `no-card`"
                         % line_no, where)
        elif basis != TURBO_BASIS_NO_CARD and not TURBO_BASIS_CARD.match(basis):
            report.warn("K极速修复",
                        u"第 %d 行 `basis` = %r 不是认识的形状（只认 `card-NNN` / `tc-NNN` / "
                        u"`CAS-NNN`，可 `+` 组合 / `no-card`）——卡号查不到对应条目时，这次改动就没有依据可追"
                        % (line_no, basis), where)
        if TURBO_BASIS_CARD.match(basis or ""):
            basis_with_card += 1
        else:
            basis_no_card += 1

        # `pack` 面向客户的快速体验包：`-` = 本轮未涉及（不报）；空 = 没说清（同 `evidence` 口径报 ERROR）；
        # 其余按**项目根相对路径**判存在性，落空 → WARN（照 C 组与 `evidence` 同一口径，不升级成 ERROR）。
        if not pack:
            report.error("K极速修复",
                         u"第 %d 行 `pack` 为空——要么写快速体验包的项目根相对路径、"
                         u"要么写 `-`（本轮未涉及交付/体验包）" % line_no, where)
        elif pack != u"-":
            pack_rows += 1
            pk = pack.replace("\\", "/").strip("/")
            if not os.path.exists(os.path.join(root, *pk.split("/"))):
                report.warn("K极速修复",
                            u"第 %d 行 `pack` 指向的 `%s` 不存在——台账里写了快速体验包，"
                            u"项目里却找不到它，「客户拿到的东西」就无从追溯"
                            % (line_no, pack), where)

        if not TURBO_ID.match(rid):
            report.error("K极速修复",
                         u"第 %d 行 `id` = %r 不匹配 `TRB-NNN`（三位数字）、或重复、或未按出现顺序升序"
                         % (line_no, rid), where)
        else:
            ids.append(int(rid[4:]))
        if not (at or exit_):
            report.note("K极速修复", u"第 %d 行 `at` 与 `exit` 都空（明明有这一行）" % line_no)

        if bool(exit_) != bool(exit_reason):
            report.error("K极速修复",
                         u"第 %d 行 `exit` 与 `exit_reason` 一空一满（%s）——"
                         u"进了没记退 / 退了没记因；两个都空才是「仍在极速中」"
                         % (line_no, u"填了 exit 没写原因" if exit_ else u"写了原因却没记退出时间"),
                         where)

        if depth not in TURBO_DEPTHS:
            report.error("K极速修复",
                         u"第 %d 行 `depth` = %r 不在 {smoke, targeted}——"
                         u"写了 full 说明这一轮本不该走极速修复"
                         % (line_no, depth), where)
        depths[depth] = depths.get(depth, 0) + 1

        if not re.match(r"^\d+$", files) or int(files) <= 0:
            report.error("K极速修复",
                         u"第 %d 行 `files` = %r 不是正整数——无法判断是否越过退出线"
                         % (line_no, files), where)
        elif int(files) >= 4 and not exit_:
            report.error("K极速修复",
                         u"第 %d 行 `files` = %s（≥4）且 `exit` 为空——退出线是「一次修复动到 ≥4 个文件"
                         u"就必须退出极速、回正常流程」，该退未退" % (line_no, files), where)

        if not verified:
            report.error("K极速修复",
                         u"第 %d 行 `verified` 为空——必须显式写 `是`/`否`："
                         u"不写等于把「没独立验证」读成「验证过了」" % line_no, where)
        elif verified not in (u"是", u"否"):
            report.warn("K极速修复",
                        u"第 %d 行 `verified` = %r 不是认识的取值（只认 `是` / `否`）"
                        % (line_no, verified), where)

        for name, val in ((u"at", at), (u"exit", exit_)):
            if val and not TURBO_TS.match(val):
                report.warn("K极速修复",
                            u"第 %d 行 `%s` = %r 不是 `YYYY-MM-DD HH:MM` 格式"
                            % (line_no, name, val), where)

        if evidence:
            ev = evidence.replace("\\", "/").strip("/")
            if not os.path.exists(os.path.join(root, *ev.split("/"))):
                report.warn("K极速修复",
                            u"第 %d 行 `evidence` 指向的 `%s` 不存在——证据指针落空，"
                            u"这一轮「留了自检证据」就只是一句话" % (line_no, evidence), where)

        if symptom:
            symptom_seen.setdefault(symptom, []).append(bool(exit_))
        checked += 1
        if exit_:
            exited += 1
        else:
            in_turbo += 1

    for rid in sorted({r for r in ids if ids.count(r) > 1}):
        report.error("K极速修复",
                     u"`id` TRB-%03d 重复出现 %d 次（`id` 必须全局唯一）"
                     % (rid, ids.count(rid)), where)
    if ids and ids != sorted(ids):
        report.error("K极速修复",
                     u"`id` 未按出现顺序升序：%s（应按 `TRB-NNN` 递增登记）"
                     % u"、".join("TRB-%03d" % x for x in ids), where)
    for symptom, exits in symptom_seen.items():
        if len(exits) >= 3 and not exits[-1]:
            report.error("K极速修复",
                         u"同一 `symptom`「%s」已出现 %d 行且最后一行 `exit` 仍为空——"
                         u"同一问题反复打补丁仍不收口 = 该退未退（应退出极速、回正常流程开卡）"
                         % (symptom, len(exits)), where)

    mode_txt = u"、".join(u"%s %d" % (k, v) for k, v in sorted(depths.items())) or u"无记录"
    report.note("K极速修复",
                u"台账 %d 条：仍在极速中 %d / 已退出 %d；档位 %s；"
                u"依据分布：有卡 %d 条 / 无卡 %d 条（`no-card` 计入无卡）；体验包 %d 条（`pack` 非 `-`）"
                % (checked, in_turbo, exited, mode_txt,
                   basis_with_card, basis_no_card, pack_rows))


# ---------------------------------------------------------------- U 体验门自洽（v5.17.0）

# v5.17.0 起「体验门禁」分三层归属：**可判条款**归测试工程师（L1/L2/L3 出机器证据）、
# **判断类条款**归 UI设计师（Pass 2' 交付前第二遍走查）、**取舍放行**归产品经理（只裁决）。
# 它和极速修复一样，唯一留下的机器痕迹是机读块 `machine:ux`；但比极速修复更容易失真：
# 「客户看了说好」也能被写成一条记录，skipped 留空也能让门禁看起来"跑过了"。
# 故本组只做四件事：条款块结构可核、门禁映射不残缺、三类归属有人认领、账本自洽。
UX_CONTRACT = os.path.join("project-state", u"契约", u"EXPERIENCE.md")
UX_ROLES = os.path.join("project-state", u"契约", u"角色责任清单.md")
UX_OPEN = re.compile(r"<!--\s*machine:ux\s*-->")
UX_CLOSE = re.compile(r"<!--\s*/machine:ux\s*-->")

# 账本列头**逐字比对**：少一列就意味着那一列无法校核——尤其 `verdict` / `note` / `evidence`，
# 少了它们就等于把「skipped 没写原因」「没有证据」洗成通过。
UX_COLUMNS = ("id", "at", "card_or_tc", "clauses", "level", "verdict", "evidence", "note")
UX_ID = re.compile(r"^UXR-\d{3}$")
UX_TS = re.compile(r"^\d{4}-\d{2}-\d{2}(?: |T)\d{2}:\d{2}$")
UX_CLAUSE_ID = re.compile(r"^UX-[A-Z][A-Z0-9]*-\d{2}$")
UX_TARGET = re.compile(r"^(?:card-\d{3}|tc-\d{3})$")
UX_LEVELS = ("smoke", "targeted", "full")
UX_VERDICTS = ("pass", "fail", "skipped")
# 门禁映射表必须四层齐、三档齐——缺哪层就等于"那层没人跑"，缺哪档就等于"该档没定义下限"。
UX_GATE_TOKENS = (u"L1 静态", u"L2 运行时断言", u"L3 像素基线", u"L4 隔离评审",
                  u"smoke", u"targeted", u"full", u"machine:ux")
# 三类归属：可判条款 → 测试工程师；判断类 → UI设计师 Pass 2'；取舍放行 → 产品经理只裁决。
UX_ROLE_TOKENS = (u"R-qa-32", u"R-ui-23", u"R-pmgr-83", u"Pass 2'", u"UX 门")


def _ux_rows(txt):
    """取 machine:ux 块里的表格数据行；没有机读块返回 None（与"块在但 0 行"区分开）。"""
    mo = UX_OPEN.search(txt)
    if not mo:
        return None
    mc = UX_CLOSE.search(txt, mo.end())
    body = txt[mo.end():mc.start()] if mc else txt[mo.end():]
    rows = []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not any(cells):
            continue
        if set("".join(cells)) <= set("-: "):
            continue
        rows.append(cells)
    return rows


def _ux_clause_table(txt):
    """从体验契约里取「可验收条款（ux-clause）」表格行：返回 [(id, 层, 列数), ...]。

    **只认表格首列是 `UX-<节缩码>-NN` 的行**——条款正文里顺手提到 id 不算登记，
    否则一段说明文字就能让条款"看起来存在"，这正是伪条款最省事的伪造方式。
    """
    out = []
    for line in txt.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not cells:
            continue
        m = UX_CLAUSE_ID.match(cells[0])
        if m:
            out.append((cells[0], cells[3] if len(cells) > 3 else u"", len(cells)))
    return out


def check_ux_gate(root, report):
    """U 组：体验门禁（条款块 / 门禁映射 / 三类归属 / machine:ux 账本）的自洽性。

    **判据**：
    体验契约不存在 → INFO 跳过（v5.17.0 前的项目/最小测试树，属机制未同步，不是损坏）；
    契约在但没有任何 ux-clause 条款行 → ERROR（条款层没落地，体验门无从判）；
    条款行 id 重复 / 层不是 L1/L2/L3/L4（含 `L2+L4` 这类组合写法）→ ERROR；
    缺「体验门禁映射」节 / 缺「门禁条款强制说明」节 / 四层或缺档位映射的任一标记缺失 → ERROR；
    角色责任清单不存在 → INFO 跳过；在而缺 `R-qa-32`（体验门执行）/ `R-ui-23`（Pass 2'）/
    `R-pmgr-83`（只裁决）任一 → ERROR（三类归属里那一类就没人认领）；
    账本：无 `machine:ux` 块 → INFO 跳过；有块但无列头 / 列头不逐字 → ERROR；
    逐行：列数≠8、`id` 不匹配 `^UXR-\\d{3}$`/重复/乱序、`card_or_tc` 为空、`clauses` 为空、
    `level` ∉ {smoke,targeted,full}、`verdict` ∉ {pass,fail,skipped}、`verdict=pass` 而
    `level≠full`（越级结论）、`verdict=skipped` 而 `note` 空（没写原因）、`evidence` 为空
    → 各报 ERROR；`at` 时间格式坏、`card_or_tc` 不是 `card-NNN`/`tc-NNN`、`evidence` 指针不存在
    → 各记 WARN。`clauses` 的每个 id 必须能在契约里找到，找不到 = ERROR（契约缺口，应提 CR 回填，
    不许在账本里现场编条款）。

    **诚实边界**：本组核不出"断言到底跑没跑"、"证据文件是不是这次生成的"、
    "L4 走查是不是真做了判断"——账本可以写得很齐整而事实并非如此。
    门禁的真相在 `scripts/ux_gate/` 的工具 JSON 与基线图里，本组只保证**账本不能自相矛盾**。
    """
    pages = rel(root, os.path.join(root, UX_CONTRACT))
    if not os.path.isfile(os.path.join(root, UX_CONTRACT)):
        report.note("U体验门",
                    u"没有 %s（v5.17.0 前的项目或最小测试树），体验门 U 组跳过" % pages)
        return
    txt = read(os.path.join(root, UX_CONTRACT))

    # ---- U1 条款块结构
    rows = _ux_clause_table(txt)
    if not rows:
        report.error("U体验门",
                     u"体验契约里没有任何 ux-clause 条款行——条款层没落地，体验门无从判："
                     u"每节应有 `**可验收条款（ux-clause）**` + 五列表格"
                     u"（id | 条款 | 判据 | 层 | 证据形态）", pages)
    seen = {}
    for cid, layer, ncol in rows:
        seen[cid] = seen.get(cid, 0) + 1
        if ncol != 5:
            report.error("U体验门",
                         u"条款 %s 所在行有 %d 列（应为 5 列：id | 条款 | 判据 | 层 | 证据形态），"
                         u"该条款无法按结构校核" % (cid, ncol), pages)
        if layer not in (u"L1", u"L2", u"L3", u"L4", u"L1+L2", u"L1+L3", u"L2+L3",
                         u"L2+L4", u"L1+L4", u"L1+L2+L3"):
            report.error("U体验门",
                         u"条款 %s 的「层」= %r 不是 L1/L2/L3/L4 或其组合——层写不清，"
                         u"就意味着没人说得清这条该在哪个档位跑" % (cid, layer), pages)
    for cid, n in sorted(seen.items()):
        if n > 1:
            report.error("U体验门",
                         u"条款 id %s 重复出现 %d 次（id 只增不改、不回收，重复=两处判据会分叉）"
                         % (cid, n), pages)

    # ---- U2 门禁映射完整性
    for head in (u"体验门禁映射", u"门禁条款强制说明"):
        if head not in txt:
            report.error("U体验门",
                         u"体验契约缺「%s」节——四层门禁怎么跑、谁来判、哪档能写什么结论"
                         u"必须有唯一出处，否则执行时一定会各自解释" % head, pages)
    missing = [t for t in UX_GATE_TOKENS if t not in txt]
    if missing:
        report.error("U体验门",
                     u"体验契约的门禁映射不完整，缺：%s——四层要齐（L1/L2/L3/L4）、"
                     u"档位要齐（smoke/targeted/full）、账本列要写（machine:ux）"
                     % u"、".join(missing), pages)

    # ---- U3 三类归属是否有人认领
    roles = os.path.join(root, UX_ROLES)
    if not os.path.isfile(roles):
        report.note("U体验门", u"没有 %s，三类归属检查跳过" % rel(root, roles))
    else:
        rtxt = read(roles)
        where = rel(root, roles)
        miss = [t for t in UX_ROLE_TOKENS if t not in rtxt]
        if miss:
            report.error("U体验门",
                         u"角色责任清单没有覆盖体验门的三类归属，缺：%s——"
                         u"可判条款归测试工程师（R-qa-32）、判断类归 UI设计师（R-ui-23 的 Pass 2'）、"
                         u"取舍放行归产品经理（R-pmgr-83，只裁决）；缺哪类，那类条款就没人认领"
                         % u"、".join(miss), where)

    # ---- U4 machine:ux 账本自洽
    blocks = []
    for path in md_files(root, "project-state"):
        body = read(path)
        if UX_OPEN.search(body):
            blocks.append((path, body))
    if not blocks:
        report.note("U体验门",
                    u"project-state 下没有 `machine:ux` 账本（本轮无体验门记录，或项目从未跑过体验门），跳过")
        return

    clause_ids = set(cid for cid, _l, _n in rows)
    all_ids = []
    level_counts = {}
    verdict_counts = {}
    checked = 0
    for path, body in blocks:
        where = rel(root, path)
        mo = UX_OPEN.search(body)
        while mo:
            mc = UX_CLOSE.search(body, mo.end())
            if not mc:
                report.error("U体验门",
                             u"`machine:ux` 块没有闭合标签——未闭合的块会把后文全部吞进表格，"
                             u"账本与正文都不可核", where)
                break
            chunk = body[mo.end():mc.start()]
            rws = _ux_rows(chunk)
            if not rws:
                report.error("U体验门", u"`machine:ux` 块里没有表格（列头都没有，无法校核）", where)
            elif tuple(rws[0]) != UX_COLUMNS:
                report.error("U体验门",
                             u"machine:ux 块的列头不是约定的 %d 列（顺序不许换、不许少列、不许改名）"
                             u"——应为 `%s`，实际是 `%s`"
                             % (len(UX_COLUMNS), u" | ".join(UX_COLUMNS),
                                u" | ".join(rws[0])), where)
            else:
                for i, cells in enumerate([r for r in rws[1:] if r != list(UX_COLUMNS)], 1):
                    line_no = i + 1
                    if len(cells) != len(UX_COLUMNS):
                        report.error("U体验门",
                                     u"第 %d 行有 %d 列（应为 %d 列），该行无法校核：`%s`"
                                     % (line_no, len(cells), len(UX_COLUMNS), u" | ".join(cells)),
                                     where)
                        continue
                    (rid, at, target, clauses, level, verdict, evidence, note) = cells
                    checked += 1
                    level_counts[level] = level_counts.get(level, 0) + 1
                    verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1

                    if not UX_ID.match(rid):
                        report.error("U体验门",
                                     u"第 %d 行 `id` = %r 不匹配 `UXR-NNN`（三位数字）" % (line_no, rid),
                                     where)
                    else:
                        all_ids.append(int(rid[4:]))
                    if not target:
                        report.error("U体验门",
                                     u"第 %d 行 `card_or_tc` 为空——没有卡/用例号，这条记录无从追溯" % line_no,
                                     where)
                    elif not UX_TARGET.match(target):
                        report.warn("U体验门",
                                    u"第 %d 行 `card_or_tc` = %r 不是 `card-NNN` / `tc-NNN` 形状"
                                    % (line_no, target), where)
                    if not clauses:
                        report.error("U体验门",
                                     u"第 %d 行 `clauses` 为空——体验门结论必须回指契约条款 id，"
                                     u"否则这条记录在判什么就没人知道" % line_no, where)
                    else:
                        bad = [c.strip() for c in re.split(r"[,，]", clauses) if c.strip()
                               and c.strip() not in clause_ids]
                        if bad:
                            report.error("U体验门",
                                         u"第 %d 行 `clauses` 引用了契约里不存在的条款：%s——"
                                         u"对不上 = 契约缺口（应提 CR 回填 EXPERIENCE.md），"
                                         u"不许在账本里现场编条款" % (line_no, u"、".join(bad)), where)
                    if level not in UX_LEVELS:
                        report.error("U体验门",
                                     u"第 %d 行 `level` = %r 不在 {smoke, targeted, full}"
                                     % (line_no, level), where)
                    if not verdict:
                        report.error("U体验门",
                                     u"第 %d 行 `verdict` 为空——不写结论等于门禁没跑却在台账里占位"
                                     % line_no, where)
                    elif verdict not in UX_VERDICTS:
                        report.error("U体验门",
                                     u"第 %d 行 `verdict` = %r 不在 {pass, fail, skipped}"
                                     % (line_no, verdict), where)
                    else:
                        if verdict == u"pass" and level != u"full":
                            report.error("U体验门",
                                         u"第 %d 行 `level` = %r 却写了 `pass`——只有 full 档允许出 pass，"
                                         u"smoke/targeted 的 pass 是越级结论"
                                         % (line_no, level), where)
                        if verdict == u"skipped" and not note:
                            report.error("U体验门",
                                         u"第 %d 行 `verdict=skipped` 但 `note` 为空——skipped 必须写原因，"
                                         u"留空 = 未收口（那正是门禁造假的入口）" % line_no, where)
                    if not evidence:
                        report.error("U体验门",
                                     u"第 %d 行 `evidence` 为空——结论必须指向证据（脚本 JSON / 截图 / "
                                     u"基线 diff）；「客户看了说好」不属于证据" % line_no, where)
                    elif evidence not in (u"-", u"—"):
                        ev = evidence.replace("\\", "/").strip("/")
                        if not os.path.exists(os.path.join(root, *ev.split("/"))):
                            report.warn("U体验门",
                                        u"第 %d 行 `evidence` 指向的 `%s` 不存在——证据指针落空，"
                                        u"这条结论就只剩一句话" % (line_no, evidence), where)
                    elif verdict in (u"pass", u"fail"):
                        report.error("U体验门",
                                     u"第 %d 行 `verdict=%s` 却把 `evidence` 写成 `-`——"
                                     u"通过/失败都必须有证据；只有 skipped 允许免证据"
                                     % (line_no, verdict), where)
                    if at and not UX_TS.match(at):
                        report.warn("U体验门",
                                    u"第 %d 行 `at` = %r 不是 `YYYY-MM-DD HH:MM` 格式" % (line_no, at),
                                    where)
            mo = UX_OPEN.search(body, mc.end())

    if all_ids and all_ids != sorted(all_ids):
        report.error("U体验门",
                     u"`id` 未按出现顺序升序：%s（账本是时间序，乱序说明在补写历史）"
                     % u"、".join("UXR-%03d" % x for x in all_ids))
    for rid in sorted({r for r in all_ids if all_ids.count(r) > 1}):
        report.error("U体验门",
                     u"`id` UXR-%03d 重复出现 %d 次（`id` 必须全局唯一升序）"
                     % (rid, all_ids.count(rid)))
    mode_txt = u"、".join(u"%s %d" % (k, v) for k, v in sorted(level_counts.items())) or u"无记录"
    verdict_txt = u"、".join(u"%s %d" % (k, v) for k, v in sorted(verdict_counts.items())) or u"无记录"
    report.note("U体验门",
                u"体验契约条款 %d 条；machine:ux 账本 %d 条（%d 个文件）；档位 %s；结论 %s"
                % (len(set(clause_ids)), checked, len(blocks), mode_txt, verdict_txt))


# ---------------------------------------------------------------- 主流程

def collect_files(root):
    """agent.md + 全部 SKILL.md + project-state 下 md（作为交叉引用/引用存在性的语料）。"""
    out = []
    agent = os.path.join(root, "agent.md")
    if os.path.isfile(agent):
        out.append(agent)
    skills_dir = os.path.join(root, "skills")
    if os.path.isdir(skills_dir):
        for cur, _d, fs in os.walk(skills_dir):
            for f in fs:
                if f.endswith(".md"):
                    out.append(os.path.join(cur, f))
    out.extend(md_files(root, "project-state"))
    return sorted(set(out))


def parse_args(argv):
    """返回 (root, opts)；用法错误时 root=None 且 opts 为退出码。"""
    root = os.getcwd()
    opts = {"json": False, "quiet": False, "mode": None}
    for a in argv[1:]:
        if a == "--json":
            opts["json"] = True
        elif a == "--quiet":
            opts["quiet"] = True
        elif a == "--project":
            opts["mode"] = "project"
        elif a == "--framework":
            opts["mode"] = "framework"
        elif a in ("-h", "--help"):
            sys.stdout.write(__doc__)
            return None, 0
        elif a.startswith("-"):
            sys.stderr.write("未知参数：%s\n" % a)
            return None, 2
        else:
            root = a
    return os.path.abspath(root), opts


def main(argv):
    _reconfigure()
    root, opts = parse_args(argv)
    if root is None:
        return opts

    if not os.path.isfile(os.path.join(root, "agent.md")):
        sys.stderr.write("不是框架/项目根（缺 agent.md）：%s\n" % root)
        return 2
    if not os.path.isfile(os.path.join(root, "project-state", "config.json")):
        sys.stderr.write("不是框架/项目根（缺 project-state/config.json）：%s\n" % root)
        return 2

    report = Report()
    cfg, schema_ok = load_config(root, report)
    mode = opts["mode"] or detect_mode(cfg)
    label = "项目体检" if mode == "project" else "框架自查"

    agent_lines = read(os.path.join(root, "agent.md")).splitlines()
    files = collect_files(root)

    fw = check_versions(root, report)
    maxes = check_numbering(root, report, fw) or {}
    check_references(root, report, files, cfg)
    check_registry(root, report, cfg, schema_ok)
    check_registry_vs_disk(root, report)
    check_single_source(root, report, agent_lines)
    check_brief_skeleton(root, report, agent_lines, fw)
    check_roles(root, report, agent_lines)
    check_crossrefs(root, report, files, maxes)
    check_drift(root, report, cfg, fw, mode)
    check_mode_declaration(root, report, cfg, mode)
    check_turbo_ledger(root, report)
    check_workspace_map_drift(root, report)

    if opts["json"]:
        sys.stdout.write(json.dumps({
            "root": root,
            "mode": mode,
            "schema_ok": schema_ok,
            "ok": report.ok,
            "counts": {ERROR: report.n(ERROR), WARN: report.n(WARN), INFO: report.n(INFO)},
            "findings": [f.as_dict() for f in report.findings],
            "notes": report.infos,
        }, ensure_ascii=False, indent=2) + "\n")
        return 0 if report.ok else 1

    if not opts["quiet"]:
        for note in report.infos:
            sys.stdout.write("[INFO] %s｜%s\n" % (note["check"], note["message"]))
        sys.stdout.write("\n")
    for f in sorted(report.findings, key=lambda x: _LEVEL_ORDER[x.level]):
        where = ("（%s）" % f.where) if f.where else ""
        sys.stdout.write("[%s] %s｜%s%s\n" % (f.level, f.check, f.message, where))
    sys.stdout.write("\n%s：ERROR %d / WARN %d  —  %s\n"
                     % (label, report.n(ERROR), report.n(WARN),
                        "无阻塞问题" if report.ok else "存在 ERROR，须修复后再提交"))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))

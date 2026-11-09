#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用迁移器（migrate.py）——把「项目停在旧框架版本」变成一条可预览、可执行、幂等的路径。

**为什么要有它（2026-09-15 实测教训）**
框架在推进、项目在分叉：两个真实项目分别停在 v3.22.0 / v3.18.0，而框架已 v4.9.0，
中间的成本控制（v4.6.0）、可实施性关卡、复盘校准（v4.7.0）、派发简报缓存纪律（v4.8.0）
在两个项目上的产出**都是零**——没有任何一条通道能告诉用户"你落后了、代价是什么、怎么补"。
此前每次结构变更都写一次性脚本（`migrate_v317_card_folders.py`、`fix_v317_references.py`），
它们的共同问题：只解决"当时那一次"（于是补丁的补丁照样要写）、没人能从项目侧问出
"我该跑哪些迁移、还差什么"、迁移完没有统一的复查入口。

本脚本把它们收成一条链：
    ① 判（按 schema_version 与磁盘实况）→ ② 报计划（**默认 dry-run，什么都不改**）
    → ③ `--apply` 执行（改文件前先备份进 `tmp/`，可回滚）→ ④ 复查（自动跑 framework_lint.py）

**迁移判据是 config.json 的 `schema_version`，不是框架版本号**——框架版本每次都涨（加一行
说明也涨），拿它当迁移条件会被无关版本刷屏；结构版本只在"配置怎么解释"变了才涨。
两者都要看：结构版本决定"怎么读配置"，框架版本决定"缺哪些机制"（见 J 组漂移检查）。

**幂等**：每一步先看当前状态，已到位就跳过；重复 `--apply` 不产生第二次改动（有断言兜住）。

用法
----
    python scripts/migrate.py [根目录]                  # 只报计划（默认 dry-run）
    python scripts/migrate.py [根目录] --apply          # 执行（改文件前先备份到 tmp/）
    python scripts/migrate.py --list                    # 列出全部迁移步骤与判据
    python scripts/migrate.py [根目录] --only <步骤id> --apply
    python scripts/migrate.py [根目录] --json
退出码：0 = 无待办（全部到位）；1 = 有待迁移或待人工项；2 = 用法/根目录错误。
"""

import datetime as _dt
import io
import json
import os
import re
import shutil
import subprocess
import sys

CONFIG_SCHEMA = "1"
SCHEMA_HINT = "跑 `python scripts/migrate.py --dry-run <项目根>` 看迁移计划，再 `--apply` 执行"

SCHEMA_COMMENT = (
    "配置结构版本（v4.9.0），**与框架版本解耦**：只在 config.json 的**结构**变了才升"
    "（加字段不算，改字段含义/形态才算，如 v3.12 的 `paths.layout` 是字符串、v3.18 起是对象）。"
    "工具据此决定「按哪套结构解释本文件」：**读不到或认不出即报错并指路 scripts/migrate.py，"
    "而不是崩溃或猜**。迁移工具只认本字段，不认框架版本号。"
)

EXTERNAL_ROOTS_COMMENT = (
    "外部根（v4.9.0）：**不在项目内、但项目文档会引用**的全局工具/技能所在目录。"
    "`framework_lint.py` 的 C 组按此解析引用——不声明则只探测默认位置并记 INFO"
    "（实测：示例工作台B 46 条引用 ERROR 里 44 条是全局工具被当成缺失，声明后归零）。"
    "`~` 由脚本展开；**声明的根在本机不存在会被忽略并记 INFO**（别的机器没装 Trae 属正常，不算缺陷）。"
)

DEFAULT_EXTERNAL_ROOTS = {"skills": ["~/.zcode/skills"],
                          "scripts": ["~/.zcode/scripts"]}

GUIDANCE_COMMENT = (
    "指导模式开关（v5.0.0）：`on` / `off`，默认 `on`。**与运行模式（full/rapid）正交**——"
    "它管的是「机制有没有被用」，不是「走哪套流程」。`on` 时 `scripts/guide_audit.py` 的发现项"
    "进 ⑥ 验收前置检查第 15 项（关口类 ERROR 未清零不得进验收）；`off` 时该项不适用，"
    "**须在项目计划注明依据**（不许现场默认关掉）。它不绑在项目经理角色上：项目经理未启用时"
    "由主控内联执行（复用「临时兼岗」，不新造机制）。"
)

MODE_COMMENT = (
    "运行模式（v5.3.0 起**默认 quick**）：`quick`=快速模式（未判为大项目的一律走这里）/ "
    "`full`=完整模式（12 角色全流程）。**完整模式只有两个正当入口**：① 用户明确指定；"
    "② 项目经理按 U1~U7 评估为大项目**并已告知用户**。**不许以「这看起来是个大项目」静默切档**；"
    "判不准时按 `quick` 起步——正向切档只是接着往下走，反向切档会让已铺开的契约与关卡变成沉没成本。"
    "**切档必须在《主控工作计划》的「运行模式」段留痕**（当前档位 + 怎么进来的）；"
    "`scripts/framework_lint.py` 的 I 组按此判 `mode=full` 有没有入口凭据。"
    "定了 `quick` 还应同步把 `profile` 定到 A（单环境），别留「快速模式却按三库要求」的矛盾。"
)

# 指导发现台账（v5.1.0，G11 的数据源；**v5.24.0 六列升七列**）。**内容是团队追加的**，脚本只保证"文件在、表头对"。
# 表头结构定义在这里而不是散在文档里：G11 按这七列解析，列序变了就解析不出来。
# **列数自适应**：G11 同时认七列（v5.24.0+ 新台账）与六列（v5.1.0 老台账，第 3 列是「场景」）——
# 老项目不强制迁移也能跑；迁移工具**只建不改**，已有台账一律不碰（见 apply_guidance_ledger）。
LEDGER_TEMPLATE = """# 指导发现台账

> 用途：登记 **G01–G11 抓不到、靠人或用户才发现**的「机制被绕过」与「机制不好用」事件，并保证每条都有归属。
> 处置见 `skills/项目经理/指导手册.md` 的 G11 与 §7；纪律见 注意事项 43 与 62（v5.24.0 摩擦即登记）。
> 七列语义：**触发原话**照抄用户当场那句话（不改写——改写就等于替用户做结论；**一句话、≤120 字，超了就截断并挂 `@<会话指针>`，不许贴整段对话**——本表是摩擦点的索引，不是对话存档）；
> **摩擦类型**五选一——`绕过`（人主动不守规矩）／`不合用`（想守规矩但守不下来）／`没写清`（条款不存在或被加载层挡住）／
> `默认值错`（模板默认值与实际不符）／`验收太晚`（问题一直拖到用户手里才发现）；
> **现有检查能否抓到**填的是"发现当时"的判断（是/否）；**归属**填的是"现在由谁看着它"，
> 可以是后来才新增的检查号。归属只有三类，都必须真实存在：
>   ① 检查号（如 `G05`，须在 `scripts/guide_audit.py` 的 CATALOG 里）；
>   ② 变更单号（`CR-YYYYMMDD-NNN`，须在 `问题/变更申请单/` 下真有此单）；
>   ③ 检查工具脚本名（如 `path_audit.py`，须在 `scripts/` 下真有此脚本）。
> **三类都没有 = 零归属 = 未收口**，`scripts/guide_audit.py` 的 G11 直接报 ERROR。
> **摩擦类型空着 = 还是草稿、没转正**（G11 报 WARN）：复盘时按类型聚类，同类 ≥3 条就是本迭代该改的条款。
> 本表由团队按需追加；**表头结构由 `scripts/migrate.py` 维护**（要改列请改脚本）。

| 编号 | 发现日期 | 触发原话（不改写，可带 @会话指针） | 摩擦类型 | 现有检查能否抓到 | 归属 | 状态 |
|---|---|---|---|---|---|---|
"""

# 步骤状态
DONE, TODO, MANUAL, NA = "done", "todo", "manual", "na"
STATE_LABEL = {DONE: "已到位", TODO: "待迁移", MANUAL: "需人工", NA: "不适用"}
_ORDER = {TODO: 0, MANUAL: 1, DONE: 2, NA: 3}


def _reconfigure():
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


# ---------------------------------------------------------------- 写入器（备份 + 幂等）

class Writer(object):
    """所有改动都必须经它——**改前先备份、内容相同不写**。

    两条纪律都有来由：
    - 备份：迁移是"批量改项目文件"，没有回滚路径的自动化工具不该被允许跑（v3.17 迁移脚本
      定的规矩，此处沿用），备份落 `tmp/`（唯一临时区）+ 目标相对路径，核对无误后可删。
    - 相同不写：幂等不能只靠"判断状态"，也要防"重写一遍内容其实没变"——后者会让
      git diff 永久变脏、也让人以为迁移干了活。**返回 False = 确实没动它。**
    """

    def __init__(self, root, apply=False, backup=True):
        self.root = root
        self.apply = apply
        self.backup = backup
        self.stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.bakdir = None
        self.touched = []
        self.skipped_same = []

    def _ensure_backup(self, path):
        if not self.backup or self.bakdir is None:
            return
        if not os.path.isfile(path):
            return
        dst = os.path.join(self.bakdir, rel(self.root, path))
        if os.path.exists(dst):
            return
        d = os.path.dirname(dst)
        if d and not os.path.isdir(d):
            os.makedirs(d)
        shutil.copy2(path, dst)

    def write(self, path, text):
        """写文件。dry-run 时只登记；内容相同时记入 skipped_same 并返回 False。"""
        old = read(path) if os.path.isfile(path) else None
        if old == text:
            self.skipped_same.append(rel(self.root, path))
            return False
        if not self.apply:
            self.touched.append(rel(self.root, path))
            return True
        if self.backup and self.bakdir is None:
            self.bakdir = os.path.join(self.root, "tmp", "migrate_backup_" + self.stamp)
        self._ensure_backup(path)
        d = os.path.dirname(path)
        if d and not os.path.isdir(d):
            os.makedirs(d)
        with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        self.touched.append(rel(self.root, path))
        return True

    def mkdir(self, path):
        if os.path.isdir(path):
            return False
        if self.apply:
            os.makedirs(path)
        self.touched.append(rel(self.root, path) + "/")
        return True

    def copy_from(self, src, dst):
        """从框架源复制模板文件（内容相同则跳过）。"""
        if not os.path.isfile(src):
            return False
        return self.write(dst, read(src))


# ---------------------------------------------------------------- 步骤 1：config schema

def _load_cfg(root):
    p = os.path.join(root, "project-state", "config.json")
    if not os.path.isfile(p):
        return p, None, "config.json 不存在"
    try:
        cfg = json.loads(read(p))
    except Exception as exc:
        return p, None, "config.json 不是合法 JSON：%s" % exc
    if not isinstance(cfg, dict):
        return p, None, "config.json 顶层应为对象，实际是 %s" % type(cfg).__name__
    return p, cfg, None


def detect_config_schema(root, ctx):
    _p, cfg, err = _load_cfg(root)
    if err:
        return MANUAL, err + "——先修好 JSON 再谈迁移"
    sv = cfg.get("schema_version")
    if sv is None:
        return TODO, "旧版配置缺 `schema_version`（工具无法判断该按哪套结构解释）"
    if str(sv) != CONFIG_SCHEMA:
        return MANUAL, ("`schema_version`=%s，本工具支持 %s——**本机框架比项目旧或不支持该版本**，"
                        "请先更新框架再迁移" % (sv, CONFIG_SCHEMA))
    return DONE, "`schema_version`=%s" % sv


def apply_config_schema(root, ctx, w):
    p, cfg, err = _load_cfg(root)
    if err or "schema_version" in cfg:
        return []
    text = read(p)
    ins = ('{\n  "schema_version": "%s",\n  "_comment_schema_version": %s,\n'
           % (CONFIG_SCHEMA, json.dumps(SCHEMA_COMMENT, ensure_ascii=False)))
    new, n = re.subn(r"^\{\s*\n", ins, text, count=1)
    if not n:                      # 单行 JSON：退化为在 { 后插入
        new = re.sub(r"^\{", '{"schema_version": "%s", ' % CONFIG_SCHEMA, text, count=1)
    if w.write(p, new):
        return ["config.json 顶部补 `schema_version: \"1\"`（原有键序与内容不变，只插两行）"]
    return []


# ---------------------------------------------------------------- 步骤 2：external_roots

def detect_external_roots(root, ctx):
    _p, cfg, err = _load_cfg(root)
    if err:
        return NA, "config.json 不可读（已在上一项报告）"
    if cfg.get("external_roots"):
        return DONE, "已声明 `external_roots`"
    return TODO, ("未声明 `external_roots`——引用全局工具/技能的项目会被 C 组判成"
                  "「引用了不存在的脚本」（实测 44 条假阳性）")


def apply_external_roots(root, ctx, w):
    p, cfg, err = _load_cfg(root)
    if err or cfg.get("external_roots"):
        return []
    text = read(p)
    block = ('  "external_roots": %s,\n  "_comment_external_roots": %s,\n'
             % (json.dumps(DEFAULT_EXTERNAL_ROOTS, ensure_ascii=False),
                json.dumps(EXTERNAL_ROOTS_COMMENT, ensure_ascii=False)))
    if re.search(r'^\s*"paths"\s*:', text, re.M):
        new, n = re.subn(r'^(\s*)"paths"\s*:', lambda m: block + m.group(1) + '"paths":',
                         text, count=1, flags=re.M)
    else:
        new, n = re.subn(r"\n?(\s*)\}\s*$", lambda m: "\n" + block.rstrip(",\n") + "\n}\n",
                         text, count=1)
    if not n:
        return []
    if w.write(p, new):
        return ["config.json 补 `external_roots`（skills / scripts 两类外部根，按默认位置预填）"]
    return []


# ---------------------------------------------------------------- 步骤 3：guidance 开关

def detect_guidance(root, ctx):
    _p, cfg, err = _load_cfg(root)
    if err:
        return NA, "config.json 不可读（已在上一项报告）"
    g = cfg.get("guidance")
    if g in ("on", "off"):
        return DONE, "`guidance` = %s" % g
    if g is None:
        return TODO, ("未声明 `guidance`——v5.0.0 的指导模式靠它开关："
                      "关掉会少掉「需求没建卡 / 环境被降级 / 变更没审批就执行」这类检查")
    return MANUAL, "`guidance` = %r，取值只认 \"on\" / \"off\"" % (g,)


def apply_guidance(root, ctx, w):
    p, cfg, err = _load_cfg(root)
    if err or "guidance" in cfg:
        return []
    text = read(p)
    block = ('  "guidance": "on",\n  "_comment_guidance": %s,\n'
             % json.dumps(GUIDANCE_COMMENT, ensure_ascii=False))
    if re.search(r'^\s*"paths"\s*:', text, re.M):
        new, n = re.subn(r'^(\s*)"paths"\s*:', lambda m: block + m.group(1) + '"paths":',
                         text, count=1, flags=re.M)
    else:
        new, n = re.subn(r"\n?(\s*)\}\s*$", lambda m: "\n" + block.rstrip(",\n") + "\n}\n",
                         text, count=1)
    if not n:
        return []
    if w.write(p, new):
        return ["config.json 补 `guidance: \"on\"`（指导模式默认开；关掉须在项目计划注明依据）"]
    return []


# ---------------------------------------------------------------- 步骤 4：paths.layout 转对象

# 八域（v4.5.0）的默认相对路径：命中即自动填，未命中留 `<待填>` 交人工。
DOMAIN_DEFAULTS = [("开发记忆区", "project-state"), ("代码区", "repos"),
                   ("客户交互区", "交付"), ("内部产物区", "产物"),
                   ("环境与工具链", "环境配置"), ("输入素材区", "素材"),
                   ("运行期数据区", "data"), ("临时区", "tmp")]


def detect_layout(root, ctx):
    _p, cfg, err = _load_cfg(root)
    if err:
        return NA, "config.json 不可读（已在上一项报告）"
    paths = cfg.get("paths")
    if not isinstance(paths, dict):
        return MANUAL, "`paths` 段缺失或不是对象——路径注册表不存在，须按「统一路径管理」重建"
    lay = paths.get("layout")
    if isinstance(lay, dict):
        pend = [k for k, v in lay.items() if not k.startswith("_") and re.search(r"<[^>]+>", str(v))]
        if pend:
            return MANUAL, "`paths.layout` 已是对象，但有 %d 项仍是 `<待填>` 占位：%s" % (len(pend), pend)
        return DONE, "`paths.layout` 是对象（八域 → 相对路径）"
    if lay is None:
        return TODO, "缺 `paths.layout`（八域 → 相对路径的对照表）"
    return TODO, ("`paths.layout` 是 %s（v3.12 时代写法：一段说明文字）——"
                  "**旧 schema 会让框架体检直接崩溃**（`layout.items()` 抛 AttributeError，"
                  "整份报告一行都出不来）" % type(lay).__name__)


def apply_layout(root, ctx, w):
    p, cfg, err = _load_cfg(root)
    if err:
        return []
    paths = cfg.get("paths")
    if not isinstance(paths, dict) or isinstance(paths.get("layout"), dict):
        return []
    old = paths.get("layout")
    # 项目已登记的实际路径若与八域默认名一致，直接沿用；否则留占位交人工（不猜域映射）。
    top = {k: v for k, v in paths.items()
           if not k.startswith("_") and k != "layout" and isinstance(v, str)}
    laid = {}
    for dom, default in DOMAIN_DEFAULTS:
        hit = None
        for k, v in top.items():
            if str(v).split("#")[0].strip().rstrip("/") == default:
                hit = v
        laid[dom] = hit or "<待填>"
    comment = ("域注册表用途说明（由 migrate.py 从旧写法转来）：①开发记忆区 ②代码区 ③客户交互区（对外件）"
               "④内部产物区 ⑤环境与工具链 ⑥输入素材区 ⑦运行期数据区 ⑧临时区；实际路径以 `paths` 顶层"
               "同名各项为准（v4.5.0 起两处一一对应，缺项由 `framework_lint.py` 报错）。**`<待填>` 需人工"
               "补真实相对路径**——工具不猜域映射（猜错比空缺更难发现）。")
    if isinstance(old, str) and old.strip():
        comment += " 旧写法原文（迁移前）：%s" % old.strip()
    obj = {"_comment": comment}
    obj.update(laid)
    literal = json.dumps(obj, ensure_ascii=False, indent=6)
    literal = "\n".join([literal.splitlines()[0]] +
                        ["    " + l for l in literal.splitlines()[1:]])
    text = read(p)
    pat = re.compile(r'("layout"\s*:\s*)("(?:[^"\\]|\\.)*"|null)', re.S)
    new, n = pat.subn(lambda m: m.group(1) + literal, text, count=1)
    if not n:
        return []
    if w.write(p, new):
        filled = len([1 for v in laid.values() if v != "<待填>"])
        return ["`paths.layout` 由说明字符串转为八域对象（自动填 %d 项、`<待填>` %d 项，"
                "旧原文保留在 `_comment`）" % (filled, 8 - filled)]
    return []


# ---------------------------------------------------------------- 步骤 4：内联清单拆到契约文件

# agent.md 是**每次派发都要进上下文**的文件；把编号清单本体搬出去是 v4.9.0 的减重主线。
# 搬走的只是位置：`N. ` 编号即编号契约（B 组查连续性、G 组按编号校验交叉引用），一条都不许重排。
SPLIT_SECTIONS = [
    ("验收标准", "project-state/验收标准.md", r"^##\s*验收标准", r"^#{1,2} "),
    ("注意事项", "project-state/注意事项.md", r"^##\s*注意事项", r"^#{1,2} "),
]

POINTER_TMPL = """> **清单本体已移至 `{target}`（v4.9.0）**——本节只留指针。**改清单请改那个文件**：
> `N. ` 编号即编号契约（`scripts/framework_lint.py` 的 B 组查连续性、G 组按编号校验交叉引用），
> 搬走的只是位置，**编号一条都不许重排**。为什么搬：agent.md 是每次派发都要进上下文的文件，
> 把几十条清单住在里面，等于每一轮派发都为它付一次 token。
"""


def _find_section(lines, head, nxt):
    start = None
    for i, line in enumerate(lines):
        if re.match(head, line):
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if re.match(nxt, lines[j]):
            end = j
            break
    return start, end


def detect_split(root, ctx):
    agent = os.path.join(root, "agent.md")
    if not os.path.isfile(agent):
        return NA, "agent.md 不存在"
    lines = read(agent).splitlines()
    pending = []
    for label, target, head, nxt in SPLIT_SECTIONS:
        b = _find_section(lines, head, nxt)
        tp = os.path.join(root, *target.split("/"))
        n = len([1 for l in lines[b[0]:b[1]] if re.match(r"^\d+\.\s", l)]) if b else 0
        if n:
            pending.append("%s（%d 条，在 agent.md）" % (label, n))
        elif not os.path.isfile(tp):
            # 拆完之后 agent.md 仍留 `## <标签>` 标题（**指针小节**），故不能只看"小节在不在"。
            # 判据必须是「小节里还有没有编号项」：**0 条 + 目标文件在 = 已是指针态**；
            # 0 条 + 目标文件不在 = 清单被清空且没搬走（真丢失）。
            pending.append("%s（agent.md 与 %s 都没有——清单丢了？）" % (label, target))
    if pending:
        return TODO, "；".join(pending)
    return DONE, "两份清单均已在契约文件，agent.md 只留指针"


def apply_split(root, ctx, w):
    agent = os.path.join(root, "agent.md")
    if not os.path.isfile(agent):
        return []
    text = read(agent)
    lines = text.splitlines(True)
    actions = []
    # 从后往前替换，避免前面的改动挪动后面的行号
    spans = []
    plain = [l.rstrip("\n") for l in lines]
    for label, target, head, nxt in SPLIT_SECTIONS:
        b = _find_section(plain, head, nxt)
        if not b:
            continue
        # **只搬"真有编号项"的小节**：拆完之后 agent.md 里留的是同名指针小节（0 条），
        # 若不加这道闸，第二次 --apply 会把指针当成清单本体写回目标文件，**把真清单覆盖掉**。
        # 幂等不能只靠 detect 判状态，写入侧必须自己拒绝对空小节动手（数据安全级保护）。
        if not any(re.match(r"^\d+\.\s", l) for l in plain[b[0]:b[1]]):
            continue
        spans.append((b[0], b[1], label, target))
    for start, end, label, target in sorted(spans, reverse=True):
        body = "".join(lines[start:end]).rstrip("\n") + "\n"
        item_count = len([1 for l in body.splitlines() if re.match(r"^\d+\.\s", l)])
        header = ("<!-- 本文件由 scripts/migrate.py（v4.9.0）从 agent.md 拆出：清单本体只有这一份，"
                  "agent.md 只留指针。改动请改本文件。 -->\n\n"
                  "> 版本：随大型项目开发框架 v4.9.0\n\n")
        target_path = os.path.join(root, *target.split("/"))
        if w.write(target_path, header + body):
            actions.append("`%s` ← 从 agent.md 拆出「%s」（%d 条编号项，正文逐字未改）"
                           % (target, label, item_count))
        pointer = "## %s\n\n%s" % (label, POINTER_TMPL.format(target=target))
        lines[start:end] = [pointer]
        actions.append("agent.md「%s」小节改为指针（%d 行 → %d 行）"
                       % (label, end - start, len(pointer.splitlines())))
    if actions:
        w.write(agent, "".join(lines))
    return actions


# ---------------------------------------------------------------- 步骤 4b：agent.md 描述瘦身

# agent.md 的 frontmatter `description` 在真实仓库里都长成了**版本编年史**（框架本体 19,169 字符，
# 从 v3.6.0 一路记到当前），而它是**每次加载 agent.md / 每次派发子代理都要进上下文**的一段文本。
# 减重不改信息：编年史原文整体搬进 `project-state/记忆/框架编年.md`（逐字保留，可追溯），
# description 只留"是什么 / 适用什么 / 不适用什么 / 去哪找细节"。
DESC_MAX = 2000
CHRONICLE_REL = "project-state/记忆/框架编年.md"

DESC_SUMMARY = (
    "基于共享工作区的多角色事件驱动协作 Agent（大型项目开发框架）。12 个专业角色在共享工作区并行开发，"
    "通过需求卡（card-NNN）+ 系统级测试卡（tc-NNN）+ CR 变更请求实现上下游双向协商、架构演进与反馈循环；"
    "主控 agent.md 负责编排、裁决与验收前置检查，各角色独立 AI 上岗、产出强制落盘（进度 json + 描述性文件，"
    "没有落盘 = 没干）。**适用**：多模块 / 多角色 / 多里程碑的系统级代码工程（web / desktop / mobile-native / "
    "game 四设计域），需要长时无人值守编排、成本熔断、可实施性关卡与迭代复盘的项目。**不适用**：单文件小工具、"
    "一次性脚本、纯问答。机制清单见 `project-state/注意事项.md`，验收判据见 `project-state/验收标准.md`，"
    "版本编年（各机制自哪版起、怎么用）见 `project-state/记忆/框架编年.md`。"
)


def _read_description(root):
    """取 agent.md frontmatter 的 description 原文。

    **不能用 json/yaml 解析**：真实文件里 description 内有未转义的双引号（框架本体 180 个），
    严格解析器会判非法。宿主用的是宽松解析（取该行首个 `"` 到末个 `"` 之间），本工具照此对齐。
    """
    p = os.path.join(root, "agent.md")
    if not os.path.isfile(p):
        return p, None
    for line in read(p).splitlines():
        if line.startswith("description:"):
            v = line[len("description:"):].strip()
            if v.startswith('"') and v.endswith('"') and len(v) >= 2:
                return p, v[1:-1]
            return p, v
    return p, None


def detect_slim_desc(root, ctx):
    _p, desc = _read_description(root)
    if desc is None:
        return NA, "agent.md 无 frontmatter description（或文件不存在）"
    if len(desc) > DESC_MAX:
        return TODO, ("description %d 字符（阈值 %d）——它是版本编年史而不是描述；"
                      "每次加载 agent.md / 派发子代理都要为它付一次 token" % (len(desc), DESC_MAX))
    return DONE, "description %d 字符（≤ %d）" % (len(desc), DESC_MAX)


def apply_slim_desc(root, ctx, w):
    p, desc = _read_description(root)
    if desc is None or len(desc) <= DESC_MAX:
        return []
    chro = os.path.join(root, *CHRONICLE_REL.split("/"))
    stamp = _dt.date.today().isoformat()
    head = ""
    if os.path.isfile(chro):
        head = read(chro)
    else:
        head = ("# 框架编年（版本历史与机制来历）\n\n"
                "> **这份文件回答两个问题**：①某机制是哪个版本加的、当时为了解决什么；"
                "②本项目落后框架时，差的是哪些东西（`scripts/migrate.py` 与 "
                "`scripts/framework_lint.py` 的 J 组漂移检查都以此为依据）。\n"
                "> **它是历史记录，不是当前规格**——当前规格以 agent.md / 注意事项.md / 验收标准.md "
                "为准；两者冲突时以现行文件为准。\n")
    block = ("\n\n## agent.md description 迁移原文（%s，由 scripts/migrate.py 从 description 字段整体搬出）\n\n"
             "> 以下为迁移前的 description 全文（%d 字符），**逐字保留**：它是各机制「哪版起的、为什么」"
             "的原始记录。新的 description 只留适用面与指针。\n\n%s\n" % (stamp, len(desc), desc))
    actions = []
    if w.write(chro, head.rstrip("\n") + block):
        actions.append("`%s` ← 收下 description 原文 %d 字符（逐字保留，可追溯）" % (CHRONICLE_REL, len(desc)))
    text = read(p)
    new_line = 'description: "%s"' % DESC_SUMMARY
    new, n = re.subn(r"^description:.*$", lambda m: new_line, text, count=1, flags=re.M)
    if n and w.write(p, new):
        actions.append("agent.md description %d → %d 字符（保留适用面与指针，去掉编年史）"
                       % (len(desc), len(DESC_SUMMARY)))
    return actions


# ---------------------------------------------------------------- 步骤 5：卡目录与模板

CARD_ASSETS = [("project-state/需求卡/需求卡模板.md", "需求卡模板.md"),
               ("project-state/测试卡/测试卡模板.md", "测试卡模板.md")]


def _framework_root():
    """本脚本所在仓库的根（框架被复制进项目后，`../` 仍是框架根，模板就在那）。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def detect_card_dirs(root, ctx):
    missing = []
    for relp, _n in CARD_ASSETS:
        p = os.path.join(root, *relp.split("/"))
        if not os.path.isfile(p):
            missing.append(relp)
    if not missing:
        return DONE, "需求卡/测试卡目录与模板齐备"
    return TODO, "缺 %s" % missing


def apply_card_dirs(root, ctx, w):
    src_root = _framework_root()
    actions = []
    for relp, name in CARD_ASSETS:
        dst = os.path.join(root, *relp.split("/"))
        if os.path.isfile(dst):
            continue
        src = os.path.join(src_root, *relp.split("/"))
        if not os.path.isfile(src):
            actions.append("**需人工**：框架源里找不到模板 `%s`（框架根=%s），请手工创建" % (relp, src_root))
            continue
        w.mkdir(os.path.dirname(dst))
        if w.copy_from(src, dst):
            actions.append("建 `%s`（从框架模板复制）" % relp)
    cards = os.path.join(root, "project-state", "需求卡")
    if os.path.isdir(cards):
        flat = [f for f in os.listdir(cards) if re.match(r"^card-\d+\.md$", f)]
        if flat:
            actions.append("检出 %d 个平铺卡（card-NNN.md）——**卡卷宗迁移（v3.17.0）请单独跑**："
                           "`python scripts/migrate.py --only card-folders --apply`" % len(flat))
    return actions


# ---------------------------------------------------------------- 步骤 6：卡卷宗（包装 v3.17 脚本）

def detect_card_folders(root, ctx):
    cands = [("project-state", "需求卡", r"^card-\d+\.md$"),
             ("project-state", "测试卡", r"^tc-\d+\.md$"),
             ("project-state", "思路", None)]
    flat = []
    d = os.path.join(root, "project-state", "需求卡")
    if os.path.isdir(d):
        flat += [f for f in os.listdir(d) if re.match(r"^card-\d+\.md$", f, re.I)]
    d = os.path.join(root, "project-state", "测试卡")
    if os.path.isdir(d):
        flat += [f for f in os.listdir(d) if re.match(r"^tc-\d+\.md$", f, re.I)]
    if flat:
        return TODO, "%d 个平铺卡/测试卡待转卡卷宗（一卡一目录）" % len(flat)
    return DONE, "无平铺卡（已是卡卷宗结构，或尚无卡）"


def apply_card_folders(root, ctx, w):
    script = os.path.join(_framework_root(), "scripts", "migrate_v317_card_folders.py")
    if not os.path.isfile(script):
        return ["**需人工**：找不到 `scripts/migrate_v317_card_folders.py`（框架源=%s）"
                % _framework_root()]
    cmd = [sys.executable, script, root] + (["--apply"] if w.apply else [])
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (r.stdout or "") + (r.stderr or "")
    tail = [l for l in out.splitlines() if l.strip()][-6:]
    head = ["跑 `migrate_v317_card_folders.py%s`（返回码 %d）"
            % (" --apply" if w.apply else "（dry-run）", r.returncode)]
    return head + ["    " + l.strip()[:150] for l in tail]


# ---------------------------------------------------------------- 步骤 7：框架源指针（人工）

def detect_framework_source(root, ctx):
    _p, cfg, err = _load_cfg(root)
    if err:
        return NA, "config.json 不可读（已在上一项报告）"
    v = cfg.get("framework_source")
    if not v or not isinstance(v, str) or not str(v).strip():
        return MANUAL, "未声明 `framework_source`——J 组无法回答「本项目落后框架多少」"
    if re.search(r"<[^>]+>", str(v)):
        return MANUAL, "`framework_source` 仍是 `<待填>` 占位：填上框架源仓库路径即可比对版本漂移"
    return DONE, "`framework_source` = %s" % v


# ---------------------------------------------------------------- 步骤：指导发现台账（v5.1.0）

def _ledger_path(root):
    return os.path.join(root, "project-state", "记忆", "指导发现台账.md")


def detect_guidance_ledger(root, ctx):
    p = _ledger_path(root)
    if os.path.isfile(p):
        return DONE, "`project-state/记忆/指导发现台账.md` 已在"
    return TODO, ("缺指导发现台账——**自增强回路没有入口**：靠人或用户发现的绕过没有地方登记，"
                  "G01–G11 这张清单就永远不会变长（没台账 ≠ 没绕过，是没人登记）")


def apply_guidance_ledger(root, ctx, w):
    p = _ledger_path(root)
    if os.path.isfile(p):
        # **只建不改**：台账里装的是人写的事件记录，迁移工具不许碰它的内容。
        return []
    w.mkdir(os.path.dirname(p))
    if w.write(p, LEDGER_TEMPLATE):
        return ["建 `project-state/记忆/指导发现台账.md`（七列表头就位，发现按行追加）"]
    return []


def detect_batch_guard(root, ctx):
    """v5.2.0：**项目自带的** `scripts/task_estimate.py` 支不支持跑批闸门。

    **判据是脚本内容，不是版本号**（版本号会说谎：项目可能抄了旧脚本却改了头部版本）。
    三种实况，各有不同的正确处置：
    - 项目没内置 `scripts/task_estimate.py`（直接用框架侧脚本）→ **不适用**，不是缺陷；
    - 内置且含 `--guard` / `--contract-init` → **已到位**；
    - 内置但**是 v5.2.0 之前的脚本** → **需人工**：这种项目"跑到几点收工"会**静默失效**——
      主控调不出闸门，也就没有一个环节会在到点时说不。**不许自动覆盖**：项目可能对本脚本
      做过本地改动，整文件替换属结构变更，得走"随框架刷新"那条路（有 diff 可看）。
    """
    p = os.path.join(root, "scripts", "task_estimate.py")
    if not os.path.isfile(p):
        return NA, "本项目未内置 scripts/task_estimate.py（直接用框架侧脚本，无需迁移）"
    text = read(p)
    if "--guard" in text and "--contract-init" in text:
        return DONE, "scripts/task_estimate.py 已支持 --guard / --contract-init（跑批闸门可用）"
    return MANUAL, ("scripts/task_estimate.py 是 v5.2.0 之前的版本（无 --guard / --contract-init）："
                    "随框架刷新该脚本后再跑长时/无人值守跑批——**旧脚本没有闸门，"
                    "用户声明的「跑到几点 / 跑到多少 token 收工」没有任何一环会执行**")


# ---------------------------------------------------------------- 步骤：运行模式显式化（v5.3.0）

def _mode_str(cfg):
    return str(cfg.get("mode", "") or "").strip().lower()


QUICK_ALIASES = ("quick", "fast", "rapid", "vibe", u"快速")


def _mode_section(txt):
    """取《主控工作计划》里「运行模式」段的正文；**没有这一段就返回 None**。

    这条「只在段内找」是刻意的：整份文件里随手写一句「已告知用户」不算入口凭据，
    否则「静默切档」只要在别处补一句就蒙混过关——而那正是本次要拦的形态。
    找不到段落 = 没有留痕（**不是「找不到就当有」**）。
    """
    m = re.search(u"^#{1,6}\\s*.*运行模式.*$", txt, re.M)
    if not m:
        return None
    seg = txt[m.end():]
    nxt = re.search(u"^#{1,6}\\s", seg, re.M)
    return seg[:nxt.start()] if nxt else seg


def _plan_evidence(root):
    """在项目里找「已告知用户 / 用户明确指定」的留痕。

    **判据是纸面凭据，不是「感觉评估过了」**——`mode=full` 本身不是错（切档是一等公民），
    错的是**没有入口凭据**就躺在 full 上。只认两处白名单位置，避免把正文里随手提一句
    「完整模式」也算成凭据（那正是「静默切档」最常见的样子）。
    """
    hits = []
    plan = os.path.join(root, "project-state", u"主控工作计划.md")
    if os.path.isfile(plan):
        txt = read(plan)
        seg = _mode_section(txt)
        if seg is not None:
            pat = (u"(用户明确|用户指定|用户要求|用户点名|已告知用户|"
                   u"用户已知情|用户确认)")
            if re.search(pat, seg) or re.search(u"U[1-7]", seg):
                hits.append(rel(root, plan))
    cfgp = os.path.join(root, "project-state", "config.json")
    if os.path.isfile(cfgp):
        fc = read(cfgp)
        cpat = u'"_comment_mode\\s*:\\s*\"[^\"]*(已告知|用户明确|用户指定|入口凭据)'
        if re.search(cpat, fc):
            hits.append(rel(root, cfgp))
    return hits


def detect_mode(root, ctx):
    _p, cfg, err = _load_cfg(root)
    if err:
        return NA, "config.json 不可读（已在上一项报告）"
    m = _mode_str(cfg)
    if m in QUICK_ALIASES:
        if str(cfg.get("profile", "")).upper().startswith("C"):
            return TODO, ("`mode` 已是快速模式，但 `profile` 仍是 `C-standard-env`（标准三环境）"
                          "——**「快速模式却按三库要求」**：本步骤会同步把 profile 定到 A（单环境）")
        return DONE, "`mode` = %s（快速模式，v5.3.0 的默认档）" % m
    if m == "full":
        hits = _plan_evidence(root)
        if hits:
            return DONE, "`mode` = full，且入口凭据在 %s" % u"、".join(hits)
        return MANUAL, ("`mode` = full 但**找不到入口凭据**——v5.3.0 起完整模式只有两个正当入口："
                        "① 用户明确指定；② 项目经理评估 U1~U7 为大项目**并已告知用户**。"
                        "请在《主控工作计划》的「运行模式」段补一段留痕（当前档位 + 怎么进来的），"
                        "或确认本项目其实该按快速模式跑（把 `mode` 改为 quick）。"
                        "**本工具不自动改档**：说不上来凭据就改档 = 用工具替用户做决定")
    if not m:
        return TODO, ("未声明 `mode`——v5.3.0 起默认 `quick`（快速模式）：**没明确判为大项目的项目都走这里**。"
                      "本步骤会补上显式声明，免得「缺省」被读成「随便哪档都行」")
    if m == "explore":
        return DONE, ("`mode` = explore（探索模式，v5.25.0）：收工判 `scripts/wu_ledger.py --gate`，见 agent.md「探索模式」与注意事项 65")
    return MANUAL, "`mode` = %r，取值只认 \"quick\" / \"full\" / \"explore\"" % (cfg.get("mode"),)


def apply_mode(root, ctx, w):
    p, cfg, err = _load_cfg(root)
    if err:
        return []
    m = _mode_str(cfg)
    if m == "full":
        return []                      # **不自动降档**：凭据缺失交人工（见 detect_mode）
    out = []
    text = read(p)
    if not m:
        block = ('  "mode": "quick",\n  "_comment_mode": %s,\n'
                 % json.dumps(MODE_COMMENT, ensure_ascii=False))
        if re.search(r'^\s*"paths"\s*:', text, re.M):
            new, n = re.subn(r'^(\s*)"paths"\s*:', lambda mm: block + mm.group(1) + '"paths":',
                             text, count=1, flags=re.M)
        else:
            new, n = re.subn(r"\n?(\s*)\}\s*$",
                             lambda mm: "\n" + block.rstrip(",\n") + "\n}\n",
                             text, count=1)
        if n and w.write(p, new):
            out.append(u"config.json 补 `mode: \"quick\"`（v5.3.0 默认档；改 full 须有入口凭据）")
            text = new
    if str(cfg.get("profile", "")).upper().startswith("C"):
        if re.search(r'^\s*"profile"\s*:', text, re.M):
            new, n = re.subn(r'^(\s*)"profile"\s*:\s*"[^"]*"',
                             lambda mm: mm.group(1) + '"profile": "A-single-env"',
                             text, count=1, flags=re.M)
            if n and w.write(p, new):
                out.append(u"config.json 把 `profile` 定到 A（单环境）——快速模式不该按三库要求跑"
                           u"（原 C-standard-env 的含义已记在本次备份里）")
    return out


# ---------------------------------------------------------------- 步骤：方案视图区（v5.16.0）

SOLUTION_SUBDIRS = ("原始资料", "产品稿", "原型图")
SOLUTION_TEMPLATES = (
    "方案/README.md",
    "方案/原始资料/清单.md",
    "方案/产品稿/README.md",
    "方案/原型图/README.md",
    "方案/原型图/清单.md",
)


def _solution_root(root):
    """方案视图区实际相对路径（认 `paths.方案` 登记，缺省 `方案`）。"""
    _p, cfg, err = _load_cfg(root)
    rel_dir = "方案"
    if not err and isinstance(cfg.get("paths"), dict):
        raw = cfg["paths"].get("方案")
        if isinstance(raw, str) and raw.strip():
            rel_dir = raw.split("#")[0].split("——")[0].strip().strip("`").replace("\\", "/").strip("/")
    return rel_dir or "方案"


def _solution_base(root):
    parts = [p for p in _solution_root(root).split("/") if p]
    return os.path.join(root, *parts) if parts else os.path.join(root, "方案")


def _solution_missing(root):
    base = _solution_base(root)
    if not os.path.isdir(base):
        return base, ["（整目录）"]
    missing = []
    for sub in SOLUTION_SUBDIRS:
        if not os.path.isdir(os.path.join(base, sub)):
            missing.append(sub + "/")
    if not os.path.isfile(os.path.join(base, "README.md")):
        missing.append("README.md")
    if not os.path.isfile(os.path.join(base, "原始资料", "清单.md")):
        missing.append("原始资料/清单.md")
    return base, missing


def detect_solution_area(root, ctx):
    _p, cfg, err = _load_cfg(root)
    if err:
        return NA, "config.json 不可读（已在上一项报告）"
    base, missing = _solution_missing(root)
    if not missing:
        return DONE, "方案视图区齐备（%s：原始资料 / 产品稿 / 原型图 + 索引与台账）" % rel(root, base)
    mode = _mode_str(cfg)
    if mode in QUICK_ALIASES and not os.path.isdir(base):
        return NA, ("快速模式未建方案视图区（默认按需建，不强制）；判为大项目转 full 后再建，"
                    "收口体检 `python -X utf8 scripts/solution_audit.py` 会判「无法检查」")
    return TODO, ("方案视图区缺 %s——完整模式必须齐备（客户原始件要有地方归档、产品稿与原型图要能"
                  "随时看）；责任人=项目经理，收口跑 `solution_audit.py`") % "、".join(missing)


def apply_solution_area(root, ctx, w):
    base, _missing = _solution_missing(root)
    actions = []
    if not os.path.isdir(base):
        w.mkdir(base)
        actions.append("建 `%s/`（方案视图区根）" % rel(root, base))
    for sub in SOLUTION_SUBDIRS:
        d = os.path.join(base, sub)
        if not os.path.isdir(d):
            w.mkdir(d)
            actions.append("建 `%s/`" % rel(root, d))
    src_root = _framework_root()
    for relp in SOLUTION_TEMPLATES:
        tail = relp.split("/")[1:]
        dst = os.path.join(base, *tail)
        if os.path.isfile(dst):
            continue                      # **只建不改**：README/台账里是人写的内容
        src = os.path.join(src_root, *relp.split("/"))
        if not os.path.isfile(src):
            actions.append("**需人工**：框架源里找不到模板 `%s`（框架根=%s），请手工创建"
                           % (relp, src_root))
            continue
        w.mkdir(os.path.dirname(dst))
        if w.copy_from(src, dst):
            actions.append("建 `%s`（从框架模板复制）" % rel(root, dst))
    actions += _register_solution_path(root, w)
    return actions


def _register_solution_path(root, w):
    """`paths.方案` 没登记才补一行；已登记（或人工改过）一律不动。"""
    p, cfg, err = _load_cfg(root)
    if err:
        return []
    paths = cfg.get("paths")
    if isinstance(paths, dict) and "方案" in paths:
        return []
    text = read(p)
    if '"paths"' not in text:
        return ["**需人工**：config.json 里没有 `paths` 段，请手工登记 `\"方案\": \"方案\"`"]
    new, n = re.subn(r'^(\s*)"paths"\s*:\s*\{',
                     lambda mm: mm.group(0) + "\n" + mm.group(1) + '  "方案": "方案",',
                     text, count=1, flags=re.M)
    if n and w.write(p, new):
        return ["config.json 登记 `paths.方案`（根级白名单第④类「已登记的项目自定域」）"]
    return ["**需人工**：自动登记 `paths.方案` 失败，请手工补一行"]


STEPS = [
    ("config-schema", "config.json 声明 schema_version（结构版本）", "4.9.0",
     detect_config_schema, apply_config_schema),
    ("external-roots", "config.json 声明 external_roots（外部根）", "4.9.0",
     detect_external_roots, apply_external_roots),
    ("guidance", "config.json 声明 guidance（指导模式开关）", "5.0.0",
     detect_guidance, apply_guidance),
    ("guidance-ledger", "project-state/记忆/指导发现台账.md 就位（G11 数据源）", "5.1.0",
     detect_guidance_ledger, apply_guidance_ledger),
    ("layout-object", "paths.layout 由说明字符串转为八域对象", "4.9.0",
     detect_layout, apply_layout),
    ("split-checklists", "agent.md 内联的验收标准/注意事项拆到契约文件", "4.9.0",
     detect_split, apply_split),
    ("slim-agent-md", "agent.md description 版本编年史搬到框架编年，只留适用面", "4.9.0",
     detect_slim_desc, apply_slim_desc),
    ("card-dirs", "需求卡/测试卡目录与模板就位", "3.17.0",
     detect_card_dirs, apply_card_dirs),
    ("card-folders", "平铺卡转卡卷宗（一卡一目录）", "3.17.0",
     detect_card_folders, apply_card_folders),
    ("framework-source", "声明 framework_source（版本漂移比对源）", "4.9.0",
     detect_framework_source, None),
    ("batch-guard", "项目内置的 task_estimate.py 支持跑批闸门（--guard / --contract-init）", "5.2.0",
     detect_batch_guard, None),
    ("mode-explicit", "config.json 显式声明运行模式（默认 quick）并与 profile 自洽", "5.3.0",
     detect_mode, apply_mode),
    ("solution-area", "方案视图区（方案/ 原始资料·产品稿·原型图）就位，PM 台账可维护", "5.16.0",
     detect_solution_area, apply_solution_area),
]


# ---------------------------------------------------------------- 主流程

def parse_args(argv):
    root = os.getcwd()
    opts = {"apply": False, "json": False, "list": False, "only": None, "no_backup": False}
    for a in argv[1:]:
        if a == "--apply":
            opts["apply"] = True
        elif a == "--dry-run":
            opts["apply"] = False
        elif a == "--json":
            opts["json"] = True
        elif a == "--list":
            opts["list"] = True
        elif a == "--no-backup":
            opts["no_backup"] = True
        elif a == "--only":
            opts["only"] = "__next__"
        elif a in ("-h", "--help"):
            sys.stdout.write(__doc__)
            return None, 0
        elif a.startswith("-"):
            sys.stderr.write("未知参数：%s\n" % a)
            return None, 2
        elif opts["only"] == "__next__":
            opts["only"] = a
        else:
            root = a
    return os.path.abspath(root), opts


def run_lint(root):
    """迁移后自动复查——**没有复查入口的迁移工具，等于把"有没有真的修好"留给用户自己记**。"""
    lint = os.path.join(_framework_root(), "scripts", "framework_lint.py")
    if not os.path.isfile(lint):
        return None
    r = subprocess.run([sys.executable, lint, root, "--quiet"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (r.stdout or "") + (r.stderr or "")
    for l in reversed(out.splitlines()):
        if l.strip().startswith(("框架自查：", "项目体检：")):
            return l.strip()
    return None


def main(argv):
    _reconfigure()
    root, opts = parse_args(argv)
    if root is None:
        return opts

    if opts["list"]:
        for sid, title, since, _d, _a in STEPS:
            sys.stdout.write("%-18s v%-7s %s\n" % (sid, since, title))
        return 0

    if not os.path.isfile(os.path.join(root, "project-state", "config.json")):
        sys.stderr.write("不是框架/项目根（缺 project-state/config.json）：%s\n" % root)
        return 2

    _p, cfg, _err = _load_cfg(root)
    ctx = {"cfg": cfg}
    w = Writer(root, apply=opts["apply"], backup=not opts["no_backup"])

    results = []
    for sid, title, since, detect, apply_fn in STEPS:
        if opts["only"] and sid != opts["only"]:
            continue
        state, detail = detect(root, ctx)
        actions = []
        if state == TODO and apply_fn is not None:
            actions = apply_fn(root, ctx, w) or []
            if actions:
                state, detail = detect(root, ctx)   # 应用后重判：拿到的是"迁移后的真实状态"
        elif state == TODO and apply_fn is None:
            state = MANUAL
        results.append({"id": sid, "title": title, "since": since,
                        "state": state, "detail": detail, "actions": actions})

    pending = [r for r in results if r["state"] in (TODO, MANUAL)]
    if opts["json"]:
        sys.stdout.write(json.dumps({
            "root": root, "applied": opts["apply"], "pending": len(pending),
            "steps": results,
            "touched": w.touched, "unchanged": w.skipped_same,
            "backup": w.bakdir,
        }, ensure_ascii=False, indent=2) + "\n")
        return 0 if not pending else 1

    mode = "迁移执行" if opts["apply"] else "迁移计划（dry-run，未改动任何文件）"
    sys.stdout.write("# %s · %s\n\n" % (mode, root))
    for r in sorted(results, key=lambda x: (_ORDER[x["state"]], x["since"])):
        sys.stdout.write("[%s] %-18s v%-7s %s\n" % (STATE_LABEL[r["state"]], r["id"], r["since"],
                                                    r["title"]))
        sys.stdout.write("        %s\n" % r["detail"])
        for a in r["actions"]:
            sys.stdout.write("        · %s\n" % a)
    sys.stdout.write("\n")
    if w.skipped_same:
        sys.stdout.write("内容未变、未重复写入：%s\n" % w.skipped_same)
    if opts["apply"]:
        if w.bakdir:
            sys.stdout.write("备份：%s（核对无误后可删）\n" % rel(root, w.bakdir))
        if w.touched:
            sys.stdout.write("本次改动 %d 项：%s\n" % (len(w.touched), w.touched))
        summary = run_lint(root)
        if summary:
            sys.stdout.write("复查（framework_lint.py）：%s\n" % summary)
    else:
        pending_auto = [r for r in pending if r["state"] == TODO]
        if pending_auto:
            sys.stdout.write("执行：python scripts/migrate.py %s --apply\n" % root)
    sys.stdout.write("\n待迁移 %d 项 / 需人工 %d 项 / 已到位 %d 项\n"
                     % (len([r for r in results if r["state"] == TODO]),
                        len([r for r in results if r["state"] == MANUAL]),
                        len([r for r in results if r["state"] == DONE])))
    return 0 if not pending else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))

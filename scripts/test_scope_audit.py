#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_scope_audit.py — 单轮测试量（测试深度档位）只读检查器（v5.9.0）

为什么有这个脚本
----------------
v5.9.0 之前，框架里**只有「多久开一轮系统级测试」的节拍**（`config.json` 的
`test_card_cadence`：milestone / ship / every-N），**没有「这一轮测多少」的判据**。
结果是一个真实存在的默认行为：**测试工程师每轮都按全量跑**——
给客户交付的交付包当然要全量，但「修了个小功能给用户看一眼」也全量，
烧掉的是 token 与时间，而**没有任何字段留下「这轮为什么测这么多」**。
测试卡模板里虽然有 `not_doing`（不做清单），但它是**可选的自由文本**：
不填不报错、填了没人看，等于没有。

于是本版引入**三档测试深度**（与频率正交，频率管「多久一轮」，深度管「单轮范围」）：

    full      全量：交付包 / 上线前一轮 / 结构或契约变更 / 认证与敏感数据
    targeted  定向：只跑受影响维度 + 受影响链路的回归子集
    smoke     冒烟：只跑关键路径最小闭环（小修复给人看）

定档链（谁定、谁被管）：
    产品经理 → 声明本轮**交付用途**（deliverable / demo / internal）
    项目经理 → 据用途 + 改动影响面 + 预算**定档**，写进 tc.md 并报用户确认
    主控     → 建卡时固化档位（守「不自出题自判分」）
    测试工程师 → 按档执行；发现影响面超出档位**必须上报升级**，不得自行扩成全量

本脚本只核**声明得对不对**，不判**档位定得合不合理**（那是人的判断）。

四条纪律（每条都有先例）
  ① **「有问题」与「查不了」必须分开**：没有任何测试卡 → 退出码 **2**、`ok=null`，
     **不是「没问题」**（同 `case_audit.py` / `recall.py`）。
  ② **格式旧 ≠ 内容缺**：v5.9.0 之前的测试卡没有这两个键，整卡只报**一次**
     `DEPTH-FORMAT-STALE`，**不逐卡再报**「档位缺 / 用途缺」——
     否则升级当天所有老卡一起变红，真检测力反而被淹没（同 v5.7.0/v5.8.0 教训）。
  ③ **不稳定的量不进闸门**：只核「声明了没有 + 有没有自相矛盾」，
     **不判**「定向档是不是真的只测了受影响面」——机器判不了（同 v5.8.0）。
  ④ **保守选择允许**：`demo` 用途却定 `full` **不报错**——宁可多测不可少测，
     机器不替人做「这轮可以省」的判断。

用法
----
    python scripts/test_scope_audit.py <项目根>                    # 只读体检
    python scripts/test_scope_audit.py <项目根> --json             # 机器可读
    python scripts/test_scope_audit.py <项目根> --check            # 只印计数（门禁用）
    python scripts/test_scope_audit.py <项目根> --allow-unmigrated # 迁移期：格式旧降级

    退出码：0 干净 ｜ 1 有 ERROR ｜ 2 无法检查（**不是「没问题」**）
"""

import json
import os
import re
import sys

VERSION = "5.9.0"

# ---------------------------------------------------------------- 契约常量
DEPTHS = ["full", "targeted", "smoke"]
USES = ["deliverable", "demo", "internal"]
DEPTH_LABEL = {"full": "全量", "targeted": "定向", "smoke": "冒烟"}
USE_LABEL = {"deliverable": "交付包/对外", "demo": "演示/给用户看", "internal": "内部自用"}

# 连续冒烟超过这个张数（即 ≥3 张）报 WARN：防「永远冒烟」
SMOKE_STREAK_MAX = 2

# 「空」的写法（同 case_audit.py：不把这些当内容）
EMPTY = {"", "-", "--", "无", "n/a", "na", "none", "tbd", "todo", "待定", "[]", "[ ]"}

ID_RE = re.compile(r"^tc-(\d+)$")

SEV_ORDER = {"ERROR": 0, "WARN": 1, "INFO": 2}
SEV_LABEL = {"ERROR": "错误", "WARN": "告警", "INFO": "提示"}


def out(text=""):
    sys.stdout.write(text + "\n")


def read_text(path):
    """UTF-8 优先、GBK 兜底：真实工程里这两种都存在（先例：recall.py / case_audit.py）。"""
    with open(path, "rb") as fh:
        raw = fh.read()
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def uncomment(value):
    """剥掉 frontmatter 行内注释与包裹引号（模板里的 `# 说明` 是给人看的）。"""
    v = value.strip()
    if v[:1] in ("'", '"'):
        q = v[0]
        j = v.find(q, 1)
        return (v[1:j] if j > 0 else v[1:]).strip()
    m = re.search(r"\s#", v)
    if m:
        v = v[:m.start()]
    return v.strip()


def parse_frontmatter(text):
    """读 `---` 之间的键值 → ({键: (值, 行号)}, 闭合行号)。无 frontmatter 返回 (None, 0)。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, 0
    fm = {}
    for i in range(1, len(lines)):
        s = lines[i].strip()
        if s == "---":
            return fm, i + 1
        if not s or s.startswith("#") or ":" not in s:
            continue
        k, v = s.split(":", 1)
        fm[k.strip()] = (uncomment(v), i + 1)
    return None, 0          # frontmatter 没闭合：当「读不到」，交给 NO-FRONTMATTER


def resolve_base(root):
    """项目根 → project-state 目录（含 `测试卡/` 的那层）。"""
    if not root:
        root = os.getcwd()
    root = os.path.abspath(root)
    inner = os.path.join(root, "project-state")
    if os.path.isdir(inner):
        return inner
    if os.path.isdir(os.path.join(root, "测试卡")):
        return root
    return root if os.path.isdir(root) else None


def scan_cards(base):
    """扫 `测试卡/tc-*/tc.md` → [(tc 名, 序号 or None, 相对路径, frontmatter or None)]，按序号排序。"""
    tcdir = os.path.join(base, "测试卡")
    cards = []
    if not os.path.isdir(tcdir):
        return cards
    for name in sorted(os.listdir(tcdir)):
        path = os.path.join(tcdir, name, "tc.md")
        if not os.path.isfile(path):
            continue
        m = ID_RE.match(name)
        num = int(m.group(1)) if m else None
        fm, _end = parse_frontmatter(read_text(path))
        cards.append((name, num, path, fm))
    cards.sort(key=lambda c: (c[1] is None, c[1] if c[1] is not None else 0, c[0]))
    return cards


def rel(base, path):
    try:
        return os.path.relpath(path, base).replace("\\", "/")
    except ValueError:
        return path


class Report(object):
    def __init__(self):
        self.items = []
        self.counts = {"ERROR": 0, "WARN": 0, "INFO": 0}

    def add(self, code, sev, where, message):
        self.items.append({"code": code, "severity": sev, "where": where, "message": message})
        self.counts[sev] += 1


def check_cards(base, allow_unmigrated):
    """核心检查。返回 (report, cards, rows, dist, streak)。

    rows = 每卡一行的可读摘要（供人眼扫「这轮到底测多少」）。
    """
    rep = Report()
    cards = scan_cards(base)
    rows = []
    dist = {"full": 0, "targeted": 0, "smoke": 0, "?": 0}
    depths_seq = []

    for name, num, path, fm in cards:
        where = rel(base, path)
        if fm is None:
            rep.add("DEPTH-NO-FRONTMATTER", "ERROR", where,
                    "`tc.md` 没有可解析的 frontmatter（`---` 未闭合或缺失）——档位无处声明")
            dist["?"] += 1
            continue

        raw_depth = fm.get("test_depth", ("", 0))[0]
        raw_use = fm.get("delivery_use", ("", 0))[0]
        raw_note = fm.get("depth_note", ("", 0))[0]
        raw_notdoing = fm.get("not_doing", ("", 0))[0]
        cadence = fm.get("cadence_round", ("", 0))[0]
        has_depth = raw_depth != ""
        has_use = raw_use != ""
        depth = raw_depth.strip().lower()
        use = raw_use.strip().lower()
        note = raw_note.strip()
        notdoing = raw_notdoing.strip()

        # ---- ② 格式旧 ≠ 内容缺：两键都缺 = 这张卡还没迁移到 v5.9.0，整卡只说一次
        if not has_depth and not has_use:
            sev = "WARN" if allow_unmigrated else "WARN"
            rep.add("DEPTH-FORMAT-STALE", sev, where,
                    "这张卡没有 `test_depth` / `delivery_use`——**格式旧 ≠ 内容缺**："
                    "按 v5.9.0 之前的卡处理，本卡不再逐条报「档位缺 / 用途缺」；"
                    "补档方式：按 `测试卡/测试卡模板.md` 补两键（项目经理定档）")
            dist["?"] += 1
            rows.append((name, "?", "?", cadence, "未迁移"))
            continue

        if not has_depth:
            rep.add("DEPTH-MISSING", "ERROR", where,
                    "声明了 `delivery_use` 却没有 `test_depth`——**这一轮测多少没人定**，"
                    "默认就会退回全量（项目经理定档：full / targeted / smoke）")
            dist["?"] += 1
        elif depth not in DEPTHS:
            rep.add("DEPTH-INVALID", "ERROR", where,
                    "`test_depth: %s` 不在三档内（%s）" % (raw_depth, " / ".join(DEPTHS)))
            dist["?"] += 1
        else:
            dist[depth] += 1

        if not has_use:
            rep.add("DEPTH-DELIVERY-MISSING", "ERROR", where,
                    "声明了 `test_depth` 却没有 `delivery_use`——**本轮交付用途没声明**，"
                    "「交付包」与「给人看一眼」就分不开（产品经理声明：%s）" % " / ".join(USES))
        elif use not in USES:
            rep.add("DEPTH-DELIVERY-INVALID", "ERROR", where,
                    "`delivery_use: %s` 不在三类内（%s）" % (raw_use, " / ".join(USES)))

        # ---- 硬规则 H1：交付包/对外 → 必须全量
        if use == "deliverable" and has_depth and depth and depth != "full":
            rep.add("DEPTH-DELIVERY-MISMATCH", "ERROR", where,
                    "本轮是**交付包/对外**（`delivery_use: deliverable`）却定档 `%s`——"
                    "**交付包必须全量（full）**；要省测试量就换用途（demo / internal）再说" % depth)

        # ---- 硬规则 H2：上线前一轮（cadence_round 以 ship 开头）→ 必须全量
        if cadence.strip().lower().startswith("ship") and has_depth and depth and depth != "full":
            rep.add("DEPTH-SHIP-NOT-FULL", "ERROR", where,
                    "`cadence_round: %s` 是**上线前一轮**却定档 `%s`——上线前必须全量（full）"
                    % (cadence, depth))

        # ---- 硬规则 H3：非全量档必须写清「不测什么 + 依据」
        if has_depth and depth in DEPTHS and depth != "full":
            miss = []
            if note in EMPTY:
                miss.append("`depth_note`（这一轮**不测什么** + 依据）")
            if notdoing in EMPTY:
                miss.append("`not_doing`（明确不覆盖的维度）")
            if miss:
                rep.add("DEPTH-NOT-DOING-MISSING", "ERROR", where,
                        "定档 `%s`（省测试量）却没写 %s——**省下来的东西必须写在纸上**，"
                        "否则下一轮没人知道哪些面是没测过的" % (depth, " 与 ".join(miss)))

        if has_depth and depth in DEPTHS:
            depths_seq.append((name, depth))
            rows.append((name, depth, use or "?", cadence, note if note not in EMPTY else ""))

    # ---- 硬规则 H4：连续冒烟不许超 SMOKE_STREAK_MAX 张（防「永远冒烟」）
    streak = []
    run = []
    for name, depth in depths_seq:
        if depth == "smoke":
            run.append(name)
        else:
            if len(run) > SMOKE_STREAK_MAX:
                streak.append(list(run))
            run = []
    if len(run) > SMOKE_STREAK_MAX:
        streak.append(list(run))
    for grp in streak:
        rep.add("DEPTH-SMOKE-STREAK", "WARN", "%s 起 %d 张" % (rel(base, os.path.join(
            base, "测试卡", grp[0], "tc.md")), len(grp)),
                "连续 %d 张都定 `smoke`（%s）——**冒烟是例外不是常态**："
                "连着这么多轮只跑关键路径，未测面会积压；下一轮至少 `targeted`，"
                "或把积压写进《主控工作计划》" % (len(grp), " → ".join(grp)))

    return rep, cards, rows, dist, streak


def main(argv):
    root = None
    as_json = False
    check_only = False
    allow_unmigrated = False

    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--json":
            as_json = True
        elif a == "--check":
            check_only = True
        elif a == "--allow-unmigrated":
            allow_unmigrated = True
        elif a in ("-h", "--help"):
            out(__doc__.strip())
            return 0
        elif a.startswith("--"):
            out("未知参数：%s（见 --help）" % a)
            return 2
        else:
            root = a
        i += 1

    base = resolve_base(root)
    if base is None:
        out("项目根不存在或不可读：%s" % (root or os.getcwd()))
        return 2

    rep, cards, rows, dist, streak = check_cards(base, allow_unmigrated)

    if not cards:
        tcdir = os.path.join(base, "测试卡")
        if as_json:
            out(json.dumps({
                "version": VERSION, "base": base, "cards": 0, "ok": None, "exit": 2,
                "counts": rep.counts, "dist": dist, "items": rep.items,
                "rows": [],
            }, ensure_ascii=False, indent=2))
            return 2
        out("== test_scope_audit.py v%s ==" % VERSION)
        out("项目根：%s" % base)
        out("")
        out("[提示] DEPTH-NO-TESTCARD  %s  **无法检查，不是「没问题」**："
            % rel(base, tcdir))
        out("        一轮系统级测试卡都没有（`测试卡/tc-NNN/tc.md` 不存在）——"
            "引擎没开过，谈不上「测试量是否合理」。")
        out("        小项目/快速模式确实可能不启用测试卡：那要在《主控工作计划》写明豁免依据（验收标准 #24）。")
        out("")
        out("汇总：ERROR 0 / WARN 0 / INFO 0")
        out("退出码 2 —— **无法检查，不是「没问题」**")
        return 2

    exit_code = 1 if rep.counts["ERROR"] else 0

    if as_json:
        out(json.dumps({
            "version": VERSION,
            "base": base,
            "cards": len(cards),
            "ok": rep.counts["ERROR"] == 0,
            "exit": exit_code,
            "counts": rep.counts,
            "dist": dist,
            "smoke_streaks": streak,
            "rows": [{"tc": r[0], "depth": r[1], "use": r[2], "cadence": r[3], "note": r[4]}
                     for r in rows],
            "items": rep.items,
        }, ensure_ascii=False, indent=2))
        return exit_code

    if check_only:
        out("测试卡 %d 张，档位 full %d / targeted %d / smoke %d ｜ ERROR %d / WARN %d（退出码 %d）"
            % (len(cards), dist["full"], dist["targeted"], dist["smoke"],
               rep.counts["ERROR"], rep.counts["WARN"], exit_code))
        return exit_code

    out("== test_scope_audit.py v%s ==" % VERSION)
    out("项目根：%s" % base)
    out("测试卡：%d 张 ｜ 档位分布：full %d / targeted %d / smoke %d%s"
        % (len(cards), dist["full"], dist["targeted"], dist["smoke"],
           ("  ｜ 未迁移 %d" % dist["?"]) if dist["?"] else ""))
    out("")
    out("%-10s %-9s %-14s %-18s %s" % ("测试卡", "档位", "交付用途", "触发点", "不测范围摘要"))
    for name, depth, use, cadence, note in rows:
        out("%-10s %-9s %-14s %-18s %s"
            % (name, DEPTH_LABEL.get(depth, depth), USE_LABEL.get(use, use), cadence or "-",
               (note[:40] + "…") if len(note) > 40 else (note or "-")))
    out("")
    if not rep.items:
        out("无 ERROR / WARN：每张测试卡都**声明了档位与交付用途**，且没有自相矛盾。")
        out("（**只证明「声明得对」，不证明「档位定得合理」**——"
            "影响面判断与省不省得下来是人定的，本脚本不判。）")
    else:
        for it in sorted(rep.items, key=lambda x: (SEV_ORDER[x["severity"]], x["where"])):
            out("[%-5s] %-26s %s" % (SEV_LABEL[it["severity"]], it["code"], it["where"]))
            out("        %s" % it["message"])
    out("")
    out("汇总：ERROR %d / WARN %d / INFO %d" % (rep.counts["ERROR"], rep.counts["WARN"], rep.counts["INFO"]))
    if rep.counts["ERROR"]:
        out("退出码 1 —— 有 ERROR：打回项目经理重定档 / 产品经理补交付用途（前置检查第 19 项）。")
        return 1
    out("退出码 0 —— 干净。")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main(sys.argv))

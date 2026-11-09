#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""explore_loop_audit.py —— 探索→修复卡→评估→归档 闭环检查器（v5.27.0）

为什么要有它（2026-11-06 用户定案）：
  探索模式不再有夜班那套 WU 记账/三闸；**状态由修复卡承载**，闭环是：
  **探索（地图 S-NNN）→ 建修复卡（source 指回探索）→ 卡走状态机 → 修完 + 复验
  → 产品经理评估三分支（沉需求卡 / 沉测试卡 / 只归档）→ 修复卡归档留痕**。
  本脚本只核这条链上的**登记自洽与指针真实**。

用法：
    python -X utf8 scripts/explore_loop_audit.py <项目根>            # 体检
    python -X utf8 scripts/explore_loop_audit.py <项目根> --json     # 机器可读
    python -X utf8 scripts/explore_loop_audit.py <项目根> --check    # 只印计数

退出码：
    0 干净
    1 有 ERROR
    2 无法检查（**没有探索域、也没有任何探索修复卡**——本项目没做过探索闭环，2 ≠ 没问题）

核什么：
  ① 探索修复卡的识别：`source` 指向探索（`S-NNN` / `探索#S-NNN` / `探索:<路径>`）
  ② 必填：`anchor` 与 `entry` 不得同时为空（v5.7.0 纪律）
  ③ 状态机：`status ∈ {todo,in_progress,done,reviewing,blocked}`（v3.16.0 单一语义，不新造状态）
  ④ `status=done` 的探索修复卡必须已完成评估：`disposition ∈ {card,tc,archive}`
     - `card` → `disposition_ref` 必须是真实存在的 `card-NNN` 目录
     - `tc`   → `disposition_ref` 必须是真实存在的 `tc-NNN` 目录
     - `archive` → `disposition_note` 非空（≥6 字，写清为什么不需要沉淀）
  ⑤ `source` 里的 `S-NNN` 必须在 `契约/探索域.md` 的 `machine:scope` 里真实存在（悬空 = ERROR）

核不出（诚实边界）：
  - 「这张修复卡是不是真修好了」「评估结论对不对」——那归三路验收与产品经理判断
  - 「归档后痕迹够不够」——只核字段在位，不核内容质量
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

SCOPE_REL = Path("project-state") / "契约" / "探索域.md"
CARD_ROOT = Path("project-state") / "需求卡"
STATUSES = {"todo", "in_progress", "done", "reviewing", "blocked"}
DISPOSITIONS = {"card", "tc", "archive"}
SCOPE_OPEN, SCOPE_CLOSE = "<!-- machine:scope -->", "<!-- /machine:scope -->"
COVERAGE = ("本结论只在「探索→修复卡→评估→归档」的登记自洽与指针真实范围内成立，"
            "不覆盖：修复是否真完成、评估结论是否正确、归档痕迹是否足够")


def _err(code, where, msg):
    return {"level": "ERROR", "code": code, "where": where, "msg": msg}


def _warn(code, where, msg):
    return {"level": "WARN", "code": code, "where": where, "msg": msg}


def _frontmatter(text: str) -> dict:
    """极简 YAML 前置块解析：只取 key: value，去掉行内 ` # 注释`。"""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    out = {}
    for ln in text[3:end].splitlines():
        m = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", ln)
        if not m:
            continue
        k, v = m.group(1), m.group(2)
        v = re.sub(r"\s+#.*$", "", v).strip().strip('"').strip("'")
        out[k] = v
    return out


def _scope_probe_ids(root: Path):
    p = root / SCOPE_REL
    if not p.exists():
        return None
    text = p.read_text(encoding="utf-8-sig", errors="replace")
    if SCOPE_OPEN not in text or SCOPE_CLOSE not in text:
        return None
    seg = text.split(SCOPE_OPEN, 1)[1].split(SCOPE_CLOSE, 1)[0]
    ids = set()
    for ln in seg.splitlines():
        m = re.match(r"^\|\s*(S-\d{3})\s*\|", ln.strip())
        if m:
            ids.add(m.group(1))
    return ids


def audit(root: Path):
    issues, stats = [], {"cards": 0, "explore_cards": 0, "done": 0, "disposed": 0,
                         "card": 0, "tc": 0, "archive": 0, "pending_eval": 0}
    scope_ids = _scope_probe_ids(root)
    card_root = root / CARD_ROOT
    cards = sorted(card_root.glob("card-*/card.md")) if card_root.is_dir() else []

    for cf in cards:
        fm = _frontmatter(cf.read_text(encoding="utf-8-sig", errors="replace"))
        where = str(cf.relative_to(root))
        stats["cards"] += 1
        source = (fm.get("source") or "").strip()
        if not source or source in ("-", '""'):
            continue  # 普通需求卡
        # 只处理"探索修复卡"
        is_explore = bool(re.search(r"探索", source)) or bool(re.search(r"(^|[^\w])S-\d{3}", source))
        if not is_explore:
            continue
        stats["explore_cards"] += 1
        status = (fm.get("status") or "").strip()
        # ② anchor/entry
        a, e = (fm.get("anchor") or "").strip(), (fm.get("entry") or "").strip()
        if (not a or a in ("-", '""')) and (not e or e in ("-", '""')):
            issues.append(_err("EL-ANCHOR-MISSING", where, "修复卡 anchor 与 entry 不得同时为空（v5.7.0）"))
        # ③ 状态机
        if status not in STATUSES:
            issues.append(_err("EL-STATUS", where,
                               f"status={status!r} 不在 {sorted(STATUSES)}（沿用现有五状态，不新造）"))
        # ⑤ source 指针
        for pid in re.findall(r"S-\d{3}", source):
            if scope_ids is None:
                issues.append(_warn("EL-SCOPE-MISSING", where,
                                    "source 指到探索探针，但项目里没有可读的 `契约/探索域.md` 机读块"))
                break
            if pid not in scope_ids:
                issues.append(_err("EL-PROBE-DANGLING", where, f"source 指向的探针 {pid} 不在探索域里"))
        # ① 完成必须评估
        if status == "done":
            stats["done"] += 1
            disp = (fm.get("disposition") or "").strip()
            ref = (fm.get("disposition_ref") or "").strip()
            note = (fm.get("disposition_note") or "").strip()
            if not disp or disp in ("-", '""'):
                stats["pending_eval"] += 1
                issues.append(_err("EL-DISPOSITION-MISSING", where,
                                   "探索修复卡已 done 但没做评估——必须填 `disposition: card|tc|archive`（产品经理评估）"))
                continue
            if disp not in DISPOSITIONS:
                issues.append(_err("EL-DISPOSITION-VALUE", where,
                                   f"disposition={disp!r} 只能是 card（沉需求卡）/ tc（沉测试卡）/ archive（只归档）"))
                continue
            stats["disposed"] += 1
            stats[disp] += 1
            if disp in ("card", "tc"):
                expect = r"card-\d{3}" if disp == "card" else r"tc-\d{3}"
                if not re.fullmatch(expect, ref or ""):
                    issues.append(_err("EL-DISPOSITION-REF", where,
                                       f"disposition={disp} 时 disposition_ref 必须是 {expect}，实填 {ref!r}"))
                else:
                    target = (card_root / ref) if disp == "card" else (root / "project-state" / "测试卡" / ref)
                    if not target.is_dir():
                        issues.append(_err("EL-DISPOSITION-SINK", where,
                                           f"沉入目标不存在：{ref}（评估要落到真实卡片/测试卡上）"))
            if disp == "archive":
                if len(note) < 6:
                    issues.append(_err("EL-DISPOSITION-NOTE", where,
                                       "disposition=archive 时 disposition_note 必须写清为什么不需沉淀（≥6 字）"))
    if stats["explore_cards"] == 0 and (scope_ids is None or len(scope_ids) == 0):
        return "no-explore", issues, stats
    return "ok", issues, stats


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    as_json = "--json" in argv
    quiet = "--check" in argv
    root = Path(args[0]).resolve() if args else Path.cwd()
    state, issues, stats = audit(root)
    errors = [i for i in issues if i["level"] == "ERROR"]
    warnings = [i for i in issues if i["level"] == "WARN"]
    payload = {"state": state, **stats, "errors": errors, "warnings": warnings, "coverage": COVERAGE}
    if state == "no-explore":
        msg = "无法检查：项目里既没有 `契约/探索域.md`，也没有任何探索修复卡（本项目没做过探索闭环，2 ≠ 没问题）"
        if as_json:
            payload["reason"] = msg
            print(json.dumps(payload, ensure_ascii=False, indent=1))
        else:
            print(msg)
        return 2
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
    elif quiet:
        print(f"ERROR {len(errors)} / WARN {len(warnings)} / 探索修复卡 {stats['explore_cards']}")
    else:
        for it in issues:
            print(f"[{it['level']}] {it['code']} {it['where']} — {it['msg']}")
        print(f"卡片 {stats['cards']}｜探索修复卡 {stats['explore_cards']}｜done {stats['done']}"
              f"｜已评估 {stats['disposed']}（沉需求卡 {stats['card']} / 沉测试卡 {stats['tc']} / 只归档 {stats['archive']}）"
              f"｜待评估 {stats['pending_eval']}｜ERROR {len(errors)}｜WARN {len(warnings)}")
        print(f"覆盖面：{COVERAGE}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scope_audit.py —— 探索域（探索模式"探什么"）的只读检查器（v5.26.0）

为什么要有它（2026-11-06）：
  探索模式已有「backlog（队列）→ WU 台账（记账）→ 三闸（停机）」，**缺最上游的地图**。
  没有地图，backlog 就退化成"项目经理拍脑袋列活"。本脚本守两件事：
  ① **探索域本身自洽**：三族（负面/数据/体验）× 类型学（F1~F11 / D1~D6 / E1~E8），
     每格必须挂**契约判据来源**；体验族必须留**视觉证据**（截图/CLS/录屏）。
  ② **backlog 不许凭空派活**：每条 WU 的判据指针必须能解析到「探索域格号」或「真实存在的契约文件」。

用法：
    python -X utf8 scripts/scope_audit.py <项目根>            # 体检 + 覆盖率摘要
    python -X utf8 scripts/scope_audit.py <项目根> --json     # 机器可读
    python -X utf8 scripts/scope_audit.py <项目根> --check    # 门禁：只印计数

退出码：0 干净 / 1 有 ERROR / 2 无法检查（**2 ≠ 没问题**：没探索域、空表、列头不符都走这里）

诚实边界：
  - 只核**登记自洽与指针真实**：核不出「这条探针该不该探」「判据条款是不是真管这件事」
  - 「判据来源路径存在」不等于「条款内容对得上」——内容对得上归测试工程师与主控抽读
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

SCOPE_REL = Path("project-state") / "契约" / "探索域.md"
BACKLOG_REL = Path("project-state") / "契约" / "探索backlog.md"
SCOPE_COLS = ["id", "family", "kind", "target", "identity", "channel",
              "face", "criterion", "risk", "evidence", "status"]
FAMILIES = {"负面", "数据", "体验"}
KIND_RANGES = {"负面": ("F", 1, 11), "数据": ("D", 1, 6), "体验": ("E", 1, 8)}
CHANNELS = {"界面", "接口", "文件", "消息", "定时"}
FACES = {"列表", "详情", "直达链接", "导出", "搜索", "通知", "统计", "无"}
RISKS = {"P0", "P1", "P2"}
STATUSES = {"待探", "已探", "空洞"}
VISUAL = ("截图", "CLS", "录屏")
SCOPE_OPEN, SCOPE_CLOSE = "<!-- machine:scope -->", "<!-- /machine:scope -->"
BACK_OPEN, BACK_CLOSE = "<!-- machine:backlog -->", "<!-- /machine:backlog -->"
COVERAGE = ("本结论只在「探索域登记自洽 + 指针真实」范围内成立，"
            "不覆盖：探针该不该探、判据条款是否真的管这件事、证据是否真采到")


def _err(code, where, msg):
    return {"level": "ERROR", "code": code, "where": where, "msg": msg}


def _warn(code, where, msg):
    return {"level": "WARN", "code": code, "where": where, "msg": msg}


def _block(text: str, open_m: str, close_m: str):
    if open_m not in text or close_m not in text:
        return None, None
    seg = text.split(open_m, 1)[1].split(close_m, 1)[0]
    lines = [ln.strip() for ln in seg.strip().splitlines() if ln.strip()]
    if not lines:
        return [], []
    header = [c.strip() for c in lines[0].strip("|").split("|")]
    rows = []
    for ln in lines[1:]:
        if set(ln) <= {"|", "-", " ", ":"}:
            continue
        rows.append([c.strip() for c in ln.strip("|").split("|")])
    return header, rows


def _resolve(root: Path, rel: str):
    """判据路径解析：先按项目根，再按 project-state（框架索引习惯写「契约/xxx.md」）。"""
    rel = rel.split("#", 1)[0].strip()
    if not rel:
        return None, False
    for base in (root, root / "project-state"):
        cand = (base / rel)
        if cand.exists():
            return cand, True
    return None, False


def _criterion_path_ok(root: Path, crit: str) -> bool:
    return _resolve(root, crit)[1]


def audit(root: Path):
    issues: list[dict] = []
    stats = {"rows": 0, "负面": 0, "数据": 0, "体验": 0, "待探": 0, "已探": 0, "空洞": 0}

    sp = root / SCOPE_REL
    if not sp.exists():
        return "no-scope", issues, stats, {}
    header, rows = _block(sp.read_text(encoding="utf-8-sig", errors="replace"),
                          SCOPE_OPEN, SCOPE_CLOSE)
    if header is None:
        return "no-block", [_err("SCP-BLOCK-MISSING", str(SCOPE_REL),
                                 "缺 machine:scope 机读块（探索域必须在机读块里，否则 backlog 指针无从校验）")], stats, {}
    if header != SCOPE_COLS:
        return "bad-cols", [_err("SCP-COLUMNS", str(SCOPE_REL),
                                 f"列头不符：期望 {' | '.join(SCOPE_COLS)}，实际 {' | '.join(header)}")], stats, {}
    if not rows:
        return "empty", [_err("SCP-EMPTY", str(SCOPE_REL),
                              "探索域是空的（一行探针都没有 = 这张地图没画，backlog 只能凭空派活）")], stats, {}

    scope_ids: dict[str, dict] = {}
    last_no = 0
    for i, row in enumerate(rows, start=2):
        where = f"{SCOPE_REL}:{i}"
        if len(row) != len(SCOPE_COLS):
            issues.append(_err("SCP-ROW-WIDTH", where, f"列数 {len(row)} ≠ {len(SCOPE_COLS)}（字段内不许出现 |）"))
            continue
        d = dict(zip(SCOPE_COLS, row))
        sid = d["id"]
        m = re.fullmatch(r"S-(\d{3})", sid or "")
        if not m:
            issues.append(_err("SCP-ID", where, f"id={sid!r} 不是 S-NNN 形式"))
        else:
            no = int(m.group(1))
            if no <= last_no:
                issues.append(_err("SCP-ID-ORDER", where, f"id={sid} 未升序（上一条 S-{last_no:03d}）"))
            last_no = max(last_no, no)
        fam, kind = d["family"], d["kind"]
        if fam not in FAMILIES:
            issues.append(_err("SCP-FAMILY", where, f"family={fam!r} 不在 {sorted(FAMILIES)}"))
        else:
            prefix, lo, hi = KIND_RANGES[fam]
            km = re.fullmatch(prefix + r"(\d{1,2})", kind or "")
            if not km:
                issues.append(_err("SCP-KIND-FAMILY", where,
                                   f"kind={kind!r} 与族 {fam} 不匹配（该族只能是 {prefix}{lo}~{prefix}{hi}）"))
            else:
                n = int(km.group(1))
                if not (lo <= n <= hi):
                    issues.append(_err("SCP-KIND-RANGE", where, f"kind={kind} 超出该族范围 {prefix}{lo}~{prefix}{hi}"))
            stats[fam] += 1
        if not d["target"].strip() or d["target"] == "-":
            issues.append(_err("SCP-TARGET", where, "target 为空：这条探针没有目标面"))
        if not d["identity"].strip() or d["identity"] == "-":
            issues.append(_err("SCP-IDENTITY", where, "identity 为空：不知道该以谁的身份打"))
        if d["channel"] not in CHANNELS:
            issues.append(_err("SCP-CHANNEL", where, f"channel={d['channel']!r} 不在 {sorted(CHANNELS)}"))
        if d["face"] not in FACES:
            issues.append(_err("SCP-FACE", where, f"face={d['face']!r} 不在 {sorted(FACES)}"))
        if d["risk"] not in RISKS:
            issues.append(_err("SCP-RISK", where, f"risk={d['risk']!r} 只能是 P0/P1/P2"))
        if d["status"] not in STATUSES:
            issues.append(_err("SCP-STATUS", where, f"status={d['status']!r} 不在 {sorted(STATUSES)}"))
        crit = d["criterion"].strip()
        if not crit or crit == "-":
            if d["status"] != "空洞":
                issues.append(_warn("SCP-NO-CRITERION", where,
                                    "判据来源为空却不是「空洞」——指不到契约的探针不能进 backlog 的发现活"))
        elif not _criterion_path_ok(root, crit):
            issues.append(_err("SCP-CRITERION-DANGLING", where,
                               f"判据来源路径不存在：{crit.split('#')[0]}"))
        ev = d["evidence"]
        if not ev.strip() or ev == "-":
            issues.append(_err("SCP-EVIDENCE", where, "evidence 为空：探到什么算数？"))
        elif fam == "体验" and not any(v in ev for v in VISUAL):
            issues.append(_err("SCP-EVIDENCE-VISUAL", where,
                               f"体验族的证据必须含 {VISUAL} 之一（现填 {ev!r}）——体验问题没有视觉证据就不可复核"))
        if d["status"] in STATUSES:
            stats["status_" + d["status"]] = stats.get("status_" + d["status"], 0) + 1
        stats["rows"] += 1
        if m:
            scope_ids[sid] = d

    # ---- backlog 三级指针 ----
    bp = root / BACKLOG_REL
    stats["backlog_rows"] = 0
    if bp.exists():
        btext = bp.read_text(encoding="utf-8-sig", errors="replace")
        bheader, brows = _block(btext, BACK_OPEN, BACK_CLOSE)
        if bheader is None:
            issues.append(_warn("SCP-BACKLOG-NO-BLOCK", str(BACKLOG_REL),
                                "backlog 没有 machine:backlog 机读块，指针无法校验"))
        elif brows:
            for j, brow in enumerate(brows, start=2):
                if len(brow) < 4:
                    issues.append(_warn("SCP-BACKLOG-WIDTH", f"{BACKLOG_REL}:{j}", "backlog 行字段不足，跳过指针校验"))
                    continue
                wid, ptr = brow[0], brow[3]
                stats["backlog_rows"] += 1
                where = f"{BACKLOG_REL}:{j}"
                if not ptr or ptr in ("-", "无"):
                    issues.append(_err("SCP-BACKLOG-NO-POINTER", where, f"{wid} 没写判据指针 = 凭空派活"))
                    continue
                if "探索域.md#" in ptr:
                    ref = ptr.split("#", 1)[1].strip()
                    if not re.fullmatch(r"S-\d{3}", ref):
                        issues.append(_err("SCP-BACKLOG-BAD-REF", where, f"{wid} 的格号 {ref!r} 不是 S-NNN"))
                    elif ref not in scope_ids:
                        issues.append(_err("SCP-BACKLOG-DANGLING", where, f"{wid} 指向的 {ref} 不在探索域里"))
                else:
                    path = ptr.split("#", 1)[0].strip()
                    if not path.startswith("契约/") and not path.startswith("project-state/契约/"):
                        issues.append(_err("SCP-BACKLOG-POINTER-FORM", where,
                                           f"{wid} 的指针 {ptr!r} 既不是 探索域.md#S-NNN 也不在契约目录下"))
                    elif not _resolve(root, path)[1]:
                        issues.append(_err("SCP-BACKLOG-CRITERION-DANGLING", where,
                                           f"{wid} 的契约文件不存在：{path}"))
    return "ok", issues, stats, {"ids": list(scope_ids)}


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    as_json = "--json" in argv
    quiet = "--check" in argv
    root = Path(args[0]).resolve() if args else Path.cwd()
    state, issues, stats, extra = audit(root)
    errors = [i for i in issues if i["level"] == "ERROR"]
    warnings = [i for i in issues if i["level"] == "WARN"]
    payload = {"state": state, "rows": stats.get("rows", 0),
               "families": {k: stats.get(k, 0) for k in FAMILIES},
               "status": {s: stats.get("status_" + s, 0) for s in STATUSES},
               "backlog_rows": stats.get("backlog_rows", 0),
               "errors": errors, "warnings": warnings, "coverage": COVERAGE}
    if state != "ok":
        msg = {
            "no-scope": f"无法检查：没有 {SCOPE_REL}（项目没画探索域）",
            "no-block": f"无法检查：{SCOPE_REL} 缺 machine:scope 机读块",
            "bad-cols": "无法检查：探索域机读块列头不符",
            "empty": "无法检查：探索域一行探针都没有（空地图 = backlog 只能凭空派活）",
        }[state]
        if as_json:
            payload["reason"] = msg
            print(json.dumps(payload, ensure_ascii=False, indent=1))
        else:
            print(msg)
            for it in issues:
                print(f"[{it['level']}] {it['code']} {it['where']} — {it['msg']}")
        return 2
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
    elif quiet:
        print(f"ERROR {len(errors)} / WARN {len(warnings)} / 探针 {stats['rows']} / backlog {stats['backlog_rows']}")
    else:
        for it in issues:
            print(f"[{it['level']}] {it['code']} {it['where']} — {it['msg']}")
        print(f"探针 {stats['rows']}（负面 {stats.get('负面',0)} / 数据 {stats.get('数据',0)} / 体验 {stats.get('体验',0)}）"
              f"｜状态 待探 {stats.get('status_待探',0)} / 已探 {stats.get('status_已探',0)} / 空洞 {stats.get('status_空洞',0)}"
              f"｜backlog {stats['backlog_rows']} 行｜ERROR {len(errors)}｜WARN {len(warnings)}")
        print(f"覆盖面：{COVERAGE}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

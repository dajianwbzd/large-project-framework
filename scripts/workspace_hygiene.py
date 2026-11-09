#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v4.4.0 工作区整洁收口（workspace_hygiene）：把「工作区要干净」从原则变成动作。

与 path_audit.py 的分工（互补不重复）：
- path_audit.py：根级**合规**盘点（白名单/游离/归位）——只报告，**归位属结构变更**由主控裁决后执行。
- workspace_hygiene.py（本脚本）：**清理**盘点与执行——垃圾 / 临时 / 中间产物 / 交付件的收口。

设计原则：
- **默认只报告（dry-run）**：不加 --apply 绝不删任何东西；删除动作必须显式开关。
- **分级授权**：能自动删的只有「明确无用」的（L1 垃圾 + L2 超龄 tmp + L2.5 散落中间件 + 交付中间物）；
  产物旧版本 / 交付旧版本走**归档**（可逆）；`.log` 与**已发给客户的副本只报告不删**（前者可能是唯一故障线索，
  后者是不可逆对外动作）。
- **硬保护清单**：证据（卡卷宗 `证据/`）、`交付/发出记录.md`、`.git/`，以及 `project-state/` 下的
  一切正式产出（**其根级散落的一次性中间件除外**，那些仍按 L2.5 清理）—— 一律不进入清理候选
  （误删证据 = 证据链断裂，比脏乱更贵；误删正式产物的备份/写入中临时件 = 数据丢失）。
- **在飞资产保护**：tmp/ 与产物局里名字含 backup/回滚/部署/分片 等提示词的条目**只报告不删**
  （真实教训：待用户扫码期间的部署分片躺在 tmp 里，误删即中断在飞任务）。

用法：
    python workspace_hygiene.py <项目根>                      # 干跑报告（默认）
    python workspace_hygiene.py <项目根> --json               # 机器可读
    python workspace_hygiene.py <项目根> --write              # 同时写 project-state/整洁盘点-<日期>.md
    python workspace_hygiene.py <项目根> --apply              # 执行 L1+L2+L2.5+交付中间物 删除
    python workspace_hygiene.py <项目根> --apply --archive    # 另把 L3 产物旧版本移 产物/归档/
    python workspace_hygiene.py <项目根> --apply --archive-delivery   # 另把 L4 交付旧版本移 交付/归档/
    python workspace_hygiene.py <项目根> --apply --confirm-delivered  # 登记「已发出副本」清单（**本脚本永不代删**）
    python workspace_hygiene.py <项目根> --tmp-age-days 7             # tmp 清理阈值天数（默认 7.0，保守）
    python workspace_hygiene.py <项目根> --artifact-age-days 14       # 产物旧版本阈值天数（默认 14.0）

退出码：0 正常 / 2 不是框架工作区。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import sys
from pathlib import Path

# ── 判据常量 ────────────────────────────────────────────────────────────────
JUNK_DIR_NAMES = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".DS_Store"}
JUNK_FILE_NAMES = {".DS_Store", "Thumbs.db"}
JUNK_SUFFIX = (".pyc", ".pyo")
JUNK_PREFIX = ("~$",)

# 名字自带「临时身份」的散落物：可自动删（L2.5）
TEMP_NAME_HINTS = ("%{", "content-length", "_curl", ".part", ".partial")
TEMP_SUFFIX = (".out", ".tmp", ".bak")
# 日志**只报告不自动删**：失败日志常是唯一线索，删前须人工确认（对齐 path_audit「只提示」纪律）
LOG_SUFFIX = (".log",)
TEMP_DATED = re.compile(r"^_\d{8}[_-]")          # _20260906_login_probe.ps1（tmp 命名纪律）
SCAN_SUFFIX = (".py", ".ps1", ".sh", ".bat", ".js", ".cmd")

# 交付/产物中间的「在飞」提示词：只报告不删
INFLIGHT_HINTS = ("backup", "bak", "回滚", "rollback", "部署", "deploy", "分片", "part", "hotfix", "热修")

# 归档包后缀（产物/交付旧版本判定用）
PACKAGE_SUFFIX = (".zip", ".7z", ".rar", ".tar", ".gz", ".tgz", ".apk", ".exe", ".msi", ".dmg", ".pkg", ".ipa")

VERSION_TOKEN = re.compile(r"[-_.]?v?\d+(?:[.\-_]\d+)*", re.IGNORECASE)

# 龄读数哨兵：`st_mtime` 读不到时给这个值（**负数**，与任何真实龄区间不相交）。
# F-34：它曾被与「未来 mtime」塌缩成同一个 `-1`，导致渲染层只能显示 `?`。
AGE_UNREADABLE = -1


def family_key(name: str) -> str:
    """把带版本/日期的名字归一成「同族」键：app-v1.2.zip / app-20260901.zip → app。"""
    stem = Path(name).stem.lower()
    key = VERSION_TOKEN.sub("-", stem)
    key = re.sub(r"-{2,}", "-", key).strip("-_ ")
    return key or stem


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024.0
    return f"{n:.1f}GB"


def size_of(p: Path) -> int:
    try:
        if p.is_file():
            return p.stat().st_size
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    except OSError:
        return 0


def mtime_days(p: Path, now: _dt.datetime) -> int:
    """距今天数（向下取整）。**取不到 st_mtime 返回 -1**（哨兵：调用方须当「在飞」处理）。

    v4.4.x 修 F-34：mtime 落在**未来**时，`now - mtime` 为负——原实现把这个负值**原样返回**，
    于是 `0 <= age < tmp_age` 对它是 False、落进 `tmp_delete`。而「未来 mtime」在真实工作区
    **不是异常而是常态**：NAS/网络盘与本地时钟不一致、跨机拷贝保留源 mtime、用户手工调过时钟——
    这些情况下**刚刚创建的在飞文件**会被判成「超龄」而进删除桶（`--apply` 即真删）。
    此处把未来 mtime **夹到 0**（＝「就在此刻」），与 `-1` 哨兵分开：
    前者是**已知的现在**，后者是**读不到**。
    """
    try:
        age = (now - _dt.datetime.fromtimestamp(p.stat().st_mtime)).days
    except OSError:
        return AGE_UNREADABLE
    return 0 if age < 0 else age


def age_cause(age: int) -> str:
    """把龄读数翻成**可区分的原因码**（F-34 台账要求「不得都塌缩成 `-1`」）。

    `-1` 哨兵同时承载过两种截然不同的语义（stat 失败 / 未来 mtime），
    渲染层遂只能显示 `?`——**在一张「可清理」表里，无法解释的读数就是缺陷**。
    """
    if age == AGE_UNREADABLE:
        return "stat-failed"
    if age == 0:
        return "now-or-future"
    return "aged"


def is_inflight(name: str) -> bool:
    low = name.lower()
    return any(h in low for h in INFLIGHT_HINTS)


def is_junk(p: Path) -> bool:
    if p.is_dir():
        return p.name in JUNK_DIR_NAMES
    return (
        p.name in JUNK_FILE_NAMES
        or p.name.endswith(JUNK_SUFFIX)
        or p.name.startswith(JUNK_PREFIX)
    )


def is_temp_litter(p: Path) -> bool:
    """根级/state 根散落物里「明确是一次性中间件」的那部分（可自动删）。"""
    low = p.name.lower()
    if p.is_dir():
        return False
    if TEMP_DATED.match(p.name):
        return True
    if low.endswith(TEMP_SUFFIX):
        return True
    return any(h in low for h in TEMP_NAME_HINTS)


def is_log_litter(p: Path) -> bool:
    return p.is_file() and p.name.lower().endswith(LOG_SUFFIX)


# ── 硬保护（**必须落成代码守卫，不能只写在文档里**）─────────────────────────
# 与 agent.md「统一路径管理」§2.2.1/§2.3、项目经理 skill §11、完成定义 §9.9 的
# 硬保护清单同源：误删证据 = 证据链断裂，比工作区脏乱更贵。
PROTECTED_DIR_NAMES = {"证据", ".git"}
# project-state 是正式产出区：整区硬保护，唯根级散落的一次性中间件按 §9.9 可清（见 is_state_root_litter）
PROTECTED_STATE_DIR = "project-state"


def domain_dirs(root: Path) -> tuple:
    """客户交互区/内部产物区目录名（v4.5.0 paths 注册表口径）：读 project-state/config.json
    paths 的「交付」「产物」映射——框架文档默认 `交付/`、`产物/`，项目可注册自定名
    （如本项目 交付产物/、部署产物/），注册表是唯一事实源；读不到/未注册回退默认名。"""
    art, dl = "产物", "交付"
    try:
        paths = (json.loads((root / "project-state" / "config.json").read_text(encoding="utf-8")) or {}).get("paths", {})
        art = str(paths.get("产物") or art).replace("\\", "/").strip("/\\") or art
        dl = str(paths.get("交付") or dl).replace("\\", "/").strip("/\\") or dl
    except Exception:
        pass  # config 缺失/损坏 = 回退默认名（框架默认行为不变），不在盘点报告里静默吞错
    return art, dl


def protected_rel_paths(root: Path) -> tuple:
    """《发出记录.md》是对外唯一凭证：域名按 config 注册表解析（默认 交付/）。"""
    return (f"{domain_dirs(root)[1]}/发出记录.md",)


def rel_parts(root: Path, p: Path) -> tuple:
    try:
        return p.relative_to(root).parts
    except ValueError:
        return p.parts


def is_state_root_litter(root: Path, p: Path) -> bool:
    """`project-state/` **根级**散落的一次性中间件——§9.9 之下唯一允许清理的 state 内容。

    只认「深度恰为 `project-state/<文件>`」+「名字自带临时身份」，**不按文件名枚举正式产出**：
    枚举式白名单在新增正式产出时必然漏保护（本框架手抄登记表已因此漂移过）。
    """
    if not p.is_file():
        return False
    parts = rel_parts(root, p)
    return len(parts) == 2 and parts[0] == PROTECTED_STATE_DIR and is_temp_litter(p)


def is_protected(root: Path, p: Path) -> bool:
    """硬保护判定：`证据/`（卡卷宗与测试卡）、`交付/发出记录.md`、`.git/`，
    以及 `project-state/` 下的一切正式产出（**仅其根级散落的一次性中间件除外**）一律不清理。

    `project-state/` 是正式产出区：正式产物的备份/写入中临时件（`xxx.bak`、`overall.json.tmp`）
    被当垃圾清掉就是数据丢失，故整区保护；只有根级的 tmp 命名纪律漏网者仍按 §9.9 清理，
    且**不递归**（子目录内即便形如中间件也属正式产出，一律保护）。
    """
    parts = rel_parts(root, p)
    if any(seg in PROTECTED_DIR_NAMES for seg in parts):
        return True
    if PROTECTED_STATE_DIR in parts:
        return not is_state_root_litter(root, p)
    rel = "/".join(parts)
    return any(rel == q or rel.startswith(q + "/") for q in protected_rel_paths(root))


def rel_str(root: Path, p: Path) -> str:
    try:
        return str(p.relative_to(root))
    except ValueError:
        return str(p)


def guard(root: Path, p: Path, skipped: list) -> bool:
    """命中硬保护 → 记入 skipped 并返回 True（调用方跳过该候选）。"""
    if is_protected(root, p):
        skipped.append({"path": rel_str(root, p), "kind": "hard-protected"})
        return True
    return False


def iter_junk(root: Path, skipped: list | None = None):
    """字节码/缓存/编辑器临时文件。**父目录已入选则不再列子项**（避免同名重复计数）；
    命中垃圾目录即剪枝不再下钻（`node_modules`/`.git`/虚拟环境同理只跳过不扫描）。

    命中硬保护目录（`证据/`、`.git/`、`project-state/`）时**剪枝、不列、记披露**——
    `证据/` 下的 `__pycache__` 同属证据链，`project-state/` 下的一切（`进度/__pycache__`、
    `契约/xxx.bak`）都属正式产出区，绝不参与清理。
    v4.4.0 修：原实现只跳过 `.git`，「证据硬保护」没有落成代码，`--apply` 会删进卡卷宗。
    v4.4.x 修：`project-state/` 同样只写在文档里——原实现递归下钻，会把正式产出区内的
    `__pycache__`/字节码当垃圾删掉（同一类「假保护」缺陷，故此处按 is_protected 统一剪枝）。"""
    if skipped is None:
        skipped = []
    prune = {".git", "node_modules", ".venv", "venv"}
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        for d in list(dirnames):
            if d in prune:
                dirnames.remove(d)
                continue
            dp = here / d
            if is_protected(root, dp):
                dirnames.remove(d)
                skipped.append({"path": rel_str(root, dp), "kind": "hard-protected"})
                continue
            if d in JUNK_DIR_NAMES:
                dirnames.remove(d)
                yield here / d
        for f in filenames:
            p = here / f
            if is_junk(p) and not is_protected(root, p):
                yield p


def old_in_family(files: list[Path]) -> list[Path]:
    """同族多文件 → 保留最新（mtime 最大，并列取名字最大），其余为旧版本候选。"""
    if len(files) < 2:
        return []
    ordered = sorted(files, key=lambda p: (p.stat().st_mtime, p.name))
    return ordered[:-1]


def scan(root: Path, now: _dt.datetime, tmp_age: float, art_age: float):
    r = {}
    skipped: list = []          # 硬保护命中项：只披露，不进任何清理候选
    art_name, dl_name = domain_dirs(root)   # 域名按 config 注册表（默认 产物//交付/）
    r["_art_name"], r["_dl_name"] = art_name, dl_name

    # L1 垃圾
    junk = sorted({p for p in iter_junk(root, skipped)}, key=lambda p: str(p))
    r["junk"] = [{"path": str(p.relative_to(root)), "size": size_of(p)} for p in junk]

    # L2 tmp 超龄（在飞的只报告）
    tmp_dir = root / "tmp"
    r["tmp_delete"], r["tmp_keep"] = [], []
    if tmp_dir.is_dir():
        for item in sorted(tmp_dir.iterdir(), key=lambda p: str(p)):
            if item.name == ".gitkeep" or guard(root, item, skipped):
                continue
            age = mtime_days(item, now)
            rec = {"path": str(item.relative_to(root)), "size": size_of(item), "age_days": age,
                   "age_cause": age_cause(age)}
            # 删除判据取「**明确超龄**」：mtime 读不到（-1 哨兵）或落在未来（0，见 mtime_days）
            # 都不构成超龄证据。原写法 `0 <= age < tmp_age` 把这两类一并送进删除桶（F-34）。
            if is_inflight(item.name) or not (tmp_age <= age):
                r["tmp_keep"].append(rec)
            else:
                r["tmp_delete"].append(rec)

    # L2.5 根级 / project-state 根散落的一次性中间件（tmp 命名纪律的漏网者）
    litter, litter_logs = [], []
    for base in (root, root / "project-state"):
        if not base.is_dir():
            continue
        for p in sorted(base.iterdir(), key=lambda x: x.name):
            if p.is_dir() or p.name in {".gitkeep", ".gitignore"}:
                continue
            # 手工白名单只对 project 根分支是防线；project-state 根分支已由 is_protected 兜底，
            # 新增正式产出不再需要登记（这里保留是保护项目根的 agent.md/config.json 等）。
            if p.name in {"agent.md", "config.json", "协作规范.md", "完成定义.md", "任务清单.md", "运行日志.md"}:
                continue
            if guard(root, p, skipped):
                continue
            rec = {"path": str(p.relative_to(root)), "size": size_of(p), "inflight": is_inflight(p.name)}
            if is_temp_litter(p):
                litter.append(rec)
            elif is_log_litter(p):
                litter_logs.append(rec)
    r["litter"] = litter
    r["litter_logs"] = litter_logs

    # L2.6 散落脚本/文档（只报告，归位归 path_audit）
    stray_scripts = []
    for base in (root, root / "project-state"):
        if not base.is_dir():
            continue
        for p in sorted(base.iterdir(), key=lambda x: x.name):
            if p.is_file() and p.name.lower().endswith(SCAN_SUFFIX) and not is_protected(root, p):
                stray_scripts.append(str(p.relative_to(root)))
    r["stray_scripts"] = stray_scripts

    # L3 产物区（域名按 config 注册表）同族旧版本
    art_dir = root / art_name
    r["artifacts_old"] = []
    if art_dir.is_dir():
        packs = [p for p in art_dir.iterdir() if p.is_file() and p.name.lower().endswith(PACKAGE_SUFFIX)]
        fams: dict[str, list[Path]] = {}
        for p in packs:
            fams.setdefault(family_key(p.name), []).append(p)
        for key, group in fams.items():
            for old in old_in_family(group):
                if guard(root, old, skipped):
                    continue
                _a = mtime_days(old, now)
                rec = {"path": str(old.relative_to(root)), "size": size_of(old),
                       "age_days": _a, "age_cause": age_cause(_a), "family": key}
                # 同 L2：**明确超龄**才归档，-1 哨兵与未来 mtime（0）一律保留（F-34 同类面）
                if is_inflight(old.name) or not (art_age <= rec["age_days"]):
                    r.setdefault("artifacts_keep", []).append(rec)
                else:
                    r["artifacts_old"].append(rec)
    r.setdefault("artifacts_keep", [])

    # L4 交付区（域名按 config 注册表）整洁度
    dl_dir = root / dl_name
    r["delivery_junk"], r["delivery_old"], r["delivery_delivered"] = [], [], []
    r["delivery_record"] = False
    if dl_dir.is_dir():
        r["delivery_record"] = (dl_dir / "发出记录.md").is_file()
        tops = [p for p in dl_dir.iterdir() if p.name not in {"发出记录.md", "归档", ".gitkeep"}]
        for p in tops:
            if guard(root, p, skipped):
                continue
            if is_temp_litter(p) or is_junk(p) or (p.is_dir() and p.name.lower() in
                                                   {"staging", "temp", "tmp", "中间产物", "打包中间"}):
                r["delivery_junk"].append({"path": str(p.relative_to(root)), "size": size_of(p)})
        file_tops = [p for p in tops if p.is_file() and p.name.lower().endswith(PACKAGE_SUFFIX)]
        fams: dict[str, list[Path]] = {}
        for p in file_tops:
            fams.setdefault(family_key(p.name), []).append(p)
        for key, group in fams.items():
            for old in old_in_family(group):
                if guard(root, old, skipped):
                    continue
                r["delivery_old"].append({"path": str(old.relative_to(root)), "size": size_of(old),
                                          "age_days": mtime_days(old, now), "family": key})
        # 已发出副本识别：文件名出现在《发出记录.md》正文里 → 该本地副本已交付给客户
        record_text = ""
        rec_path = dl_dir / "发出记录.md"
        if r["delivery_record"]:
            try:
                record_text = rec_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                record_text = ""
        for p in tops:
            if p.is_file() and p.name in record_text and not guard(root, p, skipped):
                r["delivery_delivered"].append({
                    "path": str(p.relative_to(root)), "size": size_of(p),
                    "age_days": mtime_days(p, now),
                    "superseded": any(o["path"] == str(p.relative_to(root)) for o in r["delivery_old"]),
                })

    # 同名去重（同一路径可能被多个分支命中）
    r["protected_skipped"] = sorted({(s["path"], s["kind"]) for s in skipped})
    return r


def apply_actions(root: Path, r: dict, args) -> list[str]:
    done: list[str] = []
    if args.apply:
        for item in r["junk"]:
            p = root / item["path"]
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
            done.append(f"删除垃圾 `{item['path']}`（{human(item['size'])}）")
        for item in r["tmp_delete"]:
            p = root / item["path"]
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
            done.append(f"清 tmp `{item['path']}`（{human(item['size'])}，{item['age_days']} 天）")
        for item in r["litter"]:
            if item["inflight"]:
                continue
            p = root / item["path"]
            p.unlink(missing_ok=True)
            done.append(f"删一次性中间件 `{item['path']}`（{human(item['size'])}）")
        for item in r["delivery_junk"]:
            p = root / item["path"]
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
            done.append(f"清交付中间物 `{item['path']}`（{human(item['size'])}）")

    art_name, dl_name = r.get("_art_name", "产物"), r.get("_dl_name", "交付")

    if args.apply and args.archive and r["artifacts_old"]:
        dest = root / art_name / "归档"
        dest.mkdir(parents=True, exist_ok=True)
        for item in r["artifacts_old"]:
            src = root / item["path"]
            if src.exists():
                shutil.move(str(src), str(dest / src.name))
                done.append(f"归档产物旧版 `{item['path']}` → `{art_name}/归档/`")

    if args.apply and args.archive_delivery and r["delivery_old"]:
        stamp = _dt.datetime.now().strftime("%Y%m%d")
        dest = root / dl_name / "归档" / stamp
        dest.mkdir(parents=True, exist_ok=True)
        for item in r["delivery_old"]:
            src = root / item["path"]
            if src.exists():
                shutil.move(str(src), str(dest / src.name))
                done.append(f"归档交付旧版 `{item['path']}` → `{dl_name}/归档/{stamp}/`")

    if args.confirm_delivered and r["delivery_delivered"]:
        done.append(f"（--confirm-delivered：已登记 {len(r['delivery_delivered'])} 项已发出副本；"
                    "本地副本仍需项目经理逐项列清单处置，脚本不代删）")

    return done


def render(root: Path, r: dict, now: _dt.datetime, done: list[str], args) -> str:
    L: list[str] = []
    L.append(f"# 工作区整洁盘点 · {root.name}")
    L.append("")
    L.append(f"> 时间：{now:%Y-%m-%d %H:%M}　模式：{'执行清理' if args.apply else '只报告（dry-run）'}")
    art_name, dl_name = r.get("_art_name", "产物"), r.get("_dl_name", "交付")
    L.append("> 分级：L1 垃圾 / L2 tmp 超龄 / L2.5 散落中间件 / L3 产物旧版 / L4 交付件。"
             f"（产物区=`{art_name}/`、交付区=`{dl_name}/`，域名按 config 注册表）")
    L.append("> **硬保护**（代码守卫，非仅文档）：任何路径含 `证据/`、`《发出记录.md》`、`.git/`，"
             "以及 `project-state/` 下的一切正式产出（仅其根级散落的一次性中间件除外）—— 永不进入清理候选。")
    L.append("> 控制台兼容：本报告刻意只用 ASCII 状态符，避免 Windows GBK 控制台打印崩溃。")
    L.append("")

    def table(title: str, rows: list[dict], cols: list[str], note: str = ""):
        L.append(f"## {title}：{len(rows)} 项" + (f"（{human(sum(x['size'] for x in rows))}）" if rows else ""))
        L.append("")
        if rows:
            L.append("| " + " | ".join(cols) + " |")
            L.append("|" + "---|" * len(cols))
            for x in rows:
                vals = []
                for c in cols:
                    if c == "路径":
                        vals.append(f"`{x['path']}`")
                    elif c == "体积":
                        vals.append(human(x["size"]))
                    elif c == "天前":
                        a = x.get("age_days", AGE_UNREADABLE)
                        # F-34：这里原来是 `"?" if a < 0`——把「读不到」与「未来」一起打成 `?`，
                        # 在「可清理」表里给出**无法解释的读数**（红旗读法：表里出现 `?` 即判据失效）。
                        vals.append(age_cause(a) if a < 0 else str(a))
                    else:
                        vals.append(str(x.get(c, "")))
                L.append("| " + " | ".join(vals) + " |")
        else:
            L.append("（空）")
        if note:
            L.append("")
            L.append(note)
        L.append("")

    table("L1 垃圾（缓存/编辑器临时/字节码）", r["junk"], ["路径", "体积"],
          "→ `--apply` 直接删，零风险。")
    table("L2 tmp 超龄（可清理）", r["tmp_delete"], ["路径", "体积", "天前"],
          "→ `--apply` 删除；**执行前确认无在飞任务**（部署分片/回滚备份/便携运行时）。")
    table("L2 tmp 在飞/未到期（保留）", r["tmp_keep"], ["路径", "体积", "天前"],
          "→ 名字含 backup/回滚/部署/分片 等提示词或未到阈值：**只报告不删**。")
    table("L2.5 散落一次性中间件", r["litter"], ["路径", "体积"],
          "→ `--apply` 删除（名字自带临时标识：`_YYYYMMDD_*`、`*.out/.tmp/.bak`、curl 误产物）。")
    table("L2.5 散落日志（**需人工确认，本脚本不删**）", r["litter_logs"], ["路径", "体积"],
          "→ 失败日志常是唯一线索：先确认无追溯价值，再手工移 `tmp/` 或删除；**有证据价值的转正式路径**。")
    if r["stray_scripts"]:
        L.append(f"## L2.6 根级散落脚本/文档：{len(r['stray_scripts'])} 项（**归位属结构变更，本脚本不动**）")
        L.append("")
        L.append("> " + "、".join(f"`{s}`" for s in r["stray_scripts"]))
        L.append("> 处置走 `scripts/path_audit.py` + 主控裁决（工具→`scripts/`、一次性→`tmp/`、对外→`交付/`）。")
        L.append("")
    table(f"L3 {art_name}/ 同族旧版本", r["artifacts_old"], ["路径", "体积", "天前"],
          f"→ `--apply --archive` 移入 `{art_name}/归档/`（**保留最新有效版**）。")
    if r.get("artifacts_keep"):
        table(f"L3 {art_name}/ 保留（在飞或未到期）", r["artifacts_keep"], ["路径", "体积", "天前"])
    table(f"L4 {dl_name}/ 中间物", r["delivery_junk"], ["路径", "体积"],
          "→ `--apply` 删除（打包 staging/压缩中间件不留客户交互区）。")
    table(f"L4 {dl_name}/ 被取代旧版本", r["delivery_old"], ["路径", "体积", "天前"],
          f"→ `--apply --archive-delivery` 移入 `{dl_name}/归档/<日期>/`；"
          "**《发出记录.md》与其回执永不清理**（审计链）。")
    if r["delivery_delivered"]:
        L.append(f"## L4 已发给客户的本地副本：{len(r['delivery_delivered'])} 项（**本脚本永不自动删**）")
        L.append("")
        L.append("| 路径 | 体积 | 天前 | 已被新版取代 |")
        L.append("|---|---|---|---|")
        for d in r["delivery_delivered"]:
            age = "?" if d["age_days"] < 0 else str(d["age_days"])
            L.append(f"| `{d['path']}` | {human(d['size'])} | {age} | {'是' if d['superseded'] else '否'} |")
        L.append("")
        L.append("> 这些文件名出现在《发出记录.md》正文里 = **已交付客户**。删除本地副本属**不可逆对外动作**："
                 "须项目经理列清单（文件名/版本/哈希/发给谁/回执），经主控/用户确认后再手工处置；"
                 "**《发出记录.md》是唯一凭证，永不随件删除**。")
        L.append("")
    L.append(f"## {dl_name}/ 审计链：{'`发出记录.md` 在位（审计链完整）' if r['delivery_record'] else f'缺 `{dl_name}/发出记录.md`（审计链断裂，须补）'}")
    L.append("")

    if r.get("protected_skipped"):
        L.append(f"## 硬保护命中（跳过，**不在任何清理候选内**）：{len(r['protected_skipped'])} 项")
        L.append("")
        L.append("> 这些目录/文件被代码守卫拦下——即使 `--apply` 也不会被删/被移动。"
                 "误删证据 = 证据链断裂，比工作区脏乱更贵。")
        L.append("")
        for path, kind in r["protected_skipped"]:
            L.append(f"- `{path}`（{kind}）")
        L.append("")

    if done:
        L.append(f"## 本次已执行：{len(done)} 项")
        L.append("")
        for d in done:
            L.append(f"- {d}")
        L.append("")
    elif not args.apply:
        L.append("## 处置建议（**本脚本默认只报告，不删任何东西**）")
        L.append("")
        L.append("```bash")
        L.append(f"python scripts/workspace_hygiene.py {root} --apply                 # L1+L2+L2.5+交付中间物")
        L.append(f"python scripts/workspace_hygiene.py {root} --apply --archive        # 另归档产物旧版")
        L.append(f"python scripts/workspace_hygiene.py {root} --apply --archive-delivery  # 另归档交付旧版")
        L.append("```")
        L.append("")
        L.append("> 无法判定归属的游离物**不删**，登记 `project-state/待办/自然人.md` 请用户裁决。")
        L.append("")
    return "\n".join(L)


def main() -> int:
    # Windows 控制台（GBK/CP936）打印中文报告时，遇到不可编码字符会 UnicodeEncodeError 崩掉。
    # 只放宽 errors，不改编码（改 utf-8 会在 GBK 控制台显示乱码）。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, OSError):
            pass

    ap = argparse.ArgumentParser(description="工作区整洁收口（默认只报告）")
    ap.add_argument("project_root")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--write", action="store_true", help="同时写 project-state/整洁盘点-<日期>.md")
    ap.add_argument("--apply", action="store_true", help="执行 L1+L2+L2.5+交付中间物 删除")
    ap.add_argument("--archive", action="store_true", help="配合 --apply：产物旧版本移 产物/归档/")
    ap.add_argument("--archive-delivery", action="store_true", help="配合 --apply：交付旧版本移 交付/归档/")
    ap.add_argument("--confirm-delivered", action="store_true",
                    help="登记「已发出副本」清单供人工逐项处置（本脚本永不代删对外件）")
    ap.add_argument("--tmp-age-days", type=float, default=7.0, help="tmp 清理阈值（默认 7 天，保守）")
    ap.add_argument("--artifact-age-days", type=float, default=14.0, help="产物旧版本阈值（默认 14 天）")
    args = ap.parse_args()

    root = Path(args.project_root).expanduser().resolve()
    if not (root / "project-state").is_dir():
        print(f"[错误] {root} 下没有 project-state/（不像框架工作区）")
        return 2

    now = _dt.datetime.now()
    r = scan(root, now, args.tmp_age_days, args.artifact_age_days)
    done = apply_actions(root, r, args)

    if args.json:
        print(json.dumps({**r, "executed": done, "generated": now.strftime("%Y-%m-%d %H:%M:%S")},
                         ensure_ascii=False, indent=2))
        return 0

    text = render(root, r, now, done, args)
    print(text)
    if args.write:
        out = root / "project-state" / f"整洁盘点-{now:%Y%m%d}.md"
        out.write_text(text + "\n", encoding="utf-8")
        print(f"\n[已写入] {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v3.22.0 Git 审计（git_audit）：对**单个代码库**做版本控制治理机检，**只报告不改动**。

设计原则（对齐 scripts/path_audit.py）：
- **只报告，不改动**——修复动作由角色按 `project-state/契约/Git工作流.md` 执行（分支/合并属写操作，脚本不代劳）。
- **判据来自契约（配置驱动零硬编码）**：`--branch-model` 指向的 `契约/Git工作流.md` 内有一段机读块
  （```json git-workflow-model 围栏），默认分支名/分支命名正则/提交信息正则/敏感词/大文件阈值全从那里读；
  契约缺失或机读块缺失时**回退内置默认**（零依赖兜底优先）并在报告里明示"判据来源"。
- **零依赖**：纯标准库 + 调用本机 `git` CLI，不装任何包。
- **未覆盖项明说**（见报告末段）：服务端保护分支策略、历史里删过的密钥、CI 是否真跑、冲突"合得对不对"等
  本地看不到或本脚本不判——**不假装覆盖**。

用法：
    python git_audit.py <代码库路径>                                  # 人类可读报告，只报告
    python git_audit.py <代码库路径> --branch-model 契约/Git工作流.md   # 指定判据契约（相对代码库或当前目录）
    python git_audit.py <代码库路径> --commits 30                      # 检查最近 30 条提交信息
    python git_audit.py <代码库路径> --json                            # 机器可读

退出码：
    0 = 无 findings（严重/高/中 均为 0，可另有"提示"）
    1 = 有 findings（严重/高/中 ≥1）——**"提示"不影响退出码**，见 README 段「严重度与门禁」
    2 = 用法错 / 环境错（路径不存在、git 不可用、--branch-model 指定但文件不存在）
"""
from __future__ import annotations

import argparse
import datetime as _dt
import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SEV_TITLE = {"critical": "严重", "high": "高", "medium": "中", "low": "提示"}
SEV_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
FAIL_SEVS = ("critical", "high", "medium")  # 影响退出码的严重度；"提示"不影响

# 内置默认判据（契约机读块缺失时的兜底；与 契约/Git工作流.md §7 机读块同构）
DEFAULT_MODEL = {
    "default_branch": "master",
    "protected_branches": ["master"],
    # slug 允许 ASCII 与中文（\w 含 CJK），但禁空格/斜杠/标点——分支名要能在命令行与 URL 里干净传递
    "branch_patterns": {
        "feat": r"^feat/card-\d{3,}-[\w.-]+$",
        "fix": r"^fix/card-\d{3,}-[\w.-]+$",
        "hotfix": r"^hotfix/[\w.-]+$",
        "chore": r"^chore/[\w.-]+$",
    },
    "commit_pattern": r"^(feat|fix|refactor|docs|test|chore|hotfix)\([^)]+\): .+(\[(?:card-\d{3,}|tc-\d{3,}|hotfix-[A-Za-z0-9._-]+)(?:,(?:card-\d{3,}|tc-\d{3,}|hotfix-[A-Za-z0-9._-]+))*\])$",
    "commit_types": ["feat", "fix", "refactor", "docs", "test", "chore", "hotfix"],
    "require_card_tag": True,
    "allow_merge_commits": True,
    "codeup_hosts": ["codeup.aliyun.com"],
    "gitignore_required": [".env", "*.key", "*.pem", "data", "产物", "node_modules", "__pycache__", "build", "dist", "tmp"],
    "gitignore_allow": [".env.example", ".env.sample", ".env.template"],
    "suspicious_name_patterns": [".env", ".env.*", "*.key", "*.pem", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*", "*credentials*", "*secrets.*"],
    "secret_name_allow": [".env.example", ".env.sample", ".env.template"],
    "secret_regex": r"(api[_-]?key|secret|password|passwd|sk-)",
    "secret_scan_exclude": ["*.lock", "*.min.js", "*.min.css", "*.map", "package-lock.json"],
    "large_file_mb": 10,
    "branch_stale_days": 3,
    "recent_commits": 20,
}

NOT_COVERED = [
    "服务端保护分支策略（云效 Codeup 的「保护分支/合并请求必审」是服务端设置，本地看不到）——需在云效网页核对，或用 `git push --dry-run origin master` 观察是否被拒",
    "提交者身份的真实性（`user.name`/`user.email` 可被任意设置，脚本只能看有没有配，不能验真身）",
    "历史中曾经误入、后被删除的密钥（本脚本只扫当前索引/HEAD）——需全历史：`git log -p -S'<关键词>' --all` 或 `git log --all --diff-filter=D --name-only`",
    "CI/CD 是否真的在跑、是否真通过（服务端流水线状态，本地不可见）",
    "合并冲突「合得对不对」（脚本只能看分支合没合、提交信息合不合规，语义正确性必须靠**受影响回归复跑**，见 契约/Git工作流.md §4）",
    "Git LFS 对象是否真的上传成功（需 `git lfs fsck`，依赖 lfs 扩展；本脚本只在检测到 `.gitattributes` 含 `filter=lfs` 时提示）",
    "代码内容质量本身（不属版本控制治理，归 契约/代码复查规则库.md 十域）",
]


# ---------------------------------------------------------------- 基础工具

def find_git() -> str | None:
    for name in ("git", "git.exe"):
        p = None
        try:
            from shutil import which
            p = which(name)
        except Exception:
            p = None
        if p:
            return p
    return None


GIT_BIN: str | None = None
ENV = dict(os.environ, GIT_PAGER="cat", GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")


def run_git(repo: Path, args: list[str], timeout: int = 90) -> tuple[int, str, str]:
    """执行 git（-C repo），返回 (rc, stdout, stderr)，均按 UTF-8 解码容错。"""
    return run_git_raw(repo, args, timeout, decode=True)  # type: ignore[return-value]


def run_git_raw(repo: Path, args: list[str], timeout: int = 90, decode: bool = False):
    cmd = [GIT_BIN or "git", "-c", "core.quotepath=false", "-c", "i18n.logOutputEncoding=UTF-8", "-C", str(repo)] + list(args)
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, env=ENV)
    except FileNotFoundError:
        return (127, "", "未找到 git 可执行文件")
    except subprocess.TimeoutExpired:
        return (124, "", f"git 超时（{timeout}s）：{' '.join(args)}")
    if decode:
        return (p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace"))
    return (p.returncode, p.stdout, p.stderr)


def human_mb(n: int) -> str:
    return f"{n / 1024 / 1024:.1f}MB" if n >= 1024 * 1024 else f"{n / 1024:.0f}KB"


def one_line(s: str, n: int = 160) -> str:
    s = " ".join((s or "").split())
    return s[:n] + "…" if len(s) > n else s


SECRET_REDACTED = "（原为命中正文，按机密纪律已脱敏，不回显）"


def redact_secret_hit(hit_line: str, pattern: str) -> str:
    """把 `git grep` 命中行脱敏成「文件:行号 ← 命中关键词[…]」。

    为什么必须脱敏：GIT-006b 的输出会被写进 `运行日志.md`（框架纪律要求自检留痕），
    原实现把命中正文截 130 字符原样打印 ⇒ **跑一次自检就把凭据明文抄进日志**，
    密钥卫生审计自身成了泄漏路径。这里只保留定位信息（文件:行号）与命中的关键词名，
    正文一律不回显——定位够用，复现不需要看到值。
    """
    parts = (hit_line or "").split(":", 2)
    if len(parts) < 3:
        return SECRET_REDACTED
    path, lineno, content = parts[0], parts[1], parts[2]
    try:
        names = sorted({m.group(0).lower() for m in re.finditer(pattern, content)})
    except re.error:
        names = []
    return "%s:%s  ← 命中关键词 %s（值已脱敏）" % (path, lineno, names)


# ---------------------------------------------------------------- 判据契约（机读块）

def extract_machine_block(text: str, marker: str = "git-workflow-model") -> dict | None:
    """从契约 md 里取 ```json git-workflow-model 围栏内的 JSON（人读条款之外的机读判据）。"""
    lines = text.splitlines()
    inside = False
    buf: list[str] = []
    for ln in lines:
        s = ln.strip()
        if not inside and s.startswith("```") and marker in s:
            inside = True
            buf = []
            continue
        if inside:
            if s.startswith("```"):
                try:
                    return json.loads("\n".join(buf))
                except json.JSONDecodeError as e:
                    print(f"[警告] 机读块不是合法 JSON（{e}），回退内置默认判据", file=sys.stderr)
                    return None
            buf.append(ln)
    return None


def load_model(path: Path | None) -> tuple[dict, str]:
    """返回（判据, 来源说明）。契约给了就用契约，否则内置默认（并明示）。"""
    model = json.loads(json.dumps(DEFAULT_MODEL))  # deep copy
    if path is None:
        return model, "内置默认（未传 --branch-model）"
    if not path.is_file():
        return model, f"内置默认（--branch-model 指定了 {path} 但文件不存在）"
    block = extract_machine_block(path.read_text(encoding="utf-8", errors="replace"))
    if not block:
        return model, f"内置默认（{path} 内无 ```json git-workflow-model 机读块）"
    model.update(block)
    for k, v in block.items():
        if k.startswith("_"):
            model.pop(k, None)
    return model, f"契约机读块：{path}"


# ---------------------------------------------------------------- 检查

class Audit:
    def __init__(self, target: Path, model: dict, model_src: str, commits_n: int):
        self.target = target
        self.model = model
        self.model_src = model_src
        self.commits_n = commits_n
        self.findings: list[dict] = []
        self.passed: list[str] = []
        self.facts: dict = {}

    def add(self, fid: str, sev: str, title: str, evidence: list[str], advice: str, items: list[str] | None = None):
        self.findings.append({"id": fid, "severity": sev, "title": title,
                              "evidence": evidence, "advice": advice, "items": items or []})

    def ok(self, msg: str):
        self.passed.append(msg)

    def note(self, fid: str, title: str, evidence: list[str], advice: str, items: list[str] | None = None):
        self.add(fid, "low", title, evidence, advice, items)


def norm_pattern(s: str) -> str | None:
    s = s.strip()
    if not s or s.startswith("#"):
        return None
    return s.lstrip("/").rstrip("/").lower()


def ignore_samples(pat: str) -> list[str]:
    """把契约里的排除项写成可被 `git check-ignore` 实测的样本路径（`*` 部位换成 x；目录名补 /x）。"""
    p = pat.strip().rstrip("/").replace("**", "x").replace("*", "x")
    cands = [p]
    if "/" not in p:
        cands.append(p + "/x")
    return [c for c in cands if c]


def check_ignore(repo: Path, sample: str) -> str | None:
    """实测某路径是否被生效的 .gitignore 规则忽略；命中返回 `<文件>:<行>:<规则>`（git check-ignore -v）。"""
    rc, out, _ = run_git(repo, ["check-ignore", "-v", "--", sample])
    if rc == 0 and out.strip():
        return out.strip().split("\t")[0]
    return None


def audit_repo(a: Audit, repo: Path) -> None:
    m = a.model
    rc, out, err = run_git(repo, ["rev-parse", "--show-toplevel"])
    if rc != 0:
        a.facts["is_repo"] = False
        return
    a.facts["is_repo"] = True
    a.facts["toplevel"] = out.strip()
    if Path(a.facts["toplevel"]).resolve() != Path(repo).resolve():
        a.facts["subdir"] = str(Path(repo).resolve())
    rc, out, _ = run_git(repo, ["rev-parse", "--is-bare-repository"])
    bare = out.strip() == "true"
    a.facts["bare"] = bare

    # —— 1. 分支与默认分支
    rc, out, _ = run_git(repo, ["rev-parse", "--abbrev-ref", "HEAD"])
    cur = out.strip()
    if rc != 0 or not cur:
        rc2, out2, _ = run_git(repo, ["symbolic-ref", "--short", "HEAD"])
        cur = out2.strip() if rc2 == 0 else "?"
    a.facts["branch"] = cur
    declared = m.get("default_branch", "master")
    rc, _, _ = run_git(repo, ["show-ref", "--verify", "--quiet", f"refs/heads/{declared}"])
    default_exists = rc == 0
    if not default_exists:
        rc, out, _ = run_git(repo, ["symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD"])
        alt = out.strip().split("/")[-1] if rc == 0 and out.strip() else ""
        if not alt:
            for cand in ("master", "main"):
                rc2, _, _ = run_git(repo, ["show-ref", "--verify", "--quiet", f"refs/heads/{cand}"])
                if rc2 == 0:
                    alt = cand
                    break
        eff = alt or declared
        a.add("GIT-003a", "medium", f"契约声明的默认分支 `{declared}` 在本地不存在",
              [f"`git show-ref --verify refs/heads/{declared}` → 非 0（分支不存在）",
               f"`git branch --format=%(refname:short)` → {one_line(', '.join(branch_list(repo)) or '（无分支）')}"],
              f"要么按契约建 `{declared}`（`git branch {declared} <起点>` 或改默认分支名重推），"
              f"要么把契约 §0/§7 的 `default_branch` 改成实际默认分支（二选一，别让文档与实际分叉）")
        a.facts["default_branch"] = eff
        a.facts["default_branch_declared"] = declared
    else:
        a.facts["default_branch"] = declared
        a.facts["default_branch_declared"] = declared
    default = a.facts["default_branch"]

    rc, out, _ = run_git(repo, ["rev-parse", "--is-shallow-repository"])
    a.facts["shallow"] = out.strip() == "true"

    has_commits = True
    rc, out, _ = run_git(repo, ["rev-list", "--count", "HEAD"])
    if rc != 0:
        has_commits = False
        a.facts["commits_total"] = 0
    else:
        a.facts["commits_total"] = int(out.strip() or 0)

    # —— 2. 远端
    rc, out, _ = run_git(repo, ["remote", "-v"])
    remotes: dict[str, dict[str, str]] = {}
    for ln in out.splitlines():
        parts = ln.split()
        if len(parts) >= 3:
            remotes.setdefault(parts[0], {})[parts[2].strip("()")] = parts[1]
    a.facts["remotes"] = remotes
    if not remotes:
        a.add("GIT-002a", "high", "无远端仓库（只有本地库）",
              ["`git remote -v` → （空输出）"],
              "本地库 = 云端没有副本：磁盘损坏/误删/误覆盖不可恢复，也无法协作与追责。"
              "按 契约/Git工作流.md §1 建云效仓库后 `git remote add origin <地址>` 并 `git push -u origin <默认分支>`")
    else:
        hosts = m.get("codeup_hosts", [])
        urls = [u for d in remotes.values() for u in d.values()]
        if hosts and not any(any(h in u for h in hosts) for u in urls):
            a.add("GIT-002b", "medium", "远端未指向云效 Codeup（本项目定档：云效新建仓库）",
                  ["`git remote -v` →", *[f"    {u}" for u in urls]],
                  "本项目 §0 定档为云效 Codeup；非云效远端要么换成云效地址，要么在契约 §0 写明例外与理由")
        else:
            a.ok(f"GIT-002 远端已配置且指向云效：{one_line('; '.join(urls), 120)}")
        no_push = [name for name, d in remotes.items() if "push" not in d]
        if no_push:
            a.add("GIT-002c", "medium", "远端只有 fetch 地址、没有 push 地址（只读克隆）",
                  [f"`git remote -v` → {name}: {'; '.join(f'{k}={v}' for k, v in d.items())}" for name, d in remotes.items() if name in no_push],
                  "无法推送 = 产出回不到云端。补 push 地址：`git remote set-url --push origin <地址>`")
        else:
            a.ok("GIT-002 远端 fetch/push 地址齐备")

    # —— 3. 当前分支合规
    protected = set(m.get("protected_branches", [])) | {default}
    if cur in ("HEAD", "?", ""):
        a.add("GIT-003b", "high", "处于 detached HEAD（不在任何分支上）",
              [f"`git rev-parse --abbrev-ref HEAD` → {cur}"],
              "detached HEAD 上的提交没有分支认领，极易丢失。`git switch <分支>` 回到工作分支；已有的游离提交先 `git branch <新分支> HEAD` 认领")
    elif cur in protected:
        dirty, ahead = worktree_dirty(repo), ahead_of_remote(repo, cur)
        if dirty or (ahead or 0) > 0:
            a.add("GIT-003c", "medium", f"在保护分支 `{cur}` 上直接开发",
                  [f"`git status --porcelain` → {len(dirty)} 条未提交改动" if dirty else "`git status --porcelain` → 干净",
                   f"`git rev-list --count origin/{cur}..HEAD` → 本地领先 {ahead} 个提交" if ahead is not None else "（无 origin/<默认分支> 跟踪引用，无法比领先数）"],
                  f"一卡一分支：`git switch -c feat/card-NNN-<slug>` 后再开发；已发生的改动：`git stash` 或 `git switch -c` 带走，"
                  f"已直推的提交按 §4 用合并请求补齐纪律，别继续在 {cur} 上堆")
        else:
            a.note("GIT-003c", f"当前位于保护分支 `{cur}`，工作区干净且未领先远端（收口态，正常）",
                   [f"`git status --porcelain` → 干净", f"`git rev-list --count origin/{cur}..HEAD` → 0"],
                   "要开工请先按分支模型建分支（一卡一分支）")
    else:
        hit = [k for k, pat in (m.get("branch_patterns") or {}).items() if re.match(pat, cur)]
        if hit:
            a.ok(f"GIT-003 当前分支命名合规：`{cur}`（类型 {hit[0]}）")
        else:
            a.add("GIT-003d", "medium", f"分支名 `{cur}` 不符合分支模型",
                  [f"`git rev-parse --abbrev-ref HEAD` → {cur}",
                   "判据（契约机读块 branch_patterns）：",
                   *[f"    {k}: {v}" for k, v in (m.get("branch_patterns") or {}).items()]],
                  "改名对齐模型：`git branch -m <新名>`；分支名必须与卡片号绑定（feat|fix/card-NNN-<slug>）")
        # 分支号 → 工作区卡片 存在性（仅真实项目工作区；框架模板仓库 project_name 为空则跳过）
        ws = find_workspace(repo)
        mm = re.search(r"(card|tc)-(\d{3,})", cur)
        if ws and mm:
            kind, num = mm.group(1), mm.group(2)
            d = ws / ("需求卡" if kind == "card" else "测试卡") / f"{kind}-{num}"
            if d.is_dir():
                a.ok(f"GIT-003 分支号与工作区卡片对应：`{kind}-{num}` → {d.relative_to(ws)}")
            else:
                a.note("GIT-003e", f"分支引用的卡片 `{kind}-{num}` 在工作区不存在（分支与卡片脱节）",
                       [f"`{d}` → 不存在（工作区：{ws}）"],
                       "分支命名与卡片号强绑定：建分支前先建卡，或改分支名对齐真实卡号")

    # —— 4. 工作区干净度
    dirty = worktree_dirty(repo)
    if bare:
        a.note("GIT-004", "裸仓库（无工作区），工作区干净度检查跳过", [f"`git rev-parse --is-bare-repository` → true"], "无需处置")
    elif dirty:
        staged = [x for x in dirty if x[:2].strip() and not x.startswith("??")]
        untracked = [x for x in dirty if x.startswith("??")]
        a.add("GIT-004", "medium", f"工作区不干净（{len(dirty)} 条未提交改动）",
              [f"`git status --porcelain` → {len(dirty)} 条（已改动/已暂存 {len(staged)} · 未跟踪 {len(untracked)}）",
               *[f"    {one_line(x, 110)}" for x in dirty[:8]],
               "（只列前 8 条）" if len(dirty) > 8 else "",
              ],
              "**派发前工作区应干净**：未提交改动会串进别人的合并、让回滚点失准。"
              "先提交或 `git stash` 再交给下一个角色；未跟踪文件确认无用则删或写进 .gitignore")
    else:
        a.ok("GIT-004 工作区干净（无未提交改动）")

    # —— 5. 最近 N 条提交信息
    if not has_commits:
        a.note("GIT-005", "仓库尚无提交（unborn HEAD），提交信息检查跳过", ["`git rev-list --count HEAD` → 非 0 退出码"],
               "首次提交按 §3 写：`<type>(<scope>): <subject> [card-NNN]`")
    else:
        rc, out, _ = run_git(repo, ["log", f"-n{max(a.commits_n, 1)}", "--format=%h\x1f%s"])
        rows = [ln.split("\x1f", 1) for ln in out.splitlines() if "\x1f" in ln]
        cpat = m.get("commit_pattern", DEFAULT_MODEL["commit_pattern"])
        types = "|".join(m.get("commit_types", []))
        tag_re = r"\[(card|tc)-\d{3,}\]|\[hotfix-[A-Za-z0-9._-]+\]"
        bad_fmt, bad_tag, merged = [], [], 0
        for sha, subj in rows:
            if m.get("allow_merge_commits", True) and (subj.startswith("Merge ") or subj.startswith("merge:")):
                merged += 1
                continue
            if re.match(cpat, subj):
                continue
            if types and re.match(rf"^({types})\([^)]+\): .+", subj) and not re.search(tag_re, subj):
                bad_tag.append((sha, subj))
            else:
                bad_fmt.append((sha, subj))
        a.facts["commits_checked"] = len(rows) - merged
        a.facts["commits_bad_format"] = len(bad_fmt)
        a.facts["commits_missing_card_tag"] = len(bad_tag)
        if bad_fmt:
            a.add("GIT-005a", "medium", f"最近 {len(rows)} 条提交中有 {len(bad_fmt)} 条不符合提交信息规范",
                  [f"`git log -n{len(rows)} --format=%h%x1f%s` → 不合规 {len(bad_fmt)} 条",
                   "判据（commit_pattern）：" + cpat,
                   *[f"    {sha} {one_line(s, 100)}" for sha, s in bad_fmt[:5]],
                   "（只列前 5 条）" if len(bad_fmt) > 5 else ""],
                  "按 §3 重写为 `<type>(<scope>): <subject> [card-NNN]`（未推送的可 `git commit --amend` / `rebase -i` 改；"
                  "已推送的别再改历史，后续提交规范即可）")
        if bad_tag:
            a.add("GIT-005b", "medium", f"最近 {len(rows)} 条提交中有 {len(bad_tag)} 条缺卡片标记",
                  [*[f"    {sha} {one_line(s, 100)}" for sha, s in bad_tag[:5]],
                   "（只列前 5 条）" if len(bad_tag) > 5 else ""],
                  "提交与卡卷宗必须可对账：末尾补 `[card-NNN]`（系统级测试轮用 `[tc-NNN]`，急修用 `[hotfix-<事故号>]`）")
        if not bad_fmt and not bad_tag:
            a.ok(f"GIT-005 最近 {len(rows)} 条提交信息全部合规（含卡片标记；合并提交 {merged} 条豁免）")

    # —— 6. 可疑入库：密钥/凭据 + 大文件
    tracked = tracked_files(repo)
    a.facts["tracked_files"] = len(tracked) if tracked is not None else None
    if tracked:
        allow = [x.lower() for x in m.get("secret_name_allow", [])]
        hits = []
        for p in tracked:
            base = p.rsplit("/", 1)[-1]
            low = p.lower()
            if any(fnmatch.fnmatch(base.lower(), al) for al in allow):
                continue
            if any(fnmatch.fnmatch(base.lower(), pat) or fnmatch.fnmatch(low, pat) for pat in m.get("suspicious_name_patterns", [])):
                hits.append(p)
        if hits:
            a.add("GIT-006a", "critical", f"疑似密钥/凭据文件已入库（{len(hits)} 个）",
                  ["`git ls-files` + 名称判据 →", *[f"    {h}" for h in hits[:10]],
                   "（只列前 10 条）" if len(hits) > 10 else ""],
                  "① 先把文件从版本控制摘掉：`git rm --cached <文件>` + 写进 .gitignore（内容不清除，历史里仍在）；"
                  "② **立即轮换该凭据**（轮换 > 改历史，见 §5）；③ 如需清历史再走 `git filter-repo` 并全员同步")
        else:
            a.ok("GIT-006 无 .env/*.key/*.pem 类文件入库")
        # `-- .`：范围锁在传入路径（避免子目录审计时把整仓命中都算进来）
        rc, out, err = run_git(repo, ["grep", "--cached", "-n", "-I", "-E", m.get("secret_regex", DEFAULT_MODEL["secret_regex"]), "--", "."])
        if rc == 128:
            a.note("GIT-006b", "密钥字符串扫描未能执行（索引为空或 git grep 报错）",
                   [f"`git grep --cached -n -I -E '{m.get('secret_regex')}' -- .` → exit 128 / {one_line(err, 120)}"],
                   "仓库无提交/无索引时属正常；有提交而报错需查 git 版本与索引状态")
        elif rc == 1:
            a.ok(f"GIT-006 密钥字符串扫描无命中（`git grep --cached -n -I -E '{m.get('secret_regex')}' -- .`）")
        else:
            excl = m.get("secret_scan_exclude", [])
            lines = [ln for ln in out.splitlines()
                     if not any(fnmatch.fnmatch(ln.split(":", 1)[0].lower(), e.lower()) for e in excl)]
            n = len(lines)
            sev = "high" if n else "low"
            if n:
                a.add("GIT-006b", sev, f"密钥关键词命中 {n} 处（需人工确认是否真凭据）",
                      [f"`git grep --cached -n -I -E '{m.get('secret_regex')}' -- .` → {n} 行命中",
                       *[f"    {redact_secret_hit(x, m.get('secret_regex', DEFAULT_MODEL['secret_regex']))}"
                         for x in lines[:8]],
                       "（只列前 8 条；**命中正文一律不回显**——本检查器的输出要写进运行日志）"],
                      "逐条判定：真凭据 → 立即轮换 + 从入库内容移除（§5）；同名的变量名/文档示例 → 按《忽略决策记录》登记后放过。"
                      "**命中但忽略必须留记录（问题/审核记录.md）**")
            else:
                a.ok("GIT-006 密钥关键词仅命中被排除文件（lock/min 等）")

    # —— 7. 大文件入库
    big = big_tracked_files(repo, m.get("large_file_mb", 10))
    if big is None:
        a.note("GIT-007", "大文件检查未能执行（尚无 HEAD，无入库文件表）", ["`git ls-tree -r -l HEAD` → 非 0"], "首次提交后再跑")
    elif big:
        lfs = (repo / ".gitattributes").is_file() and "filter=lfs" in (repo / ".gitattributes").read_text(encoding="utf-8", errors="replace")
        sev = "low" if lfs else "medium"
        a.add("GIT-007", sev, f"入库大文件 {len(big)} 个（>{m.get('large_file_mb')}MB）" + ("（已配 Git LFS）" if lfs else ""),
              ["`git ls-tree -r -l HEAD` 按体积排序 →", *[f"    {human_mb(s)}  {p}" for p, s in big[:10]],
               "（只列前 10 条）" if len(big) > 10 else ""],
              "大文件走 Git LFS（`.gitattributes` 加 `*.<ext> filter=lfs`）或移出仓库只留生成脚本/下载指针；"
              "**已入库的改 LFS 需重写历史**（`git lfs migrate import --above=10MB`），先与团队确认窗口期")
    else:
        a.ok(f"GIT-007 无 >{m.get('large_file_mb')}MB 的入库大文件")

    # —— 8. .gitignore 基线（**实测生效性**，不读文件文本——嵌套 .gitignore/根 .gitignore 由 git 自己判）
    required = [x.strip() for x in m.get("gitignore_required", []) if x.strip()]
    missing, effective = [], {}
    for pat in required:
        hit = None
        for s in ignore_samples(pat):
            hit = check_ignore(repo, s)
            if hit:
                break
        (effective.setdefault(pat, hit) if hit else missing.append(pat))
    if missing and len(missing) == len(required):
        a.add("GIT-008a", "medium", "无有效 .gitignore（基线排除项全部未生效）",
              ["`git check-ignore -v <样本路径>` → 全部非 0（未被忽略）",
               *[f"    未生效：{x}" for x in missing]],
              "按 §1 基线落 .gitignore：" + "、".join(required) +
              "（`.env`/`*.key` 等密钥类、`data/`/`产物/`/`tmp/` 数据与临时类、`node_modules/`/`__pycache__`/`build`/`dist` 依赖产物类）")
    elif missing:
        a.add("GIT-008b", "medium", f".gitignore 基线排除项有 {len(missing)} 个**实测未生效**",
              ["`git check-ignore -v <样本路径>`（逐项实测，非读文本比对）→",
               *[f"    未生效：{x}" for x in missing],
               "  已生效示例：" + ("; ".join(f"{k} ← {v}" for k, v in list(effective.items())[:3]) or "（无）")],
              "补齐缺项；**补了才有意义**——已入库的文件要 `git rm --cached` 才真的摘出去（见下一条）")
    else:
        a.ok(f"GIT-008 .gitignore 基线排除项全部实测生效（{len(required)} 项，`git check-ignore -v` 逐项验过；"
             f"生效来源示例：{'; '.join(f'{k} ← {v}' for k, v in list(effective.items())[:2])}）")
    rc, out, _ = run_git(repo, ["ls-files", "-i", "-c", "--exclude-standard", "--", "."])
    tracked_ignored = [x for x in out.splitlines() if x.strip()]
    if rc == 0 and tracked_ignored:
        a.add("GIT-008c", "medium", f"已入库文件被 .gitignore 命中（{len(tracked_ignored)} 个，历史遗留）",
              ["`git ls-files -i -c --exclude-standard -- .` →", *[f"    {x}" for x in tracked_ignored[:8]],
               "（只列前 8 条）" if len(tracked_ignored) > 8 else ""],
              "这些文件仍在版本控制里（.gitignore 对已跟踪文件无效）：确认不该入库则 `git rm --cached <文件>` 后提交；"
              "**先确认内容是否含密钥**（含则先轮换，见 §5）")

    # —— 9. 未合并分支与偏离度
    rc, out, _ = run_git(repo, ["branch", "--no-merged", default, "--format=%(refname:short)"])
    unmerged = [x.strip() for x in out.splitlines() if x.strip()] if rc == 0 else []
    a.facts["unmerged_branches"] = len(unmerged)
    stale_days = float(m.get("branch_stale_days", 3) or 3)
    if rc != 0:
        a.note("GIT-009", f"未合并分支检查未能执行（默认分支 `{default}` 不存在？）",
               [f"`git branch --no-merged {default}` → 非 0 / {one_line(out, 120)}"], "先解决默认分支不存在的问题（见 GIT-003a）")
    elif unmerged:
        rows, stale = [], []
        for b in unmerged:
            rc1, o1, _ = run_git(repo, ["rev-list", "--count", f"{default}..{b}"])
            rc2, o2, _ = run_git(repo, ["rev-list", "--count", f"{b}..{default}"])
            rc3, o3, _ = run_git(repo, ["log", "-1", "--format=%cI", b])
            age = None
            try:
                age = (_dt.datetime.now(_dt.timezone.utc) - _dt.datetime.fromisoformat(o3.strip())).days
            except Exception:
                pass
            ahead = o1.strip() if rc1 == 0 else "?"
            behind = o2.strip() if rc2 == 0 else "?"
            rows.append((b, ahead, behind, age))
            if age is not None and age >= stale_days:
                stale.append((b, ahead, behind, age))
        detail = [f"    {b}：领先 {a} 提交 / 落后 {bd} 提交 / 最后提交 {age if age is not None else '?'} 天前"
                  for b, a, bd, age in rows[:10]]
        body = ["`git branch --no-merged " + default + "` → " + str(len(unmerged)) + " 个", *detail,
                "（只列前 10 个）" if len(unmerged) > 10 else ""]
        if stale:
            a.add("GIT-009a", "medium", f"有 {len(stale)} 个过期未合并分支（≥{stale_days:g} 天未合回）",
                  body,
                  "一卡一分支、功能分支短期（1-3 天）合回：能合就合（走 §4 合并协议），合不了的写清阻塞原因（提 CR）；"
                  "**卡已 done 而分支未合并 = 悬空交付**，收口时按 §6 对账")
        else:
            a.note("GIT-009", f"有 {len(unmerged)} 个未合并分支（均未过期，属在飞任务）", body,
                   "收口时逐个确认：合并 / 提 CR / 说明保留理由")
    else:
        a.ok(f"GIT-009 无未合并分支（相对 `{default}`）")

    # —— 10. 提交身份
    rc, out, _ = run_git(repo, ["config", "user.name"])
    name = out.strip() if rc == 0 else ""
    rc2, out2, _ = run_git(repo, ["config", "user.email"])
    mail = out2.strip() if rc2 == 0 else ""
    a.facts["identity"] = {"name": name, "email": mail}
    if name and mail:
        a.ok(f"GIT-010 提交身份已配置：{name} <{mail}>")
    else:
        a.note("GIT-010", "提交身份未配置（`user.name`/`user.email` 为空）",
               [f"`git config user.name` → {'（空）' if not name else name}",
                f"`git config user.email` → {'（空）' if not mail else mail}"],
               "提交无法归属到人 = 出问题追不到责任方：`git config user.name/email <值>`（项目级）或 --global 配一次")


def branch_list(repo: Path) -> list[str]:
    rc, out, _ = run_git(repo, ["branch", "--format=%(refname:short)"])
    return [x.strip() for x in out.splitlines() if x.strip()] if rc == 0 else []


def worktree_dirty(repo: Path) -> list[str]:
    rc, out, _ = run_git(repo, ["status", "--porcelain"])
    return [x for x in out.splitlines() if x.strip()] if rc == 0 else []


def ahead_of_remote(repo: Path, branch: str) -> int | None:
    rc, out, _ = run_git(repo, ["rev-list", "--count", f"origin/{branch}..HEAD"])
    if rc != 0:
        return None
    try:
        return int(out.strip())
    except ValueError:
        return None


def tracked_files(repo: Path) -> list[str] | None:
    rc, out, _ = run_git_raw(repo, ["ls-files", "-z"])
    if rc != 0:
        return None
    return [x.decode("utf-8", "replace") for x in out.split(b"\x00") if x]


def big_tracked_files(repo: Path, mb: float) -> list[tuple[str, int]] | None:
    # `-- .`：把范围限制在传入路径（子目录场景与 `git ls-files` 口径一致）
    rc, out, _ = run_git_raw(repo, ["ls-tree", "-r", "-l", "-z", "HEAD", "--", "."])
    if rc != 0:
        return None
    res = []
    for rec in out.split(b"\x00"):
        if not rec:
            continue
        meta, _, path = rec.partition(b"\t")
        parts = meta.split()
        if len(parts) < 4 or parts[1] != b"blob":
            continue
        try:
            size = int(parts[3])
        except ValueError:
            continue
        if size > mb * 1024 * 1024:
            res.append((path.decode("utf-8", "replace"), size))
    res.sort(key=lambda x: -x[1])
    return res


def find_workspace(repo: Path) -> Path | None:
    """向上找真实项目工作区：含 project-state/config.json 且 project_name 非空（框架模板 project_name 为空，跳过）。"""
    cur = Path(repo).resolve()
    for _ in range(5):
        cfg = cur / "project-state" / "config.json"
        if cfg.is_file():
            try:
                if (json.loads(cfg.read_text(encoding="utf-8")).get("project_name") or "").strip():
                    return cur
            except Exception:
                pass
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


def unversioned_report(a: Audit, target: Path) -> None:
    """不是 git 仓库 → 最高优先级 finding，并给出「哪些代码根没接入」的具体清单。"""
    a.facts["is_repo"] = False
    ev = [f"`git -C {target} rev-parse --show-toplevel` → 非 0（fatal: not a git repository）"]
    items: list[str] = []

    ws = find_workspace(target)
    a.facts["workspace"] = str(ws) if ws else None
    if ws:
        cfg = json.loads((ws / "project-state" / "config.json").read_text(encoding="utf-8"))
        repos = (cfg.get("paths") or {}).get("repos") or []
        for r in ([repos] if isinstance(repos, str) else repos):
            if not isinstance(r, str):
                continue
            p = (ws / r).resolve()
            state = "已是 git 仓库" if (p / ".git").exists() else "**未接入版本控制**"
            mark = "　← 本次审计目标" if p == Path(target).resolve() else ""
            items.append(f"{r} → {state}" + ("" if p.is_dir() else "（路径不存在）") + mark)
        if items:
            ev.append("config.json 登记代码根 × 是否接入版本控制：")
            ev.extend([f"    {x}" for x in items])
    else:
        # 向下两层的 .git 探测（无 config.json 时的兜底线索）
        found = []
        for depth1 in sorted(target.iterdir()) if target.is_dir() else []:
            if not depth1.is_dir() or depth1.name in {".git", "node_modules", ".venv", "__pycache__"}:
                continue
            if (depth1 / ".git").exists():
                found.append(f"{depth1.name}（是 git 仓库）")
            else:
                for depth2 in sorted(depth1.iterdir()):
                    if depth2.is_dir() and (depth2 / ".git").exists():
                        found.append(f"{depth1.name}/{depth2.name}（是 git 仓库）")
        ev.append("向下两层探测 .git → " + ("、".join(found) if found else "未发现任何 git 仓库"))

    a.add("GIT-001", "critical", "未接入版本控制（本项目/代码根不是 git 仓库）",
          ev,
          "这是最高优先级问题：**没有版本控制 = 丢数据不可恢复、无法回滚、无法追责谁改的、无法协作合流**。"
          "按 契约/Git工作流.md §1「新项目接入（硬动作）」执行：云效建仓 → 本地 `git init` → `.gitignore` 基线 → "
          "首次提交 → `git remote add origin` → `git push -u origin <默认分支>`；**一个代码根一个仓库**，"
          "`config.json` 的 `paths.repos` 每项都要有对应仓库")


# ---------------------------------------------------------------- 输出

def sort_findings(fs: list[dict]) -> list[dict]:
    return sorted(fs, key=lambda f: (SEV_RANK.get(f["severity"], 9), f["id"]))


def render_text(a: Audit) -> str:
    fs = sort_findings(a.findings)
    cnt = {s: sum(1 for f in fs if f["severity"] == s) for s in SEV_RANK}
    fails = sum(cnt[s] for s in FAIL_SEVS)
    L: list[str] = []
    L.append(f"# Git 审计 · {Path(a.facts.get('toplevel') or a.target).name}")
    L.append("")
    L.append(f"> 时间：{a.now}　代码库：{a.facts.get('toplevel') or str(a.target)}")
    if a.facts.get("is_repo"):
        L.append(f"> 当前分支：`{a.facts.get('branch')}`　默认分支：`{a.facts.get('default_branch')}`"
                 f"（契约声明 `{a.facts.get('default_branch_declared')}`）　提交总数：{a.facts.get('commits_total')}")
    if a.facts.get("subdir"):
        L.append(f"> 检查范围：仅 `{a.facts['subdir']}`（仓库根 `{a.facts['toplevel']}`）——文件类检查按此目录为界")
    L.append(f"> 判据来源：{a.model_src}（**只报告不改动**；修复动作按 契约/Git工作流.md）")
    L.append("")
    L.append("## 一、结论")
    L.append("")
    if not a.facts.get("is_repo"):
        L.append("**未接入版本控制（GIT-001）** —— 其余版本控制类检查全部不适用。")
    else:
        L.append(f"- 总体：**{'有 ' + str(fails) + ' 项 finding' if fails else '无 finding（通过）'}**"
                 f"　严重 {cnt['critical']} · 高 {cnt['high']} · 中 {cnt['medium']} · 提示 {cnt['low']}")
        L.append(f"- 退出码：{0 if fails == 0 else 1}（0 = 无 findings；提示不影响退出码）")
    sections: list[tuple[str, list[str]]] = []
    if fs:
        sec = ["", "(按严重度排序；每条的「证据」是真实命令与输出，可复跑核对)", ""]
        for f in fs:
            sec.append("")
            sec.append(f"### [{SEV_TITLE[f['severity']]}] {f['id']} {f['title']}")
            for i, e in enumerate(f["evidence"]):
                if not e.strip():
                    continue
                if i == 0:
                    sec.append(f"- 证据：{e}")
                else:
                    sec.append(e if e.startswith("    ") else f"  {e}")
            sec.append(f"- 建议：{f['advice']}")
        sections.append(("Findings", sec))
    if a.passed:
        sections.append(("通过项", ["", *[f"- ✅ {p}" for p in a.passed]]))
    sections.append(("未覆盖（本脚本不判，别当成已核）", ["", *[f"- {x}" for x in NOT_COVERED]]))
    for idx, (title, body) in enumerate(sections, start=2):
        L.append("")
        L.append(f"## {'二三四五六七八九十'[idx - 2]}、{title}")
        L.extend(body)
    L.append("")
    L.append("> 时点纪律（对齐 v3.18.0 路径盘点）：**接管 / 批次收口 / 里程碑收口 / 用户要求** 四类时点跑本脚本，"
             "结果写 `project-state/运行日志.md`；有 finding 时按上面「建议」列回退对应角色处置。")
    return "\n".join(L)


def render_json(a: Audit) -> str:
    fs = sort_findings(a.findings)
    cnt = {s: sum(1 for f in fs if f["severity"] == s) for s in SEV_RANK}
    fails = sum(cnt[s] for s in FAIL_SEVS)
    return json.dumps({
        "generated": a.now,
        "target": str(a.target),
        "model_source": a.model_src,
        "facts": a.facts,
        "summary": {**cnt, "findings": fails, "exit_code": 0 if fails == 0 else 1},
        "findings": fs,
        "passed": a.passed,
        "not_covered": NOT_COVERED,
    }, ensure_ascii=False, indent=2)


def main() -> int:
    global GIT_BIN
    try:
        sys.stdout.reconfigure(errors="replace")  # 控制台编码不含的字符退化显示，不炸
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="Git 审计（只报告不改动）")
    ap.add_argument("repo", help="代码库路径（一个代码根一个仓库；不是仓库时直报未接入版本控制）")
    ap.add_argument("--branch-model", default=None, help="分支模型契约 md（默认找 契约/Git工作流.md，找不到用内置默认）")
    ap.add_argument("--commits", type=int, default=None, help="检查最近 N 条提交信息（默认取契约 recent_commits=20）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    GIT_BIN = find_git()
    target = Path(args.repo).expanduser()
    if not target.exists():
        print(f"[错误] 路径不存在：{target}", file=sys.stderr)
        return 2
    target = target.resolve()
    if not GIT_BIN:
        print("[错误] 未找到 git 可执行文件（请安装 git 或把它加进 PATH）", file=sys.stderr)
        return 2

    # 判据契约：显式指定 > 代码库/工作区内的 契约/Git工作流.md > 无
    mpath: Path | None = None
    if args.branch_model:
        cand = Path(args.branch_model)
        if not cand.is_absolute():
            for base in (target, Path.cwd()):
                if (base / cand).is_file():
                    cand = (base / cand)
                    break
        if not cand.is_file():
            print(f"[错误] --branch-model 指定的契约不存在：{cand}", file=sys.stderr)
            return 2
        mpath = cand
    else:
        for cand in (target / "project-state" / "契约" / "Git工作流.md",
                     target / "契约" / "Git工作流.md",
                     Path.cwd() / "project-state" / "契约" / "Git工作流.md"):
            if cand.is_file():
                mpath = cand
                break
    model, model_src = load_model(mpath)

    a = Audit(target, model, model_src, commits_n=args.commits or int(model.get("recent_commits", 20)))
    a.now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    rc, out, _ = run_git(target, ["rev-parse", "--show-toplevel"])
    if rc != 0:
        unversioned_report(a, target)
    else:
        a.facts["toplevel"] = out.strip()
        # 用 target（可能是仓库子目录）跑检查：文件类检查按 cwd 前缀限定范围，与"一个代码根一个仓库"的口径一致
        audit_repo(a, target)
    a.findings = sort_findings(a.findings)

    print(render_json(a) if args.json else render_text(a))
    fails = sum(1 for f in a.findings if f["severity"] in FAIL_SEVS)
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

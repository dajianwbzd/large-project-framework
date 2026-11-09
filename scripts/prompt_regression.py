#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v3.22.0 提示词回归（prompt_regression）：结构断言（离线可跑）+ 快照结构对比 + 可选联网抽检。

配套契约：`契约/Prompt规范.md` §4「回归与评测（硬门禁）」；复查判据见 `契约/代码复查规则库.md` 域 9。

设计原则（与框架纪律对齐）：
- **零依赖（纯标准库）**：不引第三方包，Python 3.8+ 可直接跑（零依赖兜底纪律）。
- **离线可跑是硬要求**：`--adapter offline` 不联网、不需要 key——断言对象是用例里记录的模型输出
  （`output` 内联文本 / `output_file` 文件），用于「结构断言」与「快照结构对比」。
- **跳过 ≠ 通过**：`openai` 适配器**没有 key 时逐条记 SKIP 并返回码 0**，绝不假装通过；
  语义断言（准确度/语气/命名是否合规）本脚本**不判定**（需抽样人评或模型评），一律记 SKIP。
- **对比结构而非文本**：快照存的是**结构指纹**（键集/类型/枚聚集/长度/行式字段序），
  动态内容（编号、文件名、自由文本）经归一化后只进「骨架」维度，默认仅提示不判失败
  （`--strict-skeleton` 可升级为失败）。

退出码：0 = 无失败（含全跳过）　1 = 有失败　2 = 用法错（参数/评测集不可读/格式不合法）

用法：
    python prompt_regression.py --cases 代码/后端/prompts/eval/scan_classify.json --adapter offline
    python prompt_regression.py --cases <集> --adapter offline --update-snapshot   # 建立/刷新基线
    python prompt_regression.py --cases <集> --adapter openai --model <model> --base-url <url>
    python prompt_regression.py --cases <集> --adapter offline --json              # 机器可读报告

评测集格式（手写友好；顶层可为数组，也可为 {prompt_id, version, cases:[...]}）：
    [
      {
        "id": "scan-001",
        "tags": ["normal"],
        "input_vars": {"text": "按空间命名", "files": "1. a.png"},
        "output": "IMG 1 || NAME 客厅.png || CAT 场景图 || ...",   # 离线断言对象（或 output_file）
        "output_file": "../证据/scan-001.txt",                     # 与 output 二选一（相对评测集目录）
        "messages": {"system": "...", "user": "..."},              # 联网抽检用（可含 {{var}} 占位符）
        "assertions": [
          {"type": "line_records", "sep": "||", "keys": ["IMG","NAME","CAT","TAGS","DESC","USE"], "count": 2},
          {"type": "contains", "value": "客厅"},
          {"type": "max_chars", "value": 4000},
          {"type": "snapshot_match"},
          {"type": "semantic", "note": "命名是否符合空间命名规范"}
        ]
      }
    ]

断言类型（type → 参数）：
    json_valid                                  输出是合法 JSON
    json_required   {paths:[...]}               必需字段（点分路径，支持 items[].field 通配）
    json_types      {path: "string|integer|number|boolean|array|object"}
    json_enum       {path: [值白名单]}
    json_len        {path: 整数 | [min,max]}
    line_records    {sep, keys, count}          行式字段契约（字段顺序 + 分隔符 + 条数）
    regex           {pattern, min_count}        正则命中次数
    contains / not_contains {value}             包含 / 不包含
    max_chars / min_chars / max_lines {value}   长度预算
    vars_isolated   {open, close, vars:[...]}   **注入防护**：用户变量值的每个出现点都必须在分隔块内
    snapshot_match                              与上次快照的结构指纹一致
    semantic        {note}                      语义断言（本脚本不判定 → SKIP）
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2
SKIP_AUTHORS = "语义断言：需抽样人评或模型评（本脚本不判定，见 契约/Prompt规范.md §4.2）"

# 语义断言类型名（一律 SKIP）
SEMANTIC_TYPES = {"semantic", "model_judge", "human_review"}

# 环境变量 key 解析链（前者优先；--key-env 可指定）
DEFAULT_KEY_ENVS = ("PROMPT_EVAL_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY")


# ---------------------------------------------------------------- 基础工具

def _utf8_stdout() -> None:
    """Windows 控制台/重定向下也能安全打印中文（零依赖，失败不阻断）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass


def _mask(text: str, keep: int = 4) -> str:
    """敏感串掩码（日志/报告里不落 key 原文）。"""
    text = str(text or "")
    if len(text) <= keep:
        return "***"
    return text[:keep] + "***"


def _now() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _types_of(value) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if value is None:
        return "null"
    return type(value).__name__


def _type_matches(value, expect: str) -> bool:
    expect = str(expect).lower()
    if expect == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return _types_of(value) == expect


_MISSING = object()


def _resolve_path(data, path: str) -> tuple[list, str | None]:
    """点分路径取值，支持 `items[].field` 通配；返回 (值列表, 错误说明)。"""
    tokens: list[str] = []
    for part in str(path).split("."):
        m = re.fullmatch(r"([^\[\]]*)((\[\])*)", part)
        if not m:
            return [], f"路径片段非法：{part}"
        name, wild = m.group(1), m.group(2)
        if name:
            tokens.append(name)
        for _ in wild:
            tokens.append("[]")

    def walk(node, idx: int) -> list:
        if idx == len(tokens):
            return [node]
        tok = tokens[idx]
        if tok == "[]":
            if not isinstance(node, list):
                return []
            out: list = []
            for item in node:
                out.extend(walk(item, idx + 1))
            return out
        if isinstance(node, dict):
            if tok not in node:
                return []
            return walk(node[tok], idx + 1)
        return []

    values = walk(data, 0)
    if not values:
        return [], f"路径不存在或为空：{path}"
    return values, None


# ---------------------------------------------------------------- 快照结构指纹

def _mask_scalar(value) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "#"
    if value is None:
        return "null"
    text = str(value)
    text = re.sub(r"\d+", "#", text)
    text = re.sub(r"[A-Za-z]+", "w", text)
    return text[:24]


def _json_shape(node) -> str:
    if isinstance(node, dict):
        inner = ", ".join(f"{k}: {_json_shape(v)}" for k, v in list(node.items())[:12])
        return "{" + inner + ("…" if len(node) > 12 else "") + "}"
    if isinstance(node, list):
        if not node:
            return "[]"
        return "[" + _json_shape(node[0]) + "…]" if len(node) > 1 else "[" + _json_shape(node[0]) + "]"
    return _mask_scalar(node)


def _collect_json_dims(node, prefix: str, paths: dict, lens: dict, text_lens: dict) -> None:
    """结构维度收集：paths=键集+类型；lens=**集合长度**（结构，漂移即失败）；
    text_lens=字符串长度（内容，漂移仅提示——自由文本长度本就会变）。"""
    if isinstance(node, dict):
        for k, v in node.items():
            _collect_json_dims(v, f"{prefix}.{k}" if prefix else str(k), paths, lens, text_lens)
    elif isinstance(node, list):
        path = f"{prefix}[]" if prefix else "[]"
        lens[path] = len(node)
        for item in node:
            _collect_json_dims(item, path, paths, lens, text_lens)
    else:
        paths.setdefault(prefix, set()).add(_types_of(node))
        if isinstance(node, str):
            text_lens[prefix] = len(node)


def _fingerprint(output: str, source: str, prompt_id: str, version: str) -> dict:
    """输出 → 结构指纹（键集/类型/枚聚集/长度/行式字段序；不含原文）。"""
    fp: dict = {
        "prompt_id": prompt_id, "version": version, "updated": _now(),
        "source": source, "chars": len(output), "lines": output.count("\n") + 1,
    }
    text = output.strip()
    data = None
    parsed_ok = False
    try:
        data = json.loads(text)
        parsed_ok = True
    except (ValueError, TypeError):
        data = None
    if parsed_ok:
        paths: dict = {}
        lens: dict = {}
        text_lens: dict = {}
        _collect_json_dims(data, "", paths, lens, text_lens)
        scalars: dict = {}
        _collect_scalar_sets(data, "", scalars)
        fp["kind"] = "json"
        fp["json"] = {
            "paths": sorted(paths.keys()),
            "types": {k: sorted(v) for k, v in sorted(paths.items())},
            "lens": {k: lens[k] for k in sorted(lens)},
            "text_lens": {k: text_lens[k] for k in sorted(text_lens)},
            "scalar_sets": {k: sorted(v) for k, v in sorted(scalars.items())},
        }
        fp["skeleton"] = _json_shape(data)[:600]
        return fp

    # 行式字段契约（如 `IMG 1 || NAME x || CAT y`）
    recs = _split_line_records(output)
    if recs:
        fp["kind"] = "line_records"
        fp["line"] = {
            "count": len(recs),
            "keys": recs[0][0],
            "key_sets_consistent": all(r[0] == recs[0][0] for r in recs),
        }
        fp["skeleton"] = " || ".join(f"{k} {_mask_scalar(v)}" for k, v in recs[0][1])[:600]
        return fp

    fp["kind"] = "text"
    body = [ln for ln in text.splitlines() if ln.strip()]
    fp["text"] = {"non_empty_lines": len(body)}
    fp["skeleton"] = " / ".join(_mask_scalar(ln)[:40] for ln in body[:20])[:600]
    return fp


def _collect_scalar_sets(node, prefix: str, out: dict, limit: int = 20) -> None:
    """短标量集合（枚举性字段漂移可查）；值过长/过多则忽略（避免把自由文本当结构）。"""
    if isinstance(node, dict):
        for k, v in node.items():
            _collect_scalar_sets(v, f"{prefix}.{k}" if prefix else str(k), out, limit)
    elif isinstance(node, list):
        for item in node:
            _collect_scalar_sets(item, f"{prefix}[]" if prefix else "[]", out, limit)
    elif isinstance(node, (str, int, float, bool)) or node is None:
        if isinstance(node, str) and len(node) > 24:
            return
        bucket = out.setdefault(prefix, set())
        if len(bucket) < limit:
            bucket.add(str(node))


def _diff_fingerprint(old: dict, new: dict) -> tuple[list[str], list[str]]:
    """结构指纹对比 → (失败项, 提示项)。失败=结构漂移；提示=骨架/规模变化。"""
    fails: list[str] = []
    warns: list[str] = []
    if old.get("kind") != new.get("kind"):
        fails.append(f"输出形态变化：{old.get('kind')} → {new.get('kind')}")
        return fails, warns
    if old.get("kind") == "json":
        o, n = old.get("json", {}), new.get("json", {})
        o_paths, n_paths = set(o.get("paths", [])), set(n.get("paths", []))
        for p in sorted(o_paths - n_paths):
            fails.append(f"字段消失：{p}")
        for p in sorted(n_paths - o_paths):
            fails.append(f"字段新增：{p}")
        for p in sorted(o_paths & n_paths):
            ot, nt = o.get("types", {}).get(p), n.get("types", {}).get(p)
            if ot != nt:
                fails.append(f"字段类型变化：{p} {ot} → {nt}")
            ol, nl = o.get("lens", {}).get(p), n.get("lens", {}).get(p)
            if ol != nl:
                fails.append(f"集合长度变化：{p} {ol} → {nl}")
            tl_o, tl_n = o.get("text_lens", {}).get(p), n.get("text_lens", {}).get(p)
            if tl_o is not None and tl_n is not None and tl_o != tl_n:
                warns.append(f"文本长度变化：{p} {tl_o} → {tl_n}")
            os_, ns_ = o.get("scalar_sets", {}).get(p), n.get("scalar_sets", {}).get(p)
            if os_ is not None and ns_ is not None and os_ != ns_:
                gone = [v for v in os_ if v not in ns_][:5]
                add = [v for v in ns_ if v not in os_][:5]
                fails.append(f"枚聚集变化：{p} 少了{gone} 多了{add}")
    elif old.get("kind") == "line_records":
        o, n = old.get("line", {}), new.get("line", {})
        if o.get("keys") != n.get("keys"):
            fails.append(f"行式字段序变化：{o.get('keys')} → {n.get('keys')}")
        if o.get("count") != n.get("count"):
            fails.append(f"结果条数变化：{o.get('count')} → {n.get('count')}")
    elif old.get("kind") == "text":
        o, n = old.get("text", {}), new.get("text", {})
        if o.get("non_empty_lines") != n.get("non_empty_lines"):
            warns.append(f"非空行数变化：{o.get('non_empty_lines')} → {n.get('non_empty_lines')}")
    if old.get("skeleton") != new.get("skeleton"):
        warns.append("骨架变化（归一化后仍不一致，可能是内容分布漂移）")
    if old.get("chars") != new.get("chars") or old.get("lines") != new.get("lines"):
        warns.append(f"规模变化：{old.get('chars')}字符/{old.get('lines')}行 → "
                     f"{new.get('chars')}字符/{new.get('lines')}行")
    return fails, warns


# ---------------------------------------------------------------- 断言引擎

def _split_line_records(output: str) -> list[tuple[list[str], list[tuple[str, str]]]]:
    """`KEY 值 || KEY 值` 行 → [(键序列, [(键, 值)...]), ...]；非该形态的行忽略。"""
    recs: list[tuple[list[str], list[tuple[str, str]]]] = []
    for raw in output.splitlines():
        line = raw.strip()
        if not line or "||" not in line:
            continue
        parts = [p.strip() for p in line.split("||")]
        pairs: list[tuple[str, str]] = []
        ok = True
        for part in parts:
            m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*[=：:]?\s*(.*)$", part)
            if not m:
                ok = False
                break
            pairs.append((m.group(1).upper(), m.group(2).strip()))
        if ok and len(pairs) >= 2:
            recs.append(([k for k, _ in pairs], pairs))
    return recs


def run_assertion(spec: dict, ctx: dict) -> tuple[str, str]:
    """单条断言 → (状态 PASS/FAIL/SKIP, 说明)。ctx: {output, case, adapter}。"""
    kind = str(spec.get("type") or spec.get("kind") or "").strip().lower()
    if kind in SEMANTIC_TYPES:
        return "SKIP", spec.get("note") or SKIP_AUTHORS
    if not kind:
        return "FAIL", "断言缺少 type"
    output = ctx["output"]
    if output is None and kind != "vars_isolated":
        return "SKIP", "用例未提供输出（output/output_file 缺失，联网抽检需 --adapter openai）"
    text = output or ""

    try:
        if kind == "json_valid":
            try:
                json.loads(text.strip())
            except (ValueError, TypeError) as exc:
                return "FAIL", f"输出不是合法 JSON：{exc}"
            return "PASS", "输出是合法 JSON"

        if kind == "json_required":
            data = json.loads(text)
            bad = []
            for path in spec.get("paths", []):
                values, err = _resolve_path(data, path)
                if err:
                    bad.append(f"{path}（{err}）")
            return ("FAIL", "必需字段缺失：" + "、".join(bad)) if bad else ("PASS", "必需字段齐全")

        if kind in {"json_types", "json_enum", "json_len"}:
            data = json.loads(text)
            checks = spec.get("paths") or spec.get("fields")
            if not isinstance(checks, dict) or not checks:
                if spec.get("path"):
                    checks = {str(spec["path"]): spec.get("value")}
                else:
                    return "FAIL", f"{kind} 参数需为 {{路径: 期望}} 形式（paths 或 path+value）"
            bad = []
            for path, expect in checks.items():
                values, err = _resolve_path(data, path)
                if err:
                    bad.append(f"{path}（{err}）")
                    continue
                if kind == "json_types":
                    wrong = [v for v in values if not _type_matches(v, expect)]
                    if wrong:
                        bad.append(f"{path} 期望 {expect}，实得 {_types_of(wrong[0])}")
                elif kind == "json_enum":
                    wrong = [v for v in values if v not in expect]
                    if wrong:
                        bad.append(f"{path} 出现白名单外取值：{wrong[:3]}")
                else:
                    lo, hi = (expect if isinstance(expect, (list, tuple)) else (None, expect))
                    for v in values:
                        if not isinstance(v, (str, list, dict)):
                            bad.append(f"{path} 非可计长度类型（{_types_of(v)}）")
                            break
                        n = len(v)
                        if (lo is not None and n < lo) or (hi is not None and n > hi):
                            bad.append(f"{path} 长度 {n} 超出 {expect}")
                            break
            return ("FAIL", f"{kind}：" + "；".join(bad)) if bad else ("PASS", f"{kind} 通过")

        if kind == "line_records":
            recs = _split_line_records(text)
            if not recs:
                return "FAIL", "未解析到任何 `KEY 值 || KEY 值` 记录行"
            keys = [str(k).upper() for k in (spec.get("keys") or recs[0][0])]
            bad = []
            got = recs[0][0]
            if got != keys:
                bad.append(f"字段序不符：期望 {keys}，实得 {got}")
            count = spec.get("count")
            if isinstance(count, int) and len(recs) != count:
                bad.append(f"条数不符：期望 {count}，实得 {len(recs)}")
            empty_fields = [k for k, v in recs[0][1] if not v]
            if empty_fields:
                bad.append(f"字段为空：{empty_fields}")
            return ("FAIL", "；".join(bad)) if bad else ("PASS", f"行式契约通过（{len(recs)} 条）")

        if kind == "regex":
            pattern = spec.get("pattern") or ""
            hits = len(re.findall(pattern, text, re.M))
            need = int(spec.get("min_count", 1))
            if hits < need:
                return "FAIL", f"正则命中 {hits} 次 < 期望 {need} 次（/{pattern}/）"
            return "PASS", f"正则命中 {hits} 次"

        if kind in {"contains", "not_contains"}:
            value = str(spec.get("value", ""))
            hit = value in text
            if kind == "contains":
                return ("PASS", f"包含 {value!r}") if hit else ("FAIL", f"缺少必需内容 {value!r}")
            return ("FAIL", f"出现禁止内容 {value!r}") if hit else ("PASS", f"未出现禁止内容 {value!r}")

        if kind in {"max_chars", "min_chars", "max_lines"}:
            n = len(text) if kind != "max_lines" else text.count("\n") + 1
            limit = int(spec.get("value", 0))
            ok = n <= limit if kind in {"max_chars", "max_lines"} else n >= limit
            return ("PASS", f"{kind}={limit}，实得 {n}") if ok else \
                ("FAIL", f"{kind} 不满足：期望{'≤' if kind != 'min_chars' else '≥'}{limit}，实得 {n}")

        if kind == "vars_isolated":
            prompt = ctx.get("prompt")
            if not prompt:
                return "SKIP", "用例未提供 messages/prompt（无法做注入结构检查）"
            open_m, close_m = str(spec.get("open", "<user_input>")), str(spec.get("close", "</user_input>"))
            blocks = re.findall(re.escape(open_m) + r"(.*?)" + re.escape(close_m), prompt, re.S)
            covered = "\n".join(blocks)
            bad = []
            for var in spec.get("vars", []):
                raw = str((ctx["case"].get("input_vars") or {}).get(var, ""))
                if not raw:
                    continue
                if raw not in prompt:
                    bad.append(f"{var} 未出现在提示词中")
                    continue
                if raw not in covered:
                    bad.append(f"{var} 的值出现在分隔块之外（可被当作指令执行）")
            return ("FAIL", "注入防护不达标：" + "；".join(bad)) if bad else \
                ("PASS", f"{len(spec.get('vars', []))} 个变量均在分隔块内")

        if kind == "snapshot_match":
            return ctx["snapshot_compare"]()

        return "FAIL", f"未知断言类型：{kind}"
    except json.JSONDecodeError as exc:
        return "FAIL", f"输出不是合法 JSON：{exc}"
    except (TypeError, KeyError, ValueError) as exc:
        return "FAIL", f"断言执行异常：{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------- 用例装载

def load_cases(path: Path) -> tuple[list[dict], str, str]:
    """评测集 → (用例列表, prompt_id, version)；格式非法抛 ValueError。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"评测集不可读或非合法 JSON：{path}（{exc}）") from exc
    if isinstance(data, list):
        cases, prompt_id, version = data, "", ""
    elif isinstance(data, dict):
        cases = data.get("cases")
        prompt_id = str(data.get("prompt_id") or "")
        version = str(data.get("version") or "")
    else:
        raise ValueError("评测集顶层必须是数组或含 cases 的对象")
    if not isinstance(cases, list) or not cases:
        raise ValueError("评测集 cases 为空")
    for i, case in enumerate(cases, 1):
        if not isinstance(case, dict):
            raise ValueError(f"第 {i} 条用例不是对象")
        if not case.get("id"):
            raise ValueError(f"第 {i} 条用例缺少 id")
        if not isinstance(case.get("assertions"), list):
            raise ValueError(f"用例 {case['id']} 缺少 assertions 数组")
    return cases, prompt_id, version


def case_output(case: dict, case_dir: Path) -> tuple[str | None, str]:
    """取离线断言对象：内联 output > output_file；都没有 → (None, "无")。"""
    if isinstance(case.get("output"), str):
        return case["output"], "inline"
    rel = case.get("output_file")
    if rel:
        p = Path(rel)
        p = p if p.is_absolute() else (case_dir / p)
        try:
            return p.read_text(encoding="utf-8"), f"file:{p.name}"
        except OSError as exc:
            return None, f"output_file 不可读（{exc}）"
    return None, "none"


def fill_template(text: str, vars_map: dict) -> str:
    out = str(text)
    for k, v in (vars_map or {}).items():
        out = out.replace("{{" + str(k) + "}}", str(v))
    return out


def build_messages(case: dict) -> list[dict] | None:
    """用例 → messages（支持 {system,user} 或数组；模板变量用 input_vars 填充）。"""
    vars_map = case.get("input_vars") or {}
    msgs = case.get("messages")
    if isinstance(msgs, list) and msgs:
        return [{"role": m.get("role", "user"), "content": fill_template(m.get("content", ""), vars_map)}
                for m in msgs]
    if isinstance(msgs, dict):
        out = []
        if msgs.get("system"):
            out.append({"role": "system", "content": fill_template(msgs["system"], vars_map)})
        if msgs.get("user"):
            out.append({"role": "user", "content": fill_template(msgs["user"], vars_map)})
        if out:
            return out
    prompt = case.get("prompt") or case.get("prompt_template")
    if prompt:
        return [{"role": "user", "content": fill_template(prompt, vars_map)}]
    return None


# ---------------------------------------------------------------- OpenAI 兼容适配器

def _resolve_key(key_env: str | None) -> tuple[str, str]:
    """→ (环境变量名, key)；都没配 → ("<名>", "")。"""
    names = (key_env,) if key_env else DEFAULT_KEY_ENVS
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return name, value
    return (key_env or DEFAULT_KEY_ENVS[0]), ""


def call_openai(messages: list[dict], *, base_url: str, model: str, key: str, temperature: float | None,
                max_tokens: int | None, timeout: float, response_format: dict | None) -> tuple[str, dict]:
    """OpenAI 兼容 /chat/completions（纯标准库）；→ (content, usage)。"""
    body: dict = {"model": model, "messages": messages}
    if temperature is not None:
        body["temperature"] = temperature
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    if response_format:
        body["response_format"] = response_format
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8", "replace"))
    content = payload["choices"][0]["message"]["content"]
    if not isinstance(content, str) or not content.strip():
        raise ValueError("返回 content 为空（推理模型未关 thinking 时常见，见 契约/Prompt规范.md §5）")
    return content, payload.get("usage") or {}


# ---------------------------------------------------------------- 主流程

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="prompt_regression.py",
        description="提示词回归：离线结构断言 + 快照结构对比（零依赖，Python 3.8+）。",
        add_help=True,
    )
    parser.add_argument("--cases", required=True, help="评测集 JSON 路径")
    parser.add_argument("--adapter", choices=("offline", "openai"), default="offline",
                        help="offline=离线结构断言（默认）；openai=OpenAI 兼容接口抽检")
    parser.add_argument("--json", action="store_true", help="只输出机器可读 JSON 报告")
    parser.add_argument("--snapshot-dir", default="", help="快照目录（默认 <评测集目录>/snapshots）")
    parser.add_argument("--update-snapshot", action="store_true", help="用本次输出重建/刷新快照基线")
    parser.add_argument("--require-snapshot", action="store_true", help="快照缺失时判失败（CI/门禁用）")
    parser.add_argument("--strict-skeleton", action="store_true", help="骨架提示项升级为失败")
    parser.add_argument("--only", default="", help="只跑指定用例 id（逗号分隔）")
    parser.add_argument("-v", "--verbose", action="store_true", help="逐条断言都打印（含通过项，取证用）")
    parser.add_argument("--key-env", default="", help=f"API key 环境变量名（默认依序 {DEFAULT_KEY_ENVS}）")
    parser.add_argument("--base-url", default=os.environ.get("PROMPT_EVAL_BASE_URL", "https://api.deepseek.com/v1"),
                        help="OpenAI 兼容 base_url")
    parser.add_argument("--model", default=os.environ.get("PROMPT_EVAL_MODEL", ""), help="模型名（联网抽检必填）")
    parser.add_argument("--temperature", type=float, default=None)
    parser.add_argument("--max-tokens", type=int, default=None)
    parser.add_argument("--timeout", type=float, default=120.0, help="单次请求超时秒数（默认 120）")
    args = parser.parse_args(argv)

    cases_path = Path(args.cases).expanduser()
    if not cases_path.is_file():
        print(f"[用法错] 评测集不存在：{cases_path}", file=sys.stderr)
        return EXIT_USAGE
    try:
        cases, prompt_id, version = load_cases(cases_path)
    except ValueError as exc:
        print(f"[用法错] {exc}", file=sys.stderr)
        return EXIT_USAGE

    only = {s.strip() for s in args.only.split(",") if s.strip()}
    if only:
        cases = [c for c in cases if str(c.get("id")) in only]
        if not cases:
            print(f"[用法错] --only 未命中任何用例：{args.only}", file=sys.stderr)
            return EXIT_USAGE

    case_dir = cases_path.parent
    snap_dir = Path(args.snapshot_dir).expanduser() if args.snapshot_dir else (case_dir / "snapshots")

    key_env, key = ("", "")
    if args.adapter == "openai":
        key_env, key = _resolve_key(args.key_env or None)
        if not args.model:
            print("[用法错] --adapter openai 需要 --model（或设 PROMPT_EVAL_MODEL）", file=sys.stderr)
            return EXIT_USAGE
        if not key:
            # 明确跳过：不联网、不假装通过、退出码 0（契约/Prompt规范.md §4.2「跳过 ≠ 通过」）
            report = {
                "cases_file": str(cases_path), "adapter": args.adapter, "prompt_id": prompt_id,
                "version": version, "skipped_all": True, "skip_reason": f"未配置 API key（环境变量 {key_env}）",
                "totals": {"cases": len(cases), "passed": 0, "failed": 0, "skipped": len(cases),
                           "assertions": {"passed": 0, "failed": 0, "skipped": 0}, "tokens": {}},
                "results": [{"id": c["id"], "status": "SKIP", "source": "openai(no-key)",
                             "tags": c.get("tags", []),
                             "assertions": [{"type": a.get("type"), "status": "SKIP",
                                             "detail": f"未配置 API key（{key_env}），联网抽检整体跳过"}
                                            for a in c.get("assertions", [])]} for c in cases],
                "exit_code": EXIT_OK,
            }
            if args.json:
                print(json.dumps(report, ensure_ascii=False, indent=2))
            else:
                print(f"== prompt_regression ==  评测集：{cases_path}")
                print(f"[SKIP] 未配置 API key（环境变量 {key_env}）——联网抽检整体跳过，"
                      f"共 {len(cases)} 条用例未执行。")
                print("       离线结构断言不受影响：--adapter offline")
                print(f"汇总：用例 {len(cases)}　通过 0　失败 0　跳过 {len(cases)}")
                print("退出码：0（无失败；跳过 ≠ 通过，门禁前请补跑或写明豁免依据）")
            return EXIT_OK

    results: list[dict] = []
    tot = {"cases": 0, "passed": 0, "failed": 0, "skipped": 0}
    atot = {"passed": 0, "failed": 0, "skipped": 0}
    utot = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    started = time.perf_counter()

    for case in cases:
        cid = str(case["id"])
        output, source = case_output(case, case_dir)
        usage: dict = {}
        call_error = ""

        if args.adapter == "openai":
            messages = build_messages(case)
            if not messages:
                output, source, call_error = None, "openai", "用例无 messages/prompt，无法联网抽检"
            else:
                try:
                    req_fmt = case.get("response_format")
                    output, usage = call_openai(
                        messages, base_url=args.base_url, model=args.model, key=key,
                        temperature=args.temperature if args.temperature is not None
                        else case.get("temperature"),
                        max_tokens=args.max_tokens if args.max_tokens is not None else case.get("max_tokens"),
                        timeout=args.timeout, response_format=req_fmt)
                    source = f"openai:{args.model}"
                    for k in utot:
                        utot[k] += int((usage or {}).get(k) or 0)
                except urllib.error.HTTPError as exc:
                    body = ""
                    try:
                        body = exc.read().decode("utf-8", "replace")[:200]
                    except OSError:
                        pass
                    output, call_error = None, f"HTTP {exc.code}：{body}（key={_mask(key)}）"
                except (urllib.error.URLError, TimeoutError, OSError) as exc:
                    output, call_error = None, f"网络/超时失败：{exc}"
                except (ValueError, KeyError, IndexError, TypeError) as exc:
                    output, call_error = None, f"响应不可用：{type(exc).__name__}: {exc}"

        # 快照
        snap_path = snap_dir / f"{cid}.json"
        old_fp = None
        if snap_path.is_file():
            try:
                old_fp = json.loads(snap_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                old_fp = None
        new_fp = _fingerprint(output, source, prompt_id, version) if output is not None else None
        refreshed = False
        if args.update_snapshot and new_fp is not None and not call_error:
            # 先落基线再断言：本次即为"新基线"，不再对旧基线判漂移（刷新动作本身要留痕）
            snap_dir.mkdir(parents=True, exist_ok=True)
            snap_path.write_text(json.dumps(new_fp, ensure_ascii=False, indent=2), encoding="utf-8")
            old_fp, refreshed = new_fp, True

        def snapshot_compare() -> tuple[str, str]:
            if refreshed:
                return "PASS", f"已刷新快照基线（{snap_path.name}，本次不判漂移）"
            if new_fp is None:
                return "SKIP", "本次无输出，无法与快照对比"
            if old_fp is None:
                return ("SKIP", f"快照缺失（{snap_path.name}）——加 --update-snapshot 建立基线") \
                    if not args.require_snapshot else \
                    ("FAIL", f"快照缺失（{snap_path.name}）且 --require-snapshot 已开启")
            fails, warns = _diff_fingerprint(old_fp, new_fp)
            if fails or (warns and args.strict_skeleton):
                return "FAIL", "结构漂移：" + "；".join(fails + (warns if args.strict_skeleton else []))
            return "PASS", ("结构未漂移" + ("；" + "；".join(warns) if warns else ""))

        ctx = {
            "output": None if call_error else output,
            "case": case,
            "adapter": args.adapter,
            "prompt": (build_messages(case) or [{}])[-1].get("content", "") if case.get("messages") or case.get("prompt") else None,
            "snapshot_compare": snapshot_compare,
        }

        rows: list[dict] = []
        if call_error:
            rows.append({"type": "call", "status": "FAIL", "detail": call_error})
        for spec in case.get("assertions", []):
            status, detail = run_assertion(spec, ctx)
            rows.append({"type": str(spec.get("type") or spec.get("kind") or "?"), "status": status,
                         "detail": detail})
        status = "FAIL" if any(r["status"] == "FAIL" for r in rows) else \
            ("SKIP" if all(r["status"] == "SKIP" for r in rows) else "PASS")
        results.append({"id": cid, "status": status, "source": source, "tags": case.get("tags", []),
                        "assertions": rows, "usage": usage})
        tot["cases"] += 1
        tot[{"PASS": "passed", "FAIL": "failed", "SKIP": "skipped"}[status]] += 1
        for r in rows:
            if r["status"] == "PASS":
                atot["passed"] += 1
            elif r["status"] == "FAIL":
                atot["failed"] += 1
            else:
                atot["skipped"] += 1

    exit_code = EXIT_FAIL if tot["failed"] else EXIT_OK
    report = {
        "cases_file": str(cases_path), "adapter": args.adapter, "prompt_id": prompt_id,
        "version": version, "elapsed_s": round(time.perf_counter() - started, 2),
        "totals": {**tot, "assertions": atot, "tokens": utot}, "results": results, "exit_code": exit_code,
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return exit_code

    print(f"== prompt_regression ==  评测集：{cases_path}")
    print(f"   适配器：{args.adapter}　用例：{tot['cases']}　快照目录：{snap_dir}"
          f"　{'（本次已刷新快照）' if args.update_snapshot else ''}")
    for r in results:
        mark = {"PASS": "PASS", "FAIL": "FAIL", "SKIP": "SKIP"}[r["status"]]
        tag = f" tags[{','.join(r['tags'])}]" if r.get("tags") else ""
        n_ok = sum(1 for a in r["assertions"] if a["status"] == "PASS")
        print(f"[{mark}] {r['id']}{tag} → {n_ok}/{len(r['assertions'])} 断言通过（来源 {r['source']}）")
        for a in r["assertions"]:
            if a["status"] != "PASS" or args.verbose:
                print(f"        - {a['status']} {a['type']}：{a['detail']}")
    if args.adapter == "openai":
        print(f"token 用量：{utot}")
    print(f"汇总：用例 {tot['cases']}（通过 {tot['passed']} / 失败 {tot['failed']} / 跳过 {tot['skipped']}）"
          f"　断言 {sum(atot.values())}（通过 {atot['passed']} / 失败 {atot['failed']} / 跳过 {atot['skipped']}）")
    if atot["skipped"]:
        print("提示：跳过 ≠ 通过——语义断言需抽样人评/模型评；快照缺失请 --update-snapshot 建立基线。")
    print(f"退出码：{exit_code}（0 无失败 / 1 有失败 / 2 用法错）")
    return exit_code


if __name__ == "__main__":
    _utf8_stdout()
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[中断] 用户取消", file=sys.stderr)
        sys.exit(EXIT_USAGE)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""v5.5.0 代码风格盘点（style_audit）：扫项目代码，报告命名风格漂移与契约不符。

设计原则（同 `path_audit.py`）：
- **只报告，不改动**——改名属结构变更，由主控裁决后执行（agent.md「统一路径管理」同类纪律）。
- **判据来自契约，不来自脚本内定**：读 `project-state/契约/代码风格.md` §0.2 的 JSON 定档块；
  **契约缺失/无定档块时退回框架默认风格，并在报告首行标注"判据来源=框架默认"**——不假装是项目契约。
- **契约里声明的判据必须真的生效**（v5.5.0 补）：`positions`（按位置判 casing）/`conversion`（转换点白名单）/
  `synonyms`（禁用同义词）/`infra`（基础设施文件命名）此前"契约写了、脚本不读"，现全部接上。
- **例外登记生效 + 行内豁免**：读契约 §4 例外登记表，登记项不再作为违规报告；**到期已过的例外重新报告为命中**；
  单行误报可用 `style-audit: off[: rule]` … `style-audit: on` 就地豁免（借 dotenv-linter 的 on/off）。
- **未覆盖项明说**（见报告最后一节）——不假装覆盖了做不到的检查。

v5.5.0 覆盖范围（2026-09-19 代码风格打磨）：python / javascript(含 .vue) / sql / **gdscript(.gd)** /
**c(.c/.h)** / shell(.sh/.bat) / powershell(.ps1) / **style(.css/.scss/.less)** /
**基础设施文件**（Dockerfile、docker-compose\*.yml、CI yml、.env\*），并跳过**非源码树**（证据/快照/备份）
且如实列出。

用法：
    python style_audit.py <项目根>                                  # 人类可读报告
    python style_audit.py <项目根> --json                           # 机器可读（可入 CI）
    python style_audit.py <项目根> --scope repos/backend            # 只扫一个范围（可多次）
    python style_audit.py <项目根> --since origin/master            # **只报本次新增/改动**（本卡新增口径）
    python style_audit.py <项目根> --style-file project-state/契约/代码风格.md
    python style_audit.py <项目根> --max-file-kb 2048               # 超大文件跳过阈值（默认 2048KB）

退出码：0 = 无命中；1 = 有命中；2 = 用法错（根不存在 / 显式指定的契约文件不存在）。
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

# ---------------------------------------------------------------------------
# 0. 框架默认风格（= 契约模板 §0.2 的同一份；与 契约/api设计规范.md §1「JSON 用 camelCase」一致）
#    注意：这是**兜底判据**。项目一旦在 代码风格.md 定档，以项目定档为准。
#    v5.5.0（2026-09-19 代码风格打磨）新增三块，都是"契约声明了、脚本原先从不读"的判据：
#      `positions` —— 按**位置**判 casing（JSON 字段/环境变量/错误码/配置键/事件名/缓存 key/提示词 id/路由参数）
#      `infra`     —— 基础设施文件（Dockerfile/compose/CI/shell）的命名定档
#      `conversion.modules|files` 与 `synonyms` —— 从"填了不算数"改为**真判据**（此前脚本改去 grep
#                      `to_camel`/`camelize` **猜**有没有转换设施，从不读契约填的白名单）
#    另：`languages` 补齐 `.vue`（此前在 CODE_EXTS 里却无 language 条目 = 扫到不判）、`.scss/.less`、
#      `shell`、以及 **game 域的 `gdscript`(.gd) 与 `c`(.c/.h)**（实测游戏工程 1234 个 .gd 此前零判据）。
# ---------------------------------------------------------------------------
DEFAULT_STYLE: dict = {
    "style_version": 2,
    "languages": {
        "python": {"ext": [".py"], "file": "snake_case", "func": "snake_case",
                   "class": "PascalCase", "const": "UPPER_SNAKE", "test_func": "test_snake_case"},
        "javascript": {"ext": [".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue"], "file": "kebab-case",
                       "func": "camelCase", "class": "PascalCase", "const": "UPPER_SNAKE", "test_func": "any"},
        "sql": {"ext": [".sql"], "file": "snake_case"},
        "gdscript": {"ext": [".gd"], "file": "snake_case", "func": "snake_case",
                     "class": "PascalCase", "const": "UPPER_SNAKE", "test_func": "any"},
        "c": {"ext": [".c", ".h"], "file": "snake_case", "func": "snake_case",
              "class": "PascalCase", "const": "UPPER_SNAKE", "test_func": "any"},
        "shell": {"ext": [".sh", ".ps1", ".bat"], "file": "snake_case", "func": "snake_case"},
        "style": {"ext": [".css", ".scss", ".less"], "class": "kebab-case"},
    },
    "database": {"table": "snake_case", "column": "snake_case", "index": "idx_<table>_<column>",
                 "foreign_key": "fk_<table>_<target>", "migration": "<NNN>_<name>.sql"},
    "api": {"path_segment": "kebab-case", "path_param": "any", "json_field": "camelCase"},
    "other": {"dir": "kebab-case|snake_case", "css_class": "kebab-case", "env_var": "UPPER_SNAKE"},
    # —— 位置判据：`where` 声明"在哪儿收"，casing 未写则取 POSITION_DEFAULT_CASING ——
    "positions": {
        "json_field": {"where": ["json_key"]},
        "env_var": {"where": ["env_file", "getenv", "compose_env"], "allow": []},
        "error_code": {"where": ["error_code"]},
        "config_key": {"where": ["config_key"]},
        "event_name": {"where": ["event_topic"]},
        "cache_key": {"where": ["cache_key"]},
        "prompt_id": {"where": ["prompt_header", "prompt_registry"]},
        "path_param": {"where": ["route_param"],
                       "by_lang": {"python": "snake_case", "javascript": "camelCase",
                                   "gdscript": "snake_case", "c": "snake_case"}},
    },
    "infra": {"docker_env_key": "UPPER_SNAKE", "docker_stage": "kebab-case",
              "compose_service": "kebab-case", "ci_job": "kebab-case", "shell_func": "snake_case"},
    "conversion": {"modules": ["serializer", "serialize", "case_convert", "adapters"], "files": []},
    "synonyms": {},
}

# `positions.*` 的默认 casing（契约 §0.2 的 positions 项可覆写）
POSITION_DEFAULT_CASING = {
    "json_field": "camelCase", "env_var": "UPPER_SNAKE", "error_code": "UPPER_SNAKE",
    "config_key": "lower_dot", "event_name": "lower_dot", "cache_key": "cache_key",
    "prompt_id": "prompt_id", "path_param": "any",
}

# 扫描时跳过的目录（第三方/构建产物/运行期数据/框架自身域）——跳过的会写进报告，不静默隐藏
SKIP_DIRS = {
    ".git", ".hg", ".svn", ".idea", ".vscode", ".vs", ".tox", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".cache", ".next", ".nuxt", ".svelte-kit", ".venv", "venv", "env", "site-packages",
    "node_modules", "bower_components", "vendor", "__pycache__", "dist", "build", "out", "target",
    "coverage", "htmlcov", "egg-info", "_internal", "产物", "tmp", "交付", "素材", "环境配置", "data",
    "dist-electron", "release", "logs",
    # **第三方/参考语料**（v5.5.0 加）：项目里下载的对标源码不该按本项目风格判——
    # 实测游戏工程新增覆盖后 629 条命中里绝大多数落在 `demo/refs/_new/<下载的开源项目>/`、
    # `<某项目>/extracted/`，那些是别人的代码，判它们只会淹没自己的问题。
    "refs", "reference", "references", "third_party", "thirdparty", "external", "extracted",
}
# **非源码树**：证据/快照/备份。跳过但**在报告里列出**（不静默隐藏）。
# 缘起（v5.5.0）：真机对拍发现，证据目录下的 .json 快照与探针脚本会被判成"JSON 字段/错误码"违规，
# 把真违规淹掉（实测 135+119 条）。这些不是"受域8 管的新增代码"，故整树跳过并如实报告。
NON_SOURCE_DIRS = {"证据", "bak", "backup", "备份", "前像", "snapshot", "snapshots", "old"}
# 隐藏目录不能一刀切跳过——CI 配置就住在 .github/workflows/ 与 .circleci/ 里
HIDDEN_SKIP = {".git", ".hg", ".svn", ".idea", ".vscode", ".vs", ".tox", ".mypy_cache",
               ".pytest_cache", ".ruff_cache", ".cache", ".next", ".nuxt", ".venv", ".svelte-kit"}
SKIP_FILE_PATTERNS = ("*.min.js", "*.min.css", "*.bundle.js", "package-lock.json", "yarn.lock", "pnpm-lock.yaml")
# 代码扩展名（含 v5.5.0 新增：.vue/.scss/.less/.gd/.c/.h/.sh/.ps1/.bat）
CODE_EXTS = (".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue", ".sql",
             ".css", ".scss", ".less", ".gd", ".c", ".h", ".sh", ".ps1", ".bat")
NONASCII_CHECK_EXTS = (".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".sql",
                       ".gd", ".c", ".h", ".sh", ".ps1", ".bat")
JS_LANGS = ("javascript",)
GD_LANG = "gdscript"
# 基础设施文件（**无扩展名或扩名不在 CODE_EXTS**，按文件名识别；v5.5.0 新增）
DOCKERFILE_NAMES = ("dockerfile", "containerfile")
COMPOSE_PREFIXES = ("docker-compose", "compose")
ENV_PREFIXES = (".env", "env.")
CI_PATH_HINTS = (".github/workflows/", ".circleci/", ".gitlab-ci", "azure-pipelines", "jenkinsfile")
CI_FILE_NAMES = (".gitlab-ci.yml", ".gitlab-ci.yaml", "azure-pipelines.yml", "jenkinsfile")
SQL_KEYWORDS = {
    "constraint", "primary", "foreign", "unique", "check", "index", "key", "references", "on", "using",
    "create", "table", "if", "not", "exists", "add", "column", "alter", "and", "or", "as", "with",
    "default", "collate", "generated", "autoincrement", "auto_increment", "comment", "engine",
}
JS_METHOD_KEYWORDS = {"if", "for", "while", "switch", "catch", "function", "return", "else", "do", "typeof",
                      "new", "delete", "in", "of", "case", "break", "continue", "throw", "with", "await"}
CONST_ALLOWLIST = {"logger", "log", "app", "db", "router", "bp", "blueprint", "celery", "engine", "settings",
                   "session", "metadata", "base", "cache", "config", "cfg", "client", "pool", "conn"}

RULE_LABEL = {
    "file-naming": "文件命名",
    "file-nonascii": "文件名含非 ASCII（代码文件）",
    "dir-naming": "目录命名",
    "dir-style-mixed": "同级目录风格混用",
    "class-naming": "类名风格",
    "const-naming": "模块级常量命名（启发式）",
    "func-naming": "函数命名风格",
    "func-style-mixed": "同目录函数风格混用（风格漂移）",
    "test-func-naming": "测试函数命名",
    "db-table-naming": "表名风格",
    "db-column-naming": "列名风格",
    "db-index-template": "索引命名模板",
    "db-fk-template": "外键命名模板",
    "db-migration-name": "迁移文件命名",
    "api-path-style": "接口路径风格",
    "api-path-mixed": "接口路径风格混用",
    "api-param-mixed": "路径参数写法混用（同仓）",
    "css-class-style": "CSS 类名风格",
    "css-class-mixed": "CSS 类名风格混用",
    "json-crosslayer-drift": "跨层字段名漂移（需人工复核）",
    "exception-expired": "已登记例外过期",
    # —— v5.5.0 新增（借 sqlfluff 的族前缀 + dotenv-linter 的 check 注册表）——
    "json-field-casing": "JSON 字段 casing（载荷位置）",
    "env-var-casing": "环境变量 casing",
    "error-code-casing": "错误码 casing",
    "config-key-casing": "配置键 casing",
    "event-name-casing": "事件 / topic 名 casing",
    "cache-key-shape": "缓存 key 形态",
    "prompt-id-shape": "提示词 id 形态（<域>.<用途>.vN）",
    "path-param-casing": "路由参数名 casing",
    "synonym-banned": "使用了禁用的同义词",
    "conversion-blindspot": "契约 §2 声明的转换点无对应模块",
    "docker-env-key": "Dockerfile ENV/ARG 键 casing",
    "docker-stage": "Dockerfile 多阶段 stage 名",
    "compose-service": "compose 服务名",
    "ci-job": "CI job 名",
    "shell-func": "shell / ps1 函数名",
    "migration-template": "迁移文件名模板（模板驱动）",
}
# 规则族（报告里按族汇总，便于"按族裁决/开关"；借 sqlfluff 的 AL/CP/RF 前缀思路）
RULE_FAMILY = {
    "file-naming": "STY", "file-nonascii": "STY", "dir-naming": "STY", "dir-style-mixed": "STY",
    "class-naming": "STY", "const-naming": "STY", "func-naming": "STY", "func-style-mixed": "STY",
    "test-func-naming": "STY", "db-table-naming": "STY", "db-column-naming": "STY",
    "db-index-template": "STY", "db-fk-template": "STY", "db-migration-name": "STY",
    "migration-template": "STY", "api-path-style": "STY", "api-path-mixed": "STY", "api-param-mixed": "STY",
    "css-class-style": "STY", "css-class-mixed": "STY", "synonym-banned": "STY",
    "json-field-casing": "CFG", "env-var-casing": "CFG", "error-code-casing": "CFG",
    "config-key-casing": "CFG", "event-name-casing": "CFG", "cache-key-shape": "CFG",
    "prompt-id-shape": "CFG", "path-param-casing": "CFG",
    "json-crosslayer-drift": "CVT", "conversion-blindspot": "CVT",
    "docker-env-key": "INF", "docker-stage": "INF", "compose-service": "INF", "ci-job": "INF",
    "shell-func": "INF",
    "exception-expired": "META",
}
# 行内豁免注释：`style-audit: off[: rule,rule]` … `style-audit: on`（借 dotenv-linter 的 on/off）
INLINE_SWITCH_RX = re.compile(r"style-audit:\s*(off|on)\s*:?\s*([A-Za-z0-9\-,\s*]*)")
# —— v5.5.0 位置判据的采集正则 ——
RE_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
RE_GETENV = re.compile(r"os\.(?:getenv|environ\.get)\s*\(\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']"
                       r"|os\.environ\s*\[\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']\s*\]"
                       r"|process\.env\.([A-Za-z_][A-Za-z0-9_]*)"
                       r"|process\.env\s*\[\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']\s*\]"
                       r"|import\.meta\.env\.([A-Za-z_][A-Za-z0-9_]*)"
                       r"|OS\.GetEnvironmentVariable\s*\(\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']"
                       r"|\$env:([A-Za-z_][A-Za-z0-9_]*)")
RE_JSON_KEY = re.compile(r"[\"']([A-Za-z_][A-Za-z0-9_]*(?:_[A-Za-z0-9]+|[A-Z][a-z0-9]+)*)[\"']\s*:")
# JS/TS 的**无引号对象键**（`{ cover_sub: 1 }`）——只收"像 snake 或 camel"的名字，
# 排除 `case x:` / `default:` 这类单个小写词
RE_JS_BARE_KEY = re.compile(r"(?<![\w$.])([A-Za-z_][A-Za-z0-9_]*)\s*:(?!:)")
RE_JS_KEYWORDS = {"case", "default", "if", "else", "for", "while", "switch", "return", "typeof",
                  "function", "class", "const", "let", "var", "in", "of", "new", "do", "catch", "try"}
RE_ROUTE = re.compile(r"\.(?:get|post|put|patch|delete|head|options|use)\s*\(\s*[\"'](/[^\"']*)[\"']")
RE_ROUTE_PARAM = re.compile(r"(?::([A-Za-z_]\w*))|(?:\{([A-Za-z_]\w*)\})")
RE_CODE_ASSIGN = re.compile(r"^\s*([A-Z][A-Z0-9_]*CODE[A-Z0-9_]*)\s*=\s*[\"']([A-Za-z0-9_.\-]+)[\"']")
RE_CODE_JSON = re.compile(r"[\"']code[\"']\s*:\s*[\"']([A-Za-z0-9_.\-]+)[\"']")
# 配置键：捕获**必须宽松**——只写 [a-z.] 就永远看不见 camelCase 违规（判据自废）
RE_CONFIG_KEY = re.compile(r"(?:config|cfg|settings|conf)\.(?:get|getenv|load)\s*\(\s*[\"']"
                           r"([A-Za-z][A-Za-z0-9_.\-]*\.[A-Za-z][A-Za-z0-9_.\-]*)[\"']"
                           r"|(?:\bCFG|\bCONFIG|\bSettings)\s*\[\s*[\"']"
                           r"([A-Za-z][A-Za-z0-9_.\-]*\.[A-Za-z][A-Za-z0-9_.\-]*)[\"']\s*\]")
RE_EVENT = re.compile(r"\b(?:publish|emit|subscribe|dispatch|send_event|produce)\s*\(\s*[\"']([A-Za-z0-9_.\-]+)[\"']")
RE_CACHE_KEY = re.compile(r"\b(?:cache|redis|memcached)\.(?:get|set|delete|exists|incr|expire)\s*\(\s*[\"']([A-Za-z0-9_:.\-]+)[\"']")
RE_PROMPT_HEADER = re.compile(r"<!--\s*id:\s*([A-Za-z0-9_.\-]+)\s*-->")
RE_PROMPT_REG = re.compile(r"^\|\s*`?(prompt\.[A-Za-z0-9_.\-]+)`?\s*\|")
RE_DOCKER_ENV = re.compile(r"^\s*(?:ENV|ARG|LABEL)\s+([A-Za-z_][\w.\-]*)")
RE_DOCKER_FROM = re.compile(r"^\s*FROM\s+\S+\s+AS\s+([A-Za-z0-9_.\-]+)", re.I)
RE_YAML_KEY = re.compile(r"^(\s*)([A-Za-z0-9_.\-]+)\s*:")
RE_CSS_SELECTOR = re.compile(r"^\s*\.([A-Za-z][A-Za-z0-9_-]*)")
RE_CLASS_ATTR = re.compile(r"class\s*=\s*[\"']([^\"']+)[\"']")
RE_PAYLOAD_WINDOW = re.compile(r"json\.dumps\s*\(|JSONResponse\s*\(|jsonify\s*\(|return\s*\{|to_dict\s*\(|asdict\s*\(|dict\s*\(")
# **载荷边界**（`json_field` 判据的位置门；详见 looks_payload_bearing 的注释）
PAYLOAD_DIRS = {"api", "apis", "client", "clients", "serializers", "serializer",
                "responses", "dtos", "adapters"}
PAYLOAD_NAME_RX = re.compile(r"^(serial|response|payload|dto|adapter|schema_|api_)")
PAYLOAD_NAME_EXTRA = re.compile(r"(^|[-_])client|_response$|-response$")
POSITION_RULE = {
    "json_field": "json-field-casing", "env_var": "env-var-casing", "error_code": "error-code-casing",
    "config_key": "config-key-casing", "event_name": "event-name-casing", "cache_key": "cache-key-shape",
    "prompt_id": "prompt-id-shape", "path_param": "path-param-casing",
}
# **平台/系统自带**的环境变量：不由本项目命名，项目无权改名
# （真实工程对拍发现：`SystemRoot` 被报成 env-var casing 违规——Windows 提供的名字，改不了）
DEFAULT_ENV_ALLOW = {"SystemRoot", "windir", "ComSpec", "ProgramFiles", "ProgramFiles(x86)",
                     "ProgramData", "APPDATA", "LOCALAPPDATA", "USERPROFILE", "USERNAME",
                     "COMPUTERNAME", "TEMP", "TMP", "PATH", "HOME", "PWD", "SHELL", "LANG",
                     "TZ", "CI", "PYTHONPATH", "VIRTUAL_ENV", "HOSTNAME", "NODE_ENV"}

# ---------------------------------------------------------------------------
# 1. 风格谓词
# ---------------------------------------------------------------------------


# casing 名 → 正则（数字类统一写作 `[0-9]`；`disallowDigits` 时整体抽掉）——借 spectral casing.ts 的 CASES 表
CASING_PATTERNS = {
    "snake_case": r"_{0,2}[a-z][a-z0-9_]*",
    "camelCase": r"_{0,2}[a-z][a-zA-Z0-9]*",
    "PascalCase": r"_{0,2}[A-Z][a-zA-Z0-9]*",
    "UPPER_SNAKE": r"_{0,2}[A-Z][A-Z0-9_]*",
    "kebab-case": r"[a-z0-9]+(-[a-z0-9]+)*",
    "flat": r"[a-z][a-z0-9]*",
    "cobol": r"[A-Z][A-Z0-9]*(-[A-Z][A-Z0-9]*)*",
    "lower_dot": r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*",
    "cache_key": r"[a-z][a-z0-9_]*(?::[A-Za-z0-9_\-]+)+",
    "prompt_id": r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+\.v[0-9]+",
}
# casing 词表的**别名**（v5.5.0 修）：契约默认把 python 的 test_func 写成 `test_snake_case`，
# 但它不是合法 casing 词 → 旧实现里 `match_style("test_snake_case", name)` 恒为 False，
# **测试文件里的每个 test_* 函数都被误报**（框架自身实测 11 条全是合法 snake 名，全部假阳性）。
# 前缀 `test` 的要求由调用处保证（只对 test 开头的函数判），故此处归一到 snake_case。
CASING_ALIAS = {"test_snake_case": "snake_case", "test_camelCase": "camelCase"}
KNOWN_CASINGS = set(CASING_PATTERNS) | set(CASING_ALIAS) | {"any"}


def match_style(style: str, name: str, opts: dict | None = None) -> bool:
    """名字是否符合风格；`any` / 空 = 不检查；多值用 `|` 分隔（任一命中即可）。

    v5.5.0 把 casing 做成**表驱动**（借 spectral `packages/functions/src/casing.ts` 的 CASES 表），
    并补上原先"文档写了但没实现"的注入口径：
      `disallowDigits`（禁数字）、`separator{char,allowLeading}`（允许前导/尾随分隔符，
      最多 2 个，覆盖 Python `__version__` 这类 dunder）。
    **未知 casing 词 → 不判并计入契约词表校验**（`collect_unknown_casings`）——
    旧实现遇到未知词会"全部判违规"，`test_snake_case` 就是这么把所有测试函数判死的。
    """
    if not style or style == "any":
        return True
    opts = opts or {}
    sep = opts.get("separator") or {}
    for s in str(style).split("|"):
        s = CASING_ALIAS.get(s.strip(), s.strip())
        if s in ("any", ""):
            return True
        pat = CASING_PATTERNS.get(s)
        if pat is None:
            continue                      # 未知词：不判（由词表校验去报，避免"静默全判违规"）
        if opts.get("disallowDigits"):
            # 抽掉字符类里的数字段（`[a-z0-9]`→`[a-z]`）；`[0-9]` 单独成类时保留，避免出现空字符类
            pat = pat.replace("0-9", "").replace("[]", "[0-9]")
        probe = name
        if sep.get("char") and sep.get("allowLeading"):
            esc = re.escape(sep["char"])
            probe = re.sub(r"^%s{1,2}" % esc, "", probe)
            probe = re.sub(r"%s{1,2}$" % esc, "", probe)
        if re.fullmatch(pat, probe):
            return True
    return False


def collect_unknown_casings(style: dict) -> list:
    """校验契约 §0.2 里的 casing 词是否都在词表内（把"拼错的风格词"从静默变成报告）。
    返回 ["键路径 = 取值", ...]。
    **模板串不算 casing 词**：`database.index = "idx_<table>_<column>"` 里含 `<...>` 占位符，
    是命名模板而不是 casing（第一版把它们报成"词不在词表内"，属误报）。"""
    bad: list = []

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, path + [str(k)])
        elif isinstance(node, str):
            if "<" in node and ">" in node:
                return                      # 模板串（idx_<table>_<column> 等），不是 casing 词
            for one in node.split("|"):
                one = one.strip()
                if one and one not in KNOWN_CASINGS:
                    bad.append("%s = %s" % (".".join(path), one))

    for key in ("languages", "other", "api", "database", "infra", "positions"):
        if key in style:
            walk(style[key], [key])
    return bad


def drift_kind(name: str) -> str:
    """把名字归类为 drift 判据用的一侧：camel 侧（含大写）/ snake 侧（含下划线）/ 中性（单词小写）。"""
    n = name.strip("_")
    if not n:
        return "neutral"
    if re.search(r"[a-z][A-Z]", n):
        return "camel"
    if "_" in n:
        return "snake"
    return "neutral"


def camel_to_snake(name: str) -> str:
    s = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    return s.lower()


def _is_sql_block_end(line: str) -> bool:
    """判断是否 CREATE TABLE 语句体的结束行：去掉引号/逗号/空白后恰为 `)` 或 `);`。
    这样 `'  UNIQUE(team_id, email)',`（列行）不会被误判为结束行。"""
    bare = line.strip().strip("'\",`").strip()
    return bare in (")", ");", ") ;")


# ---------------------------------------------------------------------------
# 2. 契约读取（定档块 + 例外登记表）
# ---------------------------------------------------------------------------


def load_style(path: Path) -> tuple[dict, str, list[dict]]:
    """返回 (style, 判据来源说明, 例外表)。定档块缺失或解析失败 → 退回框架默认（来源说明会写明）。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    style, src = None, ""
    for m in re.finditer(r"```json\s*\n(.*?)```", text, re.S):
        try:
            obj = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and ("languages" in obj or "style_version" in obj):
            style = obj
            break
    if style is None:
        style, src = DEFAULT_STYLE, f"框架默认风格（{path.name} 内未找到 ```json 定档块）"
    else:
        src = f"项目定档（{path}）"
    # 合并缺省键：项目只写了一半的项，用框架默认补齐，避免"部分继承"造成的空洞
    merged = json.loads(json.dumps(DEFAULT_STYLE))
    for k, v in style.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k].update(v)
        else:
            merged[k] = v
    return merged, src, parse_exceptions(text)


def parse_exceptions(text: str) -> list[dict]:
    """读 §4 例外登记表：`| 路径 | 规则 | 理由 | 批准人 | 到期 |`；含 `<...>` 占位符的行忽略。"""
    out: list[dict] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line.startswith("|") or line.count("|") < 5:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 5 or any(c in cells for c in ("路径（glob 或 `文件:行`）", "路径")):
            continue
        if "<" in cells[0] or ">" in cells[0] or "---" in cells[0]:
            continue
        if not re.search(r"[/\\.*]", cells[0]):
            continue
        out.append({"path": cells[0].strip("`"), "rule": cells[1], "reason": cells[2],
                    "approver": cells[3], "due": cells[4]})
    return out


def exception_verdict(rel: str, rule: str, exceptions: list[dict], today: _dt.date):
    """返回 (是否被登记抑制, 命中规则是否过期, 命中的例外条目)。"""
    for ex in exceptions:
        pat = ex["path"]
        if not (fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(rel, pat.rstrip("/") + "/*") or rel == pat):
            continue
        rule_cell = ex["rule"]
        if rule_cell not in ("*", "全部", "any") and rule not in rule_cell and RULE_LABEL.get(rule, rule) not in rule_cell:
            continue
        due = None
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", ex["due"])
        if m:
            try:
                due = _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                due = None
        expired = bool(due and due < today)
        return (not expired), expired, ex
    return False, False, None


# ---------------------------------------------------------------------------
# 3. 源码读取与注释剥离（保留行号：注释位置换成空格，不删行）
# ---------------------------------------------------------------------------


def blank_comments(text: str, ext: str) -> str:
    """把注释内容换成空格（保留换行与行号），避免"注释里的 SQL/示例"被当成真代码扫到。
    v5.5.0：补 .gd / .c / .h / .sh / .ps1 / .bat / .scss / .less / yaml / Dockerfile / .env 的注释符。"""
    out = list(text)
    if ext in (".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".css", ".scss", ".less", ".vue", ".c", ".h"):
        for m in re.finditer(r"/\*.*?\*/", text, re.S):
            for i in range(m.start(), m.end()):
                if out[i] != "\n":
                    out[i] = " "
    if ext in (".py", ".gd", ".sh", ".ps1", ".bat", ".yaml", ".yml", ".env", "") or ext.startswith(".env"):
        marker = "#"
    elif ext == ".sql":
        marker = "--"
    elif ext in (".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue", ".scss", ".less", ".c", ".h"):
        marker = "//"
    else:
        return "".join(out)
    lines = "".join(out).split("\n")
    for i, line in enumerate(lines):
        pos = _comment_pos(line, marker)
        if pos >= 0:
            lines[i] = line[:pos]
    return "\n".join(lines)


def _comment_pos(line: str, marker: str) -> int:
    """返回行内注释起点（不在字符串内）；无注释返回 -1。"""
    in_str, quote, i = False, "", 0
    while i < len(line):
        ch = line[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                in_str = False
        else:
            if ch in ("'", '"', "`"):
                in_str, quote = True, ch
            elif line.startswith(marker, i):
                return i
        i += 1
    return -1


# ---------------------------------------------------------------------------
# 4. 范围解析
# ---------------------------------------------------------------------------


def resolve_scope(root: Path, scope_args: list[str]) -> tuple[list[Path], str]:
    """扫描范围：默认 = config.json 登记的代码域（repos/*），无登记则退回项目根。"""
    if scope_args:
        dirs = []
        for s in scope_args:
            p = (root / s).resolve() if not Path(s).is_absolute() else Path(s)
            if p.is_dir():
                dirs.append(p)
        return dirs, "命令行 --scope 指定"
    cfg_path = root / "project-state" / "config.json"
    if cfg_path.is_file():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cfg = {}
        repos = (cfg.get("paths") or {}).get("repos")
        if isinstance(repos, str):
            repos = [repos]
        if isinstance(repos, list):
            dirs = [root / r for r in repos if isinstance(r, str) and (root / r).is_dir()]
            if dirs:
                return dirs, "config.json 的 paths.repos（六域「代码区」）"
    if (root / "repos").is_dir():
        return [root / "repos"], "自动识别 repos/（六域「代码区」）"
    return [root], "项目根（未发现 repos/ 与 config.json 登记）"


def looks_payload_bearing(rel: str, extra_files=None) -> bool:
    """该路径是否可能承载"要发出去的载荷"？—— `json_field` 判据的**位置门**，同时用作 .json 的扫描门。
    判不出就不判（宁可少报）：真机对拍教训——按"所有对象键"判会报 29,155 条，93% 是合法 snake 键；
    按全路径子串判会把"路径里含 response 的证据快照"当载荷；把 `services/`（框架自己的业务逻辑层）
    当载荷层又会误伤 125 条。故只认**序列化层目录名 / 载荷类文件名**，外加契约显式加白。"""
    for d in [str(x) for x in (extra_files or [])]:
        d = d.strip().strip("/")
        if d and (fnmatch.fnmatch(rel, d) or rel.endswith("/" + d) or rel == d):
            return True
    parts = rel.lower().split("/")
    if any(p in PAYLOAD_DIRS for p in parts[:-1]):
        return True
    stem = parts[-1].rsplit(".", 1)[0]
    return bool(PAYLOAD_NAME_RX.match(stem) or PAYLOAD_NAME_EXTRA.search(stem))


def is_scope_file(p: Path, root: Path) -> bool:
    """本脚本**该看**的文件：代码文件（CODE_EXTS）+ 基础设施文件 + 载荷类 .json + 提示词/配置类 .md。
    v5.5.0 修：`.md` / `.json` 原先**根本不在扫描范围** → 提示词资产与 `配置中心.md` 的 prompt.* 注册表
    无人判（`prompt-id-shape` 判据等于摆设）；但也不能全扫（游戏工程有 474 个 .md、264 个 .json），
    故按"是不是提示词/配置/载荷"收窄。"""
    name = p.name.lower()
    if name in DOCKERFILE_NAMES or name.startswith(DOCKERFILE_NAMES):
        return True
    if name.startswith(COMPOSE_PREFIXES) and p.suffix.lower() in (".yml", ".yaml"):
        return True
    if name.startswith(ENV_PREFIXES):
        return True
    if name in CI_FILE_NAMES:
        return True
    rel = rel_of(p, root)
    low = rel.lower()
    if p.suffix.lower() in (".yml", ".yaml") and any(h in low for h in CI_PATH_HINTS):
        return True
    if p.suffix.lower() == ".json":
        return looks_payload_bearing(rel)
    if p.suffix.lower() == ".md":
        return "prompt" in low or "配置中心" in rel
    return p.suffix.lower() in CODE_EXTS


def walk_files(dirs: list[Path], root: Path, max_kb: float) -> tuple[list[Path], list[str], list[str], list[str]]:
    """遍历扫描范围；跳过第三方/构建/运行期/**证据快照**目录并在报告里列出（不静默隐藏）。"""
    files: list[Path] = []
    skipped_dirs: list[str] = []
    skipped_big: list[str] = []
    skipped_patterns: list[str] = []
    seen: set[Path] = set()
    for d in dirs:
        for cur, subs, names in os.walk(d):
            keep = []
            for s in sorted(subs):
                if s in SKIP_DIRS:
                    skipped_dirs.append(rel_of(Path(cur) / s, root) + "/")
                elif s.startswith(".") and s in HIDDEN_SKIP:
                    skipped_dirs.append(rel_of(Path(cur) / s, root) + "/")
                elif s in NON_SOURCE_DIRS or any(
                        s.startswith(n + "-") or s.startswith(n + "_") for n in NON_SOURCE_DIRS):
                    # 也匹配 `证据-wave31` / `backup-2026` 这类**带后缀**的证据/快照目录
                    skipped_dirs.append(rel_of(Path(cur) / s, root) + "/（非源码树：证据/快照/备份）")
                else:
                    keep.append(s)
            subs[:] = keep
            for n in sorted(names):
                p = Path(cur) / n
                if p in seen:
                    continue
                if any(fnmatch.fnmatch(n, pat) for pat in SKIP_FILE_PATTERNS):
                    skipped_patterns.append(rel_of(p, root))
                    continue
                if not is_scope_file(p, root):
                    continue
                try:
                    size = p.stat().st_size
                except OSError:
                    continue
                if size > max_kb * 1024:
                    skipped_big.append(rel_of(p, root))
                    continue
                seen.add(p)
                files.append(p)
    return sorted(files), skipped_dirs, skipped_big, skipped_patterns


def rel_of(p: Path, root: Path) -> str:
    try:
        return p.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return p.as_posix()


# ---------------------------------------------------------------------------
# 5. 主扫描
# ---------------------------------------------------------------------------


class Audit:
    def __init__(self, root: Path, style: dict, exceptions: list[dict], scope_dirs: list[Path] | None = None,
                 since: str | None = None):
        self.root = root
        self.style = style
        self.exceptions = exceptions
        self.today = _dt.date.today()
        # 扫描根（各代码仓）——"同仓"类判定按它分组，不按项目根的第一段（项目根常有 repos/ 层）
        self.scope_rels = sorted([rel_of(d, root) for d in (scope_dirs or [])], key=len, reverse=True)
        self.findings: list[dict] = []
        self.suppressed: list[dict] = []
        self.expired: list[dict] = []
        self.files_scanned = 0
        self.seen_paths: list[str] = []            # 已扫文件（供 conversion-blindspot 判"模块是否存在"）
        self.ext_counter: dict[str, int] = {}
        self.repos: dict[str, dict] = {}          # 顶层名 → {funcs: [...], paths: [...], params: set, css: [...]}
        self.snake_pool: dict[str, set] = {}      # 名字 → {file}
        self.camel_pool: dict[str, set] = {}
        self.conv_facilities: list[str] = []
        self.db_tables: dict[str, str] = {}       # 表名 → 首见文件
        self.notes: list[str] = []
        self.since: str | None = since              # --since：只判本卡新增/改动（v5.5.0）
        self.inline: dict[int, object] = {}       # 行内豁免：行号 → "*" 或 {规则名}
        self.inline_suppressed: list[dict] = []
        self.templates_checked: set[str] = set()  # 提示词资产等已判过的模板

    def _repo_key(self, rel: str) -> str:
        """同仓判定键 = 文件所属的扫描根（各代码仓）；无匹配则退回首段目录。"""
        for sr in self.scope_rels:
            if rel == sr or rel.startswith(sr + "/"):
                return sr
        return rel.split("/")[0] if "/" in rel else "."

    # -- 命中登记（含例外判定） ------------------------------------------------
    def hit(self, rel: str, line: int, rule: str, current: str, expected: str, advice: str, severity: str = "无前缀"):
        # 行内豁免优先于一切（v5.5.0）：`style-audit: off[: rule,rule]` … `on`
        sup = self.inline.get(line)
        if sup and (sup == "*" or rule in sup):
            self.inline_suppressed.append({"file": rel, "line": line, "rule": rule,
                                           "rule_label": RULE_LABEL.get(rule, rule), "current": current})
            return
        ok, expired, ex = exception_verdict(rel, rule, self.exceptions, self.today)
        item = {"file": rel, "line": line, "rule": rule, "rule_label": RULE_LABEL.get(rule, rule),
                "family": RULE_FAMILY.get(rule, "OTH"),
                "current": current, "expected": expected, "advice": advice, "severity": severity}
        if ok:
            item["exception"] = ex
            self.suppressed.append(item)
            return
        if expired:
            item["severity"] = "Medium"
            item["advice"] = f"例外已于 {ex['due']} 到期，须裁决（改代码 / 续期须报用户 / 升格为正式风格）：{advice}"
            self.expired.append(item)
        self.findings.append(item)

    def note(self, text: str):
        if text not in self.notes:
            self.notes.append(text)

    # -- 逐文件 ---------------------------------------------------------------
    def scan_file(self, path: Path):
        rel = rel_of(path, self.root)
        ext = path.suffix.lower()
        self.ext_counter[ext] = self.ext_counter.get(ext, 0) + 1
        lang, lstyle = None, None
        for lname, lcfg in (self.style.get("languages") or {}).items():
            if ext in [e.lower() for e in (lcfg.get("ext") or [])]:
                lang, lstyle = lname, lcfg
                break
        repo = self._repo_key(rel)
        rinfo = self.repos.setdefault(repo, {"funcs": [], "paths": [], "params": set(), "css": []})

        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        self.files_scanned += 1
        self.seen_paths.append(rel)
        self.inline = self._inline_directives(raw, ext)

        # —— 基础设施文件（v5.5.0）：Dockerfile / compose / CI yaml / .env ——
        name_low = path.name.lower()
        if name_low in DOCKERFILE_NAMES or name_low.startswith(DOCKERFILE_NAMES):
            self._scan_dockerfile(rel, raw)
            return
        if name_low.startswith(COMPOSE_PREFIXES) and ext in (".yml", ".yaml"):
            self._scan_compose(rel, raw)
            return
        if name_low.startswith(ENV_PREFIXES):
            self._scan_env_file(rel, raw)
            return
        if ext in (".yml", ".yaml"):
            self._scan_ci(rel, raw)
            return

        # 文件命名（迁移文件按模板单独判，不走通用文件风格）
        is_migration = bool(re.fullmatch(r"\d{3,}_[a-z0-9_]+", path.stem))
        if lstyle and lstyle.get("file") and not is_migration and path.name.isascii():
            stem = path.stem
            probe = re.sub(r"(^test_|_test$|\.test$|\.spec$|\.d$)", "", stem)
            if not match_style(lstyle["file"], probe):
                self.hit(rel, 0, "file-naming", f"{path.name}", f"文件名 {lstyle['file']}（{lang}）",
                         "改名并同步引用（改名属结构变更，先出方案报主控）")
        if ext in NONASCII_CHECK_EXTS and not path.name.isascii():
            self.hit(rel, 0, "file-nonascii", path.name, "代码文件名用 ASCII",
                     "代码文件名含中文会导致跨平台/命令行/打包（PyInstaller、Docker、URL）问题，建议改 ASCII 名", "Low")

        if "toCamel" in raw or "to_camel" in raw or "camelize" in raw or "snakeToCamel" in raw or "snake_to_camel" in raw:
            if rel not in self.conv_facilities:
                self.conv_facilities.append(rel)
        text = blank_comments(raw, ext)
        lines = text.split("\n")

        if lang == "python":
            self._py_scans(rel, lines, lstyle, rinfo, text)
        elif lang in JS_LANGS:
            self._js_scans(rel, lines, lstyle, rinfo, text)
        elif lang == GD_LANG:
            self._gd_scans(rel, lines, lstyle, rinfo)
        elif lang == "c":
            self._c_scans(rel, lines, lstyle, rinfo)
        elif lang == "shell":
            self._shell_scans(rel, lines)
        elif ext == ".sql":
            self._sql_file_name(rel, path)
        if ext in (".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue",
                   ".gd", ".sql", ".c", ".h"):
            self._sql_scans(rel, lines)
            self._api_scans(rel, lines)
            self._pool_scans(rel, lines)
        if ext in (".css", ".scss", ".less"):
            self._css_scans(rel, lines, rinfo)
        if ext == ".vue":
            self._vue_template_scans(rel, lines)

        # —— v5.5.0 新增三块：位置判据 / 同义词 / 跨层转换点 ——
        self._positions_scans(rel, raw, lines, lang, ext)
        self._synonym_scans(rel, lines)
        self._conversion_scans(rel, lines, lang)

    # -- Python --------------------------------------------------------------
    def _py_scans(self, rel, lines, lstyle, rinfo, raw):
        cls_rx = re.compile(r"^\s*class\s+([A-Za-z_]\w*)")
        func_rx = re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)")
        const_rx = re.compile(r"^([A-Za-z_]\w*)\s*(?::[^=\n]+)?=\s*(.+)$")
        for i, line in enumerate(lines, 1):
            m = cls_rx.match(line)
            if m and not match_style(lstyle.get("class", "PascalCase"), m.group(1)):
                self.hit(rel, i, "class-naming", f"class {m.group(1)}",
                         f"类名 {lstyle.get('class', 'PascalCase')}", "改为 PascalCase")
            m = func_rx.match(line)
            if m:
                is_test_file = bool(re.search(r"(^|/)(tests?|spec)/", rel)) or Path(rel).name.startswith("test_")
                self._check_func(rel, i, m.group(1), lstyle, rinfo, "python")
                if is_test_file and m.group(1).startswith("test") and lstyle.get("test_func", "any") != "any":
                    if not match_style(lstyle["test_func"], m.group(1)):
                        self.hit(rel, i, "test-func-naming", f"def {m.group(1)}()",
                                 f"测试函数 {lstyle['test_func']}", "测试名要能定位「被测对象 + 场景」")
            m = const_rx.match(line)
            if m and not line[0].isspace() and self._looks_const(m.group(1), m.group(2), raw):
                if m.group(1) not in CONST_ALLOWLIST and not match_style(lstyle.get("const", "UPPER_SNAKE"), m.group(1)):
                    self.hit(rel, i, "const-naming", f"模块级 {m.group(1)} = <字面量>",
                             f"常量 {lstyle.get('const', 'UPPER_SNAKE')}",
                             "若确为常量改 UPPER_SNAKE；若为可变模块对象请改小写并登记 §4 例外", "Low")

    @staticmethod
    def _looks_const(name: str, value: str, raw: str) -> bool:
        """模块级「疑似常量」启发式：名字带下划线 + 赋值为字面量 + 全文未被再赋值/改值。
        仍可能误报——用 Low 严重度 + 契约 §4 例外表兜住（宁可少报也不把变量当常量打回）。"""
        if "_" not in name or not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            return False
        v = value.strip()
        if not v or not (v[0] in "\"'[{(" or v[0].isdigit() or v in ("True", "False", "None")):
            return False
        esc = re.escape(name)
        mutations = len(re.findall(rf"(?<![\w.]){esc}\s*(?:=[^=]|\+=|-=|\*=|/=|\|=)", raw))
        if mutations > 1:
            return False
        if re.search(rf"(?<![\w.]){esc}\.(?:append|extend|update|add|pop|remove|setdefault)\s*\(", raw):
            return False
        return True

    # -- JavaScript / TypeScript ---------------------------------------------
    def _js_scans(self, rel, lines, lstyle, rinfo, raw):
        cls_rx = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)")
        func_rxs = [
            re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\("),
            re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function\b|\([^)]*\)\s*=>)"),
            re.compile(r"^ {2,}([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{"),
        ]
        const_rx = re.compile(r"^(?:export\s+)?const\s+([A-Za-z_$][\w$]*)\s*=\s*(.+)$")
        for i, line in enumerate(lines, 1):
            m = cls_rx.match(line)
            if m and not match_style(lstyle.get("class", "PascalCase"), m.group(1)):
                self.hit(rel, i, "class-naming", f"class {m.group(1)}",
                         f"类名 {lstyle.get('class', 'PascalCase')}", "改为 PascalCase")
            for rx in func_rxs:
                m = rx.match(line)
                if not m:
                    continue
                name = m.group(1)
                if name in JS_METHOD_KEYWORDS:
                    continue
                self._check_func(rel, i, name, lstyle, rinfo, "javascript")
                break
            m = const_rx.match(line)
            if m and self._looks_const(m.group(1), m.group(2), raw):
                if not match_style(lstyle.get("const", "UPPER_SNAKE"), m.group(1)):
                    self.hit(rel, i, "const-naming", f"const {m.group(1)} = <字面量>",
                             f"常量 {lstyle.get('const', 'UPPER_SNAKE')}",
                             "若确为常量改 UPPER_SNAKE；若为普通绑定请登记 §4 例外", "Low")

    # -- 函数命名 + 同目录漂移收集 -------------------------------------------
    def _check_func(self, rel, line, name, lstyle, rinfo, lang):
        declared = lstyle.get("func", "any")
        # PascalCase 命名的函数按「构造器/类」判（ES5 `function ApiError(...)` 就是类，不是函数名违规）
        if name[:1].isupper():
            want_cls = lstyle.get("class", "PascalCase")
            if not match_style(want_cls, name):
                self.hit(rel, line, "class-naming", f"构造器函数 {name}()", f"类/构造器名 {want_cls}",
                         "构造器按类命名（PascalCase）")
            return
        if declared != "any" and not match_style(declared, name):
            self.hit(rel, line, "func-naming", f"{name}()", f"函数名 {declared}（{lang}）",
                     "按契约 §1.1 改名（同目录混用会被 func-style-mixed 一并报出）")
        kind = drift_kind(name)
        if kind != "neutral":
            rinfo["funcs"].append({"file": rel, "line": line, "name": name, "kind": kind, "lang": lang})

    # -- DB 提取 --------------------------------------------------------------
    def _sql_file_name(self, rel: str, path: Path):
        """迁移文件名：**模板驱动**（v5.5.0 修）。
        旧实现把 `\\d{3,}_[a-z0-9_]+` 写死在代码里，`database.migration` 模板改了不生效；
        若项目定 Flyway 风格 `V<NNN>__<name>.sql`，还会误报"必须以三位序号开头"。"""
        tmpl = ((self.style.get("database") or {}).get("migration") or "").strip()
        if not tmpl or "NNN" not in tmpl:
            return
        in_mig_dir = bool(re.search(r"(migration|migrations|迁移)", rel, re.I))
        if not in_mig_dir:
            return
        # re.escape 不转义 `<`/`>`，故模板占位符替换**不能**带反斜杠（旧写法会替换不到 → 全部迁移文件误报）
        pat = "^" + re.escape(tmpl).replace("<NNN>", r"\d{3,}").replace("<name>", r"[a-z0-9_]+") + "$"
        if not re.match(pat, path.name):
            self.hit(rel, 0, "db-migration-name", path.name,
                     f"迁移文件 {tmpl}（如 007_add_team_id_to_users.sql）",
                     "迁移名必须与 database.migration 模板一致（模板驱动，改模板即改判据）")

    def _sql_scans(self, rel, lines):
        db = self.style.get("database") or {}
        table_rx = re.compile(r"CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+[\"'`\[]?([A-Za-z_][\w.]*)", re.I)
        col_add_rx = re.compile(r"ADD\s+COLUMN\s+[\"'`\[]?([A-Za-z_]\w*)", re.I)
        idx_rx = re.compile(r"CREATE\s+(?:UNIQUE\s+)?INDEX(?:\s+IF\s+NOT\s+EXISTS)?\s+[\"'`\[]?([A-Za-z_]\w*)"
                            r"[\"'`\]]?\s+ON\s+[\"'`\[]?([A-Za-z_]\w*)", re.I)
        fk_rx = re.compile(r"CONSTRAINT\s+[\"'`\[]?([A-Za-z_]\w*)[\"'`\]]?\s+FOREIGN\s+KEY\s*\(([^)]*)\)\s+REFERENCES\s+"
                           r"[\"'`\[]?([A-Za-z_]\w*)", re.I)
        cur_table, cur_line = None, 0
        for i, line in enumerate(lines, 1):
            if cur_table:
                if _is_sql_block_end(line):
                    cur_table = None
                    continue
                self._check_cols(rel, i, line, db, f"表 {cur_table}（定义自第 {cur_line} 行）")
                continue
            m = table_rx.search(line)
            if m:
                name = m.group(1).split(".")[-1]
                self.db_tables.setdefault(name, rel)
                if db.get("table") and not match_style(db["table"], name):
                    self.hit(rel, i, "db-table-naming", f"表 {name}", f"表名 {db['table']}",
                             "表名风格须与 数据模型.md 一致（改表属结构变更，先出迁移方案）")
                rest = line[m.end():]
                if "(" in rest and not _is_sql_block_end(line):
                    cur_table, cur_line = name, i
                    continue
                if "(" in line:
                    self._check_cols(rel, i, line[line.index("(") + 1:], db, f"表 {name}")
            m = col_add_rx.search(line)
            if m and db.get("column") and not match_style(db["column"], m.group(1)):
                self.hit(rel, i, "db-column-naming", f"新增列 {m.group(1)}", f"列名 {db['column']}",
                         "列名风格须与 数据模型.md 一致")
            m = idx_rx.search(line)
            if m:
                self._check_index(rel, i, m.group(1), m.group(2), line, db)
            m = fk_rx.search(line)
            if m:
                self._check_fk(rel, i, m.group(1), m.group(3), db)

    def _check_cols(self, rel, line_no, fragment, db, where):
        style = db.get("column")
        if not style:
            return
        for piece in re.split(r",(?![^()]*\))", fragment):
            head = piece.strip().strip("'\",")
            if not head:
                continue
            m = re.match(r"[\"'`\[]?([A-Za-z_]\w*)", head)
            if not m:
                continue
            name = m.group(1)
            if name.lower() in SQL_KEYWORDS:
                continue
            if not match_style(style, name):
                self.hit(rel, line_no, "db-column-naming", f"列 {name}{(' · ' + where) if where else ''}",
                         f"列名 {style}", "列名风格须与 数据模型.md 一致")

    def _check_index(self, rel, line_no, idx_name, table, line, db):
        tmpl = db.get("index") or ""
        if not tmpl:
            return
        prefix = tmpl.split("<")[0]
        cols = []
        m = re.search(r"\(([^)]*)\)", line)
        if m:
            cols = [c.strip().strip("'\"`").split()[0].split("(")[0] for c in m.group(1).split(",") if c.strip()]
        cols = [c for c in cols if c]
        if prefix and not idx_name.startswith(prefix):
            self.hit(rel, line_no, "db-index-template", f"索引 {idx_name}（表 {table}）",
                     f"索引模板 {tmpl}（前缀 {prefix}）", "索引名一律带前缀，便于扫索引列表时归类")
            return
        rest = idx_name[len(prefix):] if prefix else idx_name
        expected = f"{table}_{cols[0]}" if cols else table
        if rest != table and not (rest.startswith(table + "_") and expected.startswith(rest)):
            self.hit(rel, line_no, "db-index-template", f"索引 {idx_name} ON {table}({', '.join(cols) or '-'})",
                     f"索引模板 {tmpl} ⇒ 期望形如 {prefix}{table}_{cols[0] if cols else '<列>'}",
                     f"索引名里的表名段必须能对上真实表名（本处为 `{table}`），否则看名字猜不到是哪张表")

    def _check_fk(self, rel, line_no, fk_name, target, db):
        tmpl = db.get("foreign_key") or ""
        if not tmpl:
            return
        parts = tmpl.replace("<table>", r"(?P<table>[a-z0-9_]+)").replace("<target>", r"(?P<target>[a-z0-9_]+)")
        if not re.fullmatch(parts, fk_name):
            self.hit(rel, line_no, "db-fk-template", f"外键约束 {fk_name} → {target}",
                     f"外键模板 {tmpl}", "外键名按模板命名；若项目统一用内联 REFERENCES（不具名），请把模板改成 `any` 并登记")

    # -- 接口路径 --------------------------------------------------------------
    def _api_scans(self, rel, lines):
        route_rx = re.compile(r"\.(?:get|post|put|patch|delete|head|options|use)\s*\(\s*[\"'`](/[^\"'`]*)")
        cfg_file = bool(re.search(r"(config|route|routers?|api|url|paths|endpoint)", rel, re.I))
        reg_rx = re.compile(r"(?:^|\s|\{|\()([A-Za-z_$][\w$]*)\s*[:=]\s*[\"'`](/[A-Za-z0-9_\-/{}.]*(?::[A-Za-z_]\w*)?[A-Za-z0-9_\-/{}.]*)['\"`]")
        repo = self._repo_key(rel)
        rinfo = self.repos.setdefault(repo, {"funcs": [], "paths": [], "params": set(), "css": []})
        seen_line: set[tuple[int, str]] = set()
        for i, line in enumerate(lines, 1):
            found = []
            for m in route_rx.finditer(line):
                found.append((m.group(1), "route"))
            if cfg_file:
                for m in reg_rx.finditer(line):
                    val = m.group(2)
                    if len(val) < 2 or any(c in val for c in " <=>") or re.search(r"/[^/]*\.\w{2,4}$", val):
                        continue
                    found.append((val, "registry"))
            for raw, src in found:
                if (i, raw) in seen_line:
                    continue
                seen_line.add((i, raw))
                self._check_path(rel, i, raw, src, rinfo)

    def _check_path(self, rel, line, raw, src, rinfo):
        want = (self.style.get("api") or {}).get("path_segment", "")
        path = raw.split("?")[0]
        segs = [s for s in path.split("/") if s]
        params = set()
        for s in segs:
            if s.startswith(":") or (s.startswith("{") and s.endswith("}")):
                params.add("colon" if s.startswith(":") else "brace")
                continue
            if want and want != "any" and not match_style(want, s):
                advice = ("路径段风格须与契约 §1.3 一致（名词复数、全小写）；改路径 = 破坏性变更，须走版本化 + `api.yaml` 同步"
                          if want == "kebab-case" else "路径段风格须与契约 §1.3 一致")
                self.hit(rel, line, "api-path-style", f"{raw}（{src}）", f"路径段 {want}",
                         advice, "无前缀" if "_" in s or any(c.isupper() for c in s) else "Low")
        rinfo["paths"].append({"file": rel, "line": line, "path": raw, "src": src,
                               "segs": [s for s in segs if not s.startswith((":", "{"))]})
        rinfo["params"] |= params

    # -- CSS 类名 -------------------------------------------------------------
    def _css_scans(self, rel, lines, rinfo):
        want = (self.style.get("other") or {}).get("css_class", "")
        rx = re.compile(r"(?<![0-9A-Za-z_#\-])\.([A-Za-z][A-Za-z0-9_-]*)")
        for i, line in enumerate(lines, 1):
            for m in rx.finditer(line):
                cls = m.group(1)
                if cls.endswith("-") or len(cls) < 2:
                    continue
                rinfo["css"].append({"file": rel, "line": i, "name": cls})
                if want and want != "any" and not match_style(want, cls):
                    self.hit(rel, i, "css-class-style", f".{cls}", f"CSS 类名 {want}",
                             "类名风格统一（改名须同步模板与 JS 里的选择器）", "Low")

    # -- 行内豁免（v5.5.0，借 dotenv-linter 的 on/off）-------------------------
    @staticmethod
    def _inline_directives(raw: str, ext: str) -> dict:
        """`style-audit: off[: rule,rule]` 起到 `style-audit: on`（或文件末）之间的行被豁免。
        返回 {行号: "*" 或 {规则名}}；用于压低单行误报，**不改契约**。

        **只在注释里生效，且该注释必须"以指令起头"**（v5.5.0 两次实测教训）：
        ① 不做"必须在注释里"的限制，`style_audit.py` 自己的模块文档串里那句
           "单行误报可用 `style-audit: off`…`style-audit: on` 就地豁免"会被当成指令，
           **把整个文件豁免掉**（自指陷阱，521 行被静默压制）；
        ② 只做①还不够——脚本里解释这条机制的**普通注释**（如 `# 行内豁免注释：style-audit: off …`）
           同样会把它后面的代码全豁免掉。故要求指令是**整条注释的开头**：
           `# style-audit: off: env-var-casing` 生效，`# 文档：style-audit: off` 不生效。
        另：同一行可有多条指令，按出现顺序生效。"""
        if ext in (".md", ".json", ".env") or ext.startswith(".env"):
            return {}
        marker = "#" if ext in (".py", ".gd", ".sh", ".ps1", ".bat", ".yaml", ".yml") else "//"
        off: dict = {}
        state = None
        for i, line in enumerate(raw.split("\n"), 1):
            cpos = _comment_pos(line, marker)
            if cpos >= 0 and line[cpos:].lstrip(marker + " \t").startswith("style-audit:"):
                for m in INLINE_SWITCH_RX.finditer(line):
                    if m.start() < cpos:
                        continue                   # 指令出现在注释之前 = 在字符串里，不算
                    if m.group(1) == "off":
                        rules = (m.group(2) or "").strip()
                        state = "*" if (not rules or rules == "*") else \
                            {r.strip() for r in rules.split(",") if r.strip()}
                    else:
                        state = None
            # 注意：豁免要覆盖区间内的**每一行**，不能只在"含注释的行"上生效
            if state:
                off[i] = state
        return off

    # -- 位置判据（v5.5.0 核心新增）------------------------------------------
    # 为什么要有它：契约 §1.3 早就写了「JSON 字段 camelCase / 环境变量 UPPER_SNAKE / 配置键小写点分 /
    # 错误码 UPPER_SNAKE / 事件名小写点分 / 缓存 key <域>:<标识>」，但脚本从不读这些判据。
    # 借 spectral `casing.ts` 的思路：**casing 是可装配的谓词，挂到"位置"上判**，而不是收集名字池猜配对。
    POSITION_WHERE = ("json_key", "env_file", "getenv", "compose_env", "error_code", "config_key",
                      "event_topic", "cache_key", "prompt_header", "prompt_registry", "route_param")
    # **文件类型也是位置的一部分**（v5.5.0 实测教训）：不按文件类型收窄采集位，
    # `env_file` 的 `KEY=` 正则会命中 Python 里的 `step = ...` 这类普通赋值——
    # 真机自检时它一次报出 3997 条假命中。位置判据的"位置"必须同时约束「什么文件 + 什么语法位置」。
    CODE_LIKE_EXTS = (".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue", ".gd", ".c", ".h")

    @staticmethod
    def _allowed_wheres(rel: str, ext: str) -> set:
        """**文件类型也是位置的一部分**（v5.5.0 实测教训）：不按文件类型收窄采集位，
        `env_file` 的 `KEY=` 正则会命中 Python 里的 `step = ...` 这类普通赋值——
        真机自检时它一次报出 3997 条假命中。位置判据的"位置"必须同时约束「什么文件 + 什么语法位置」。"""
        low = rel.lower()
        base = low.split("/")[-1]
        if base.startswith(ENV_PREFIXES):
            return {"env_file"}
        if base.startswith(COMPOSE_PREFIXES):
            return {"compose_env"}
        if ext in (".yml", ".yaml"):
            return set()
        if ext == ".json":
            return {"json_key"}            # 只判载荷类 json 的对象键（is_scope_file 已按载荷收窄）
        if ext == ".md":
            # 只判「提示词资产首行 id」与「配置中心 prompt.* 注册表」；散文里的示例不构成定档
            return ({"prompt_registry"} if "配置中心" in rel else set()) | \
                   ({"prompt_header"} if "prompt" in low else set())
        if ext in Audit.CODE_LIKE_EXTS:
            return {"getenv", "error_code", "config_key", "event_topic", "cache_key",
                    "route_param", "json_key"}
        return set()

    def _position_casing(self, pname: str) -> tuple[str, dict]:
        pos = (self.style.get("positions") or {}).get(pname) or {}
        return (pos.get("casing") or POSITION_DEFAULT_CASING.get(pname, "any")), pos

    def _env_allowed(self, name: str) -> bool:
        """平台/系统自带的环境变量不算违规（项目无权改名），可用 positions.env_var.allow 追加。"""
        pos = (self.style.get("positions") or {}).get("env_var") or {}
        return name in (set(DEFAULT_ENV_ALLOW) | {str(x) for x in (pos.get("allow") or [])})

    @staticmethod
    def _is_serialization_layer(rel: str) -> bool:
        return bool(re.search(r"(serial|response|schema|payload|dto|adapter)", rel, re.I))

    # **载荷边界**：见模块级 `looks_payload_bearing`（判不出就不判，宁可少报）
    def _payload_bearing(self, rel: str) -> bool:
        pos = (self.style.get("positions") or {}).get("json_field") or {}
        return looks_payload_bearing(rel, pos.get("files") or [])

    @staticmethod
    def _payload_lines(lines: list[str]) -> set:
        """Python 里"载荷构造窗口"：序列化调用起，遇列 0 去缩进或超 60 行止。"""
        out: set = set()
        start = 0
        for idx, line in enumerate(lines, 1):
            if start and idx > start and line[:1] not in ("", " ", "\t") and idx - start > 1:
                start = 0
            if RE_PAYLOAD_WINDOW.search(line):
                start = idx
            if start and idx - start <= 60:
                out.add(idx)
        return out

    def _positions_scans(self, rel: str, raw: str, lines: list[str], lang, ext: str):
        positions = self.style.get("positions") or {}
        if not positions:
            return
        allowed = self._allowed_wheres(rel, ext)
        if not allowed:
            return
        py_payload = self._payload_lines(lines)
        ser_layer = self._is_serialization_layer(rel)
        for pname, cfg in positions.items():
            casing, pos = self._position_casing(pname)
            if pname == "path_param" and lang and pos.get("by_lang"):
                casing = pos["by_lang"].get(lang, casing)
            if casing == "any":
                continue
            rule = POSITION_RULE.get(pname)
            if not rule:
                continue
            for where in (cfg.get("where") or []):
                if where not in self.POSITION_WHERE:
                    self.note(f"positions.{pname}.where 里的 `{where}` 是未知采集位，已忽略（可用位："
                              f"{'、'.join(self.POSITION_WHERE)}）")
                    continue
                if where not in allowed:
                    continue              # 文件类型不匹配 → 该采集位对本文件不适用
                for line, value, how in self._collect(where, lines, rel, ext, py_payload, ser_layer):
                    if not value or not isinstance(value, str):
                        continue
                    if pname == "env_var" and self._env_allowed(value):
                        continue
                    if pname == "json_field" and not self._payload_bearing(rel):
                        continue
                    if pname == "json_field" and ext == ".py" and not (ser_layer or line in py_payload):
                        continue
                    if pname == "prompt_id" and where == "prompt_registry" and "配置中心" not in rel:
                        continue
                    if match_style(casing, value):
                        continue
                    sev = "Medium" if pname in ("json_field", "env_var") else "Low"
                    self.hit(rel, line, rule, f"{value}（{how}）",
                             f"positions.{pname} = {casing}（契约 §1.3）",
                             f"按契约 §1.3 把该位置的命名改成 {casing}；"
                             f"该位置由 契约/代码风格.md §0.2 的 positions.{pname}.where 声明，改位置 = 改契约",
                             sev)

    @staticmethod
    def _in_yaml_block(lines: list, lineno: int, keys: tuple) -> bool:
        """判断第 lineno 行是否处于某个 `keys:` 块的缩进范围内（向上找第一个更浅的键）。"""
        cur = lines[lineno - 1]
        if not cur[:1].isspace():
            return False
        ind = len(cur) - len(cur.lstrip())
        for j in range(lineno - 2, -1, -1):
            line = lines[j]
            if not line.strip():
                continue
            jind = len(line) - len(line.lstrip())
            if jind >= ind:
                continue
            m = re.match(r"\s*([A-Za-z0-9_.\-]+)\s*:", line)
            return bool(m and m.group(1) in keys)
        return False

    def _collect(self, where, lines, rel, ext, py_payload, ser_layer) -> list:
        out: list = []
        js_like = ext in (".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue")
        for i, line in enumerate(lines, 1):
            if where == "env_file":
                m = RE_ENV_LINE.match(line)
                if m and not line.lstrip().startswith(("#", "//")):
                    out.append((i, m.group(1), "env 文件键"))
            elif where == "getenv":
                for m in RE_GETENV.finditer(line):
                    out.append((i, next(g for g in m.groups() if g), "取环境变量"))
            elif where == "compose_env":
                # **必须限定在 environment:/env_file: 块内**——否则 compose 里任何缩进键（`image:`、
                # `ports:`）都会被当环境变量判（实测一次报出满屏假命中）。
                if self._in_yaml_block(lines, i, ("environment", "env_file")):
                    m = re.match(r"\s+(?:-\s*)?([A-Za-z_][A-Za-z0-9_]*)\s*[:=]", line)
                    if m:
                        out.append((i, m.group(1), "compose environment"))
            elif where == "error_code":
                if not line.lstrip().startswith(("#", "//")):
                    m = RE_CODE_ASSIGN.match(line)
                    if m:
                        out.append((i, m.group(2), "错误码常量"))
                    else:
                        m = RE_CODE_JSON.search(line)
                        if m:
                            out.append((i, m.group(1), "错误码响应值"))
            elif where == "config_key":
                for m in RE_CONFIG_KEY.finditer(line):
                    out.append((i, next(g for g in m.groups() if g), "配置键"))
            elif where == "event_topic":
                for m in RE_EVENT.finditer(line):
                    out.append((i, m.group(1), "事件/topic"))
            elif where == "cache_key":
                for m in RE_CACHE_KEY.finditer(line):
                    out.append((i, m.group(1), "缓存 key"))
            elif where == "prompt_header":
                m = RE_PROMPT_HEADER.search(line)
                if m and i <= 3:
                    out.append((i, m.group(1), "提示词资产首行 id"))
            elif where == "prompt_registry":
                m = RE_PROMPT_REG.match(line)
                if m:
                    out.append((i, m.group(1)[len("prompt."):], "配置中心 prompt.* 注册表"))
            elif where == "route_param":
                for m in RE_ROUTE.finditer(line):
                    for pm in RE_ROUTE_PARAM.finditer(m.group(1)):
                        out.append((i, pm.group(1) or pm.group(2), "路由参数"))
            elif where == "json_key":
                for m in RE_JSON_KEY.finditer(line):
                    out.append((i, m.group(1), "JSON 键"))
                if js_like:
                    for m in RE_JS_BARE_KEY.finditer(line):
                        n = m.group(1)
                        if n in RE_JS_KEYWORDS or (n.islower() and "_" not in n):
                            continue          # 单个小写词不判（避免标签/关键字误报）
                        out.append((i, n, "对象键（无引号）"))
        return out

    # -- 禁用同义词（v5.5.0：契约 §0.2 synonyms 从"填了不算数"改为真判据）------
    def _synonym_scans(self, rel: str, lines: list[str]):
        """边界口径：前一个字符不能是字母数字（`userMember` 不判），后一个字符不能是**小写字母或数字**
        ——于是 `memberId` / `member_id` / `"member"` 命中，而 `membership`（后接小写 s）不命中。

        **只在代码文件里判**（同义词说的是"标识符/字段名"，不是散文）：否则契约 `代码风格.md`
        §0.2 里那行 `synonyms` 模板占位符会被自己判违规。
        **注意**：本函数自身的文档串也属"代码文件里的文本"——所以这里不写占位词字面量
        （实测：写了就会把自己报成 `synonym-banned`）。"""
        ext = Path(rel).suffix.lower()
        if ext not in self.CODE_LIKE_EXTS:
            return
        for adopted, banned in (self.style.get("synonyms") or {}).items():
            for b in (banned or []):
                b = str(b).strip()
                if not b:
                    continue
                rx = re.compile(r"(?<![A-Za-z0-9])%s(?![a-z0-9])" % re.escape(b))
                for i, line in enumerate(lines, 1):
                    if rx.search(line):
                        self.hit(rel, i, "synonym-banned", f"`{b}`",
                                 f"全项目统一用 `{adopted}`（契约 §0.2 synonyms 禁用 `{b}`）",
                                 "同义多词：改用采用名；确需并存则走 CR 统一后写进契约 §0.2 synonyms", "Medium")

    # -- 跨层转换点（v5.5.0：契约 §2 白名单**真正生效**）----------------------
    def is_conversion_point(self, rel: str) -> bool:
        conv = self.style.get("conversion") or {}
        low = rel.lower().replace("\\", "/")
        for c in [str(x) for x in (list(conv.get("modules") or []) + list(conv.get("files") or []))]:
            c = c.strip().strip("/").lower()
            if not c:
                continue
            if low == c or low.endswith("/" + c) or ("/" + c + "/") in ("/" + low + "/") or c in low:
                return True
        return False

    def _conversion_scans(self, rel: str, lines: list[str], lang):
        """同一文件里 `cover_sub` 与 `coverSub` 并存：
        **只在该文件不是 §2 白名单转换点时**才报——白名单里的双写正是它的职责。
        这是对旧实现（grep `to_camel`/`camelize` **猜**有没有转换设施）的替换：现在读契约填的名单。"""
        if not lang:
            return
        snake_at, camel_at = {}, {}
        for i, line in enumerate(lines, 1):
            for m in RE_JSON_KEY.finditer(line):
                n = m.group(1)
                if re.fullmatch(r"[a-z][a-z0-9]*_[a-z0-9_]*", n):
                    snake_at.setdefault(n, i)
                elif re.fullmatch(r"[a-z][a-zA-Z0-9]*", n) and re.search(r"[a-z][A-Z]", n):
                    camel_at.setdefault(n, i)
        if not (snake_at and camel_at):
            return
        in_conv = self.is_conversion_point(rel)
        for sname, line in sorted(snake_at.items()):
            parts = sname.split("_")
            cname = parts[0] + "".join(p[:1].upper() + p[1:] for p in parts[1:])
            if cname not in camel_at or in_conv:
                continue
            self.hit(rel, line, "json-crosslayer-drift", f"`{sname}` 与 `{cname}` 同文件并存",
                     f"跨层转换只允许在契约 §2 白名单（conversion.modules/files）",
                     "疑似在业务代码里手写改名（DB 层 snake、JSON 层 camel 同文件双写）——"
                     "要么走序列化层，要么把该文件登记进 §2 白名单 / §4 例外", "Medium")

    def conversion_blindspot(self):
        """契约 §2 白名单里声明的转换点，代码树里没有对应模块 → 报（治"填了不算数"的反面：
        契约承诺了一个不存在的转换点）。`--since` 增量模式下不跑（那是项目级结构判据）。"""
        if self.since:
            return
        conv = self.style.get("conversion") or {}
        declared = [str(x).strip().strip("/") for x in (list(conv.get("modules") or []) + list(conv.get("files") or []))]
        seen = " || ".join(self.seen_paths).lower()
        for c in declared:
            if c and c.lower() not in seen:
                self.hit("(项目根)", 0, "conversion-blindspot", f"契约 §2 声明了转换点 `{c}`",
                         "契约 §2 的 conversion.modules/files 应在代码树里有对应模块",
                         "要么建该模块（它就是唯一的转换点），要么把这项从契约 §2 删掉——"
                         "写了不存在的转换点，等于域8 检查点 2 无法判", "Low")

    # -- 基础设施文件（v5.5.0）------------------------------------------------
    def _scan_dockerfile(self, rel: str, raw: str):
        infra = self.style.get("infra") or {}
        for i, line in enumerate(raw.split("\n"), 1):
            if line.lstrip().startswith("#"):
                continue
            m = RE_DOCKER_FROM.match(line)
            if m and infra.get("docker_stage") and infra["docker_stage"] != "any" \
                    and not match_style(infra["docker_stage"], m.group(1)):
                self.hit(rel, i, "docker-stage", f"AS {m.group(1)}",
                         f"infra.docker_stage = {infra['docker_stage']}",
                         "多阶段构建的 stage 名统一，便于 --target 引用与缓存复用")
            m = RE_DOCKER_ENV.match(line)
            if m and infra.get("docker_env_key") and infra["docker_env_key"] != "any":
                key = m.group(1).split("=")[0]
                if not self._env_allowed(key) and not match_style(infra["docker_env_key"], key):
                    self.hit(rel, i, "docker-env-key", key, f"infra.docker_env_key = {infra['docker_env_key']}",
                             "Dockerfile 的 ENV/ARG/LABEL 键与项目环境变量定档保持一致（契约 §1.3 env_var）")

    def _scan_env_file(self, rel: str, raw: str):
        self._positions_scans(rel, raw, raw.split("\n"), None, ".env")

    def _scan_compose(self, rel: str, raw: str):
        infra = self.style.get("infra") or {}
        lines = raw.split("\n")
        svc_indent = None
        for i, line in enumerate(lines, 1):
            m = RE_YAML_KEY.match(line)
            if not m:
                continue
            indent, key = len(m.group(1)), m.group(2)
            if key == "services" and svc_indent is None:
                svc_indent = indent
                continue
            if svc_indent is not None and indent <= svc_indent:
                svc_indent = None
            if svc_indent is not None and indent == svc_indent + 2 and infra.get("compose_service") \
                    and infra["compose_service"] != "any":
                if not match_style(infra["compose_service"], key):
                    self.hit(rel, i, "compose-service", key, f"infra.compose_service = {infra['compose_service']}",
                             "compose 服务名统一（也是容器名/网络别名的来源）")
        self._positions_scans(rel, raw, lines, None, ".yml")

    def _scan_ci(self, rel: str, raw: str):
        infra = self.style.get("infra") or {}
        jobs_indent = None
        for i, line in enumerate(raw.split("\n"), 1):
            m = RE_YAML_KEY.match(line)
            if not m:
                continue
            indent, key = len(m.group(1)), m.group(2)
            if key == "jobs" and jobs_indent is None:
                jobs_indent = indent
                continue
            if jobs_indent is not None and indent <= jobs_indent:
                jobs_indent = None
            if jobs_indent is not None and indent == jobs_indent + 2 and infra.get("ci_job") \
                    and infra["ci_job"] != "any":
                if not match_style(infra["ci_job"], key):
                    self.hit(rel, i, "ci-job", key, f"infra.ci_job = {infra['ci_job']}",
                             "CI job 名统一（分支保护与必需检查项按名引用，改名要同步服务端设置）")

    def _vue_template_scans(self, rel: str, lines: list[str]):
        want = (self.style.get("other") or {}).get("css_class", "any")
        if not want or want == "any":
            return
        for i, line in enumerate(lines, 1):
            for m in RE_CLASS_ATTR.finditer(line):
                for cls in m.group(1).split():
                    if cls and not match_style(want, cls):
                        self.hit(rel, i, "css-class-style", f".{cls}", f"CSS 类名 {want}",
                                 "类名风格统一（改名须同步模板与 JS 里的选择器）", "Low")

    # -- GDScript / C / shell（v5.5.0：game 域与基础设施语言补上）--------------
    def _gd_scans(self, rel, lines, lstyle, rinfo):
        """GDScript：`class_name X` / `func name(` / `const NAME`。
        **常量分两种**（Godot 官方风格指南）：纯值常量用 `UPPER_SNAKE`，而持有 `preload()`/`load()`
        引用（类/场景/资源）的常量用 `PascalCase`；混判会把 Godot 项目满屏误报（实测 65 条）。"""
        class_rx = re.compile(r"^\s*class_name\s+([A-Za-z_]\w*)")
        inner_cls_rx = re.compile(r"^\s*class\s+([A-Za-z_]\w*)")
        func_rx = re.compile(r"^\s*(?:static\s+)?func\s+([A-Za-z_]\w*)")
        const_rx = re.compile(r"^\s*const\s+([A-Za-z_]\w*)")
        for i, line in enumerate(lines, 1):
            m = class_rx.match(line) or inner_cls_rx.match(line)
            if m and not match_style(lstyle.get("class", "PascalCase"), m.group(1)):
                self.hit(rel, i, "class-naming", f"class {m.group(1)}",
                         f"类名 {lstyle.get('class', 'PascalCase')}", "改为 PascalCase")
            m = func_rx.match(line)
            if m:
                self._check_func(rel, i, m.group(1), lstyle, rinfo, GD_LANG)
            m = const_rx.match(line)
            if m:
                is_ref = bool(re.search(r"\b(?:pre)?load\s*\(", line))
                want = "PascalCase" if is_ref else lstyle.get("const", "UPPER_SNAKE")
                if not match_style(want, m.group(1)):
                    self.hit(rel, i, "const-naming", f"const {m.group(1)} = <{'preload/load 引用' if is_ref else '字面量'}>",
                             f"常量 {want}（Godot：preload 引用类/场景用 PascalCase，纯值用 UPPER_SNAKE）",
                             "按 Godot 官方风格指南改名（引用型常量 PascalCase / 值型 UPPER_SNAKE）", "Low")

    def _c_scans(self, rel, lines, lstyle, rinfo):
        """C/C++：只判三类安全项——`#define` 常量、顶层函数定义、`typedef struct` 名。
        函数判定要求"列 0 起、以 `)` 或 `{` 结尾、不以 `;` 结尾"，避免把顶层调用语句当定义。"""
        define_rx = re.compile(r"^\s*#\s*define\s+([A-Za-z_]\w*)")
        typedef_rx = re.compile(r"^\s*typedef\s+(?:struct|union|enum)\s+(?:[A-Za-z_]\w*\s*)?\{?")
        func_rx = re.compile(r"^[A-Za-z_][\w \t*]*?\b([a-z_][A-Za-z0-9_]*)\s*\([^;]*\)\s*\{?\s*$")
        skip_kw = {"if", "for", "while", "switch", "return", "else", "do", "case", "sizeof"}
        for i, line in enumerate(lines, 1):
            m = define_rx.match(line)
            if m and not match_style(lstyle.get("const", "UPPER_SNAKE"), m.group(1)):
                self.hit(rel, i, "const-naming", f"#define {m.group(1)}",
                         f"宏常量 {lstyle.get('const', 'UPPER_SNAKE')}", "宏名改 UPPER_SNAKE", "Low")
                continue
            if typedef_rx.match(line):
                continue
            m = func_rx.match(line)
            if m and m.group(1) not in skip_kw:
                self._check_func(rel, i, m.group(1), lstyle, rinfo, "c")

    def _shell_scans(self, rel, lines):
        """shell/bat 函数名（PowerShell 的动宾命名另域，默认不判，见契约 §0.2 languages.powershell）。"""
        infra = self.style.get("infra") or {}
        want = infra.get("shell_func", "any")
        if not want or want == "any":
            return
        rx = re.compile(r"^\s*(?:function\s+)?([A-Za-z_][\w\-]*)\s*\(\s*\)\s*\{?\s*$")
        for i, line in enumerate(lines, 1):
            if line.lstrip().startswith("#"):
                continue
            m = rx.match(line)
            if not m:
                continue
            name = m.group(1)
            if name in ("if", "for", "while", "case", "do", "done", "then", "fi", "main"):
                continue
            if not match_style(want, name):
                self.hit(rel, i, "shell-func", f"{name}()", f"shell 函数名 {want}",
                         "脚本函数名按 infra.shell_func 定档（两个运维脚本互相 source 时尤其要一致）", "Low")

    # -- 跨层字段池 -----------------------------------------------------------
    def _pool_scans(self, rel, lines):
        """收集"字段名"池：对象键（`"x":` / `x=` / `['x']`）与取值访问（`.get("x")` / `["x"]`）。
        池只用于 camel↔snake 配对，宽一点无害；配对结果一律标"需人工复核"。"""
        rxs = (
            re.compile(r"[\"'`]([a-z][A-Za-z0-9_]{2,})[\"'`]\s*(?::|=>|\]|=)"),
            re.compile(r"[\"'`]([a-z][A-Za-z0-9_]{2,})[\"'`]\s*\]"),
            re.compile(r"\.(?:get|query|has|pop|set|delete)\s*\(\s*[\"'`]([a-z][A-Za-z0-9_]{2,})[\"'`]"),
        )
        for line in lines:
            for rx in rxs:
                for m in rx.finditer(line):
                    n = m.group(1)
                    if "_" in n and re.fullmatch(r"[a-z][a-z0-9_]*", n):
                        self.snake_pool.setdefault(n, set()).add(rel)
                    elif re.search(r"[a-z][A-Z]", n):
                        self.camel_pool.setdefault(n, set()).add(rel)

    # -- 跨文件：目录命名 / 函数混用 / 路径混用 --------------------------------
    def cross_checks(self, files: list[Path]):
        # 目录命名 + 同级风格混用
        dir_seen: dict[str, dict[str, list[str]]] = {}
        want_dir = (self.style.get("other") or {}).get("dir", "")
        for p in files:
            rel = rel_of(p, self.root)
            parts = rel.split("/")[:-1]
            for idx, d in enumerate(parts):
                if d.startswith((".", "_")) or not d.isascii():
                    continue
                parent = "/".join(parts[:idx]) or "."
                info = dir_seen.setdefault(parent, {"kebab": [], "snake": [], "bad": []})
                if "-" in d:
                    info["kebab"].append(d)
                elif "_" in d:
                    info["snake"].append(d)
                elif want_dir and want_dir != "any" and not match_style(want_dir, d):
                    info["bad"].append(d)
        for parent, info in dir_seen.items():
            for d in sorted(set(info["bad"])):
                self.hit(parent, 0, "dir-naming", f"目录 {d}/", f"目录名 {want_dir}",
                         "目录名风格须与契约一致（改目录属结构变更，先出方案报主控）", "Low")
            if info["kebab"] and info["snake"]:
                self.hit(parent, 0, "dir-style-mixed",
                         f"同级目录 {sorted(set(info['kebab']))} 与 {sorted(set(info['snake']))}",
                         "同级目录统一用 kebab-case 或 snake_case（择一）",
                         "两种风格并存 = 半年后没人知道新目录该叫什么")

        # 同目录函数风格混用（按「目录 × 语言」分组，≥2 侧各 ≥2 个才报，避免单名误报与跨语言误报）
        for repo, rinfo in self.repos.items():
            by_dir: dict[tuple[str, str], list[dict]] = {}
            for f in rinfo["funcs"]:
                d = f["file"].rsplit("/", 1)[0] if "/" in f["file"] else "."
                by_dir.setdefault((d, f["lang"]), []).append(f)
            for (d, lang), fs in sorted(by_dir.items()):
                camel = [f for f in fs if f["kind"] == "camel"]
                snake = [f for f in fs if f["kind"] == "snake"]
                if len(camel) >= 2 and len(snake) >= 2:
                    ex_c = "、".join(sorted({f"`{f['name']}()`" for f in camel})[:3])
                    ex_s = "、".join(sorted({f"`{f['name']}()`" for f in snake})[:3])
                    self.hit(d, 0, "func-style-mixed",
                             f"[{lang}] camel {len(camel)} 个（{ex_c}…）与 snake {len(snake)} 个（{ex_s}…）并存",
                             f"同一目录的 {lang} 函数名风格统一（按契约 §1.1 的该语言定档）",
                             "风格漂移：按定档改一侧；一侧若属历史遗留，登记契约 §4 例外或建专项卡")
            # 同仓路径风格混用
            kebab = [p for p in rinfo["paths"] if any("-" in s for s in p["segs"])]
            snake = [p for p in rinfo["paths"] if any("_" in s for s in p["segs"])]
            if kebab and snake:
                self.hit(repo, 0, "api-path-mixed",
                         f"连字符路径（{kebab[0]['path']}）与下划线路径（{snake[0]['path']}）并存",
                         "同一仓内路径风格统一（契约 §1.3）",
                         "同一套接口两种拼法 = 前端路由表与文档对不上")
            if len(rinfo["params"]) > 1:
                self.hit(repo, 0, "api-param-mixed",
                         "同仓同时出现 `:id` 与 `{id}` 两种路径参数写法", "同一仓统一一种（框架语法强制的除外）",
                         "Express 用 `:id`、FastAPI 用 `{id}` 属框架强制；若同仓两套写法混用才需统一", "Low")
            css_kebab = [c for c in rinfo["css"] if "-" in c["name"]]
            css_snake = [c for c in rinfo["css"] if "_" in c["name"]]
            if css_kebab and css_snake:
                self.hit(repo, 0, "css-class-mixed",
                         f"`.{css_kebab[0]['name']}` 与 `.{css_snake[0]['name']}` 并存", "类名风格统一（契约 §1.3）",
                         "同一套样式两种拼法 = 改名字改不干净")

        # 跨层漂移：camel ↔ snake 两版并存
        # v5.5.0：**同文件**并存交给 `_conversion_scans`（它读契约 §2 白名单，白名单内的双写不报）；
        # 这里只处理**跨文件**并存，并且同样看白名单——跨层转换的正常形态就是"DB 层 snake 在 models/、
        # JSON 层 camel 在序列化层"，两侧都不在白名单才是可疑漂移。
        pairs = []
        for cname, cfiles in self.camel_pool.items():
            sname = camel_to_snake(cname)
            if sname in self.snake_pool:
                pairs.append({"camel": cname, "snake": sname, "camel_files": sorted(cfiles),
                              "snake_files": sorted(self.snake_pool[sname])})
        other = [p for p in pairs if not (set(p["camel_files"]) & set(p["snake_files"]))]
        reported = 0
        for p in other:
            if reported >= 10:
                break
            conv_side = [f for f in (p["snake_files"] + p["camel_files"]) if self.is_conversion_point(f)]
            if conv_side:
                continue                      # 有一侧是 §2 白名单转换点 → 这正是它该干的事
            reported += 1
            has_facility = bool(self.conv_facilities)
            sev = "Low" if has_facility else "Medium"
            adv = ("该仓存在转换设施，请确认这两个名字确实是同一字段（若确是，登记 §2 转换点或 §4 例外）；"
                   "若两侧都不是 §2 白名单转换点，说明转换没有落在指定位置"
                   if has_facility else
                   "两侧都不在契约 §2 白名单，且未发现任何转换设施——请人工复核是否同一字段的两套名字")
            self.hit(p["snake_files"][0], 0, "json-crosslayer-drift",
                     f"`{p['snake']}`（{p['snake_files'][0]}）与 `{p['camel']}`（{p['camel_files'][0]}）两版并存",
                     "同一字段跨层只保留一种拼写 + 一处转换点（契约 §2）", adv, sev)
        if len(pairs) > 35:
            self.note(f"跨层候选对共 {len(pairs)} 组，报告只列前 {reported} 组（其余按同一规则处理）")


# ---------------------------------------------------------------------------
# 6. 报告
# ---------------------------------------------------------------------------


def build_report(a: Audit, root: Path, style_src: str, scope_desc: str, dirs: list[Path],
                 skipped: tuple[list[str], list[str], list[str]]) -> dict:
    by_rule: dict[str, int] = {}
    by_family: dict[str, int] = {}
    for f in a.findings:
        by_rule[f["rule"]] = by_rule.get(f["rule"], 0) + 1
        by_family[f["family"]] = by_family.get(f["family"], 0) + 1
    sk_dirs, sk_big, sk_pat = skipped
    return {
        "workspace": str(root),
        "generated": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "style_source": style_src,
        "scope": scope_desc,
        "scope_dirs": [rel_of(d, root) for d in dirs],
        "since": a.since,
        "files_scanned": a.files_scanned,
        "findings": a.findings,
        "summary_by_rule": [{"rule": k, "label": RULE_LABEL.get(k, k), "count": v}
                            for k, v in sorted(by_rule.items(), key=lambda x: -x[1])],
        "summary_by_family": [{"family": k, "count": v}
                              for k, v in sorted(by_family.items(), key=lambda x: -x[1])],
        "findings_total": len(a.findings),
        "suppressed_by_exception": [{"file": s["file"], "line": s["line"], "rule": s["rule"],
                                     "rule_label": s["rule_label"], "current": s["current"],
                                     "due": (s.get("exception") or {}).get("due", "")} for s in a.suppressed],
        "suppressed_by_inline": a.inline_suppressed,
        "expired_exceptions": len(a.expired),
        "skipped_dirs": sk_dirs, "skipped_big_files": sk_big, "skipped_pattern_files": sk_pat,
        "extension_histogram": dict(sorted(a.ext_counter.items(), key=lambda x: -x[1])),
        "notes": a.notes,
        "not_covered": [
            "局部变量与函数参数命名（无 AST 时正则误报率高，不做）",
            "命名语义质量（是否表意、是否名实相符，需人工判断）",
            "注释/文档里引用的名字与代码是否同步",
            "含中日韩字符的文件名/目录名不做风格判定（框架自身与中文项目大量使用中文名，判了全是噪音）",
            "**json_field 只在「可判定的载荷边界」内判**：目录名 ∈ api/clients/serializers/responses/dtos/adapters、"
            "或文件名以 serial|response|payload|dto|adapter 开头 / 含 client；其余位置（如 services/ 业务逻辑层、"
            "配置与数据 .json、证据快照）**不判**——判不出载荷边界就宁可不报，避免把合法 snake 键判成违规。"
            "需要扩大范围时在契约 §0.2 的 positions.json_field.files 显式加白",
            "JS 测试的描述式命名（it/describe 文本无固定风格）",
            "跨行拼接或运行时拼出的 SQL（只解析标准写法 CREATE TABLE / ADD COLUMN / CREATE INDEX）",
            "Python 三引号字符串内的 SQL（不做字符串剥离，可能误报，见 §报告说明）",
            "C/C++ 只判 `#define` 常量与顶层函数定义，不判结构体成员/局部变量/宏参数（需要预处理器语义）",
            "PowerShell 的动宾函数名（Get-Xxx）默认不判——需项目在契约 §0.2 的 languages.powershell.func 显式定档",
            "代码格式（缩进/引号/行宽）——交给 black / prettier，本脚本不重复造轮子",
        ],
    }


def render(rep: dict) -> str:
    L: list[str] = []
    L.append(f"# 代码风格盘点 · {Path(rep['workspace']).name}")
    L.append("")
    L.append(f"> 时间：{rep['generated']}　**判据来源：{rep['style_source']}**")
    L.append(f"> 扫描范围：{rep['scope']} → {'、'.join('`' + d + '`' for d in rep['scope_dirs'])}")
    if rep.get("since"):
        L.append(f"> 增量口径：**--since {rep['since']}**（只判本次新增/改动文件；存量命中不在本报告）")
    L.append(f"> 纪律：**只报告不改动**——改名/归位属结构变更，由主控裁决后执行（契约 `代码风格.md` §0.3）。")
    L.append(f"")
    L.append(f"## 一、结论摘要")
    L.append("")
    L.append(f"- 扫描文件：**{rep['files_scanned']}** 个　命中：**{rep['findings_total']}** 处　"
             f"已登记例外抑制：{len(rep['suppressed_by_exception'])} 处　"
             f"行内豁免：{len(rep.get('suppressed_by_inline') or [])} 处　例外过期：{rep['expired_exceptions']} 处")
    if rep.get("summary_by_family"):
        fam_label = {"STY": "命名风格", "CFG": "位置类配置（JSON 字段/环境变量/配置键/错误码/事件/缓存/提示词 id）",
                     "CVT": "跨层转换与同义词", "INF": "基础设施文件（Dockerfile/compose/CI/shell）",
                     "META": "例外治理", "OTH": "其他"}
        L.append("- 按族：" + "、".join(
            f"{fam_label.get(s['family'], s['family'])} {s['count']}" for s in rep["summary_by_family"]))
    if rep["skipped_dirs"]:
        L.append(f"- 跳过目录（第三方/构建/运行期/**非源码树**）：{'、'.join('`' + d + '`' for d in rep['skipped_dirs'][:12])}"
                 + ("…" if len(rep["skipped_dirs"]) > 12 else ""))
    if rep["skipped_big_files"]:
        L.append(f"- 跳过超大文件（>{len(rep['skipped_big_files'])} 个）：{'、'.join('`' + d + '`' for d in rep['skipped_big_files'][:5])}")
    if rep["summary_by_rule"]:
        L.append("")
        L.append("| 规则 | 命中 |")
        L.append("|---|---|")
        for s in rep["summary_by_rule"]:
            L.append(f"| {s['label']}（`{s['rule']}`） | {s['count']} |")
    else:
        L.append("")
        L.append("（无命中 ✅）")
    if rep["notes"]:
        L.append("")
        for n in rep["notes"]:
            L.append(f"> ⚠ {n}")

    L.append("")
    L.append("## 二、命中明细（按文件分组）")
    L.append("")
    if not rep["findings"]:
        L.append("（无）")
    else:
        cur = None
        for f in sorted(rep["findings"], key=lambda x: (x["file"], x["line"])):
            if f["file"] != cur:
                cur = f["file"]
                L.append("")
                L.append(f"### `{cur}`")
                L.append("")
                L.append("| 行 | 规则 | 现状 | 期望 | 建议 | 严重度 |")
                L.append("|---|---|---|---|---|---|")
            L.append(f"| {f['line'] or '-'} | {f['rule_label']} | {f['current']} | {f['expected']} | "
                     f"{f['advice']} | {f['severity']} |")

    L.append("")
    L.append(f"## 三、已登记例外（不再报告；共 {len(rep['suppressed_by_exception'])} 处）")
    L.append("")
    if rep["suppressed_by_exception"]:
        L.append("| 文件 | 行 | 规则 | 现状 | 到期 |")
        L.append("|---|---|---|---|---|")
        for s in rep["suppressed_by_exception"]:
            L.append(f"| `{s['file']}` | {s['line'] or '-'} | {s['rule_label']} | {s['current']} | {s['due'] or '-'} |")
        L.append("")
        L.append("> 例外不是永久豁免：到期前须裁决「改代码 / 续期（再报用户）/ 升格为正式风格」，见契约 §4。")
    else:
        L.append("（无）")

    inline = rep.get("suppressed_by_inline") or []
    L.append("")
    L.append(f"## 三之二、行内豁免（`style-audit: off` 就地压制；共 {len(inline)} 处）")
    L.append("")
    if inline:
        L.append("| 文件 | 行 | 规则 | 现状 |")
        L.append("|---|---|---|---|")
        for s in inline:
            L.append(f"| `{s['file']}` | {s['line'] or '-'} | {s['rule_label']} | {s['current']} |")
        L.append("")
        L.append("> 行内豁免用在**单行误报**上（颗粒度到行），与契约 §4 例外表（颗粒度到文件/规则、需理由+批准人+到期）"
                 "是两套：**行内豁免不写理由等于把违规藏起来**——验收时集成工程师会对照本表复核，"
                 "请在该行注释里写清为什么豁免。")
    else:
        L.append("（无）")

    L.append("")
    L.append("## 四、未覆盖项（**本脚本不做，不假装覆盖**）")
    L.append("")
    for n in rep["not_covered"]:
        L.append(f"- {n}")

    L.append("")
    L.append("## 五、处置建议（主控执行）")
    L.append("")
    L.append("1. **新增代码命中的 → 打回该卡修复**（对照契约 §3.3 提交前清单）；")
    L.append("2. **存量命中的 → 不改本轮历史代码**（范围纪律）：登记契约 §4 例外（带到期）或立专项重构卡；")
    L.append("3. **跨层漂移提示 → 交集成工程师人工复核**（域 8 检查点 2/5），确认后落 `问题/审核记录.md`；")
    L.append("4. 结论与处置写 `运行日志.md`（与 `path_audit.py` 同为批次收口执法动作）。")
    return "\n".join(L)


def changed_since(root: Path, ref: str) -> tuple[set[str], str]:
    """`--since <ref>`：取"相对该 ref 的新增/改动文件"（含未提交与未跟踪）。
    **必须是 `git diff --name-only <ref>`**（与工作区比），不能写 `<ref> HEAD`——后者是两次提交之间比，
    `HEAD..HEAD` 恒为空集，会静默漏掉全部改动。"""
    try:
        a = subprocess.run(["git", "-C", str(root), "diff", "--name-only", ref],
                           capture_output=True, text=True, timeout=60)
        b = subprocess.run(["git", "-C", str(root), "ls-files", "--others", "--exclude-standard"],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return set(), f"--since 取 git 变更集失败（{e}），退化为全量扫描"
    if a.returncode != 0:
        return set(), (f"--since 取 git 变更集失败（git diff rc={a.returncode}: "
                       f"{(a.stderr or '').strip()[:120]}），退化为全量扫描")
    out = {x.strip().replace("\\", "/") for x in (a.stdout + "\n" + b.stdout).splitlines() if x.strip()}
    return out, ""


def main() -> int:
    ap = argparse.ArgumentParser(description="代码风格盘点（只报告，不改动）")
    ap.add_argument("project_root")
    ap.add_argument("--style-file", default=None, help="契约 代码风格.md（默认 <项目根>/project-state/契约/代码风格.md）")
    ap.add_argument("--scope", action="append", default=[], help="只扫指定相对路径（可多次）")
    ap.add_argument("--since", default=None,
                    help="git ref：只报本次新增/改动文件的命中（本卡新增口径，如 --since origin/master）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--max-file-kb", type=float, default=2048.0)
    args = ap.parse_args()

    root = Path(args.project_root).expanduser().resolve()
    if not root.is_dir():
        print(f"[错误] 项目根不存在：{root}")
        return 2
    if args.style_file:
        sf = Path(args.style_file)
        sf = sf if sf.is_absolute() else (root / sf)
        if not sf.is_file():
            print(f"[错误] 指定的契约文件不存在：{sf}")
            return 2
    else:
        sf = root / "project-state" / "契约" / "代码风格.md"
    style, style_src, exceptions = load_style(sf) if sf.is_file() else (
        DEFAULT_STYLE, "框架默认风格（未找到 project-state/契约/代码风格.md）", [])
    vocab_bad = collect_unknown_casings(style)          # 契约里的 casing 词表校验（拼错的风格词不静默）
    if vocab_bad:
        style_src += f"　⚠ 契约里有 {len(vocab_bad)} 处 casing 词不在词表内（已按**不判**处理）：{'；'.join(vocab_bad[:5])}"

    dirs, scope_desc = resolve_scope(root, args.scope)
    if not dirs:
        print(f"[错误] 扫描范围为空（--scope 指定的路径都不存在）")
        return 2
    files, sk_dirs, sk_big, sk_pat = walk_files(dirs, root, args.max_file_kb)
    a = Audit(root, style, exceptions, dirs, since=args.since)
    if args.since:
        changed, warn = changed_since(root, args.since)
        if warn:
            a.note(warn)
            a.since = None                    # 取不到变更集 → 退回全量并声明
        else:
            before = len(files)
            files = [p for p in files if rel_of(p, root) in changed]
            a.note(f"--since {args.since}：{before} 个扫描文件里 **{len(files)} 个**属于本次新增/改动，"
                   f"其余存量不判（本卡新增口径，契约 §3.3）")
    if not files:
        rep = build_report(a, root, style_src, scope_desc, dirs, (sk_dirs, sk_big, sk_pat))
        print(json.dumps(rep, ensure_ascii=False, indent=2) if args.json else render(rep))
        print(f"\n[提示] 范围内没有可扫描的源码文件（非代码目录 / 空目录 / 本卡无新增）——"
              f"判据与范围已打印，不做任何改动。", file=sys.stderr)
        return 0
    for p in files:
        a.scan_file(p)
    a.cross_checks(files)
    a.conversion_blindspot()
    rep = build_report(a, root, style_src, scope_desc, dirs, (sk_dirs, sk_big, sk_pat))
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(render(rep))
    return 1 if a.findings else 0


if __name__ == "__main__":
    sys.exit(main())

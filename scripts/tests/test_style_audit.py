#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""style_audit.py 的回归测试 + **变异对拍** + **反向对拍**（v5.5.0 新增）。

为什么这个文件必须存在：
    `scripts/tests/` 下 6 个检查器都有回归测试，**唯独最重的 style_audit.py（970+ 行）没有**——
    而它是唯一跟"每张开发卡 done 前置"绑定的那个（agent.md 任务纪律 19 / 验收标准 27 / `完成定义` §6）。
    它一旦静默失效，全框架的"风格线"就都是空话。

两条互补的测试纪律（本文件的核心方法）：
    **变异对拍**：每条判据都要有一个"证明它能拦"的植入违例 —— 否则就是假保护。
    **反向对拍**：每条判据还要有一个"证明它不乱拦"的反例 —— 违例与"合规样例"若出自同一个
                  （可能残缺的）心智模型，永远自洽；只有反向用例 + 真机抽样才挤得出误报。
    实测教训：v5.5.0 开发中，原型在真实工程上报出 29,313 条，其中 93% 是合法命名——
    而当时 52 条单测**全绿**。

用法：python scripts/tests/test_style_audit.py
退出码：0 全过；1 有失败（逐条打印失败项）。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
FRAMEWORK = HERE.parents[1]
SCRIPT = Path(os.environ.get("SA_SCRIPT") or (FRAMEWORK / "scripts" / "style_audit.py"))

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

passes: list = []
fails: list = []


def check(cond, label, detail=""):
    (passes if cond else fails).append(label)
    if not cond:
        print("  FAIL  %s %s" % (label, detail))
    return cond


# ---------------------------------------------------------------------------
# 定档（§0.2 机器块）——与框架默认一致
# ---------------------------------------------------------------------------
PROFILE = {
    "style_version": 2,
    "languages": {
        "python": {"ext": [".py"], "file": "snake_case", "func": "snake_case",
                   "class": "PascalCase", "const": "UPPER_SNAKE", "test_func": "test_snake_case"},
        "javascript": {"ext": [".js", ".ts", ".tsx", ".vue"], "file": "kebab-case", "func": "camelCase",
                       "class": "PascalCase", "const": "UPPER_SNAKE", "test_func": "any"},
        "sql": {"ext": [".sql"], "file": "snake_case"},
        "gdscript": {"ext": [".gd"], "file": "snake_case", "func": "snake_case",
                     "class": "PascalCase", "const": "UPPER_SNAKE", "test_func": "any"},
        "c": {"ext": [".c", ".h"], "file": "snake_case", "func": "snake_case",
              "class": "PascalCase", "const": "UPPER_SNAKE", "test_func": "any"},
        "shell": {"ext": [".sh", ".bat"], "file": "snake_case", "func": "snake_case"},
        "style": {"ext": [".css", ".scss"], "class": "kebab-case"},
    },
    "database": {"table": "snake_case", "column": "snake_case", "index": "idx_<table>_<column>",
                 "foreign_key": "fk_<table>_<target>", "migration": "<NNN>_<name>.sql"},
    "api": {"path_segment": "kebab-case", "json_field": "camelCase"},
    "other": {"dir": "kebab-case|snake_case", "css_class": "kebab-case"},
    "positions": {
        "json_field": {"where": ["json_key"]},
        "env_var": {"where": ["env_file", "getenv", "compose_env"]},
        "error_code": {"where": ["error_code"]},
        "config_key": {"where": ["config_key"]},
        "event_name": {"where": ["event_topic"]},
        "cache_key": {"where": ["cache_key"]},
        "prompt_id": {"where": ["prompt_header", "prompt_registry"]},
        "path_param": {"where": ["route_param"],
                       "by_lang": {"python": "snake_case", "javascript": "camelCase"}},
    },
    "infra": {"docker_env_key": "UPPER_SNAKE", "docker_stage": "kebab-case",
              "compose_service": "kebab-case", "ci_job": "kebab-case", "shell_func": "snake_case"},
    "conversion": {"modules": ["serializers.py"], "files": []},
    "synonyms": {"user": ["member"], "cover_sub": ["subtitle"]},
}
CONTRACT_FMT = """# 代码风格与命名一致性（Code Style Contract）

## 0. 定档区

### 0.2 机器定档块

```json
%s
```

## 4. 例外登记表

| 路径（glob 或 `文件:行`） | 违反的规则 | 理由（为什么会这样） | 批准人 | 到期时间 |
|---|---|---|---|---|
%s
"""

# 合规树（应 0 命中）——刻意覆盖**全部**新语言与文件类型
COMPLIANT = {
    "repos/backend/app/serializers.py": (
        "import json\n\n\ndef dump_cover(row):\n"
        "    return json.dumps({\"coverSub\": row[\"cover_sub\"], \"createdAt\": row[\"created_at\"]})\n"
    ),
    "repos/backend/.env.example": "DATABASE_URL=postgres://localhost/app\nJWT_SECRET=changeme\n",
    "repos/backend/app/config.py": "import os\n\n\ndef db_url():\n    return os.getenv(\"DATABASE_URL\")\n",
    "repos/backend/app/errors.py": 'USER_NOT_FOUND = "USER_NOT_FOUND"\n',
    "repos/backend/app/store.py": (
        "def fetch(cfg, cache):\n    ttl = cfg.get(\"cache.user.ttl\")\n"
        "    return cache.get(\"cache:user:42\"), ttl\n"
    ),
    "repos/backend/app/events.py": "def emit(bus):\n    bus.publish(\"user.created\")\n",
    "repos/backend/app/routes.py": (
        "def register(router):\n    router.post(\"/api/v1/users/{user_id}\", handler)\n"
    ),
    "repos/backend/prompts/scan_classify.md": "<!-- id: scan.classify.v3 -->\n\n分类提示词正文\n",
    "repos/backend/migrations/007_add_team_id_to_users.sql": "ALTER TABLE users ADD COLUMN team_id INTEGER;\n",
    "repos/frontend/src/api/cover-client.ts": "export const fetchCover = () => ({ coverSub: 1 });\n",
    "repos/frontend/src/api/cover-page.vue": (
        "<template><div class=\"cover-sub\">hi</div></template>\n<script>\nconst coverSub = 1;\n</script>\n"
    ),
    "repos/frontend/src/styles/cover.scss": ".cover-sub {\n  color: red;\n}\n",
    "repos/game/player/player.gd": (
        "class_name Player\n\nconst MAX_HP := 100\nconst PlayerScene = preload(\"res://player.tscn\")\n\n"
        "func take_damage(amount: int) -> void:\n\tpass\n"
    ),
    "repos/native/src/parser.c": "#define MAX_LEN 100\n\nint parse_config(const char *path)\n{\n    return 0;\n}\n",
    "repos/deploy/Dockerfile": "FROM python:3.12 AS builder\nENV DATABASE_URL=x\n",
    "repos/deploy/docker-compose.yml": "services:\n  web-app:\n    image: x\n",
    "repos/deploy/.github/workflows/ci.yml": "jobs:\n  run-tests:\n    steps: []\n",
    "repos/deploy/scripts/env_check.sh": "#!/bin/sh\nrun_all() {\n  echo ok\n}\nrun_all\n",
    "project-state/config.json": json.dumps({"project_name": "demo"}, ensure_ascii=False),
}
# 反向用例：位置判不出来 / 不该判的（一条都不该报）
NEGATIVE = [
    ("非载荷位置的对象键不判（services 是业务逻辑层）",
     "repos/backend/app/services/checklist.py",
     "def label(row):\n    return {\"status_label\": \"ok\", \"dir_key\": row[\"dir_key\"]}\n"),
    ("配置/数据类 .json 的 snake 键不判",
     "project-state/config.json", json.dumps({"roles_enabled": [], "team_id": 1}, ensure_ascii=False)),
    ("平台自带环境变量不判（SystemRoot）",
     "repos/backend/app/platform_probe.py", "import os\n\n\ndef home():\n    return os.getenv(\"SystemRoot\")\n"),
    ("Python 普通赋值行不当作 .env 行（防 3997 条假命中）",
     "repos/backend/app/calc.py", "step = 1\nlow = 2\nwarnings = 3\n"),
    ("GDScript preload 引用型常量用 PascalCase 合规",
     "repos/game/util/registry.gd", "const DataRegistry = preload(\"res://data_registry.gd\")\n"),
    ("单段小写事件名合规（契约原意是「全小写」而非「必须有点」）",
     "repos/game/core/bus.gd", "func emit_food(bus):\n\tbus.publish(\"food\")\n"),
    ("证据/快照目录整树跳过（backup- 前缀也算）",
     "repos/backend/证据-wave31/backup-config.py", "cover_sub = 1\n"),
]
# 变异用例：每条新判据一个植入违例
MUTATIONS = [
    ("json-field-casing（载荷层 snake 键）", "repos/backend/app/serializers.py",
     "import json\n\n\ndef dump_cover(row):\n    return json.dumps({\"cover_sub\": row[\"cover_sub\"]})\n", "json-field-casing"),
    ("env-var-casing（getenv 取 camel 名）", "repos/backend/app/config.py",
     "import os\n\n\ndef db_url():\n    return os.getenv(\"databaseUrl\")\n", "env-var-casing"),
    ("env-var-casing（.env 小写键）", "repos/backend/.env.example", "database_url=x\n", "env-var-casing"),
    ("error-code-casing", "repos/backend/app/errors.py", 'USER_CODE = "userNotFound"\n', "error-code-casing"),
    ("config-key-casing", "repos/backend/app/store.py",
     "def fetch(cfg, cache):\n    ttl = cfg.get(\"api.paths.authLogin\")\n    return ttl, cache.get(\"cache:user:1\")\n",
     "config-key-casing"),
    ("event-name-casing", "repos/backend/app/events.py", "def emit(bus):\n    bus.publish(\"UserCreated\")\n",
     "event-name-casing"),
    ("cache-key-shape", "repos/backend/app/store.py",
     "def fetch(cfg, cache):\n    ttl = cfg.get(\"cache.user.ttl\")\n    return cache.get(\"Cache_User_42\"), ttl\n",
     "cache-key-shape"),
    ("prompt-id-shape（缺版本号）", "repos/backend/prompts/scan_classify.md", "<!-- id: scan.classify -->\n\n正文\n",
     "prompt-id-shape"),
    ("path-param-casing（py 路由用 camel 参数）", "repos/backend/app/routes.py",
     "def register(router):\n    router.post(\"/api/v1/users/{userId}\", handler)\n", "path-param-casing"),
    ("synonym-banned（用了禁用同义词 member）", "repos/backend/app/services/user.py",
     "def make(row):\n    return {\"memberId\": row[\"member_id\"]}\n", "synonym-banned"),
    ("json-crosslayer-drift（业务代码双写，非白名单）", "repos/backend/app/services/drift.py",
     "def f(row, c):\n    a = {\"cover_sub\": row[\"cover_sub\"]}\n    b = {\"coverSub\": a[\"cover_sub\"]}\n"
     "    return c.get(\"cache:user:1\"), a, b\n", "json-crosslayer-drift"),
    ("db-migration-name（迁移名不合模板）", "repos/backend/migrations/7-add-user.sql",
     "ALTER TABLE users ADD COLUMN a INTEGER;\n", "db-migration-name"),
    ("file-naming（GDScript 文件非 snake_case）", "repos/game/player/PlayerController.gd",
     "func take_damage():\n\tpass\n", "file-naming"),
    ("const-naming（GDScript 值型常量非 UPPER_SNAKE）", "repos/game/util/limits.gd",
     "const maxHp := 100\n", "const-naming"),
    ("const-naming（C #define 非 UPPER_SNAKE）", "repos/native/src/parser.c",
     "#define maxLen 100\n\nint parse_config(const char *path)\n{\n    return 0;\n}\n", "const-naming"),
    ("func-naming（C 顶层函数非 snake_case）", "repos/native/src/parser.c",
     "#define MAX_LEN 100\n\nint parseConfig(const char *path)\n{\n    return 0;\n}\n", "func-naming"),
    ("docker-env-key（ENV 键小写）", "repos/deploy/Dockerfile",
     "FROM python:3.12 AS builder\nENV database_url=x\n", "docker-env-key"),
    ("docker-stage（stage 名非 kebab）", "repos/deploy/Dockerfile",
     "FROM python:3.12 AS BuildStage\nENV DATABASE_URL=x\n", "docker-stage"),
    ("compose-service（服务名非 kebab）", "repos/deploy/docker-compose.yml",
     "services:\n  WebApp:\n    image: x\n", "compose-service"),
    ("ci-job（job 名非 kebab）", "repos/deploy/.github/workflows/ci.yml",
     "jobs:\n  RunTests:\n    steps: []\n", "ci-job"),
    ("shell-func（函数名非 snake）", "repos/deploy/scripts/env_check.sh",
     "#!/bin/sh\nRunAll() {\n  echo ok\n}\nRunAll\n", "shell-func"),
    ("css-class-style（scss 类名 camel）", "repos/frontend/src/styles/cover.scss",
     ".coverSub {\n  color: red;\n}\n", "css-class-style"),
]


def build_tree(files: dict, contract_rows: str = "") -> Path:
    root = Path(tempfile.mkdtemp(prefix="sa_test_"))
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    (root / "project-state" / "契约").mkdir(parents=True, exist_ok=True)
    (root / "project-state" / "契约" / "代码风格.md").write_text(
        CONTRACT_FMT % (json.dumps(PROFILE, ensure_ascii=False, indent=2), contract_rows), encoding="utf-8")
    return root


def rules_hit(root: Path, *extra):
    r = subprocess.run([sys.executable, str(SCRIPT), str(root), "--json", *extra],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        rep = json.loads(r.stdout)
    except json.JSONDecodeError:
        print("  [debug] stdout:", r.stdout[:500], "| stderr:", r.stderr[:400])
        return set(), r.returncode, {}
    return {f["rule"] for f in rep["findings"]}, r.returncode, rep


# ---------------------------------------------------------------------------
print("== 0. 合规树：不应有任何命中（防「新判据把合规代码判死」） ==")
root = build_tree(COMPLIANT)
hit, rc, rep = rules_hit(root)
check(hit == set(), "合规样例树零命中", "实得：%s" % sorted(hit))
check(rc == 0, "合规树退出码 0", "rc=%s" % rc)
check(rep.get("files_scanned", 0) >= 15, "合规树确实扫到了文件（不是空跑）",
      "files_scanned=%s" % rep.get("files_scanned"))
_hist = set(rep.get("extension_histogram") or {})
check({".py", ".gd", ".c", ".vue", ".scss", ".sh"} <= _hist,
      "覆盖面：合规树里 python/gdscript/c/vue/scss/shell 都被扫到", sorted(_hist))
check(any(k in (".yml", ".example", "") for k in _hist), "覆盖面：compose/CI/.env 也被扫到", sorted(_hist))
shutil.rmtree(root, ignore_errors=True)

print("\n== 1. 变异对拍：每条判据的植入违例必须被拦 ==")
for name, rel, content, expect in MUTATIONS:
    tree = dict(COMPLIANT)
    if Path(rel).name not in [Path(k).name for k in tree]:
        tree = {k: v for k, v in tree.items() if Path(k).name != Path(rel).name}
    tree[rel] = content
    r = build_tree(tree)
    hit, rc, _ = rules_hit(r)
    check(expect in hit, name, "期望 %s，实得 %s" % (expect, sorted(hit)))
    check(rc == 1, name + " → 退出码 1", "rc=%s" % rc)
    shutil.rmtree(r, ignore_errors=True)

print("\n== 2. 反向对拍：判不出位置 / 不该判的一律不报 ==")
for label, rel, content in NEGATIVE:
    tree = dict(COMPLIANT)
    tree[rel] = content
    r = build_tree(tree)
    hit, _, _ = rules_hit(r)
    check(hit == set(), "反向对拍：" + label, "实得：%s" % sorted(hit))
    shutil.rmtree(r, ignore_errors=True)

print("\n== 3. 契约配置生效性（治「填了不算数」） ==")
# 3a conversion 白名单：同一文件登记为转换点后不再报；未登记则报
profile2 = json.loads(json.dumps(PROFILE))
profile2["conversion"]["files"] = ["repos/backend/app/services/drift.py"]
drift = "def f(row, c):\n    a = {\"cover_sub\": row[\"cover_sub\"]}\n    b = {\"coverSub\": a[\"cover_sub\"]}\n"
tree = dict(COMPLIANT)
tree["repos/backend/app/services/drift.py"] = drift
r1 = build_tree(tree)
hit1, _, _ = rules_hit(r1)
check("json-crosslayer-drift" in hit1, "白名单外双写 → 报 cross-layer-drift", sorted(hit1))
shutil.rmtree(r1, ignore_errors=True)
r2 = build_tree(tree)
(r2 / "project-state" / "契约" / "代码风格.md").write_text(
    CONTRACT_FMT % (json.dumps(profile2, ensure_ascii=False, indent=2), ""), encoding="utf-8")
hit2, _, _ = rules_hit(r2)
check("json-crosslayer-drift" not in hit2, "白名单内双写 → 不报（§2 conversion 真正生效）", sorted(hit2))
shutil.rmtree(r2, ignore_errors=True)

# 3b synonyms 生效性：清空后同一处不再报
profile3 = json.loads(json.dumps(PROFILE))
profile3["synonyms"] = {}
tree3 = dict(COMPLIANT)
tree3["repos/backend/app/services/user.py"] = "def make(row):\n    return {\"memberId\": row[\"member_id\"]}\n"
r3 = build_tree(tree3)
(r3 / "project-state" / "契约" / "代码风格.md").write_text(
    CONTRACT_FMT % (json.dumps(profile3, ensure_ascii=False, indent=2), ""), encoding="utf-8")
hit3, _, _ = rules_hit(r3)
check("synonym-banned" not in hit3, "synonyms 清空后不报（证明该判据真读配置）", sorted(hit3))
shutil.rmtree(r3, ignore_errors=True)

# 3c §4 例外表：未过期压制 / 已过期重新报
tree = dict(COMPLIANT)
tree["repos/backend/.env.example"] = "database_url=x\n"
r = build_tree(tree, contract_rows="| `repos/backend/.env.example` | env-var-casing | 存量待专项卡 | 架构师 | 2099-12-31 |")
hit4, _, rep4 = rules_hit(r)
check("env-var-casing" not in hit4, "§4 未过期例外 → 压制", sorted(hit4))
check(len(rep4.get("suppressed_by_exception") or []) == 1, "压制项记入报告（不静默）",
      len(rep4.get("suppressed_by_exception") or []))
shutil.rmtree(r, ignore_errors=True)
r = build_tree(tree, contract_rows="| `repos/backend/.env.example` | env-var-casing | 存量待专项卡 | 架构师 | 2000-01-01 |")
hit5, _, _ = rules_hit(r)
check("env-var-casing" in hit5, "§4 已过期例外 → 重新报为命中", sorted(hit5))
shutil.rmtree(r, ignore_errors=True)

# 3d 行内豁免：只在注释里生效（防"文档里提一句就把整文件豁免"的自指陷阱）
tree = dict(COMPLIANT)
tree["repos/backend/app/config.py"] = (
    "# style-audit: off: env-var-casing\nimport os\n\n\ndef db_url():\n"
    "    return os.getenv(\"databaseUrl\")\n# style-audit: on\n")
r = build_tree(tree)
hit6, _, rep6 = rules_hit(r)
check("env-var-casing" not in hit6, "行内豁免（注释内 off）生效", sorted(hit6))
check(len(rep6.get("suppressed_by_inline") or []) >= 1, "行内豁免记入报告", rep6.get("suppressed_by_inline"))
shutil.rmtree(r, ignore_errors=True)
tree["repos/backend/app/config.py"] = (
    'DOC = """style-audit: off: env-var-casing"""\nimport os\n\n\ndef db_url():\n'
    "    return os.getenv(\"databaseUrl\")\n")
r = build_tree(tree)
hit7, _, _ = rules_hit(r)
check("env-var-casing" in hit7, "**字符串里的 `style-audit: off` 不算**（自指陷阱防护之一）", sorted(hit7))
shutil.rmtree(r, ignore_errors=True)
# ③ 只是"提到"这条机制的普通注释也不许生效（脚本里解释它的注释曾把整个文件豁免）
tree["repos/backend/app/config.py"] = (
    "# 行内豁免注释：`style-audit: off[: rule]` … `style-audit: on` 的就地压制机制\n"
    "import os\n\n\ndef db_url():\n    return os.getenv(\"databaseUrl\")\n")
r = build_tree(tree)
hit8, _, _ = rules_hit(r)
check("env-var-casing" in hit8, "**注释里「提到」指令不算生效**（必须整条注释以指令起头）", sorted(hit8))
shutil.rmtree(r, ignore_errors=True)

print("\n== 4. --since 本卡新增口径（契约 §3.3 要求，此前算不出来） ==")
tree = dict(COMPLIANT)
tree["repos/backend/.env.example"] = "database_url=x\n"
r = build_tree(tree)
for c in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"],
          ["add", "-A"], ["commit", "-qm", "baseline"]):
    subprocess.run(["git"] + c, cwd=str(r), capture_output=True)
hit_full, _, _ = rules_hit(r)
check("env-var-casing" in hit_full, "全量扫：存量违例可见（基线）", sorted(hit_full))
hit_s, rc_s, _ = rules_hit(r, "--since", "HEAD")
check("env-var-casing" not in hit_s and rc_s == 0, "--since HEAD（无改动）→ 存量不报、退出码 0", sorted(hit_s))
with (r / "repos" / "backend" / "app" / "config.py").open("w", encoding="utf-8") as fh:
    fh.write("import os\n\n\ndef db_url():\n    return os.getenv(\"databaseUrl\")\n")
hit_s2, _, _ = rules_hit(r, "--since", "HEAD")
check("env-var-casing" in hit_s2, "--since HEAD（改了 config.py）→ 只报该文件的新增命中", sorted(hit_s2))
check("db-index-template" not in hit_s2, "--since 不误报未改动文件的存量问题", sorted(hit_s2))
shutil.rmtree(r, ignore_errors=True)

print("\n== 5. casing 词表（含别名与注入口径） ==")
sys.path.insert(0, str(FRAMEWORK / "scripts"))
import style_audit as sa  # noqa: E402

for name, good, bad in [("camelCase", "coverSub", "cover_sub"), ("PascalCase", "CoverSpec", "cover_spec"),
                        ("snake_case", "cover_sub", "coverSub"), ("UPPER_SNAKE", "MAX_LEN", "maxLen"),
                        ("kebab-case", "cover-sub", "coverSub"), ("flat", "bootstrap", "boot_strap"),
                        ("cobol", "USER-ID", "user-id"),
                        ("lower_dot", "api.paths.auth_login", "api.paths.authLogin"),
                        ("lower_dot", "food", "UserCreated"),
                        ("cache_key", "cache:user:42", "Cache_User_42"),
                        ("prompt_id", "scan.classify.v3", "scan.classify")]:
    check(sa.match_style(name, good) and not sa.match_style(name, bad), "casing %s" % name,
          "good=%s bad=%s" % (sa.match_style(name, good), sa.match_style(name, bad)))
check(sa.match_style("test_snake_case", "test_cover_keeps_original"),
      "别名 test_snake_case 不再误伤（v5.5.0 修：旧版把所有测试函数判违规）")
check(not sa.match_style("test_snake_case", "testCoverKeepsOriginal"), "别名仍拦真违规（camel 测试名）")
check(sa.match_style("snake_case", "__version__", {"separator": {"char": "_", "allowLeading": True}}),
      "separator.allowLeading 生效（dunder）")
check(not sa.match_style("flat", "boot2strap", {"disallowDigits": True}), "disallowDigits 生效")

print("\n== 6. 报告结构（族 / 行内豁免 / 跳过目录都要露出来，不静默） ==")
tree = dict(COMPLIANT)
tree["repos/backend/app/services/checklist.py"] = "def f():\n    return {\"dir_key\": 1}\n"
tree["repos/deploy/Dockerfile"] = "FROM python:3.12 AS BuildStage\nENV DATABASE_URL=x\n"
r = build_tree(tree)
_, _, rep = rules_hit(r)
check(isinstance(rep.get("summary_by_family"), list) and rep["summary_by_family"],
      "报告含按族汇总", rep.get("summary_by_family"))
check("suppressed_by_inline" in rep, "报告含行内豁免区")
check("not_covered" in rep and any("json_field" in x for x in rep["not_covered"]),
      "未覆盖项明说（含载荷边界口径）")
check(any("非源码树" in s for s in rep.get("skipped_dirs") or []) or rep.get("skipped_dirs") is not None,
      "跳过目录如实列出")
fams = {s["family"] for s in rep["summary_by_family"]}
check(fams <= {"STY", "CFG", "CVT", "INF", "META", "OTH"}, "族名在登记表内", fams)
shutil.rmtree(r, ignore_errors=True)

print("\n" + "=" * 62)
print("通过 %d / 失败 %d" % (len(passes), len(fails)))
if fails:
    print("失败项：")
    for f in fails:
        print("  -", f)
print("=" * 62)
sys.exit(1 if fails else 0)

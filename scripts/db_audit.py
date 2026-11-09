#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""db_audit.py —— 数据库只读巡检（大型项目开发框架 v3.22.0 / 数据工程师角色）

用法
    python scripts/db_audit.py <库文件或连接串> [--json] [--out FILE] [--top N]

只读保证（硬性，脚本内显式声明）
    * SQLite 一律以 URI `mode=ro` 打开，并额外执行 `PRAGMA query_only=ON` 二次上锁；
    * 全程只发 SELECT / PRAGMA（只读语句），**不执行任何 INSERT / UPDATE / DELETE / DDL**，
      不创建索引、不改表结构、不写任何文件（除 --out 指定的报告文件与你指定的路径）；
    * 只读打开失败时**明确告警并降级**（仍加 query_only 上锁），绝不静默以可写方式打开。

未覆盖的库（明确提示，不假装支持）
    PostgreSQL / MySQL / MongoDB 等连接串**本脚本未覆盖**——需 psycopg / pymysql 等第三方驱动，
    与"零依赖兜底优先"冲突。检测到 postgres:// postgresql:// mysql:// mongodb:// 等直接退出码 2 并提示改用：
      PostgreSQL: psql -c "\\d+ <table>" / EXPLAIN (ANALYZE,BUFFERS) / pg_stat_statements。

巡检项
    1 表清单与行数 · 2 索引清单 + 缺索引/冗余索引建议 · 3 迁移版本 · 4 孤儿外键（含弱外键）
    5 重复数据（整行重复 / 唯一性候选列重复 / 疑似业务重复） · 6 时间字段空值率 · 7 表体积 Top
    8 字段与表命名风格统计 · 9 账实对账线索（余额列 vs 流水累计）

退出码（主控在批次收口按此判定，不必读全文）
    0 = 无 high/medium findings（可含 low/info 提示）
    1 = 存在 high/medium findings（需处置，见报告末尾「后续行动」）
    2 = 用法错误 / 目标库不支持
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_USAGE = 2

SEV_ORDER = ("high", "medium", "low", "info")
SEV_LABEL = {"high": "高危", "medium": "中危", "low": "低危", "info": "提示"}
BLOCKING_SEV = ("high", "medium")

MIGRATION_TABLES = (
    "schema_migrations", "migrations", "_migrations", "migration", "db_version",
    "knex_migrations", "sequelize_meta", "flyway_schema_history",
    "alembic_version", "__efmigrationshistory",
)
UNIQUE_CANDIDATE_COLS = (
    "username", "user_name", "email", "slug", "code", "phone", "token",
    "uuid", "external_id", "mobile", "id_card",
)
LOW_CARD_COLS = (
    "status", "state", "type", "kind", "category", "stage", "level",
    "role", "disabled", "enabled", "is_deleted", "is_active",
)
BASE_TIME_COLS = (
    "created_at", "updated_at", "deleted_at", "occurred_at",
    "inserted_at", "added_at", "ts",
)
OWNER_COLS = ("user_id", "owner_id", "account_id", "member_id", "customer_id", "uid")
LEDGER_DELTA_COLS = ("delta", "amount", "change", "points_change", "diff")
LEDGER_BALANCE_COLS = ("balance_after", "after_balance", "balance", "balance_remain")
BALANCE_TARGET_COLS = ("points", "balance", "amount", "score", "credits", "coins")
CONN_STR_PREFIXES = (
    "postgres://", "postgresql://", "mysql://", "mariadb://",
    "mongodb://", "mongodb+srv://", "redis://", "mssql://", "oracle://",
)

# 超过该行数的表跳过全表扫描类检查（整行重复/孤儿外键），改出 info 提示避免拖死库
MAX_ROWS_FULL_SCAN = 200000

RE_SNAKE = re.compile(r"^_*[a-z][a-z0-9]*(?:_[a-z0-9]+)*_*$")
RE_CAMEL = re.compile(r"^[a-z][a-z0-9]*(?:[A-Z][a-z0-9]*)+$")
RE_PASCAL = re.compile(r"^[A-Z][A-Za-z0-9]*$")

# 动态 SQL 模板：表名/列名在 SQL 语法上无法用绑定参数，故模板收敛为模块常量；
# 填充值一律经 safe_ident() 白名单校验 + qi() 双引号转义，再由 _fill() 做占位符替换。
_SQL_COUNT = "SELECT COUNT(*) FROM {t}"
_SQL_LATEST = "SELECT * FROM {t} ORDER BY {order} LIMIT 20"
_SQL_TIME_NULL = "SELECT COUNT(*) FROM {t} WHERE {c} IS NULL"


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def qi(ident):
    """SQLite 标识符引用"""
    return '"%s"' % str(ident).replace('"', '""')


# 动态标识符的注入载体字符：引号/分号/注释/空白/NUL/通配。命中即拒绝。
_IDENT_FORBIDDEN = tuple('";-/* \t\r\n\x00\x1a%')


def safe_ident(ident):
    """动态 SQL 标识符统一收口（防注入的第一道闸）。

    审计任意 schema 时，表名/列名无法用绑定参数（SQL 语法要求标识符是字面量），
    故采取「黑名单字符拒绝 + qi() 双引号转义」双保险：本函数先拒绝一切注入载体，
    qi() 再转义引用。所有动态标识符拼接必须经此函数，**不许在 execute 调用点
    直接格式化**（统一写成先拼变量、再 execute 的两步式）。
    """
    s = str(ident)
    if not s or any(ch in s for ch in _IDENT_FORBIDDEN):
        raise ValueError("拒绝非常规标识符（含注入载体字符）：%r" % s)
    return s


def _fill(template, **idents):
    """模板占位符替换的唯一收口：值必须已经 safe_ident() + qi() 处理过再传入。"""
    out = template
    for k, v in idents.items():
        out = out.replace("{%s}" % k, v)
    return out


def human_size(n):
    if n is None:
        return "-"
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < step:
            return "%.1f %s" % (n, unit)
        n /= step
    return "%.1f PB" % n


def classify_name(name):
    if RE_SNAKE.match(name):
        return "snake_case"
    if RE_CAMEL.match(name):
        return "camelCase"
    if RE_PASCAL.match(name):
        return "PascalCase"
    return "其他"


def ro_uri(path):
    """构造 SQLite 只读 URI（Windows/Posix 通吃）"""
    p = os.path.abspath(path).replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p
    return "file:" + p + "?mode=ro"


def finding(check, severity, subject, message, advice=""):
    return {
        "check": check,
        "severity": severity,
        "subject": subject,
        "message": message,
        "advice": advice,
    }


# --------------------------------------------------------------------------- #
# 连接
# --------------------------------------------------------------------------- #
def looks_like_conn_string(target):
    low = target.lower()
    return any(low.startswith(p) for p in CONN_STR_PREFIXES) or (
        "://" in low and not low.startswith("file:")
    )


def connect_readonly(target):
    """返回 (conn, mode_note, warnings)。任何情况下都不写库。"""
    warnings = []
    if looks_like_conn_string(target):
        scheme = target.split("://", 1)[0]
        msg = [
            "目标看起来是连接串（scheme=%s），本脚本**未覆盖**非 SQLite 库。" % scheme,
            "原因：需要 psycopg / pymysql 等第三方驱动，与「零依赖兜底优先」冲突。",
            "请改用对应库的原生只读手段：",
            "  PostgreSQL: psql -c '\\d+ <table>' ；EXPLAIN (ANALYZE, BUFFERS) <sql> ；pg_stat_statements 按 total_exec_time 排序",
            "  MySQL     : SHOW INDEX FROM <table> ；EXPLAIN <sql> ；performance_schema.events_statements_summary_by_digest",
            "SQLite 本地库请直接传文件路径（如 tmp/dev.db）。",
        ]
        raise UsageError("\n".join(msg))

    if not os.path.isfile(target):
        raise UsageError("找不到目标库文件：%s" % target)

    with open(target, "rb") as fh:
        head = fh.read(16)
    if head != b"SQLite format 3\x00":
        raise UsageError(
            "目标不是 SQLite 数据库文件（文件魔数不匹配）：%s\n"
            "本脚本只支持 SQLite；PostgreSQL / MySQL 请用原生只读命令（见 --help 说明）。" % target
        )

    conn = None
    degraded = False
    try:
        conn = sqlite3.connect(ro_uri(target), uri=True, timeout=10)
        conn.execute("PRAGMA query_only = ON")
    except sqlite3.Error as exc:  # WAL 缺 -shm 等极端情况
        degraded = True
        warnings.append(
            "只读模式（mode=ro）打开失败：%s；已降级为普通连接但保持 PRAGMA query_only=ON 上锁，"
            "脚本仍不执行任何写语句。" % exc
        )
        conn = sqlite3.connect(target, timeout=10)
        conn.execute("PRAGMA query_only = ON")

    mode_note = "mode=ro + PRAGMA query_only=ON" + ("（降级：普通连接 + query_only）" if degraded else "")
    return conn, mode_note, warnings


class UsageError(Exception):
    pass


# --------------------------------------------------------------------------- #
# 巡检各项
# --------------------------------------------------------------------------- #
def collect(conn, top_n):
    data = {}
    findings = []
    meta = {}

    meta["sqlite_version"] = sqlite3.sqlite_version
    meta["journal_mode"] = (conn.execute("PRAGMA journal_mode").fetchone() or ["-"])[0]
    meta["page_size"] = conn.execute("PRAGMA page_size").fetchone()[0]
    meta["page_count"] = conn.execute("PRAGMA page_count").fetchone()[0]
    meta["user_version"] = conn.execute("PRAGMA user_version").fetchone()[0]
    meta["foreign_keys_pragma"] = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    try:
        meta["integrity_check"] = conn.execute("PRAGMA quick_check").fetchone()[0]
    except sqlite3.Error as exc:
        meta["integrity_check"] = "不可用：%s" % exc

    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]

    # ---- 1. 表清单 / 行数 / 列 / 主键 -------------------------------------
    table_info = {}
    for t in tables:
        # pragma_table_info 表值函数：标识符走绑定参数（SQLite 3.16+），不再字符串拼 PRAGMA
        cols = list(conn.execute("SELECT * FROM pragma_table_info(?)", (t,)))
        try:
            stmt = _fill(_SQL_COUNT, t=qi(safe_ident(t)))
            rows = conn.execute(stmt).fetchone()[0]
        except sqlite3.Error as exc:
            rows = None
            findings.append(finding("table_scan", "medium", t, "行数统计失败：%s" % exc))
        table_info[t] = {
            "rows": rows,
            "columns": [
                {"name": c[1], "type": (c[2] or "").upper(), "notnull": bool(c[3]),
                 "pk": bool(c[5]), "default": c[4]}
                for c in cols
            ],
            "pk": [c[1] for c in cols if c[5]],
            "size_bytes": None,
            "index_bytes": None,
        }
        if not table_info[t]["pk"]:
            findings.append(
                finding("no_primary_key", "medium", t,
                        "表无主键（%d 列 %s 行）" % (len(cols), rows),
                        "无主键 = 重复行无法定位、无法可靠更新；补 `id` 主键或显式复合主键")
            )

    # ---- 体积（dbstat；不可用则回退） -------------------------------------
    dbstat_ok = False
    sizes = {}
    try:
        sizes = {r[0]: r[1] for r in conn.execute("SELECT name, SUM(pgsize) FROM dbstat GROUP BY name")}
        dbstat_ok = True
    except sqlite3.Error:
        dbstat_ok = False

    # ---- 2. 索引清单 / 缺索引 / 冗余索引 ----------------------------------
    declared_fks = {}
    for t in tables:
        declared_fks[t] = [
            {"col": r[3], "ref_table": r[2], "ref_col": r[4], "on_delete": r[6]}
            for r in conn.execute("SELECT * FROM pragma_foreign_key_list(?)", (t,))
        ]

    indexes = []
    idx_by_table = {}
    for t in tables:
        idx_by_table[t] = []
        for r in conn.execute("SELECT * FROM pragma_index_list(?)", (t,)):
            name, unique, origin, partial = r[1], bool(r[2]), r[3], bool(r[4])
            cols = [c[2] for c in conn.execute("SELECT * FROM pragma_index_info(?)", (name,))]
            auto = name.startswith("sqlite_autoindex_")
            indexes.append({
                "table": t, "name": name, "unique": unique, "origin": origin,
                "partial": partial, "columns": cols, "auto": auto,
                "size_bytes": sizes.get(name),
            })
            idx_by_table[t].append({"name": name, "unique": unique, "columns": cols, "auto": auto})

    small_time_cols = []
    for t in tables:
        info = table_info[t]
        rows = info["rows"] or 0
        first_cols = {ix["columns"][0] for ix in idx_by_table[t] if ix["columns"]}
        first_cols |= {ix["columns"][0] for ix in idx_by_table[t] if ix["columns"] and ix["unique"]}
        col_names = [c["name"] for c in info["columns"]]

        # 缺索引：声明外键列
        for fk in declared_fks[t]:
            if fk["col"] not in first_cols:
                sev = "high" if rows >= 10000 else "medium"
                findings.append(
                    finding("missing_index", sev, "%s.%s" % (t, fk["col"]),
                            "声明外键列无最左前缀索引（表 %s 行）" % rows,
                            "CREATE INDEX idx_%s_%s ON %s(%s); —— 外键列无索引会让「按父表查子表」全表扫" % (t, fk["col"], t, fk["col"]))
                )

        # 缺索引：时间列（按体量定级；小表聚合成一条 info，避免刷屏）
        for c in col_names:
            if c in BASE_TIME_COLS and c not in first_cols:
                if rows >= 10000:
                    findings.append(
                        finding("missing_index", "medium", "%s.%s" % (t, c),
                                "时间列无索引（表 %s 行）" % rows,
                                "按时间范围出报表/列表分页会全表扫；CREATE INDEX idx_%s_%s ON %s(%s)" % (t, c, t, c))
                    )
                elif rows >= 1000:
                    findings.append(
                        finding("missing_index", "low", "%s.%s" % (t, c),
                                "时间列无索引（表 %s 行）" % rows,
                                "数据量继续增长前 CREATE INDEX idx_%s_%s ON %s(%s)" % (t, c, t, c))
                    )
                else:
                    small_time_cols.append("%s.%s(表 %s 行)" % (t, c, rows))

        # 缺索引：低基数筛选列
        for c in col_names:
            if c in LOW_CARD_COLS and c not in first_cols and rows >= 100:
                findings.append(
                    finding("missing_index", "low", "%s.%s" % (t, c),
                            "高频筛选候选列无索引（表 %s 行）" % rows,
                            "先确认确有该筛选查询再建索引（低基数列过滤收益有限，避免为建而建）")
                )

    if small_time_cols:
        findings.append(
            finding("missing_index", "info", "(小表时间列 %d 处)" % len(small_time_cols),
                    "小表（<1000 行）时间列无索引（暂不影响查询）：%s" % ", ".join(small_time_cols[:12])
                    + (" 等" if len(small_time_cols) > 12 else ""),
                    "表长大后（>1000 行且按时间范围筛选/分页）再补索引；现在建 = 只增加写放大")
        )

    # 冗余索引：单列索引被其他索引最左前缀覆盖
    for t in tables:
        ixs = idx_by_table[t]
        for a in ixs:
            if len(a["columns"]) != 1:
                continue
            for b in ixs:
                if b["name"] == a["name"] or not b["columns"]:
                    continue
                if len(b["columns"]) > 1 and b["columns"][0] == a["columns"][0]:
                    findings.append(
                        finding("redundant_index", "low", "%s / %s" % (t, a["name"]),
                                "单列索引 %s(%s) 被 %s(%s) 的最左前缀覆盖" % (a["name"], a["columns"][0], b["name"], ",".join(b["columns"])),
                                "删除冗余索引可减少写放大；删除前确认无强制走单列索引的 hints/计划")
                    )
                    break
                if b["unique"] and not b["auto"] and len(b["columns"]) == 1 and b["columns"][0] == a["columns"][0]:
                    findings.append(
                        finding("redundant_index", "low", "%s / %s" % (t, a["name"]),
                                "单列索引 %s(%s) 与唯一索引 %s(%s) 重复" % (a["name"], a["columns"][0], b["name"], b["columns"][0]),
                                "保留唯一索引即可，删除重复的普通索引")
                    )
                    break
        # 与 UNIQUE 约束（autoindex）重复的显式索引
        autos = {tuple(ix["columns"]) for ix in ixs if ix["auto"] and ix["unique"]}
        for a in ixs:
            if a["auto"] or a["unique"]:
                continue
            if tuple(a["columns"]) in autos:
                findings.append(
                    finding("redundant_index", "low", "%s / %s" % (t, a["name"]),
                            "显式索引 %s(%s) 与 UNIQUE 约束自动索引完全重复" % (a["name"], ",".join(a["columns"])),
                            "UNIQUE 约束已提供该索引，删除显式索引即可")
                )

    # ---- 3. 迁移版本 --------------------------------------------------------
    migration = None
    for t in tables:
        if t.lower() in MIGRATION_TABLES:
            cols = [c["name"] for c in table_info[t]["columns"]]
            version_col = next((c for c in cols if c in ("version", "id", "revision", "name")), cols[0])
            time_col = next((c for c in cols if c.endswith("_at") or c in ("applied", "ran_on", "executed_at")), None)
            ver_col = next((c for c in cols if c in ("version", "id", "revision")), None)
            order_parts = []
            if time_col:
                order_parts.append("%s DESC" % qi(time_col))
            if ver_col and ver_col != time_col:
                order_parts.append("%s DESC" % qi(ver_col))
            order = ", ".join(order_parts) if order_parts else "%s DESC" % qi(cols[0])
            rows = list(conn.execute(_fill(_SQL_LATEST, t=qi(safe_ident(t)), order=order)))
            migration = {
                "table": t, "count": table_info[t]["rows"], "columns": cols,
                "latest": [dict(zip(cols, r)) for r in rows[:5]],
                "order_by": order,
            }
            break
    if migration is None and tables:
        findings.append(
            finding("no_migration_table", "medium", "(全库)",
                    "未找到迁移记录表（schema_migrations / migrations / flyway_schema_history 等）",
                    "无迁移记录 = 库状态不可追溯、三库（dev/test/prod）无从核对；要求后端补迁移记录表并登记执行状态")
        )

    # ---- 4. 孤儿外键（声明 FK + 弱外键） -----------------------------------
    orphan_rows = []
    declared_count = 0
    for t in tables:
        rows_t = table_info[t]["rows"] or 0
        for fk in declared_fks[t]:
            declared_count += 1
            if rows_t > MAX_ROWS_FULL_SCAN:
                findings.append(finding("orphan_fk", "info", "%s.%s" % (t, fk["col"]),
                                        "表过大（%d 行）已跳过孤儿检查" % rows_t, "改在只读副本上跑或分片检查"))
                continue
            sql = (
                "SELECT COUNT(*) FROM %s c WHERE c.%s IS NOT NULL AND NOT EXISTS "
                "(SELECT 1 FROM %s p WHERE p.%s = c.%s)"
                % (qi(t), qi(fk["col"]), qi(fk["ref_table"]), qi(fk["ref_col"]), qi(fk["col"]))
            )
            try:
                bad = conn.execute(sql).fetchone()[0]
            except sqlite3.Error as exc:
                findings.append(finding("orphan_fk", "low", "%s.%s" % (t, fk["col"]), "检查失败：%s" % exc))
                continue
            row = {"table": t, "column": fk["col"], "ref": "%s.%s" % (fk["ref_table"], fk["ref_col"]),
                   "declared": True, "orphans": bad}
            orphan_rows.append(row)
            if bad:
                findings.append(
                    finding("orphan_fk", "high", "%s.%s -> %s" % (t, fk["col"], fk["ref_table"]),
                            "%d 行孤儿（有 FK 约束却出现，说明写入绕过了约束或校验被关闭）" % bad,
                            "先清存量（补父行或置 NULL/删行，走 SQL 修改申请单），再开 PRAGMA foreign_keys=ON 堵新增")
                )

    weak_fk_rows = []
    weak_ok = {}
    for t in tables:
        rows_t = table_info[t]["rows"] or 0
        if rows_t > MAX_ROWS_FULL_SCAN:
            continue
        for c in [c["name"] for c in table_info[t]["columns"]]:
            if c == "id" or not (c.endswith("_id") or c.endswith("_by")):
                continue
            if any(fk["col"] == c for fk in declared_fks[t]):
                continue
            target = resolve_target_table(t, c, tables, table_info)
            if not target:
                continue
            ref_col = "id"
            sql = (
                "SELECT COUNT(*) FROM %s c WHERE c.%s IS NOT NULL AND NOT EXISTS "
                "(SELECT 1 FROM %s p WHERE p.%s = c.%s)"
                % (qi(t), qi(c), qi(target), qi(ref_col), qi(c))
            )
            try:
                bad = conn.execute(sql).fetchone()[0]
            except sqlite3.Error:
                continue
            weak_fk_rows.append({"table": t, "column": c, "ref": "%s.%s" % (target, ref_col), "orphans": bad})
            if bad:
                findings.append(
                    finding("orphan_fk", "high", "%s.%s -> %s" % (t, c, target),
                            "%d 行孤儿引用（该列无 FK 约束保护）" % bad,
                            "清洗存量 + 新增迁移补 FOREIGN KEY 约束，否则孤儿行会一路污染报表口径")
                )
            else:
                weak_ok.setdefault(target, []).append("%s.%s" % (t, c))

    for target, cols in sorted(weak_ok.items(), key=lambda kv: -len(kv[1])):
        findings.append(
            finding("weak_fk", "medium", "-> %s（%d 处）" % (target, len(cols)),
                    "疑似外键列但无 FOREIGN KEY 约束（当前孤儿 0 行，仅靠代码自律）：%s" % ", ".join(cols),
                    "补迁移加约束（存量已验证无孤儿，可直接加）；不加 = 靠代码自律保证引用完整性，"
                    "一旦写入路径绕过校验就产生孤儿行，报表口径随之失真")
        )

    # ---- 5. 重复数据 --------------------------------------------------------
    col_first = {t: {ix["columns"][0] for ix in idx_by_table[t] if ix["columns"]} for t in tables}
    unique_covered = {t: set() for t in tables}
    for t in tables:
        for ix in idx_by_table[t]:
            if ix["unique"] and ix["columns"]:
                unique_covered[t].update(ix["columns"])
    dup_rows = []
    for t in tables:
        rows_t = table_info[t]["rows"] or 0
        if rows_t == 0 or rows_t > MAX_ROWS_FULL_SCAN:
            continue
        cols = [c["name"] for c in table_info[t]["columns"]]
        # 5.1 整行重复
        collist = ", ".join(qi(c) for c in cols)
        sql = ("SELECT COUNT(*) FROM (SELECT %s FROM %s GROUP BY %s HAVING COUNT(*) > 1)"
               % (collist, qi(t), collist))
        try:
            dup_groups = conn.execute(sql).fetchone()[0]
        except sqlite3.Error:
            dup_groups = 0
        if dup_groups:
            dup_rows.append({"table": t, "kind": "整行重复", "groups": dup_groups})
            findings.append(
                finding("duplicate_rows", "medium", t,
                        "%d 组整行完全重复（每列取值都相同）" % dup_groups,
                        "整行重复通常意味着缺主键/唯一约束或重放写入；确认后补 UNIQUE 约束并清洗")
            )
        # 5.2 唯一性候选列重复（无 UNIQUE 约束保护）
        for c in cols:
            if c not in UNIQUE_CANDIDATE_COLS or c in unique_covered[t]:
                continue
            sql = ("SELECT COUNT(*), COUNT(*) - COUNT(DISTINCT %s) FROM %s WHERE %s IS NOT NULL"
                   % (qi(c), qi(t), qi(c)))
            try:
                nonnull, dup = conn.execute(sql).fetchone()
            except sqlite3.Error:
                continue
            if dup:
                findings.append(
                    finding("duplicate_values", "medium", "%s.%s" % (t, c),
                            "%d/%d 行取值重复（该列疑似唯一性业务键但无 UNIQUE 约束）" % (dup, nonnull),
                            "先确认业务是否允许重复；不允许则补 UNIQUE 约束（存量先去重）")
                )
        # 5.3 疑似业务重复（同 owner + 同 kind 多次）
        owner = next((c for c in cols if c in OWNER_COLS), None)
        kindc = next((c for c in cols if c in ("kind", "type", "action", "event", "reason")), None)
        if owner and kindc:
            sql = ("SELECT %s, %s, COUNT(*) c FROM %s WHERE %s IS NOT NULL GROUP BY %s, %s "
                   "HAVING c > 1 ORDER BY c DESC LIMIT 3"
                   % (qi(owner), qi(kindc), qi(t), qi(kindc), qi(owner), qi(kindc)))
            try:
                hits = list(conn.execute(sql))
            except sqlite3.Error:
                hits = []
            if hits:
                desc = "; ".join("%s=%s %s=%s 重复 %d 次" % (owner, h[0], kindc, h[1], h[2]) for h in hits)
                findings.append(
                    finding("suspicious_duplicate", "low", t,
                            "同一 owner 多次同类型记录（可能业务允许，需人工确认）：%s" % desc,
                            "业务允许则写进 契约/指标口径.md 的排除/说明；不允许则加唯一约束并查重放逻辑")
                )

    # ---- 6. 时间字段空值率 --------------------------------------------------
    time_nulls = []
    for t in tables:
        rows_t = table_info[t]["rows"] or 0
        if rows_t == 0:
            continue
        for c in [x["name"] for x in table_info[t]["columns"]]:
            if not (c.endswith("_at") or c.endswith("_time") or c.endswith("_date") or c in BASE_TIME_COLS):
                continue
            try:
                nulls = conn.execute(
                    _fill(_SQL_TIME_NULL, t=qi(safe_ident(t)), c=qi(safe_ident(c)))
                ).fetchone()[0]
            except sqlite3.Error:
                continue
            rate = nulls * 100.0 / rows_t if rows_t else 0.0
            time_nulls.append({"table": t, "column": c, "rows": rows_t, "nulls": nulls, "null_rate": round(rate, 1)})
            if c in BASE_TIME_COLS and nulls:
                findings.append(
                    finding("time_null", "medium", "%s.%s" % (t, c),
                            "基建时间列空值 %d/%d（%.0f%%）" % (nulls, rows_t, rate),
                            "created_at/updated_at 不应为空；补默认值 + NOT NULL，并对存量回填")
                )
            elif rate >= 50:
                findings.append(
                    finding("time_null", "info", "%s.%s" % (t, c),
                            "空值率 %.0f%%（%d/%d）—— 多为业务未发生，须在口径里说明" % (rate, nulls, rows_t),
                            "若报表按该列过滤，空值行会被静默丢弃；写入 契约/指标口径.md 的排除规则")
                )
            elif rate > 20:
                findings.append(
                    finding("time_null", "low", "%s.%s" % (t, c),
                            "空值率 %.0f%%（%d/%d）" % (rate, nulls, rows_t),
                            "确认是「业务未发生」还是「写漏」；前者写口径，后者补写路径")
                )

    # ---- 7. 表体积 Top ------------------------------------------------------
    for t in tables:
        if dbstat_ok:
            table_info[t]["size_bytes"] = sizes.get(t)
            table_info[t]["index_bytes"] = sum(
                sizes.get(ix["name"]) or 0 for ix in idx_by_table[t]
            )
    size_top = sorted(
        [{"table": t, "rows": table_info[t]["rows"],
          "size_bytes": table_info[t]["size_bytes"],
          "index_bytes": table_info[t]["index_bytes"]}
         for t in tables],
        key=lambda r: (r["size_bytes"] or 0), reverse=True
    )[:top_n]

    # ---- 8. 命名风格 --------------------------------------------------------
    style = {"table": {}, "column": {}, "table_total": 0, "column_total": 0}
    for t in tables:
        s = classify_name(t)
        style["table"][s] = style["table"].get(s, 0) + 1
        style["table_total"] += 1
        for c in table_info[t]["columns"]:
            s2 = classify_name(c["name"])
            style["column"][s2] = style["column"].get(s2, 0) + 1
            style["column_total"] += 1
    off = (style["column"].get("camelCase", 0) + style["column"].get("PascalCase", 0) + style["column"].get("其他", 0)
           + style["table"].get("camelCase", 0) + style["table"].get("PascalCase", 0) + style["table"].get("其他", 0))
    if off:
        findings.append(
            finding("naming_style", "low", "(全库)",
                    "命名风格不统一：表 %s / 列 %s（非 snake_case 共 %d 处）"
                    % (style["table"], style["column"], off),
                    "新表新列一律 snake_case；存量列改名属破坏性变更，须走 SQL 修改申请单或登记不改")
        )

    # ---- 9. 账实对账线索 ----------------------------------------------------
    ledger = []
    for t in tables:
        cols = [c["name"] for c in table_info[t]["columns"]]
        owner = next((c for c in cols if c in OWNER_COLS), None)
        delta = next((c for c in cols if c in LEDGER_DELTA_COLS), None)
        bal = next((c for c in cols if c in LEDGER_BALANCE_COLS), None)
        if not (owner and delta):
            continue
        target = resolve_target_table(t, owner, tables, table_info)
        if not target:
            continue
        target_cols = [c["name"] for c in table_info[target]["columns"]]
        tbal = next((c for c in target_cols if c in BALANCE_TARGET_COLS), None)
        sql = ("SELECT c.%s, COUNT(*), COALESCE(SUM(c.%s),0) FROM %s c WHERE c.%s IS NOT NULL GROUP BY c.%s"
               % (qi(owner), qi(delta), qi(t), qi(owner), qi(owner)))
        try:
            groups = list(conn.execute(sql))
        except sqlite3.Error:
            continue
        diffs = []
        for owner_val, cnt, s in groups:
            if tbal is None:
                diffs.append({"owner": owner_val, "ledger_sum": s, "target_value": None, "diff": None, "rows": cnt})
                continue
            row = conn.execute("SELECT %s FROM %s WHERE id = ?" % (qi(tbal), qi(target)), (owner_val,)).fetchone()
            tv = row[0] if row else None
            diffs.append({
                "owner": owner_val, "ledger_sum": s, "target_value": tv,
                "diff": (None if tv is None else tv - s), "rows": cnt,
            })
        bad = [d for d in diffs if d["diff"] not in (None, 0)]
        ledger.append({
            "ledger_table": t, "owner_col": owner, "delta_col": delta,
            "balance_col": bal, "target": "%s.%s" % (target, tbal) if tbal else target,
            "checked": len(diffs), "mismatch": len(bad),
            "rows": [d for d in diffs if d["diff"] not in (None, 0)][:5],
        })
        if bad and tbal:
            detail = "; ".join("%s=%s 流水合计=%s 主表=%s 差额=%s"
                               % (owner, d["owner"], d["ledger_sum"], d["target_value"], d["diff"]) for d in bad[:5])
            findings.append(
                finding("ledger_mismatch", "medium", "%s.%s vs %s.%s" % (t, delta, target, tbal),
                        "%d/%d 个 owner 账实不符（明细流水累计 ≠ 主表余额）：%s" % (len(bad), len(diffs), detail),
                        "对账线索，须人工确认初始值口径；差额不为 0 即需排查事务/回填/重复发放，修数走 SQL 修改申请单")
            )
        elif tbal:
            findings.append(
                finding("ledger_mismatch", "info", "%s.%s vs %s.%s" % (t, delta, target, tbal),
                        "账实相符：%d 个 owner 的流水累计与主表余额一致" % len(diffs), "")
            )

    data.update({
        "meta": meta,
        "tables": table_info,
        "table_order": tables,
        "indexes": indexes,
        "indexes_by_table": idx_by_table,
        "declared_fks": declared_fks,
        "orphans": {"declared": orphan_rows, "weak": weak_fk_rows},
        "migration": migration,
        "duplicates": dup_rows,
        "time_nulls": time_nulls,
        "size_top": size_top,
        "dbstat_ok": dbstat_ok,
        "naming": style,
        "ledger": ledger,
    })
    return data, findings


def resolve_target_table(table, col, tables, table_info):
    """把 `xxx_id` / `xxx_by` 解析到被引表（存在才返回）"""
    names = set(tables)
    lower = {t.lower(): t for t in tables}
    stem = col[:-3] if col.endswith("_id") else col[:-3]  # xxx_id / xxx_by -> xxx
    cands = []
    if col.endswith("_by"):
        cands = ["users", "user", "accounts", "account", "members", "member", "operators"]
    else:
        cands = [stem, stem + "s", stem + "es", stem + "_s"]
        if stem.endswith("y"):
            cands.append(stem[:-1] + "ies")
        if stem.endswith("s"):
            cands.append(stem[:-1])
        cands += ["users", "user"] if stem in ("created", "updated", "owner", "author", "reviewer") else []
    for c in cands:
        if c in names:
            return c
        if c.lower() in lower:
            return lower[c.lower()]
    return None


# --------------------------------------------------------------------------- #
# 报告渲染
# --------------------------------------------------------------------------- #
def render_text(target, data, findings, mode_note, warnings, top_n):
    L = []
    meta = data["meta"]
    total_rows = sum((data["tables"][t]["rows"] or 0) for t in data["table_order"])
    L.append("=" * 78)
    L.append("数据库只读巡检报告  db_audit.py (大型项目开发框架 v3.22.0 / 数据工程师)")
    L.append("=" * 78)
    L.append("目标库    : %s" % os.path.abspath(target))
    L.append("引擎      : SQLite %s | journal=%s | page=%sB x %s = %s"
             % (meta["sqlite_version"], meta["journal_mode"], meta["page_size"],
                meta["page_count"], human_size(meta["page_size"] * meta["page_count"])))
    L.append("quick_check: %s | user_version=%s | 外键开关=%s"
             % (meta["integrity_check"], meta["user_version"],
                "ON" if meta["foreign_keys_pragma"] else "OFF"))
    L.append("巡检时间  : %s" % datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"))
    L.append("只读模式  : %s（脚本不执行任何写操作）" % mode_note)
    L.append("")
    for w in warnings:
        L.append("!! 告警: %s" % w)
    L.append("")

    L.append("--- 1. 表清单与行数（%d 张业务表 / 合计 %d 行）---" % (len(data["table_order"]), total_rows))
    L.append("| 表 | 行数 | 列数 | 主键 | 表体积 | 索引体积 |")
    L.append("|---|---|---|---|---|---|")
    for t in data["table_order"]:
        info = data["tables"][t]
        L.append("| %s | %s | %d | %s | %s | %s |"
                 % (t, info["rows"], len(info["columns"]),
                    ",".join(info["pk"]) or ("(无)" if info["pk"] else "(无)"),
                    human_size(info["size_bytes"]) if data["dbstat_ok"] else "-",
                    human_size(info["index_bytes"]) if data["dbstat_ok"] else "-"))
    L.append("")

    L.append("--- 2. 索引清单（%d 个）---" % len(data["indexes"]))
    L.append("| 表 | 索引 | 列 | 唯一 | 来源 | 体积 |")
    L.append("|---|---|---|---|---|---|")
    for ix in sorted(data["indexes"], key=lambda r: (r["table"], r["name"])):
        L.append("| %s | %s | %s | %s | %s | %s |"
                 % (ix["table"], ix["name"], ",".join(ix["columns"]), "是" if ix["unique"] else "否",
                    {"c": "CREATE INDEX", "u": "UNIQUE 约束", "pk": "主键"}.get(ix["origin"], ix["origin"]),
                    human_size(ix["size_bytes"]) if data["dbstat_ok"] else "-"))
    L.append("")

    L.append("--- 3. 迁移版本 ---")
    mig = data["migration"]
    if mig:
        L.append("迁移表: %s（%s 条记录）" % (mig["table"], mig["count"]))
        for row in mig["latest"]:
            L.append("  %s" % " | ".join("%s=%s" % (k, v) for k, v in row.items()))
    else:
        L.append("未找到迁移记录表（schema_migrations / migrations / flyway_schema_history 等）")
    L.append("")

    L.append("--- 4. 外键与孤儿检查（声明 FK %d 条 / 弱外键 %d 条）---"
             % (len(data["orphans"]["declared"]), len(data["orphans"]["weak"])))
    L.append("| 表.列 | 被引 | 约束 | 孤儿行 |")
    L.append("|---|---|---|---|")
    for r in data["orphans"]["declared"]:
        L.append("| %s.%s | %s | 已声明 | %d |" % (r["table"], r["column"], r["ref"], r["orphans"]))
    for r in data["orphans"]["weak"]:
        L.append("| %s.%s | %s | (无约束) | %d |" % (r["table"], r["column"], r["ref"], r["orphans"]))
    if not data["orphans"]["declared"] and not data["orphans"]["weak"]:
        L.append("| （无） | - | - | - |")
    L.append("")

    L.append("--- 5. 时间字段空值率（%d 列）---" % len(data["time_nulls"]))
    L.append("| 表.列 | 行数 | 空值 | 空值率 |")
    L.append("|---|---|---|---|")
    for r in data["time_nulls"]:
        L.append("| %s.%s | %d | %d | %.1f%% |"
                 % (r["table"], r["column"], r["rows"], r["nulls"], r["null_rate"]))
    if not data["time_nulls"]:
        L.append("| （无时间列） | - | - | - |")
    L.append("")

    L.append("--- 6. 表体积 Top %d ---" % top_n)
    L.append("| 表 | 行数 | 表体积 | 索引体积 |")
    L.append("|---|---|---|---|")
    for r in data["size_top"]:
        L.append("| %s | %s | %s | %s |"
                 % (r["table"], r["rows"], human_size(r["size_bytes"]), human_size(r["index_bytes"])))
    if not data["dbstat_ok"]:
        L.append("（dbstat 不可用，体积未采集；当前 sqlite 构建未启用 SQLITE_ENABLE_DBSTAT_VTAB）")
    L.append("")

    L.append("--- 7. 命名风格统计（表 %d / 列 %d）---" % (data["naming"]["table_total"], data["naming"]["column_total"]))
    L.append("表名: %s" % data["naming"]["table"])
    L.append("列名: %s" % data["naming"]["column"])
    L.append("")

    L.append("--- 8. 账实对账线索 ---")
    if data["ledger"]:
        for r in data["ledger"]:
            if r["rows"] and r["rows"][0]["target_value"] is None:
                L.append("%s.%s → %s：目标表无余额列，仅采集流水合计（未做比对，非 finding）"
                         % (r["ledger_table"], r["delta_col"], r["target"]))
                continue
            L.append("%s.%s → %s：受检 owner %d 个，账实不符 %d 个"
                     % (r["ledger_table"], r["delta_col"], r["target"], r["checked"], r["mismatch"]))
            for row in r["rows"]:
                L.append("    owner=%s 流水合计=%s 主表值=%s 差额=%s"
                         % (row["owner"], row["ledger_sum"], row["target_value"], row["diff"]))
    else:
        L.append("（未发现「流水 + 余额」形态的表，跳过）")
    L.append("")

    counts = {s: 0 for s in SEV_ORDER}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    blocking = counts["high"] + counts["medium"]
    L.append("=" * 78)
    L.append("--- 9. 巡检结论（分级 findings）---")
    L.append("高危 high=%d | 中危 medium=%d | 低危 low=%d | 提示 info=%d"
             % (counts["high"], counts["medium"], counts["low"], counts["info"]))
    L.append("")
    for sev in SEV_ORDER:
        items = [f for f in findings if f["severity"] == sev]
        if not items:
            continue
        L.append("[%s / %s] %d 项" % (SEV_LABEL[sev], sev, len(items)))
        for f in items:
            L.append("  - (%s) %s：%s" % (f["check"], f["subject"], f["message"]))
            if f["advice"]:
                L.append("      处置: %s" % f["advice"])
        L.append("")
    L.append("--- 10. 汇总与退出码 ---")
    L.append("表数=%d 行数合计=%d 索引数=%d findings=%d（high+medium=%d）"
             % (len(data["table_order"]), total_rows, len(data["indexes"]), len(findings), blocking))
    code = EXIT_FINDINGS if blocking else EXIT_OK
    L.append("退出码规则: 0=无 high/medium findings；1=有 high/medium findings；2=用法错")
    L.append("本次退出码: %d%s" % (code, "（需处置，见上方 high/medium 项）" if code else "（仅提示项）"))
    L.append("=" * 78)
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="db_audit.py",
        description="数据库只读巡检（零依赖，仅支持 SQLite；PostgreSQL/MySQL 未覆盖）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  python scripts/db_audit.py tmp/dev.db\n"
               "  python scripts/db_audit.py tmp/dev.db --json\n"
               "  python scripts/db_audit.py tmp/dev.db --out 需求卡/card-021/证据/数据工程师/巡检-dev.txt\n"
               "\n只读保证: mode=ro + PRAGMA query_only=ON，脚本不执行任何写操作。\n"
               "退出码: 0=无 high/medium findings / 1=有 high/medium findings / 2=用法错",
    )
    ap.add_argument("target", help="SQLite 库文件路径（连接串会被明确拒绝）")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON（不输出人读报告）")
    ap.add_argument("--out", metavar="FILE", help="同时把人读报告写入文件（证据落盘用）")
    ap.add_argument("--top", type=int, default=10, help="表体积 Top N（默认 10）")
    args = ap.parse_args(argv)

    t0 = time.time()
    try:
        conn, mode_note, warnings = connect_readonly(args.target)
    except UsageError as exc:
        sys.stderr.write("[用法错误] %s\n" % exc)
        return EXIT_USAGE

    try:
        data, findings = collect(conn, max(1, args.top))
    except sqlite3.Error as exc:
        sys.stderr.write("[SQLite 错误] 巡检中断：%s\n" % exc)
        return EXIT_USAGE
    finally:
        try:
            conn.close()
        except Exception:
            pass

    counts = {s: 0 for s in SEV_ORDER}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    blocking = counts["high"] + counts["medium"]
    code = EXIT_FINDINGS if blocking else EXIT_OK
    elapsed = round(time.time() - t0, 2)

    if args.json:
        payload = {
            "tool": "db_audit.py",
            "framework": "大型项目开发框架 v3.22.0",
            "target": os.path.abspath(args.target),
            "engine": "sqlite",
            "readonly_mode": mode_note,
            "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "elapsed_sec": elapsed,
            "warnings": warnings,
            "summary": {
                "tables": len(data["table_order"]),
                "rows_total": sum((data["tables"][t]["rows"] or 0) for t in data["table_order"]),
                "indexes": len(data["indexes"]),
                "findings_total": len(findings),
                "severity_counts": counts,
                "blocking": blocking,
                "exit_code": code,
            },
            "findings": findings,
            "sections": {
                "meta": data["meta"],
                "tables": data["tables"],
                "indexes": data["indexes"],
                "declared_fks": data["declared_fks"],
                "orphans": data["orphans"],
                "migration": data["migration"],
                "duplicates": data["duplicates"],
                "time_nulls": data["time_nulls"],
                "size_top": data["size_top"],
                "naming": data["naming"],
                "ledger": data["ledger"],
            },
        }
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n")
    else:
        report = render_text(args.target, data, findings, mode_note, warnings, max(1, args.top))
        sys.stdout.write(report + "\n")

    if args.out:
        report = render_text(args.target, data, findings, mode_note, warnings, max(1, args.top))
        out_path = os.path.realpath(os.path.abspath(args.out))
        outdir = os.path.dirname(out_path)
        if outdir and not os.path.isdir(outdir):
            os.makedirs(outdir, exist_ok=True)
        Path(out_path).write_text(report + "\n", encoding="utf-8", newline="\n")
        if not args.json:
            sys.stdout.write("报告已写入: %s\n" % out_path)

    return code


if __name__ == "__main__":
    sys.exit(main())

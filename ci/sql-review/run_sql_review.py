#!/usr/bin/env python3
import argparse
import json
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pymysql
import yaml


# MyBatis XML / 注解里会被识别为 SQL 语句的标签类型。
SQL_TAGS = {"select", "update", "delete", "insert"}
# 仅审核这些后缀的文件，避免把普通业务代码也纳入 SQL 扫描。
SQL_FILE_SUFFIXES = ("Mapper.xml", "Mapper.java", "Repository.java", "DAO.java")
# 这些动态标签会被保留成占位标记，供后续静态规则判断。
DYNAMIC_SQL_TAGS = {"if", "choose", "when", "otherwise", "trim", "where", "set", "foreach"}
# 提取列名时需要排除的 SQL 关键字，避免误把关键字识别成字段名。
SQL_STOP_WORDS = {
    "and",
    "or",
    "in",
    "like",
    "regexp",
    "between",
    "is",
    "not",
    "null",
    "exists",
    "select",
    "from",
    "where",
    "order",
    "group",
    "limit",
    "having",
    "join",
    "left",
    "right",
    "inner",
    "outer",
    "on",
    "asc",
    "desc",
}
MYBATIS_GUARDED_WHERE_TOKENS = (
    "<where>",
    "<include:example_where_clause>",
    "<include:update_by_example_where_clause>",
)
PAGINATION_HINT_TOKENS = ("rowbounds", "pagehelper", "page", "pagination", "pageable")


def normalize_sql(sql: str) -> str:
    """统一 SQL 文本格式。

    作用：
    1. 把换行、制表符、多空格压缩成单个空格。
    2. 去掉首尾空白，方便后续规则和正则匹配。

    输入：
    - 原始 SQL 字符串，可能来自 XML、注解或动态 SQL 片段。

    输出：
    - 适合做规则匹配和结果展示的单行 SQL。
    """
    return re.sub(r"\s+", " ", sql).strip()


def load_rules(path: Path) -> dict[str, Any]:
    """读取 YAML 规则配置。

    作用：
    - 从 `mysql_rules.yaml` 加载阻断级别、分页关键字、函数关键字等规则参数。

    输入：
    - 规则文件路径。

    输出：
    - 规则配置字典，供静态规则和 EXPLAIN 分析复用。
    """
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def run_git_diff(repo: Path, target_branch: str | None, commit_sha: str | None) -> list[str]:
    """获取当前 MR 相对目标分支的变更文件列表。

    作用：
    - 在 GitLab MR 场景下，只审核 `origin/目标分支...当前提交` 的差异文件。
    - 如果缺少目标分支或提交信息，则返回空列表，表示本次无法自动定位变更范围。

    输入：
    - `repo`：仓库根目录。
    - `target_branch`：MR 目标分支名，通常来自 `CI_MERGE_REQUEST_TARGET_BRANCH_NAME`。
    - `commit_sha`：当前提交 SHA，通常来自 `CI_COMMIT_SHA`。

    输出：
    - 变更文件相对路径列表。
    """
    if not target_branch or not commit_sha:
        return []
    cmd = ["git", "diff", "--name-only", f"origin/{target_branch}...{commit_sha}"]
    result = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def is_sql_related(path: str) -> bool:
    """判断文件是否属于 SQL 审核范围。

    作用：
    - 只保留 Mapper / DAO / Repository 这类可能携带 SQL 的文件。

    输入：
    - 仓库内相对路径。

    输出：
    - `True` 表示该文件需要进入 SQL 提取流程。
    """
    return path.endswith(SQL_FILE_SUFFIXES)


def collect_element_text(elem: ET.Element) -> str:
    """递归提取 MyBatis XML 节点里的 SQL 文本。

    作用：
    - 不仅提取节点纯文本，还会把 `<if>`、`<where>`、`<include>` 等动态标签编码进结果。
    - 这样后续规则仍能识别“这是动态 SQL”以及“这里引用了 include 片段”。

    输入：
    - MyBatis XML 中的一个 SQL 语句节点。

    输出：
    - 保留动态标签痕迹的原始 SQL 文本。
    """
    parts: list[str] = []
    if elem.text:
        parts.append(elem.text)
    for child in list(elem):
        tag = child.tag.split("}")[-1]
        if tag in DYNAMIC_SQL_TAGS:
            parts.append(f" <{tag}> ")
        if tag == "include":
            refid = child.attrib.get("refid", "")
            parts.append(f" <include:{refid}> ")
        parts.append(collect_element_text(child))
        if child.tail:
            parts.append(child.tail)
    return "".join(parts)


def extract_from_xml(path: Path) -> list[dict[str, Any]]:
    """从 MyBatis XML 文件中提取可审核的 SQL 条目。

    作用：
    - 识别 `<select>`、`<update>`、`<delete>`、`<insert>`。
    - 为每条语句附带文件路径、命名空间、语句 ID、原始 SQL、规范化 SQL、是否动态 SQL。

    输入：
    - Mapper XML 文件路径。

    输出：
    - SQL 条目列表，每个条目都是后续规则检查的基础数据结构。
    """
    tree = ET.parse(path)
    root = tree.getroot()
    namespace = root.attrib.get("namespace", "")
    entries: list[dict[str, Any]] = []
    for elem in root:
        tag = elem.tag.split("}")[-1]
        if tag not in SQL_TAGS:
            continue
        statement_id = elem.attrib.get("id", "")
        raw_sql = collect_element_text(elem)
        entries.append(
            {
                "file": str(path),
                "source_type": "xml",
                "sql_type": tag.upper(),
                "statement_id": f"{namespace}.{statement_id}" if namespace else statement_id,
                "raw_sql": raw_sql,
                "normalized_sql": normalize_sql(raw_sql),
                "dynamic": any(marker in raw_sql for marker in ["<if>", "<choose>", "<foreach>", "<where>", "<set>"]),
            }
        )
    return entries


def decode_java_string_literal(value: str) -> str:
    """解析 Java 字符串字面量。

    作用：
    - 去掉首尾双引号。
    - 把 `\\n`、`\\t`、Unicode 转义等还原成真实字符。

    输入：
    - Java 注解中的字符串字面量片段。

    输出：
    - 还原后的普通字符串。
    """
    value = value.strip()
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    return bytes(value, "utf-8").decode("unicode_escape")


def parse_annotation_payload(payload: str) -> str:
    """把注解里的多段字符串拼装成 SQL。

    作用：
    - MyBatis 注解 SQL 可能写成多段字符串拼接，这里统一提取并按空格拼接。

    输入：
    - `@Select(...)` / `@Update(...)` 等注解括号中的原始文本。

    输出：
    - 可供审核的 SQL 文本。
    """
    strings = re.findall(r'"(?:[^"\\]|\\.)*"', payload, flags=re.S)
    return " ".join(decode_java_string_literal(item) for item in strings)


def extract_from_java(path: Path) -> list[dict[str, Any]]:
    """从 Java 注解中提取可审核的 SQL 条目。

    作用：
    - 识别 `@Select`、`@Update`、`@Delete`、`@Insert`。
    - 自动拼装 `包名.类型名.方法名` 形式的语句标识，方便在报告中定位。

    输入：
    - Java Mapper / DAO / Repository 文件路径。

    输出：
    - SQL 条目列表。
    """
    text = path.read_text(encoding="utf-8")
    package_match = re.search(r"package\s+([\w.]+);", text)
    package_name = package_match.group(1) if package_match else ""
    interface_match = re.search(r"(?:interface|class)\s+(\w+)", text)
    type_name = interface_match.group(1) if interface_match else path.stem
    entries: list[dict[str, Any]] = []
    pattern = re.compile(
        r"@(Select|Update|Delete|Insert)\s*\((.*?)\)\s*[^;{]*?\b(\w+)\s*\(",
        flags=re.S,
    )
    for match in pattern.finditer(text):
        annotation, payload, method_name = match.groups()
        raw_sql = parse_annotation_payload(payload)
        entries.append(
            {
                "file": str(path),
                "source_type": "java_annotation",
                "sql_type": annotation.upper(),
                "statement_id": ".".join(part for part in [package_name, type_name, method_name] if part),
                "raw_sql": raw_sql,
                "normalized_sql": normalize_sql(raw_sql),
                "dynamic": "<script>" in raw_sql or "<if" in raw_sql,
            }
        )
    return entries


def collect_sql_candidates(repo: Path, changed_files: list[str]) -> list[dict[str, Any]]:
    """汇总所有需要审核的 SQL 条目。

    作用：
    - 遍历本次 MR 的变更文件。
    - 只处理 SQL 相关文件。
    - 同一文件即使在 diff 里出现多次，也只提取一次，避免重复审查。

    输入：
    - `repo`：仓库根目录。
    - `changed_files`：Git diff 得到的相对路径列表。

    输出：
    - 来自 XML 和 Java 注解的全部 SQL 条目。
    """
    sql_entries: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for rel in changed_files:
        if not is_sql_related(rel):
            continue
        path = (repo / rel).resolve()
        if not path.exists() or path in seen:
            continue
        seen.add(path)
        if path.name.endswith("Mapper.xml"):
            sql_entries.extend(extract_from_xml(path))
        elif path.suffix == ".java":
            sql_entries.extend(extract_from_java(path))
    return sql_entries


def normalize_for_rules(sql: str) -> str:
    """为静态规则匹配准备 SQL。

    作用：
    - 在通用规范化基础上转成小写，减少规则正则的大小写分支。

    输入：
    - 原始或规范化 SQL。

    输出：
    - 适合做静态规则匹配的小写 SQL。
    """
    normalized = normalize_sql(sql).lower()
    # 先去掉 MyBatis 占位符，避免后续把 jdbcType、动态排序参数等误识别成列名或关键字。
    return re.sub(r"[#$]\{[^}]+\}", " ?", normalized)


def has_guarded_where(entry: dict[str, Any], sql: str) -> bool:
    """判断语句是否已经具备稳定的 WHERE 保护。"""
    if " where " in f" {sql} ":
        return True
    raw_sql = (entry.get("raw_sql") or "").lower()
    normalized_sql = (entry.get("normalized_sql") or "").lower()
    return any(token in raw_sql or token in normalized_sql for token in MYBATIS_GUARDED_WHERE_TOKENS)


def should_check_dynamic_full_scan(entry: dict[str, Any], sql_type: str) -> bool:
    """筛选真正需要检查“动态 SQL 全表风险”的语句。"""
    if sql_type not in {"SELECT", "UPDATE", "DELETE"}:
        return False
    statement_id = (entry.get("statement_id") or "").lower()
    raw_sql = (entry.get("raw_sql") or "").lower()
    if "<foreach>" in raw_sql:
        return False
    mbg_safe_keywords = (
        "byexample",
        "example_where_clause",
        "update_by_example_where_clause",
    )
    return not any(token in statement_id or token in raw_sql for token in mbg_safe_keywords)


def has_pagination_hint(entry: dict[str, Any], sql: str, config: dict[str, Any]) -> bool:
    """判断语句是否已经带有分页能力或分页调用约定。"""
    pagination_keywords = [item.lower() for item in config.get("pagination_keywords", [])]
    if any(keyword in sql for keyword in pagination_keywords):
        return True
    statement_id = (entry.get("statement_id") or "").lower()
    return any(token in statement_id for token in PAGINATION_HINT_TOKENS)


def is_count_query(sql: str) -> bool:
    """判断是否为聚合计数查询。"""
    return bool(re.search(r"^select\s+count\s*\(", sql))


def normalize_for_explain(sql: str) -> str:
    """把 MyBatis SQL 转成尽量可执行的 EXPLAIN SQL。

    作用：
    - 把 `#{}`、`${}` 替换成占位值。
    - 把 `<where>`、`<set>` 等动态标签做保守修正。
    - 清理 `<include:...>` 和其他 XML 标签，让 SQL 尽量接近真实可执行语句。

    输入：
    - MyBatis 风格的 SQL。

    输出：
    - 用于 `EXPLAIN ...` 的 SQL 片段。
    """
    sql = normalize_sql(sql)
    sql = re.sub(r"#\{[^}]+\}", "1", sql)
    sql = re.sub(r"\$\{[^}]+\}", "1", sql)
    sql = sql.replace("<where>", " __WHERE__ ").replace("</where>", " ")
    sql = sql.replace("<set>", " __SET__ ").replace("</set>", " ")
    sql = re.sub(r"<include:[^>]+>", " ", sql)
    sql = re.sub(r"<[^>]+>", " ", sql)
    sql = re.sub(r"__WHERE__\s+(and|or)\b", " WHERE ", sql, flags=re.I)
    sql = re.sub(r"__SET__\s*,", " SET ", sql, flags=re.I)
    sql = sql.replace("__WHERE__", " WHERE ").replace("__SET__", " SET ")
    sql = re.sub(r"\bwhere\s+(and|or)\b", "WHERE ", sql, flags=re.I)
    sql = re.sub(r"\bset\s*,", "SET ", sql, flags=re.I)
    sql = re.sub(r"\bfrom\s+([^\s,]+)\s+and\b", r"from \1 where", sql, flags=re.I)
    sql = re.sub(r"\bin\s*\(\s*\)", "in (1)", sql, flags=re.I)
    return normalize_sql(sql).rstrip(";")


def add_finding(findings: list[dict[str, Any]], entry: dict[str, Any], level: str, rule: str, reason: str, suggestion: str, block: bool, **extra: Any) -> None:
    """向风险列表追加一条审核结果。

    作用：
    - 把规则命中信息统一封装成标准结构，便于后续汇总、排序和报告渲染。

    输入：
    - `findings`：当前累计风险列表。
    - `entry`：对应的 SQL 条目。
    - `level` / `rule` / `reason` / `suggestion`：规则结果描述。
    - `block`：该问题是否应阻断合并。
    - `extra`：额外信息，例如列名列表或 EXPLAIN 行。

    输出：
    - 无返回值，原地修改 `findings`。
    """
    item = {
        "file": entry["file"],
        "statement_id": entry["statement_id"],
        "sql_type": entry["sql_type"],
        "level": level,
        "rule": rule,
        "reason": reason,
        "suggestion": suggestion,
        "block_merge": block,
        "sql": entry["normalized_sql"],
    }
    item.update(extra)
    findings.append(item)


def looks_like_collection_query(entry: dict[str, Any], sql: str, config: dict[str, Any]) -> bool:
    """判断 SQL 是否更像“列表型查询”。

    作用：
    - “缺分页”不应打在所有 SELECT 上，只想提示那些明显可能返回大量结果的列表查询。
    - 这里采用启发式判断：方法名关键字、group by / order by、复合 where 等。

    输入：
    - `entry`：SQL 条目，用于读取语句 ID。
    - `sql`：已小写规范化的 SQL。
    - `config`：规则配置，用于读取方法名关键字。

    输出：
    - `True` 表示更像列表集合查询，适合继续检查分页缺失问题。
    """
    statement_id = (entry.get("statement_id") or "").lower()
    keywords = [item.lower() for item in config.get("collection_query_keywords", [])]
    if is_count_query(sql):
        return False
    if any(keyword in statement_id for keyword in keywords):
        return True
    if re.search(r"\bgroup\s+by\b|\border\s+by\b", sql):
        return True
    if re.search(r"\bwhere\s+id\s*=\s*([#${?]|\d+)", sql):
        return False
    if re.search(r"\bwhere\b", sql) and re.search(r"\b(and|or)\b", sql):
        return True
    return " where " not in f" {sql} "


def run_static_rules(entry: dict[str, Any], config: dict[str, Any]) -> list[dict[str, Any]]:
    """执行不依赖数据库的静态规则检查。

    作用：
    - 发现 `select *`、无 where 的 DML、前导 `%like`、缺分页、过长 `IN`、列上函数、动态 SQL 全表风险等问题。

    输入：
    - `entry`：单条 SQL 语句。
    - `config`：规则配置。

    输出：
    - 命中的静态风险列表。
    """
    findings: list[dict[str, Any]] = []
    sql = normalize_for_rules(entry["normalized_sql"])
    sql_type = entry["sql_type"].upper()
    block_levels = set(config.get("block_levels", []))
    long_in_threshold = int(config.get("long_in_threshold", 5))
    function_tokens = [item.lower() for item in config.get("indexed_column_functions", [])]

    if sql_type == "SELECT" and re.search(r"^select\s+\*\s+from\b", sql):
        add_finding(findings, entry, "P1", "select_star", "查询使用了 SELECT *，会扩大 I/O 和回表风险。", "请明确列名，只查询必要字段。", "P1" in block_levels)

    if sql_type in {"UPDATE", "DELETE"} and not has_guarded_where(entry, sql):
        add_finding(findings, entry, "P0", "dml_without_where", "UPDATE/DELETE 未检测到 WHERE，存在全表修改或删除风险。", "请补充精确的 WHERE 条件，并增加防呆保护。", "P0" in block_levels)

    if " like " in f" {sql} " and re.search(r"like\s+['\"]?%", sql):
        add_finding(findings, entry, "P1", "leading_wildcard_like", "存在前导模糊 LIKE，索引通常无法命中。", "优先改成后缀模糊、倒排索引或搜索引擎方案。", "P1" in block_levels)

    if sql_type == "SELECT" and " from " in sql and not has_pagination_hint(entry, sql, config):
        if looks_like_collection_query(entry, sql, config):
            add_finding(findings, entry, "P2", "missing_pagination", "SELECT 未识别到分页关键字，且语句形态更像列表集合查询，可能存在大结果集风险。", "请确认是否需要 LIMIT/OFFSET/PageHelper 等分页手段。", False)

    in_markers = re.findall(r"\?", sql)
    if " in " in sql and len(in_markers) >= long_in_threshold:
        add_finding(findings, entry, "P2", "long_in_list", "IN 参数较长，可能导致 SQL 过长或执行计划变差。", "考虑分批、临时表、JOIN 或其他替代方案。", False)

    if any(token in sql for token in function_tokens):
        add_finding(findings, entry, "P1", "function_on_column", "WHERE 条件中疑似对列做函数计算，可能导致索引失效。", "将函数计算移到参数侧，或建立函数索引/冗余列。", "P1" in block_levels)

    if entry.get("dynamic") and should_check_dynamic_full_scan(entry, sql_type) and not has_guarded_where(entry, sql):
        add_finding(findings, entry, "P1", "dynamic_sql_full_scan_risk", "动态 SQL 未见稳定 WHERE，参数缺省时可能退化为全表扫描。", "请为动态 SQL 增加兜底过滤条件，并补充空参数测试。", "P1" in block_levels)

    return findings


def build_mysql_connection() -> pymysql.connections.Connection | None:
    """创建审核用 MySQL 连接。

    作用：
    - 为索引元数据检查和 EXPLAIN 提供数据库连接。
    - 如果环境变量不完整，则返回 `None`，让脚本退化成纯静态审查。

    输入：
    - 无显式参数，依赖 `AUDIT_DB_*` 系列环境变量。

    输出：
    - `pymysql` 连接对象，或 `None`。
    """
    required = ["AUDIT_DB_HOST", "AUDIT_DB_NAME", "AUDIT_DB_USER", "AUDIT_DB_PASSWORD"]
    if not all(os.getenv(key) for key in required):
        return None
    return pymysql.connect(
        host=os.environ["AUDIT_DB_HOST"],
        port=int(os.getenv("AUDIT_DB_PORT", "3306")),
        user=os.environ["AUDIT_DB_USER"],
        password=os.environ["AUDIT_DB_PASSWORD"],
        database=os.environ["AUDIT_DB_NAME"],
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


def split_table_name(raw_name: str) -> tuple[str, str]:
    """拆分 `schema.table` 形式的表名。

    作用：
    - 兼容 SQL 中既可能写全限定名，也可能只写表名的情况。

    输入：
    - SQL 解析出的表名原始文本。

    输出：
    - `(schema, table)` 元组；如果没有显式 schema，则默认使用审核库名。
    """
    cleaned = raw_name.strip().strip("`")
    if "." in cleaned:
        schema_name, table_name = cleaned.split(".", 1)
        return schema_name.strip("`"), table_name.strip("`")
    return os.getenv("AUDIT_DB_NAME", ""), cleaned


def parse_table_refs(sql: str, sql_type: str) -> list[dict[str, str]]:
    """从 SQL 中提取表引用信息。

    作用：
    - 识别 SELECT / UPDATE / DELETE / INSERT 中涉及的表、schema、别名。
    - 供后续字段归属判断、索引元数据查询使用。

    输入：
    - `sql`：已规范化的 SQL。
    - `sql_type`：SQL 类型。

    输出：
    - 表引用列表，每项包含 `schema`、`table`、`alias`。
    """
    refs: list[dict[str, str]] = []
    lowered = normalize_for_rules(sql)

    def normalize_alias(alias: str | None, fallback: str) -> str:
        candidate = (alias or fallback).strip("`").lower()
        if not candidate or candidate in SQL_STOP_WORDS or candidate == "set":
            return fallback.strip("`").lower()
        return candidate

    if sql_type == "SELECT":
        for match in re.finditer(r"\b(from|join)\s+([`.\w]+)(?:\s+(?:as\s+)?(\w+))?", lowered):
            table_name = match.group(2)
            fallback_alias = table_name.split(".")[-1]
            alias = normalize_alias(match.group(3), fallback_alias)
            schema_name, pure_table = split_table_name(table_name)
            refs.append({"schema": schema_name, "table": pure_table, "alias": alias})
    elif sql_type == "UPDATE":
        match = re.search(r"\bupdate\s+([`.\w]+)(?:\s+(?:as\s+)?(\w+))?", lowered)
        if match:
            table_name = match.group(1)
            fallback_alias = table_name.split(".")[-1]
            alias = normalize_alias(match.group(2), fallback_alias)
            schema_name, pure_table = split_table_name(table_name)
            refs.append({"schema": schema_name, "table": pure_table, "alias": alias})
    elif sql_type == "DELETE":
        match = re.search(r"\bdelete\s+from\s+([`.\w]+)(?:\s+(?:as\s+)?(\w+))?", lowered)
        if match:
            table_name = match.group(1)
            fallback_alias = table_name.split(".")[-1]
            alias = normalize_alias(match.group(2), fallback_alias)
            schema_name, pure_table = split_table_name(table_name)
            refs.append({"schema": schema_name, "table": pure_table, "alias": alias})
    elif sql_type == "INSERT":
        match = re.search(r"\binsert\s+into\s+([`.\w]+)", lowered)
        if match:
            table_name = match.group(1)
            schema_name, pure_table = split_table_name(table_name)
            refs.append({"schema": schema_name, "table": pure_table, "alias": pure_table})
    return refs


def extract_columns(section: str) -> list[str]:
    """从条件片段中提取列名候选。

    作用：
    - 识别 `=`、`like`、`in`、`between` 等运算符左侧的字段。
    - 用于 where / join 条件的索引候选分析。

    输入：
    - SQL 的某一段条件文本。

    输出：
    - 字段名列表。
    """
    candidates: list[str] = []
    for match in re.finditer(
        r"([`.\w]+)\s*(=|!=|<>|>=|<=|>|<|\slike\b|\sregexp\b|\sin\b|\sbetween\b|\sis\b)",
        section,
        flags=re.I,
    ):
        raw_column = match.group(1).strip("`").lower()
        if raw_column in SQL_STOP_WORDS or raw_column.isdigit():
            continue
        if raw_column.endswith("."):
            continue
        parts = [part for part in raw_column.split(".") if part]
        if not parts:
            continue
        if any(part in SQL_STOP_WORDS or part == "set" for part in parts):
            continue
        if len(parts) > 2:
            continue
        candidates.append(raw_column)
    return candidates


def extract_list_columns(section: str) -> list[str]:
    """从排序 / 分组列表中提取字段候选。

    作用：
    - 从 `order by a desc, b`、`group by a, b` 中抽取字段。
    - 忽略函数表达式和关键字。

    输入：
    - `order by` 或 `group by` 后面的列表文本。

    输出：
    - 字段名列表。
    """
    columns: list[str] = []
    for chunk in section.split(","):
        token = chunk.strip().lower()
        token = re.sub(r"\s+(asc|desc)\b", "", token)
        token = token.strip("` ")
        if not token or token in SQL_STOP_WORDS or token == "?":
            continue
        if "(" in token and ")" in token:
            continue
        parts = [part for part in token.split(".") if part]
        if any(part in SQL_STOP_WORDS or part == "set" for part in parts):
            continue
        columns.append(token)
    return columns


def extract_index_usage_candidates(sql: str, sql_type: str) -> dict[str, Any]:
    """提取用于索引分析的字段候选信息。

    作用：
    - 解析表引用。
    - 提取 where、join、order by、group by 中的字段。
    - 这些只是“候选字段”，真正是否有索引要到数据库查询元数据。

    输入：
    - `sql`：待分析 SQL。
    - `sql_type`：SQL 类型。

    输出：
    - 包含表引用和各类字段列表的字典。
    """
    normalized = normalize_for_rules(sql)
    table_refs = parse_table_refs(normalized, sql_type)

    where_match = re.search(r"\bwhere\b(.*?)(?:\border\s+by\b|\bgroup\s+by\b|\blimit\b|$)", normalized, flags=re.I)
    where_columns = extract_columns(where_match.group(1)) if where_match else []

    join_columns: list[str] = []
    for match in re.finditer(r"\bon\b(.*?)(?:\bjoin\b|\bwhere\b|\border\s+by\b|\bgroup\s+by\b|\blimit\b|$)", normalized, flags=re.I):
        join_columns.extend(extract_columns(match.group(1)))

    order_match = re.search(r"\border\s+by\b(.*?)(?:\blimit\b|$)", normalized, flags=re.I)
    order_columns = extract_list_columns(order_match.group(1)) if order_match else []

    group_match = re.search(r"\bgroup\s+by\b(.*?)(?:\border\s+by\b|\blimit\b|$)", normalized, flags=re.I)
    group_columns = extract_list_columns(group_match.group(1)) if group_match else []

    return {
        "table_refs": table_refs,
        "where_columns": sorted(set(where_columns)),
        "join_columns": sorted(set(join_columns)),
        "order_columns": sorted(set(order_columns)),
        "group_columns": sorted(set(group_columns)),
    }


def fetch_index_metadata(cursor: Any, schema_name: str, table_name: str) -> dict[str, Any]:
    """读取指定表的索引元数据。

    作用：
    - 查询 `information_schema.statistics`，收集索引名、索引列顺序。
    - 同时构建“字段 -> 索引名列表”的映射，方便快速判断字段是否被索引覆盖。

    输入：
    - `cursor`：数据库游标。
    - `schema_name` / `table_name`：目标表信息。

    输出：
    - 索引元数据字典。
    """
    cursor.execute(
        """
        SELECT index_name, column_name, seq_in_index, non_unique
        FROM information_schema.statistics
        WHERE table_schema = %s AND table_name = %s
        ORDER BY index_name, seq_in_index
        """,
        (schema_name, table_name),
    )
    rows = list(cursor.fetchall())
    index_map: dict[str, list[str]] = {}
    column_to_indexes: dict[str, list[str]] = {}
    for row in rows:
        index_name = str(row["index_name"])
        column_name = str(row["column_name"]).lower()
        index_map.setdefault(index_name, []).append(column_name)
        column_to_indexes.setdefault(column_name, []).append(index_name)
    return {
        "schema": schema_name,
        "table": table_name,
        "indexes": index_map,
        "column_to_indexes": column_to_indexes,
    }


def resolve_column_table(column: str, table_refs: list[dict[str, str]]) -> tuple[str | None, str]:
    """把字段解析到具体表。

    作用：
    - 处理 `a.id` 这种带别名字段。
    - 在单表查询场景下，把未带前缀的字段默认归属到该表。

    输入：
    - `column`：字段文本。
    - `table_refs`：SQL 中解析出的表引用列表。

    输出：
    - `(table_name, pure_column)`；如果无法确定表归属，则表名返回 `None`。
    """
    normalized = column.strip().strip("`").lower()
    if "." in normalized:
        alias, pure_column = normalized.split(".", 1)
        for ref in table_refs:
            if ref["alias"].lower() == alias or ref["table"].lower() == alias:
                return ref["table"], pure_column
        return None, pure_column
    if len(table_refs) == 1:
        return table_refs[0]["table"], normalized
    return None, normalized


def analyze_index_metadata(
    entry: dict[str, Any],
    metadata_cache: dict[tuple[str, str], dict[str, Any]],
    lookup: dict[str, Any],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    """根据索引元数据判断字段是否缺少索引。

    作用：
    - 检查过滤列 / 关联列是否缺索引。
    - 检查排序列 / 分组列是否缺索引。
    - 结果用于静态推断潜在性能风险。

    输入：
    - `entry`：当前 SQL 条目。
    - `metadata_cache`：已读取的索引元数据缓存。
    - `lookup`：字段候选提取结果。
    - `config`：规则配置。

    输出：
    - 索引元数据风险列表。
    """
    findings: list[dict[str, Any]] = []
    block_levels = set(config.get("block_levels", []))
    table_refs = lookup.get("table_refs", [])
    table_map = {ref["table"].lower(): ref for ref in table_refs}

    def find_missing(rule_name: str, columns: list[str], level: str, reason_prefix: str, suggestion: str) -> None:
        """在指定字段集合中找出未建立索引的字段。

        作用：
        - 作为 `analyze_index_metadata` 内部复用逻辑，避免 where / order / group 检查重复写一套。
        """
        missing_columns: list[str] = []
        for column in columns:
            table_name, pure_column = resolve_column_table(column, table_refs)
            if not table_name:
                continue
            ref = table_map.get(table_name.lower())
            if not ref:
                continue
            meta = metadata_cache.get((ref["schema"], ref["table"]))
            if not meta:
                continue
            if pure_column not in meta["column_to_indexes"]:
                missing_columns.append(column)
        if missing_columns:
            add_finding(
                findings,
                entry,
                level,
                rule_name,
                f"{reason_prefix}：{', '.join(sorted(set(missing_columns)))}。",
                suggestion,
                level in block_levels,
                columns=sorted(set(missing_columns)),
            )

    find_missing(
        "missing_filter_index",
        lookup.get("where_columns", []) + lookup.get("join_columns", []),
        "P1",
        "过滤列或关联列未发现索引元数据",
        "请为过滤列或 JOIN 列补充合适索引，并确认联合索引顺序是否匹配查询条件。",
    )
    find_missing(
        "missing_sort_index",
        lookup.get("order_columns", []) + lookup.get("group_columns", []),
        "P2",
        "排序列或分组列未发现索引元数据",
        "请评估 ORDER BY/GROUP BY 列是否需要单列或联合索引支持。",
    )
    return findings


def run_explain(cursor: Any, sql: str) -> list[dict[str, Any]]:
    """执行 EXPLAIN 并返回执行计划。

    作用：
    - 将上一步规范化后的 SQL 送入数据库执行 `EXPLAIN`。

    输入：
    - `cursor`：数据库游标。
    - `sql`：已转换成可执行形式的 SQL。

    输出：
    - EXPLAIN 返回的计划行列表。
    """
    cursor.execute(f"EXPLAIN {sql}")
    return list(cursor.fetchall())


def analyze_explain(entry: dict[str, Any], plan_rows: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    """根据 EXPLAIN 结果判断运行时风险。

    作用：
    - 识别全表扫描、扫描行数过大、Using filesort、Using temporary、possible_keys 未命中等问题。
    - 这是对静态规则的补充，基于真实数据库优化器判断。

    输入：
    - `entry`：当前 SQL 条目。
    - `plan_rows`：EXPLAIN 返回的计划行。
    - `config`：规则配置。

    输出：
    - EXPLAIN 风险列表。
    """
    findings: list[dict[str, Any]] = []
    full_scan_threshold = int(os.getenv("AUDIT_FAIL_ON_FULL_SCAN_ROWS", "500"))
    warn_rows_threshold = int(os.getenv("AUDIT_MAX_ROWS", "1000"))
    block_levels = set(config.get("block_levels", []))
    sql = normalize_for_rules(entry["normalized_sql"])

    for row in plan_rows:
        access_type = str(row.get("type") or "").upper()
        rows_examined = int(row.get("rows") or 0)
        extra = str(row.get("Extra") or "")
        possible_keys = str(row.get("possible_keys") or "")
        chosen_key = str(row.get("key") or "")

        if access_type == "ALL" and rows_examined >= full_scan_threshold:
            add_finding(findings, entry, "P1", "explain_full_scan", f"EXPLAIN 显示全表扫描，预估扫描行数 {rows_examined}。", "补充索引或改写过滤条件，避免出现 type=ALL。", True, explain_row=row)
        elif rows_examined >= warn_rows_threshold:
            add_finding(findings, entry, "P2", "high_rows_estimate", f"EXPLAIN 预估扫描行数较高：{rows_examined}。", "确认索引命中情况，评估是否需要缩小扫描范围。", False, explain_row=row)

        if "Using filesort" in extra:
            add_finding(findings, entry, "P2", "using_filesort", "EXPLAIN 显示 Using filesort，排序可能有额外开销。", "评估 ORDER BY 与索引顺序是否匹配。", False, explain_row=row)
        if "Using temporary" in extra:
            add_finding(findings, entry, "P2", "using_temporary", "EXPLAIN 显示 Using temporary，可能产生临时表开销。", "评估 GROUP BY/ORDER BY 写法与索引设计。", False, explain_row=row)
        if not possible_keys and entry["sql_type"] in {"SELECT", "UPDATE", "DELETE"} and " where " in f" {sql} ":
            add_finding(findings, entry, "P2", "no_possible_keys", "EXPLAIN 未识别到 possible_keys，可用索引候选不足。", "检查过滤列是否建立合适索引。", False, explain_row=row)
        if possible_keys and not chosen_key and entry["sql_type"] in {"SELECT", "UPDATE", "DELETE"}:
            add_finding(findings, entry, "P1", "possible_keys_but_no_key", f"EXPLAIN 存在 possible_keys={possible_keys}，但实际未选择索引。", "检查条件选择性、函数包列、隐式类型转换或排序分组是否导致优化器放弃索引。", "P1" in block_levels, explain_row=row)

    return findings


def summarize(findings: list[dict[str, Any]]) -> dict[str, int]:
    """统计各风险等级的数量。

    作用：
    - 为最终 JSON 和 Markdown 报告提供汇总信息。

    输入：
    - 全部风险列表。

    输出：
    - 形如 `{"P0": x, "P1": y, "P2": z}` 的统计字典。
    """
    return {
        "P0": sum(1 for item in findings if item["level"] == "P0"),
        "P1": sum(1 for item in findings if item["level"] == "P1"),
        "P2": sum(1 for item in findings if item["level"] == "P2"),
    }


def main() -> int:
    """执行 GitLab SQL 审核主流程。

    流程说明：
    1. 解析命令行参数。
    2. 计算本次 MR 的变更文件。
    3. 从相关文件中提取 SQL。
    4. 执行静态规则。
    5. 若数据库可用，则补充索引元数据检查和 EXPLAIN。
    6. 汇总结果并输出 JSON。
    7. 若 `block_merge=true`，返回非零退出码以阻断合并。
    """
    parser = argparse.ArgumentParser(description="收集变更 SQL，执行静态规则检查、索引元数据检查，并在可用时运行 EXPLAIN。")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--rules", default="rules/mysql_rules.yaml")
    parser.add_argument("--output", required=True)
    parser.add_argument("--changed-files", nargs="*")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    rules_path = Path(args.rules).resolve()
    output_path = Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    changed_files = args.changed_files or run_git_diff(
        repo,
        os.getenv("CI_MERGE_REQUEST_TARGET_BRANCH_NAME"),
        os.getenv("CI_COMMIT_SHA"),
    )
    entries = collect_sql_candidates(repo, changed_files)
    config = load_rules(rules_path)

    explain_enabled = True
    index_metadata_enabled = True
    explain_errors: list[dict[str, Any]] = []
    explain_applied = 0
    findings: list[dict[str, Any]] = []
    index_metadata_cache: dict[tuple[str, str], dict[str, Any]] = {}

    conn = build_mysql_connection()
    if conn is None:
        explain_enabled = False
        index_metadata_enabled = False

    try:
        cursor = conn.cursor() if conn is not None else None
        for entry in entries:
            findings.extend(run_static_rules(entry, config))

            lookup = extract_index_usage_candidates(entry["normalized_sql"], entry["sql_type"])
            entry["index_lookup"] = lookup

            if cursor:
                for ref in lookup["table_refs"]:
                    cache_key = (ref["schema"], ref["table"])
                    if cache_key not in index_metadata_cache:
                        try:
                            index_metadata_cache[cache_key] = fetch_index_metadata(cursor, ref["schema"], ref["table"])
                        except Exception as exc:  # noqa: BLE001
                            index_metadata_cache[cache_key] = {
                                "schema": ref["schema"],
                                "table": ref["table"],
                                "indexes": {},
                                "column_to_indexes": {},
                                "error": str(exc),
                            }
                findings.extend(analyze_index_metadata(entry, index_metadata_cache, lookup, config))

            explain_sql = normalize_for_explain(entry["normalized_sql"])
            entry["explain_sql"] = explain_sql
            entry["explain_plan"] = []
            if not cursor or not explain_sql:
                continue
            try:
                plan_rows = run_explain(cursor, explain_sql)
                entry["explain_plan"] = plan_rows
                explain_applied += 1
                findings.extend(analyze_explain(entry, plan_rows, config))
            except Exception as exc:  # noqa: BLE001
                explain_errors.append({"statement_id": entry["statement_id"], "error": str(exc)})
    finally:
        if conn is not None:
            conn.close()

    serializable_indexes: dict[str, Any] = {}
    for (schema_name, table_name), meta in index_metadata_cache.items():
        serializable_indexes[f"{schema_name}.{table_name}" if schema_name else table_name] = {
            "schema": meta.get("schema"),
            "table": meta.get("table"),
            "indexes": meta.get("indexes", {}),
            "error": meta.get("error"),
        }

    findings.sort(key=lambda item: (item["level"], item["file"], item["statement_id"], item["rule"]))
    result = {
        "repo": str(repo),
        "rules_file": str(rules_path),
        "changed_files": changed_files,
        "sql_count": len(entries),
        "entries": entries,
        "finding_count": len(findings),
        "block_merge": any(item["block_merge"] for item in findings),
        "summary": summarize(findings),
        "findings": findings,
        "explain_enabled": explain_enabled,
        "explain_applied": explain_applied,
        "explain_errors": explain_errors,
        "index_metadata_enabled": index_metadata_enabled,
        "index_metadata": serializable_indexes,
    }
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 1 if result["block_merge"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

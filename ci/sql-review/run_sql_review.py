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


SQL_TAGS = {"select", "update", "delete", "insert"}
SQL_FILE_SUFFIXES = ("Mapper.xml", "Mapper.java", "Repository.java", "DAO.java")
DYNAMIC_SQL_TAGS = {"if", "choose", "when", "otherwise", "trim", "where", "set", "foreach"}
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


def normalize_sql(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


def load_rules(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def run_git_diff(repo: Path, target_branch: str | None, commit_sha: str | None) -> list[str]:
    if not target_branch or not commit_sha:
        return []
    cmd = ["git", "diff", "--name-only", f"origin/{target_branch}...{commit_sha}"]
    result = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def is_sql_related(path: str) -> bool:
    return path.endswith(SQL_FILE_SUFFIXES)


def collect_element_text(elem: ET.Element) -> str:
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
    value = value.strip()
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    return bytes(value, "utf-8").decode("unicode_escape")


def parse_annotation_payload(payload: str) -> str:
    strings = re.findall(r'"(?:[^"\\]|\\.)*"', payload, flags=re.S)
    return " ".join(decode_java_string_literal(item) for item in strings)


def extract_from_java(path: Path) -> list[dict[str, Any]]:
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
    return normalize_sql(sql).lower()


def normalize_for_explain(sql: str) -> str:
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
    statement_id = (entry.get("statement_id") or "").lower()
    keywords = [item.lower() for item in config.get("collection_query_keywords", [])]
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
    findings: list[dict[str, Any]] = []
    sql = normalize_for_rules(entry["normalized_sql"])
    sql_type = entry["sql_type"].upper()
    block_levels = set(config.get("block_levels", []))
    long_in_threshold = int(config.get("long_in_threshold", 5))
    pagination_keywords = [item.lower() for item in config.get("pagination_keywords", [])]
    function_tokens = [item.lower() for item in config.get("indexed_column_functions", [])]

    if sql_type == "SELECT" and re.search(r"^select\s+\*\s+from\b", sql):
        add_finding(findings, entry, "P1", "select_star", "查询使用 SELECT *，会扩大 I/O 和回表风险。", "明确列名，只查询必要字段。", "P1" in block_levels)

    if sql_type in {"UPDATE", "DELETE"} and " where " not in f" {sql} ":
        add_finding(findings, entry, "P0", "dml_without_where", "UPDATE/DELETE 未检测到 WHERE，存在全表修改或删除风险。", "补充精确 WHERE 条件，并增加防呆保护。", "P0" in block_levels)

    if " like " in f" {sql} " and re.search(r"like\s+['\"]?%", sql):
        add_finding(findings, entry, "P1", "leading_wildcard_like", "存在前导模糊 LIKE，索引通常无法命中。", "优先改成后缀模糊、倒排索引或搜索引擎方案。", "P1" in block_levels)

    if sql_type == "SELECT" and " from " in sql and not any(keyword in sql for keyword in pagination_keywords):
        if looks_like_collection_query(entry, sql, config):
            add_finding(findings, entry, "P2", "missing_pagination", "SELECT 未识别到分页关键字，且语句形态更像列表集合查询，可能存在大结果集风险。", "确认是否需要 LIMIT/OFFSET/PageHelper 等分页手段。", False)

    in_markers = re.findall(r"\?", sql)
    if " in " in sql and len(in_markers) >= long_in_threshold:
        add_finding(findings, entry, "P2", "long_in_list", "IN 参数较长，可能导致 SQL 过长或执行计划变差。", "考虑分批、临时表、JOIN 或其他替代方案。", False)

    if any(token in sql for token in function_tokens):
        add_finding(findings, entry, "P1", "function_on_column", "WHERE 条件中疑似对列做函数计算，可能导致索引失效。", "将函数计算移到参数侧，或建立函数索引/冗余列。", "P1" in block_levels)

    if entry.get("dynamic") and " where " not in f" {sql} ":
        add_finding(findings, entry, "P1", "dynamic_sql_full_scan_risk", "动态 SQL 未见稳定 WHERE，参数缺省时可能退化为全表扫描。", "为动态 SQL 增加兜底过滤条件，并补充空参数测试。", "P1" in block_levels)

    return findings


def build_mysql_connection() -> pymysql.connections.Connection | None:
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
    cleaned = raw_name.strip().strip("`")
    if "." in cleaned:
        schema_name, table_name = cleaned.split(".", 1)
        return schema_name.strip("`"), table_name.strip("`")
    return os.getenv("AUDIT_DB_NAME", ""), cleaned


def parse_table_refs(sql: str, sql_type: str) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    lowered = normalize_for_rules(sql)
    if sql_type == "SELECT":
        for match in re.finditer(r"\b(from|join)\s+([`.\w]+)(?:\s+(?:as\s+)?(\w+))?", lowered):
            table_name = match.group(2)
            alias = match.group(3) or table_name.split(".")[-1]
            schema_name, pure_table = split_table_name(table_name)
            refs.append({"schema": schema_name, "table": pure_table, "alias": alias})
    elif sql_type == "UPDATE":
        match = re.search(r"\bupdate\s+([`.\w]+)(?:\s+(?:as\s+)?(\w+))?", lowered)
        if match:
            table_name = match.group(1)
            alias = match.group(2) or table_name.split(".")[-1]
            schema_name, pure_table = split_table_name(table_name)
            refs.append({"schema": schema_name, "table": pure_table, "alias": alias})
    elif sql_type == "DELETE":
        match = re.search(r"\bdelete\s+from\s+([`.\w]+)(?:\s+(?:as\s+)?(\w+))?", lowered)
        if match:
            table_name = match.group(1)
            alias = match.group(2) or table_name.split(".")[-1]
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
    candidates: list[str] = []
    for match in re.finditer(
        r"([`.\w]+)\s*(=|!=|<>|>=|<=|>|<|\slike\b|\sregexp\b|\sin\b|\sbetween\b|\sis\b)",
        section,
        flags=re.I,
    ):
        raw_column = match.group(1).strip("`").lower()
        if raw_column in SQL_STOP_WORDS or raw_column.isdigit():
            continue
        candidates.append(raw_column)
    return candidates


def extract_list_columns(section: str) -> list[str]:
    columns: list[str] = []
    for chunk in section.split(","):
        token = chunk.strip().lower()
        token = re.sub(r"\s+(asc|desc)\b", "", token)
        token = token.strip("` ")
        if not token or token in SQL_STOP_WORDS:
            continue
        if "(" in token and ")" in token:
            continue
        columns.append(token)
    return columns


def extract_index_usage_candidates(sql: str, sql_type: str) -> dict[str, Any]:
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
    findings: list[dict[str, Any]] = []
    block_levels = set(config.get("block_levels", []))
    table_refs = lookup.get("table_refs", [])
    table_map = {ref["table"].lower(): ref for ref in table_refs}

    def find_missing(rule_name: str, columns: list[str], level: str, reason_prefix: str, suggestion: str) -> None:
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
        "过滤或关联列未发现索引元数据",
        "为过滤列/JOIN 列补充合适索引，并确认联合索引顺序是否匹配查询条件。",
    )
    find_missing(
        "missing_sort_index",
        lookup.get("order_columns", []) + lookup.get("group_columns", []),
        "P2",
        "排序或分组列未发现索引元数据",
        "评估 ORDER BY/GROUP BY 列是否需要单列或联合索引支持。",
    )
    return findings


def run_explain(cursor: Any, sql: str) -> list[dict[str, Any]]:
    cursor.execute(f"EXPLAIN {sql}")
    return list(cursor.fetchall())


def analyze_explain(entry: dict[str, Any], plan_rows: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
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
    return {
        "P0": sum(1 for item in findings if item["level"] == "P0"),
        "P1": sum(1 for item in findings if item["level"] == "P1"),
        "P2": sum(1 for item in findings if item["level"] == "P2"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect changed SQL, run static checks, inspect index metadata, and optionally EXPLAIN against a test database.")
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

#!/usr/bin/env python3
import argparse
import json
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from typing import Any

import pymysql
import yaml

SQL_TAGS = {"select", "update", "delete", "insert"}
EXPLAINABLE_TYPES = {"SELECT", "UPDATE", "DELETE"}
DYNAMIC_TAG_MARKERS = ("<if", "<choose", "<when", "<otherwise", "<trim", "<where", "<set", "<foreach", "<include:", "<script")
DB_ENV_KEYS = [
    "SQL_REVIEW_DB_HOST",
    "SQL_REVIEW_DB_PORT",
    "SQL_REVIEW_DB_NAME",
    "SQL_REVIEW_DB_USER",
    "SQL_REVIEW_DB_PASSWORD",
]


def normalize_sql(sql: str) -> str:
    return re.sub(r"\s+", " ", sql or "").strip()


def load_rules(path: str) -> dict[str, Any]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def get_changed_files(repo: Path, cli_changed_files: list[str] | None) -> list[str]:
    if cli_changed_files:
        return [item for item in cli_changed_files if item.strip()]

    github_base = os.getenv("GITHUB_BASE_SHA") or os.getenv("GITHUB_EVENT_PULL_REQUEST_BASE_SHA")
    github_head = os.getenv("GITHUB_HEAD_SHA") or os.getenv("GITHUB_EVENT_PULL_REQUEST_HEAD_SHA")
    if github_base and github_head:
        return git_diff_files(repo, github_base, github_head)

    mr_target = os.getenv("CI_MERGE_REQUEST_TARGET_BRANCH_NAME")
    commit_sha = os.getenv("CI_COMMIT_SHA")
    if mr_target and commit_sha:
        return git_diff_name_only(repo, f"origin/{mr_target}...{commit_sha}")

    return git_diff_name_only(repo, "HEAD~1...HEAD")


def git_diff_files(repo: Path, base_sha: str, head_sha: str) -> list[str]:
    return git_diff_name_only(repo, f"{base_sha}...{head_sha}")


def git_diff_name_only(repo: Path, revspec: str) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", revspec],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def collect_element_text(elem: ET.Element) -> str:
    parts: list[str] = []
    if elem.text:
        parts.append(elem.text)
    for child in list(elem):
        child_tag = child.tag.split("}")[-1]
        if child_tag in {"if", "choose", "when", "otherwise", "trim", "where", "set", "foreach", "script"}:
            parts.append(f" <{child_tag}> ")
        if child_tag == "include":
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
        normalized = normalize_sql(raw_sql)
        entries.append(
            {
                "file": str(path),
                "source_type": "xml",
                "sql_type": tag.upper(),
                "statement_id": f"{namespace}.{statement_id}" if namespace else statement_id,
                "raw_sql": raw_sql,
                "normalized_sql": normalized,
                "dynamic": any(marker in raw_sql.lower() for marker in DYNAMIC_TAG_MARKERS),
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
        normalized = normalize_sql(raw_sql)
        entries.append(
            {
                "file": str(path),
                "source_type": "java_annotation",
                "sql_type": annotation.upper(),
                "statement_id": ".".join(part for part in [package_name, type_name, method_name] if part),
                "raw_sql": raw_sql,
                "normalized_sql": normalized,
                "dynamic": any(marker in raw_sql.lower() for marker in DYNAMIC_TAG_MARKERS),
            }
        )
    return entries


def is_sql_related(path: str) -> bool:
    return path.endswith("Mapper.xml") or path.endswith("Mapper.java") or path.endswith("Repository.java") or path.endswith("DAO.java")


def add_static_finding(findings: list[dict[str, Any]], entry: dict[str, Any], level: str, rule: str, reason: str, suggestion: str, block: bool) -> None:
    findings.append(
        {
            "category": "static",
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
    )


def has_guarded_where(entry: dict[str, Any], sql: str) -> bool:
    if " where " in f" {sql} ":
        return True
    raw_sql = (entry.get("raw_sql") or "").lower()
    normalized_sql = (entry.get("normalized_sql") or "").lower()
    guard_tokens = (
        "<where>",
        "<include:example_where_clause>",
        "<include:update_by_example_where_clause>",
    )
    return any(token in raw_sql or token in normalized_sql for token in guard_tokens)


def should_check_dynamic_full_scan(entry: dict[str, Any], sql_type: str) -> bool:
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
    sql = normalize_sql(entry["normalized_sql"]).lower()
    sql_type = entry["sql_type"].upper()
    block_levels = set(config.get("block_levels", []))
    long_in_threshold = int(config.get("long_in_threshold", 5))
    pagination_keywords = [item.lower() for item in config.get("pagination_keywords", [])]
    function_tokens = [item.lower() for item in config.get("indexed_column_functions", [])]

    if sql_type == "SELECT" and re.search(r"^select\s+\*\s+from\b", sql):
        add_static_finding(findings, entry, "P1", "select_star", "查询使用 SELECT *，会扩大 I/O 和回表风险。", "明确列名，只查询必要字段。", "P1" in block_levels)

    if sql_type in {"UPDATE", "DELETE"} and not has_guarded_where(entry, sql):
        add_static_finding(findings, entry, "P0", "dml_without_where", "UPDATE/DELETE 未检测到 WHERE，存在全表修改/删除风险。", "补充精确 WHERE 条件，并增加防呆保护。", "P0" in block_levels)

    if " like " in f" {sql} " and re.search(r"like\s+['\"]?%", sql):
        add_static_finding(findings, entry, "P1", "leading_wildcard_like", "存在前导模糊 LIKE，索引通常无法命中。", "优先改成后缀模糊、倒排索引或搜索引擎方案。", "P1" in block_levels)

    if sql_type == "SELECT" and " from " in sql and not any(keyword in sql for keyword in pagination_keywords):
        if looks_like_collection_query(entry, sql, config):
            add_static_finding(findings, entry, "P2", "missing_pagination", "SELECT 未识别到分页关键字，且语句形态更像列表/集合查询，可能存在大结果集风险。", "确认是否需要 LIMIT/OFFSET/PageHelper 等分页手段。", False)

    in_markers = re.findall(r"\?", sql)
    if " in " in sql and len(in_markers) >= long_in_threshold:
        add_static_finding(findings, entry, "P2", "long_in_list", "IN 参数较长，可能导致 SQL 过长或执行计划变差。", "考虑分批、临时表、JOIN 或其他替代方案。", False)

    if any(token in sql for token in function_tokens):
        add_static_finding(findings, entry, "P1", "function_on_column", "WHERE/条件中疑似对列做函数计算，可能导致索引失效。", "将函数计算移到参数侧，或建立函数索引/冗余列。", "P1" in block_levels)

    if entry.get("dynamic") and should_check_dynamic_full_scan(entry, sql_type) and not has_guarded_where(entry, sql):
        add_static_finding(findings, entry, "P1", "dynamic_sql_full_scan_risk", "动态 SQL 未见稳定 WHERE，参数缺省时可能退化为全表扫描。", "为动态 SQL 增加兜底过滤条件，并补充空参数测试。", "P1" in block_levels)

    return findings


def explain_env() -> dict[str, str]:
    return {key: os.getenv(key, "") for key in DB_ENV_KEYS}


def missing_db_env(db_conf: dict[str, str]) -> list[str]:
    return [key for key, value in db_conf.items() if not value]


def create_db_connection(db_conf: dict[str, str]):
    return pymysql.connect(
        host=db_conf["SQL_REVIEW_DB_HOST"],
        port=int(db_conf["SQL_REVIEW_DB_PORT"]),
        user=db_conf["SQL_REVIEW_DB_USER"],
        password=db_conf["SQL_REVIEW_DB_PASSWORD"],
        database=db_conf["SQL_REVIEW_DB_NAME"],
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        read_timeout=5,
        write_timeout=5,
        connect_timeout=5,
        autocommit=True,
    )


def sanitize_sql_for_explain(entry: dict[str, Any]) -> tuple[bool, str | None, str | None]:
    sql_type = (entry.get("sql_type") or "").upper()
    sql = normalize_sql(entry.get("normalized_sql") or entry.get("raw_sql") or "")
    lowered = sql.lower()

    if sql_type not in EXPLAINABLE_TYPES:
        return False, None, f"sql type {sql_type} 不执行 EXPLAIN"
    if not sql:
        return False, None, "SQL 为空"
    if any(marker in lowered for marker in DYNAMIC_TAG_MARKERS):
        return False, None, "SQL 含 MyBatis 动态标签，当前无法安全展开"
    if "${" in sql:
        return False, None, "SQL 含 ${} 文本替换，占位不安全，跳过 EXPLAIN"

    explain_sql = re.sub(r"#\{[^}]+\}", "1", sql)
    explain_sql = re.sub(r"\?", "1", explain_sql)
    explain_sql = explain_sql.strip().rstrip(";")
    if not re.match(r"^(select|update|delete)\b", explain_sql, flags=re.I):
        return False, None, "SQL 不是可支持的 SELECT/UPDATE/DELETE 语句"
    return True, f"EXPLAIN {explain_sql}", None


def summarize_explain_rows(rows: list[dict[str, Any]], rows_threshold: int) -> tuple[str | None, list[dict[str, Any]], list[str]]:
    findings: list[dict[str, Any]] = []
    levels: list[str] = []

    for row in rows:
        access_type = str(row.get("type") or "")
        key_name = row.get("key")
        scanned_rows = int(row.get("rows") or 0)
        extra = str(row.get("Extra") or row.get("extra") or "")
        table_name = str(row.get("table") or "")

        if access_type.upper() == "ALL":
            level = "P1" if scanned_rows < rows_threshold else "P0"
            findings.append({"level": level, "rule": "explain_full_table_scan", "reason": f"table={table_name or '-'} type=ALL，疑似全表扫描", "evidence": {"type": access_type, "rows": scanned_rows, "key": key_name, "extra": extra}})
            levels.append(level)
        if not key_name:
            findings.append({"level": "P1", "rule": "explain_missing_index", "reason": f"table={table_name or '-'} 未命中索引(key=NULL)", "evidence": {"type": access_type, "rows": scanned_rows, "key": key_name, "extra": extra}})
            levels.append("P1")
        if scanned_rows >= rows_threshold:
            findings.append({"level": "P1", "rule": "explain_large_rows", "reason": f"预估扫描行数较大(rows={scanned_rows})", "evidence": {"type": access_type, "rows": scanned_rows, "key": key_name, "extra": extra}})
            levels.append("P1")
        if "Using filesort" in extra:
            findings.append({"level": "P2", "rule": "explain_using_filesort", "reason": "执行计划包含 Using filesort", "evidence": {"type": access_type, "rows": scanned_rows, "key": key_name, "extra": extra}})
            levels.append("P2")
        if "Using temporary" in extra:
            findings.append({"level": "P2", "rule": "explain_using_temporary", "reason": "执行计划包含 Using temporary", "evidence": {"type": access_type, "rows": scanned_rows, "key": key_name, "extra": extra}})
            levels.append("P2")

    highest = None
    for candidate in ("P0", "P1", "P2"):
        if candidate in levels:
            highest = candidate
            break
    return highest, findings, levels


def run_explain_for_entries(entries: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    db_conf = explain_env()
    missing = missing_db_env(db_conf)
    rows_threshold = int(config.get("explain_rows_threshold", 10000))
    explain_summary = {
        "enabled": True,
        "db_env_present": not missing,
        "db_env_missing": missing,
        "db_connected": False,
        "attempted": 0,
        "succeeded": 0,
        "failed": 0,
        "skipped": 0,
        "connection_error": None,
    }

    explainable_candidates: list[tuple[dict[str, Any], str]] = []
    for entry in entries:
        can_explain, explain_sql, reason = sanitize_sql_for_explain(entry)
        entry["explain"] = {
            "attempted": False,
            "explainable": can_explain,
            "success": False,
            "skipped_reason": reason,
            "error": None,
            "explain_sql": explain_sql,
            "rows": [],
            "findings": [],
            "risk_level": None,
        }
        if can_explain and explain_sql:
            explainable_candidates.append((entry, explain_sql))
        else:
            explain_summary["skipped"] += 1

    if missing:
        explain_summary["connection_error"] = f"missing env: {', '.join(missing)}"
        return explain_summary

    try:
        connection = create_db_connection(db_conf)
    except Exception as exc:
        explain_summary["connection_error"] = str(exc)
        return explain_summary

    explain_summary["db_connected"] = True
    try:
        with connection.cursor() as cursor:
            for entry, explain_sql in explainable_candidates:
                explain_summary["attempted"] += 1
                entry["explain"]["attempted"] = True
                try:
                    cursor.execute(explain_sql)
                    rows = cursor.fetchall()
                    risk_level, findings, _ = summarize_explain_rows(rows, rows_threshold)
                    entry["explain"].update(
                        {
                            "success": True,
                            "rows": rows,
                            "findings": findings,
                            "risk_level": risk_level,
                        }
                    )
                    explain_summary["succeeded"] += 1
                except Exception as exc:
                    entry["explain"].update({"success": False, "error": str(exc)})
                    explain_summary["failed"] += 1
    finally:
        connection.close()

    return explain_summary


def build_result(entries: list[dict[str, Any]], changed_files: list[str], explain_summary: dict[str, Any]) -> dict[str, Any]:
    all_findings: list[dict[str, Any]] = []
    summary = {"P0": 0, "P1": 0, "P2": 0}
    block_reasons: list[str] = []

    for entry in entries:
        for finding in entry.get("static_findings", []):
            all_findings.append(finding)
            summary[finding["level"]] += 1
            if finding["block_merge"]:
                block_reasons.append(f"static:{finding['rule']}:{entry['statement_id']}")
        for finding in entry.get("explain", {}).get("findings", []):
            block = finding["level"] in {"P0", "P1"}
            payload = {
                "category": "explain",
                "file": entry["file"],
                "statement_id": entry["statement_id"],
                "sql_type": entry["sql_type"],
                "level": finding["level"],
                "rule": finding["rule"],
                "reason": finding["reason"],
                "suggestion": "结合索引、过滤条件、排序字段调整 SQL 或索引设计。",
                "block_merge": block,
                "sql": entry["normalized_sql"],
                "evidence": finding.get("evidence", {}),
            }
            all_findings.append(payload)
            summary[payload["level"]] += 1
            if block:
                block_reasons.append(f"explain:{finding['rule']}:{entry['statement_id']}")

    all_findings.sort(key=lambda item: (item["level"], item["category"], item["file"], item["statement_id"]))
    return {
        "changed_files": changed_files,
        "sql_count": len(entries),
        "finding_count": len(all_findings),
        "block_merge": any(item["block_merge"] for item in all_findings),
        "block_reasons": block_reasons,
        "summary": summary,
        "explain_summary": explain_summary,
        "entries": entries,
        "findings": all_findings,
    }


def render_markdown(result: dict[str, Any]) -> str:
    grouped = defaultdict(list)
    for finding in result.get("findings", []):
        grouped[finding["level"]].append(finding)

    explain_summary = result.get("explain_summary", {})
    lines = [
        "# GitHub SQL 审查报告",
        "",
        f"- 改动文件数：{len(result.get('changed_files', []))}",
        f"- SQL 条目数：{result.get('sql_count', 0)}",
        f"- 风险条目数：{result.get('finding_count', 0)}",
        f"- 是否建议阻断：{'是' if result.get('block_merge') else '否'}",
        "",
        "## EXPLAIN 执行概览",
        "",
        f"- 数据库环境变量齐全：{'是' if explain_summary.get('db_env_present') else '否'}",
        f"- 数据库连接成功：{'是' if explain_summary.get('db_connected') else '否'}",
        f"- 已尝试 EXPLAIN：{explain_summary.get('attempted', 0)}",
        f"- 成功：{explain_summary.get('succeeded', 0)}",
        f"- 失败：{explain_summary.get('failed', 0)}",
        f"- 跳过：{explain_summary.get('skipped', 0)}",
    ]
    if explain_summary.get("db_env_missing"):
        lines.append(f"- 缺失环境变量：{', '.join(explain_summary['db_env_missing'])}")
    if explain_summary.get("connection_error"):
        lines.append(f"- 连接/执行说明：{explain_summary['connection_error']}")
    lines.append("")

    if not result.get("findings"):
        lines.append("未发现命中的静态风险；若 EXPLAIN 未执行，请补齐数据库环境。\n")
        return "\n".join(lines)

    for level in ["P0", "P1", "P2"]:
        items = grouped.get(level, [])
        lines.append(f"## {level}（{len(items)}）")
        lines.append("")
        if not items:
            lines.append("无")
            lines.append("")
            continue
        for idx, item in enumerate(items, 1):
            lines.append(f"### {level}-{idx} [{item['category']}] {item['rule']}")
            lines.append(f"- 文件：`{item['file']}`")
            lines.append(f"- 语句：`{item['statement_id']}`")
            lines.append(f"- SQL 类型：`{item['sql_type']}`")
            lines.append(f"- 阻断：`{'是' if item['block_merge'] else '否'}`")
            lines.append(f"- 原因：{item['reason']}")
            lines.append(f"- 建议：{item['suggestion']}")
            if item.get("evidence"):
                lines.append(f"- 证据：`{json.dumps(item['evidence'], ensure_ascii=False, sort_keys=True)}`")
            lines.append(f"- SQL：`{item['sql']}`")
            lines.append("")

    lines.append("## SQL 明细")
    lines.append("")
    for entry in result.get("entries", []):
        explain = entry.get("explain", {})
        lines.append(f"### `{entry['statement_id']}`")
        lines.append(f"- 文件：`{entry['file']}`")
        lines.append(f"- 类型：`{entry['sql_type']}`")
        lines.append(f"- 动态 SQL：`{'是' if entry.get('dynamic') else '否'}`")
        lines.append(f"- 静态命中数：`{len(entry.get('static_findings', []))}`")
        lines.append(f"- EXPLAIN 可执行：`{'是' if explain.get('explainable') else '否'}`")
        if explain.get("attempted"):
            lines.append(f"- EXPLAIN 成功：`{'是' if explain.get('success') else '否'}`")
        if explain.get("skipped_reason"):
            lines.append(f"- EXPLAIN 跳过原因：{explain['skipped_reason']}")
        if explain.get("error"):
            lines.append(f"- EXPLAIN 错误：{explain['error']}")
        if explain.get("risk_level"):
            lines.append(f"- EXPLAIN 风险等级：`{explain['risk_level']}`")
        lines.append(f"- SQL：`{entry['normalized_sql']}`")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=".")
    parser.add_argument("--changed-files", nargs="*")
    parser.add_argument("--rules", required=True)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-markdown", required=True)
    parser.add_argument("--output-sql", required=True)
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    changed_files = get_changed_files(repo, args.changed_files)
    entries: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for rel in changed_files:
        if not is_sql_related(rel):
            continue
        path = (repo / rel).resolve()
        if not path.exists() or path in seen:
            continue
        seen.add(path)
        if path.name.endswith("Mapper.xml"):
            entries.extend(extract_from_xml(path))
        elif path.suffix == ".java":
            entries.extend(extract_from_java(path))

    config = load_rules(args.rules)
    for entry in entries:
        entry["static_findings"] = run_static_rules(entry, config)

    explain_summary = run_explain_for_entries(entries, config)

    changed_sql = {
        "repo": str(repo),
        "changed_files": changed_files,
        "sql_count": len(entries),
        "entries": entries,
    }
    result = build_result(entries, changed_files, explain_summary)

    Path(args.output_sql).write_text(json.dumps(changed_sql, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    Path(args.output_json).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    Path(args.output_markdown).write_text(render_markdown(result), encoding="utf-8")
    return 1 if result.get("block_merge") else 0


if __name__ == "__main__":
    raise SystemExit(main())

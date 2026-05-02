#!/usr/bin/env python3
import argparse
import json
import re
from pathlib import Path
import yaml


def load_json(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding='utf-8'))


def normalize(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip().lower()


def add_finding(findings: list, entry: dict, level: str, rule: str, reason: str, suggestion: str, block: bool) -> None:
    findings.append({
        'file': entry['file'],
        'statement_id': entry['statement_id'],
        'sql_type': entry['sql_type'],
        'level': level,
        'rule': rule,
        'reason': reason,
        'suggestion': suggestion,
        'block_merge': block,
        'sql': entry['normalized_sql'],
    })


def looks_like_collection_query(entry: dict, sql: str, config: dict) -> bool:
    statement_id = (entry.get('statement_id') or '').lower()
    keywords = [item.lower() for item in config.get('collection_query_keywords', [])]
    if any(keyword in statement_id for keyword in keywords):
        return True
    if re.search(r'\bgroup\s+by\b|\border\s+by\b', sql):
        return True
    if re.search(r'\bwhere\s+id\s*=\s*([#${?]|\d+)', sql):
        return False
    if re.search(r'\bwhere\b', sql) and re.search(r'\b(and|or)\b', sql):
        return True
    return ' where ' not in f' {sql} '


def run_rules(entry: dict, config: dict) -> list[dict]:
    findings = []
    sql = normalize(entry['normalized_sql'])
    sql_type = entry['sql_type'].upper()
    block_levels = set(config.get('block_levels', []))
    long_in_threshold = int(config.get('long_in_threshold', 5))
    pagination_keywords = [item.lower() for item in config.get('pagination_keywords', [])]
    function_tokens = [item.lower() for item in config.get('indexed_column_functions', [])]

    if sql_type == 'SELECT' and re.search(r'^select\s+\*\s+from\b', sql):
        add_finding(findings, entry, 'P1', 'select_star', '查询使用 SELECT *，会扩大 I/O 和回表风险。', '明确列名，只查询必要字段。', 'P1' in block_levels)

    if sql_type in {'UPDATE', 'DELETE'} and ' where ' not in f' {sql} ':
        add_finding(findings, entry, 'P0', 'dml_without_where', 'UPDATE/DELETE 未检测到 WHERE，存在全表修改/删除风险。', '补充精确 WHERE 条件，并增加防呆保护。', 'P0' in block_levels)

    if ' like ' in f' {sql} ' and re.search(r"like\s+['\"]?%", sql):
        add_finding(findings, entry, 'P1', 'leading_wildcard_like', '存在前导模糊 LIKE，索引通常无法命中。', '优先改成后缀模糊、倒排索引或搜索引擎方案。', 'P1' in block_levels)

    if sql_type == 'SELECT' and ' from ' in sql and not any(keyword in sql for keyword in pagination_keywords):
        if looks_like_collection_query(entry, sql, config):
            add_finding(findings, entry, 'P2', 'missing_pagination', 'SELECT 未识别到分页关键字，且语句形态更像列表/集合查询，可能存在大结果集风险。', '确认是否需要 LIMIT/OFFSET/PageHelper 等分页手段。', False)

    in_markers = re.findall(r'\?', sql)
    if ' in ' in sql and len(in_markers) >= long_in_threshold:
        add_finding(findings, entry, 'P2', 'long_in_list', 'IN 参数较长，可能导致 SQL 过长或执行计划变差。', '考虑分批、临时表、JOIN 或其他替代方案。', False)

    if any(token in sql for token in function_tokens):
        add_finding(findings, entry, 'P1', 'function_on_column', 'WHERE/条件中疑似对列做函数计算，可能导致索引失效。', '将函数计算移到参数侧，或建立函数索引/冗余列。', 'P1' in block_levels)

    if entry.get('dynamic') and ' where ' not in f' {sql} ':
        add_finding(findings, entry, 'P1', 'dynamic_sql_full_scan_risk', '动态 SQL 未见稳定 WHERE，参数缺省时可能退化为全表扫描。', '为动态 SQL 增加兜底过滤条件，并补充空参数测试。', 'P1' in block_levels)

    return findings


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', required=True)
    parser.add_argument('--rules', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()

    data = load_json(args.input)
    config = yaml.safe_load(Path(args.rules).read_text(encoding='utf-8'))
    findings = []
    for entry in data.get('entries', []):
        findings.extend(run_rules(entry, config))

    findings.sort(key=lambda item: (item['level'], item['file'], item['statement_id']))
    summary = {
        'P0': sum(1 for item in findings if item['level'] == 'P0'),
        'P1': sum(1 for item in findings if item['level'] == 'P1'),
        'P2': sum(1 for item in findings if item['level'] == 'P2'),
    }
    result = {
        'sql_count': data.get('sql_count', 0),
        'finding_count': len(findings),
        'block_merge': any(item['block_merge'] for item in findings),
        'summary': summary,
        'findings': findings,
    }
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()

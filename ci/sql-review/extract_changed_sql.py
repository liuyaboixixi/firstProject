#!/usr/bin/env python3
import argparse
import json
import os
import re
import subprocess
from pathlib import Path
import xml.etree.ElementTree as ET

SQL_TAGS = {"select", "update", "delete", "insert"}
ANNOTATIONS = ("Select", "Update", "Delete", "Insert")


def normalize_sql(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


def get_changed_files(repo: Path, target_branch: str | None, commit_sha: str | None) -> list[str]:
    if not target_branch or not commit_sha:
        return []
    cmd = [
        "git", "diff", "--name-only",
        f"origin/{target_branch}...{commit_sha}",
    ]
    result = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def collect_element_text(elem: ET.Element) -> str:
    parts: list[str] = []
    if elem.text:
        parts.append(elem.text)
    for child in list(elem):
        if child.tag in {"if", "choose", "when", "otherwise", "trim", "where", "set", "foreach"}:
            parts.append(f" <{child.tag}> ")
        if child.tag == "include":
            refid = child.attrib.get("refid", "")
            parts.append(f" <include:{refid}> ")
        parts.append(collect_element_text(child))
        if child.tail:
            parts.append(child.tail)
    return "".join(parts)


def extract_from_xml(path: Path) -> list[dict]:
    tree = ET.parse(path)
    root = tree.getroot()
    namespace = root.attrib.get("namespace", "")
    entries = []
    for elem in root:
        tag = elem.tag.split('}')[-1]
        if tag not in SQL_TAGS:
            continue
        statement_id = elem.attrib.get("id", "")
        raw_sql = collect_element_text(elem)
        entries.append({
            "file": str(path),
            "source_type": "xml",
            "sql_type": tag.upper(),
            "statement_id": f"{namespace}.{statement_id}" if namespace else statement_id,
            "raw_sql": raw_sql,
            "normalized_sql": normalize_sql(raw_sql),
            "dynamic": any(t in raw_sql for t in ["<if>", "<choose>", "<foreach>", "<where>", "<set>"]),
        })
    return entries


def decode_java_string_literal(value: str) -> str:
    value = value.strip()
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    return bytes(value, 'utf-8').decode('unicode_escape')


def parse_annotation_payload(payload: str) -> str:
    strings = re.findall(r'"(?:[^"\\]|\\.)*"', payload, flags=re.S)
    return " ".join(decode_java_string_literal(item) for item in strings)


def extract_from_java(path: Path) -> list[dict]:
    text = path.read_text(encoding='utf-8')
    package_match = re.search(r'package\s+([\w.]+);', text)
    package_name = package_match.group(1) if package_match else ""
    interface_match = re.search(r'(?:interface|class)\s+(\w+)', text)
    type_name = interface_match.group(1) if interface_match else path.stem
    entries = []
    pattern = re.compile(
        r'@(Select|Update|Delete|Insert)\s*\((.*?)\)\s*[^;{]*?\b(\w+)\s*\(',
        flags=re.S,
    )
    for match in pattern.finditer(text):
        annotation, payload, method_name = match.groups()
        raw_sql = parse_annotation_payload(payload)
        entries.append({
            "file": str(path),
            "source_type": "java_annotation",
            "sql_type": annotation.upper(),
            "statement_id": ".".join(part for part in [package_name, type_name, method_name] if part),
            "raw_sql": raw_sql,
            "normalized_sql": normalize_sql(raw_sql),
            "dynamic": "<script>" in raw_sql or "<if" in raw_sql,
        })
    return entries


def is_sql_related(path: str) -> bool:
    return path.endswith('Mapper.xml') or path.endswith('Mapper.java') or path.endswith('Repository.java') or path.endswith('DAO.java')


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', default='.')
    parser.add_argument('--output', required=True)
    parser.add_argument('--changed-files', nargs='*')
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    changed_files = args.changed_files or get_changed_files(
        repo,
        os.getenv('CI_MERGE_REQUEST_TARGET_BRANCH_NAME'),
        os.getenv('CI_COMMIT_SHA'),
    )
    sql_entries = []
    seen = set()
    for rel in changed_files:
        if not is_sql_related(rel):
            continue
        path = (repo / rel).resolve()
        if not path.exists() or path in seen:
            continue
        seen.add(path)
        if path.name.endswith('Mapper.xml'):
            sql_entries.extend(extract_from_xml(path))
        elif path.suffix == '.java':
            sql_entries.extend(extract_from_java(path))

    output = {
        'repo': str(repo),
        'changed_files': changed_files,
        'sql_count': len(sql_entries),
        'entries': sql_entries,
    }
    Path(args.output).write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()

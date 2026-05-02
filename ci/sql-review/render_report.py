#!/usr/bin/env python3
import argparse
import json
from collections import defaultdict
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()

    data = json.loads(Path(args.input).read_text(encoding='utf-8'))
    grouped = defaultdict(list)
    for finding in data.get('findings', []):
        grouped[finding['level']].append(finding)

    lines = []
    lines.append('# GitLab SQL 审查报告')
    lines.append('')
    lines.append(f"- SQL 条目数：{data.get('sql_count', 0)}")
    lines.append(f"- 风险条目数：{data.get('finding_count', 0)}")
    lines.append(f"- 是否建议阻断：{'是' if data.get('block_merge') else '否'}")
    lines.append('')

    if not data.get('findings'):
        lines.append('未发现命中的 SQL 风险规则。')
    else:
        for level in ['P0', 'P1', 'P2']:
            items = grouped.get(level, [])
            lines.append(f'## {level}（{len(items)}）')
            lines.append('')
            if not items:
                lines.append('无')
                lines.append('')
                continue
            for idx, item in enumerate(items, 1):
                lines.append(f"### {level}-{idx} {item['rule']}")
                lines.append(f"- 文件：`{item['file']}`")
                lines.append(f"- 语句：`{item['statement_id']}`")
                lines.append(f"- SQL 类型：`{item['sql_type']}`")
                lines.append(f"- 阻断：`{'是' if item['block_merge'] else '否'}`")
                lines.append(f"- 原因：{item['reason']}")
                lines.append(f"- 建议：{item['suggestion']}")
                lines.append(f"- SQL：`{item['sql']}`")
                lines.append('')

    Path(args.output).write_text("\n".join(lines).strip() + "\n", encoding='utf-8')


if __name__ == '__main__':
    main()

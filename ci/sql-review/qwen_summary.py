#!/usr/bin/env python3
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', required=True)
    parser.add_argument('--prompt', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()

    input_path = Path(args.input)
    prompt_path = Path(args.prompt)
    output_path = Path(args.output)

    if not input_path.exists():
        output_path.write_text(
            json.dumps(
                {'status': 'degraded', 'reason': f'input file not found: {input_path}'},
                ensure_ascii=False,
                indent=2,
            ) + "\n",
            encoding='utf-8',
        )
        return 0

    input_data = input_path.read_text(encoding='utf-8')
    prompt_text = prompt_path.read_text(encoding='utf-8')

    qwen_path = shutil.which('qwen')
    if not qwen_path:
        fallback_bin = Path.home() / '.npm-global/bin/qwen'
        if fallback_bin.exists():
            qwen_path = str(fallback_bin)

    if not qwen_path:
        output_path.write_text('Qwen CLI 未安装，跳过 AI 总结。\n', encoding='utf-8')
        return 0

    full_prompt = f"{prompt_text}\n\n以下是待分析 JSON：\n{input_data}\n"
    result = subprocess.run(
        [qwen_path, '-y', full_prompt],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        fallback = {
            'status': 'degraded',
            'reason': 'qwen execution failed',
            'stderr': result.stderr.strip(),
            'stdout': result.stdout.strip(),
        }
        output_path.write_text(json.dumps(fallback, ensure_ascii=False, indent=2) + "\n", encoding='utf-8')
        return 0

    output_path.write_text(result.stdout.strip() + "\n", encoding='utf-8')
    return 0


if __name__ == '__main__':
    sys.exit(main())

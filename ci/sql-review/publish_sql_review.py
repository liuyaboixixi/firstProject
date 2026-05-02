#!/usr/bin/env python3
import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path


def load_review(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def render_markdown(data: dict) -> str:
    grouped = defaultdict(list)
    for finding in data.get("findings", []):
        grouped[finding["level"]].append(finding)

    lines = []
    lines.append("# GitLab SQL 审查报告")
    lines.append("")
    lines.append(f"- SQL 条目数：{data.get('sql_count', 0)}")
    lines.append(f"- 风险条目数：{data.get('finding_count', 0)}")
    lines.append(f"- 是否建议阻断：{'是' if data.get('block_merge') else '否'}")
    lines.append(f"- 是否启用 EXPLAIN：{'是' if data.get('explain_enabled') else '否'}")
    lines.append(f"- 成功执行 EXPLAIN 条数：{data.get('explain_applied', 0)}")
    lines.append(f"- 是否启用索引元数据检查：{'是' if data.get('index_metadata_enabled') else '否'}")
    lines.append("")

    if data.get("explain_errors"):
        lines.append("## EXPLAIN 异常")
        lines.append("")
        for item in data["explain_errors"]:
            lines.append(f"- `{item['statement_id']}`: {item['error']}")
        lines.append("")

    if data.get("index_metadata"):
        lines.append("## 索引元数据概览")
        lines.append("")
        for table_name, meta in sorted(data["index_metadata"].items()):
            if meta.get("error"):
                lines.append(f"- `{table_name}`: 读取失败，{meta['error']}")
                continue
            index_names = ", ".join(meta.get("indexes", {}).keys()) if meta.get("indexes") else "无"
            lines.append(f"- `{table_name}`: {index_names}")
        lines.append("")

    if not data.get("findings"):
        lines.append("未发现命中的 SQL 风险规则。")
        return "\n".join(lines).strip() + "\n"

    for level in ["P0", "P1", "P2"]:
        items = grouped.get(level, [])
        lines.append(f"## {level}（{len(items)}）")
        lines.append("")
        if not items:
            lines.append("无")
            lines.append("")
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
            if item.get("columns"):
                lines.append(f"- 相关列：`{', '.join(item['columns'])}`")
            if item.get("explain_row"):
                row = item["explain_row"]
                lines.append(
                    f"- EXPLAIN：`type={row.get('type')}, key={row.get('key')}, possible_keys={row.get('possible_keys')}, rows={row.get('rows')}, Extra={row.get('Extra')}`"
                )
            lines.append("")
    return "\n".join(lines).strip() + "\n"


def maybe_run_qwen(review_json: Path, prompt: Path) -> str | None:
    qwen_path = shutil.which("qwen")
    if not qwen_path:
        fallback_bin = Path.home() / ".npm-global/bin/qwen"
        if fallback_bin.exists():
            qwen_path = str(fallback_bin)
    if not qwen_path or not prompt.exists():
        return None

    input_data = review_json.read_text(encoding="utf-8")
    prompt_text = prompt.read_text(encoding="utf-8")
    full_prompt = f"{prompt_text}\n\n以下是待分析 JSON：\n{input_data}\n"
    result = subprocess.run(
        [qwen_path, "-y", full_prompt],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.strip() + "\n"


def maybe_post_gitlab(comment: str) -> tuple[bool, str]:
    token = os.getenv("GITLAB_ACCESS_TOKEN")
    project_id = os.getenv("CI_PROJECT_ID")
    mr_iid = os.getenv("CI_MERGE_REQUEST_IID")
    api_base = os.getenv("CI_API_V4_URL")
    if not all([token, project_id, mr_iid, api_base]):
        return False, "missing gitlab env"

    payload = json.dumps({"body": comment}, ensure_ascii=False).encode("utf-8")
    url = f"{api_base}/projects/{project_id}/merge_requests/{mr_iid}/discussions"
    request = urllib.request.Request(
        url=url,
        data=payload,
        method="POST",
        headers={
            "PRIVATE-TOKEN": token,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
            response.read()
        return True, "posted"
    except urllib.error.HTTPError as exc:
        return False, f"http error {exc.code}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Render SQL review report, optionally summarize with Qwen, and optionally publish to GitLab.")
    parser.add_argument("--input", required=True)
    parser.add_argument("--markdown-output", required=True)
    parser.add_argument("--comment-output")
    parser.add_argument("--prompt", default="prompts/sql_review_summary_prompt.md")
    parser.add_argument("--skip-qwen", action="store_true")
    parser.add_argument("--post-gitlab", action="store_true")
    args = parser.parse_args()

    review_path = Path(args.input).resolve()
    markdown_output = Path(args.markdown_output).resolve()
    comment_output = Path(args.comment_output).resolve() if args.comment_output else None
    prompt_path = Path(args.prompt).resolve()

    data = load_review(review_path)
    markdown = render_markdown(data)
    markdown_output.parent.mkdir(parents=True, exist_ok=True)
    markdown_output.write_text(markdown, encoding="utf-8")

    comment = markdown
    if not args.skip_qwen:
        ai_summary = maybe_run_qwen(review_path, prompt_path)
        if ai_summary:
            comment = ai_summary

    if comment_output:
        comment_output.parent.mkdir(parents=True, exist_ok=True)
        comment_output.write_text(comment, encoding="utf-8")

    if args.post_gitlab:
        ok, reason = maybe_post_gitlab(comment)
        if not ok:
            print(f"gitlab comment publish skipped/failed: {reason}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

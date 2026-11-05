#!/usr/bin/env python3
"""Render the canonical JSON report into a human-readable markdown view.

The markdown view is DERIVED DATA. Agents must consume the JSON, never parse
this file to make decisions.

Usage:
  python render_report.py report.json [--out report.md] [--print]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True  # keep the installed skill dir free of __pycache__
sys.path.insert(0, str(Path(__file__).resolve().parent))
from rcp_common import TIER_LABEL  # noqa: E402


def code_block(text, start):
    out = ["```python"]
    for i, line in enumerate(text.split("\n"), start):
        out.append(f"{i:>5} | {line}")
    out.append("```")
    return "\n".join(out)


def render(report):
    s = report.get("summary", {})
    by_safety = s.get("by_safety", {})
    lines = []
    lines.append("# 冗余代码清单（人类可读视图）")
    lines.append("")
    lines.append("> **Agent 注意**：本文件是派生视图，不是数据源。唯一权威数据是同目录的 "
                 "`redundancy-report.json`。决策、编辑、校验一律以 JSON 为准。")
    lines.append("")
    lines.append(f"生成时间：`{report.get('generated_at', '')}`  ")
    lines.append(f"扫描根：`{report.get('root', '')}`  ")
    lines.append(f"Schema：`{report.get('schema_version', '')}` / `{report.get('tool', '')}`")
    lines.append("")
    lines.append("## 1. 总览")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("|---|---|")
    lines.append(f"| 已分析 Python 文件 | {s.get('files_scanned', 0)} |")
    lines.append(f"| 总行数 | {s.get('total_lines', 0)} |")
    lines.append(f"| 冗余项总数 | {s.get('findings_total', 0)} |")
    lines.append(f"| STATIC（可直接精简） | {by_safety.get('static', 0)} |")
    lines.append(f"| VERIFIED（需测试/确认） | {by_safety.get('verified', 0)} |")
    lines.append(f"| REVIEW（仅报告） | {by_safety.get('review', 0)} |")
    lines.append(f"| 可自动精简行数 | {s.get('auto_eligible_lines', 0)} "
                 f"({round((s.get('auto_eligible_ratio', 0) or 0) * 100, 2)}%) |")
    lines.append("")
    if s.get("by_rule"):
        lines.append("### 按规则")
        lines.append("")
        lines.append("| rule_id | 数量 |")
        lines.append("|---|---|")
        for k, v in sorted(s["by_rule"].items(), key=lambda x: (-x[1], x[0])):
            lines.append(f"| `{k}` | {v} |")
        lines.append("")
    lines.append("### 安全档位（直接取自报告 JSON）")
    lines.append("")
    lines.append("| 档位 | 说明 |")
    lines.append("|---|---|")
    tiers = report.get("safety_tiers", {})
    for k in ("static", "verified", "review"):
        lines.append(f"| `{k}` | {tiers.get(k, TIER_LABEL.get(k, ''))} |")
    lines.append("")

    findings = report.get("findings", [])
    by_file = {}
    for f in findings:
        by_file.setdefault(f.get("file", "?"), []).append(f)

    lines.append(f"## 2. 多余代码明细（{len(findings)} 项）")
    lines.append("")
    if not findings:
        lines.append("_未发现冗余代码。_")
        lines.append("")
    for rel in sorted(by_file):
        lines.append(f"### `{rel}`")
        lines.append("")
        for f in sorted(by_file[rel], key=lambda x: (x["span"]["start_line"], x["rule_id"])):
            sp = f["span"]
            lines.append(f"#### `{f['id']}` · `{f['rule_id']}` · {TIER_LABEL.get(f['safety'], f['safety'])}")
            lines.append("")
            lines.append(f"- 标题：{f.get('title', '')}")
            lines.append(f"- 位置：`{rel}:{sp['start_line']}-{sp['end_line']}`")
            lines.append(f"- 置信度：{f.get('confidence', '')} ｜ 行为变化：`{f.get('behavior_change', '')}`")
            lines.append(f"- 编辑动作：`{f['edit']['action']}` ｜ 期望哈希：`{f.get('expected_hash', '')[:24]}…`")
            lines.append(f"- 判定理由：{f.get('reason', '')}")
            if f.get("why_blocked"):
                lines.append(f"- **未自动应用原因**：{f['why_blocked']}")
            if f.get("suggestion"):
                lines.append(f"- 处置建议：{f['suggestion']}")
            lines.append(f"- Agent 指令：{f.get('agent_hint', '')}")
            lines.append("")
            lines.append(code_block(f.get("snippet", ""), sp["start_line"]))
            lines.append("")
            if f["edit"]["action"] == "replace_lines":
                lines.append(f"替换为：`{f['edit']['replacement']}`")
                lines.append("")

    review = [f for f in findings if f.get("safety") == "review"]
    lines.append("## 3. 需人工确认项（REVIEW）")
    lines.append("")
    lines.append("| id | 文件:行 | rule_id | 拦截原因 | 建议 |")
    lines.append("|---|---|---|---|---|")
    for f in review:
        sp = f["span"]
        why = (f.get("why_blocked") or "").replace("|", "\\|")
        sug = (f.get("suggestion") or "").replace("|", "\\|")
        lines.append(f"| `{f['id']}` | `{f['file']}:{sp['start_line']}-{sp['end_line']}` | "
                     f"`{f['rule_id']}` | {why} | {sug} |")
    lines.append("")

    others = report.get("unanalyzed_files", [])
    lines.append("## 4. 非 Python 文件（需 Agent 按同一 schema 补写 finding）")
    lines.append("")
    if others:
        lines.append("| 文件 | 状态 |")
        lines.append("|---|---|")
        for o in others:
            lines.append(f"| `{o['file']}` | 未被 AST 分析器覆盖 |")
        lines.append("")
        lines.append("补写方式：阅读 `references/report-schema.md`，向 JSON 的 `findings` 数组追加同结构对象，"
                     "`expected_hash` 用 `sha256:` + 片段文本（以 `\\n` 连接、行尾归一化）的哈希，"
                     "然后重跑 `apply_prune.py --dry-run` 校验。")
        lines.append("")
    else:
        lines.append("_无。_")
        lines.append("")

    errs = report.get("parse_errors", [])
    if errs:
        lines.append("## 5. 解析失败文件")
        lines.append("")
        for e in errs:
            lines.append(f"- `{e['file']}`：{e['error']}")
        lines.append("")

    lines.append("## 6. 下一步")
    lines.append("")
    lines.append("```bash")
    lines.append("python scripts/apply_prune.py redundancy-report.json --dry-run")
    lines.append("python scripts/apply_prune.py redundancy-report.json --apply --tests \"pytest -q\"")
    lines.append("python scripts/apply_prune.py --rollback <backup_manifest.json>")
    lines.append("```")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(report.get("canonical_note", ""))
    lines.append("")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Render redundancy-report.json as markdown")
    ap.add_argument("report")
    ap.add_argument("--out", default=None)
    ap.add_argument("--print", dest="do_print", action="store_true")
    args = ap.parse_args(argv)
    data = json.loads(Path(args.report).read_text(encoding="utf-8"))
    md = render(data)
    if args.out:
        p = Path(args.out).resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(md, encoding="utf-8")
        print(f"markdown written: {p}")
    if args.do_print or not args.out:
        print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

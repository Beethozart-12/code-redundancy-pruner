#!/usr/bin/env python3
"""Single entry point for code-redundancy-pruner.

From-scratch users only need this file; it resolves the skill directory,
merges configuration, checks the environment and forwards to the worker
scripts. Stdlib only, Python 3.9+.

  python rcp.py doctor
  python rcp.py init-config
  python rcp.py scan . --exclude '*_test.py'
  python rcp.py prune redundancy-report.json --dry-run
  python rcp.py prune redundancy-report.json --apply --tests "pytest -q"
  python rcp.py --config path/to/rcp.config.json config
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True  # keep the installed skill dir free of __pycache__
sys.path.insert(0, str(Path(__file__).resolve().parent))
from rcp_common import (  # noqa: E402
    TOOL, SKILL_DIR, SCRIPTS_DIR, DEFAULT_CONFIG_PATH, REQUIRED_SCRIPTS,
    load_config, validate_config, public_config, find_project_config,
)


def run_script(name, argv):
    cmd = [sys.executable, str(SCRIPTS_DIR / name), *map(str, argv)]
    return subprocess.run(cmd).returncode


def _row(name, ok, detail):
    return f"| {name} | {'PASS' if ok else 'FAIL'} | {str(detail).replace('|', '/')} |"


def cmd_doctor(args):
    try:
        cfg = load_config(args.config)
    except (OSError, ValueError) as exc:
        print(f"[{TOOL}] doctor: cannot load config: {exc}")
        return 1
    need = tuple(cfg.get("python_min_version", [3, 9]))
    py = sys.version_info[:3]
    rows = [
        _row("Python >= " + ".".join(map(str, need)), py >= need,
             f"{'.'.join(map(str, py))} @ {sys.executable}"),
        _row("内置配置可读", DEFAULT_CONFIG_PATH.is_file(), DEFAULT_CONFIG_PATH.name),
    ]
    errs = validate_config(cfg)
    rows.append(_row("配置字段完整", not errs,
                     "; ".join(errs) if errs else json.dumps(cfg["_meta"]["loaded_from"], ensure_ascii=False)))
    missing = [s for s in REQUIRED_SCRIPTS if not (SCRIPTS_DIR / s).is_file()]
    rows.append(_row("脚本齐全", not missing,
                     f"{len(REQUIRED_SCRIPTS)} 个" + (f"，缺 {missing}" if missing else "")))
    bad = []
    for s in REQUIRED_SCRIPTS:
        p = SCRIPTS_DIR / s
        if p.is_file():
            try:
                compile(p.read_text(encoding="utf-8"), str(p), "exec")
            except SyntaxError as exc:
                bad.append(f"{s}:{exc.lineno}")
    rows.append(_row("脚本语法（compile）", not bad, "; ".join(bad) or "全部可编译"))
    fixture = SKILL_DIR / "assets" / "fixtures" / "sample_redundant.py"
    rows.append(_row("自检样本", fixture.is_file(), fixture.name))
    refs = [SKILL_DIR / "references" / n for n in
            ("quickstart.md", "report-schema.md", "redundancy-catalog.md", "lossless-guarantee.md")]
    rows.append(_row("参考文档", all(p.is_file() for p in refs),
                     f"{sum(p.is_file() for p in refs)}/4"))
    rows.append(_row("当前目录可写", os.access(os.getcwd(), os.W_OK), os.getcwd()))
    pconf = find_project_config()
    rows.append(_row("项目配置（可选）", True, pconf or "未创建 → rcp init-config"))

    print(f"\n=== {TOOL} doctor ===")
    print("| 检查项 | 结果 | 说明 |")
    print("|---|---|---|")
    print("\n".join(rows))
    failed = sum(1 for r in rows if "| FAIL |" in r)
    print(f"\nVERDICT: {'HEALTHY' if failed == 0 else 'NEEDS_ATTENTION'} ({len(rows) - failed}/{len(rows)} PASS)")

    if args.full:
        print("\n--- running full selftest ---")
        rc = run_script("selftest.py", [])
        if rc != 0:
            return rc
    # Print a path that actually works from the user's current directory;
    # a bare "scripts/rcp.py" only exists inside the skill folder.
    entry = os.path.relpath(str(SCRIPTS_DIR / "rcp.py"), os.getcwd()).replace(os.sep, "/")
    print(f"\nNEXT (cwd: {os.getcwd()}):")
    print(f'  python "{entry}" init-config')
    print(f'  python "{entry}" scan .')
    print(f'  python "{entry}" prune redundancy-report.json --dry-run')
    return 0 if failed == 0 else 1


def cmd_init_config(args):
    try:
        cfg = load_config(args.config)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 1
    dest = Path(args.dir or os.getcwd()).resolve() / cfg["output"]["config_filename"]
    if dest.exists() and not args.force:
        print(f"ERROR: already exists: {dest} (use --force to overwrite)")
        return 1
    template = {
        "config_version": "1.0",
        "_inherit": "未列出的字段自动继承技能内置 config/defaults.json；列表字段为整体覆盖，不是追加",
        "rule_overrides": {},
        "prune": {"default_test_command": None},
        "_examples": {
            "禁用规则": {"rule_overrides": {"PY_COMMENTED_OUT_CODE": False}},
            "强制降级": {"rule_overrides": {"PY_UNUSED_IMPORT": "review"}},
            "排除文件": {"scan": {"exclude_globs": ["*_test.py", "generated/*"]}},
            "默认测试命令": {"prune": {"default_test_command": "pytest -q"}},
            "自定义语法检查": {"language_syntax_commands": {"javascript": "node --check {file}"}},
        },
    }
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(template, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[{TOOL}] project config written: {dest}")
    print("  只写需要改的字段；未写字段自动继承内置默认值。查看生效配置：rcp config")
    return 0


def cmd_config(args):
    try:
        cfg = load_config(args.config)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 1
    print(json.dumps(public_config(cfg), ensure_ascii=False, indent=2))
    return 0


def cmd_scan(args, cfg):
    out = cfg["output"]
    argv = [args.target]
    rest = list(args.pass_args or [])
    no_md = "--no-md" in rest
    if "--no-md" in rest:
        rest.remove("--no-md")
    has_md = any(x in rest for x in ("--md",))
    if not no_md and not has_md:
        argv += ["--md", out["report_md"]]
    has_out = any(x in rest for x in ("--out",))
    if not has_out:
        argv += ["--out", out["report_json"]]
    if args.config and not any(x in rest for x in ("--config",)):
        argv += ["--config", args.config]
    argv += rest
    return run_script("scan_redundancy.py", argv)


def cmd_prune(args, cfg):
    argv = [args.report]
    rest = list(args.pass_args or [])
    if args.config and not any(x in rest for x in ("--config",)):
        argv += ["--config", args.config]
    argv += rest
    return run_script("apply_prune.py", argv)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=f"{TOOL}: scan, show and losslessly prune redundant code")
    ap.add_argument("--config", default=None,
                    help="explicit rcp.config.json (put BEFORE the subcommand)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("doctor", help="check environment, config and scripts")
    p.add_argument("--full", action="store_true", help="also run the full selftest")
    p.add_argument("--config", dest="sub_config", default=None, help=argparse.SUPPRESS)

    p = sub.add_parser("init-config", help="write a project rcp.config.json")
    p.add_argument("--dir", default=None, help="target directory (default: cwd)")
    p.add_argument("--force", action="store_true")
    p.add_argument("--config", dest="sub_config", default=None, help=argparse.SUPPRESS)

    p = sub.add_parser("config", help="print the effective merged config")
    p.add_argument("--config", dest="sub_config", default=None, help=argparse.SUPPRESS)

    p = sub.add_parser("scan", help="scan a file/directory -> report JSON + markdown")
    p.add_argument("target")
    p.add_argument("pass_args", nargs=argparse.REMAINDER,
                   help="extra scan_redundancy.py args, e.g. --exclude '*_test.py' --no-md")

    p = sub.add_parser("render", help="render an existing JSON report to markdown")
    p.add_argument("report")
    p.add_argument("pass_args", nargs=argparse.REMAINDER)

    p = sub.add_parser("prune", help="dry-run (default) or --apply lossless pruning")
    p.add_argument("report")
    p.add_argument("pass_args", nargs=argparse.REMAINDER)

    p = sub.add_parser("rollback", help="restore files from a backup manifest")
    p.add_argument("manifest")
    p.add_argument("pass_args", nargs=argparse.REMAINDER)

    sub.add_parser("selftest", help="run the end-to-end selftest")

    args = ap.parse_args(argv)
    # A hidden subcommand-level --config uses a different dest so it does not
    # overwrite the top-level value; when present it takes precedence.
    if getattr(args, "sub_config", None):
        args.config = args.sub_config

    if args.cmd == "doctor":
        return cmd_doctor(args)
    if args.cmd == "init-config":
        return cmd_init_config(args)
    if args.cmd == "config":
        return cmd_config(args)
    if args.cmd == "selftest":
        return run_script("selftest.py", [])
    if args.cmd == "render":
        return run_script("render_report.py", [args.report, *(args.pass_args or [])])
    if args.cmd == "rollback":
        return run_script("apply_prune.py",
                          ["--rollback", args.manifest, *(args.pass_args or [])])

    try:
        cfg = load_config(args.config)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 1
    if args.cmd == "scan":
        return cmd_scan(args, cfg)
    if args.cmd == "prune":
        return cmd_prune(args, cfg)
    ap.error(f"unknown command: {args.cmd}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

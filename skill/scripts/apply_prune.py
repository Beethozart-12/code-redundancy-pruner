#!/usr/bin/env python3
"""Lossless code pruner: validate, verify, apply, rollback.

Consumes the canonical JSON report from scan_redundancy.py. Language-agnostic
at the edit layer (pure line-span deletion / single-line replacement), so an
Agent can append findings for any language using the same schema.

Default mode is DRY-RUN. Nothing is written unless --apply is given.

Usage:
  python apply_prune.py report.json --dry-run
  python apply_prune.py report.json --apply --tests "pytest -q"
  python apply_prune.py report.json --apply --accept-verified
  python apply_prune.py --rollback .redundancy-pruner/backups/<stamp>/manifest.json
"""
from __future__ import annotations

import argparse
import ast
import builtins
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from datetime import datetime
from pathlib import Path

sys.dont_write_bytecode = True  # keep the installed skill dir free of __pycache__
sys.path.insert(0, str(Path(__file__).resolve().parent))
from rcp_common import (  # noqa: E402
    TOOL, ALLOWED_SAFETY, ALLOWED_ACTION, REQUIRED_FINDING_KEYS,
    h, sha, utcnow, read_raw, bodies_of, eol_of, ident_tokens,
    load_config, syntax_command, language_for,
)

BUILTINS = set(dir(builtins))


def collect_bindings(tree):
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            out.add(n.id)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
        elif isinstance(n, ast.arg):
            out.add(n.arg)
        elif isinstance(n, ast.alias):
            out.add(n.asname or n.name.split(".")[0])
        elif isinstance(n, ast.ExceptHandler) and n.name:
            out.add(n.name)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            out.update(n.names)
    return out


def collect_loads(tree):
    return {n.id for n in ast.walk(tree)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}


def regex_bindings(snippet):
    ded = textwrap.dedent(snippet)
    names = set()
    for m in re.finditer(
        r"^\s*(?:async\s+)?def\s+(\w+)|^\s*class\s+(\w+)|"
        r"^\s*([A-Za-z_]\w*)\s*(?:,[^=\n]+)?\s*=(?!=)",
        ded, re.M,
    ):
        names.update(g for g in m.groups() if g)
    for m in re.finditer(r"^\s*(?:from\s+\S+\s+)?import\s+(.+)$", ded, re.M):
        for part in m.group(1).split(","):
            tok = part.strip().split()
            if tok:
                names.add(tok[-1] if "as" in tok else tok[0].split(".")[0])
    return names


def bindings_in_snippet(snippet):
    try:
        return collect_bindings(ast.parse(textwrap.dedent(snippet)))
    except (SyntaxError, ValueError, IndentationError):
        return regex_bindings(snippet)


# ---------------------------------------------------------------- validation

def validate_report(report):
    errs = []
    if report.get("tool") != TOOL:
        errs.append("report.tool must be 'code-redundancy-pruner'")
    if "root" not in report:
        errs.append("missing report.root")
    findings = report.get("findings")
    if not isinstance(findings, list):
        errs.append("report.findings must be a list")
        return errs
    seen_ids = set()
    for i, f in enumerate(findings):
        tag = f"findings[{i}]"
        for k in REQUIRED_FINDING_KEYS:
            if k not in f:
                errs.append(f"{tag}: missing {k}")
        if any(k not in f for k in REQUIRED_FINDING_KEYS):
            continue
        if f["safety"] not in ALLOWED_SAFETY:
            errs.append(f"{tag}: invalid safety {f['safety']!r}")
        if f["id"] in seen_ids:
            errs.append(f"{tag}: duplicate id {f['id']}")
        seen_ids.add(f["id"])
        sp = f["span"]
        if not isinstance(sp, dict) or not all(k in sp for k in ("start_line", "end_line")):
            errs.append(f"{tag}: span needs start_line/end_line")
        elif not (isinstance(sp["start_line"], int) and isinstance(sp["end_line"], int)
                  and 1 <= sp["start_line"] <= sp["end_line"]):
            errs.append(f"{tag}: invalid span bounds")
        ed = f["edit"]
        if ed.get("action") not in ALLOWED_ACTION:
            errs.append(f"{tag}: invalid edit.action {ed.get('action')!r}")
        if ed.get("action") == "replace_lines" and not isinstance(ed.get("replacement"), str):
            errs.append(f"{tag}: replace_lines needs string replacement")
        if not isinstance(f["expected_hash"], str) or not f["expected_hash"].startswith("sha256:"):
            errs.append(f"{tag}: expected_hash must be 'sha256:<hex>'")
        if f.get("file", "").startswith(("/", "\\")) or ".." in Path(f.get("file", "")).parts:
            errs.append(f"{tag}: file must be a relative path under root")
    return errs


def git_dirty(root):
    try:
        p = subprocess.run(["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=20)
        if p.returncode != 0 or p.stdout.strip() != "true":
            return None
        q = subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=30)
        return bool(q.stdout.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def run_cmd(cmd, cwd, timeout):
    try:
        # errors="replace" is mandatory: on Windows a subprocess may emit
        # non-UTF-8 bytes (cp936 / WSL noise) and a bare text=True would kill
        # the reader thread with UnicodeDecodeError (ERR-20261105-001).
        p = subprocess.run(cmd, shell=True, cwd=str(cwd), capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=timeout)
        return p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


# ---------------------------------------------------------------- main pipeline

def prune(args):
    cfg = getattr(args, "cfg", None) or load_config(getattr(args, "config", None))
    rp = Path(args.report).resolve()
    report = json.loads(rp.read_text(encoding="utf-8"))
    errs = validate_report(report)
    if errs:
        print("ERROR: report validation failed:")
        for e in errs[:20]:
            print("  - " + e)
        return 2
    root = Path(args.cwd).resolve() if args.cwd else Path(report["root"]).resolve()
    if not root.exists():
        print(f"ERROR: root not found: {root}", file=sys.stderr)
        return 2

    findings = report["findings"]
    selected, skipped = [], []
    for f in findings:
        if f["safety"] == "review":
            skipped.append({"id": f["id"], "reason": "review tier: never auto-applied"})
            continue
        if f["safety"] == "verified" and not (args.accept_verified or args.tests):
            skipped.append({"id": f["id"], "reason": "verified tier: needs --tests or --accept-verified"})
            continue
        selected.append(f)

    # L0: span + hash gate against current on-disk content
    gate_ok, stale = [], []
    for f in selected:
        p = root / f["file"]
        if not p.is_file():
            stale.append({"id": f["id"], "reason": "file missing"})
            continue
        try:
            raw = read_raw(p)
        except (UnicodeDecodeError, OSError) as exc:
            stale.append({"id": f["id"], "reason": f"unreadable: {exc}"})
            continue
        b = bodies_of(raw.splitlines(keepends=True))
        s, e = f["span"]["start_line"], f["span"]["end_line"]
        if e > len(b):
            stale.append({"id": f["id"], "reason": f"span {s}-{e} out of range (file has {len(b)} lines)"})
            continue
        text = "\n".join(b[s - 1:e])
        if sha(text) != f["expected_hash"]:
            stale.append({"id": f["id"], "reason": "span_hash mismatch: file changed since scan; re-run scanner"})
            continue
        gate_ok.append(f)

    # per-file grouping + overlap detection
    by_file = {}
    for f in gate_ok:
        by_file.setdefault(f["file"], []).append(f)
    rejected = []
    for rel, group in list(by_file.items()):
        group.sort(key=lambda x: (x["span"]["start_line"], x["span"]["end_line"]))
        clean, last_end = [], 0
        for f in group:
            s, e = f["span"]["start_line"], f["span"]["end_line"]
            if s <= last_end:
                rejected.append({"id": f["id"], "reason": f"overlaps another finding in {rel}"})
                continue
            clean.append(f)
            last_end = e
        if clean:
            by_file[rel] = clean
        else:
            del by_file[rel]

    plans = []
    for rel in sorted(by_file):
        p = root / rel
        raw = read_raw(p)
        lines = raw.splitlines(keepends=True)
        b = bodies_of(lines)
        new_lines = list(lines)
        edits = sorted(by_file[rel], key=lambda x: x["span"]["start_line"], reverse=True)
        file_err = None
        for f in edits:
            ed = f["edit"]
            s, e = f["span"]["start_line"], f["span"]["end_line"]
            if ed["action"] == "delete_lines":
                new_lines[s - 1:e] = []
            else:
                repl = ed["replacement"]
                if "\n" in repl or "\r" in repl:
                    file_err = f"{f['id']}: replace_lines replacement must be a single line"
                    break
                eol = eol_of(lines[e - 1]) if 0 < e <= len(lines) else ""
                new_lines[s - 1:e] = [repl + eol]
        if file_err:
            rejected.append({"id": "-", "reason": file_err})
            continue
        new_raw = "".join(new_lines)
        plans.append({"rel": rel, "path": p, "old_raw": raw, "new_raw": new_raw,
                      "old_lines": len(lines), "new_lines": len(new_lines),
                      "edits": by_file[rel]})

    # L1/L2/L3 verification
    hard_fail, warnings = [], []
    for pl in plans:
        checks = ["L0_span_hash"]
        rel, old, new = pl["rel"], pl["old_raw"], pl["new_raw"]
        is_py = rel.lower().endswith(".py")

        # L2: deletion-only / token-subset, no line growth
        l2_ok = True
        for f in pl["edits"]:
            ed = f["edit"]
            s, e = f["span"]["start_line"], f["span"]["end_line"]
            orig = "\n".join(bodies_of(old.splitlines(keepends=True))[s - 1:e])
            if ed["action"] == "replace_lines":
                if not ident_tokens(ed["replacement"]) <= ident_tokens(orig):
                    hard_fail.append(f"{f['id']}: L2 token_subset failed (replacement introduces new identifiers)")
                    l2_ok = False
                    break
                if len(ed["replacement"]) > len(orig):
                    hard_fail.append(f"{f['id']}: L2 replacement longer than original span")
                    l2_ok = False
                    break
        if not l2_ok:
            continue
        if pl["new_lines"] > pl["old_lines"]:
            hard_fail.append(f"{rel}: L2 line count increased")
            continue
        checks.append("L2_deletion_or_token_subset")

        # L1: syntax
        if is_py:
            try:
                new_tree = ast.parse(new, filename=rel)
                checks.append("L1_ast_parse")
            except SyntaxError as exc:
                hard_fail.append(f"{rel}: L1 syntax error after pruning: {exc.msg} (line {exc.lineno})")
                continue
        else:
            new_tree = None
            finding_lang = next((f.get("language") for f in pl["edits"] if f.get("language")), None)
            cmd_tpl = args.syntax_cmd or syntax_command(
                cfg, finding_lang or language_for(rel, cfg), Path(rel).suffix.lower())
            if not cmd_tpl:
                warnings.append(
                    f"{rel}: L1 skipped - no syntax checker available for "
                    f"{finding_lang or language_for(rel, cfg) or Path(rel).suffix} "
                    "on this platform (install the tool, or set language_syntax_commands / --syntax-cmd)")
                checks.append("L1_skipped_no_command")
            else:
                tmp = Path(args.tmp_dir or str(root)) / (
                    ".rcp_check_" + re.sub(r"\W", "_", rel)[-60:] + Path(rel).suffix)
                tmp.parent.mkdir(parents=True, exist_ok=True)
                # Path.write_text(newline=...) is Python 3.10+; use open() so
                # the skill keeps its 3.9+ promise and CRLF stays byte-exact.
                with open(tmp, "w", encoding="utf-8", newline="") as fh:
                    fh.write(new)
                cmd = cmd_tpl.replace("{file}", str(tmp))
                rc, _, err = run_cmd(cmd, root, args.test_timeout)
                try:
                    tmp.unlink()
                except OSError:
                    pass
                if rc == 0:
                    checks.append("L1_syntax_cmd")
                else:
                    hard_fail.append(f"{rel}: L1 syntax command failed (rc={rc}): {err.strip()[:200]}")
                    continue

        # L3: name resolution (python only)
        if is_py and new_tree is not None:
            removed = set()
            for f in pl["edits"]:
                removed.update(f.get("evidence", {}).get("unbound_names", []))
                removed.update(bindings_in_snippet(f["snippet"]))
            bound_new = collect_bindings(new_tree)
            loads_new = collect_loads(new_tree)
            danger = ((removed - bound_new) & loads_new) - BUILTINS
            if danger:
                hard_fail.append(f"{rel}: L3 name_resolution failed: removed bindings still loaded: {sorted(danger)}")
                continue
            checks.append("L3_name_resolution")

        pl["checks"] = checks
        pl["diff"] = "".join(difflib.unified_diff(
            old.splitlines(keepends=True), new.splitlines(keepends=True),
            fromfile="a/" + rel, tofile="b/" + rel))
        if args.tests and args.apply:
            pl["level"] = "L4"
        elif args.tests:
            pl["level"] = "L4-pending"
        else:
            pl["level"] = "L3" if "L3_name_resolution" in checks else "L2"

    if hard_fail:
        print("ERROR: verification failed; no files were modified:")
        for x in hard_fail[:20]:
            print("  - " + x)
        return 3

    # L4: behavioral tests (baseline must pass before any write)
    tests_info = None
    baseline = None
    if args.tests:
        rc, out, err = run_cmd(args.tests, root, args.test_timeout)
        baseline = {"returncode": rc, "stdout_sha256": h(out)}
        if rc != 0:
            print(f"ERROR: baseline test command failed (rc={rc}); aborting. stderr: {err.strip()[:300]}")
            return 3
        tests_info = {"command": args.tests, "cwd": str(root),
                      "baseline": baseline, "after": None,
                      "mode": "apply" if args.apply else "dry-run (after-run skipped)"}

    applied_ids = sorted(f["id"] for pl in plans for f in pl["edits"])
    manifest_path = None
    if args.apply and plans:
        dirty = git_dirty(root)
        if dirty and not args.allow_dirty:
            print("ERROR: git working tree has uncommitted changes. Commit/stash first, "
                  "or pass --allow-dirty. No files were modified.")
            return 3
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        bdir = Path(args.backup_dir).resolve() / stamp
        (bdir / "files").mkdir(parents=True, exist_ok=True)
        for pl in plans:
            dst = bdir / "files" / pl["rel"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(pl["path"], dst)
        manifest = {
            "schema_version": "1.0", "tool": TOOL, "created_at": utcnow(),
            "root": str(root).replace(os.sep, "/"), "report": str(rp).replace(os.sep, "/"),
            "applied_ids": applied_ids,
            "files": [{"file": pl["rel"], "sha256_before": h(pl["old_raw"])} for pl in plans],
        }
        manifest_path = bdir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        for pl in plans:
            with open(pl["path"], "w", encoding="utf-8", newline="") as fh:
                fh.write(pl["new_raw"])

        if args.tests:
            rc, out, err = run_cmd(args.tests, root, args.test_timeout)
            tests_info["after"] = {"returncode": rc, "stdout_sha256": h(out)}
            bad = rc != 0 or (not args.no_compare_output and h(out) != baseline["stdout_sha256"])
            if bad:
                for pl in plans:
                    shutil.copy2(bdir / "files" / pl["rel"], pl["path"])
                print("ERROR: tests failed / output changed after pruning. Automatic rollback performed.")
                return 4

    result = {
        "schema_version": "1.0", "tool": TOOL, "generated_at": utcnow(),
        "mode": "apply" if args.apply else "dry-run",
        "root": str(root).replace(os.sep, "/"),
        "report": str(rp).replace(os.sep, "/"),
        "applied_ids": applied_ids if args.apply else [],
        "would_apply_ids": [] if args.apply else applied_ids,
        "verified_accepted_without_tests": bool(args.accept_verified and not args.tests),
        "skipped": skipped + stale,
        "rejected": rejected,
        "warnings": warnings,
        "files": [
            {"file": pl["rel"], "lines_before": pl["old_lines"], "lines_after": pl["new_lines"],
             "lines_removed": pl["old_lines"] - pl["new_lines"],
             "verification": pl.get("checks", []), "verification_level": pl.get("level", "L0"),
             "edits": [{"id": f["id"], "rule_id": f["rule_id"], "action": f["edit"]["action"],
                        "span": f["span"]} for f in pl["edits"]]}
            for pl in plans
        ],
        "tests": tests_info,
        "backup_manifest": str(manifest_path).replace(os.sep, "/") if manifest_path else None,
        "rollback_hint": (f"python scripts/apply_prune.py --rollback {manifest_path}"
                          if manifest_path else "dry-run: nothing to roll back"),
        "diff": "\n".join(pl.get("diff", "") for pl in plans),
        "lossless_definition": "语义等价（仅删除/标识符子集替换）+ 结构与可读性不退化 + 可回滚",
    }
    out_path = Path(args.out).resolve() if args.out else rp.parent / "prune-result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[{TOOL}] mode={result['mode']}  files={len(plans)}  "
          f"edits={len(applied_ids)}  lines_removed="
          f"{sum(x['lines_removed'] for x in result['files'])}")
    for x in result["files"]:
        print(f"  {x['file']}: -{x['lines_removed']} lines  "
              f"verify={x['verification_level']} [{', '.join(x['verification'])}]")
    if skipped or stale or rejected:
        print(f"  skipped={len(skipped) + len(stale)}  rejected={len(rejected)}")
    if warnings:
        for w in warnings[:5]:
            print("  WARN " + w)
    if args.tests:
        print(f"  tests: baseline rc={baseline['returncode']}" +
              (f", after rc={tests_info['after']['returncode']}" if tests_info.get("after") else ""))
    print(f"  result JSON: {out_path}")
    if result["backup_manifest"]:
        print(f"  backup manifest: {result['backup_manifest']}")
    if args.show_diff and result["diff"]:
        print("\n--- unified diff ---\n" + result["diff"])
    return 0


def rollback(args):
    mp = Path(args.rollback).resolve()
    m = json.loads(mp.read_text(encoding="utf-8"))
    if m.get("tool") != TOOL:
        print("ERROR: not a code-redundancy-pruner manifest", file=sys.stderr)
        return 2
    root = Path(args.rollback_root).resolve() if args.rollback_root else Path(m["root"])
    bdir = mp.parent / "files"
    restored, failed = [], []
    for item in m["files"]:
        src, dst = bdir / item["file"], root / item["file"]
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            if h(read_raw(dst)) == item["sha256_before"]:
                restored.append(item["file"])
            else:
                failed.append(f"{item['file']}: hash mismatch after restore")
        except OSError as exc:
            failed.append(f"{item['file']}: {exc}")
    print(f"[{TOOL}] rollback: restored={len(restored)} failed={len(failed)}")
    for f in failed:
        print("  - " + f)
    return 0 if not failed else 3


def main(argv=None):
    ap = argparse.ArgumentParser(description="Verify and apply lossless code pruning from a JSON report")
    ap.add_argument("report", nargs="?", help="path to redundancy-report.json")
    ap.add_argument("--apply", action="store_true", help="actually write files (default: dry-run)")
    ap.add_argument("--dry-run", action="store_true", help="explicit dry-run (default)")
    ap.add_argument("--tests", default=None, help="test command; must pass before AND after pruning")
    ap.add_argument("--accept-verified", action="store_true",
                    help="allow VERIFIED-tier findings without tests (user-acknowledged risk)")
    ap.add_argument("--syntax-cmd", default=None,
                    help="non-python syntax check template, e.g. 'node --check {file}'")
    ap.add_argument("--cwd", default=None, help="override report root / test cwd")
    ap.add_argument("--backup-dir", default=None, help="backup root (default <root>/.redundancy-pruner/backups)")
    ap.add_argument("--out", default=None, help="result JSON path (default prune-result.json next to report)")
    ap.add_argument("--show-diff", action="store_true")
    ap.add_argument("--no-compare-output", action="store_true",
                    help="only require test exit codes, do not compare stdout")
    ap.add_argument("--allow-dirty", action="store_true", help="allow applying on a dirty git tree")
    ap.add_argument("--test-timeout", type=int, default=None)
    ap.add_argument("--tmp-dir", default=None)
    ap.add_argument("--config", default=None, help="explicit rcp.config.json path")
    ap.add_argument("--rollback", default=None, help="restore from a backup manifest.json")
    ap.add_argument("--rollback-root", default=None)
    args = ap.parse_args(argv)

    if args.rollback:
        return rollback(args)
    if not args.report:
        ap.error("report path is required (or use --rollback)")

    cfg = load_config(args.config)
    pc = cfg["prune"]
    args.cfg = cfg
    if args.tests is None:
        args.tests = pc.get("default_test_command")
    if args.syntax_cmd is None:
        args.syntax_cmd = pc.get("syntax_command")
    if args.test_timeout is None:
        args.test_timeout = pc.get("test_timeout") or 600
    if not args.no_compare_output:
        args.no_compare_output = bool(pc.get("no_compare_output"))
    if args.out is None:
        args.out = str(Path(args.report).resolve().parent / cfg["output"]["prune_result"])

    rp = Path(args.report).resolve()
    if args.backup_dir is None:
        r = json.loads(rp.read_text(encoding="utf-8"))
        base = Path(args.cwd).resolve() if args.cwd else Path(r.get("root", ".")).resolve()
        args.backup_dir = str(base / pc["backup_dirname"])
    return prune(args)


if __name__ == "__main__":
    raise SystemExit(main())

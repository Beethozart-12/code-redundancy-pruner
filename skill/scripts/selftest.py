#!/usr/bin/env python3
"""End-to-end selftest: scan -> render -> dry-run -> apply -> verify -> rollback.

Runs in a throwaway temp directory; never touches real project files.

Usage:
  python scripts/selftest.py
"""
from __future__ import annotations

import ast
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
SCRIPTS = SKILL / "scripts"
FIXTURE = SKILL / "assets" / "fixtures" / "sample_redundant.py"

EXPECTED_RULES = {
    "PY_UNUSED_IMPORT",
    "PY_REDUNDANT_PASS",
    "PY_NOOP_LITERAL_STMT",
    "PY_SELF_ASSIGN",
    "PY_UNREACHABLE_AFTER_TERMINATOR",
    "PY_UNUSED_EXCEPT_BINDING",
    "PY_UNUSED_LOCAL",
    "PY_COMMENTED_OUT_CODE",
}
FORBIDDEN_AFTER = [
    "import os",
    "from typing import List",
    "result = result",
    '"stray no-op string"',
    "dead_var = 7",
    "as exc:",
]


def py(*args, cwd=None):
    return subprocess.run([sys.executable, *map(str, args)], cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True, errors="replace", timeout=120)


def check(name, cond, detail="", results=None):
    ok = bool(cond)
    (results if results is not None else []).append((name, "PASS" if ok else "FAIL", detail))
    return ok


def main():
    results = []
    tmp = Path(tempfile.mkdtemp(prefix="rcp-selftest-"))
    work = tmp / "proj"
    work.mkdir(parents=True)
    target = work / "sample.py"
    shutil.copy2(FIXTURE, target)
    original = target.read_bytes()

    # 1. scan
    r = py(SCRIPTS / "scan_redundancy.py", target,
           "--out", work / "report.json", "--md", work / "report.md", cwd=work)
    check("scan exits 0", r.returncode == 0, r.stderr.strip()[:200], results)
    if r.returncode != 0:
        return report(results)
    rep = json.loads((work / "report.json").read_text(encoding="utf-8"))
    rules = {f["rule_id"] for f in rep["findings"]}
    missing = sorted(EXPECTED_RULES - rules)
    check("all expected rules detected", not missing, "missing=" + str(missing), results)
    check("JSON schema fields present",
          all(k in rep for k in ("schema_version", "root", "summary", "findings", "safety_tiers")),
          "", results)
    check("stable ids + sorted findings",
          [f["id"] for f in rep["findings"]] == [f"F{i:04d}" for i in range(1, len(rep["findings"]) + 1)],
          f"n={len(rep['findings'])}", results)
    check("every finding has agent_hint + edit + hash",
          all(f.get("agent_hint") and f.get("edit") and f.get("expected_hash", "").startswith("sha256:")
              for f in rep["findings"]), "", results)
    check("review findings never auto-applicable",
          all(f["safety"] != "review" or f["verification"] == [] for f in rep["findings"]), "", results)

    # 2. markdown view
    md = (work / "report.md").read_text(encoding="utf-8")
    check("markdown view renders", "冗余代码清单" in md and "PY_UNUSED_IMPORT" in md, "", results)

    # 3. dry-run (no writes)
    r = py(SCRIPTS / "apply_prune.py", work / "report.json", "--dry-run",
           "--out", work / "dry.json", cwd=work)
    check("dry-run exits 0", r.returncode == 0, r.stdout.strip()[-300:] + r.stderr.strip()[:200], results)
    check("dry-run does not modify source", target.read_bytes() == original, "", results)
    dry = json.loads((work / "dry.json").read_text(encoding="utf-8")) if (work / "dry.json").exists() else {}
    check("verified tier gated without --tests",
          any("needs --tests" in x.get("reason", "") for x in dry.get("skipped", [])), "", results)

    # 4. apply
    r = py(SCRIPTS / "apply_prune.py", work / "report.json", "--apply",
           "--accept-verified", "--allow-dirty", "--show-diff",
           "--out", work / "apply.json", cwd=work)
    check("apply exits 0", r.returncode == 0,
          r.stdout.strip()[-600:] + r.stderr.strip()[:300], results)
    if r.returncode != 0:
        return report(results)
    res = json.loads((work / "apply.json").read_text(encoding="utf-8"))
    check("edits actually applied", len(res["applied_ids"]) >= 6,
          f"applied={len(res['applied_ids'])}", results)
    check("L3 name_resolution passed",
          all("L3_name_resolution" in x["verification"] for x in res["files"]), "", results)
    check("unified diff produced", bool(res["diff"]), "", results)

    # 5. semantics / readability of the pruned file
    new_text = target.read_text(encoding="utf-8")
    try:
        ast.parse(new_text)
        parsed = True
    except SyntaxError as exc:
        parsed = False
        new_text += f"\n# {exc}"
    check("pruned file still parses", parsed, "", results)
    leftovers = [s for s in FORBIDDEN_AFTER if s in new_text]
    check("targeted redundancy removed", not leftovers, "left=" + str(leftovers), results)
    check("meaningful code retained",
          all(s in new_text for s in ("def compute", "math.sqrt", "json.dumps", "import json",
                                      "return payload", "class Holder")), "", results)
    check("no readability degradation (line count did not grow)",
          len(new_text.splitlines()) <= len(original.decode("utf-8").splitlines()), "", results)

    # 6. rollback
    manifest = res.get("backup_manifest")
    check("backup manifest created", bool(manifest), "", results)
    if manifest:
        r = py(SCRIPTS / "apply_prune.py", "--rollback", manifest, cwd=work)
        check("rollback exits 0", r.returncode == 0, r.stdout.strip()[:200], results)
        check("rollback restores byte-identical original", target.read_bytes() == original, "", results)

    # 7. standalone renderer
    r = py(SCRIPTS / "render_report.py", work / "report.json", "--out", work / "again.md", cwd=work)
    check("standalone renderer exits 0", r.returncode == 0, r.stderr.strip()[:200], results)

    shutil.rmtree(tmp, ignore_errors=True)
    phase2(results)
    phase3(results)
    phase4(results)
    return report(results)


def phase2(results):
    """Edge cases: CRLF, stale hash, Agent-authored non-python finding, test rollback."""
    tmp = Path(tempfile.mkdtemp(prefix="rcp-selftest2-"))
    work = tmp / "proj"
    work.mkdir(parents=True)

    # a) CRLF must survive the edit pipeline untouched
    crlf = work / "crlf.py"
    original = b'import os\r\n\r\ndef f():\r\n    return 1\r\n    x = 2\r\n'
    crlf.write_bytes(original)
    r = py(SCRIPTS / "scan_redundancy.py", crlf, "--out", work / "crlf.json", cwd=work)
    check("CRLF: scan exits 0", r.returncode == 0, r.stderr.strip()[:200], results)
    r = py(SCRIPTS / "apply_prune.py", work / "crlf.json", "--apply",
           "--accept-verified", "--allow-dirty", "--out", work / "crlf-apply.json", cwd=work)
    check("CRLF: apply exits 0", r.returncode == 0, r.stdout.strip()[-250:], results)
    new = crlf.read_bytes()
    check("CRLF: line endings preserved", b"\r\n" in new and b"\n" not in new.replace(b"\r\n", b""), "", results)
    try:
        ast.parse(new.decode("utf-8"))
        ok = True
    except SyntaxError:
        ok = False
    check("CRLF: pruned file parses", ok, "", results)
    res = json.loads((work / "crlf-apply.json").read_text(encoding="utf-8"))
    if res.get("backup_manifest"):
        py(SCRIPTS / "apply_prune.py", "--rollback", res["backup_manifest"], cwd=work)
    check("CRLF: rollback byte-identical", crlf.read_bytes() == original, "", results)

    # b) stale span hash must be refused
    stale = work / "stale.py"
    shutil.copy2(FIXTURE, stale)
    py(SCRIPTS / "scan_redundancy.py", stale, "--out", work / "stale.json", cwd=work)
    stale.write_text("# injected\n" + stale.read_text(encoding="utf-8"), encoding="utf-8")
    r = py(SCRIPTS / "apply_prune.py", work / "stale.json", "--dry-run",
           "--accept-verified", "--out", work / "stale-dry.json", cwd=work)
    dry = json.loads((work / "stale-dry.json").read_text(encoding="utf-8")) \
        if (work / "stale-dry.json").exists() else {}
    check("stale hash refused", r.returncode == 0 and
          any("span_hash mismatch" in x.get("reason", "") for x in dry.get("skipped", [])),
          f"rc={r.returncode}", results)

    # c) Agent-authored finding for a non-python file (same schema)
    js = work / "app.js"
    js.write_text("const a = 1;\nconst unused = 2;\nconsole.log(a);\n", encoding="utf-8")
    import hashlib
    snippet = "const unused = 2;"
    custom = {
        "schema_version": "1.0", "tool": "code-redundancy-pruner", "root": str(work),
        "findings": [{
            "id": "F0001", "rule_id": "JS_UNUSED_CONST", "category": "unused_variable",
            "title": "未使用的常量", "safety": "static", "confidence": 0.9,
            "behavior_change": "none", "file": "app.js", "language": "javascript",
            "span": {"start_line": 2, "end_line": 2}, "snippet": snippet,
            "expected_hash": "sha256:" + hashlib.sha256(snippet.encode()).hexdigest(),
            "reason": "无引用", "evidence": {"unbound_names": ["unused"]},
            "edit": {"action": "delete_lines", "start_line": 2, "end_line": 2, "replacement": ""},
            "dependencies": [], "verification": [], "agent_hint": "Delete line 2 of app.js",
        }],
    }
    (work / "custom.json").write_text(json.dumps(custom, ensure_ascii=False), encoding="utf-8")
    # Use a Python-only command so this check never depends on node being installed.
    (work / "custom-syntax.json").write_text(json.dumps(
        {"language_syntax_commands": {"javascript": "python --version"}}, ensure_ascii=False),
        encoding="utf-8")
    r = py(SCRIPTS / "apply_prune.py", work / "custom.json", "--apply", "--allow-dirty",
           "--config", work / "custom-syntax.json",
           "--out", work / "custom-apply.json", cwd=work)
    check("Agent-authored non-python finding applies",
          r.returncode == 0 and "const unused" not in js.read_text(encoding="utf-8"),
          r.stdout.strip()[-250:], results)
    cres = json.loads((work / "custom-apply.json").read_text(encoding="utf-8")) \
        if (work / "custom-apply.json").exists() else {}
    check("config-driven L1 syntax command applied",
          bool(cres.get("files")) and
          all("L1_syntax_cmd" in f["verification"] for f in cres["files"]), "", results)

    # d) failing post-prune test triggers automatic rollback
    sample = work / "sample.py"
    shutil.copy2(FIXTURE, sample)
    before = sample.read_bytes()
    guard = work / "guard.py"
    guard.write_text(
        "import pathlib, sys\n"
        "sys.exit(0 if 'import os' in pathlib.Path('sample.py').read_text(encoding='utf-8') else 1)\n",
        encoding="utf-8")
    py(SCRIPTS / "scan_redundancy.py", sample, "--out", work / "guard.json", cwd=work)
    r = py(SCRIPTS / "apply_prune.py", work / "guard.json", "--apply", "--accept-verified",
           "--allow-dirty", "--tests", "python guard.py", cwd=work)
    check("failed test blocks pruning (rc=4)", r.returncode == 4, r.stdout.strip()[-250:], results)
    check("failed test auto-rollback byte-identical", sample.read_bytes() == before, "", results)

    shutil.rmtree(tmp, ignore_errors=True)


def phase3(results):
    """From-scratch facilities: config, facade, launchers, clean self-audit."""
    tmp = Path(tempfile.mkdtemp(prefix="rcp-selftest3-"))
    work = tmp / "proj"
    work.mkdir(parents=True)
    rcp = SCRIPTS / "rcp.py"
    target = work / "sample.py"
    shutil.copy2(FIXTURE, target)

    r = py(rcp, "doctor", cwd=work)
    check("doctor HEALTHY", r.returncode == 0 and "HEALTHY" in r.stdout,
          r.stdout.strip()[-200:], results)
    r = py(rcp, "config", cwd=work)
    try:
        cfg = json.loads(r.stdout)
        cfg_ok = all(k in cfg for k in ("scan", "prune", "output", "rule_overrides"))
    except json.JSONDecodeError:
        cfg_ok = False
    check("effective config is valid JSON", r.returncode == 0 and cfg_ok, "", results)
    check("Windows/Unix launchers present",
          (SKILL / "rcp.bat").is_file() and (SKILL / "rcp.sh").is_file(), "", results)
    # Pure text check: never spawn a shell (Windows hosts may route `bash`
    # through WSL, which is blocked in sandboxed environments).
    sh_text = (SKILL / "rcp.sh").read_text(encoding="utf-8")
    check("rcp.sh shebang + exec line",
          sh_text.startswith("#!/usr/bin/env bash") and 'exec "$PY"' in sh_text, "", results)

    r = py(rcp, "init-config", "--dir", work, cwd=work)
    conf = work / "rcp.config.json"
    check("init-config writes project config",
          r.returncode == 0 and conf.is_file(), "", results)
    check("init-config refuses overwrite",
          py(rcp, "init-config", "--dir", work, cwd=work).returncode == 1, "", results)
    try:
        json.loads(conf.read_text(encoding="utf-8"))
        tpl_ok = True
    except (json.JSONDecodeError, OSError):
        tpl_ok = False
    check("generated config is valid JSON", tpl_ok, "", results)

    r = py(rcp, "scan", "sample.py", "--no-md", cwd=work)
    check("facade scan exits 0", r.returncode == 0, r.stdout.strip()[-250:], results)
    rep_path = work / "redundancy-report.json"
    check("facade scan writes report", rep_path.is_file(), "", results)
    r = py(rcp, "prune", "redundancy-report.json", "--dry-run", cwd=work)
    check("facade prune dry-run exits 0", r.returncode == 0, r.stdout.strip()[-250:], results)
    check("facade prune does not modify source",
          target.read_bytes() == FIXTURE.read_bytes(), "", results)

    conf.write_text(json.dumps({"rule_overrides": {"PY_REDUNDANT_PASS": False}}),
                    encoding="utf-8")
    py(SCRIPTS / "scan_redundancy.py", "sample.py", "--out", work / "off.json", cwd=work)
    off = json.loads((work / "off.json").read_text(encoding="utf-8"))
    check("rule_overrides false disables rule",
          "PY_REDUNDANT_PASS" not in off["summary"]["by_rule"], "", results)
    check("disabled rule counted in exclusions",
          any(x.get("exclusions", {}).get("rule_disabled_by_config", 0) >= 2
              for x in off["per_file"]), "", results)

    conf.write_text(json.dumps({"rule_overrides": {"PY_UNUSED_IMPORT": "review"}}),
                    encoding="utf-8")
    py(SCRIPTS / "scan_redundancy.py", "sample.py", "--out", work / "down.json", cwd=work)
    down = json.loads((work / "down.json").read_text(encoding="utf-8"))
    imps = [f for f in down["findings"] if f["rule_id"] == "PY_UNUSED_IMPORT"]
    check("rule_overrides forces safety downgrade",
          bool(imps) and all(f["safety"] == "review" for f in imps), "", results)

    conf.unlink(missing_ok=True)
    r = py(SCRIPTS / "scan_redundancy.py", SCRIPTS, "--out", work / "self.json", cwd=work)
    own = json.loads((work / "self.json").read_text(encoding="utf-8")) \
        if (work / "self.json").exists() else {}
    by_safety = own.get("summary", {}).get("by_safety", {})
    check("self-audit: zero auto-applicable findings",
          r.returncode == 0 and by_safety.get("static", 1) == 0
          and by_safety.get("verified", 1) == 0, str(by_safety), results)
    check("self-audit: no false-positive defs/imports/comments",
          all(f["rule_id"] not in ("PY_UNUSED_DEF", "PY_UNUSED_IMPORT", "PY_COMMENTED_OUT_CODE")
              for f in own.get("findings", [])),
          str(own.get("summary", {}).get("by_rule", {})), results)

    shutil.rmtree(tmp, ignore_errors=True)


def phase4(results):
    """Real-usage regressions (ERR-20261105-002).

    Earlier selftests only used one Python file and replaced the built-in L1
    command with `python --version`, so two production defects stayed hidden.
    These checks exercise multi-file reports, the real product subprocess path
    and the built-in command templates on a simulated Windows host.
    """
    import shlex

    tmp = Path(tempfile.mkdtemp(prefix="rcp-selftest4-"))
    work = tmp / "proj"
    work.mkdir(parents=True)
    shutil.copy2(FIXTURE, work / "a.py")
    (work / "b.py").write_text("import os\n\ndef kept():\n    return 1\n", encoding="utf-8")

    r = py(SCRIPTS / "scan_redundancy.py", ".", "--out", work / "multi.json", cwd=work)
    rep_path = work / "multi.json"
    rep = json.loads(rep_path.read_text(encoding="utf-8")) if rep_path.is_file() else {}
    ids = [f["id"] for f in rep.get("findings", [])]
    check("multi-file scan exits 0", r.returncode == 0, r.stdout.strip()[-200:], results)
    check("multi-file ids are globally unique",
          len(ids) >= 10 and len(ids) == len(set(ids)), f"n={len(ids)}", results)
    check("multi-file ids are contiguous F0001..",
          bool(ids) and ids == [f"F{i:04d}" for i in range(1, len(ids) + 1)], "", results)
    r = py(SCRIPTS / "apply_prune.py", "multi.json", "--dry-run",
           "--out", work / "multi-prune.json", cwd=work)
    check("multi-file report passes prune validation", r.returncode == 0,
          r.stdout.strip()[-250:], results)

    # doctor's NEXT lines must be copy-pasteable from the user's project dir
    d = py(SCRIPTS / "rcp.py", "doctor", cwd=work)
    lines = [ln.strip() for ln in d.stdout.splitlines() if ln.strip().startswith("python ")]
    scan_line = next((ln for ln in lines if " scan ." in ln), "")
    toks = shlex.split(scan_line) if scan_line else []
    entry = (work / toks[1]).resolve() if len(toks) > 1 else None
    check("doctor NEXT path resolves from cwd",
          bool(entry) and entry.is_file(), scan_line, results)
    if entry:
        check("doctor NEXT entry is runnable",
              py(entry, "config", cwd=work).returncode == 0, "", results)

    # non-UTF-8 bytes from a real subprocess must not kill the reader thread
    probe = work / "emit_bad_bytes.py"
    probe.write_text("import sys\nsys.stdout.buffer.write(b'\\xd2\\x00\\xff\\n')\n",
                     encoding="utf-8")
    driver = work / "probe_encoding.py"
    driver.write_text(
        "import sys\n"
        "sys.dont_write_bytecode = True\n"
        f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "import apply_prune as a\n"
        f"probe = {str(probe).replace(chr(92), '/')!r}\n"
        f"work = {str(work).replace(chr(92), '/')!r}\n"
        "cmd = '\"' + sys.executable + '\" \"' + probe + '\"'\n"
        "rc, out, err = a.run_cmd(cmd, work, 30)\n"
        "print('ENCODING_OK' if rc == 0 and 'Traceback' not in err else 'ENCODING_BAD')\n",
        encoding="utf-8")
    e = py(driver, cwd=work)
    check("run_cmd survives non-UTF-8 subprocess output",
          e.returncode == 0 and "ENCODING_OK" in e.stdout,
          (e.stdout + e.stderr).strip()[-250:], results)

    # built-in L1 commands must degrade to a warning on Windows, never hard-fail
    code = (
        "import sys, os\n"
        "sys.dont_write_bytecode = True\n"
        f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "import rcp_common as c\n"
        "cfg = c.load_config()\n"
        "real = os.name\n"
        "os.name = 'nt'\n"
        "win_shell = c.syntax_command(cfg, 'shell')\n"
        "fake = dict(cfg)\n"
        "fake['language_syntax_commands'] = dict(cfg['language_syntax_commands'])\n"
        "fake['language_syntax_commands']['shell'] = 'python --version > /dev/null'\n"
        "win_redirect = c.syntax_command(fake, 'shell')\n"
        "os.name = 'posix'\n"
        "posix_shell = c.syntax_command(cfg, 'shell')\n"
        "os.name = real\n"
        "print('WIN_SHELL=%r WIN_REDIRECT=%r POSIX_SHELL=%r' % (win_shell, win_redirect, posix_shell))\n"
    )
    g = py("-c", code, cwd=work)
    check("L1 guard skips bash/WSL and unix redirects on Windows",
          g.returncode == 0 and "WIN_SHELL=None" in g.stdout
          and "WIN_REDIRECT=None" in g.stdout,
          (g.stdout + g.stderr).strip()[-250:], results)
    check("L1 guard does not disable posix commands",
          g.returncode == 0 and "POSIX_SHELL=None" not in g.stdout,
          g.stdout.strip()[-150:], results)

    defaults = json.loads((SKILL / "config" / "defaults.json").read_text(encoding="utf-8"))
    check("builtin go syntax command has no unix redirect",
          "/dev/null" not in (defaults["language_syntax_commands"].get("go") or ""), "", results)
    docs = "".join((SKILL / "references" / n).read_text(encoding="utf-8") for n in
                   ("quickstart.md", "redundancy-catalog.md", "lossless-guarantee.md"))
    check("docs carry no stale 'must provide --syntax-cmd' / fixed selftest count",
          "必须提供 `--syntax-cmd`" not in docs and "31 项检查" not in docs, "", results)

    # Guard the advertised Python 3.9+ floor. Path.write_text(newline=...) was
    # added in 3.10 and once broke the whole L1 path on 3.9 (ERR-20261105-003).
    compat_bad = []
    for p in sorted(SCRIPTS.glob("*.py")):
        src = p.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src, filename=str(p), feature_version=(3, 9))
        except SyntaxError as exc:
            compat_bad.append(f"{p.name}: 3.9 syntax: {exc.msg}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr in ("write_text", "read_text") \
                    and any(k.arg == "newline" for k in node.keywords):
                compat_bad.append(f"{p.name}:{node.lineno}: newline= is Python 3.10+")
    check("Python 3.9 syntax/API compatibility (static)",
          not compat_bad, "; ".join(compat_bad)[:200], results)

    shutil.rmtree(tmp, ignore_errors=True)


def report(results):
    print("\n=== code-redundancy-pruner selftest ===")
    print("| # | 检查项 | 结果 | 备注 |")
    print("|---|---|---|---|")
    for i, (name, status, detail) in enumerate(results, 1):
        clean = detail.replace("|", "/").replace("\r", " ").replace("\n", " ")
        print(f"| {i} | {name} | {status} | {clean} |")
    failed = [x for x in results if x[1] == "FAIL"]
    print(f"\nTOTAL {len(results)}  PASS {len(results) - len(failed)}  FAIL {len(failed)}")
    if failed:
        print("FAILED: " + ", ".join(x[0] for x in failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

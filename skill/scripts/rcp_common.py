#!/usr/bin/env python3
"""Shared constants, helpers and config loading for code-redundancy-pruner.

Single source of truth for constants that would otherwise be duplicated across
scan_redundancy.py / render_report.py / apply_prune.py / rcp.py. Stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

TOOL = "code-redundancy-pruner"
SCHEMA_VERSION = "1.0"
CONFIG_VERSION = "1.0"

STATIC = "static"
VERIFIED = "verified"
REVIEW = "review"

SAFETY_TIERS = {
    STATIC: "静态证明无损；apply_prune.py 可直接应用",
    VERIFIED: "静态近似无损；需 --tests 通过或 --accept-verified 才应用",
    REVIEW: "仅报告，永不自动应用；需人工确认后显式提升 safety",
}
TIER_LABEL = {
    STATIC: "STATIC / 静态证明无损",
    VERIFIED: "VERIFIED / 需行为验证",
    REVIEW: "REVIEW / 仅报告",
}
ALLOWED_SAFETY = frozenset(SAFETY_TIERS)
ALLOWED_ACTION = frozenset({"delete_lines", "replace_lines"})
REQUIRED_FINDING_KEYS = (
    "id", "rule_id", "safety", "file", "span", "snippet", "expected_hash", "edit",
)

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = SKILL_DIR / "scripts"
DEFAULT_CONFIG_PATH = SKILL_DIR / "config" / "defaults.json"
CONFIG_FILENAME = "rcp.config.json"
REQUIRED_SCRIPTS = (
    "rcp.py", "rcp_common.py", "scan_redundancy.py", "render_report.py",
    "apply_prune.py", "selftest.py",
)

# Public API. Also tells PY_UNUSED_DEF that these are cross-module exports,
# so dogfooding this skill on its own source produces no false positives.
__all__ = [
    "TOOL", "SCHEMA_VERSION", "CONFIG_VERSION",
    "STATIC", "VERIFIED", "REVIEW", "SAFETY_TIERS", "TIER_LABEL",
    "ALLOWED_SAFETY", "ALLOWED_ACTION", "REQUIRED_FINDING_KEYS",
    "SKILL_DIR", "SCRIPTS_DIR", "DEFAULT_CONFIG_PATH", "CONFIG_FILENAME",
    "REQUIRED_SCRIPTS",
    "h", "sha", "utcnow", "read_raw", "bodies_of", "eol_of", "ident_tokens",
    "deep_merge", "find_project_config", "load_config", "validate_config",
    "rule_setting", "syntax_command", "language_for", "public_config", "assign_ids",
]


def h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha(text: str) -> str:
    return "sha256:" + h(text)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_raw(path) -> str:
    # newline="" preserves CRLF so the hash/edit pipeline stays byte-lossless
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def bodies_of(lines):
    return [ln.rstrip("\r\n") for ln in lines]


def eol_of(line: str) -> str:
    return line[len(line.rstrip("\r\n")):]


def ident_tokens(text: str):
    return set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text))


def deep_merge(base, over):
    """Recursively merge dicts; scalars/lists from `over` replace `base`."""
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def find_project_config(cwd=None, max_levels=8):
    """Walk up from cwd looking for rcp.config.json."""
    cur = Path(cwd or os.getcwd()).resolve()
    for _ in range(max_levels):
        cand = cur / CONFIG_FILENAME
        if cand.is_file():
            return cand
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    return None


def load_config(explicit=None, cwd=None):
    """Builtin defaults <- project rcp.config.json <- explicit --config."""
    if not DEFAULT_CONFIG_PATH.is_file():
        raise FileNotFoundError(f"builtin config missing: {DEFAULT_CONFIG_PATH}")
    cfg = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    sources = {"defaults": str(DEFAULT_CONFIG_PATH).replace(os.sep, "/")}
    found = find_project_config(cwd)
    if found:
        cfg = deep_merge(cfg, json.loads(found.read_text(encoding="utf-8")))
        sources["project"] = str(found).replace(os.sep, "/")
    if explicit:
        p = Path(explicit).resolve()
        if not p.is_file():
            raise FileNotFoundError(f"--config not found: {p}")
        cfg = deep_merge(cfg, json.loads(p.read_text(encoding="utf-8")))
        sources["explicit"] = str(p).replace(os.sep, "/")
    cfg["_meta"] = {"loaded_from": sources}
    return cfg


def validate_config(cfg):
    errors = []
    for key in ("config_version", "python_min_version", "scan", "rule_overrides",
                "language_syntax_commands", "extension_language", "prune", "output"):
        if key not in cfg:
            errors.append(f"missing top-level key: {key}")
    scan = cfg.get("scan", {})
    for key in ("include_globs", "exclude_globs", "exclude_dirs", "source_extensions",
                "suppress_markers", "max_bytes", "max_files"):
        if key not in scan:
            errors.append(f"missing scan.{key}")
    for rule, val in (cfg.get("rule_overrides") or {}).items():
        if rule.startswith("_"):
            continue
        if val is not False and val not in ALLOWED_SAFETY:
            errors.append(f"rule_overrides.{rule}: must be false or one of {sorted(ALLOWED_SAFETY)}")
    return errors


def rule_setting(cfg, rule_id):
    """Return (enabled, safety_override_or_None)."""
    val = (cfg.get("rule_overrides") or {}).get(rule_id)
    if val is False:
        return False, None
    if val in ALLOWED_SAFETY:
        return True, val
    return True, None


# Unix-only shell syntax that cmd.exe cannot parse. A configured command that
# uses it is silently skipped on Windows instead of failing the whole L1 gate.
UNIX_ONLY_TOKENS = ("/dev/null", "/tmp/", "> /dev/null", "2>/dev/null")
# On Windows `bash`/`sh` frequently resolves to a WSL alias whose launch is
# blocked, and its output is often non-UTF-8. Never use it for L1 there.
WIN_UNSAFE_EXES = frozenset({"bash", "sh", "wsl"})


def syntax_command(cfg, language=None, extension=None):
    """Return a usable L1 command template, or None to skip L1 with a warning.

    Zero-setup guarantee: a built-in command that cannot run on this machine
    (missing tool, Unix-only path/redirect, WSL bash alias) must degrade to a
    warning, never to `verification failed`.
    """
    lang = language or (cfg.get("extension_language") or {}).get(extension)
    if not lang:
        return None
    tpl = (cfg.get("language_syntax_commands") or {}).get(lang)
    if not isinstance(tpl, str) or not tpl.strip():
        return None
    exe = tpl.strip().split()[0].strip("\"'")
    if os.name == "nt" and (any(t in tpl for t in UNIX_ONLY_TOKENS)
                            or exe in WIN_UNSAFE_EXES):
        return None
    if not shutil.which(exe):
        return None
    return tpl


def language_for(rel, cfg):
    ext = Path(rel).suffix.lower()
    return (cfg.get("extension_language") or {}).get(ext)


def public_config(cfg):
    return {k: v for k, v in cfg.items() if not k.startswith("_")}


def assign_ids(findings):
    """Deterministically sort findings and assign globally unique F0001.. ids.

    Must run once on the *whole* report, not per file: per-file numbering makes
    ids collide as soon as a project has two Python files, and apply_prune.py
    rejects a report with duplicate ids.
    """
    ordered = sorted(findings, key=lambda f: (
        f.get("file", ""), f.get("span", {}).get("start_line", 0),
        f.get("rule_id", ""), f.get("title", "")))
    for i, f in enumerate(ordered, 1):
        f["id"] = f"F{i:04d}"
    return ordered

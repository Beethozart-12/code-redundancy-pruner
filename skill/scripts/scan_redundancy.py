#!/usr/bin/env python3
"""Deterministic redundancy scanner (Python AST, stdlib only).

Produces the canonical machine-readable report consumed by render_report.py
and apply_prune.py. Never modifies source files.

Usage:
  python scan_redundancy.py <path> [--out report.json] [--md report.md]
                            [--include '*.py'] [--exclude '*_test.py']
                            [--max-bytes 1000000] [--print-json]
"""
from __future__ import annotations

import argparse
import ast
import fnmatch
import io
import json
import os
import re
import sys
import tokenize
from pathlib import Path

sys.dont_write_bytecode = True  # keep the installed skill dir free of __pycache__
sys.path.insert(0, str(Path(__file__).resolve().parent))
from rcp_common import (  # noqa: E402
    TOOL, SCHEMA_VERSION, STATIC, VERIFIED, REVIEW, SAFETY_TIERS,
    sha, utcnow, read_raw, bodies_of, load_config, rule_setting, assign_ids,
)

COMMENT_DIRECTIVES = (
    "TODO", "FIXME", "NOTE", "HACK", "XXX", "WARN", "WARNING", "SEE", "DOC",
    "RCP:", "NOQA", "TYPE:", "PRAGMA", "PYLINT", "FLAKE8", "MYPY", "ESLINT",
    "TS-IGNORE", "REGION", "ENDREGION", "NOSONAR",
)
ENTRYPOINT_NAMES = {
    "main", "run", "app", "cli", "handler", "lambda_handler", "setup", "teardown",
    "setUp", "tearDown", "setUpClass", "tearDownClass", "fixture", "test_main",
    "create_app", "get_app", "application", "wsgi_app", "asgi_app", "cli_main",
}
# ':' deliberately omitted: it turns section headers like "# L1: syntax" into
# a valid (but meaningless) AnnAssign and causes false positives.
CODE_CHARS = ("=", "(", ")", "[", "]", "{", "}", ",", "return ", "import ",
              "def ", "class ", "if ", "for ", "while ", "print")
TERMINATORS = (ast.Return, ast.Raise, ast.Break, ast.Continue)


class FileAnalyzer:
    def __init__(self, path: Path, root: Path, cfg=None):
        self.path = path
        self.root = root
        self.cfg = cfg if cfg is not None else load_config()
        self.rel = os.path.relpath(str(path), str(root)).replace(os.sep, "/")
        self.raw = read_raw(path)
        self.lines = self.raw.splitlines(keepends=True)
        self.bodies = bodies_of(self.lines)
        self.tree = ast.parse(self.raw, filename=self.rel)
        self.parent = {}
        for n in ast.walk(self.tree):
            for c in ast.iter_child_nodes(n):
                self.parent[c] = n
        self.loads = set()
        self.stores = set()
        self.any_names = set()
        for n in ast.walk(self.tree):
            if isinstance(n, ast.Name):
                self.any_names.add(n.id)
                if isinstance(n.ctx, ast.Load):
                    self.loads.add(n.id)
                elif isinstance(n.ctx, (ast.Store, ast.Del)):
                    self.stores.add(n.id)
        self.all_exports = set()
        for n in ast.walk(self.tree):
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Name) and t.id == "__all__":
                        try:
                            v = ast.literal_eval(n.value)
                        except Exception:
                            v = None
                        if isinstance(v, (list, tuple, set)):
                            self.all_exports.update(str(x) for x in v)
        # `from __future__ import annotations` does NOT hide names from the AST:
        # annotation expressions are still ast.Name nodes. Only get_type_hints()
        # calls or literal string annotations can require a runtime import.
        self.dynamic_annotations = self._has_dynamic_annotation_resolution()
        self.dynamic_eval = any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id in ("eval", "exec", "compile", "globals", "locals", "__import__")
            for n in ast.walk(self.tree)
        )
        self.findings = []
        self.exclusions = {
            "future_imports": 0, "star_imports": 0, "suppressed_by_comment": 0,
            "init_reexports": 0, "dynamic_name_resolution": 0,
            "optional_import_guard": 0, "inline_comment_on_import": 0,
            "overlap_dropped": 0, "rule_disabled_by_config": 0,
        }

    # ---------- structural helpers ----------

    def _has_dynamic_annotation_resolution(self):
        for n in ast.walk(self.tree):
            if isinstance(n, ast.Call) and (
                (isinstance(n.func, ast.Name) and n.func.id == "get_type_hints")
                or (isinstance(n.func, ast.Attribute) and n.func.attr == "get_type_hints")
            ):
                return True
        for n in ast.walk(self.tree):
            anns = []
            if isinstance(n, ast.AnnAssign):
                anns.append(n.annotation)
            elif isinstance(n, ast.arg):
                anns.append(n.annotation)
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                anns.append(n.returns)
            for a in anns:
                if a is None:
                    continue
                for sub in ast.walk(a):
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                        return True
        return False

    def enclosing_block(self, node):
        cur, p = node, self.parent.get(node)
        while p is not None:
            for field in ("body", "orelse", "finalbody", "handlers"):
                lst = getattr(p, field, None)
                if isinstance(lst, list) and any(x is cur for x in lst):
                    return lst
            cur, p = p, self.parent.get(p)
        return None

    def nearest_scope(self, node):
        cur = self.parent.get(node)
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
                return cur
            cur = self.parent.get(cur)
        return None

    def has_keep(self, lineno):
        if not (0 < lineno <= len(self.bodies)):
            return False
        line = self.bodies[lineno - 1].lower()
        markers = self.cfg.get("scan", {}).get("suppress_markers") or []
        return any(str(m).lower() in line for m in markers)

    def span_text(self, start, end):
        return "\n".join(self.bodies[start - 1:end])

    def in_optional_import_guard(self, node):
        cur = self.parent.get(node)
        while cur is not None:
            if isinstance(cur, ast.Try):
                for h in cur.handlers:
                    if h.type is None:
                        return True
                    try:
                        txt = ast.unparse(h.type)
                    except Exception:
                        return True
                    if "ImportError" in txt or "ModuleNotFoundError" in txt:
                        return True
            cur = self.parent.get(cur)
        return False

    def string_lines(self):
        covered = set()
        try:
            for tok in tokenize.generate_tokens(io.StringIO(self.raw).readline):
                if tok.type == tokenize.STRING:
                    for ln in range(tok.start[0], tok.end[0] + 1):
                        covered.add(ln)
        except (tokenize.TokenError, IndentationError, SyntaxError):
            pass
        return covered

    # ---------- finding factory ----------

    def mk(self, rule, title, tier, confidence, start, end, reason, *,
           evidence=None, action="delete_lines", replacement="", category=None,
           unbound_names=None, why_blocked=None, suggestion=None):
        enabled, override = rule_setting(self.cfg, rule)
        if not enabled:
            self.exclusions["rule_disabled_by_config"] += 1
            return
        if override:
            if override == REVIEW and tier != REVIEW:
                why_blocked = why_blocked or "配置 rule_overrides 强制降为 review"
                suggestion = suggestion or "在 rcp.config.json 中调整该规则后重扫。"
            tier = override
            reason += f" [safety 由配置覆盖为 {override}]"
        s = max(1, int(start))
        e = min(len(self.bodies), int(end))
        e = max(s, e)
        text = self.span_text(s, e)
        ev = dict(evidence or {})
        if unbound_names:
            ev["unbound_names"] = sorted(set(unbound_names))
        action_check = "deletion_only" if action == "delete_lines" else "token_subset"
        f = {
            "id": "F0000",
            "rule_id": rule,
            "category": category or rule.lower().replace("py_", ""),
            "title": title,
            "safety": tier,
            "confidence": round(float(confidence), 2),
            "behavior_change": "none" if tier != REVIEW else "unknown",
            "file": self.rel,
            "language": "python",
            "span": {"start_line": s, "end_line": e},
            "snippet": text,
            "expected_hash": sha(text),
            "reason": reason,
            "evidence": ev,
            "edit": {
                "action": action,
                "start_line": s,
                "end_line": e,
                "replacement": replacement,
            },
            "dependencies": [],
            "verification": (
                [] if tier == REVIEW else
                ["span_hash", "syntax_parse", action_check, "name_resolution"]
                + (["tests"] if tier == VERIFIED else [])
            ),
        }
        if tier == REVIEW:
            f["why_blocked"] = why_blocked or "静态证据不足，无法确定删除不改变可观测行为"
            f["suggestion"] = suggestion or "确认无外部/动态引用后，把 safety 改为 static 并在 justification 中写明依据，再交由 apply_prune.py 处理。"
            f["agent_hint"] = (
                f"DO NOT APPLY. Report only. {rule} at {self.rel}:{s}-{e}. "
                f"Blocked: {f['why_blocked']}"
            )
        else:
            verb = "Delete" if action == "delete_lines" else "Replace"
            f["agent_hint"] = (
                f"{verb} lines {s}-{e} of {self.rel} exactly as described in edit; "
                "do not rewrite any other line; run apply_prune.py to verify and apply."
            )
        self.findings.append(f)

    # ---------- STATIC rules (provably lossless) ----------

    def rule_noop_literal(self):
        for n in ast.walk(self.tree):
            if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)):
                continue
            p = self.parent.get(n)
            if isinstance(p, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                    and p.body and p.body[0] is n:
                continue  # docstring
            blk = self.enclosing_block(n)
            if blk is None or len(blk) <= 1:
                continue  # would leave an empty suite
            if self.has_keep(n.lineno) or "#" in self.bodies[n.lineno - 1]:
                continue
            self.mk(
                "PY_NOOP_LITERAL_STMT", "无副作用的字面量表达式语句", STATIC, 0.99,
                n.lineno, n.end_lineno or n.lineno,
                "该 Expr 为 ast.Constant 且不是所在作用域首语句（非 docstring），求值时无任何可观测副作用；删除后剩余语句仍构成合法 suite。",
                evidence={"node": "ast.Expr(ast.Constant)", "non_docstring": True,
                          "siblings": len(blk)},
                category="noop_statement",
            )

    def rule_redundant_pass(self):
        for n in ast.walk(self.tree):
            if not isinstance(n, ast.Pass):
                continue
            blk = self.enclosing_block(n)
            if blk is None or len(blk) <= 1:
                continue  # sole statement -> removal would create a syntax error
            line = self.bodies[n.lineno - 1]
            if self.has_keep(n.lineno) or "#" in line:
                continue  # preserve attached information
            self.mk(
                "PY_REDUNDANT_PASS", "非空语句块中的冗余 pass", STATIC, 0.99,
                n.lineno, n.end_lineno or n.lineno,
                "pass 是空操作；所在语句块尚有其它语句，删除后语法与执行路径均不变。",
                evidence={"node": "ast.Pass", "siblings": len(blk)},
                category="noop_statement",
            )

    def rule_unreachable(self):
        candidates = []
        for n in ast.walk(self.tree):
            blk = getattr(n, "body", None)
            if not isinstance(blk, list) or len(blk) < 2:
                continue
            for i, st in enumerate(blk[:-1]):
                if not isinstance(st, TERMINATORS):
                    continue
                dead = blk[i + 1:]
                if any(isinstance(d, (ast.Global, ast.Nonlocal)) for d in dead):
                    continue  # compile-time directive: never touch
                s = dead[0].lineno
                e = max((d.end_lineno or d.lineno) for d in dead)
                term_end = st.end_lineno or st.lineno
                first = s
                while first > term_end + 1:
                    prev = self.bodies[first - 2].strip()
                    if prev == "" or prev.startswith("#"):
                        first -= 1
                    else:
                        break
                s = min(s, first)
                candidates.append((s, e, type(st).__name__, len(dead), [d.__class__.__name__ for d in dead]))
                break
        # keep widest regions; drop nested/subsumed ones
        kept = []
        for c in sorted(candidates, key=lambda x: (x[0], -(x[1] - x[0]))):
            if any(o[0] <= c[0] and c[1] <= o[1] and o != c for o in candidates):
                self.exclusions["overlap_dropped"] += 1
                continue
            kept.append(c)
        for s, e, term, count, kinds in kept:
            self.mk(
                "PY_UNREACHABLE_AFTER_TERMINATOR", f"{term} 之后的不可达语句", STATIC, 0.98,
                s, e,
                f"同一语句块内 {term} 无条件终止控制流，其后 {count} 条语句永不可达；删除不改变任何执行路径。相邻注释/空行一并归入删除区间。",
                evidence={"terminator": term, "dead_statement_count": count,
                          "dead_node_types": kinds, "global_nonlocal_present": False},
                category="dead_code",
            )

    def rule_self_assign(self):
        for n in ast.walk(self.tree):
            if not (isinstance(n, ast.Assign) and len(n.targets) == 1
                    and isinstance(n.targets[0], ast.Name)
                    and isinstance(n.value, ast.Name)
                    and isinstance(n.value.ctx, ast.Load)
                    and n.targets[0].id == n.value.id):
                continue
            scope = self.nearest_scope(n)
            if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue  # class/module bodies have dict-observable effects
            name = n.targets[0].id
            if name.startswith("_") or self.has_keep(n.lineno):
                continue
            if "#" in self.bodies[n.lineno - 1]:
                continue
            self.mk(
                "PY_SELF_ASSIGN", f"自赋值 {name} = {name}", STATIC, 0.97,
                n.lineno, n.end_lineno or n.lineno,
                "函数局部简单名称对自身的赋值不改变绑定值，且右侧为纯 Name Load；删除为语义恒等变换。",
                evidence={"name": name, "scope": "function"},
                unbound_names=[name], category="noop_statement",
            )

    def rule_except_binding(self):
        for n in ast.walk(self.tree):
            if not (isinstance(n, ast.ExceptHandler) and n.name):
                continue
            if n.name in self.any_names:
                continue  # conservative: referenced anywhere in the file
            lineno = n.lineno
            if not (0 < lineno <= len(self.bodies)) or self.has_keep(lineno):
                continue
            line = self.bodies[lineno - 1]
            pat = re.compile(r"\bas\s+" + re.escape(n.name) + r"\b")
            if not pat.search(line):
                continue  # multi-line header -> leave to review
            new_line = pat.sub("", line, count=1)
            new_line = re.sub(r"\s+:$", ":", new_line)
            if not new_line.rstrip().endswith(":"):
                continue
            self.mk(
                "PY_UNUSED_EXCEPT_BINDING", f"未使用的异常绑定 as {n.name}", STATIC, 0.96,
                lineno, lineno,
                "except 子句的 as 名称在文件内无任何引用；Python 在 handler 结束时本就会删除该绑定，移除它不改变作用域或异常传播。",
                evidence={"name": n.name, "reconstructed_line": new_line},
                action="replace_lines", replacement=new_line,
                unbound_names=[n.name], category="unused_binding",
            )

    # ---------- VERIFIED rules (need tests or explicit ack) ----------

    def rule_unused_import(self):
        for n in ast.walk(self.tree):
            if not isinstance(n, (ast.Import, ast.ImportFrom)):
                continue
            if isinstance(n, ast.ImportFrom) and n.module == "__future__":
                self.exclusions["future_imports"] += 1
                continue
            if isinstance(n, ast.ImportFrom) and any(a.name == "*" for a in n.names):
                self.exclusions["star_imports"] += 1
                continue
            if self.has_keep(n.lineno):
                self.exclusions["suppressed_by_comment"] += 1
                continue
            aliases = []
            for a in n.names:
                bound = a.asname or (a.name.split(".")[0] if isinstance(n, ast.Import) else a.name)
                aliases.append((a, bound))
            unused = [(a, b) for a, b in aliases
                      if b not in self.loads and b not in self.all_exports and b != "_"]
            if not unused:
                continue
            unused_ids = {id(a) for a, _ in unused}
            unbound = sorted({b for _, b in unused})
            start, end = n.lineno, n.end_lineno or n.lineno
            has_inline_comment = any("#" in ln for ln in self.bodies[start - 1:end])

            tier, blocked = VERIFIED, None
            if self.dynamic_annotations:
                tier, blocked = REVIEW, "存在 get_type_hints 调用或字符串字面量注解，导入可能被运行时动态解析"
            elif self.dynamic_eval:
                tier, blocked = REVIEW, "文件使用 eval/exec/globals/__import__，名称可能被动态解析"
            elif os.path.basename(str(self.path)) == "__init__.py":
                tier, blocked = REVIEW, "__init__.py 中的导入通常是包级再导出"
                self.exclusions["init_reexports"] += 1
            elif self.in_optional_import_guard(n):
                tier, blocked = REVIEW, "位于 try/except ImportError 可选依赖守卫内"
                self.exclusions["optional_import_guard"] += 1
            elif has_inline_comment:
                tier, blocked = REVIEW, "导入行含行内注释，删除会丢失信息（信息无损约束）"
                self.exclusions["inline_comment_on_import"] += 1

            if tier == REVIEW:
                self.mk(
                    "PY_UNUSED_IMPORT", f"未使用的导入 {', '.join(unbound)}", REVIEW, 0.90,
                    start, end,
                    f"绑定名 {unbound} 在模块内无 Load 引用，也不在 __all__ 中；但存在动态引用或信息丢失风险。",
                    evidence={"bindings": unbound, "reference_count": 0,
                              "module": getattr(n, "module", None),
                              "statement": type(n).__name__},
                    category="unused_import", why_blocked=blocked,
                    suggestion="确认无动态引用与再导出需求、且行内注释无需保留后，把 safety 改为 verified 或 static。",
                )
                continue

            if len(unused) == len(aliases):
                action, replacement = "delete_lines", ""
            else:
                kept = [a for a, _ in aliases if id(a) not in unused_ids]
                parts = [a.name + (f" as {a.asname}" if a.asname else "") for a in kept]
                indent = re.match(r"\s*", self.bodies[start - 1]).group(0)
                if isinstance(n, ast.Import):
                    rebuilt = "import " + ", ".join(parts)
                else:
                    mod = "." * (n.level or 0) + (n.module or "")
                    rebuilt = f"from {mod} import " + ", ".join(parts)
                action, replacement = "replace_lines", indent + rebuilt

            self.mk(
                "PY_UNUSED_IMPORT", f"未使用的导入 {', '.join(unbound)}", VERIFIED, 0.93,
                start, end,
                f"绑定名 {unbound} 在模块内无 Load 引用，也不在 __all__ 中。删除/收窄导入不改变任何执行路径；唯一残余风险是模块导入时的副作用，故要求行为验证。",
                evidence={"bindings": unbound, "reference_count": 0,
                          "statement": type(n).__name__,
                          "module": getattr(n, "module", None),
                          "rebuilt": replacement or None},
                action=action, replacement=replacement,
                unbound_names=unbound, category="unused_import",
                suggestion="提供 --tests \"<测试命令>\" 或显式 --accept-verified 后才会被自动精简。",
            )

    # ---------- REVIEW rules (report only) ----------

    def rule_unused_local(self):
        for n in ast.walk(self.tree):
            if not (isinstance(n, ast.Assign) and len(n.targets) == 1
                    and isinstance(n.targets[0], ast.Name)):
                continue
            scope = self.nearest_scope(n)
            if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            name = n.targets[0].id
            if name.startswith("_") or name in self.loads or name in self.all_exports:
                continue
            if self.dynamic_eval or self.has_keep(n.lineno):
                continue
            if any(isinstance(d, (ast.Global, ast.Nonlocal)) and name in d.names
                   for d in ast.walk(scope)):
                continue
            self.mk(
                "PY_UNUSED_LOCAL", f"未被引用的局部变量 {name}", REVIEW, 0.75,
                n.lineno, n.end_lineno or n.lineno,
                f"赋值目标 {name} 在文件内无 Load 引用，但其右值可能带有副作用或用于调试/延长对象生命周期。",
                evidence={"name": name, "rhs": type(n.value).__name__},
                category="unused_variable",
                why_blocked="右值可能含副作用（I/O、调用、属性访问），静态证明不足",
                suggestion="确认右值为纯表达式（常量/字面量容器）后，可把 safety 改为 static。",
            )

    def rule_unused_def(self):
        if self.dynamic_eval or os.path.basename(str(self.path)) == "__init__.py":
            self.exclusions["dynamic_name_resolution"] += 1
            return
        for n in self.tree.body:
            if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if n.name.startswith("__") or n.name in ENTRYPOINT_NAMES or n.decorator_list:
                continue
            if n.name in self.loads or n.name in self.all_exports:
                continue
            self.mk(
                "PY_UNUSED_DEF", f"未被模块内引用的顶层定义 {n.name}", REVIEW, 0.60,
                n.lineno, n.end_lineno or n.lineno,
                f"顶层 {type(n).__name__} {n.name} 在模块内无引用，但可能是公共 API、反射目标、测试入口或框架回调。",
                evidence={"name": n.name, "node": type(n).__name__},
                category="dead_definition",
                why_blocked="跨模块调用、反射、插件注册无法由单文件静态分析排除",
                suggestion="全仓搜索名称并确认无外部引用后，再人工删除（本 skill 不会自动删定义）。",
            )

    def rule_constant_false_branch(self):
        for n in ast.walk(self.tree):
            if not isinstance(n, ast.If):
                continue
            t = n.test
            if isinstance(t, ast.Constant) and (t.value is False or t.value == 0
                                                or t.value is None or t.value == ""):
                self.mk(
                    "PY_CONSTANT_FALSE_BRANCH", "常量为假的分支", REVIEW, 0.85,
                    n.lineno, n.end_lineno or n.lineno,
                    "条件为编译期常量假值，分支体不可达；但常被用作特性开关或调试开关。",
                    evidence={"constant": repr(t.value), "has_orelse": bool(n.orelse)},
                    category="dead_branch",
                    why_blocked="可能是有意保留的特性开关/调试开关，删除会丢失作者意图",
                    suggestion="确认非开关代码后手工删除，或改用配置常量。",
                )

    def rule_redundant_else(self):
        for n in ast.walk(self.tree):
            if not isinstance(n, ast.If) or not n.orelse or not n.body:
                continue
            if not isinstance(n.body[-1], (ast.Return, ast.Raise, ast.Break, ast.Continue)):
                continue
            self.mk(
                "PY_REDUNDANT_ELSE_AFTER_RETURN", "return/raise 后冗余 else", REVIEW, 0.88,
                n.lineno, n.end_lineno or n.lineno,
                "if 分支无条件终止，else 可以去嵌套；属于等价重构而非纯删除，会改变 AST 结构。",
                evidence={"terminator": type(n.body[-1]).__name__},
                category="redundant_nesting",
                why_blocked="去嵌套是结构性改写，不属于无损删除；会改变 AST 与行号",
                suggestion="如需去嵌套，手工重构并跑测试；本 skill 只报告不改写。",
            )

    def rule_duplicate_block(self):
        for n in ast.walk(self.tree):
            blk = getattr(n, "body", None)
            if not isinstance(blk, list) or len(blk) < 6:
                continue
            sig = [ast.dump(s, include_attributes=False) for s in blk]
            seen = {}
            for i in range(len(blk) - 2):
                key = "\x00".join(sig[i:i + 3])
                if key in seen and i - seen[key] >= 3:
                    self.mk(
                        "PY_DUPLICATE_BLOCK", "重复的连续语句序列", REVIEW, 0.70,
                        blk[i].lineno, blk[i + 2].end_lineno or blk[i + 2].lineno,
                        "存在 ≥3 条 AST 完全相同的连续语句序列；抽取公共函数可减少重复，但会改写控制流。",
                        evidence={"first_occurrence_line": blk[seen[key]].lineno,
                                  "sequence_length": 3},
                        category="duplication",
                        why_blocked="消除重复需要新增函数/参数，属于重构而非无损删除",
                        suggestion="评估后手工抽取函数；不要自动合并。",
                    )
                    break
                seen.setdefault(key, i)

    def rule_commented_code(self):
        str_lines = self.string_lines()
        groups, cur = [], []

        def flush():
            if cur:
                groups.append(list(cur))
                cur.clear()

        for i, line in enumerate(self.bodies, 1):
            if i in str_lines:
                flush()
                continue
            m = re.match(r"^\s*#\s?(.*)$", line)
            if not m:
                flush()
                continue
            body = m.group(1).strip()
            if not body:
                flush()
                continue
            upper = body.upper()
            if body.startswith("#!") or "coding:" in body or "coding=" in body:
                flush()
                continue
            if any(upper.startswith(p) for p in COMMENT_DIRECTIVES) or "://" in body:
                flush()
                continue
            if not any(ch in body for ch in CODE_CHARS):
                flush()
                continue
            try:
                t = ast.parse(body)
            except (SyntaxError, ValueError):
                flush()
                continue
            if not t.body:
                flush()
                continue
            if len(t.body) == 1 and isinstance(t.body[0], ast.Expr) \
                    and isinstance(t.body[0].value, (ast.Name, ast.Constant)):
                flush()
                continue
            # "# L1: syntax" parses as a value-less AnnAssign, not real code.
            if len(t.body) == 1 and isinstance(t.body[0], ast.AnnAssign) \
                    and t.body[0].value is None:
                flush()
                continue
            cur.append(i)
        flush()
        for g in groups:
            self.mk(
                "PY_COMMENTED_OUT_CODE", "注释掉的代码块", REVIEW, 0.65,
                g[0], g[-1],
                "连续注释行去注释后可被解析为合法 Python 语句，属于残留代码。删除不影响运行时，但会丢失作者留下的信息。",
                evidence={"comment_lines": len(g), "parses_as_python": True},
                category="dead_comment",
                why_blocked="信息无损约束：注释可能承载待办/说明/历史决策，默认不删",
                suggestion="确认仅为废弃代码后，可用 --include-comments 视角人工删除。",
            )

    # ---------- orchestration ----------

    def run(self):
        self.rule_noop_literal()
        self.rule_redundant_pass()
        self.rule_unreachable()
        self.rule_self_assign()
        self.rule_except_binding()
        self.rule_unused_import()
        self.rule_unused_local()
        self.rule_unused_def()
        self.rule_constant_false_branch()
        self.rule_redundant_else()
        self.rule_duplicate_block()
        self.rule_commented_code()
        self._dedupe()
        return self.findings

    def _dedupe(self):
        editable = [f for f in self.findings if f["safety"] != REVIEW]
        review = [f for f in self.findings if f["safety"] == REVIEW]
        editable.sort(key=lambda f: (f["span"]["start_line"],
                                     -(f["span"]["end_line"] - f["span"]["start_line"]),
                                     f["rule_id"]))
        kept, last_end = [], 0
        for f in editable:
            s, e = f["span"]["start_line"], f["span"]["end_line"]
            if s <= last_end:
                self.exclusions["overlap_dropped"] += 1
                continue
            kept.append(f)
            last_end = e
        # file-local ids only; main() re-assigns ids across the whole report
        self.findings = assign_ids(kept + review)


def collect(path: Path, includes, excludes, max_bytes, max_files,
            exclude_dirs, source_exts):
    if path.is_file():
        files = [path]
    else:
        files = []
        for p in path.rglob("*"):
            if not p.is_file():
                continue
            parts = set(p.relative_to(path).parts[:-1]) if p.is_relative_to(path) else set()
            if parts & exclude_dirs:
                continue
            files.append(p)
    py_files, other_files, skipped = [], [], []
    for p in files:
        rel = os.path.relpath(str(p), str(path if path.is_dir() else path.parent)).replace(os.sep, "/")
        if any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(p.name, g) for g in excludes):
            skipped.append({"file": rel, "reason": "excluded by --exclude"})
            continue
        if p.suffix.lower() not in source_exts:
            skipped.append({"file": rel, "reason": "non-source extension"})
            continue
        if p.stat().st_size > max_bytes:
            skipped.append({"file": rel, "reason": "exceeds --max-bytes"})
            continue
        try:
            read_raw(p)
        except (UnicodeDecodeError, OSError):
            skipped.append({"file": rel, "reason": "not utf-8 text / unreadable"})
            continue
        if p.suffix.lower() == ".py" and any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(p.name, g) for g in includes):
            py_files.append(p)
        else:
            other_files.append(rel)
        if len(py_files) + len(other_files) >= max_files:
            break
    return sorted(py_files), sorted(other_files), skipped


def main(argv=None):
    ap = argparse.ArgumentParser(description="Scan Python sources for redundant code and emit an Agent-readable JSON report.")
    ap.add_argument("path", help="file or directory to scan")
    ap.add_argument("--out", default=None, help="JSON report path (default from config)")
    ap.add_argument("--md", default=None, help="also write a human-readable markdown view")
    ap.add_argument("--include", action="append", default=None, help="glob for python files (repeatable)")
    ap.add_argument("--exclude", action="append", default=None, help="glob to skip (repeatable)")
    ap.add_argument("--max-bytes", type=int, default=None)
    ap.add_argument("--max-files", type=int, default=None)
    ap.add_argument("--config", default=None, help="explicit rcp.config.json path")
    ap.add_argument("--print-json", action="store_true", help="print full JSON to stdout")
    ap.add_argument("--root", default=None, help="report root (default: scanned dir / file parent)")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    sc = cfg["scan"]
    includes = args.include or sc["include_globs"]
    excludes = args.exclude if args.exclude is not None else sc["exclude_globs"]
    max_bytes = args.max_bytes if args.max_bytes is not None else sc["max_bytes"]
    max_files = args.max_files if args.max_files is not None else sc["max_files"]
    out_path = args.out or cfg["output"]["report_json"]

    target = Path(args.path).resolve()
    if not target.exists():
        print(f"ERROR: path not found: {target}", file=sys.stderr)
        return 2
    root = Path(args.root).resolve() if args.root else (target if target.is_dir() else target.parent)
    py_files, other_files, skipped = collect(
        target, includes, excludes, max_bytes, max_files,
        set(sc["exclude_dirs"]), set(sc["source_extensions"]))

    findings, parse_errors, per_file = [], [], []
    for p in py_files:
        try:
            a = FileAnalyzer(p, root, cfg)
            fs = a.run()
            findings.extend(fs)
            per_file.append({
                "file": a.rel,
                "lines": len(a.bodies),
                "findings": len(fs),
                "auto_eligible": sum(1 for f in fs if f["safety"] != REVIEW),
                "exclusions": {k: v for k, v in a.exclusions.items() if v},
            })
        except SyntaxError as exc:
            rel = os.path.relpath(str(p), str(root)).replace(os.sep, "/")
            parse_errors.append({"file": rel, "error": f"SyntaxError: {exc.msg} at line {exc.lineno}"})

    # Single global numbering pass: ids are unique across every analysed file.
    findings = assign_ids(findings)

    by_safety = {k: sum(1 for f in findings if f["safety"] == k)
                 for k in (STATIC, VERIFIED, REVIEW)}
    by_rule = {}
    for f in findings:
        by_rule[f["rule_id"]] = by_rule.get(f["rule_id"], 0) + 1
    total_lines = sum(x["lines"] for x in per_file)
    removable = sum(f["span"]["end_line"] - f["span"]["start_line"] + 1
                    for f in findings if f["safety"] != REVIEW)

    report = {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL,
        "generated_at": utcnow(),
        "root": str(root).replace(os.sep, "/"),
        "scan_scope": {
            "target": str(target).replace(os.sep, "/"),
            "python_files_analyzed": len(py_files),
            "include_globs": includes,
            "exclude_globs": excludes,
            "max_bytes": max_bytes,
            "max_files": max_files,
            "config_sources": cfg.get("_meta", {}).get("loaded_from", {}),
        },
        "summary": {
            "files_scanned": len(py_files),
            "total_lines": total_lines,
            "findings_total": len(findings),
            "by_safety": by_safety,
            "by_rule": by_rule,
            "auto_eligible_lines": removable,
            "auto_eligible_ratio": round(removable / total_lines, 4) if total_lines else 0.0,
        },
        "safety_tiers": dict(SAFETY_TIERS),
        "findings": findings,
        "per_file": per_file,
        "unanalyzed_files": [
            {"file": rel,
             "reason": "Python AST 分析器不覆盖该语言",
             "agent_action": "按 references/report-schema.md 手动分析并把 finding 追加到本报告 findings 数组（保持同一 schema），再交给 apply_prune.py"}
            for rel in other_files[:200]
        ],
        "parse_errors": parse_errors,
        "skipped_files": skipped[:200],
        "canonical_note": "本报告 JSON 是唯一权威数据源；markdown 视图由 render_report.py 生成，禁止从 markdown 反推数据。",
    }

    out = Path(out_path).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.md:
        from render_report import render  # noqa: E402
        md = Path(args.md).resolve()
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text(render(report), encoding="utf-8")

    if args.print_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"[{TOOL}] scanned {len(py_files)} python file(s), {len(findings)} finding(s)")
        print(f"  static={by_safety[STATIC]}  verified={by_safety[VERIFIED]}  review={by_safety[REVIEW]}")
        print(f"  JSON: {out}")
        if args.md:
            print(f"  Markdown: {Path(args.md).resolve()}")
        if parse_errors:
            print(f"  parse errors: {len(parse_errors)} (see JSON)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

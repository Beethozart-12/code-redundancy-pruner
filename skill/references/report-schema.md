# Report Schema（Agent 可读契约）

`redundancy-report.json` 是唯一权威数据源。`redundancy-report.md` 由 `render_report.py` 派生，**禁止从 markdown 反推数据**。

## 1. 顶层结构

| 字段 | 类型 | 说明 |
|---|---|---|
| `schema_version` | string | 当前 `"1.0"` |
| `tool` | string | 必须 `"code-redundancy-pruner"`，`apply_prune.py` 会校验 |
| `generated_at` | ISO-8601 | UTC 生成时间 |
| `root` | string | 绝对路径；所有 `file` 相对此根解析 |
| `scan_scope` | object | `target` / `python_files_analyzed` / `include_globs` / `exclude_globs` / `max_bytes` / `max_files` / `config_sources`（本次生效的配置来源） |
| `summary` | object | `files_scanned` / `total_lines` / `findings_total` / `by_safety` / `by_rule` / `auto_eligible_lines` / `auto_eligible_ratio` |
| `safety_tiers` | object | 三个档位的文字说明 |
| `findings` | array | **核心数组**，结构见下 |
| `per_file` | array | 每文件 `file` / `lines` / `findings` / `auto_eligible` / `exclusions` |
| `unanalyzed_files` | array | 非 Python 文件；含 `agent_action` 提示 |
| `parse_errors` | array | AST 解析失败的文件，不参与精简 |
| `skipped_files` | array | 被排除 / 二进制 / 超限的文件 |
| `canonical_note` | string | 权威性声明 |

## 2. Finding 对象（必读）

| 字段 | 类型 | 约束 / 语义 |
|---|---|---|
| `id` | string | `F0001` 起，**整份报告内全局唯一**（不是每个文件各自从 F0001 开始）；由 `assign_ids()` 按 `(file, start_line, rule_id, title)` 排序后统一编号 |
| `rule_id` | string | 见 `redundancy-catalog.md` |
| `category` | string | 归类标签，如 `unused_import` / `dead_code` |
| `title` | string | 人类可读短标题 |
| `safety` | enum | `static` \| `verified` \| `review` |
| `confidence` | number | 0–1 |
| `behavior_change` | enum | `static`/`verified` 恒为 `none`；`review` 为 `unknown` |
| `file` | string | 相对 `root`，用 `/` 分隔；禁止绝对路径与 `..` |
| `language` | string | `python` / `javascript` / 自定 |
| `span` | object | `{start_line, end_line}`，1-based、闭区间 |
| `snippet` | string | 该区间的**精确原文**；行尾 `\r\n` 被剥离，行间用 `\n` 连接 |
| `expected_hash` | string | `"sha256:" + hex`，见第 4 节 |
| `reason` | string | 为什么判定为多余 + 为什么删除无损 |
| `evidence` | object | 机器可核验的证据；`unbound_names` 会被 L3 使用 |
| `edit` | object | `{action, start_line, end_line, replacement}`，见第 3 节 |
| `dependencies` | array | 需先应用的其它 finding id（当前规则集为空） |
| `verification` | array | 需执行的检查名；`review` 恒为 `[]` |
| `agent_hint` | string | **祈使句**，Agent 无需读源码即可执行的指令 |
| `why_blocked` | string | 仅 `review`：为何不能自动应用 |
| `suggestion` | string | 仅 `review`：如何人工确认 / 提升 |
| `justification` | string | 可选：人工把 `review` 提升为 `static`/`verified` 时必须写明依据 |

### 完整示例

```json
{
  "id": "F0003",
  "rule_id": "PY_UNUSED_IMPORT",
  "category": "unused_import",
  "title": "未使用的导入 os",
  "safety": "verified",
  "confidence": 0.93,
  "behavior_change": "none",
  "file": "src/foo.py",
  "language": "python",
  "span": {"start_line": 3, "end_line": 3},
  "snippet": "import os",
  "expected_hash": "sha256:<64 hex>",
  "reason": "绑定名 os 在模块内无 Load 引用；删除不改变执行路径，残余风险是模块导入副作用，故要求行为验证。",
  "evidence": {"bindings": ["os"], "reference_count": 0, "statement": "Import",
               "unbound_names": ["os"]},
  "edit": {"action": "delete_lines", "start_line": 3, "end_line": 3, "replacement": ""},
  "dependencies": [],
  "verification": ["span_hash", "syntax_parse", "deletion_only", "name_resolution", "tests"],
  "agent_hint": "Delete lines 3-3 of src/foo.py exactly as described in edit; do not rewrite any other line; run apply_prune.py to verify and apply."
}
```

## 3. `edit` 动作

| action | 语义 | 约束 |
|---|---|---|
| `delete_lines` | 删除 `[start_line, end_line]` 整行 | `replacement` 必须为 `""`；不得留下空 suite |
| `replace_lines` | 用单行 `replacement` 替换该区间 | `replacement` **不得含换行**；其标识符 token 必须是原区间标识符的**子集**；长度不得超过原区间；不得引入新名称 |

`replace_lines` 的典型用途：
- `import sys, json` → `import json`
- `except ValueError as exc:` → `except ValueError:`

禁止使用 `edit` 表达「抽函数」「去嵌套」「改名」等重构。`apply_prune.py` 只实现这两种纯删减动作。

## 4. `expected_hash` 计算（跨语言一致）

```python
raw = open(path, "r", encoding="utf-8", newline="").read()  # 保留 CRLF
lines = raw.splitlines(keepends=True)
bodies = [ln.rstrip("\r\n") for ln in lines]
text = "\n".join(bodies[start_line - 1:end_line])
expected_hash = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
```

要点：
- 行尾统一按 `\n` 参与哈希 → CRLF / LF 文件都能匹配。
- `snippet` 字段就是上面的 `text`。
- 应用前 `apply_prune.py` 重新计算并比对（L0）。源码变动后哈希失配，该 finding 被标记 `span_hash mismatch` 并跳过，绝不误删。

## 5. 跨语言补写 finding（Agent 操作指南）

`scan_redundancy.py` 只对 `.py` 做 AST 分析；其它文件列入 `unanalyzed_files`。补写步骤：

1. 打开目标文件，定位可证明无引用的冗余行（未使用 import / 未使用变量 / 不可达分支等）。
2. 按第 4 节计算 `snippet` 与 `expected_hash`。
3. 构造第 2 节对象，`language` 填实际语言，`rule_id` 用 `<LANG>_<RULE>` 命名（如 `JS_UNUSED_CONST`）。
4. `safety` 只在前述证据充分时填 `static`；有任何疑问填 `review`。
5. `evidence.unbound_names` 填被删除绑定名（Python 的 L3 会用到；非 Python 可省略）。
6. 追加到 `findings` 数组，保证 `id` 唯一。
7. 运行 `python scripts/render_report.py redundancy-report.json --out redundancy-report.md` 刷新视图。
8. 运行 `python scripts/apply_prune.py redundancy-report.json --dry-run` 校验。
9. 应用时 `apply_prune.py` 按 finding 的 `language` / 文件扩展名，从 `config/defaults.json` 的 `extension_language` + `language_syntax_commands` 自动选语法检查命令；也可显式 `--syntax-cmd "<命令> {file}"`。命令缺失、工具未安装或当前平台不支持时 L1 记为 `L1_skipped_no_command` 警告（不阻断）。配置方法见 `quickstart.md` §3。

### 非 Python 的最小可用 JSON（可直接改）

```json
{
  "schema_version": "1.0",
  "tool": "code-redundancy-pruner",
  "root": "<绝对目录>",
  "findings": [{
    "id": "F0001", "rule_id": "JS_UNUSED_CONST", "category": "unused_variable",
    "title": "未使用的常量", "safety": "static", "confidence": 0.9,
    "behavior_change": "none", "file": "app.js", "language": "javascript",
    "span": {"start_line": 2, "end_line": 2},
    "snippet": "const unused = 2;", "expected_hash": "sha256:<64 hex>",
    "reason": "该绑定在文件内无引用，删除不改变执行路径。",
    "evidence": {"unbound_names": ["unused"]},
    "edit": {"action": "delete_lines", "start_line": 2, "end_line": 2, "replacement": ""},
    "dependencies": [], "verification": [], "agent_hint": "Delete line 2 of app.js"
  }]
}
```

`scripts/selftest.py` 的第 29 项检查就是这个用例的端到端验证。

## 6. `apply_prune.py` 的校验规则

- `tool`、`root`、`findings` 必须存在。
- 每个 finding 必须含：`id`, `rule_id`, `safety`, `file`, `span`, `snippet`, `expected_hash`, `edit`。
- `safety` ∈ {static, verified, review}；`id` 唯一；`1 <= start_line <= end_line`。
- `edit.action` ∈ {delete_lines, replace_lines}；`replace_lines.replacement` 必须是字符串。
- `expected_hash` 必须以 `sha256:` 开头。
- `file` 不得为绝对路径或含 `..`。
- 同文件被选中的 finding 不得区间重叠；重叠项进入 `rejected`。
- `review` 项无条件跳过，不存在「强制应用 review」的开关。

## 7. 结果文件 `prune-result.json`

| 字段 | 说明 |
|---|---|
| `mode` | `apply` / `dry-run` |
| `applied_ids` / `would_apply_ids` | 实际 / 预演将应用的 id |
| `skipped` | 每项含 `id` + `reason`（review / 需测试 / 哈希失配） |
| `rejected` | 区间重叠 / 非法 edit |
| `warnings` | 非致命，如非 Python 缺 L1 检查 |
| `files[]` | `lines_before` / `lines_after` / `lines_removed` / `verification[]` / `verification_level` / `edits[]` |
| `tests` | 命令、baseline / after 的返回码与 stdout 哈希 |
| `backup_manifest` | 回滚清单路径 |
| `diff` | unified diff 全文 |
| `rollback_hint` | 可直接执行的回滚命令 |

`verification_level`：`L2` / `L3` / `L4` / `L4-pending`（有测试但处于 dry-run）。

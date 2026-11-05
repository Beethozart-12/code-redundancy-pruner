# 无损保证与验证协议

## 1. 「无损」的定义与可操作定义

### 三层无损（缺一不可）

| 维度 | 含义 | 保障手段 |
|---|---|---|
| 语义无损 | 只删除 / 只做标识符子集替换；不改变任何可观测行为 | AST 静态证明 + L0–L4 分层验证 |
| 信息无损 | 不删 docstring、不删注释、不改缩进结构、不合并语句、不重命名、不压缩成一行 | 保守规则集 + REVIEW 档位 |
| 可回溯无损 | 任何写入都有备份与一键回滚；验证失败自动回滚 | manifest + `--rollback` |

精简结果必须仍是正常可读源码（人 / Agent 均可读），不是 minify 或混淆。

### 硬约束（禁止事项）

- 禁止改写任何未在 `edit` 中声明的行；禁止新增行、合并行、重排、重命名、格式化。
- 禁止 minify、混淆、删除 docstring / 注释 / 空行结构。
- 禁止自动删除函数 / 类 / 文件 / 整段分支；这类一律 REVIEW。
- 禁止在没有 `--apply` 时写盘；`--apply` 必须有备份 manifest。
- 禁止把 `review` 自动提升；禁止跳过 L0 哈希门禁。
- 禁止对 `*.pyi`、生成代码、vendor、`.git` 目录直接精简；先用 `--exclude` 排除。

### 可操作定义

`apply_prune.py` 不追求通用程序等价性证明（不可判定），而是把无损约束收敛成**可机械检查的三条**：

1. **只删不增**：`delete_lines` 纯删除；`replace_lines` 的标识符 token 必须是原区间的子集，且不得引入新行、不得让行数增长、不得改名。
2. **语法仍合法**：Python 用 `ast.parse`；其它语言用 `--syntax-cmd`。
3. **名称仍可解析**：被删除区间里定义的名字，若在新 AST 中被 `Load` 且未在别处重新绑定，则判定为破坏性删除，整批拒绝。

再加上**行为验证（测试）**与**可回滚（备份）**，构成工程上可交付的无损保证。

精简结果必须保持原有缩进、语句分行、docstring、注释和命名 —— Agent 与人类都能继续读懂。

## 2. 分层验证

实际执行顺序为 L0 → L2 → L1 → L3 → L4。

| 层 | 名称 | 检查内容 | 失败行为 |
|---|---|---|---|
| L0 | `L0_span_hash` | 重新读取磁盘，按 `report-schema.md` 第 4 节计算哈希并比对 `expected_hash`；文件缺失 / 越界同样拒绝 | 该 finding 进入 `skipped`，其它项继续 |
| L2 | `L2_deletion_or_token_subset` | `delete_lines` 天然零新增；`replace_lines` 验证标识符子集、单行、长度不增；整文件行数不增 | 整批 `ERROR`，退出码 3，零写入 |
| L1 | `L1_ast_parse` / `L1_syntax_cmd` | Python：`ast.parse(new_text)`；其它语言：配置自动匹配或 `--syntax-cmd "node --check {file}"` 在临时文件上运行 | 整批拒绝，退出码 3 |
| L1 | `L1_skipped_no_command` | 非 Python 且无**可用**检查命令（未配置 / 工具未安装 / 当前平台不支持，如 Windows 上的 `bash -n`） | 记入 `warnings`，不阻断 |
| L3 | `L3_name_resolution` | `removed_bindings = evidence.unbound_names ∪ bindings_in_snippet`；`danger = (removed - bound_new) ∩ loads_new - builtins`；非空即拒绝 | 整批拒绝，退出码 3 |
| L4 | `tests` | `--tests "<cmd>"` 在**写盘前**跑 baseline（必须 rc=0），写盘后再跑（rc=0 且 stdout 哈希一致，除非 `--no-compare-output`） | 自动回滚，退出码 4 |

`bindings_in_snippet` 先尝试 `ast.parse(textwrap.dedent(snippet))` 收集 `Store` 名 / `def` / `class` / `arg` / `alias` / `except as` / `global` / `nonlocal`；解析失败（如 `except ... as e:` 片段）时退回正则提取。Python 内置名不参与 danger 判定，避免把 `print` 误判成被删绑定。

`verification_level` 的值域见 `report-schema.md` §7（单一定义）。

## 3. 门禁矩阵

| safety | 无 `--tests` | 有 `--tests` | `--accept-verified` |
|---|---|---|---|
| `static` | 应用 | 应用（L4） | 应用 |
| `verified` | **跳过**（`skipped` 记 `needs --tests`） | 应用（L4） | 应用（L0–L3） |
| `review` | 跳过 | 跳过 | 跳过 |

`--accept-verified` 是用户显式接受「导入可能带副作用」这一残余风险，结果 JSON 中 `verified_accepted_without_tests: true` 留痕。

## 4. 写盘与回滚

**默认 dry-run**；只有 `--apply` 才写盘。写盘前：

1. git 工作区检查：`git -C <root> status --porcelain` 非空且未加 `--allow-dirty` → 拒绝，退出码 3。
2. 备份到 `<root>/.redundancy-pruner/backups/<YYYYmmdd-HHMMSS>/files/<原相对路径>`（`shutil.copy2` 保留元数据）。
3. 写 `manifest.json`：

```json
{
  "schema_version": "1.0",
  "tool": "code-redundancy-pruner",
  "created_at": "...", "root": "...", "report": "...",
  "applied_ids": ["F0001"],
  "files": [{"file": "src/foo.py", "sha256_before": "<hex>"}]
}
```

4. 以 `newline=""` 写回，保留原 CRLF / LF 行尾（selftest 第 25、27 项验证）。
5. L4 失败 → 用备份逐文件 `copy2` 还原，打印 `Automatic rollback performed.`，退出码 4。

手动回滚：

```bash
python scripts/apply_prune.py --rollback <.../manifest.json>
# 可选 --rollback-root <替代根目录>
```

回滚后逐文件比对 `sha256_before`，不一致会报 `hash mismatch after restore`。

退出码：`0` 成功 / `2` 报告校验失败 / `3` 验证或门禁失败（零写入）/ `4` 测试失败并已自动回滚。

## 5. 已知局限（必须向用户说明）

- **单文件静态分析**：无跨模块调用图。`PY_UNUSED_DEF` 一律 REVIEW，不会自动删定义。
- **导入副作用无法静态排除**：这是 `PY_UNUSED_IMPORT` 归为 `verified` 的唯一原因；有测试时以测试为准。
- **动态名称解析**：`eval` / `exec` / `globals` / `locals` / `__import__` 出现时，导入类规则降级 REVIEW；但反射式 `getattr(obj, "name")` 不在静态可判定范围。
- **`__all__` 只识别字面量**：`__all__ += [...]`、拼接、动态构造会被判为未导出，可能误报 → 用 `# rcp:keep` 抑制。
- **无语义等价证明**：保证的是「纯删减 + 语法合法 + 名称可解析 + 测试通过」，不是形式化等价。
- **测试可能不稳定**：flaky 测试会触发回滚；`--no-compare-output` 可放宽 stdout 比较，但退出码仍必须 0。
- **非 Python**：L3 不适用；L1 依赖本机可用的语法检查命令，配置自动匹配，工具缺失 / 平台不支持时自动跳过，只剩 L0 + L2（计入 `warnings`）。
- **不支持**：`*.pyi`、宏 / 模板元编程、压缩成一行的改写、`try/finally` 中依赖行号的 traceback 断言。
- **行号漂移**：删除后行号变化，任何基于旧行号的报告必须重扫（L0 哈希门禁会拦截）。

## 6. 与 self-improving-agent 的对接

| 事件 | 动作 |
|---|---|
| 扫描前 | 读 `.learnings/LEARNINGS.md`，`grep "Pattern-Key: redundancy."`，对已知误报模式先加 `# rcp:keep` 或 `--exclude` |
| 误报 / 用户纠正 | 追加 `LRN-YYYYMMDD-XXX`，`Source: simplify-and-harden`，`Pattern-Key: redundancy.<rule_id>`，`Recurrence-Count` 递增 |
| 新模式 | 追加 `FEAT-...`，考虑在 `redundancy-catalog.md` 增补规则 |
| 命令 / 工具失败 | 追加 `ERR-...` 到 `.learnings/ERRORS.md` |
| 复发 ≥3 且跨 ≥2 任务 | 提升为 `AGENTS.md` / `CLAUDE.md` 的短预防规则，原条目 `Status: promoted` |
| 解决后 | 原条目 `Status: resolved` + `### Resolution` 块 |

首次使用需初始化 `.learnings/`（`LEARNINGS.md` / `ERRORS.md` / `FEATURE_REQUESTS.md`），不要覆盖已有文件。日志只写简短摘要与路径，不写密钥、完整源码或整段输出。

## 7. 自检覆盖

`python scripts/selftest.py` 覆盖：规则命中、schema 完整性、**多文件 ID 全局唯一**、markdown 视图、dry-run 零写入、verified 门禁、实际应用、L3、diff、语法保留、目标冗余消失、有效代码保留、行数不增长、备份、字节级回滚、CRLF、过期哈希拒绝、跨语言手工 finding、测试失败自动回滚、doctor / init-config / 门面转发 / 规则开关 / 配置驱动 L1 / 自我扫描零冗余 / 子进程非 UTF-8 输出容错 / 内置语法命令平台守卫。检查项数量以运行时输出为准，文档不写死数字。

改动任一脚本后必须重跑；全部 PASS 才可交付。**新增检查必须覆盖真实使用形态**（多文件、目标平台、默认配置），不能用替身命令绕过后再宣称可用。

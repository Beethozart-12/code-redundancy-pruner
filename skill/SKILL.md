---
name: code-redundancy-pruner
description: "Detect and display redundant/dead code, emit an Agent-readable structured report (canonical JSON with stable finding IDs, exact line spans, snippets, evidence, confidence, safety tiers and machine-actionable edit ops, plus a derived markdown view), then apply only behavior-preserving, readability-preserving simplifications with layered verification, backups and rollback. Ships a zero-dependency doctor, project config generator, unified CLI and Windows/Unix launchers so first-time users can run it from scratch. This skill should be used when a user asks to find/show/list 多余代码 / 冗余代码 / 无用代码 / dead code / unused imports or variables / unreachable statements / commented-out code / duplicate blocks, or asks for 无损精简 / 无损压缩 / lossless code pruning, or when an Agent needs a machine-parseable redundancy report it can act on. Python is analyzed deterministically via AST; any other language is supported through Agent-authored findings in the same schema."
agent_created: true
---

# Code Redundancy Pruner

## Purpose

把「多余代码」变成 **Agent 可直接消费的结构化清单**，再只做**可证明无损**的精简。三层无损（语义 / 信息 / 可回溯）定义与验证协议见 `references/lossless-guarantee.md`。精简后的代码必须仍是人 / Agent 都能读懂的正常源码，不是 minify、不是混淆。

## When to use

- 用户说「找出多余的代码」「显示冗余代码」「列出没用的 import / 变量 / 死代码」。
- 用户说「无损精简」「精简一下别改行为」「删掉冗余但不许改逻辑」。
- Agent 需要一份可解析、可定位、可执行的冗余清单，而不是自然语言描述。
- 代码交接 / 重构前清理，需要可审计、可回滚的删除记录。

不适用：主动重构（抽函数、去嵌套、改名、改架构）、性能优化、minify/打包、删除整文件。

## 从零开始（首次使用必读）

环境：Python 3.9+，**零第三方依赖**，无需 pip install、无需联网。`SKILL_DIR` = 本 SKILL.md 所在目录。

```bash
python "$SKILL_DIR/scripts/rcp.py" doctor          # 环境自检，必须先做
python "$SKILL_DIR/scripts/rcp.py" init-config     # 可选：生成项目 rcp.config.json
python "$SKILL_DIR/scripts/rcp.py" scan .          # 生成 JSON + markdown 报告
python "$SKILL_DIR/scripts/rcp.py" prune redundancy-report.json --dry-run
```

Windows 用 `SKILL_DIR\rcp.bat`，Linux / macOS 用 `SKILL_DIR/rcp.sh`。完整命令速查、配置示例、典型场景与故障排查见 **`references/quickstart.md`**。

## Agent-readable contract（核心约束）

1. `redundancy-report.json` 是**唯一权威数据源**；`redundancy-report.md` 只是派生视图。
2. 决策、编辑、校验一律读 JSON；**禁止从 markdown 反推数据**。
3. 每个 finding 必须自足：`id` / `rule_id` / `file` / `span` / `snippet` / `expected_hash` / `reason` / `evidence` / `edit` / `safety` / `confidence` / `agent_hint`。Agent 无需重读整份源码即可理解并行动。
4. `edit` 必须是机器动作：`delete_lines` 或 `replace_lines` + 精确行号 + `replacement`。禁止自然语言式「把这里简化一下」。
5. `expected_hash` 防止报告过期：源码变动后必须重新扫描。
6. ID 稳定、顺序确定、JSON 无 ANSI / 无省略号 / 无「其余 N 项略」。完整字段规范见 `references/report-schema.md`。

## 安全档位

`static` / `verified` / `review` 三档。门禁矩阵、验证层级、`review` 永不自动应用等细则见 `references/lossless-guarantee.md` §3；规则清单与误报防护见 `references/redundancy-catalog.md`。禁止脚本自动提升 `review`。

## Workflow（Agent 执行）

1. **前置**：记录范围、语言、测试命令；`git status --porcelain`（脏工作区先提交/stash，用户确认后才 `--allow-dirty`）；按 self-improving-agent 约定读 `.learnings/LEARNINGS.md` 中 `Pattern-Key: redundancy.*` 的已知误报。
2. **扫描并显示**：`rcp.py scan <target>` → 向用户展示 `summary`、三档分组、每条代码片段与行号。非 Python 文件见 `unanalyzed_files`，按 `report-schema.md` §5 补写 finding。
3. **人工确认**：逐条说明 REVIEW 的 `why_blocked` / `suggestion`；VERIFIED 确认 `--tests`，无测试时明确询问 `--accept-verified`（残余风险：模块导入副作用）。禁止跳过此步直接 `--apply`。
4. **预演**：`rcp.py prune redundancy-report.json --dry-run --show-diff`，检查 `prune-result.json` 的 `would_apply_ids` / `verification` / `skipped` / `rejected`。
5. **应用**：`rcp.py prune redundancy-report.json --apply --tests "pytest -q"`（或 `--accept-verified`）。非 Python 语法检查命令由配置自动匹配，也可显式 `--syntax-cmd`。
6. **汇报与回滚**：汇报 `prune-result.json`；用户反悔用 `rcp.py rollback <manifest>`；测试失败自动回滚（退出码 4）。
7. **学习闭环**：误报 / 纠正 / 新模式 → `.learnings/LEARNINGS.md`，`Pattern-Key: redundancy.<rule_id>`；复发 ≥3 次且跨 ≥2 任务 → 提升到 `AGENTS.md` / `CLAUDE.md`。细则见 `lossless-guarantee.md` §6。

## 红线

- 禁止改写未在 `edit` 中声明的行；禁止新增 / 合并 / 重排 / 重命名 / 格式化 / minify。
- 禁止自动删函数 / 类 / 文件 / 整段分支；`review` 永不自动应用。
- 禁止无 `--apply` 写盘；`--apply` 必须有备份 manifest；禁止跳过 L0 哈希门禁。

其余硬约束、已知局限见 `references/lossless-guarantee.md`。

## 自检

```bash
python "$SKILL_DIR/scripts/rcp.py" doctor --full
python "$SKILL_DIR/scripts/selftest.py"     # 端到端检查，数量以运行时输出为准
```
改动任一脚本后必须重跑；内含「扫描技能自身源码应为 0 冗余」的 dogfooding 验收项。

**交付前复验（不可跳过）**：把重新打好的 zip 解压到全新目录，在另一个空白项目目录里**只按 `quickstart.md` 的命令**跑一遍。必须包含 ≥2 个源码文件的多文件报告、目标平台的默认配置路径；禁止用替身命令绕过内置默认命令后宣称可用。历史教训见 `.learnings/ERR-20261105-002`。

## Resources

| 文件 | 用途 |
|---|---|
| `scripts/rcp.py` | **统一入口**：doctor / init-config / config / 转发各子命令 |
| `scripts/rcp_common.py` | 共享常量、哈希、配置加载（消除跨脚本重复） |
| `config/defaults.json` | 唯一默认配置源：排除目录、规则开关、语言语法命令、默认值 |
| `scripts/scan_redundancy.py` | Python AST 确定性扫描 → canonical JSON + markdown |
| `scripts/render_report.py` | JSON → 人类可读 markdown（派生视图） |
| `scripts/apply_prune.py` | 校验 / L0–L4 验证 / 应用 / 备份 / 回滚（语言无关编辑层） |
| `scripts/selftest.py` | 端到端自检 |
| `rcp.bat` / `rcp.sh` | Windows / Unix 一键包装，自动找 Python |
| `references/quickstart.md` | **从零开始**、配置、场景、故障排查 |
| `references/report-schema.md` | 完整字段规范、示例 JSON、跨语言补写 finding |
| `references/redundancy-catalog.md` | 规则表、无损性论证、误报防护、抑制标记 |
| `references/lossless-guarantee.md` | 三层无损、L0–L4 验证协议、回滚、已知局限、学习闭环 |
| `assets/fixtures/sample_redundant.py` | 自检用冗余样本 |

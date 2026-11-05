# code-redundancy-pruner

一个 Agent Skill：把「多余代码」变成 **Agent 可直接消费的结构化清单**，再只做**可证明无损**的精简。

Python 3.9+ · 零第三方依赖 · 无需联网 · Windows / Linux / macOS

## 它能做什么

| 需求 | 实现 |
|---|---|
| 显示多余代码 | 12 条 Python AST 规则 → JSON + Markdown 双视图，每条带行号、原代码片段、判定理由、证据 |
| Agent 能看懂 | `redundancy-report.json` 是**唯一权威源**；每条 finding 自足（`id` / `span` / `snippet` / `expected_hash` / `evidence` / `edit` / `agent_hint`），`edit` 是可直接执行的 `delete_lines` / `replace_lines` 机器动作，不是自然语言描述 |
| 无损精简 | 语义无损（纯删减 + 标识符子集替换）+ 信息无损（保留注释 / 缩进 / 命名，禁止 minify）+ 可回溯（备份 manifest + 一键回滚） |

精简后仍是人和 Agent 都能读的正常源码，不是压缩或混淆。

## 安装

```bash
# 方式 A：解压分发包（推荐）
unzip dist/code-redundancy-pruner.zip -d ~/.workbuddy/skills/

# 方式 B：从本仓库复制
cp -r skill ~/.workbuddy/skills/code-redundancy-pruner
```

Windows 对应 `%USERPROFILE%\.workbuddy\skills\`。下文 `<RCP>` 指该技能目录。

## 三步跑通

在**待分析的项目目录**里执行：

```bash
python <RCP>/scripts/rcp.py doctor          # 1) 环境自检，必须先做
python <RCP>/scripts/rcp.py init-config     # 2) 可选：生成项目 rcp.config.json
python <RCP>/scripts/rcp.py scan .          # 3) 生成 JSON + Markdown 报告
python <RCP>/scripts/rcp.py prune redundancy-report.json --dry-run --show-diff
```

Windows 可用 `<RCP>\rcp.bat doctor`，Linux / macOS 可用 `bash <RCP>/rcp.sh doctor`。

确认清单无误后才真正写盘：

```bash
python <RCP>/scripts/rcp.py prune redundancy-report.json --apply --tests "pytest -q"
python <RCP>/scripts/rcp.py rollback <backup_manifest.json>   # 反悔时还原
```

完整命令速查、配置合并规则、典型场景与故障排查见 [`skill/references/quickstart.md`](skill/references/quickstart.md)。

## 安全档位

| 档位 | 含义 | 是否自动应用 |
|---|---|---|
| `static` | 静态可证明语义恒等 | 是 |
| `verified` | 近似无损，残余风险需行为验证（未使用导入可能有模块副作用） | 需 `--tests` 通过或显式 `--accept-verified` |
| `review` | 证据不足或会丢失信息 / 结构 | **否，永不自动应用** |

验证链：L0 哈希门禁 → L2 纯删减 → L1 语法 → L3 名称解析 → L4 测试前后对比；失败零写入或自动回滚。非 Python 目标若本机没有可用的语法检查工具，L1 会降级为 warning 而不会阻断应用。

## Agent 契约（核心约束）

1. 决策 / 编辑 / 校验一律读 JSON，**禁止从 Markdown 反推数据**。
2. 禁止改写未在 `edit` 中声明的行；禁止新增、合并、重排、重命名、格式化、minify。
3. `expected_hash` 防止报告过期——源码变动后必须重新扫描。
4. ID 在**整份报告内全局唯一**且顺序确定；JSON 无 ANSI、无省略号、无「其余 N 项略」。

完整字段规范见 [`skill/references/report-schema.md`](skill/references/report-schema.md)，无损性论证与验证协议见 [`skill/references/lossless-guarantee.md`](skill/references/lossless-guarantee.md)，规则清单与误报防护见 [`skill/references/redundancy-catalog.md`](skill/references/redundancy-catalog.md)。

## 验证状态

| 检查 | 结果 |
|---|---|
| `python skill/scripts/selftest.py` | **60 / 60 PASS** |
| 同一自检在 Python 3.9 与 3.13 下 | 各 60 / 60 PASS |
| `rcp.py doctor` | HEALTHY 9/9 |
| 扫描技能自身源码（6 文件） | 0 finding |
| 多文件报告 | 17 findings / 17 个唯一 ID，`prune` RC=0 |
| 真实写盘 + 回滚 | 应用 9 项、删 8 行、字节级还原 |
| 跨平台 | Windows（cmd / PowerShell）与 Git Bash 双通道实测 |

CI 在每次 push / PR 时于 Ubuntu + Python 3.9 / 3.11 / 3.13 复跑 `doctor --full`。

改动任一脚本后必须重跑自检；交付前还要把新打的 zip 解压到全新目录，按 quickstart 原样复验（必须包含多文件与目标平台默认配置）。

## 仓库结构

| 路径 | 说明 |
|---|---|
| `skill/` | 完整技能目录，整体复制到 `~/.workbuddy/skills/code-redundancy-pruner` 即可 |
| `skill/SKILL.md` | 技能入口、Agent 执行流程与红线 |
| `skill/config/defaults.json` | 唯一默认配置源（排除目录、规则开关、语言语法命令） |
| `skill/scripts/` | 扫描 / 渲染 / 精简 / 统一入口 / 共享层 / 自检 |
| `skill/references/` | quickstart、report-schema、redundancy-catalog、lossless-guarantee |
| `skill/assets/fixtures/` | 自检用冗余样本 |
| `dist/code-redundancy-pruner.zip` | 可直接安装的分发包 |

## 已知边界

- 确定性分析目前覆盖 Python；其它语言由 Agent 按同一 schema 补写 finding，编辑层本身语言无关。
- 不提供形式化语义等价证明；保证的是「纯删减 + 语法合法 + 名称可解析 + 测试通过」。
- 不适用于主动重构（抽函数、改名、改架构）、性能优化、minify、删除整文件。

## 许可证

MIT，见 [LICENSE](LICENSE)。

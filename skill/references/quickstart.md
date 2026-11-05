# 从零开始使用（Quickstart）

面向第一次拿到本 skill、对环境一无所知的用户 / Agent。**不需要 pip install，不需要虚拟环境，不需要联网。**

## 0. 环境要求

| 项 | 要求 |
|---|---|
| Python | 3.9+（`ast.end_lineno` / `ast.unparse`） |
| 第三方依赖 | **无**，全部标准库 |
| 操作系统 | Windows / Linux / macOS |
| 权限 | 目标目录可读；`--apply` 时需要可写 |
| git | 可选；存在 git 仓库时会做工作区脏检查 |

放置位置：把整个 `code-redundancy-pruner/` 目录放到 `~/.workbuddy/skills/`（Windows 为 `C:\Users\<你>\.workbuddy\skills\`），或直接解压 `code-redundancy-pruner.zip`。目录内 `SKILL.md` 与 `config/defaults.json` 必须存在。

下文 `RCP` 代表该目录。所有命令都在**待分析的项目目录**里执行。

## 1. 三步跑通

```bash
# 1) 环境自检（必须先做）
python <RCP>/scripts/rcp.py doctor

# 2) 可选：在项目根生成配置（不生成也能跑，用内置默认）
python <RCP>/scripts/rcp.py init-config

# 3) 扫描 + 生成报告，再预演精简
python <RCP>/scripts/rcp.py scan .
python <RCP>/scripts/rcp.py prune redundancy-report.json --dry-run --show-diff
```

Windows 可用包装脚本，避免拼路径：

```bat
RCP\rcp.bat doctor
RCP\rcp.bat scan .
RCP\rcp.bat prune redundancy-report.json --dry-run
```

Linux / macOS：`bash <RCP>/rcp.sh doctor`（也可 `chmod +x` 后直接执行）。

确认无误后才真正写盘：

```bash
python <RCP>/scripts/rcp.py prune redundancy-report.json --apply --tests "pytest -q"
```

`doctor --full` 会顺带跑完整自检；任何时候 `python <RCP>/scripts/rcp.py selftest` 可单独跑。

## 2. 命令速查

| 命令 | 作用 | 默认写盘 |
|---|---|---|
| `rcp.py doctor [--full]` | 环境 / 配置 / 脚本 / 文档检查 | 否 |
| `rcp.py init-config [--dir DIR] [--force]` | 生成项目 `rcp.config.json` | 是（仅配置文件） |
| `rcp.py config` | 打印合并后的生效配置 | 否 |
| `rcp.py scan <target> [扫描参数…]` | 生成 `redundancy-report.json` + `.md` | 是（仅报告） |
| `rcp.py render <report.json> [--out X.md]` | 从 JSON 重新生成 markdown | 是（仅视图） |
| `rcp.py prune <report.json> [精简参数…]` | 默认 dry-run；`--apply` 才改源码 | 否 / `--apply` |
| `rcp.py rollback <manifest.json>` | 从备份逐文件还原 | 是（还原） |
| `rcp.py selftest` | 端到端自检 | 否（临时目录） |

`scan` / `prune` 后面可继续跟底层脚本参数（`argparse.REMAINDER` 原样转发）：

```bash
rcp.py scan src --exclude '*_test.py' --include '*.py' --no-md --print-json
rcp.py prune redundancy-report.json --apply --accept-verified --show-diff
rcp.py prune redundancy-report.json --apply --syntax-cmd "node --check {file}"
```

`--config` 放在子命令**前面**（如 `rcp.py --config ./my.json config`）；`scan`/`prune` 也接受放在末尾，会被转发给底层脚本。

## 3. 配置（单一来源）

`config/defaults.json` 是唯一默认值来源。合并优先级：**命令行参数 > 显式 `--config` > 项目根 `rcp.config.json`（向上最多找 8 层）> 内置 defaults**。

`rcp.py init-config` 生成的模板只写少量字段，其余自动继承。常用示例（写进 `rcp.config.json`）：

```json
{
  "config_version": "1.0",
  "scan": {
    "exclude_globs": ["*_test.py", "generated/*"],
    "exclude_dirs": [".git", "__pycache__", "node_modules", "vendor"]
  },
  "rule_overrides": {
    "PY_COMMENTED_OUT_CODE": false,
    "PY_UNUSED_IMPORT": "review"
  },
  "prune": {
    "default_test_command": "pytest -q",
    "test_timeout": 600
  },
  "language_syntax_commands": {
    "javascript": "node --check {file}"
  }
}
```

规则：
- **列表字段是整体覆盖，不是追加**（`exclude_dirs` 写空数组会清掉内置排除目录）。
- `rule_overrides` 值：`false` 禁用该规则；`"static" | "verified" | "review"` 强制覆盖安全档位。禁用后 `per_file[].exclusions.rule_disabled_by_config` 会记数。
- `prune.default_test_command` 一旦设置，无需每次手打 `--tests`；`verified` 档自动获得 L4 验证。
- 非 Python 语法检查命令由 `extension_language` + `language_syntax_commands` 自动匹配；`{file}` 是临时文件路径占位符。**零配置保证**：命令对应的工具未安装、或命令含类 Unix 专用路径 / 重定向（`/dev/null`、`/tmp/`）、或 Windows 上解析到 `bash`/`sh`（易触发 WSL），都会自动跳过 L1 并给出 warning，**不会**让 `--apply` 失败。只有命令已确认可用却真的执行失败，才会判定 L1 不通过。
- `scan.suppress_markers` 控制源码内联抑制，默认 `rcp:keep` / `noqa` / `pragma: keep`。

## 4. 典型场景

| 场景 | 做法 |
|---|---|
| 项目没有测试 | 先 dry-run 看清单；确认后 `--apply --accept-verified`（结果 JSON 会留痕）；或只让 `static` 生效：把 `PY_UNUSED_IMPORT` 覆盖为 `review` |
| 误报太多 | 源码加 `# rcp:keep`，或在 `rule_overrides` 禁用对应 rule_id |
| 只想看不动 | `rcp.py scan .` 后只读 `redundancy-report.md`；不跑 `prune --apply` |
| 非 Python 文件 | 按 `report-schema.md` §5 补写 finding；配置好对应语言的 `language_syntax_commands`，`prune` 会自动跑 L1 |
| 精简后想反悔 | `rcp.py rollback <backup_manifest.json>`；测试失败会自动回滚 |
| 报告过期 | 源码变动后 L0 哈希门禁会拒绝，重跑 `scan` 即可 |

## 5. 输出文件

| 文件 | 说明 |
|---|---|
| `redundancy-report.json` | **唯一权威数据源**，Agent 消费它 |
| `redundancy-report.md` | 派生人类视图，禁止从中反推数据 |
| `prune-result.json` | 应用/预演结果：applied / skipped / rejected / 验证层级 / diff / 备份路径 |
| `.redundancy-pruner/backups/<时间戳>/manifest.json` | 回滚清单 + 原文件副本 |

## 6. 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| `ModuleNotFoundError: rcp_common` | 直接复制了单个脚本 | 保持 `scripts/` 目录完整；用 `rcp.py` 入口 |
| `doctor` 报 Python 版本 FAIL | Python < 3.9 | 升级 Python；Windows 可用 `py -3` |
| `span_hash mismatch` | 扫描后源码被改动 | 重跑 `scan` |
| `verified tier: needs --tests` | 未使用导入缺行为验证 | 提供 `--tests` 或 `--accept-verified` 或配置默认测试命令 |
| `git working tree has uncommitted changes` | 脏工作区保护 | 提交/stash，或确认后加 `--allow-dirty` |
| `tests failed ... Automatic rollback` | L4 前后不一致 | 检查 flaky 测试；可用 `--no-compare-output` 只比退出码 |
| 非 Python 提示 `L1_skipped_no_command` | 该语言未配置检查命令，或工具未安装 / 当前平台不支持（`shell`、`rust`、`java` 在 Windows 上默认跳过） | 安装对应工具，或在 `language_syntax_commands` / `--syntax-cmd` 给出一个本机可用、跨平台的命令；跳过不影响 `--apply`，只是少一层 L1 |
| 非 Python 报 `L1 syntax command failed` | 命令工具存在但真的检查出语法错误（或自定义命令写错） | 看 `prune-result.json` 的 stderr 摘要；检查命令模板与 `{file}` 占位符 |
| 规则没命中 | 被配置禁用 / 抑制标记 / 排除目录 | `rcp.py config` 查生效配置，检查 `per_file[].exclusions` |
| Windows 双击 `rcp.bat` 闪退 | 无 Python 或路径含空格 | 在终端里运行看报错；路径加引号 |

## 7. 底层脚本直调（高级）

统一入口只是门面，四个脚本均可独立运行：

```bash
python <RCP>/scripts/scan_redundancy.py src --out report.json --md report.md --config rcp.config.json
python <RCP>/scripts/render_report.py report.json --out report.md
python <RCP>/scripts/apply_prune.py report.json --apply --tests "pytest -q" --config rcp.config.json
python <RCP>/scripts/selftest.py
```

字段规范见 `report-schema.md`；规则与误报防护见 `redundancy-catalog.md`；验证协议与已知局限见 `lossless-guarantee.md`。

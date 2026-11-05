# 冗余规则目录

共 12 条规则。`static` / `verified` 会进入精简流程；`review` **只显示、永不自动应用**。

## 1. STATIC — 静态可证明语义恒等

| rule_id | 检测对象 | 无损性论证 | 关键防护 |
|---|---|---|---|
| `PY_NOOP_LITERAL_STMT` | 非 docstring 的裸字面量 `Expr(ast.Constant)` | 求值常量无副作用；删除后剩余语句仍构成合法 suite | 跳过 Module/Function/Class 首语句（docstring）；跳过块内唯一语句；跳过带行内注释的行 |
| `PY_REDUNDANT_PASS` | 非空语句块中的 `pass` | `pass` 是空操作，块内还有其它语句时删除不改变语法 | 跳过块内唯一语句（否则产生空 suite）；跳过带注释的 `pass` |
| `PY_UNREACHABLE_AFTER_TERMINATOR` | 同一语句块内 `return` / `raise` / `break` / `continue` 之后的语句 | 控制流无条件终止，后续语句永不可达 | 只检查**直接**终止语句，不穿透 `if`/`try`（保守）；含 `global`/`nonlocal` 时整块跳过；相邻注释/空行并入区间；嵌套区间只保留最外层 |
| `PY_SELF_ASSIGN` | 函数局部 `x = x`（简单 Name） | 自赋值不改变绑定值，右侧为纯 Name Load | 仅函数作用域（类体/模块体赋值对 `__dict__` 可观测）；跳过 `_` 前缀；跳过带注释行 |
| `PY_UNUSED_EXCEPT_BINDING` | `except E as e:` 中 `e` 全文件无引用 | Python 在 handler 结束时本就删除该绑定；移除不改变作用域或异常传播 | 检查任意 ctx 的 Name（含 `del e`）；多行 except 头不处理；`replace_lines` 只删 ` as e` |

## 2. VERIFIED — 需行为验证

| rule_id | 检测对象 | 残余风险 | 防护 |
|---|---|---|---|
| `PY_UNUSED_IMPORT` | `import` / `from ... import` 中绑定名无 Load 引用且不在 `__all__` | **导入模块本身可能有顶层副作用**（注册插件、改全局状态） | 见下「导入降级条件」 |

两种 edit：
- 整条语句所有别名都未用 → `delete_lines`。
- 部分别名未用（如 `import sys, json`）→ `replace_lines` 重建为 `import json`，保留 `as` 与相对导入点号。

**导入降级为 `review` 的条件**（任一命中即不自动应用）：
1. 文件调用 `get_type_hints`，或注解里出现字符串字面量 → 导入可能被运行时动态解析。仅有 `from __future__ import annotations` **不触发**：注解表达式在 AST 中仍是 `ast.Name`，已被引用计数收集。
2. 文件使用 `eval` / `exec` / `compile` / `globals` / `locals` / `__import__`。
3. 文件是 `__init__.py` → 导入常用于包级再导出。
4. 导入位于 `try/except ImportError` 或 `except ModuleNotFoundError` 守卫内。
5. 导入行含行内注释 → 删除会丢信息（信息无损约束）。

**完全跳过的导入**：`from __future__ import ...`（有语义）、`from x import *`（无法静态判定）、带 `# rcp:keep` / `# noqa` 的导入。

## 3. REVIEW — 只显示

| rule_id | 检测对象 | 拦截原因 |
|---|---|---|
| `PY_UNUSED_LOCAL` | 函数内赋值但全文件无 Load 引用的变量 | 右值可能有副作用，或用于调试 / 延长对象生命周期 |
| `PY_UNUSED_DEF` | 顶层无引用、无装饰器、非 dunder、非入口名的函数/类 | 跨模块调用、反射、插件注册、框架回调无法由单文件静态分析排除 |
| `PY_CONSTANT_FALSE_BRANCH` | `if False:` / `if 0:` 等 | 常是有意保留的特性开关 / 调试开关 |
| `PY_REDUNDANT_ELSE_AFTER_RETURN` | `if` 分支以 return/raise 结尾且有 `else` | 去嵌套是结构性改写，改变 AST 与行号，不属于无损删除 |
| `PY_DUPLICATE_BLOCK` | ≥3 条 AST 完全相同的连续语句序列 | 消除重复需新增函数/参数，属于重构 |
| `PY_COMMENTED_OUT_CODE` | 连续注释行去注释后可解析为合法 Python 语句 | 信息无损：注释可能承载待办 / 说明 / 历史决策 |

`PY_UNUSED_DEF` 白名单入口名：`main`, `run`, `app`, `cli`, `handler`, `lambda_handler`, `setup`, `teardown`, `setUp*`, `tearDown*`, `fixture`, `create_app`, `get_app`, `application`, `wsgi_app`, `asgi_app`, `cli_main` 等。

## 4. 刻意不检测的内容

| 不检测 | 原因 |
|---|---|
| 行尾空格 / 多余空行 | 三引号字符串内部空白是语义的一部分，易误伤；交给格式化器 |
| 常量折叠、`x == True` 改写 | 对 numpy / 自定义 `__eq__` 不等价 |
| 字符串拼接优化、循环改写 | 属于性能重构，不是删除 |
| 未使用的函数参数 | 覆盖、接口契约、回调签名可能要求保留 |
| 缩进 / 换行 / 命名风格 | 「信息无损」要求代码保持可读，禁止格式化式改写 |
| `*.pyi`、生成代码、vendor | 语义假设不成立 |

## 5. 误报抑制

源码内联标记（不区分大小写）可让扫描器跳过该行：

```python
import legacy_plugin  # rcp:keep   # 有意保留的副作用导入
import os  # noqa: F401
```

标记列表来自 `config/defaults.json` 的 `scan.suppress_markers`（默认 `rcp:keep` / `noqa` / `pragma: keep`），对所有规则生效。

**跨模块误报防护**：公共模块用 `__all__` 声明导出名，`PY_UNUSED_IMPORT` 与 `PY_UNUSED_DEF` 会据此跳过。本 skill 的 `rcp_common.py` 即以此保证「扫描自身源码 0 误报」。

**配置化抑制**：`rcp.config.json` 的 `rule_overrides` 支持 `"<RULE_ID>": false`（禁用）或 `"<RULE_ID>": "static|verified|review"`（强制档位）。禁用计数写入 `per_file[].exclusions.rule_disabled_by_config`。

全局排除目录、源码扩展名、include/exclude glob、大小上限等**唯一默认值来源**是 `config/defaults.json`，本文件不重复列举；用 `rcp.py config` 查看生效值。命令行补充排除：`--exclude '*_test.py'`、`--max-bytes`、`--max-files`。

## 6. 非 Python 的推荐规则命名

`scan_redundancy.py` 不做其它语言的 AST 分析，由 Agent 按 `report-schema.md` 补写：

| 建议 rule_id | 检测对象 | 建议 safety |
|---|---|---|
| `JS_UNUSED_IMPORT` / `TS_UNUSED_IMPORT` | 无引用的 import | `verified`（模块副作用同 Python） |
| `JS_UNUSED_CONST` / `GO_UNUSED_VAR` | 无引用局部绑定 | `static`（右值为字面量时）/ `review` |
| `ANY_UNREACHABLE_AFTER_RETURN` | return 后语句 | `static` |
| `ANY_COMMENTED_OUT_CODE` | 注释掉的旧代码 | `review` |
| `CSS_UNUSED_SELECTOR` / `ANY_DEAD_EXPORT` | 跨文件可达性 | `review`（需全仓证据） |

非 Python 的 L1 由 `config/defaults.json` 的 `extension_language` + `language_syntax_commands` **自动匹配**，也可用 `--syntax-cmd` 显式覆盖。命令模板里 `{file}` 会替换为临时文件路径；输出已被捕获，无需重定向：
```
--syntax-cmd "node --check {file}"
--syntax-cmd "gofmt -e {file}"
```
工具未安装或当前平台不支持该命令时自动跳过 L1，在 `warnings` 中明示，只剩 L0 + L2，**不阻断应用**；只有命令可用却真的失败才拒绝整批。配置与排查见 `quickstart.md` §3 / §6。

## 7. 学习闭环

误报或新模式的 `Pattern-Key` 统一用 `redundancy.<rule_id>`。例：

```
## [LRN-20261105-001] correction
**Priority**: medium
**Area**: backend
**Pattern-Key**: redundancy.PY_UNUSED_IMPORT
**Source**: simplify-and-harden
**Recurrence-Count**: 1
### Summary
__init__.py 的 from . import X 被判为未使用导入
### Suggested Action
保持 REVIEW；全局规则写入 AGENTS.md
```

复发 ≥3 次且跨 ≥2 个任务时，把抑制规则提升到 `AGENTS.md` / `CLAUDE.md`。

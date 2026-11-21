# Lean C

[English](README.md) · **简体中文** · [繁體中文](README.zh-Hant.md)

规格优先、由独立验证器裁决的 AI C17 代码生成（V2 另支持 C++20、Frama-C/WP、Lean 4）。研究原型。

代码由 AI 编写，但是否正确由独立工具判断：Clang、Clang Static Analyzer、ASan/UBSan/LSan、CBMC，以及可选的 Frama-C/WP 与 Lean 4。它们依据的是经人工批准并哈希锁定的规格。

```
需求 -> 规格草案（不受信任）-> 人工批准（哈希锁定、HMAC 签名）
  -> 代码生成 -> 编译 -> 完整性关卡 -> 静态分析 -> sanitizer 测试
  -> CBMC ->（V2）Frama-C/WP ->（V2，可选）Lean 4
  -> 反例 -> AI 修复（策略检查，最多 N 轮）-> 重新验证 -> JSON / Markdown 报告
```

目录：[安装](#安装) · [快速开始](#快速开始) · [概念](#概念) · [命令](#命令) · [状态与退出码](#状态与退出码) · [机器接口（AI agent / CI）](#机器接口ai-agent--ci) · [LLM provider](#llm-provider) · [配置](#配置) · [V2 工具](#v2-可选工具) · [示例](#示例) · [开发 Lean C](#开发-lean-c) · [限制](#限制)

---

## 安装

需要 Linux 或 WSL2（以下命令以 Ubuntu 24.04 为例），以及 Python 3.11 以上。

```bash
sudo apt-get install -y clang-18 libclang-rt-18-dev cbmc bubblewrap
pip install -e ".[dev]"          # 运行依赖：pydantic、PyYAML；开发：pytest
leanc doctor                     # 实测 clang、分析器、sanitizers、CBMC、bwrap 沙箱与网络隔离、LLM 配置
```

`leanc doctor` 会实际运行每个工具，并为每项失败的检查给出修复方法。加上 `--json` 可获得机器可读输出（`{"ok": bool, "checks": [{name, ok, required, detail, fix}]}`）。如果 bubblewrap 沙箱不可用，sanitizer 测试会被跳过，整体状态最多只能是 `UNKNOWN`（见 [安全性](docs/security.md)）。

## 快速开始

以下全部离线运行，不需要 API key。

```bash
leanc init demo                                   # 模板：spec.yaml、include/、harness/、tests/、src/、leanc.yaml
leanc verify demo/spec.yaml                       # SPEC_UNAPPROVED（exit 40）：没有已批准的规格就不声称任何结果
# 由人审查 demo/spec.yaml、demo/harness/、demo/tests/、demo/include/ 之后：
leanc spec approve demo/spec.yaml --by alice      # 交互确认；加 --yes 可跳过
leanc verify demo/spec.yaml                       # VERIFIED_BOUNDED（exit 0）
leanc report latest --project-root demo           # Markdown；--format json 输出 JSON
```

用一个附带缺陷的示例跑完整流程（生成 → 验证 → 反例 → 修复 → 再验证）：

```bash
cp -r examples/div_by_zero /tmp/dz
leanc spec approve /tmp/dz/spec.yaml --by alice --yes
leanc run /tmp/dz/spec.yaml --provider mock --regenerate
#   initial         COUNTEREXAMPLE_FOUND  (den == 0; INT_MIN / -1)
#   repair round 1: COUNTEREXAMPLE_FOUND  accepted (improvement; continuing)
#   repair round 2: VERIFIED_BOUNDED      accepted (verified)
#   STATUS          VERIFIED_BOUNDED
```

`--provider mock` 会重放示例 `mock/` 目录中的固定 fixture。它是确定性的，并不代表 LLM 的表现；报告中会标明这是 mock 输出。

## 概念

| 术语 | 含义 |
|---|---|
| 项目根目录 | 最近一个含 `leanc.yaml` 的上级目录，否则为规格文件所在目录；可用 `--project-root` 覆盖 |
| 规格（`spec.yaml`） | 可信契约：目标签名、契约、源文件、编译/分析/测试/CBMC 设置、必需 `gates`。格式见 [docs/spec-format.md](docs/spec-format.md) |
| 受保护文件 | 规格、`sources.headers`、`verification.harness`、`tests.driver`，以及 V2 的 ACSL 头文件、Lean spec/vectors。生成与修复永远不会写入这些文件 |
| 可编辑文件 | `sources.editable`：生成与修复唯一能写入的文件 |
| 批准 | `leanc spec approve` 把规格的语义哈希与每个受保护文件的 SHA-256 绑定进一条记录，并用 `LEANC_HOME`（默认 `~/.config/leanc`）中的密钥签名。任何改动都会导致 `SPEC_UNAPPROVED`，直到重新批准为止，而且 `version` 必须递增。回滚账本会拒绝已被取代的批准 |
| 规格草案 | `leanc spec draft` 写入 `drafts/<id>/`，并标记为 `provenance: ai-draft`。在人工审查并批准之前，它永远不受信任 |
| 关卡（gate） | 一个验证阶段：`compile`、`integrity`（强制，每次都运行）、`static_analysis`、`sanitizer`、`cbmc`，V2 另有 `wp`、`lean`。必须每个必需关卡都运行且通过，才算成功 |
| 运行（run） | 每次 `generate` / `verify` / `repair` / `run` 都会创建 `.leanc/runs/<run_id>/` |

给所有 agent 的规则：不得修改受保护文件，也不得代替人执行 `spec approve`。批准就是信任边界。

## 命令

| 命令 | 作用 | 创建 run |
|---|---|---|
| `leanc doctor` | 检查工具链、沙箱与 LLM 配置 | 否 |
| `leanc init [DIR] [--template safe-array-get] [--force]` | 由模板创建项目 | 否 |
| `leanc spec draft --id ID (--requirement TEXT \| --requirement-file F)` | 需求 → `drafts/<id>/` 中不受信任的草案（exit 40） | 否 |
| `leanc spec approve SPEC --by NAME [--note N] [--yes]` | 人工批准（哈希锁定 + 签名） | 否 |
| `leanc spec status SPEC` | 批准状态、签名有效性、批准后的改动 | 否 |
| `leanc spec show SPEC` | 规范化规格、哈希、生效的 CBMC 检查 | 否 |
| `leanc generate SPEC [--force]` | 为已批准的规格写入 `sources.editable`（exit 0 表示已生成，**尚未**验证） | 是 |
| `leanc verify SPEC` | 对当前源代码运行各验证关卡 | 是 |
| `leanc repair SPEC [--max-rounds N] [--no-write]` | 先验证，再进行反例驱动的修复循环 | 是 |
| `leanc run SPEC [--regenerate] [--no-repair] [--max-rounds N] [--no-write]` | 规格检查 → 生成（若不存在）→ 验证 → 修复 → 再验证 → 报告 | 是 |
| `leanc report [RUN\|latest] [--format md\|json]` | 显示某次运行的报告；以该次运行的状态作为退出码；若成功的运行之后文件被修改，显示 STALE 并返回 20 | 否 |

通用选项：

| 选项 | 适用命令 | 作用 |
|---|---|---|
| `--json` | `report` 以外全部 | stdout 只输出单个 JSON 对象，日志写到 stderr |
| `-v` | `report` 以外全部 | 详细日志写到 stderr |
| `--project-root DIR` | spec、generate、verify、repair、run、report | 覆盖项目根目录 |
| `--gates a,b` | verify、repair、run | 只运行规格关卡的子集（结果最多 `UNKNOWN`） |
| `--fail-fast` | verify、repair、run | 第一个未通过的关卡之后就停止 |
| `--timeout-scale X` | verify、repair、run | 所有工具超时乘以 X |
| `--no-sandbox` | verify、repair、run | 不使用 bwrap；动态测试会 `SKIPPED`，结果最多 `UNKNOWN` |
| `--allow-unsandboxed` | verify、repair、run | 危险：在没有沙箱的情况下运行不可信的测试程序（只有这个标志能这样做，`leanc.yaml` 不行） |
| `--check-unapproved` | verify、repair、run | 对未批准的规格运行关卡，结果仅供参考；状态仍是 `SPEC_UNAPPROVED` |
| `--provider mock\|openai`、`--mock-dir D`、`--llm-timeout S` | spec draft、generate、repair、run | 选择 LLM |

## 状态与退出码

| exit | 状态 | 含义 |
|---|---|---|
| 0 | `PROVED` | 必需的 `wp` 关卡证明了已批准的 ACSL 契约与所有 RTE 守卫（smoke test 无异常）。这是 `PROVED` 唯一的来源 |
| 0 | `VERIFIED_BOUNDED` | 在已批准的 harness、输入界限与 unwind 界限内没有违反；所有循环都已完全展开；harness 不是 vacuous。**永远不会升级为 `PROVED`** |
| 10 | `COMPILE_ERROR` | 编译错误（或在 `warnings_policy: block` 下出现警告） |
| 11 | `STATIC_ANALYSIS_WARNING` | 在 `policy: block` 下有分析器发现，或违反完整性关卡 |
| 12 | `RUNTIME_TEST_FAILED` | sanitizer 报告、退出码或输出错误，或 Lean 测试向量不符 |
| 13 | `COUNTEREXAMPLE_FOUND` | CBMC 找到具体反例（真实的执行路径，即使 unwind 不足也成立） |
| 20 | `UNKNOWN` | 无法下结论：unwind 不足、harness vacuous、关卡被跳过、只跑关卡子集、没有形式化关卡（C++）、目标未证明 |
| 21 | `TIMEOUT` | 必需工具超时 |
| 30 | `TOOL_ERROR` | 工具崩溃或输出无法解析 |
| 40 | `SPEC_UNAPPROVED` | 当前的规格与受保护文件没有有效批准 |
| 50 | `REPAIR_EXHAUSTED` | 修复循环结束但没有成功；工作区已还原 |
| 2 | — | 用法或输入错误 |
| 70 | — | 内部错误（traceback 写到 stderr） |

“没找到反例”不等于成功。超时、工具错误、无法解析的结果、被跳过的关卡，都不算成功。汇总规则见 [docs/status-semantics.md](docs/status-semantics.md)。

## 机器接口（AI agent / CI）

使用 `--json`，并按退出码或 `status` 分支。`leanc verify SPEC --json` 的输出如下：

```json
{
  "command": "verify", "status": "COUNTEREXAMPLE_FOUND", "exit_code": 13,
  "run_id": "verify-20261121T034210Z-7c8afd", "run_dir": ".../.leanc/runs/verify-...",
  "provider": null, "report": ".../report.md", "messages": ["..."],
  "verification": {
    "status": "...", "success": false, "spec_id": "...", "spec_version": 1, "spec_hash": "...",
    "approval": {"...": "..."},
    "stages": [{"stage": "cbmc", "outcome": "FAIL", "status": "...", "required": true, "invoked": true,
                "duration_s": 1.2, "summary": "...", "tools": [], "failures": []}],
    "detected_by": ["asan", "cbmc"], "reasons": ["..."], "assumptions": ["..."], "limitations": ["..."],
    "failures": [{
      "id": "sanitizer:index_equals_count:asan:heap-buffer-overflow", "tool": "asan", "stage": "sanitizer",
      "kind": "...", "property_id": "...", "location": {"file": "src/array_lookup.c", "line": 9, "column": 16, "function": "array_lookup"},
      "inputs": [{"name": "argv", "value": "index_equals_count", "type": null}],
      "description": "...", "raw_ref": "raw/..."
    }]
  }
}
```

运行目录结构：

```
.leanc/runs/<run_id>/
  run.json  report.md  leanc.log
  verify/                         （修复轮次：repair/round-N/verify/）
    verification.json  verification.md
    snapshot/   实际被验证的源代码
    raw/        工具原始输出：cbmc-verify.json、compile/、integrity/、static_analysis/（SARIF）、sanitizer/、wp/、lean/
    logs/       每次工具调用的 stdout / stderr，按执行顺序编号
    build/
```

agent 修正代码有两种方式：

1. **外部 agent 循环。** 运行 `leanc verify SPEC --json`，读取 `verification.failures`（位置、输入、性质）与 `raw_ref`。只修改 `sources.editable` 列出的文件，然后再验证。遇到 exit 0，或 40（需要人处理）、20/21/30（不是代码缺陷，请看 `reasons`）时停止。
2. **内置循环。** 运行 `leanc run SPEC --provider openai --json`（或 `repair`）。Lean C 会调用模型、对每个补丁做策略检查，并且只把验证通过的候选写回（`--no-write` 让工作区保持不变）。

修复策略会拒绝以下补丁：写入 `sources.editable` 以外的文件、加入 `__CPROVER_*` / `LEANC_*` / `__builtin_assume`、删除 `assert`、抑制警告或 sanitizer、`#include` 逃逸、`system`/`exec`/`fork`/`socket`、inline asm、把目标函数掏空，以及过大的补丁。重复的补丁会被判定为循环。结果变成 `UNKNOWN`/`TIMEOUT` 不算改善。

## LLM provider

**mock**（`leanc.yaml` 没有设置 provider 时的默认值）。它从 `<project>/mock/` 读取 fixture（可用 `--mock-dir` 覆盖）：

```
mock/
  spec_draft.yaml、harness_draft.c、tests_draft.c    `spec draft` 返回的内容
  generate/<可编辑文件路径>        例如 generate/src/safe_ratio.c
  repair/round-<N>.json            {"root_cause": "...", "rationale": "...", "files": {"src/x.c": "<完整内容>"}}
                                   或以 "patch": "<unified diff>" 取代 "files"；缺少某轮 = 没有提案
```

**openai**（任何 OpenAI 兼容的 chat-completions 端点）：

| 变量 | 含义 |
|---|---|
| `LEANC_API_KEY`（或 `OPENAI_API_KEY`） | API key。日志、报告与 transcript 中都会遮蔽，也不会传给子进程。若用 `LEANC_BASE_URL` 指向本地服务器，可以留空 |
| `LEANC_MODEL` | 模型名称（必填） |
| `LEANC_BASE_URL` | 默认 `https://api.openai.com/v1` |
| `LEANC_LLM_TIMEOUT` | 请求超时秒数 |
| `LEANC_LLM_MAX_RETRIES` | 暂时性错误的重试次数 |
| `LEANC_LLM_MAX_TOKENS` | 最大生成 token 数（看到 `finish_reason=length` 时请调高） |
| `LEANC_LLM_TEMPERATURE` | 数字或 `default` |
| `LEANC_LLM_JSON_MODE=1` | 发送 `response_format: json_object` |
| `LEANC_LLM_PROXY` | `env`（使用 `HTTPS_PROXY`）或 `none` |

```bash
export LEANC_API_KEY=... LEANC_MODEL=<model>
leanc spec draft --id my-func --requirement "实现一个……" --provider openai   # 不受信任的草案 -> 审查 -> 批准
leanc run path/to/spec.yaml --provider openai --json
```

prompt 位于 `leanc/llm/prompts/`。评估模型时，请自行记录模型、温度与 prompt 版本。

## 配置

项目根目录可放一个可选的 `leanc.yaml`：

```yaml
provider: mock        # 默认 provider：mock | openai
mock_dir: mock
sandbox: auto         # auto | none
```

项目配置不能削弱检查、界限、关卡、批准或隔离。若设置了 `allow_unsandboxed`，该值会被忽略并发出警告。

| 变量 | 含义 |
|---|---|
| `LEANC_HOME` | 批准密钥与账本目录（默认 `~/.config/leanc`）；请放在工作区之外 |
| `LEANC_CLANG`、`LEANC_CLANGXX`、`LEANC_CBMC` | 工具路径 |
| `LEANC_FRAMAC` | `frama-c`（>= 27，含 WP）；未设置时搜索 `PATH` 与 `/opt/opam/*/bin` |
| `LEANC_LEAN` | `lean`（含 `leanchecker` 的 toolchain）；未设置时搜索 `PATH` 与 `/opt/lean/*/bin` |

## V2 可选工具

| 功能 | 安装 | 规格 | 对状态的影响 |
|---|---|---|---|
| Frama-C/WP + ACSL | `sudo apt-get install -y opam z3 cvc5 && opam init -y && opam install -y frama-c alt-ergo`（Ubuntu 的 `frama-c-base` 不含 WP） | ACSL 契约写在受保护头文件中，关卡 `wp` | `PROVED` 唯一的途径；证明修复让 AI 补上 loop invariant |
| Lean 4 | release toolchain 解压到 `/opt/lean/<版本>`，或使用 elan | 受保护的模型与定理陈述；证明由 AI 编写；kernel + `leanchecker`；只接受标准公理；差分测试向量 | 模型层级：未证明 → `UNKNOWN`，向量不符 → `RUNTIME_TEST_FAILED`；永远不会产生 `PROVED` |
| C++20 | `clang++`（clang-18 自带） | `language: {name: cpp, standard: c++20}` | 没有可用的 C++ model checker，因此最佳结果是 `UNKNOWN` |

`leanc doctor` 会报告上述每个工具。缺少某个工具时，相关测试会被跳过，而不是失败。

## 示例

每个 `examples/<name>/` 都包含 `spec.yaml`、受保护的 `include/`、`harness/`、`tests/`，附带的（通常有缺陷的）`src/`，`mock/` fixture，以及 `expected.yaml`。

| 示例 | 附带的代码 | `verify` | `run --provider mock` |
|---|---|---|---|
| `safe_array_get` | 正确 | `VERIFIED_BOUNDED` | `VERIFIED_BOUNDED` |
| `array_oob` | `index <= count` | `COUNTEREXAMPLE_FOUND`（cbmc、asan） | 1 轮 → `VERIFIED_BOUNDED` |
| `div_by_zero` | 未检查 `den` | `COUNTEREXAMPLE_FOUND`（cbmc、ubsan） | 2 轮 → `VERIFIED_BOUNDED` |
| `int_overflow` | 先加再检查 | `COUNTEREXAMPLE_FOUND` | 1 轮 → `VERIFIED_BOUNDED` |
| `null_deref` | 检查了 NULL 但没有返回 | `COUNTEREXAMPLE_FOUND`（analyzer、cbmc、ubsan） | 1 轮 → `VERIFIED_BOUNDED` |
| `loop_unwind` | 正确，但 unwind 不足 | `UNKNOWN` | `UNKNOWN`（不会被“修复”）；变体 `spec_full_unwind.yaml` → `VERIFIED_BOUNDED` |
| `vacuous_harness` | assume 互相矛盾 | `UNKNOWN`（单用 CBMC 会说 SUCCESSFUL） | `UNKNOWN` |
| `runtime_memory` | 错误路径泄漏 | `RUNTIME_TEST_FAILED`（lsan） | 1 轮 → `VERIFIED_BOUNDED` |
| `ring_buffer` | 两个 `.c`，正确 | `VERIFIED_BOUNDED` | `VERIFIED_BOUNDED` |
| `cheating_repair` | off-by-one | `COUNTEREXAMPLE_FOUND` | 第 1–4 轮被策略拒绝，第 5 轮 → `VERIFIED_BOUNDED` |
| `repair_exhausted` | `(a+b)/2` 溢出 | `COUNTEREXAMPLE_FOUND` | `REPAIR_EXHAUSTED`，工作区不变 |
| `cbmc_timeout` | 困难的 64 位等价性 | `TIMEOUT` | `TIMEOUT` |
| `wp_safe_array_get` | ACSL 契约，正确 | `PROVED` | `PROVED` |
| `wp_proof_repair` | 正确，但没有 loop invariant | `UNKNOWN` | 2 轮证明修复 → `PROVED` |
| `wp_contract_violation` | 违反后置条件 | `COUNTEREXAMPLE_FOUND` | 1 轮 → `PROVED` |
| `lean_clamp` | Lean 定理留为 `sorry` | `UNKNOWN` | 2 轮 → `VERIFIED_BOUNDED` |
| `cpp_ring_buffer` | C++20 class template | `UNKNOWN` | `UNKNOWN` |
| `cpp_bounds_bug` | C++ off-by-one | `RUNTIME_TEST_FAILED`（asan） | 1 轮 → `UNKNOWN` |

全部复现：

```bash
python scripts/run_experiments.py                       # 全部示例；应报告 N/N 符合
python scripts/run_experiments.py --only array_oob div_by_zero
```

脚本会把每个示例复制到临时项目，并用临时的 `LEANC_HOME` 批准。接着通过真正的 CLI 运行 `verify` 与 `run --provider mock --regenerate`，再与 `expected.yaml` 比对结果。输出写到 `experiments/results/`（本地生成，已加入 gitignore）。详见 [docs/experiments.md](docs/experiments.md)。

## 开发 Lean C

规则声明在 [`constraints.toml`](constraints.toml) 中。它的作用相当于 yarn 项目里的 constraints 文件，是唯一的规则来源。`scripts/constraints.py` 检查这些规则，`tests/test_architecture.py` 运行的是同一个引擎。agent 指令写在 [`CLAUDE.md`](CLAUDE.md) 中，其中的分层区块由 `constraints.toml` 自动生成。

- 严格的单向分层、无环的模块图，且只允许顶层 import：
  ```
  foundation < specs, lang, reports < rules, toolchain, llm < backends, repair < pipeline < cli < __main__
  ```
  共享代码往下层移。包的 `__init__` 只能 re-export 自己的子模块。
- `leanc/`、`tests/`、`scripts/` 中不得有注释或 docstring。意图要用名称、常量、Enum 与 frozen dataclass 表达。
- 不得有 bare/`Exception`/`BaseException` handler（唯一的例外是 `leanc/cli.py` 的 `main()`，它以 exit 70 结束），也不得吞掉异常。
- 每个 `.py` 文件最多 600 行。

| 包 | 职责 |
|---|---|
| `foundation` | 状态、数据模型、错误、哈希、凭证遮蔽、工作区（路径/符号链接安全）、沙箱、`run_command`（只接受 argv、资源限制） |
| `specs` | 规格 schema、加载、哈希、批准与账本 |
| `lang` | C / C++ / Lean lexer |
| `rules` | 关卡与修复策略共用的纯函数信任规则（C、C++、ACSL、Lean、预处理器） |
| `toolchain` | 工具探测、`BackendContext` |
| `reports` | JSON / Markdown 输出 |
| `llm` | provider 接口、mock、OpenAI 兼容、prompts、响应解析 |
| `backends` | 各关卡：clang、analyzer、integrity、sanitizer、cbmc、counterexample、wp、lean |
| `repair` | 应用补丁（fuzz 0）、生成与修复策略 |
| `pipeline` | 验证控制器、生成、修复循环、doctor、配置、命令实现 |
| `cli.py` | 只有 argparse 与 `main` |

检查：

```bash
python scripts/constraints.py                       # 检查 constraints.toml 的全部规则；有违规时 exit 1；支持 --json
python scripts/constraints.py --fix                 # 仅在 AST 不变时删除注释/docstring；重新生成 CLAUDE.md 的分层区块
python scripts/affected_tests.py --run              # 修改后：按 import 图相对于 HEAD 选出受影响的测试
python scripts/affected_tests.py --base main --run  # 整个分支
python scripts/merge_gate.py                        # 创建或合并 PR 之前运行（约 20 分钟）
```

`merge_gate.py` 在即将被合并的那个 HEAD 上强制执行“修复 → 测试 → 合并”。它按以下顺序执行，第一个失败就停止，没有跳过选项：

1. 获取基准分支，要求工作区干净且包含基准分支。
2. 运行 `constraints.py`。
3. 运行完整测试。
4. 运行 `run_experiments.py`，必须报告 N/N。
5. 检查 `spec_hash` 稳定性：自基准分支以来未改动的规格文件，用基准代码和 HEAD 代码算出的哈希必须相同，这样既有的批准才不会失效。
6. 确认 HEAD 没有移动。

成功时输出 `MERGE GATE PASS <sha>`。只合并这个 sha（使用 `expectedHeadSha` 和 merge commit）。之后有任何新 commit，都必须重新运行闸门。

完整测试需要 clang-18、CBMC 与 bwrap；WP 测试需要 Frama-C >= 27，Lean 测试需要 Lean 4。缺少某个工具时，相关测试会被跳过。

## 限制

- `VERIFIED_BOUNDED` 受限于已批准的 harness；harness 是否忠实表达需求，仍需由人判断。
- sanitizer 只覆盖可信测试实际执行到的路径。
- `PROVED` 只相对于已批准的 ACSL 契约成立，且以 WP 的内存与机器模型为前提。
- 修复策略是纵深防御（文本规则）；真正的防线是受保护的 harness 加上独立验证器。
- C++20 没有形式化关卡（CBMC 无法解析现代 C++，ESBMC 尚未集成），因此 C++ 的最佳结果是 `UNKNOWN`。
- Lean 证明的是模型，它与 C 代码之间只通过差分测试向量连接。
- bubblewrap 不是虚拟机；高风险工作请在独立的容器或 VM 中运行。

更多文档（繁体中文）：[架构](docs/architecture.md) · [规格格式](docs/spec-format.md) · [状态语义](docs/status-semantics.md) · [信任模型](docs/trust-model.md) · [安全性](docs/security.md) · [实验](docs/experiments.md) · [Roadmap](docs/roadmap.md)

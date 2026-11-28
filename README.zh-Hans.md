# Lean C

[English](README.md) · **简体中文** · [繁體中文](README.zh-Hant.md)

规格优先、由独立验证器裁决的 AI C17 代码生成（V2 另支持 C++20、Frama-C/WP、Lean 4）。研究原型。

代码由 AI 编写，但是否正确由独立工具判断：Clang、Clang Static Analyzer、ASan/UBSan/LSan、CBMC，以及可选的 CBMC 循环契约（opt-in 的 inductive profile）、Frama-C/WP 与 Lean 4。它们依据的是经人工批准并哈希锁定的规格。

```
需求 -> 规格草案（不受信任）-> 人工批准（哈希锁定、HMAC 签名）
  -> 代码生成 -> 编译 -> 完整性关卡 -> 静态分析 -> sanitizer 测试
  -> CBMC ->（opt-in）inductive CBMC 循环契约 ->（V2）Frama-C/WP ->（V2，可选）Lean 4
  -> 反例 -> AI 修复（策略检查，最多 N 轮）-> 重新验证 -> JSON / Markdown 报告
```

目录：[安装](#安装) · [快速开始](#快速开始) · [概念](#概念) · [命令](#命令) · [状态与退出码](#状态与退出码) · [关卡规则](#关卡规则) · [机器接口（AI agent / CI）](#机器接口ai-agent--ci) · [LLM provider](#llm-provider) · [配置](#配置) · [V2 工具](#v2-可选工具) · [示例](#示例) · [开发 Lean C](#开发-lean-c) · [限制](#限制)

---

## 安装

需要 Linux 或 WSL2（以下命令以 Ubuntu 24.04 为例），以及 Python 3.11 以上。

```bash
sudo apt-get install -y clang-18 libclang-rt-18-dev llvm-18 cbmc gcc bubblewrap
pip install -e ".[dev]"          # 运行依赖：pydantic、PyYAML；开发：pytest
leanc doctor                     # 实测 clang、分析器、sanitizers、gcc、CBMC、goto-cc、goto-instrument、bwrap 沙箱与网络隔离、LLM 配置
```

`leanc doctor` 会实际运行每个工具，并为每项失败的检查给出修复方法；有必需的检查失败时以 exit 30 结束。加上 `--json` 可获得机器可读输出（`{"ok": bool, "checks": [{name, ok, required, detail, fix}]}`）。如果 bubblewrap 沙箱不可用，sanitizer 测试会被跳过，整体状态最多只能是 `UNKNOWN`（见 [安全性](docs/security.md)）。

Ubuntu 24.04 的 `cbmc` 软件包是 CBMC 5.95.1，并同时安装 `goto-cc` 与 `goto-instrument`；只有 opt-in 的 `inductive` 关卡用到它们。doctor 中的 `goto-cc (inductive gate)`、`goto-instrument (inductive gate)` 与 `cbmc release pin (inductive gate)` 三行不是必需检查，因此永远不会造成 exit 30：它们报告这两个工具是否找得到，以及 `cbmc`、`goto-cc`、`goto-instrument` 是否都输出经过测试的版本标记 `(cbmc-5.95.1)`。否则 `inductive` 关卡为 `ERROR`。

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
| 项目根目录 | 最近一个含 `leanc.yaml` 的上级目录，否则为规格文件所在目录；可用 `--project-root` 覆盖。`spec draft` 没有规格文件，未给出 `--project-root` 时使用当前目录 |
| 规格（`spec.yaml`） | 可信契约：目标签名、契约、源文件、编译/分析/测试/CBMC 设置、必需 `gates`。格式见 [docs/spec-format.md](docs/spec-format.md) |
| 受保护文件 | 规格、`sources.headers`、`verification.harness`、`tests.driver`、`inductive.harness`，以及 V2 的 ACSL 头文件、Lean spec/vectors。生成与修复永远不会写入这些文件 |
| 可编辑文件 | `sources.editable`，规格有相应段落时另有 `lean.proofs` 与 `inductive.contracts`：修复唯一能写入的文件（`generate` 只写 `sources.editable`）。它们不计入批准的哈希 |
| 批准 | `leanc spec approve` 把规格的语义哈希与每个受保护文件的 SHA-256 绑定进一条记录，并用 `LEANC_HOME`（默认 `$XDG_CONFIG_HOME/leanc`，未设置 `XDG_CONFIG_HOME` 时为 `~/.config/leanc`）中的密钥签名。任何改动都会导致 `SPEC_UNAPPROVED`，直到重新批准为止，而且 `version` 必须递增。回滚账本会拒绝已被取代的批准 |
| 规格草案 | `leanc spec draft` 写入 `drafts/<id>/`，并标记为 `provenance: ai-draft`。在人工审查并批准之前，它永远不受信任 |
| 关卡（gate） | 一个验证阶段：`compile`、`integrity`（强制；在编译关卡通过后运行）、`static_analysis`、`sanitizer`、`cbmc`、opt-in 的 `inductive`，V2 另有 `wp`、`lean`。必须每个必需关卡都运行且通过，才算成功 |
| 运行（run） | 每次 `generate` / `verify` / `repair` / `run` 都会创建 `.leanc/runs/<run_id>/`。`spec draft` 会创建 `.leanc/runs/draft-<UTC 时间戳>-<hex>/`，用来存放它的 `leanc.log` 以及真实 provider 的 `llm/` transcript；除非命令中止（见 exit 2），其中不会写入 `run.json`，所以 `report latest` 会跳过它 |

给所有 agent 的规则：不得修改受保护文件，也不得代替人执行 `spec approve`。批准就是信任边界。

## 命令

| 命令 | 作用 | 创建 run |
|---|---|---|
| `leanc doctor [--no-sandbox]` | 检查工具链、沙箱与 LLM 配置（`--no-sandbox` 跳过沙箱检查） | 否 |
| `leanc init [DIR] [--template safe-array-get] [--force]` | 由模板创建项目 | 否 |
| `leanc spec draft --id ID (--requirement TEXT \| --requirement-file F) [--title T] [--force]` | 需求 → `drafts/<id>/` 中不受信任的草案（exit 40） | 仅日志目录 |
| `leanc spec approve SPEC --by NAME [--note N] [--yes]` | 人工批准（哈希锁定 + 签名） | 否 |
| `leanc spec status SPEC` | 批准状态、签名有效性、批准后的改动（未批准时 exit 40） | 否 |
| `leanc spec show SPEC` | 规范化规格、哈希、生效的 CBMC 检查 | 否 |
| `leanc generate SPEC [--force]` | 为已批准的规格写入 `sources.editable`（exit 0 表示已生成，**尚未**验证；记录的 `status` 是 `UNKNOWN`） | 是 |
| `leanc verify SPEC` | 对当前源代码运行各验证关卡 | 是 |
| `leanc repair SPEC [--max-rounds N] [--no-write]` | 先验证，再进行反例驱动的修复循环（`--max-rounds`：0 到 20） | 是 |
| `leanc run SPEC [--regenerate] [--no-repair] [--max-rounds N] [--no-write]` | 规格检查 → 生成（若不存在）→ 验证 → 修复 → 再验证 → 报告（`--max-rounds`：0 到 20） | 是 |
| `leanc report [RUN\|latest] [--format md\|json]` | 显示某次运行的报告；以该次运行的状态作为退出码；若成功的运行之后文件被修改，显示 STALE 并返回 20；状态为 `UNKNOWN`（已生成、尚未验证）的 `generate` 运行返回 0 | 否 |

通用选项：

| 选项 | 适用命令 | 作用 |
|---|---|---|
| `--json` | `report` 以外全部 | stdout 只输出单个 JSON 对象，日志写到 stderr |
| `-v` | `report` 以外全部 | 详细日志写到 stderr |
| `--project-root DIR` | spec、generate、verify、repair、run、report | 覆盖项目根目录 |
| `--gates a,b` | verify、repair、run | 只运行规格关卡的子集（结果最多 `UNKNOWN`） |
| `--fail-fast` | verify、repair、run | 第一个未通过的关卡之后就停止 |
| `--timeout-scale X` | verify、repair、run | 所有工具超时乘以 X（0.01 到 100） |
| `--no-sandbox` | verify、repair、run | 不使用 bwrap；动态测试会 `SKIPPED`，结果最多 `UNKNOWN` |
| `--allow-unsandboxed` | verify、repair、run | 危险：在没有沙箱的情况下运行不可信的测试程序（只有这个标志能这样做，`leanc.yaml` 不行） |
| `--check-unapproved` | verify、repair、run | 对未批准的规格运行关卡，结果仅供参考；状态仍是 `SPEC_UNAPPROVED` |
| `--provider mock\|openai`、`--mock-dir D`、`--llm-timeout S` | spec draft、generate、repair、run | 选择 LLM |

## 状态与退出码

| exit | 状态 | 含义 |
|---|---|---|
| 0 | `PROVED` | 必需的 `wp` 关卡证明了已批准的 ACSL 契约与所有 RTE 守卫（smoke test 无异常），且 WP 套用了每一项有语义影响的 `compile.flags` 以及构建的 `char` 符号性。这是 `PROVED` 唯一的来源 |
| 0 | `VERIFIED_INDUCTIVE` | opt-in：满足 `VERIFIED_BOUNDED` 的全部条件，并且必需的 `inductive` 关卡通过。CBMC 以 DFCC 循环契约证明：受保护的 inductive harness 中按原样编写的 `main()` 的每一次执行（每条路径最多调用一次 claimed 函数，即 `inductive.functions` 中的函数），都满足在该路径上执行到的 harness 断言，且只检查强制检查类别；每个带契约的循环都以归纳法证明（进入时与一步之后的 invariant、assigns、decreases）；非 const 的 static 对象从任意值开始，内存分配总是成功。harness 的 `LEANC_ASSUME` 只是被覆盖状态的上界（harness 的其他代码也会缩小它们，且不报告）；未被 claim 的函数与额外的 `verification.checks` 类别只有有界结果；已知缺口见[限制](#限制)。**永远不会升级为 `PROVED`** |
| 0 | `VERIFIED_BOUNDED` | 在已批准的 harness、输入界限与 unwind 界限内没有违反；所有循环都已完全展开；harness 不是 vacuous。**永远不会升级为 `PROVED`** |
| 10 | `COMPILE_ERROR` | 编译错误（或在 `warnings_policy: block` 下出现警告） |
| 11 | `STATIC_ANALYSIS_WARNING` | 在 `policy: block` 下有分析器发现，或违反完整性关卡 |
| 12 | `RUNTIME_TEST_FAILED` | sanitizer 报告、退出码或输出错误，或 Lean 测试向量不符 |
| 13 | `COUNTEREXAMPLE_FOUND` | CBMC 找到具体反例（真实的执行路径，即使 unwind 不足也成立），或 Frama-C/WP 的证明器判定某个证明目标不成立（该失败带有目标的位置，但没有输入或路径）。`inductive` 关卡永远不会报告反例 |
| 20 | `UNKNOWN` | 无法下结论：unwind 不足、harness vacuous、关卡被跳过、只跑关卡子集、没有形式化关卡（C++）、目标未证明、WP 证明所依据的 `compile.flags` 或构建目标的 `char` 符号性未被 Frama-C 套用、`inductive` 关卡为 `INCONCLUSIVE`（循环没有契约、循环契约被拒绝或未证明、性质未证明、探针不可达、函数未声明） |
| 21 | `TIMEOUT` | 必需工具超时 |
| 30 | `TOOL_ERROR` | 工具崩溃或输出无法解析，或 `inductive` 关卡为 `ERROR`（CBMC 工具不是经过测试的版本、违反 harness 规则或可信符号规则）；另外，已建立的 provider 在生成过程中出错或生成的代码被拒绝时（`generate`、`run`），`run` 无法为生成建立 provider 时，以及需要修复但无法建立 provider 时（`repair`、`run`；验证的判定保留在 `initial_status` / `verification.status`，原因在 `messages`），也是这个状态 |
| 40 | `SPEC_UNAPPROVED` | 当前的规格与受保护文件没有有效批准 |
| 50 | `REPAIR_EXHAUSTED` | 修复循环结束但没有成功；修复循环不改动工作区 |
| 2 | — | 用法或输入错误。若命令此前已创建运行目录，该次运行会以状态 `TOOL_ERROR` 记录，`report` 对它返回 30 |
| 70 | — | 内部错误（traceback 写到 stderr） |
| 130 | — | 被中断（Ctrl-C） |

“没找到反例”不等于成功。超时、工具错误、无法解析的结果、被跳过的关卡，都不算成功。必需的 `inductive` 关卡未通过时，结果最好也只是 `UNKNOWN`；所有必需关卡都通过时，必需的 `wp` 关卡给出 `PROVED` 而不是 `VERIFIED_INDUCTIVE`。汇总规则见 [docs/status-semantics.md](docs/status-semantics.md)。

## 关卡规则

这些规则堵住了“关卡通过、但代码其实没被真正检查”的漏洞；仍未堵住的列在[限制](#限制)中。它们都不会把失败变成成功。

| 规则 | 行为 |
|---|---|
| 完整性未通过 | `sanitizer`、`cbmc`、`inductive`、`wp`、`lean` 只在完整性关卡通过后运行，否则为 `SKIPPED`（报告原文：“never executed, model-checked or proved”）；`gates` 不含 `compile` 时同样如此，因为完整性关卡在编译关卡通过后才运行。`static_analysis` 照常运行。状态取自完整性关卡：`FAIL` 得 `STATIC_ANALYSIS_WARNING`，`ERROR` 得 `TOOL_ERROR`，`TIMEOUT` 得 `TIMEOUT`；完整性关卡没有运行时，状态为 `UNKNOWN`。修复循环中，让完整性通过的候选算作进展，即使随后解锁的关卡找到反例，前提是先前已运行的关卡（`compile`、`static_analysis`）没有失去通过、也没有新增失败 |
| 栈帧大小 | 完整性关卡以 `-fstack-usage` 编译每个可编辑文件（先以 `-include` 加入一个锚点函数，再加去掉警告控制后的 `compile.flags`，最后是 `-O0 -fno-lto -fembed-bitcode=off`），读取 clang 写出的栈帧大小：超过 1 MiB 的栈帧为 `STACK_FRAME_TOO_LARGE`，clang 标为 `dynamic` 的栈帧（运行时才分配的栈：VLA、`alloca`、`_alloca`）为 `UNBOUNDED_STACK`。锚点让 clang 为每个生成代码的文件写出栈用量文件；没有写出时（例如 `-emit-llvm`）或编译的退出码不是 0，完整性关卡为 `ERROR`。VLA / `alloca` 检查（`UNBOUNDED_STACK`）对每个文件编译三次。第一次带上 `compile.flags`，失败即关闭：只有每一行错误都是解析得出的 VLA / `alloca` 诊断时才算通过。第二次把 `compile.flags` 中的系统 include 选项（`-isystem`、`-idirafter`、`-iwithsysroot` 等）改成放在规格 include 目录之后的 `-I`，保持它们在 `compile.flags` 中的顺序，并去掉 `--system-header-prefix=`；`-idirafter` 的目录因此会先于工具链自己的头文件被搜索，所以这次运行可能在正式构建能编译的情况下编译不过。第三次不带 `compile.flags`，与旧版相同，同样失败即关闭，所以只靠某个 `compile.flags` 项才能编译的文件会得到 `ERROR`：include 目录请放在 `sources.include_dirs`，宏请放在 `compile.defines`。只有第二次运行只增加违规而不会失败：它编译不过时，它输出的 VLA / `alloca` 诊断仍算作违规，其他错误行被忽略；但它超时或编译器无法运行时仍然算数。因此系统 include 选项、module map 与强制包含头文件中的 pragma，都无法隐藏不带它们时会被报告的 VLA，而编译器自己的系统头文件（libstdc++）仍是系统头文件 |
| 三字符序列 | 可编辑的 C 代码中任何位置（包括注释与字符串字面量）出现三字符序列（`??/`、`??=` 等）即为 `TRIGRAPH`：clang 在 ISO C 模式（`-std=c17`）下以及 Frama-C（它的 gcc 以 `-std=c11` 运行）会替换三字符序列，CBMC 以 gcc 的 gnu 模式预处理则不会，所以注释结尾的 `??/` 只会对其中一方隐藏下一行。原始字符串前缀（`R"`、`u8R"`、`LR"` 等）为 `TOOL_DIVERGENCE`：gcc 的 gnu 模式把它读成原始字符串字面量，clang 在 C 中读成标识符加字符串。`__FILE__` 也是 `TOOL_DIVERGENCE`，因为 `-fmacro-prefix-map=` 只改变 clang 的值 |
| 类型选择与隐藏调用 | 完整性关卡在可编辑代码中以及宏展开之后（因此受保护头文件的宏在可编辑代码使用处也会被抓到）拒绝：`_Generic`、`__builtin_types_compatible_p`、`__builtin_classify_type` 为 `TOOL_DIVERGENCE`（CBMC 5.95.1 与 clang 可能选择不同的关联：`long x` 时 `_Generic(x, long: 1, long long: 2)` 在 clang 下为 1、在 CBMC 下为 2）；`mode` 属性为 `TOOL_DIVERGENCE`（CBMC 忽略它：`mode(QI)` 类型在原生构建中为 1 字节，在 CBMC 中为 4 字节）；`cleanup` 属性为 `CONSTRUCTOR`（CBMC 与 Frama-C 都不会调用清理函数，所以其中的访问从未被检查） |
| 自循环（CBMC） | verify 与 vacuity 运行在 `--unwinding-assertions` 之后传入 `--no-self-loops-to-assumptions`。没有它时，CBMC 会把跳回自身的循环（`for (;;) {}`、`while (1);`、`L: goto L;`、`while (k > K) {}`）变成假设并丢弃到达它的路径，于是在允许的输入上原生会卡住的代码也会得到 `VERIFIED_BOUNDED`；有了它，这种循环会使展开断言失败（`INCONCLUSIVE`，状态 `UNKNOWN`） |
| vacuity 构建（CBMC） | 在任何 CBMC 运行之前，每个可编辑源文件和 harness 都在没有 `-DLEANC_VACUITY_CHECK` 的情况下预处理一次（`cbmc --preprocess`，使用 `SOURCE_DATE_EPOCH=0`，因此 `__DATE__` 和 `__TIME__` 是固定的），之后若有 vacuity 运行，再在有该宏的情况下预处理一次。文本写入 `build/cbmc/verify/` 和 `build/cbmc/vacuity/`（`<path>.i`），verify、coverage 和 vacuity 运行只读取这些文件，因此无论 harness 从时钟或文件系统读取什么（例如对运行输出使用 `__has_include`），每次运行检查的都是被比较过的文本。`cbmc --preprocess` 即使预处理器失败也以 0 退出，因此 stderr 上的 `PREPROCESSING ERROR` 行（`#include` 了不存在的文件、`#error`）、不是有效 UTF-8 的输出（例如字面量中的 Latin-1 字节 `é`：`.i` 文件保存的字节会与被编译的源文件不同）以及没有源文件本身 linemarker 的输出，同样是 `ERROR`（`TOOL_ERROR`）。CBMC 自带的函数库模型以 CBMC 的默认设置预处理：`compile.flags` 与 `compile.defines` 中的 `-D` 和 `-I` 只作用于项目的代码，因此模型看不到对它所检查的宏的 `-D`（例如 `MAP_UNINITIALIZED`，`mmap` 模型把它当作 0），也看不到 `-I` 用来取代系统头文件的头文件。把代码位置上的每个 `LEANC_REACH` 展开（同一行上的 `__CPROVER_assert(0, "leanc_reach:" "label")`，字符串字面量内的文本不算，经由 `.` 或 `->` 调用的也不算）替换为 `((void)0)` 之后，一个单元的两份文本必须完全相同。否则关卡为 `ERROR`（`TOOL_ERROR`），且不启动任何 CBMC 运行：直接或通过字符串化探针依据 `LEANC_VACUITY_CHECK` 分支的 harness，会显示到达探针的是另一个程序，而不是被验证的程序。vacuity 文本中的每个这样的位置，都需要在它的文件、行和标签上有一个属于它自己的已报告探针（CBMC 丢弃的代码里通过别名写的探针不会被报告）；没有的位置即为 vacuous harness（`INCONCLUSIVE`，状态 `UNKNOWN`） |
| `__CPROVER_` 名称（CBMC） | verify 运行之后，预处理单元的 `cbmc --show-symbol-table`（若之后有 vacuity 运行则用 vacuity 单元，因为只在探针位置使用的名称在 verify 单元中未被使用而被丢弃）中不能有位于源文件里、名称以 `__CPROVER_` 开头的符号（函数、参数、局部变量、全局变量、typedef；看基本名称或最后一个 `::` 部分）。参数 `void (*__CPROVER_assert)(int, const char *)`、这种类型的局部变量或 `__CPROVER_assert` 的定义，会让 `LEANC_REACH`、`LEANC_ASSUME` 和 Lean C 插入的检查调用 harness 代码；这样的程序为 `ERROR`（`TOOL_ERROR`）。归纳关卡对它两个构建的链接后符号表套用同一规则。块作用域内重新声明内置函数也会被拒绝 |
| 预定义宏 | 仅适用于含 `cbmc` 或 `wp` 的规格。把 clang 在编译关卡选项（`-std`、`compile.flags`、`compile.defines`）下的预定义宏，与 gcc 在各验证器自身预处理方式下的预定义宏比较。CBMC：`-D__CPROVER__ -m64`、`-funsigned-char`、`-std=gnuNN`、它收到的 `-D` 定义，然后是它的 x86-64 定义（`__LP64__`、`__x86_64__` 等），顺序与 CBMC 调用 gcc 时相同。Frama-C：`-undef -nostdinc` 加 machdep 的内建宏（`frama-c -print-machdep-builtin-macros`，其中定义了 `unix` 与 `linux`）、`-D__FRAMAC__ -std=c11` 与 `compile.defines`。可编辑代码中用到取值不同的宏即为 `TOOL_DIVERGENCE`。需要 `gcc`，`wp` 还需要 `frama-c`；缺少时完整性关卡为 `ERROR` |
| `compile.flags` | 每一项都是一个选项，值直接接在选项后面（`-Iinclude`、`-DNAME=1`、`-std=c17`、`-includepre.h`）。规格验证拒绝输入文件、`-` 与 `--`。完整性关卡会询问 clang（`clang -### <项>`）某一项是否把下一项当作自己的值（`-I -Wno-such-dir`、`-iprefix -funsigned-char`）；这样的项得 `TOOL_ERROR`，因为 CBMC 转换与栈检查把每一项都当作独立选项读取。另外拒绝：`-fsanitize*` / `-fno-sanitize*`（sanitizer 由 `tests.sanitizers` 决定），以及把参数交给内部工具或替换工具链的选项：`-X...`（`-Xclang`、`-Xanalyzer`、`-Xlinker` 等）、`--for-linker`、`-mllvm`、`-Wp,` / `-Wa,` / `-Wl,`、`--analyze...`、`@file`、`--config`、`-fplugin` / `-fpass-plugin`、`-ccc-*`、`-B` / `--prefix`、`--ld-path` / `-fuse-ld=<path>`、`-resource-dir` / `--resource-dir`、`--sysroot` / `-isysroot`、`-ivfsoverlay` / `-vfsoverlay`、`--gcc-toolchain` / `--gcc-install-dir`、`-nostdlib` / `--no-standard-libraries` / `-nodefaultlibs` / `-nostartfiles`、`--driver-mode`。改变预处理器输出或在生成代码之前就停止的选项也会被拒绝，因为完整性关卡以 `compile.flags` 读取预处理后的源码：`-E`、`-P` / `--no-line-commands`、`-C` / `-CC`、`-d*`（`-dM`、`-dD` 等）、`-M` / `-MM`、`-fdirectives-only`、`-frewrite-includes`、`-fuse-line-directives`、`-fkeep-system-includes`、`-fminimize-whitespace`、`-traditional-cpp`、`-fsyntax-only`、`-fdriver-only`；预处理输出中没有该文件的 linemarker 时为 `ERROR`。改变 C 源码分词方式的选项也会被拒绝，因为 CBMC 以 gcc 的 gnu 模式（最高 gnu11）预处理，Frama-C 以 gcc 的 `-std=c11` 预处理：strict C89/C90（`-std=c89`、`-std=c90`、`-std=iso9899:1990`、`-std=iso9899:199409`、`-ansi`，它们没有 `//` 注释）、C23（`-std=c23`、`-std=c2x`、`-std=gnu23`、`-std=gnu2x`，它们有数字分隔符），以及 `-fdigraphs` / `-fno-digraphs`。`-Wwrite-strings` 会被拒绝：clang 把它转成 `-fconst-strings`，让字符串字面量的类型变成 `const char[N]`，而没有任何验证器套用这一点。静态分析收到其余参数。CBMC 会收到 `-D`、`-I`、`-std=` / `--std=` 与 `-funsigned-char`（有符号 `char` 是 CBMC 在 x86 上的默认），GCC 的长拼法（`--define-macro=`、`--undefine-macro=`、`--include-directory=`、`--unsigned-char`、`--signed-char`）也一样，但绝不会收到 `NDEBUG`。`-U` 只能取消它之前的 `-D`（函数式宏也一样）；其他 `-U` 会被列为未套用。警告与 `-g` 参数没有语义影响，但 `-Wdeprecated` / `-Wno-deprecated` 除外：它们在 C++ 中决定 `__DEPRECATED`，因此会保留给栈检查。其余参数（例如 `-O2 -ffast-math -mavx2`）列在假设“compile.flags not applied by CBMC”中；CBMC 的判定不变。对 WP 而言，每一项要么被套用，要么没有语义影响（警告与 `-g` 参数，同上），要么未被套用。只有当构建的 `char` 符号性与 Frama-C machdep 的相同（`x86_64` 上为有符号）时，char 符号性才算被套用。构建的符号性取最后一个 char 符号性参数（`-fsigned-char`、`-fno-unsigned-char`、`--signed-char` 为有符号），否则取 `clang <compile.flags> -dumpmachine` 输出的构建目标的默认值：架构 `x86_64`、`i386`、`x32`（CBMC 使用的同一张表）为有符号，其他架构视为未知是否有符号。没有 char 参数时，未知是否有符号的目标（如 `aarch64-unknown-linux-gnu`）会加入未套用项 `(default char of build target ...)`；查询失败或无法解析的三元组无论参数如何都会加入 `(build target unknown: ...)`。最后一个 `-std=` / `--std=` 选择 C11 或 C17（`c11`、`c1x`、`iso9899:2011`、`c17`、`c18`、`iso9899:2017`、`iso9899:2018`）时该参数被套用；它与 `language.standard` 一样以“Frama-C preprocesses and parses the code as c11”披露。`gnu*` 与其他标准不被套用。其余各项都未被套用，包括 `-D`、`-U` 与 `-I`（Frama-C 只收到 `sources.include_dirs` 与 `compile.defines`）。未被套用的项列在“compile.flags not applied by Frama-C”中（`wp` 阶段的 `details.compile_flags`）。只要有其中任何一项，证明就是 `wp` 关卡的 `INCONCLUSIVE`（`UNKNOWN`），绝不是 `PROVED`，而 Frama-C 模型下的结果保留在 `details.frama_c_model_verdict` 中。此时不会尝试证明修复 |
| 无函数体的函数（CBMC） | 允许：`nondet_*`、`__CPROVER*`、`__builtin_prefetch`、`__builtin_expect`。其他无函数体的被调函数会使 `cbmc` 无法通过：`<immintrin.h>` 背后的 SIMD builtin，以及完整性关卡允许、但 CBMC 没有模型的 `memchr`、`strnlen`、`strstr`、`fputc`、`putc`。路径用到这类函数返回值的违规是 `unmodelled-callee` 诊断，不是反例；其他违规仍是反例。没有反例时关卡为 `INCONCLUSIVE`（状态 `UNKNOWN`）：把调用改写成普通 C 才能得到判定 |
| 未声明的函数（CBMC） | CBMC 把对未声明函数的调用视为返回 `int`，因此未声明的 `nondet_*` 输入只会取到转换成调用方类型的 `int` 值。`function 'f' is not declared` 警告是 `undeclared-function` 诊断，会使 `cbmc` 无法通过（`INCONCLUSIVE`，状态 `UNKNOWN`）；违规仍是反例。`leanc.h` 声明了 `nondet_int`、`nondet_uint`、`nondet_long`、`nondet_ulong`、`nondet_llong`、`nondet_size_t`、`nondet_char`、`nondet_uchar`、`nondet_short`、`nondet_bool`、`nondet_int32_t`、`nondet_uint32_t`、`nondet_int64_t`、`nondet_uint64_t`：其他 `nondet_*` 请在 harness 中声明 |
| CBMC 内部错误 | 不变式违规报告为 `TOOL_ERROR`（“CBMC internal error”），并附带 File/Condition/Reason 摘录，而不是内存不足提示 |
| 契约安全检查 | `contract.safety` 中的检查会加入 CBMC 的属性检查；规格哈希不变 |
| sanitizer 构建 | 测试、所有权追踪和 Lean 向量的构建都在 `compile.flags` 之后加上 `-fno-builtin -fno-optimize-sibling-calls`，这样 clang `-O1` 无法删除未使用的分配（`malloc`、`strdup`、`aligned_alloc`、C++ `new`），也无法删除包装函数的栈帧 |
| 内存检查的局限 | `limitations` 列出泄漏检查看不到的部分；只有关卡得出判定（`PASS` 或 `FAIL`）的检查才算数。LeakSanitizer 只报告不可达的块。没有 CBMC `memory-leak` 判定时，只在测试路径上检查泄漏。CBMC 不检查对齐、严格别名、`restrict`，也不检查同一分配内子对象之间的溢出 |
| `inductive` 关卡（opt-in） | 在 `cbmc` 之后运行，前提是规格的 `gates` 列出 `inductive`；此时规格验证器要求有 `inductive` 段落、`cbmc` 关卡以及 `verification.malloc_may_fail: false`。与 `cbmc`、`wp`、`lean` 一样，它只在完整性关卡通过、并且受保护文件再次与批准比对之后运行。它需要 `cbmc`、`goto-cc` 与 `goto-instrument`（`LEANC_CBMC`、`LEANC_GOTO_CC`、`LEANC_GOTO_INSTRUMENT`），三者的版本行都恰好有一个 `(cbmc-X.Y.Z)` 标记，且都是同一个经过测试的版本（只有 5.95.1），`goto-cc` 必须以 gcc 身份运行（`gcc (goto-cc ...`）；否则为 `ERROR`。该关卡的每次工具运行（预处理、goto-cc、goto-instrument、两次 CBMC 运行）都受 `verification.timeout_s` × `--timeout-scale` 与 `verification.memory_mb` 限制；超时就是 `TIMEOUT`，永远不是部分通过。目标函数必须被 claim，这可能使关卡变慢（`examples/inductive_sponge` 约 70 秒） |
| inductive harness | `inductive.harness` 受保护。它的 `main()`（只定义一次，且在该文件中）以 `LEANC_ASSUME` 在任意状态上假设状态不变量，直接调用 claimed 函数，在调用之后断言，并至少含有一个 `LEANC_REACH`。Lean C 对自己的副本插桩，以下每一项都是 `ERROR`：某条路径在 harness 的一次 claimed 调用之后又到达第二次 claimed 调用（由插入的断言逐路径检查，因此没有 claimed 函数会从另一个函数的结束状态开始）；在 claimed 调用之后出现 `LEANC_ASSUME` 或 `__VERIFIER_assume`（同一函数内静态检查，并逐路径检查），或在假设中调用 claimed 函数；claimed 函数以直接调用以外的方式使用，或从未被调用；在函数体中引用 `main`；在进入 claimed 函数之前运行了可编辑源文件中的任何函数（假设中的可编辑 predicate、setup 调用、claimed 调用的参数：每个可编辑函数都会被插入入口检查）；`main()` 中除了唯一一个位于最后的顶层 `return` 以外的 `return`，或 `main()` 中的 `goto`；`main()` 中从最后一个 `LEANC_ASSUME`（没有就从开头）到第一次 claimed 调用结束之间的 `=`、复合赋值、`++` 或 `--`，一般声明 `T x = ...;` 的 `=` 除外；harness 翻译单元或其头文件中使用 `exit`、`abort`、`_Exit`、`quick_exit`、`longjmp`、`raise`、`err`、`errx`、`verr`、`verrx` 或其他会结束或跳出路径的名称，即使只是作为函数指针值；`leanc_` 名称；读取 inductive 运行会保留其初始值的可编辑对象（const，或含 const 成分）；`#include` 可编辑源文件；有无 `-DLEANC_VACUITY_CHECK` 的两次预处理结果除 `LEANC_REACH` 展开外还有差异。不检测：harness 的其他代码也会缩小被覆盖的状态（声明的初始值、最后一个 `LEANC_ASSUME` 之前的写入、第一次 claimed 调用之后的写入、包住断言的分支、helper 函数），因此批准者必须审查整个 harness |
| inductive 构建 | 该关卡只处理每个可编辑单元、harness（有无 `-DLEANC_VACUITY_CHECK` 各一次）及其复制模型的 `cbmc --preprocess` 输出（`SOURCE_DATE_EPOCH=0`）；stderr 上的 `PREPROCESSING ERROR` 行、不是有效 UTF-8 的输出以及没有该单元本身 linemarker 的输出都是 `ERROR`。`inductive.contracts` 中的子句必须通过严格的文法（不允许调用、赋值、`++` / `--`、`?:`、逗号、取地址、`sizeof`、量词，也不允许 `sources.editable` 中定义的宏名或 `NULL`、`INT_MAX` 等标准宏名；见 [docs/spec-format.md](docs/spec-format.md)），只有其完全加括号的渲染结果会被插入，位置是契约所在行上写出的那一个带 `{` 循环体的 `for` / `while` 循环。其他位置，或无法编译的子句（例如受保护头文件中的宏名，在那里不会被展开），都是 `contract_rejected`（`INCONCLUSIVE`）。`goto-cc --verbosity 2` 先编译可信单元（复制模型，然后是 harness），再编译可编辑单元。在链接后的符号表中，每个可信符号都必须仍绑定到它自己的定义，不得存在 `<可信名称>$link<N>` 重命名（两个 static 之间也一样：请给可编辑的那个改名），可编辑文件中的符号不得命名为 `leanc_...`，源文件中也不得声明 `__CPROVER_` 名称；否则为 `ERROR`。goto-cc 的 `function 'f' is not declared` 警告是 `undeclared_function`（`INCONCLUSIVE`）。之后运行 `goto-instrument --dfcc main --apply-loop-contracts`（从不使用 `--loop-contracts-file`，也从不省略 `--dfcc`）；harness 与 CBMC 函数库之外仍没有契约的循环是 `loop_without_contract`（`INCONCLUSIVE`），且不运行 CBMC。`memcpy`、`memmove` 与 `memset` 被替换为模型：检查来源与目的范围（`memcpy` 还检查不重叠），然后让目的内容变成任意值；非 const 的 static 对象从任意值开始；内存分配永不失败 |
| inductive 判定 | verify 运行是 `cbmc dfcc.goto --no-library <checks> --unwind U --unwinding-assertions --no-self-loops-to-assumptions [--object-bits N] --json-ui`，其中 U = max(`verification.unwind`, 最长的 assigns 列表 + 2)，且只有强制检查类别（`bounds`、`pointer`、`div-by-zero`、`signed-overflow`、`pointer-overflow`、`undefined-shift`）：从任意状态出发，合法的模运算与泄漏没有意义，因此 `verification.checks` 与 `contract.safety` 的类别只保留有界结果。每个契约都必须对应恰好一个 CBMC 循环，且该循环具备全部五个类别 `loop_invariant_base`、`loop_invariant_step`、`loop_decreases`、`loop_assigns`、`loop_step_unwinding`（否则为 `ERROR`）；完全没有这些性质的契约（其循环无法从 harness 到达）是 `contract_rejected`。vacuity 运行必须到达每个 `LEANC_REACH` 位置、每个 claimed 函数入口的 `leanc_function:<f>` 探针、每个带契约循环体开头的 `leanc_loop:<file>:<line>` 探针，以及每次 claimed 调用之后的 `leanc_return:<f>` 探针；不可达的探针是 `vacuous_harness`。不成立的性质给出 `INCONCLUSIVE` 与原因代码：`inductive_step_failed`、`suspected_non_termination`（decreases 子句，位于该循环）、`unwind_insufficient`、`unmodelled_callee` 或 `property_unproved`。这些失败的 kind 是 `unproved`，永远不是反例，因为每次 claimed 调用与每个循环都从任意状态开始，而真实执行可能永远到不了那个状态。无法判定的性质、违反 harness 规则，或 CBMC 的性质列表中缺少 harness 断言，都是 `ERROR`。该关卡的结果是 `PASS`、`INCONCLUSIVE`、`TIMEOUT` 或 `ERROR`，永远不是 `FAIL`。必需的 `PASS` 加上 `cbmc` `PASS` 得到 `VERIFIED_INDUCTIVE`，它陈述的范围列在 `assumptions` 中 |

## 机器接口（AI agent / CI）

使用 `--json`，并按退出码或 `status` 分支。以 exit 2、70 或 130 结束时，以及在 `spec approve` 的交互确认中拒绝确认（exit 40）时，不会输出 JSON 对象，消息写到 stderr。成功的 `generate` 报告的 `status` 为 `UNKNOWN`，`exit_code` 为 0。`leanc verify SPEC --json` 的输出如下：

```json
{
  "command": "verify", "status": "COUNTEREXAMPLE_FOUND", "exit_code": 13,
  "run_id": "verify-20261121T034210Z-7c8afd", "run_dir": ".../.leanc/runs/verify-...",
  "provider": null, "report": ".../report.md", "messages": ["..."],
  "verification": {
    "status": "...", "success": false, "spec_id": "...", "spec_version": 1, "spec_hash": "...",
    "approval": {"...": "..."},
    "stages": [{"stage": "cbmc", "outcome": "FAIL", "status": "...", "required": true, "invoked": ["cbmc"],
                "duration_s": 1.2, "summary": "...", "tools": ["cbmc"], "failures": 3}],
    "detected_by": ["asan", "cbmc"], "reasons": ["..."], "assumptions": ["..."], "limitations": ["..."],
    "reason_codes": [{"code": "gate_failed", "stage": "cbmc", "location": null, "detail": "..."}],
    "failures": [{
      "id": "sanitizer:index_equals_count:asan:heap-buffer-overflow", "tool": "asan", "stage": "sanitizer",
      "kind": "...", "property_id": "...", "location": {"file": "src/array_lookup.c", "line": 9, "column": 16, "function": "array_lookup"},
      "inputs": [{"name": "argv", "value": "index_equals_count", "type": null}],
      "description": "...", "raw_ref": "raw/...", "ownership_trace": null
    }]
  }
}
```

`reason_codes`（也在 `verification.json` 中，并列在 `verification.md` 与 `report.md` 的“Reason codes”下）在成功状态下为空。否则它以未通过的必需阶段解释状态（`SPEC_UNAPPROVED` 时以规格检查解释）；每一项有 `code`、`stage`、`location`（或 `null`）与 `detail`。有具体代码的 `INCONCLUSIVE` 阶段只列出具体代码；其他结果先给出通用代码（`gate_failed`、`gate_skipped`、`gate_timeout`、`tool_error`、`gate_inconclusive`）。具体代码有 `unwind_insufficient`、`vacuous_harness`、`vacuity_unchecked`、`unmodelled_callee`、`undeclared_function`、`property_unproved` 与 `frama_c_model_only_proof`，`inductive` 关卡另有 `inductive_step_failed`、`suspected_non_termination`、`contract_rejected` 与 `loop_without_contract`；`no_formal_gate` 标记既没有 `cbmc` 关卡也没有必需 `wp` 关卡的 `UNKNOWN` 规格。请按 `status` 与 `exit_code` 分支；代码说明原因。

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

C 项目出现 LeakSanitizer 失败时，`ownership_trace` 保存对该泄漏测试用例做插桩重放得到的证据。它不参与判定。C++ 和多线程的测试程序不追踪。内容如下：

- `allocation`：配置点。
- `released_at`：同一配置点的内存在其他路径上被释放的位置。
- `loss` 与 `function`：最后一个引用消失时间的上界。只有在某次检查中被列为 Direct 泄漏才算证据；LeakSanitizer 的扫描是保守的，扫描中仍找到看似指针的值并不能证明任何事。`loss` 是以下之一：
  - `lost-during-call`：该内存在调用 `function` 期间分配，且在 `function` 返回前已不可达，所以是在 `function` 或其被调用者中遗失的。这是唯一的强结论。
  - `unreachable-by-return`：`function` 不在分配时的调用栈上，到它即将返回时已没有任何引用。这不说明是在哪里遗失的。
  - `unreachable-by-next-event`：在 `function` 返回后的第一个被追踪事件之前，于 `function` 或其调用者中遗失。
  - `not-localised`：程序结束前没有任何检查发现它不可达。仍会报告 `released_at`。
- `returned_to`：`function` 返回到的调用点；被内联的函数不提供。

修复 prompt 会以明确标签显示同样的证据。它不会指出执行的是哪一行 `return`。被测代码运行在被追踪的进程中，因此它能伪造或隐藏这些证据（例如通过继承的文件描述符向追踪日志追加行，或在 stderr 输出探针标记或形似 LeakSanitizer 的行）：证据可能因此错误或缺失，但判定不受影响。

agent 修正代码有两种方式：

1. **外部 agent 循环。** 运行 `leanc verify SPEC --json`，读取 `verification.failures`（位置、输入、性质）与 `raw_ref`。只修改 `sources.editable` 列出的文件（规格有时还包括 `inductive.contracts` 或 `lean.proofs`），然后再验证。遇到 exit 0，或 40（需要人处理）、20/21/30（不是代码缺陷，请看 `reasons` 与 `reason_codes`；`loop_without_contract`、`contract_rejected` 与 `inductive_step_failed` 需要修改 `inductive.contracts`）时停止。
2. **内置循环。** 运行 `leanc run SPEC --provider openai --json`（或 `repair`）。Lean C 会调用模型、对每个补丁做策略检查，并且只把所有必需关卡都通过的候选写回：验证通过的候选（`VERIFIED_BOUNDED` / `VERIFIED_INDUCTIVE` / `PROVED`），或者在既没有 `cbmc` 也没有 `wp` 关卡的规格中状态仍为 `UNKNOWN` 的候选（`--no-write` 不把修复后的候选写回；在 `run` 中，生成缺少的源文件或以 `--regenerate` 重新生成时仍会写入 `sources.editable`）。

修复策略会拒绝以下补丁：写入可编辑文件以外的文件、加入 `__CPROVER_*` / `LEANC_*` / `__builtin_assume`、删除 `assert`、抑制警告或 sanitizer、`#include` 逃逸、`system`/`exec`/`fork`/`socket`、inline asm、把目标函数掏空，以及过大的补丁。重复的补丁会被判定为循环。结果变成 `UNKNOWN`/`TIMEOUT` 不算改善。使用 `inductive` 关卡时，契约文件必须通过子句文法（`LOOP_CONTRACT`）；在只剩证明关卡未通过、且其中包含 `inductive` 的轮次，修改其他任何文件都会在验证前被拒绝（`CONTRACTS_ONLY_ROUND`）；而当 `inductive` 关卡在修复会话开始时没有通过时，C 代码与会话开始时不同、且通过 `inductive` 关卡的候选会在验证后被拒绝（`INDUCTIVE_CODE_EDIT`）：它永远不会被接受或写回，其源文件留在 `repair/round-<n>/blocked-candidate/` 供人审查。修复衡量进展时，`inductive` 阶段的原因代码与失败一起计算。

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
| `LEANC_ALLOW_INSECURE_HTTP=1` | 允许通过明文 `http://` 把 API key 发送到非 loopback 的主机。未设置时，只要设置了 key（`OPENAI_API_KEY` 也算），这样的 `LEANC_BASE_URL` 就会被拒绝；`localhost`、`*.localhost`、loopback IP 以及不带 key 的端点始终允许 |
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

项目配置不能削弱检查、界限、关卡或批准，也不能让不可信的程序在没有沙箱的情况下运行。`sandbox: none` 的作用与 `--no-sandbox` 相同：工具也不使用 bwrap，动态测试会 `SKIPPED`。若设置了 `allow_unsandboxed`，该值会被忽略并发出警告。

| 变量 | 含义 |
|---|---|
| `LEANC_HOME` | 批准密钥与账本目录（默认 `$XDG_CONFIG_HOME/leanc`，未设置 `XDG_CONFIG_HOME` 时为 `~/.config/leanc`）；请放在工作区之外 |
| `LEANC_CLANG`、`LEANC_CLANGXX`、`LEANC_CBMC` | 工具路径 |
| `LEANC_GOTO_CC`、`LEANC_GOTO_INSTRUMENT` | `inductive` 关卡使用的 `goto-cc` 与 `goto-instrument`；未设置时搜索 `PATH`。它们与 `cbmc` 必须报告同一个经过测试的版本（5.95.1） |
| `LEANC_FRAMAC` | `frama-c`（>= 27，含 WP）；未设置时搜索 `PATH` 与 `/opt/opam/*/bin` |
| `LEANC_LEAN` | `lean`（含 `leanchecker` 的 toolchain）；未设置时搜索 `PATH` 与 `/opt/lean/*/bin` |

## V2 可选工具

| 功能 | 安装 | 规格 | 对状态的影响 |
|---|---|---|---|
| Frama-C/WP + ACSL | `sudo apt-get install -y opam z3 cvc5 && opam init -y && opam install -y frama-c alt-ergo`（Ubuntu 的 `frama-c-base` 不含 WP） | ACSL 契约写在受保护头文件中，关卡 `wp` | `PROVED` 唯一的途径；证明修复让 AI 补上 loop invariant |
| Lean 4 | release toolchain 解压到 `/opt/lean/<版本>`，或使用 elan | 受保护的模型与定理陈述；证明由 AI 编写；kernel + `leanchecker`；只接受标准公理；差分测试向量 | 模型层级：未证明 → `UNKNOWN`，向量不符 → `RUNTIME_TEST_FAILED`；永远不会产生 `PROVED` |
| C++20 | `clang++`（clang-18 自带） | `language: {name: cpp, standard: c++20}` | 没有可用的 C++ model checker，因此最佳结果是 `UNKNOWN` |
| Inductive profile（opt-in，C） | 除 `cbmc` 外不需要别的：Ubuntu 24.04 的软件包就是 CBMC 5.95.1（唯一经过测试的版本），并附带 `goto-cc` 与 `goto-instrument` | 受保护的 `inductive.harness`、可编辑的 `inductive.contracts`、`inductive.functions`（含 `target.function`），关卡 `inductive` 与 `cbmc` 并列 | `cbmc` 也通过时为 `VERIFIED_INDUCTIVE`（exit 0）；永远不会产生 `PROVED`；证明无法下结论时为 `UNKNOWN`，此时修复只能改契约文件 |

`leanc doctor` 会报告上述每个工具。缺少某个工具时，相关测试会被跳过，而不是失败（缺少 `goto-cc` 或 `goto-instrument`，或者 CBMC 版本不是 5.95.1，会让使用真实工具的 inductive 测试失败，而不是跳过）。

## 示例

每个 `examples/<name>/` 都包含 `spec.yaml`、受保护的 `include/`、附带的（通常有缺陷的）`src/`、`mock/` fixture 以及 `expected.yaml`；有 `cbmc` 关卡的示例还有受保护的 `harness/`（两个 C++ 示例没有），有 `sanitizer` 关卡的示例还有受保护的 `tests/`（`vacuous_harness` 没有）。`inductive_sponge` 另有受保护的 inductive harness `harness/sponge_inductive.c` 与可编辑的契约文件 `inductive/loops.json`。

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
| `inductive_sponge` | 正确；opt-in 的 `inductive` 关卡对所有不超过 2^48 的长度 claim `sponge_absorb` | `VERIFIED_INDUCTIVE` | `VERIFIED_INDUCTIVE`（没有修复轮次） |

全部复现：

```bash
python scripts/run_experiments.py                       # 全部示例；应报告 N/N 符合
python scripts/run_experiments.py --only array_oob div_by_zero
```

脚本会把每个示例复制到临时项目，并用临时的 `LEANC_HOME` 批准。接着通过真正的 CLI 运行 `verify` 与 `run --provider mock --regenerate`，再与 `expected.yaml` 比对结果。输出写到 `experiments/results/`（本地生成，已加入 gitignore）。详见 [docs/experiments.md](docs/experiments.md)。

## 开发 Lean C

规则声明在 [`constraints.toml`](constraints.toml) 中。它的作用相当于 yarn 项目里的 constraints 文件，是唯一的规则来源。`python -m scripts.constraints` 检查这些规则，`tests/test_architecture.py` 运行的是同一个引擎。agent 指令写在 [`CLAUDE.md`](CLAUDE.md) 中，其中的分层区块由 `constraints.toml` 自动生成。

- 严格的单向分层、无环的模块图，且只允许顶层 import：
  ```
  foundation < specs, lang, reports < rules, toolchain, llm < backends, repair < pipeline < cli < __main__
  ```
  共享代码往下层移。包的 `__init__` 只能 re-export 自己的子模块。
- `leanc/`、`tests/`、`scripts/` 中不得有注释或 docstring，`tests/` 或 `scripts/` 下的 `fixtures/` 目录除外。意图要用名称、常量、Enum 与 frozen dataclass 表达。
- 不得有 bare/`Exception`/`BaseException` handler（唯一的例外是 `leanc/cli.py` 的 `main()` 中的 `except Exception` / `except BaseException`，它以 exit 70 结束；那里同样不允许 bare `except:`），也不得吞掉异常。
- 每个 `.py` 文件最多 600 行（`fixtures/` 目录同样除外）。

| 包 | 职责 |
|---|---|
| `foundation` | 状态、数据模型、原因代码、inductive 范围文本、错误、哈希、凭证遮蔽、工作区（路径/符号链接安全）、沙箱、`run_command`（只接受 argv、资源限制） |
| `specs` | 规格 schema、加载、哈希、批准与账本 |
| `lang` | C / C++ / Lean lexer、循环子句文法 |
| `rules` | 关卡与修复策略共用的纯函数信任规则（C、C++、ACSL、Lean、预处理器、循环契约与循环位置） |
| `toolchain` | 工具探测、CPROVER 版本锁定、`BackendContext` |
| `reports` | JSON / Markdown 输出 |
| `llm` | provider 接口、mock、OpenAI 兼容、prompts、响应解析 |
| `backends` | 各关卡：clang、analyzer、integrity、sanitizer、cbmc、inductive、counterexample、wp、lean |
| `repair` | 应用补丁（fuzz 0）、生成与修复策略 |
| `pipeline` | 验证控制器、原因代码、生成、修复循环及其 inductive 防护、doctor、配置、命令实现 |
| `cli.py` | 只有 argparse 与 `main` |

检查：

```bash
python -m scripts.constraints                       # 检查 constraints.toml 的全部规则；有违规时 exit 1；支持 --json
python -m scripts.constraints --fix                 # 仅在 AST 不变时删除注释/docstring；重新生成 CLAUDE.md 的分层区块
python scripts/affected_tests.py --run              # 修改后：按 import 图相对于 HEAD 选出受影响的测试
python scripts/affected_tests.py --base main --run  # 整个分支
python -m scripts.merge_gate                        # 创建或合并 PR 之前运行（约 20 分钟）
```

`python -m scripts.merge_gate` 在即将被合并的那个 HEAD 上强制执行“修复 → 测试 → 合并”。它按以下顺序执行，第一个失败就停止，没有跳过选项：

1. 用明确的 refspec 获取基准分支。要求所有改动都已提交、HEAD 与基准不同，且 HEAD 包含基准分支。
2. 把固定下来的 HEAD 检出到一个全新的临时 worktree。之后每一步都在其中执行，并去除所有 `PYTEST_*`/`PYTHON*` 环境变量；闸门会确认 `leanc` 是从这个 worktree 导入的。
3. 运行 `python -m scripts.constraints`。
4. 运行完整测试。junit 报告必须显示测试数大于 0，且没有失败、错误或跳过；出现跳过代表缺少必要的工具。
5. 运行 `run_experiments.py`，必须报告 N/N 且 N > 0。
6. 检查批准兼容性：对每个自基准分支以来未改动的示例目录，用基准代码做出的批准，在 HEAD 代码下必须仍然有效。至少要有一个示例目录未改动，否则这一步失败。
7. 确认 worktree 仍在固定的 HEAD 上且没有被改动。

成功时输出 `MERGE GATE PASS head=<sha> base=<sha>`。合并之前要先获取远端，确认远端基准分支仍等于输出中的 base sha（否则重新运行闸门），然后只合并输出中的 head sha（使用 `expectedHeadSha` 和 merge commit）。之后有任何新 commit，都必须重新运行闸门。

完整测试需要 clang-18、gcc、CBMC 5.95.1 及其 `goto-cc` 与 `goto-instrument`（使用真实工具的 inductive 测试会运行它们；缺少 goto 工具或使用其他版本会让这些测试失败，绝不跳过）、bwrap、位于 /usr/ 的 llvm-symbolizer（llvm-18 软件包），以及支持 AVX2 的 x86-64 CPU（SIMD 测试会执行 AVX2 代码）。WP 测试以及 `wp` 规格的完整性测试需要支持 `-print-machdep-builtin-macros` 的 Frama-C >= 27（已用 33 测试）；Lean 测试需要 Lean 4。缺少某个工具时相关测试会被跳过，因此合并闸门需要全部工具。

## 限制

- `VERIFIED_BOUNDED` 受限于已批准的 harness；harness 是否忠实表达需求，仍需由人判断。
- `VERIFIED_INDUCTIVE` 相对于按原样编写的受保护 inductive harness：它涵盖其 `main()` 每条路径最多一次 claimed 调用的执行，且只针对强制检查类别与 harness 断言。harness 的 `LEANC_ASSUME` 是被覆盖状态的上界；声明的初始值、最后一个 `LEANC_ASSUME` 之前或第一次 claimed 调用之后的写入、包住断言的分支以及 helper 函数会进一步缩小它们，且不报告。断言弱，结论就弱：`examples/inductive_sponge` 只断言 `rc == SPONGE_OK` 与 `s->fill < SPONGE_RATE`，所以默默只吸收长输入一部分的代码在那里不会被发现。
- `inductive` 关卡的已知缺口：经由指针、从成员数组溢出到同一对象相邻字段或填充字节的访问（`p->a[i]`、指针运算、`q = p->a; q[i]`、复制模型），以及复制到字符串常量，都不会被报告（有界的 `cbmc` 关卡有同样的缺口；具名对象上的 `s.a[i]` 会被检查）。调用含循环的 CBMC 函数库模型（`strlen`、`strcpy`、`strncpy`、`strncat`）会使循环契约的写入集检查失败，因此这样的代码永远无法通过该关卡（不完备，但永远不是错误的通过）。
- `inductive` 关卡的修复规则（`CONTRACTS_ONLY_ROUND`、`INDUCTIVE_CODE_EDIT`）只限制修复代理：人写的、在大尺寸下放弃正确性的代码，在弱 harness 下仍可能得到 `VERIFIED_INDUCTIVE`；而使该关卡通过的正确代码修正必须由人来套用。
- sanitizer 只覆盖可信测试实际执行到的路径。
- CBMC 不对无函数体的函数（如 SIMD intrinsic）建模，也无法套用部分参数（如 `-ffast-math`、`-O2`、`-m*`）：前者使结果不确定，后者作为假设披露。WP 在 Frama-C 的模型下证明代码：Frama-C 未套用的有语义影响的 `compile.flags` 项（如 `-funsigned-char`、`-O2`、`-fwrapv`、`-D`）会使结果无法成为 `PROVED`，因此这样的规格最多是 `UNKNOWN`。默认 `char` 未知是否有符号的构建目标（如 aarch64，也包括 `char` 实为有符号的 arm64 Apple 目标）需要显式的 `-fsigned-char` 才能得到 `PROVED`。与构建目标比较的只有 `char` 符号性：与 Frama-C `x86_64` machdep 的其他机器模型差异（类型大小、`long double`、对齐）不检查。`language.standard` 只披露、不检查：Frama-C 按 C11 解析，那里的其他标准不阻止 `PROVED`。
- 宏比较只看按名称使用的预定义宏。用记号拼接（`##`）拼出的预定义宏名，以及两边工具中由系统头文件定义且取值不同的宏（例如 `M_PI`：glibc 在 `-std=gnu11` 下定义它，在 `-std=c17` 下不定义），都不会被比较：依据它们分支的代码可能让编译关卡与验证器看到不同的代码。
- `compile.flags` 的检查是一份拒绝清单，而不是允许清单。
- 栈帧大小检查以 `-O0` 度量每个函数。较高 `-O` 下的内联可能合并栈帧，调用链的总深度也不检查。受保护头文件若依 `__OPTIMIZE__` 选择函数体，度量的是 `-O0` 下的版本（可编辑代码不能使用 `__OPTIMIZE__`）。受保护头文件若把自己标为系统头文件（`#pragma GCC system_header`，或标志为 3 的 linemarker），会像以前一样隐藏自身的 VLA 警告；受保护头文件中的诊断 pragma 也能关闭之后代码的 VLA 警告，此时栈帧检查只看得到 `-O0` 下生成的函数中的 VLA。
- `PROVED` 只相对于已批准的 ACSL 契约成立，且以 WP 的内存与机器模型为前提。
- 修复策略是纵深防御（文本规则）；真正的防线是受保护的 harness 加上独立验证器。
- C++20 没有形式化关卡（CBMC 无法解析现代 C++，ESBMC 尚未集成），因此 C++ 的最佳结果是 `UNKNOWN`。
- Lean 证明的是模型，它与 C 代码之间只通过差分测试向量连接。
- bubblewrap 不是虚拟机；高风险工作请在独立的容器或 VM 中运行。

更多文档（繁体中文）：[架构](docs/architecture.md) · [规格格式](docs/spec-format.md) · [状态语义](docs/status-semantics.md) · [信任模型](docs/trust-model.md) · [安全性](docs/security.md) · [实验](docs/experiments.md) · [Roadmap](docs/roadmap.md)

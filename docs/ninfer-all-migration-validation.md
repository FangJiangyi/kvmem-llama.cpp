# ninfer-all 迁移验收记录

日期：2026-10-07。状态：**M0–M6 迁移完成，核心测试通过**。

2026-10-17 的 `08fd7019925f` 刷新单独记录在本文末尾。上述完成状态和下文旧版本表只对应
10 月 7 日迁移，不代表刷新后的基线已经完成相同覆盖；当前源码来源以 `backends/versions.json` 为准。

本代码分支收录已完成验收的迁移源码、固定上游版本和集成补丁。原混合工作区保留；迁移阶段没有覆盖原后端源码或替换运行服务。后续 Q2 制品验证和服务配置见下文。实施过程和恢复方法见 [迁移计划](ninfer-all-migration-plan.md)。构建产物、模型、个人 DSH 配置和原始会话日志不随源码提交。

## 固定版本与可复现性

| 项目 | 最终值 |
|---|---|
| ninfer 上游 | `https://github.com/iamwavecut/ninfer-all.git` |
| ninfer 基线 / gitlink | `8319e8f512477d0cfdef89e16c17c1ba9f9bc7b3` |
| 集成补丁 | `backends/patches/ninfer-kvmem.patch`，761034 字节 |
| 补丁 SHA-256 | `667b7ae9e7357dbb9cbb2d0c352cdc8fdd2a9bf6583248faf99ed0da0afc631f` |
| 准备后完整树 | `54485065cbdf4397227f6b524c08ea50c7b9ee02` |
| 父仓库基线 | `9b92d869ba4ff02fa1d7830687b5e05d93791bbe` |
| llama.cpp 基线 | `7fe450e19305b828c199d602c23a8337aaa1f03b`，本轮未修改 |
| 最终 worker SHA-256 | `4cc165bfd41ed66ae0863621f4070c268d66e8bf8df72a30df065248364867e2` |

`backends/versions.json`、`.gitmodules`、父仓库工作树的 gitlink 和完整补丁已一致固定。独立检出 `m6-clean` 从上述 ninfer 原始提交准备，完整树校验和重复准备通过；有本地源码改动时，准备保留改动，严格检查明确拒绝未记录的源码差异；把 manifest 改回旧 Ryan pin 的负向检查也明确拒绝。最终候选工作树自身的 `--check` 通过。

Windows 构建 wrapper 默认改为 8 并行，定向配置检查确认其指向候选公共 KVMem core。CI 新增 Linux/Windows 固定源码准备与重复校验，接入原有聚合门槛；**没有推送或运行远端 CI**。本机环境为 VS2022/MSVC 14.44、CUDA 13.2.86、Release `120a`/SM120 native；本轮编译均使用 8 并行。

## 迁移内容与支持边界

111 个文件的原 KVMem 补丁已三方适配，实际处理 26 个冲突文件。保留公共 KVMem、Main/MTP 原生 KV 字节、64-token 页、B/R/H、query probe/replay、KV/GDN checkpoint、Host 会话和图像身份。权重加载保留上游 tail/VMM 通路，多卡复制和 Vision 绑定按实际设备/rank 归属适配。上游新的 logprobs、ngram 输入索引、prefill layer-ready 回调和状态发布顺序也保留。

Ryan 原有 strict/mixed 内存策略已在 M1 保留和适配，default/strict/mixed 的实际短生成均通过；五个新增策略文件来自固定 Ryan blob。完整来源清单见 `m1-results/report.json`，不混入 Ryan 后来提交。

新上游 Q2_0/Q4_0/Q5_0 的生产内核、格式和物化逻辑直接采用；旧工作区 21 个量化增量逐文件处理，保留有独立价值的 golden/选行/reader 测试，不搬回未完成的旧 Q2_0 内核。QType 数字编号采用上游，v3 制品继续以格式名称持久化；旧制品已验证可读。

`qwen4_exp`、suspend、graft 和 router 尚未适配 KVMem，入口明确拒绝。DFlash、overlay Vision、并行 prefill、磁盘冷快照和跨后端 KV 转移仍遵循原支持边界。量化格式支持不等于新增模型架构支持。

## 核心运行验证

`core-results` 中 **25 个选定程序调用全部以 0 退出**。以下是验收范围；耗时仅为执行记录，不作性能比较。

| 范围 | 最小核心证据 | 结果文件前缀 |
|---|---|---|
| 原生 payload / 算子 | 九种 KV 格式的 Main/MTP 原始容器与字节；RoPE、INT8 sparse attention、mean-K/query | `payload`、`rope`、`sparse-int8`、`token-sums` |
| 参数、状态及预算 | frontend checkpoint；Engine/CLI/serve 参数；容量、缓存默认值、schema、加载报告、资源 Host/FIFO | `frontend`、`options`、`cli-options`、`serve-options`、`capacity`、`cache-defaults`、`schema`、`load-report`、`resources` |
| 实际越窗 | B128/R128，物理容量 256；输入 257 token，生成 130 token；重选两次，KV/GDN `payload_exact=1`、`state_exact=1` | `ordinary-window` |
| 普通原生对照 | 同一真实制品，原生 INT8/eager，无 KVMem；输入 257、生成 130 token | `native-control` |
| 会话与 Host 压力 | A→B→A 精确恢复；Host 压力淘汰 4 次；取消、编辑及关闭索引后的清理；结束时 resident pages 为零 | `sessions` |
| 工具历史 | thinking/tool 的 canonical input checkpoint、丢弃输出后追加、重复与取消恢复 | `tool-history` |
| Graph/MTP/ngram | 真实 Q4_0 MTP 制品，RK8V4/Graph、自适应 MTP4、ngram31；贪心/固定种子采样、取消、输出限制、编辑与代码复制 | `graph-mtp-ngram` |
| C8 | 同一制品，RK8V4/Graph、自适应 MTP4、ngram15；两个短批次，每批 8 路/48 输出上限；逐路精确重放、历史复用、实际并发解码、Main/MTP 页归零和 H 上界 | `c8` |
| 单卡图像 | 实际 IQ3_S Vision/MTP 制品；resident Vision 覆盖图像身份、重复、替换、取消/多图及预算拒绝后恢复；CPU Vision 运行 query-only 的重复、取消和多图恢复子集 | `vision-resident`、`vision-cpu` |
| 双卡文本 | 5060 Ti + 5050，层划分 40/24；RK8V4/Graph、自适应 MTP4，冷请求、重复、取消与恢复，状态字节和实际 draft 校验 | `pipeline-text` |
| 双卡 C2 图像 | 实际 Vision/MTP 制品，CPU Vision，RK8V4/Graph、自适应 MTP3；混合并发、重复、取消恢复、替换及保留；图像替换实际触发 `identity_rejected=2` | `pipeline-c2-images` |
| 三格式数值和旧制品 | Q2_0/Q4_0/Q5_0 + Q8_0 控制，K=1536/5120，T=1/8/9；独立 host byte decoder 和 FP64 oracle，linear/add/SwiGLU/embedding；旧三格式 writer 制品由新 reader/materializer 校验 | `gguf-numerics`、`old-writer` |

功能测试先查占用，以 UUID 选择空闲设备。数值检查使用 RTX 5050 Laptop `GPU-14f08a8c-8d62-4338-8ae4-c669889cdb29`；较大图像/MTP 使用 RTX 5060 Ti `GPU-5847813c-9e6e-bb43-cc5e-621aac091b6c`；双卡使用上述两张实际设备。没有性能实验。

## 转换、真实格式与交付入口

- **16 项 CPU 转换测试通过**（5.91 秒），覆盖三格式 golden bytes、选行、BF16 GDN 边界和 PQ2_0/PTQ1_0 ternary 编码。
- 增加固定 Bonsai 27B 的 `bonsai2_27b_ternary_gguf_mtp` recipe，支持独立 PTQ1_0/PQ2_0 文本来源和现有 GGUF MTP。复用并严格校验 dense MTP schema、形状和格式，保留原编码导入及 attention/MLP 分组。
- 实际 PTQ1_0 文本 + Q4_0 MTP 转为 v3 制品，790 个对象；9 个 MTP 矩阵的 36 个取样行与 GGUF 原始编码字节一致，含 interleaved 行映射。五个 norm 实际与 HF companion 比较，证实该 Bonsai r3 GGUF 存储值需减 1（F16 对照最大误差约 0.000488）；这一约定限制在该 recipe，未推广到任意 GGUF。
- 该真实 Q4_0 MTP 制品已用于上述 Graph/MTP/ngram、C8 和双卡文本测试。未通过修改制品标签来模拟格式。**Q2_0/Q5_0 没有本地真实模型样本，仅完成编码、制品互操作、物化与独立 GPU 算子验证。**
- 新 writer 的三格式制品到新 reader/materializer 通过。`qwen4_exp` 的 metadata-only 负向夹具在加载权重前被 KVMem 明确拒绝；它不是实际模型验证。
- 最终 worker 的 PE 依赖递归闭包为本地 10 个文件（含两个 cuBLAS DLL）。清理 PATH 后 `--help` 及实际 HTTP `/health`、`/props`、生成、`/stats` 通过；显式 KV 容量 256/B128/R128/max_context2048 生效，生成 `1, 2, 3, 4, 5`，Ctrl+Break 正常退出码 0。没有替换生产服务。

## 实际失败与修复

以下问题均保留证据并说明来源，没有把本次适配回归归因于旧 KVMem/llama.cpp。

| 问题及来源 | 触发、修复和证据 |
|---|---|
| M1 构建兼容 / 上游 | MSVC 的 C++20 `shared_ptr::unique()` 与 NVFP4 嵌套 lambda kernel 别名实际编译失败，分别改为等价 use_count 和直接启动同一 kernel。前者有状态测试；后者仅编译/链接验证，该快速 prompt 数值路径未独立运行。M1 完整记录见计划。 |
| Vision 接口迁移回归 | 新 bind 需要 hidden size；传入实际 text hidden size，worker 与两种 Vision 核心运行通过。 |
| HTTP 接口迁移回归 | 新多模型 lease 已取代旧 `service_`；props 改用选中模型的 `lease.service()` 和解析后 options，最终实际 HTTP 验证通过。 |
| 上游测试在 Windows 的命名冲突 | `near`/`far` 宏使资源/frontend 测试编译失败；仅改局部名称，原断言通过。 |
| 显式 KV 容量迁移回归 | 最终 worker 的 max_context2048/kv_capacity256 被新上游的普通原生长上下文检查提前拒绝。KVMem 改由原有 C×(B+R) 规则校验；新增 C1/C2 有效显式容量和不匹配拒绝的聚焦参数测试通过，最终 HTTP 运行通过。 |
| 独立准备副本的换行设置 | 首次手工克隆继承 CRLF，导致补丁/完整树检查失败；恢复固定 Git blob 的精确字节并关闭该副本的 autocrlf。最终 LF 补丁从干净基线准备与校验通过，不是源代码逻辑问题。 |
| M1 实际显存/预算不足 | 初始 Legacy 零 Host 配置及 Hybrid/Graph 预留被正确拒绝；选择有效的小保留槽配置完成原生短生成，没有改变预算规则。后续 KVMem Graph/MTP 和双卡测试已完成。 |

**没有确认新的旧 KVMem/llama.cpp 逻辑缺陷，因此没有需要用户决定保留或改变的旧行为。**

## 未扩大验证的范围

没有跑全套 CTest、18 格式全宽度矩阵、KV×MTP×Graph×Vision×并发×设备全排列、超长上下文档位扫描或性能比较。九种 KV 的容器/原始字节覆盖不等于九种真实模型全流程；运行覆盖以 INT8 和 RK8V4 的代表组合为准。未新测 llama.cpp/NVMe、DFlash、overlay Vision、并行 prefill、strict 自动 OOM 恢复矩阵或新 qwen4 架构；没有新增这些能力声明。远端 CI、独立 CLI 可执行文件和快速 NVFP4 prompt 数值路径也未运行。

## 本机证据

以下为操作者本机 `ninfer-all-migration-20261007/` 目录内的证据路径；原始结果本地留存，不是本代码分支中的文件。

| 路径 | 内容 |
|---|---|
| `m0-snapshot/capture.json` | 原父仓库和两个后端的恢复快照、HEAD、index 与工作区文件哈希 |
| `m1-results/report.json`、`m2-results/rebase/` | Ryan 来源、M1 检查、三方适配输入与冲突记录 |
| `core-results/*.json`、`*.log` | 25 个程序的命令、GPU UUID、运行前占用、退出码与原始日志 |
| `m5-results/conversion-final-tests.log` | 16 项 CPU 测试 |
| `m5-results/mtp-norm-source-check.json`、`real-q4-raw-row-check.json` | 真实来源 norm 与编码行证据 |
| `m6-results/frozen-candidate.json`、`preparation-checks.json` | 最终补丁/完整树与独立准备校验 |
| `m6-results/quant-increment-disposition.json` | 21 个量化增量文件的最终去留与上游字节一致性 |
| `m6-results/writer-and-family-guard.json` | 新制品互操作和 metadata-only 架构拒绝 |
| `m6-results/worker-dll-closure.json`、`final-worker-http.json` | 最终 binary/DLL 哈希、清理 PATH 后 HTTP 与正常退出 |
| `m6-results/original-inputs-audit.json`、`report.json` | 原源码/index 保留审计与最终验收汇总 |

## 后续真实 Q2 制品和本地连接验证

同一最终 worker 已在 RTX 5060 Ti 上运行 [ModelScope 的 Q2LynnStyle-MTP 制品](https://www.modelscope.cn/models/Merkyor/Qwen3.8-27B-Coder390-EfficientThink-Opus5.5-GPT6Astra-Grok4.7-DSV4Pro-K3-SFT-RLOO-GGUF-NInfer/tree/master/GGUF-NInfer)。下载源固定为仓库 revision `4fc03123dec322d855458de3831fc7b5788fb863`，文件为 `GGUF-NInfer/Qwen3.8-27B-Coder390-EfficientThink-Q2LynnStyle-MTP.ninfer`，大小 13272798720 字节，SHA-256 为 `cdd82b69027faba0bda619224ee851ccd0ecf0dfb176bad85a17d2df07926921`。

该模型的 Q2 名称表示混合精度：文本包含 IQ2_S、Q2_K、IQ3、IQ4 等格式，MTP 矩阵为 Q4_0；**不含 Q2_0**。这项结果补充真实低比特模型和 Q4_0 MTP 的运行证据，不能替代 Q2_0/Q5_0 的真实模型验收。

两个定向配置均采用 C1、max_context2048、B128/R128、物理 KV 容量 256、H1024 MiB、prefill128 和 FP16 GDN，冷请求与精确重复通过，KV/GDN 字节校验通过，进程正常退出：

- INT8 KV/eager、关闭 MTP/ngram：输入 765 tokens、输出 30 tokens；重复复用 758 tokens，输出一致。
- RK8V4/Graph、MTP3/ngram15：输入 33 tokens、代码输出 84 tokens；MTP 草稿 66 tokens、接受 64 tokens，重复输出一致。

后续按原 IQ3 服务参数启动 Q2，使用 INT8 KV、Graph、固定 MTP4、关闭 ngram、max_context204800、C1、B49152、H6144 MiB、最大输出32768。原 R32768 在单卡 INT8 配置下被启动容量检查拒绝：运行时需 3373348352 字节、可用 2650800128 字节；调整为 R8192 后正常启动，输出上限仍为 32768。按用户要求将 sink/recent 各设为 10240 tokens，实际 `/props` 校验通过。

DSH 默认连接指向该 Q2 模型。使用已安装的 DSH 适配器完成两次短流式请求，验证工具调用、模拟工具结果与 assistant replay。此检查覆盖模型接口和工具协议，**不覆盖真实 PowerShell 执行器**。随后独立诊断发现 DSH Desktop 0.2.0-rc.2 的 Windows 沙箱 PTY 启动失败；普通 PowerShell PTY 和带有效输入管道的同一只读沙箱命令可执行，沙箱 PTY 子进程以 127 退出。具体原生调用尚未确认，未改动 DSH 代码或文件保护规则，也没有把它归为 KVMem 推理回归。

后续原始证据在操作者本机 `q2-real-model-20261007/`：`test-results.json`、`service/live-check.json`、`dsh/adapter-verification.json` 和 `dsh/pty-diagnosis-summary.json`。没有运行性能测试或新增长上下文扫描。

## 2026-10-17：Qwen3.8-27B 上游刷新（本地验收完成）

工作分支 `feat/ninfer-all-27b-refresh` 基于 master `27a221e`，将 NInfer 固定到
`08fd7019925f43b4b42539400afd461b70f7144a`。本轮只验证 Qwen3.8-27B Dense，保留 LCP、
LRFU、RAM/SSD、MTP/ngram、工具和 reasoning 补丁及公共 CLI。35B MoE、Flash-Next、Gemma 4 的
KVMem 接入不在本轮范围；独立模型族及 host/disk expert 与 KVMem 的未适配组合明确拒绝。

原始 172 文件补丁三方应用后有 39 个文本冲突；退役/移动文件不恢复。上游 `unique_strings`
已演进为 `string_constraints`，工具语法直接使用新类和新构造参数。原生模式保留本地 mixed/strict
驻留策略（KVMem 仍只允许 default），普通分配采用上游新的 CUDA/WDDM 实现；未恢复旧 D3D12 arena。

两套程序使用相同 VS2022/MSVC 14.44、CUDA 13.2.86、Release `120a`/SM120 native，8 并行构建。
原版引擎源码未改；原版 benchmark 只通过公开 Aggregate observation 启用阶段计时，并在 JSON
增加已有 wall-time 字段和输出 token，测量方式与整合版相同。CPU 转换检查 11 项已通过；
原版和整合版 worker、benchmark 均已完成编译。24 组定向运行检查的最终结果全部通过，
覆盖 CLI/serve 参数、容量、字符串约束、工具语法、frontend、耦合 MTP、Chat/Responses、
load report、mean-K、INT8 prompt 与量化 attention 深度、历史恢复、LCP/SSD、随机采样 MTP，
以及 RAM/SSD C2 并发。量化 attention 使用独立数值参考，覆盖 head16/24、8192/61440 深度及
边界；真实历史测试启用原生 KV/GDN 字节校验。实际 HTTP health、props、models、生成、
重复生成和 stats 通过，worker 正常退出；重复生成复用 31/32 个输入 token。

本轮实际失败及处理：

| 项目 | 结论与验证 |
|---|---|
| 工具结构化编译 API 变化 | 改用上游 StringConstraintState 和新的 GrammarState 构造参数；完整 worker 编译及结构化/工具测试通过。 |
| GDN GGUF 控制矩阵 | 适配初版遗漏旧补丁对 Q2 等已注册 GGUF 格式的支持；测试改为直接调用生产 gdn_control_source 后复现失败，统一解码入口并删除重复旧 helper，11 项转换测试通过。 |
| 图像 detail 协议 | 保留原 KVMem 已支持的 original→auto 别名，采用上游 low/high 的实际语义；内部 unknown 状态改用 optional 区分空字符串与缺失，严格模式拒绝空/未知值，Chat/Responses 检查通过。这里只验证协议，不声明本轮真实 Vision 运行覆盖。 |
| 取消 prefill 后的旧测试 | 旧主线 LCP binary 同样复现“必须零复用”断言失败。实际完整检查点在 frontier41，取消处理到128；prefix/payload/state 均精确。测试允许合法完整检查点，要求复用不越过已处理输入，完整历史回归通过；未修改生产取消/恢复行为。 |
| SSD 冷请求的合批断言 | H64 MiB、D512 MiB 下异步读取使冷短回复错开，不能要求冷轮必然合批；冷轮仍检查状态、输出隔离和实际 SSD 写入，热会话仍要求实际合批及取消隔离。最终 cancel-one 为52轮/53行，resume-peers 为11轮/18行，SSD 读约1.02 GB、写约1.40 GB，无错误，资源释放通过。 |

功能性检查按空闲卡选择：小夹具与独立数值检查使用 RTX 5050 Laptop；实际 27B 模型使用
RTX 5060 Ti。没有把功能运行期间的耗时作为性能结果。

性能方案固定使用 RTX 5060 Ti（`GPU-5847813c-9e6e-bb43-cc5e-621aac091b6c`）和同一 IQ3_S 文件，
INT8 KV、prefill256、Graph。原版 logical ctx 和 Main KV 容量均为 B+G；整合版使用相同 B、G、
逻辑长度和实际 Main KV 容量。输入 P=B，每次请求 257 个输出（首 token 后计时 256 个），
不触发容量换页。先测关闭 MTP，再测固定 MTP4/ngram0；自适应 MTP/ngram31 单独列出，
因为原版校验语料中 248 个 ngram 草稿全部命中，不能把这项复制效果解释为神经 MTP 的普遍速度。

主要指标为 `256 / generation_wall_seconds`；同时保留 Program decode、prompt wall 和整轮耗时，
避免只计算 Program 执行时间而漏计 Engine/KVMem 工作。每组预热 1 次、正式重复 3 次；
昂贵的 transfer verification 与长历史换页检查独立运行，不进入同输入性能结果。

两版均用 `--device-profile auto`，实际选择相同的上游内置 RTX 5060 Ti/180 W/2026-10-16
配置（78 routed keys，catalog `5893cf72e3d60377`）；本机旧 catalog 的个人配置被两版同时忽略。
关闭 fast-prefill，保持 CUDA Graph；MTP 有效草稿宽度固定为 4，ngram0，不启用 attention window。
KVMem 为 H12288 MiB、sessions1、LRFU、score-agg=max，不启用 SSD；小预算 sink/recent 各512，
默认预算 sink12288/recent1024。普通组先测原版、MTP 组先测 KVMem；所有编译和前置功能测试
已结束，无其他模型同时占用该卡。每个请求都重新处理输入，禁用前缀复用和上下文保留。

### 同输入、同激活历史的结果

下表为生成 wall throughput 的三次均值，括号内为最小–最大值，单位 token/s。

| B / G | 推测设置 | 原版 | KVMem | KVMem 速度变化 |
|---|---|---|---|---|
| 2048 / 512 | 关闭 MTP | 31.606（31.602–31.609） | 31.425（31.415–31.433） | −0.57% |
| 2048 / 512 | 固定 MTP4，ngram0 | 88.646（88.490–88.732） | 87.620（87.503–87.736） | −1.16% |
| 36864 / 16384 | 关闭 MTP | 28.989（28.984–28.991） | 28.750（28.726–28.786） | −0.83% |
| 36864 / 16384 | 固定 MTP4，ngram0 | 80.824（80.789–80.845） | 79.979（79.963–80.012） | −1.04% |
| 2048 / 512 | 自适应 MTP4，ngram31（复制语料，单列） | 314.003（313.335–314.812） | 309.292（308.718–309.634） | −1.50% |

| B / G、推测设置 | 输入 wall token/s：原版 → KVMem | 输入速度变化 | 整轮秒数：原版 → KVMem |
|---|---|---|---|
| 2048 / 512，关闭 MTP | 794.112 → 774.234 | −2.50% | 10.679 → 10.792 |
| 2048 / 512，固定 MTP4/ngram0 | 784.460 → 768.017 | −2.10% | 5.499 → 5.589 |
| 36864 / 16384，关闭 MTP | 715.430 → 665.985 | −6.91% | 60.361 → 64.265 |
| 36864 / 16384，固定 MTP4/ngram0 | 711.655 → 663.671 | −6.74% | 54.971 → 58.754 |
| 2048 / 512，自适应 MTP4/ngram31 | 783.567 → 766.588 | −2.17% | 3.446 → 3.518 |

五个配对的实际 KV 容量、KV payload 字节数、输入和输出数量均相同，每个配对的全部六次
输出 token 序列一致。小预算容量2560，普通/MTP payload 为86507520/92049408字节；
默认预算容量53248，普通/MTP payload 为1799356416/1911951360字节。
另行诊断确认未换页时 `history == logical`，从 P=B 的历史逐步增长，
两版同时保留相同的 MTP lookahead。G 是物理容量预留，不表示每步已经激活了 B+G 个历史键。

四个普通/神经 MTP 对照的生成速度差为 −0.57%～−1.16%；最明显成本在默认预算的冷输入处理，
慢6.74%～6.91%，整轮多约3.8～3.9秒。没有把这些较小生成差异称为噪声，也没有声称开销为零。
这里测的是开启 LRFU、统计与历史管理后的整体差异，尚未逐项 profiling 分解原因。

复制语料的 ngram 每次请求运行8次验证、248/248草稿命中；其300+ token/s不能推广为一般对话或纯神经
MTP 速度。该组 Program decode 指标原版314.25、KVMem321.98，看似变快，但实际 generation wall
为314.00、309.29，故以实际 wall 结论报告为慢1.50%。

### 独立换页与边界

同一新版自动 device profile 下，额外使用 B2048/G512、P4096、固定 MTP4/ngram0 做诊断，
实际发生5次换页，原生 KV payload 与 GDN state 校验均精确；逻辑 frontier4349 对应紧凑 history2045，
保持物理容量2560。它与上表输入不同且启用昂贵校验，只证明状态/位置正确，不纳入性能配对。

本轮是固定 raw-token 语料、INT8、单5060 Ti的代表性比较，不包含多轮聊天命中收益、长历史选择质量、
其他 KV 格式、Vision、多卡、35B/MoE/新模型族或 Linux 的重新验收，也未运行远端 CI。
没有替换现有运行服务或自动合并 master。llama.cpp 和公共 KVMem core 本轮没有修改。

本机源码、构建日志和原始结果位于 `ninfer-upstream-20261017/`，不随源码提交。
`validation/results.json` 保留失败及修复后结果，按配置名称取最后一次结果；旧 binary 的取消复用失败
和冷 SSD 未重叠日志也单独保留。`performance/comparison.json`、各组 JSON、metadata/命令及 GPU 采样
保存完整分布和条件。原版仅报告插桩的精确差异在 `baseline-report-only.patch`；其引擎源码未修改。

## 2026-10-17：普通工具请求默认不附加语法掩码

同一 5060 Ti、27B IQ3_S 模型和 RC3 发布时的 33 个请求，已在刷新后的 worker 完整回放一次。
按用户要求没有重跑 RC3；旧值取 10 月 13 日的发布记录。上下文262144、B36864/G16384、H12288MiB、
INT8 Main/MTP、FP16 GDN、固定MTP3、prefill512、thinking128、LRFU sink/recent各5120、device profile off。
每次输入 token 数均与历史记录一致，32/32 工具请求复用前缀，末轮输入261522、输出512。

| 原完整回放指标 | RC3历史记录 | 刷新后、默认约束尚未关闭 |
|---|---:|---:|
| 32轮工具请求合计生成速度 | 53.24 tok/s | 47.18 tok/s（−11.40%） |
| 末轮输出速度 | 44.87 tok/s | 26.42 tok/s（−41.11%） |
| 草稿接受率 | 63.28% | 62.44% |
| 总输出 tokens | 15293 | 13872 |
| 33次请求总耗时 | 855.10s | 857.35s |

总耗时接近不能说明速度持平，因为新版少输出9.29%。两版生成内容不同：旧末轮开始输出代码，
新版末轮在512 tokens内仍主要逐项确认文件，不能把末轮速度当作相同代码的生成比较。
只有一次历史/一次新版样本，机器后台活动和内存压力不同，未把所有降速归因于单一机制。
原始记录保存在 `ninfer-upstream-20261017/task2-256k-rc3-recipe/`，后续默认值调整不覆盖该结果。

上游提交 `8192df5176c22f52f4e5916379626d0663a0fb6f` 新增 `GrammarMaskCost`：语法掩码耗时的指数
平均（alpha0.25）超过4ms时，`speculative_extent()` 返回0，恢复后重新带草稿；它独立于自适应MTP开关。
本地工具约束早在 `eb98424`（10月9日）已加入，RC3源码也包含它。上游08fd701有通用结构化输出
掩码框架，但其普通工具路径没有本地这套强约束。新上游门控与现有本地工具语法组合，导致普通工具
请求也会进入这条自动裁剪路径，并非两套掩码重复计算。

仅加逐轮观察的诊断版本，在两次3222-token短请求中记录45个零草稿轮次，其中40个是在已有草稿、
预算和上下文均足够时被语法成本门控设为0。17/33请求的诊断回放记录350个零草稿轮次，其中333个
同样由门控触发；随后用户要求先关闭默认附加计算，停止了该诊断。它不是第二次完整256K测速。
未观测到原始末轮90次的逐轮原因，不能把这90次全部追溯标成同一种原因。诊断计数随计时和机器负载
变化；诊断日志不用于替换原性能结果。代码同时显示，零草稿验证轮次仍计算后续MTP提议，因此它不等同于
切换到纯普通解码。同步等待指标也可能包含CPU掩码上传事件的等待，不能全部解释为GPU内核计算。

用户随后要求先关闭本地工具约束计算。本轮将普通工具默认改为现有 `tool_constraints:"auto"`：
非strict的普通auto请求不附加工具语法；none请求不创建工具语法、也不发布工具调用，但不再逐token
禁止文本中出现工具标签。工具定义继续留在可复用提示词中。显式basic、strict工具、required/named/allowed
选择和并行调用限制仍按请求保留对应保证，显式JSON/JSON Schema继续使用上游语法框架；none加JSON
只允许JSON，不保留工具调用备选分支。空参数归一化、工具解析和思考阶段修复继续保留。

本轮源码在 `fix/ninfer-tool-constraints-default`，未提交或推送。完成下述功能验证时，关闭后的完整256K
测速尚未重跑；随后按用户要求完成的回放结果见后节。功能验证保存在 `ninfer-upstream-20261017/mtp-diagnosis/`。

### 关闭默认约束后的验证

Windows/MSVC14.44、CUDA13.2、SM120a Release 构建完成，最终使用4路并行；构建目标只包含
`ninfer-serve` 和本次涉及的三个测试目标，未构建其他后端。修改公共默认配置导致其依赖的 CUDA
代码重编译；本轮没有修改 KV 数学、MTP 接受算法或上游4ms门控。新增测试最初有一个构造辅助函数
引用错误，修正后编译通过；旧的强制调用边界测试随后改为显式请求对应约束，初次失败记录保留。

六组行为回归全部通过：Qwen前端、工具解析、结构化输出、工具约束、Chat请求协议和Responses请求协议。
前端覆盖真实特殊工具分隔token、默认无grammar、空参数省略、none不发布调用、显式basic/required
保留grammar，以及none加JSON拒绝工具分支。约束测试继续覆盖strict参数、allowed/parallel限制和
不完整调用边界，没有通过放宽这些显式契约来取得通过。

同一5060 Ti（`GPU-5847813c-9e6e-bb43-cc5e-621aac091b6c`）和27B IQ3_S的六个真实HTTP请求全部通过。
三次默认none请求均为3222输入、512输出，原生请求日志中的零草稿轮次分别为2、2、1，均实际生成了
MTP草稿，未再出现短请求诊断中的频繁零草稿现象。普通auto请求成功解析一次工具调用；none加显式
JSON Schema得到合法的 `{"ok":true}`；强制strict工具调用得到 `ping({})`；此后再运行none也通过。
思考预算和空参数处理仍保留，测试服务已停止。

这组六请求使用B36864/G16384、H1024MiB、固定MTP3，属于功能回归；H小于完整回放的12288MiB，
也没有重放256K历史，因此不把其短请求速度与原完整回放配对或宣称完整任务性能已恢复。
完整回放原worker已另存，`task2-256k-rc3-recipe/binary-archive.json` 记录路径和原始哈希，
原始测速结果未覆盖。后端补丁已重新导出并通过固定上游源码的严格准备检查。

### 同日18:03：关闭默认约束后的完整256K回放

按用户要求，仅在新版worker重跑一次原33个请求，未重跑RC3。5060 Ti、同一27B IQ3_S模型、
模板、B36864/G16384、H12288MiB和其余启动参数均与前次完整回放一致；没有诊断插桩或额外结构化输出
开关。33个请求内容及输入token数逐一相同，32/32工具轮次前缀命中，末轮261522输入、512输出。

| 指标 | RC3历史 | 新版，关闭前 | 新版，关闭后 |
|---|---:|---:|---:|
| 32轮工具合计生成速度 | 53.24 tok/s | 47.18 tok/s | 54.50 tok/s |
| 261K末轮输出速度 | 44.87 tok/s | 26.42 tok/s | 57.96 tok/s |
| 末轮首token等待 | 16.87s | 17.13s | 16.86s |
| 全部33请求零草稿轮次 | 31 | 150 | 31 |
| 末轮零草稿轮次 | 1 | 90 | 2 |
| 总输出tokens | 15293 | 13872 | 7711 |
| 33请求总耗时 | 855.10s | 857.35s | 705.57s |

关闭后本次工具合计生成速度较关闭前高15.52%，末轮高119.36%；相对RC3历史分别高2.36%、29.19%。
合计按累计生成tokens除以累计生成时间计算，不把各轮tok/s做算术平均。输入处理有效速度为470.15
tok/s，关闭前471.41、RC3历史469.64；首token等待基本不变。频繁零草稿现象在本次完整回放中消失，
没有把正常的少量零草稿轮次解释为工具掩码门控仍在频繁触发。

工具轮次草稿接受率依次为63.28%、62.44%、59.93%。关闭后末轮接受323/483个草稿（66.87%），
有161个带草稿轮次及2个零草稿轮次。接受率分母只含实际提议的草稿；关闭前末轮92.97%的接受率
伴随90个零草稿轮次，因此不能单独用接受率排序推断速度。末轮原生decode时间18.99s降至8.57s，
device-wait指标18.57s降至8.27s；这些整体指标仍不能逐项分解CPU、GPU和输出内容变化的贡献。

行为代价也实际出现：关闭后21/33个请求把工具调用标签当普通文本输出，服务发布的工具调用数为0。
这些请求包含 `tool_choice:none`；关闭默认禁止语法后，它不再阻止模型输出工具标签。关闭前及RC3
记录均没有这种文本标签输出。总输出较关闭前少44.41%，总耗时减少17.70%包含输出变短的影响，
不能全部算成纯性能收益。末轮虽输出512tokens，仍在列批次确认清单，未进入代码；本测试不证明
代码任务完成或三版输出语义等价，也没有把这项行为变化隐去。

新回放峰值显存14445MiB，与关闭前相同；最低可用RAM约500MiB，关闭前283MiB，RC3历史9MiB。
各版本各一个样本，后台活动与输出内容不同，结果限定于本次工作负载，不作一般性能保证。
完整原始请求、输出、SSE、原生日志、2秒采样以及三方JSON/CSV对照保存在
`ninfer-upstream-20261017/task2-256k-default-auto/`。测试worker已停止，旧结果未覆盖。

### 最终实现：取消掩码耗时门控，恢复默认工具约束

用户确认掩码计算耗时不应成为是否验证草稿的条件。本次删除共享4ms阈值、耗时平滑状态和
Qwen3.5、Gemma4、Flash-Next三个核心中的对应草稿裁剪条件；没有提供另一个阈值或开关。
草稿可用数量、输出预算、上下文容量及显式配置的推测策略继续约束验证宽度。掩码本身、按草稿位置
并行计算、CPU/GPU重叠和采样前等待有效掩码仍保留；普通非推测路径仍预计算下一掩码。
原有5秒掩码失败预算独立保留，Qwen推测轮次的完成检查也处理该预算，避免取消门控后漏掉失败检查。

此前关闭默认工具语法的实验已回撤：默认重新使用basic约束，none禁止生成工具标签；显式auto
轻量解析接口保留。没有修改参数名、KV算术、接受概率算法或复用提示词的工具定义。
异步计算仍可与GPU工作重叠，但慢掩码可能延长推测批次的采样等待；取消门控不意味着复杂语法的
多路并发隔离性能保持原样。本机验证以27B单请求为主，另外两个核心尚无本轮真实模型测试。

服务与相关测试已完成4路编译。八组行为/GPU回归全部通过，包括前端none标签禁止、工具解析、
工具语法、Chat/Responses协议、JSON、掩码图回放与推测采样。5060 Ti上的七个27B IQ3_S HTTP
请求全部通过；三次none请求均为3222输入、512输出，无工具标签，零草稿轮次2、2、1，普通、
显式auto、强制strict工具及JSON Schema仍正常。

19:25完成同一33请求256K完整回放。模型、模板、GPU UUID、B/G/H及其余启动参数不变，33份请求
逐一相同，输入token数差均为0，32/32工具轮次前缀复用，末轮261522输入、512输出。没有重跑RC3。

| 指标 | RC3历史 | 新版约束开启、4ms门控开启 | 最终约束开启、无耗时门控 |
|---|---:|---:|---:|
| 工具轮次合计生成速度 | 53.24 tok/s | 47.18 tok/s | 47.27 tok/s |
| 261K末轮输出速度 | 44.87 tok/s | 26.42 tok/s | 49.23 tok/s |
| 末轮首token等待 | 16.87s | 17.13s | 17.42s |
| 全部请求零草稿轮次 | 31 | 150 | 39 |
| 末轮零草稿轮次 | 1 | 90 | 2 |
| 普通文本中的工具标签请求数 | 0 | 0 | 0 |
| 总输出tokens | 15293 | 13872 | 14322 |
| 全部请求耗时 | 855.10s | 857.35s | 864.82s |

末轮零草稿从90次降至2次，生成速度高86.31%；这证实取消门控在该末轮恢复了草稿验证，但没有
解决整体吞吐差距。工具加权速度仅高0.20%，32轮中27轮变慢、5轮变快，单轮速度变化中位数−5.29%，
最差82K轮次−12.46%（该轮输出512→226，内容长度也不同）。当前单轮速度范围34.52～60.78 tok/s，
中位数45.94。相对RC3历史，合计仍慢11.22%，24/32轮慢，最差155K轮次慢41.38%（输出512→64）。
这些下降没有当作噪声略去；尚未分解CPU掩码、调度等待、GPU和输出变化各自的贡献。

本次总耗时比开启门控时多0.87%，总输出也多3.24%，末轮首token等待多1.69%；不能声称整个任务
普遍加速。此前无约束实验54.50 tok/s的合计速度高于当前，但它有21个请求泄漏工具标签、总输出
仅7711。本次恢复约束后33个请求均无标签，发布工具调用数为0。两种无门控版本末轮的content和
reasoning文本完全相同，草稿接受计数同为323/483、带草稿161轮、零草稿2轮；保留约束的本次末轮
仍慢15.07%，说明取消4ms策略没有消除实际约束开销。这不是同机交替多次AB试验，不能把差额全部
归因到某一实现步骤。末轮仍在列批次确认清单，没有输出代码，不证明代码任务完成。

本次工具草稿接受率64.07%，末轮66.87%；末轮原生decode时间10.16s，device-wait指标9.87s。
峰值显存14445MiB；最低可用RAM501MiB；worker峰值RSS13885MiB、private commit29626MiB。
相比开启门控的原回放，峰值RSS从12046MiB增加到13885MiB，private commit相近；这些资源差异和
后台活动均保留在采样中。单次回放不能作为所有模型、硬件或复杂语法并发性能的保证。

完整四版本对照、逐轮CSV、原始输出、原生日志、采样与最终输出一致性检查位于
`ninfer-upstream-20261017/task2-256k-no-mask-cost-gate/`。测量worker已停止，程序另存并核对哈希；
源码补丁准备与格式检查通过。改动仍在本地 `fix/ninfer-tool-constraints-default`，未提交、推送。

### 当前选择：默认 auto／none 轻量路径，保留无耗时门控（2026-10-17）

用户选择先采用简单实现，不增加专门的标记状态机。默认 `tool_constraints` 改为 auto：普通非strict
auto请求保留原生工具识别与参数归一化，不附加本地工具语法；none请求保留原始工具定义以复用输入
前缀，但不创建禁止语法、不发布结构化工具调用。模型仍可能把工具标记输出成普通文本，这是已接受
的行为限制，不将“无tool_calls”等同于“答案不含工具标签”。此前21/33次标签输出的历史测量仍保留。

显式basic模式继续提供完整工具语法，包括none的禁止语法；required、指定工具、allowed子集、
parallel限制和strict等明确约束不被默认auto绕过。none加JSON格式只建立JSON约束，不提供工具调用
备选分支。4ms门控保持删除，5秒语法失败预算保留，不增加新的CLI或UI开关。

实现仅改变工具约束选择与对应的前端测试、说明，不增加采样过滤器，不修改KV运算或MTP接受算法。
最初以4路编译，用户随后要求改为8路，保留已完成结果后以8路续编，服务与相关测试最终编译通过。
旧构建中的DB16未使用变量警告仍存在；没有新增编译错误。

首轮前端回归发现底层PromptOptions.forced_tool_name的显式强制前缀也被默认auto带入了普通解析
路径，截断调用因而回到原生修复行为，违反本次保留明确强制约束的要求。已修正选择器，使该显式
强制前缀继续启用约束，并保留完整、缺少外层闭合、参数截断三类边界测试。这是默认值修改引入的
遗漏，不将原生宽松解析本身判定为缺陷。另一次运行发现聚合测试入口未包含Anthropic协议测试，
补入已有测试程序后重跑；两次初始失败记录均保留。

最终九组回归全部通过：前端、原生工具解析、JSON Schema、工具约束、Chat、Responses、Anthropic，
以及结构化与推测GPU采样（RTX 5050 Laptop）。RTX 5060 Ti上Qwen3.8-27B IQ3_S、MTP3、单请求、
H1024MiB的八个实际HTTP请求全部通过，覆盖默认auto、显式auto、默认none、none加JSON、强制strict、
强制之后切回none和显式basic。三次none均为3222输入、512输出，tool_calls为空，零草稿轮次为
2、2、1，均有实际草稿接受；第二次复用3221个输入token。三次短请求未出现工具标签，但默认none
允许模型输出标签文本的限制仍然存在，不能用这些短请求否定此前21/33次的长任务结果。

本次属于行为回归，不重跑RC3或完整256K性能任务，也未运行其他模型或复杂并发性能矩阵。
HTTP脚本中的短请求速度不作为新的性能对照；旧无约束测量不能当作当前二进制的新性能数据。
证据位于mtp-diagnosis/auto-none-no-gate-checks和auto-none-no-gate-http。测试服务已主动停止，
stopped.json的退出码1来自Windows测试清理，不是请求崩溃；程序另存并核对哈希。补丁准备和格式
检查通过；本批源码补丁、版本清单和行为说明一起纳入提交，验证范围与已知限制如上。

# KVMem 多后端运行

本文对应 `master` 的 0.18.0 多后端源码，包含活跃 KV 的 RAM/SSD 分层。Windows NInfer [0.18.0 正式版](https://github.com/kvmem/kvmem/releases/tag/v0.18.0)基于 RC3 引擎，默认 sink=12288 tokens（12K）、recent=1024 tokens（1K）。两后端参数差异见 [实现差异](multi-backend-differences.md)，历史验收范围见 [分层验收记录](active-kv-tiering-validation-20261010.md)。

0.18.0 源码采用 `iamwavecut/ninfer-all` 的固定提交 `8319e8f51247`，llama.cpp 基线保持原值。当前迁移验收进度见 [迁移记录](ninfer-all-migration-validation.md)；下文各有日期的实验是旧基线证据，不能视为新基线已经完成同等覆盖。KVMem 暂不支持新增的 `qwen4_exp`、模型 suspend、prompt graft 和 router，开启这些组合时明确报错。

Windows 入口 `scripts/windows/start-backend.ps1` 在启动时选择 `llamacpp` 或 `ninfer`。两个 worker 分进程运行，各自拥有推理引擎和 GPU 内存；公共 KVMem 策略库直接链接在 worker 内。

切换后端时的参数、默认值和执行行为区别见 [两后端实现差异](multi-backend-differences.md)，包括 ninfer 固定 64-token block、recent 默认且至少保留一页（参数至少 64 tokens）、R 与输出上限，以及 Host 和磁盘预算的不同含义。

## 启动

ninfer 预编译包可直接运行默认脚本。将包含文本、视觉和 MTP 组件的模型放到包根目录，命名为 `model.ninfer`；包和模型的完整路径不能含中文字符。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows\start-ninfer.ps1
```

默认端口18200，context200k、budget36k、gen reserve及输出上限16k、思考预算8192、双INT8、自适应MTP上限4、ngram31、CPU视觉（图片上限1024 tokens），Host预算12GiB。脚本调用统一启动器，可编辑模型路径与预算，通过 `-Gpu` 选择设备。小预算CPU/resident视觉的实际1024-token图片及八路图文功能已通过，范围见 [视觉并发验收](vision-concurrency-validation-20261006.md)；正式版在 RTX 5060 Ti 16GB 上完成了该默认配方的加载和短 Responses 请求检查；该检查不代表所有模型或完整长上下文质量已验收。各历史记录保留其原有测试范围。

解压包后，在 PowerShell 中运行：

```powershell
./scripts/windows/start-backend.ps1 -Backend ninfer `
  -Model 'D:/models/model.ninfer' -Gpu 'GPU-your-device-uuid' `
  -Context 262144 -Budget 2048 -Reserve 512 -Prefill 512 -HostMiB 12288 -SinkTokens 64 -RecentTokens 64
```

`-Worker` 可指定本地构建的 `ninfer-serve.exe` 或 `llama-kvmem-server.exe`；省略时使用包内 `workers/<backend>/` 的程序。`-DryRun` 输出启动参数，`-Describe` 查询静态能力。默认监听 `127.0.0.1:18200`，前台运行，Ctrl+C 结束本次启动的 worker。安装 NVIDIA 驱动及 Microsoft Visual C++ 2015–2022 x64 Runtime；包内保留各引擎的 DLL，模型另行提供。

当前源码的 Windows 统一入口、`start-ninfer.ps1` 及 llama.cpp 的 IQ3/IQ4 入口默认开启 LRFU，`lambda=0.5`、`top-frac=0.9`，GQA 聚合使用 `max`。sink 默认 12288 tokens（12K）、recent 默认 1024 tokens（1K，16页），两项计入 B；统一入口默认 B=32768，ninfer/IQ3/IQ4 快捷配方的 B/R 保持原值。可用 `-SinkTokens`、`-RecentTokens` 覆盖；自定义小预算时也要相应缩小这两个保留窗口。`top-frac` 控制本轮得分可累加到历史的命中范围，不是 KV 预算比例；同分块也可命中。可用 `-LrfuLambda`、`-LrfuTopFraction` 和 `-ScoreAggregation mean|max` 覆盖；`-NoLrfu -ScoreAggregation mean` 恢复无历史加权的 mean 检索。`start-ninfer.ps1 -DryRun` 可查看完整参数。直接运行 worker 时仍默认关闭 LRFU、使用 mean，sink/recent 的公共输入默认值为 0/64，再按本后端块粒度规范化；需要包含 LRFU 实现的新 worker，已发布的旧 ZIP 不适用这些参数。

当前源码的 KVMem CLI v1 由两个 worker 共用解析和默认定义。两个 EXE 都支持 `--kvmem` / `--no-kvmem`，不传 KVMem 配置默认关闭；旧命令传正的 `--kvmem-budget` 仍会启用。启用后的默认 B/R/H 为 32768/1024/16384 MiB，sessions=4；B/R/H 显式 0 会报错，需配置能容纳 B+R 的原生上下文。`--kvmem-sessions` 和脚本 `-RetainedSessions` 在两个后端都只限制非活动历史，活动请求不计入；旧 llama `--kvmem-conversations` 是总数兼容模式，不是同值别名。SSD 额度 0 表示关闭且必须省略目录，关闭 LRFU 使用 `--no-kvmem-lrfu`。新脚本必须与本轮新编译的 worker 配套；已有发布包不会被自动更新。完整定义及 llama 当前 R 输出能力限制见 [参数规范](native-cli-configuration-spec.md)。

ninfer 源码的多卡采用上游 layer pipeline。`-Gpu 'GPU-a,GPU-b'` 保留 UUID 顺序，第一张卡承担 embedding、head、MTP 和 resident Vision；CPU Vision 保留原生 Host 编码路径。可加 `-StageLayers 40,24` 指定连续层数，省略时按各卡空闲内存规划；层数总和由模型装载器验证。统一启动器禁止重复或空设备、超过八卡，以及经 `WorkerArgs` 重复传入设备参数。物理卡必须具有相同 compute capability，显存容量可不同。B/R 仍是每请求的完整逻辑窗口，H 是共享预算；各卡只持有自己的层，不按卡数拆分 B/R 或重复建立 Host 历史。

```powershell
./scripts/windows/start-backend.ps1 -Backend ninfer `
  -Worker 'D:/build/apps/ninfer-serve.exe' -Model 'D:/models/model-vision-mtp.ninfer' `
  -Gpu 'GPU-first-uuid,GPU-second-uuid' -Vision -VisionResidency cpu `
  -MtpDrafts 3 -Budget 2048 -Reserve 512 -Prefill 512 -SinkTokens 64 -RecentTokens 64
```

多卡范围与证据见 [多卡验收](ninfer-multigpu-validation-20261006.md)。这是源码能力，RC1 ZIP 不因此更新。多卡用于扩展容量，尚无本轮吞吐提升结论；`--cuda-memory-policy mixed/strict` 继续要求单卡，KVMem overlay Vision、DFlash、并行 prefill 和磁盘冷快照继续沿用当前限制。

llama.cpp 示例：

```powershell
./scripts/windows/start-backend.ps1 -Backend llamacpp `
  -Model 'D:/models/model.gguf' -Gpu 'GPU-your-device-uuid' `
  -Budget 4096 -Reserve 1024 -KvType q8_0 -SinkTokens 128 -RecentTokens 0
```

已有的 llama.cpp 可执行文件、`start-server.ps1`、IQ3/IQ4 启动脚本仍可使用。原生参数通过 `-WorkerArgs @('--flag', 'value')` 传入，例如 llama 的 MTP、mmproj 和会话配置。后端参数保留各自含义；`.gguf` 与 `.ninfer` 不互换，也不迁移跨引擎 KV。当前源码的 `-HostMiB` 对应两个后端共用的原生 KV payload RAM 预算；llama 仅在显式指定或启用 SSD 时传入，保持未配置时的行为。

## ninfer 功能与组合

当前源码支持固定 MTP1–15、多个 Host 会话、最多八条文本或 resident/CPU 图像请求，可在同一服务中混合图文；BF16/INT8/FP8/NVFP4/K8V4/RK8V4/RK4V4/RK4V4-E8/RK2V4-E8 沿用相同的请求预算规则。2026-10-10 起，磁盘入口接入公共进程内 RAM/SSD 分层，活动 KV 和非活动历史共享 D 配额；旧持久化快照已被替换。自适应 MTP 允许文本及 resident/CPU 图片 C1–8；ngram1–15 可用于九种 KV 的多路文本及 resident/CPU 图片，启动并发大于1时，配置的 ngram16–63 自动降至15并提示，单路保留原宽度。快速 prefill 允许 INT8/RK8V4/RK4V4/RK4V4-E8/RK2V4-E8 的文本及 resident/CPU 图片。CPU 图片编码沿用原生串行编码器，八条活动请求不表示八张图片同时编码。冻结的 P5 运行包仍只有单请求文本能力；新源码的参数不适用于旧包。最终可用组合以随包验收报告为准。原生非 KVMem 模式保持原来的参数入口。

双 NVFP4 已接入源码，5050 上的聚焦数值／页搬运测试及 Bonsai B128/R128 ordinary、MTP1 eager、MTP4 Graph 功能检查已通过。
两个 MTP 用例使用现有 D3D12/WDDM 选项、FP16 GDN 和关闭的 ngram；默认 CUDA 预算下 Q8 MTP 装载仍被容量检查拒绝。
5060 Ti＋IQ3S 已通过双 NVFP4 的 229 组数值／功能／质量／故障注入检查：ordinary/MTP1–4 × eager/Graph、Host 会话、2/4 路文本、resident 图像、冷快照和最高 128K 档检索。三次独立 B32768/R16384、MTP4 Graph 服务启动及越窗 HTTP 请求也通过，ngram/lookup 均关闭。[完整记录](nvfp4-kvmem-validation-20261004.md)保留失败尝试与测试修正；这是本地源码验收，现有运行包不因此获得该能力。
`-KvType nvfp4` 对 K/V 都使用 group-16 e2m1 packed codes 与 E4M3 scale 字节，
Main 与 MTP 页池使用相同格式。参数组合与双 INT8 相同：ordinary 或固定 MTP1–15、
1–4 条文本请求或一条 resident/CPU 图像请求、Host 会话复用；该历史验收中的冷快照目前已暂时禁用。
ngram 默认关闭。

`-KvType k8v4` 已接入 Main/MTP、Host 会话、文本并发和 resident 图像；此前已验证的冷快照入口当前暂时禁用。K 使用 FP8_E4M3FN codes 与一份 FP16 row scale，V 使用 NVFP4 codes 与 raw E4M3 group-16 scales；两者均沿用原生 D256 归一化 Hadamard。5060 Ti＋IQ3S 通过了精简的 26 次真实模型实验和 8 项基础检查，包括 MTP0 eager/Graph、MTP1–3 Graph、MTP4 eager/Graph、C4、图像、磁盘格式隔离、8K/32K 检索，以及 B32768/R16384 MTP4 Graph 服务。ngram/lookup 均关闭；本轮没有性能对比和全排列验收。[清单、配置与结果](k8v4-kvmem-validation-20261004.md)。

新增参数采用 12 个组合的定向补测，另加原生视觉边界诊断，累计 18 次模型启动，使用 5060 Ti＋IQ3S，ngram/lookup 关闭。启动器增加 `-AdaptiveMtp`、`-FastPrefill`、`-VisionResidency cpu`，并将 `-MtpDrafts` 扩至 0–15。数值检查、失败尝试与最小重跑证据见 [参数补测记录](parameter-admission-validation-20261004.md)。这次没有性能对比或全排列验收。

```powershell
# 固定 MTP3，两个活动文本请求；B/R 是每请求预算，H 是全局预算。
./scripts/windows/start-backend.ps1 -Backend ninfer `
  -Model 'D:/models/model-mtp.ninfer' -Gpu 'GPU-your-device-uuid' `
  -MtpDrafts 3 -Concurrency 2 -Budget 2048 -Reserve 512 -Prefill 512 -SinkTokens 64 -RecentTokens 64

# 图像需要包含视觉权重与预处理资源的制品；默认最多 1024 个视觉 tokens/图。
./scripts/windows/start-backend.ps1 -Backend ninfer `
  -Model 'D:/models/model-vision-mtp.ninfer' -Gpu 'GPU-your-device-uuid' `
  -Vision -VisionTokens 1024 -MtpDrafts 3 -Budget 2048 -Reserve 512 -Prefill 512 -SinkTokens 64 -RecentTokens 64

# 八条活动图文请求；B/R 是每路预算，ngram 显式关闭。
./scripts/windows/start-backend.ps1 -Backend ninfer `
  -Model 'D:/models/model-vision-mtp.ninfer' -Gpu 'GPU-your-device-uuid' `
  -Vision -VisionResidency cpu -Concurrency 8 -VisionTokens 1024 `
  -MtpDrafts 3 -NgramDrafts 0 -Budget 2048 -Reserve 512 -Prefill 512 -SinkTokens 64 -RecentTokens 64

# NVFP4 本地源码验收配置示例，使用新编译的 worker；ngram 关闭。
./scripts/windows/start-backend.ps1 -Backend ninfer `
  -Worker 'D:/build/apps/ninfer-serve.exe' `
  -Model 'D:/models/model-mtp.ninfer' -Gpu 'GPU-your-device-uuid' `
  -MtpDrafts 2 -NgramDrafts 0 -KvType nvfp4 -Budget 32768 -Reserve 16384
```

`-MtpDrafts` 默认 0，可选固定 1–15；`-AdaptiveMtp` 要求 MTP，允许文本/图片 C1–8；`-FastPrefill` 允许 INT8/RK8V4/RK4V4/RK4V4-E8/RK2V4-E8 的文本及 resident/CPU 图片 C1–8，可组合公共 SSD 分层；`-VisionResidency cpu` 要求 `-Vision`；`-NgramDrafts` 默认 0，可选 1–63，非零时要求 MTP：宽度 1–15 允许 C1–8；启动并发 C2–8 时，配置的 16–63 自动降至15并提示，C1 保留原宽度。文本及 resident/CPU 图片均允许 BF16/INT8/FP8/NVFP4/K8V4/RK8V4/RK4V4/RK4V4-E8/RK2V4-E8 九种 KV。多路上限 15 来自原生 GDN 验证工作区的 16 列容量；本次移除了 KVMem 额外的单路门禁。`-NgramMinMatch` 默认 12，可选 4–64。例如八路文本用 `-Concurrency 8 -MtpDrafts 3 -NgramDrafts 15 -KvType int8`，图片再加 `-Vision`。ngram 使用完整已提交 token 历史提出复制草稿，仍由目标模型验证；重复文本或代码更容易获益，不能承诺所有输入都加速。直接运行 server 开启 MTP 时，默认 ngram15 可用于多路；统一脚本默认仍显式传入 ngram0。快捷脚本默认单路/ngram31，改成多路时会自动使用 ngram15；以启动并发上限为准，当时只有一条活动请求也不改变宽度。MTP 独立 attention window、视频、overlay Vision、并行 prefill 仍被拒绝。`-DeviceProfile auto` 可选原生设备配置；公共 SSD 分层不提供重启恢复。`-RetainedSessions` 控制非活动 Host 历史数量，默认 4，上限 16。

本次 ninfer 实测制品是 Qwen3.8-27B-GSQ-RCO-IQ3_S，设备为 RTX 5060 Ti 16GB、CUDA 13.2、`sm_120a`。8K/64K/128K/256K 档的早期口令检索和后续追问通过；这组人工构造用例不代表通用长文问答质量，也不表示其他 GPU 架构已验收。示例的 B=2048、R=512、H=12 GiB 是实际通过的配置。

- B 是每请求选中历史的 GPU token 预算，R 是追加工作空间，两者按 64-token 页对齐；C 条活动请求的 Main 页池按 C×(B+R) 配置。MTP 另有原生草稿页池，Main/MTP 的 Host payload 都计入 H。
- NVFP4 的 D256 K/V 每 token 每 KV head 共 288 字节（两份 128 字节 codes 与 16 字节 scales）；K8V4 为 402 字节（256+128+2+16），INT8 为 528 字节，RK8V4 为 408 字节。换页按原始字节传输，不重新量化；权重、GDN state、workspace 与 CUDA Graph 不随 KV 格式同比缩小。格式布局身份和冷快照执行配置区分 NVFP4 与其他 KV 格式。
- prefill 是 128 的倍数且不超过 R。追加压力触发选页，逻辑历史保留原始位置。生成上限由请求输出预算和逻辑上下文上限共同决定。
- H 只限制原生 KV payload。统计索引、每会话最多四个检查点的 GDN StateImage、模型和临时缓冲另计，H 不等于进程总内存上限。
- 长期历史保存在普通 Host 内存。KV CUDA 搬运分批复用 Program 持有的 pinned 缓冲区，以 64 MiB 为目标按完整原生页对齐；单页超过该目标时至少容纳一页，小请求按需分配。D2H、H2D 与 KV 字节校验复用同一缓冲，扩容先释放旧 pin；GDN 检查点与状态校验缓冲另计。`transfer_staging_bytes` 报告搬运暂存总量，SSD 模式还包括普通 RAM scratch，并非进程全部锁页内存。验证范围见 [分批 pin 搬运验证](ninfer-pinned-transfer-validation-20261018.md)。
- 请求在执行前检查整段合法历史的 H 需求，已知预算不足返回错误。编辑历史不匹配可信前缀时从头计算。
- 如果单请求可容纳，但并发请求的全局 H 预留暂时不足，后续请求进入原生 FIFO 队列；不会让每个请求重复独占 H。服务入口默认排队时限 600 秒，Engine API 默认 30 秒，可显式配置。
- 图像区域完整保留，连同 sink 和追加页必须能装入 B；不满足时在准入阶段拒绝，可增大 B 或降低 `VisionTokens`。换图按内容、位置和精确前缀使旧 checkpoint 失效，不依赖文件名。
- 长输入的同查询工具续写可恢复 query 之后的完整端点，并保留冻结 Q/选页；新查询恢复合法的查询前检查点，重新执行 probe 与 replay。取消保留已经提交的完整检查点，不发布尚未提交的生成端点。
- 未超过 B+R 的短输入保存输入末尾前一 token 的 Base 和正常生成结束时的执行端点。短转长时，越过新 query 探测起点且不具有同查询附件的旧检查点不被复用。
- 复用与全量计算可能经过不同的原生分块边界，不能承诺两者浮点状态或所有 greedy tokens 完全相同。相同 checkpoint 的重复请求、取消恢复，以及明确答案的检索测试分别验收。

本次同时修复了 IQ3_S 模型八路/ngram15/Graph 的原生预热启动缺陷；实际验证范围见 [ngram并发补测](ngram-concurrency-validation-20261006.md)。

旧 `--lookup-ngram` 在 KVMem ninfer 中明确禁用，统一使用 `--ngram-draft-tokens` 与 `--ngram-min-match`；启动脚本对应 `-NgramDrafts` 和 `-NgramMinMatch`。经 `WorkerArgs` 传入旧参数也会被拦截。直接运行服务或调用 Engine 时，非零 lookup 配置会明确报错并指向新参数；未启用 MTP 或同时启用新 ngram 也不能放行旧路径。

## ninfer 按硬件资源自动调参

直接运行新构建的 worker，可用 `--kvmem-auto` 启用资源规划：

```sh
ninfer-serve /path/to/model.ninfer --kvmem-auto --max-context 65536 --kv-dtype int8
# 固定 B，自动选择 R、prefill 和 H；显存不足时明确拒绝，而非改写 B。
ninfer-serve /path/to/model.ninfer --kvmem-auto --max-context 65536 --kvmem-budget 8192
```

这是显式选择的保守容量规划策略，原有 `--kvmem` 和手动启动参数保持原来的含义。
`--kvmem-auto` 的 B/R/prefill 选择只做资源核算，不运行请求测速；
ninfer 自带的 DeviceProfile 设置独立，沿用用户配置。
适用于当前已支持的 Qwen3.5/3.6/3.8 路径，含 layer pipeline、MTP、resident/CPU vision；
不扩展到 Flash-Next、Gemma、host/disk 专家或原生前缀缓存。
现有 Windows `start-backend.ps1` 会显式传入 B/R/H/prefill，因此使用自动模式时请直接运行 worker。

### 规划规则与边界

1. CLI 先构造合法的最小 KVMem 形状用于能力检查；最终 B/R/H 在权重加载后、Program 分配前解析。
   不支持的组合仍走原有能力检查，未知可用 RAM 时拒绝自动规划。
2. B 的软目标沿用 32768，受逻辑上下文和 R 限制；sink/recent 的硬下限优先。
   为每个候选 prefill 以整页二分求可容纳的最大 B，再按 B 优先、相同 B 时较大 prefill 优先选择。
   这是历史容量优先的启发式，不是质量或延迟实测结论。
3. B/R 按 64-token 页对齐，prefill 按 128 对齐且不超过 R；R 还覆盖 MTP/ngram 验证宽度。
   每请求 B+R 不超过逻辑上下文，全局物理容量受 ninfer 的 int32 约束。
   各卡分别留下默认 1024 MiB 固定余量，并为预计运行开销另外留出 10% 增长空间；
   检查 `运行开销 + ceil(运行开销/10) + 固定余量 <= 权重加载后的空闲显存`。
   增长空间用于吸收形状相关 Graph/分配器变化，不是连续内存保证。
   以最紧张的一卡约束共同配置，不能将显存简单相加。自动 prefill 上限为 512。
4. H 之外预估活动/保留会话的统计索引、五类检查点及未发布替换、token 前缀、页目录、
   pinned KV 搬运缓冲、StateImage 暂存和 SSD scratch；图像开启时还扣除媒体缓存上限。
   这些 Host 预估含保守增长余量，不是进程内存的精确上限。
   再留默认 2048 MiB RAM，自动 H 取剩余空间的 50%，最多 16 GiB，并按原生页向下对齐。
   自动 H 至少须容纳所有活动 lane 的完整逻辑上下文原生 KV 历史，不能只够一次稀疏换页。
   显式 H 可主动限制历史容量，但仍至少容纳所有活动 lane 的一次 B+R 窗口；请求继续受 H/D 准入检查。
5. 显式 `--kvmem-budget`、`--kvmem-gen-reserve`、`--kvmem-host-mib`、`--prefill-chunk` 各自锁定，
   无论和 `--kvmem-auto` 的先后顺序；`--prefill-chunk 0` 按原生语义委托自动选择。
   显式值也必须满足资源预算；不会偷偷改小。
   sink/recent、保留会话数、LRFU、量化格式、并发与 SSD 路径/配额保持用户选择。
6. `--kvmem-auto-vram-reserve-mib` 和 `--kvmem-auto-ram-reserve-mib` 可调整余量。
   `--no-kvmem`、`--kv-capacity`、原生 KV headroom/Host cache、WDDM evictable 与自动模式冲突。
   启动日志报告 B/R/prefill/H、候选数、每卡规划与可用字节；`/props.kvmem` 报告解析后的值。

公共核心的 `auto_tune_test` 用穷举可行集合核对候选搜索，覆盖单/多卡瓶颈、并发、RAM 不足、
显式参数、整页边界和溢出；ninfer 的 `ninfer_kvmem_auto_test` 验证 CLI 与资源请求的连接。

### 独立 256K 配置验收脚本

`scripts/autoconfigure_ninfer_kvmem.py` 复用已编译的 `ninfer-serve` 和现有 `.ninfer` 模型，
不编译 CUDA、不下载权重、不停止其他服务。目前限定 Linux、单 NVIDIA GPU、单路文本、MTP 关闭；
默认 INT8 KV、Graph 开启、DeviceProfile 关闭，保留后端默认检索与前缀复用策略。

```sh
python3.11 scripts/autoconfigure_ninfer_kvmem.py \
  --server build-auto-ninfer-cuda/apps/ninfer-serve \
  --model models/auto-qwen38-iq4xs.ninfer \
  --out results/ninfer-autoconfigure-256k
```

第一组采用现有 worker 的默认 B32768/R1024/prefill1024，显式固定三项，开启 `--kvmem-auto`
让原生规划器在真实权重加载后检查资源。默认值未通过时，脚本首先固定 R/prefill，
让原生规划器搜索可容纳的 B；必要时按 512/256/128 缩小 prefill 并联动 R，再重新原生规划。
Python 不复刻页格式或显存成本公式。当前二进制没有 plan-only 接口，每个候选都重启 worker、
重载权重；规划阶段的通用拒绝保留为 `planner_rejected_unspecified`，不能据此断言是 GPU 或 Host 不足。

验收直接读取 `backends/ninfer/examples/cli/messages/long_niah_256k.json` 和 manifest 中的精确答案，
核对文件身份及本模型实际模板化 token 数，执行冷 prefill。通过后运行原有 Task 2：约 8K token
一轮的固定工具回放，接近 262144 总窗口，检查上下文增长、前缀复用、SSE 完整性和最后 512-token 生成。
本脚本通过 `scripts/task2_workload.py` 复用从原入口抽出的 Task 2；
原 `multimodal_canary.py` 保持原有实现，没有另造长文语料。
Task 2 不检查最终生成程序功能，NIAH 只覆盖一个中间位置的精确检索，
通过结论限定于这两个已有工作负载，不能推广为通用长文质量保证。

默认逐卡显存余量 1024MiB、Host RAM 余量 2048MiB；公共工具位于 `scripts/ninfer_kvmem_resources.py`，
提供 NVML/RSS/RAM 采样、Host/cgroup 可用内存查询、硬件信息、端口检查和结果写入。
原生规划器另外沿用 10% 运行开销增长余量。H 默认仍由已编译后端的 auto-H 策略决定，
`--host-mib` 可显式锁定；完整历史 Host 不足不会伪装成显存不足并继续缩 B。
GPU 资源拒绝进入受限候选搜索；NIAH 质量失败提高后续 B 下限并优先尝试更大 B；Host 不足、
Task 2 不完整、未知执行错误或超时停止并保留证据。`--budget/--reserve/--prefill` 显式锁定对应项。
默认 B 搜索下限 8192（不是质量保证），最多 12 次启动、总时限 3 小时，均可显式调整。

第一组 NIAH、完整 Task 2 和内存检查均通过即结束，不做速度排名。新建的空输出目录保存
`report.json`、逐候选服务日志、请求与原始回复、`long-context.json`；仅成功时生成
`selected-config.json` 和 `selected-command.txt`。输出命令固定实测 B/R/prefill，H 未锁定时仍在
下一次权重加载后重新规划。失败不生成通过配置；新运行必须使用新的输出目录。
中断或接口错误修正后，可用 `--resume-report <旧目录/report.json>` 加新 `--out` 继续：
核对模型/worker/Task 2 指纹、硬件、执行条件及验收门槛，保留原候选记录和证据目录，
重试最后一组实际参数。先复用 Task 2 的两轮诊断模式检查流式统计及真实缓存命中，
诊断运行不会被计为 256K 通过。已完整执行的冷 NIAH 仅在实际 B/R/prefill 相同、答案与原始文件
一致、源进程内存监控合格、当前 H 足以容纳该历史时才可复用；来源路径及源进程内存摘要明确记录。
未完成的 NIAH 不复用，完整 Task 2 始终从头重跑，不将部分回放升级为成功。
Task 2 的 ninfer HTTP 适配显式请求 `stream_options.include_usage=true`，避免将正常流式生成
误判为缺少 token 统计；把 `prompt_tokens_details.cached_tokens` 映射给原 Task 2 的平铺缓存字段，
同时保留原始 usage 和 SSE。缺失/矛盾的缓存统计、没有 usage 或缺少 `[DONE]` 仍拒绝验收。
原回放的 `reasoning_budget_tokens=128` 在 ninfer 适配层转换为原生 `thinking_budget=128`，
其余采样参数及输出限制沿用原工作负载；逐候选 `requests.jsonl` 保存后端实际解析的请求信息。

2026-10-21 本机 RTX 4070 Ti SUPER 16GB、32GB RAM、Qwen3.8-27B IQ4_XS、INT8 KV、
单路、Graph 开启、MTP 关闭完成验收，直接复用 10 月 19 日编译的 worker。默认
B32768/R1024/prefill1024 被原生规划器通用资源检查拒绝；随后原生拟合得到
**B17216/R1024/prefill1024**。最终运行的自动 H 为 **8984.25MiB**，没有继续做速度排序。

冷 NIAH 的实际输入 260096、输出 17，精确返回 `ORCHID=493817; COLOR=COBALT`；
两次完整冷请求均答对，最终配置复用 `results/ninfer-autoconfigure-256k-02/002/` 的完整原始证据。
本次完整 Task 2 位于 `results/ninfer-autoconfigure-256k-05/004/`：33 请求、32 工具轮，
末轮输入 **261544**、输出 **512**，合计 **262056/262144**；32 工具轮均有前缀命中。
协议预检会预热 Task 2 的最初约 16K 前缀，完整回放仍从基础请求开始，冷长输入由 NIAH 单独验收；
本结果没有用于性能比较。

NIAH 来源进程与本次完整回放进程分别监控，合并取最差观测：最低空闲显存 **1108.625MiB**、
最低可用 RAM **12.828GiB**、最高 worker RSS/HWM **13.148GiB**。这些是约 50ms 采样结果，
不是连续峰值上界。未触发资源门槛，最终 worker 正常退出，测试后无残留计算进程。
`results/ninfer-autoconfigure-256k-05/report.json` 保留全部参数尝试和旧证据来源，
同目录的 `selected-config.json`、`selected-command.txt` 保存首个完整通过的配置。

过程中暴露并修复了流式 usage 请求选项、嵌套缓存计数、原生思考预算映射，以及同参数多次恢复被
重复过滤的问题；先前失败/主动中断和未启动 worker 的报告保存在 `-01` 至 `-04`，未标为通过。
Python 3.12.14 上 17 项控制流程/Task 2/协议/恢复回归、`py_compile`、旧 Task 2 入口 `--help`
及 `git diff --check` 通过。3.11 完成语法解析检查，本机未提供可执行的 3.11 解释器。
验证范围仅上述本机模型与两个既有工作负载；不证明通用长文推理、其他 KV 格式或多并发质量。

该独立验收流程与公共资源工具不依赖离线性能校准入口。可运行以下无模型回归：

```sh
python3.11 -m unittest discover -s scripts -p test_autoconfigure_ninfer_kvmem.py
python3.11 -m unittest discover -s scripts -p test_ninfer_kvmem_resources.py
```

2026-10-22 公共工具抽取后，在 Python 3.12.14 上通过 17 项验收流程回归和 6 项资源工具测试，
并通过 Python 3.11 语法检查。原生规划器及 ninfer CLI 规划连接测试通过。
此次未改变候选顺序、Task 2 或后端执行逻辑，复用上述 256K 原始验收证据，没有重新运行长请求。

### 原生规划的既有验证记录

保守资源策略曾在本机 RTX 4070 Ti SUPER / Qwen3.8-27B IQ4_XS 上通过 9 项实模检查，
包括 INT8/BF16、显式参数、MTP1、两路并发、短上下文边界及预期 Host 资源拒绝。
默认单路 INT8 另完成近 65K 的检索、历史复用、取消恢复与内存余量检查。
原始报告保留在 `results/ninfer-conservative-auto-04/report.json`；它与上述 256K 验收是不同运行，
不能把单路质量结论推广到 MTP、并发或其他硬件。旧的额外实模验收入口已移除。

## API 与状态

`POST /v1/chat/completions` 使用原生 ninfer 的 tokenizer、chat template、采样器和协议实现。`stream: true` 返回 SSE，断开请求会触发取消；下次提交完整 messages 可恢复可信历史。`GET /health` 查询就绪状态，`GET /v1/models` 查询模型，`GET /props` 提供 `backend`、`kvmem` 预算和能力；`/stats`、`/v1/load` 提供原生运行统计及 KVMem 当前历史的 payload、mean 统计、checkpoint 图像用量。历史换入/换出计数随历史替换重置。

ninfer 直接启动参数为 `--kvmem-budget B --kvmem-gen-reserve R --kvmem-host-mib H`，其中 H 的单位为 MiB。这会关闭原生前缀缓存目录，由 KVMem 管理当前会话历史；`--no-prefix-reuse` 同时关闭 KVMem 的跨请求复用。`--kvmem-verify-transfers` 逐字节检查恢复内容，用于功能诊断，会增加传输开销。

直接指定多卡用 `--devices 0,1`，可附加 `--stage-layers 40,24`；序号对应 `CUDA_VISIBLE_DEVICES` 的 UUID 顺序。`/props` 的 `execution` 返回实际设备顺序和最终层数切分，KVMem usage 继续统计共享历史与逻辑页数量。

ninfer 保留四种用途独立的完整状态点：输入/查询前 Base、正常生成结束的已执行端点、Frontend 确认的输入回退边界、生成内容的模板重建边界。输入回退点不会被生成阶段的思考结束状态覆盖，下一轮丢弃或替换生成答案、继续追加相同工具历史时，仍可复用此前输入。未改写的 assistant/工具回放可复用生成内容；最后一个尚未执行的输出 token 留给下一轮计算。每个会话共用一份 Host KV 历史，最多四个完整状态点。长历史工具续写在查询 span 和精确输入身份一致时保留原 Q/选页，只计算新增尾部；新用户查询重新执行检索，改写后的历史仅恢复仍精确匹配的完整状态点。恢复更早状态时放弃较晚状态点，防止共享历史被覆写后仍错误命中。此补全属于 ninfer 适配，llamacpp 路径保持原有行为。

当前源码使用进程内活跃 KV 分层替代已禁用的 ninfer KVMem 冷快照实现。
两个后端的统一启动器均接受 `-HostMiB H -DiskPath 'D:/kv-cache' -DiskMiB D`；
直接启动 worker 时使用 `--kvmem-host-mib H --kvmem-disk-path PATH --kvmem-disk-mib D`。
启动器要求磁盘参数经上述顶层选项传入，不通过 WorkerArgs 重复设置。
省略 DiskPath 则不启用 SSD；仅指定 DiskMiB 或空路径会报错。

即使只有一条活跃请求，超过 H 的已完成原生 K/V 块也可放入 SSD，检索选中的块按需读取。
K、V、scale 和 MTP 数据保持原格式，活跃 mean-K 留在 RAM。H 是原生 KV 驻留预算，
不包含 recurrent checkpoint、统计、传输暂存或系统文件缓存。D 是活跃原生 KV 与闲置
会话附件共用的额度；会话转为闲置时移动原记录所有权，不再复制一份 KV。
H 必须容纳所有并发 lane 的基本工作区，H+D 还需为换页预留余量；容量不足会明确拒绝。

文件属于当前服务进程，正常退出或进程结束后释放，不导入旧 ninfer-kvmem-v5 文件，
不提供重启恢复。ninfer 的 `disk_restore=false` 保留这一含义；`active_kv_tiering=true`
和 `ssd_enabled` 分别表示分层能力及是否启用。既有 llama 会话磁盘入口保留，不能与新的
共享 SSD 配置混用。Windows 和 Linux 使用同一存储契约；当前验收状态与性能限制见
[活跃 KV 分层计划](active-kv-tiering-plan.md)，不能将 WSL 主机测试当作 Linux CUDA 验收。

已知 H 或逻辑上下文不足会在请求执行前返回错误，服务可以继续使用。实际 CUDA 驱动/设备错误仍沿用原生 ninfer 的失败处理，可能需要重启 worker；没有承诺在设备故障后继续复用同一会话。

## 源码构建和打包

引擎源码通过 `backends/llamacpp` 和 `backends/ninfer` 子模块引用既有项目，固定提交及 KVMem 补丁由主仓库管理。先运行 `python scripts/prepare-backends.py`；Windows 构建入口会自动准备所选后端。克隆、补丁维护和源码包说明见 [仓库工作流](multi-backend-repository.md)。

公共核心可用 `KVMEM_BUILD_LLAMA=OFF -DKVMEM_BUILD_SERVER_TESTS=OFF` 单独构建，llama 目标继续使用现有顶层 CMake。ninfer 在自己的构建目录中增加 `-DNINFER_KVMEM_SOURCE_DIR=<kvmem checkout>/kvmem`，构建 `ninfer-serve`；两边独立配置 CUDA 与编译器，公共核心维持 C++17，ninfer 使用 C++20。

在 VS 2022 Developer PowerShell 中运行 `scripts/windows/package-backends.ps1`，传入 `-LlamaWorker`、`-NinferWorker`、`-NinferSource`、`-OutputDir` 和 `-CudaPath`，可加 `-VcpkgInstalled` 与 `-ValidationReport`。输出目录必须不存在。脚本为每个 worker 收集 DLL，检查干净 PATH 下的启动，并写入许可文件、二进制校验值和验证报告；模型不会复制。打包成功只证明依赖可加载，推理结果以配套验证报告为准。

自适应组合另补 10 次 IQ3S／5060 Ti 实验，覆盖快速 prefill、2/4 路、ngram31/63、resident/CPU 图片 query 边界、冷快照跨进程恢复；复用既有数值检查，不进行全排列或性能比较。范围和证据见 [自适应组合补测](adaptive-combinations-validation-20261005.md)。

图片＋MTP＋ngram 当前放行九种原生 KV，支持 resident/CPU 及1–8路；多路宽度16–63自动降至15。新增 `fp8`、`rk4v4`、`rk4v4-e8`、`rk2v4-e8` 只补格式身份和准入，复用原生量化与换页实现。范围与实测见 [新增KV准入补测](other-kv-admission-validation-20261006.md)；此前五种格式见 [图片ngram补测](vision-ngram-all-kv-validation-20261006.md)，首次K8V4换行差异及对照见 [初次图片ngram补测](vision-ngram-validation-20261005.md)。

快速 prefill 的新增 RK 格式与图片准入仅移除额外门禁，复用原生内核，验收范围见 [快速prefill准入补测](fast-prefill-admission-validation-20261006.md)。该开关影响符合原生路由条件的 prompt Attention，不负责并行 prefill，其他 KV 仍用各自原有路径。

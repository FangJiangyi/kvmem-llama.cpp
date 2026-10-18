# llama.cpp v0.7.0 基础接入

接入工作和性能测量基于 KVMem `203e7d90`；提交前同步 master `c88e7b24`，
保留 PR #143 的 NInfer tool-open token 修复。后端固定到官方 v0.7.0：
`620d2225cc436ab0b001e3200e590e3b286ef9ab`。
后端仍由原生提交加版本化补丁重建，完整树和补丁校验值记录在 `backends/versions.json`。

## 范围

- 公共 KVMem 内存契约和其他后端接口保持不变。
- 文本预填充、验证批次改用上游 `common_batch` / `llama_batch_ext`；MTP 隐藏状态使用原生 state embedding 接口。
- KVMem 的逻辑位置随批次分割、重排和 MTP 追赶传播；未提供逻辑位置时保留原生路径。
- 保留 v0.7.0 的 GDN 四列计算和 MMA 预填充路径。KVMem Record 不写入已提交状态，Fold 只提交接受的前缀。
- 提示词和输出解析适配上游的新输入结构、PEG arena 和分段提示词结构。工具解析规则由上游模板生成。
- 监控读取原生层的设备信息通过 llama 适配器封装，支持 Windows 共享库构建。
- Qwen MTP follower 从 KVMem 执行域获取目标；不设置原生 `ctx_other`，避免被 v0.7.0 误判为共享 KV 模式。
- 适配器内存描述符中的后端版本更新为 `620d222+kvmem-native-v1`，公共契约和 attention payload 格式不变。

本批不增加概率 MTP、新的 draft ubatch 调优项或 UI 控件，不包含 v0.7.0 之后的开发版优化。

## 核心验证

首轮 8 项检查通过：GDN Record/Fold、GDN 输出融合、推理预算、聊天模板、工具调用 ID、Responses、公共内存契约和前缀检查点。
GDN 检查包括接受前缀、拒绝区 NaN、不变的已提交状态，以及独立双精度递推参考。
23 个公共头文件的 SHA256 与接入前一致。纯上游加补丁可精确重建版本清单中的源码树。

实际服务端额外通过：27B/MTP/replay 普通聊天、流式必选工具调用及空参数 `{}`；
5050/0.8B 的六轮尾部消息改写和前缀检查点复用，后四轮分别复用 1777/1809/1841/1873 行。
Windows 静态构建专用的 MTP/KV 测试已完成新版 API 编译检查，未运行其完整组合矩阵。

## 性能测量方法

手工驱动 `tests/llama-backend-perf.cpp` 默认不参与构建；需要时启用 `KVMEM_BUILD_PERF_TOOLS`。
同一份源文件也可直接链接到纯原生 v0.7.0 的库，以排除不同 HTTP 接口和聊天模板的影响。

- GPU：RTX 5060 Ti，UUID `GPU-5847813c-9e6e-bb43-cc5e-621aac091b6c`。
- 同一 IQ3_S GGUF、同一份 36864 个输入 token、固定 256 个输出。
- B=36864，R=16384；原生及关闭 KVMem 时 ctx=53248，开启时逻辑 ctx=131072。
- batch=ubatch=512，目标 KV q8_0，draft KV f16，MTP=3，greedy，屏蔽 EOS，无提示词复用。
- 主对照：原生、补丁构建但关闭 KVMem、开启 KVMem 的服务端默认 replay/128-token block。
  另保留 snapshots/32-token block 的诊断组。每组预热一次，测量两次。
- 首 token 计入预填充，生成速度用其后 255 个 token 除以首末 token 的时间差。

该测试考察共同容量范围内的基础接入成本；超预算检索和 256K 工具长任务不属于本批性能结论。

## 结果

| 配置 | 预填充 token/s | 生成 token/s | 生成相对原生 | 显存峰值 MiB |
|---|---:|---:|---:|---:|
| 纯原生 v0.7.0 | 564.10 | 37.94 | — | 14703 |
| 带补丁、关闭 KVMem | 566.14 | 37.75 | -0.51% | 14703 |
| KVMem 默认 replay | 558.30 | 36.78 | -3.05% | 14391 |
| KVMem snapshots 诊断组 | 558.88 | 35.54 | -6.32% | 14827 |

默认 replay 的预填充下降 1.03%，首 token 平均耗时从 65.35 秒到 66.03 秒。
两轮生成速度分别为 37.01 和 36.56 token/s。四组共 12 轮的 256 个输出 token 均一致，
每轮 drafted/accepted 都是 232/178，最终记录没有内部解码错误。

本次基础接入在这组配置下没有大幅性能回退；默认模式仍有约 3.1% 的生成开销，不是完全无损。
只有一个模型、一张 GPU、一个固定负载、两次正式重复，不将该结论外推到所有场景。
数据见 [完整测量摘要](benchmarks/llamacpp-v070-base-20261018.json)。原始日志保存在本机实验目录的 `performance/` 下。

实测后仅修正了内存描述符中的旧版本文字，并增量构建；计算和内存管理路径未再改动。

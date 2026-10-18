# NInfer 分批复用 pinned KV 搬运缓冲验证

日期：2026-10-18。关联问题：#114。此次改动针对重建时的大块 Host 锁页分配，不缩小 device KV 池，也不宣称完整修复该问题。

## 改动与不变量

`WindowMemoryTransfer` 原来按 spill/restore 最大页数分配整窗 pin，checkpoint 仅归档时也可能按整个 selected 窗口分配。缓冲扩容的赋值会先构造新分配，再销毁旧缓冲，存在新旧 pin 重叠。

现在以 64 MiB 为目标按整页分批，复用 Program 持有的同一块 pin；小请求按实际页数增长，单页超过目标时仍至少容纳一页。仅归档只考虑需写回的变更页，扩容在安全边界先释放旧 pin。每批 DMA 完成并同步后才读取 Host 字节或复用缓冲，跨调度不持有 pin 指针。

主 KV、MTP KV 及量化 scales 保留原始字节；不新增 device staging。异步 SSD 的存储读取仍在破坏旧视图前完成。同步恢复先取得并检查 resident 读取租约，再重建 GPU 页映射。KV 校验使用同一块 pin，期望字节留在普通 RAM；GDN checkpoint 与前后状态校验缓冲另计。此上限不是进程全部 pin 或 Host 历史内存的上限。

## RTX 5050 A/B 与正确性

- GPU：RTX 5050 Laptop 8GB，UUID `GPU-14f08a8c-8d62-4338-8ae4-c669889cdb29`，Windows 驱动 617.42。
- 现有 0.8B 制品的投影几何不受该后端支持，未扩展模型支持范围。采用完整 27B 制品裁出的四层执行夹具（3 GDN、1 Attention，保留原始投影/heads 与权重字节，可选原始 MTP）；用于内存和原生状态验证，不代表模型回答质量。
- 主对照：BF16 KV、MTP 关闭、C=1、context=65,536、B=32,768、R=8,192、prefill512、Host=2,048MiB、sink128/recent512，关闭 CUDA Graph 与设备自动校准。
- 同一输入按短请求、58,438-token 跨池请求、热历史复用、末尾指令改写执行，两个版本各两个新进程，顺序 A/B/B/A。其余链接输入一致，只替换窗口翻译单元。

| 指标 | 原实现，两次 | 本补丁，两次 |
|---|---:|---:|
| KV pin 容量 | 160MiB | 64MiB |
| 推理 Shared GPU Memory 峰值 | 306MiB | 210MiB |
| 推理进程 Dedicated 峰值 | 1,992.23MiB | 1,992.05MiB |
| 含模型加载的 Shared 峰值 | 338MiB | 338MiB |

短请求 pin 仅 0.25MiB，没有固定 64MiB 底座。专用显存基本不变，全程 Shared 峰值仍由模型加载主导。

另启用 INT8 KV、MTP draft=2、并发容量2、SSD512MiB、Host384MiB：两条长请求并发提交，峰值 admitted=2；重复历史恢复、真实 SSD 写入与冷回读通过。最后一组有16次恢复完成原生 K/V、scales 与 recurrent state 精确校验；回读140.38MiB，存储错误为0。流式请求收到首个 SSE 事件后断开，随后短请求正常完成8 tokens、健康检查通过；未声称覆盖 DMA 中途取消。

## RTX 5060 Ti / 27B / 256K

GPU：RTX 5060 Ti 16GB，UUID `GPU-5847813c-9e6e-bb43-cc5e-621aac091b6c`，驱动617.42。完整模型 `Qwen3.8-27B-GSQ-RCO-IQ3_S-vision-mtp.ninfer`，SHA256 `9025edb5c5831cd7c617dfcf745179db277171f68bc21dc9e5b30881411a2ca1`。

保持2026-10-13 RC3测试配方：context262,144、B36,864/R16,384、主/MTP KV INT8、GDN FP16、MTP3固定、ngram/lookup关闭、prefill512、Host12GiB、SSD关闭、会话/并发1、sink/recent各5120、LRFU0.5/0.9、GQA=max、GPU resident vision、关闭设备校准、thinking128/output512。重放相同的33个请求JSON和模拟工具结果，输入token数逐轮一致，末轮261,522+512 tokens；33/33成功、32/32工具轮次命中历史。未执行真实文件工具，未验证生成代码正确性。

| 指标 | 历史RC3，未重跑 | 本补丁 |
|---|---:|---:|
| 整卡显存峰值，约2秒采样 | 14,456MiB | 14,445MiB |
| 请求后 transfer staging 最大容量 | 未记录 | 63.551MiB |
| 最低可用物理RAM | 8.78MiB | 1,281.66MiB |
| 请求总耗时 | 855.10秒 | 811.52秒 |
| 有效prefill聚合 | 469.64 token/s | 471.86 token/s |
| 工具轮次decode聚合 | 53.24 token/s | 57.23 token/s |

本次20ms轮询的进程Dedicated峰值为14,432.75MiB，Shared为520MiB；历史RC3没有对应计数，无法直接比较。NVML v1 used包含driver reserved，本次核对reserved=259MiB；不将其原始读数与nvidia-smi used混比。约2秒的旧采样可能漏过短峰；20ms轮询也不能替代分配/释放事件的高水位统计。

同口径整卡显存仅降低11MiB（0.08%），未显示明显改善。两版device KV payload均为1,823.38MiB，device池没有因本补丁缩小。与RC3相隔其他后端改动、后台RAM压力与生成内容不同，各仅一次长任务，不能把耗时、RAM或速度差异单独归因于本补丁。总输出15,293→14,322 tokens；32轮decode有20轮提高、12轮降低，最差约-29%且该轮输出512→64 tokens。末轮prefill469.85→465.86 token/s，decode44.87→58.10 token/s；MTP接受率46.93%→66.87%、推测轮数202→161，末轮提速主要伴随更少的推测轮次，不代表统一提速或pin改善模型质量。

## 构建证据与限制

测试基于 prepared tree `543ea4af565438a04c10abf502e9236d23dc0657` 对应的已有本地构建，冻结21个链接输入，匹配其11处既有源码差异以保持ABI一致；分别重编窗口翻译单元并链接相同输入，非官方发布包全量重编。本补丁窗口文件与已测试源文件一致，叠加到当前主线的其余后端补丁未改动。构建使用8个并行任务上限；两组只有相同的既有C4267告警。

完整集成补丁及 `patch_sha256` / `patched_tree` 使用仓库 `scripts/prepare-backends.py --backend ninfer --check` 验证。主机测试脚本使用已有Python3.12.13，未找到3.11；C++/CUDA运行时未变更。

未复现用户原始 cudaMallocHost 失败，未进行失败注入或同构建5060 Ti有/无补丁性能对照。CUDA last error、请求级失败恢复及所有FATAL路径不在本次补丁范围内。原始模型/二进制/采样日志留在本地验证目录，未作为大文件提交。

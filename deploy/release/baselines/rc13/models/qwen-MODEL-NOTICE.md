---
license: qwen
license_name: Tongyi Qianwen LICENSE AGREEMENT
license_link: LICENSE
language:
- en
- zh
pipeline_tag: text-generation
tags:
- text-generation
- Coder
- Chat
- Instruct
base_model:
  - Qwen/Qwen2.5-Coder-32B-Instruct
---
# 通义千问 Qwen2.5-Coder-32B-Instruct-FP8

原模型 [qwen/Qwen2.5-Coder-32B-Instruct](https://www.modelscope.cn/models/qwen/Qwen2.5-Coder-32B-Instruct)

```python
# download model 
from modelscope import snapshot_download
model_dir = snapshot_download('okwinds/Qwen2.5-Coder-32B-Instruct-FP8')
```
## Qwen2.5 同系列量化模型

| **Model**    | GPTQ Compression & FP8                                                                                                                                                                                                                                                                                                                                                                                                     | GGUF                                                                                         | **MLX** (macOS)                                                                                                                                                                                                                                                              |
| ------------ |----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------| -------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| *3B params*  | -                                                                                                                                                                                                                                                                                                                                                                                                                          | [3B-GGUF-V3-LOT](https://www.modelscope.cn/models/okwinds/Qwen2.5-3B-Instruct-GGUF-V3-LOT)   | [3B-MLX-4bit](https://www.modelscope.cn/models/okwinds/Qwen2.5-3B-Instruct-MLX-4bit)<br/>[3B-MLX-8bit](https://www.modelscope.cn/models/okwinds/Qwen2.5-3B-Instruct-MLX-8bit)                                                                                                |
| *7B params*  | [7B-Int4-W4A16](https://www.modelscope.cn/models/okwinds/Qwen2.5-7B-Instruct-Int4-W4A16)<br/>[7B-Int8-W8A16](https://www.modelscope.cn/models/okwinds/Qwen2.5-7B-Instruct-Int8-W8A16)<br/>[7B-FP8-A16](https://www.modelscope.cn/models/okwinds/Qwen2.5-7B-Instruct-FP8-A16) <br/>[Coder-7B-Instruct-FP8](https://www.modelscope.cn/models/okwinds/Qwen2.5-Coder-7B-Instruct-FP8) | [7B-GGUF-V3-LOT](https://www.modelscope.cn/models/okwinds/Qwen2.5-7B-Instruct-GGUF-V3-LOT)   | [7B-MLX-4bit](https://www.modelscope.cn/models/okwinds/Qwen2.5-7B-Instruct-MLX-4bit)<br/>[7B-MLX-8bit](https://www.modelscope.cn/models/okwinds/Qwen2.5-7B-Instruct-MLX-8bit)                                                                                                |
| *Coder 7B params* | [Coder-7B-Int4-W4A16](https://www.modelscope.cn/models/okwinds/Qwen2.5-Coder-7B-Instruct-Int4-W4A16)<br/>[Coder-7B-Int8-W8A16](https://www.modelscope.cn/models/okwinds/Qwen2.5-Coder-7B-Instruct-Int8-W8A16) <br/>[Coder-7B-FP8-A16](https://www.modelscope.cn/models/okwinds/Qwen2.5-Coder-7B-Instruct-FP8-A16)       | [Coder-7B-GGUF-V3-LOT](https://www.modelscope.cn/models/okwinds/Qwen2.5-Coder-7B-Instruct-GGUF-V3-LOT) | [Coder-7B-MLX-4bit](https://www.modelscope.cn/models/okwinds/Qwen2.5-Coder-7B-Instruct-MLX-4bit)<br/>[Coder-7B-MLX-8bit](https://www.modelscope.cn/models/okwinds/Qwen2.5-Coder-7B-Instruct-MLX-8bit)   |
| *14B params* | [14B-Int4-W4A16](https://www.modelscope.cn/models/okwinds/Qwen2.5-14B-Instruct-Int4-W4A16)<br/>[14B-Int8-W8A16](https://www.modelscope.cn/models/okwinds/Qwen2.5-14B-Instruct-Int8-W8A16)<br/>[14B-FP8-A16](https://www.modelscope.cn/models/okwinds/Qwen2.5-14B-Instruct-FP8-A16)                                                                                                                                         | [14B-GGUF-V3-LOT](https://www.modelscope.cn/models/okwinds/Qwen2.5-14B-Instruct-GGUF-V3-LOT) | [14B-MLX-4bit](https://www.modelscope.cn/models/okwinds/Qwen2.5-14B-Instruct-MLX-4bit)<br/>[14B-MLX-8bit](https://www.modelscope.cn/models/okwinds/Qwen2.5-14B-Instruct-MLX-8bit)                                                                                            |
| *Coder 14B params* | [Coder-14B-Int4-W4A16](https://www.modelscope.cn/models/okwinds/Qwen2.5-Coder-14B-Instruct-Int4-W4A16)<br/>[Coder-14B-Int8-W8A16](https://www.modelscope.cn/models/okwinds/Qwen2.5-Coder-14B-Instruct-Int8-W8A16)                                                                                                                                        | [Coder-14B-GGUF-V3-LOT](https://www.modelscope.cn/models/okwinds/Qwen2.5-Coder-14B-Instruct-GGUF-V3-LOT) | [Coder-14B-MLX-4bit](https://www.modelscope.cn/models/okwinds/Qwen2.5-Coder-14B-Instruct-MLX-4bit)<br/>[Coder-14B-MLX-8bit](https://www.modelscope.cn/models/okwinds/Qwen2.5-Coder-14B-Instruct-MLX-8bit)  |
| *32B params* | [32B-Int4-W4A16](https://www.modelscope.cn/models/okwinds/Qwen2.5-32B-Instruct-Int4-W4A16)<br/>[32B-Int8-W8A16](https://www.modelscope.cn/models/okwinds/Qwen2.5-32B-Instruct-Int8-W8A16)<br/>[32B-FP8-A16](https://www.modelscope.cn/models/okwinds/Qwen2.5-32B-Instruct-FP8-A16)                                                                                                                                                                                                                                        | [32B-GGUF-V3-LOT](https://www.modelscope.cn/models/okwinds/Qwen2.5-32B-Instruct-GGUF-V3-LOT) | [32B-MLX-2bit](https://www.modelscope.cn/models/okwinds/Qwen2.5-32B-Instruct-MLX-2bit)<br/>[32B-MLX-4bit](https://www.modelscope.cn/models/okwinds/Qwen2.5-32B-Instruct-MLX-4bit)<br/>[32B-MLX-8bit](https://www.modelscope.cn/models/okwinds/Qwen2.5-32B-Instruct-MLX-8bit) |
| *Coder 32B params* | [Coder-32B-Int4-W4A16](https://www.modelscope.cn/models/okwinds/Qwen2.5-Coder-32B-Instruct-Int4-W4A16)<br/>[Coder-32B-FP8](https://www.modelscope.cn/models/okwinds/Qwen2.5-Coder-32B-Instruct-FP8)                                                                                                                                                                                                                                        | [Coder-32B-GGUF-V3-LOT](https://www.modelscope.cn/models/okwinds/Qwen2.5-Coder-32B-Instruct-GGUF-V3-LOT) | [Coder-32B-MLX-4bit](https://www.modelscope.cn/models/okwinds/Qwen2.5-Coder-32B-Instruct-MLX-4bit)<br/>[Coder-32B-MLX-8bit](https://www.modelscope.cn/models/okwinds/Qwen2.5-Coder-32B-Instruct-MLX-8bit) |
| *QwQ-Preview 32B params* | [QwQ-32B-Preview-Int4-W4A16](https://www.modelscope.cn/models/okwinds/QwQ-32B-Preview-Int4-W4A16)<br/>[QwQ-32B-Preview-FP8](https://www.modelscope.cn/models/okwinds/QwQ-32B-Preview-FP8)    | [QwQ-32B-GGUF-V3-LOT](https://www.modelscope.cn/models/okwinds/QwQ-32B-Preview-GGUF-V3-LOT) | [QwQ-32B-MLX-4bit](https://www.modelscope.cn/models/okwinds/QwQ-32B-Preview-MLX-4bit)<br/>[QwQ-32B-MLX-8bit](https://www.modelscope.cn/models/okwinds/QwQ-32B-Preview-MLX-8bit) |
| *72B params* | -                                                                                                                                                                                                                                                                                                                                                                                                                          | [72B-GGUF-V3-LOT](https://www.modelscope.cn/models/okwinds/Qwen2.5-72B-Instruct-GGUF-V3-LOT) | [72B-MLX-2bit](https://www.modelscope.cn/models/okwinds/Qwen2.5-72B-Instruct-MLX-2bit)<br/>[72B-MLX-4bit](https://www.modelscope.cn/models/okwinds/Qwen2.5-72B-Instruct-MLX-4bit)<br/>[72B-MLX-8bit](https://www.modelscope.cn/models/okwinds/Qwen2.5-72B-Instruct-MLX-8bit) |

## 模型概述

Qwen2.5-Coder-32B-Instruct-FP8 是一个基于 通义千问2.5-Coder-32B-Instruct 的 FP8 量化模型。

- **模型名称:** Qwen2.5-Coder-32B-Instruct-FP8
- **模型架构:** Qwen2.5
- **权重量化:** FP8

### 量化模型

该模型通过将 [Qwen2.5-Coder-32B-Instruct](https://www.modelscope.cn/models/qwen/Qwen2.5-Coder-32B-Instruct) 的权重量化为 FP8 数据类型而实现。

量化过程将每个参数从 16bit 减少到 8bit，将模型占用磁盘空间大小，以及推理时加载模型需要的GPU显存空间，减少到了大约为原模型的1/2。

FP8（Floating Point 8-bit）是一种8位浮点数数据格式，由Nvidia、Arm、Intel联合推出，旨在加速深度学习的训练和推理过程。
本模型的量化，采用 F8_E4M3 数据类型对权重进行 dynamic 量化，尽可能减少模型量化后所带来的精度损失。

> <span style="color: red;">注意：本模型需要 compute capability > 8.0（Ampere、Ada Lovelace、Hopper 架构）的 Nvidia GPU 来支持 FP8 及**混合精度**计算。</span>

## 部署推理

#### 推荐使用 vLLM （ <span style="color: red;">version > 0.6.3.post1</span> ）

Openai api 兼容模式

```bash
>>> vllm serve "/home/gavin/llm/Qwen2.5-Coder-32B-Instruct-FP8" --host 0.0.0.0 --port 8000 --quantization fp8 --gpu-memory-utilization 0.9 --served-model-name "Qwen2.5-Coder-32B-Instruct-FP8" 
```

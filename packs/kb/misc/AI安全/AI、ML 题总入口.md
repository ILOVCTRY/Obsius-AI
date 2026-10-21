---
name: ctf-ai-ml
description: AI/ML 题的总入口。用于攻击 ML 模型、构造对抗样本、模型窃取、提示词注入、成员推断、训练数据投毒、微调篡改、神经网络分析、LoRA 适配器利用、LLM 越狱，以及任何 AI 相关谜题。判据是先分清「打模型」还是「打模型外面那层壳」。
license: MIT
compatibility: Requires filesystem-based agent (Claude Code or similar) with bash, Python 3, and internet access for tool installation.
allowed-tools: Bash Read Write Edit Glob Grep Task WebFetch WebSearch
metadata:
  user-invocable: "false"
---

# AI、ML 题总入口

先分诊再选篇：**打模型本体**（权重/梯度/对抗样本）→ 模型攻击与对抗样本；**打模型外面那层**（提示词/工具调用/解析器）→ LLM 攻击。

分册：

- [[misc/AI安全/模型攻击（权重与梯度层面）.md|模型攻击]] —— 权重扰动否定、梯度反演、编码器碰撞、LoRA 合并、查询式模型窃取、成员推断
- [[misc/AI安全/对抗机器学习与后门分析.md|对抗样本]] —— FGSM / PGD / C&W、对抗补丁、分类器逃逸、数据投毒、后门检测
- [[misc/AI安全/LLM 攻击（提示词与工具层面）.md|LLM 攻击]] —— 直接/间接提示词注入、越狱、token 走私、上下文窗口操纵、工具调用利用

## 0. 判据速查

| 手上有什么 | 结论 | 去哪 |
|---|---|---|
| 给了 `original.pt` + `challenge.pt` | 权重差分，微调被用来「压制」行为 | 模型攻击 |
| 只有 `adapter_model.safetensors` | LoRA，合并 `W_base + α·(B@A)` | 模型攻击 |
| 能拿到模型梯度 | 梯度反演重建训练数据 | 模型攻击 |
| 能黑盒查询 API | 模型窃取 / 成员推断 | 模型攻击 |
| 有干净样本 + 目标标签 | FGSM 起步，不行上 PGD | 对抗样本 |
| 图像要经过缩放/压缩/拍照 | 对抗补丁（鲁棒性更强） | 对抗样本 |
| 有完整训练流程 | 数据投毒 / 后门 | 对抗样本 |
| 目标是个聊天接口 | 提示词注入 | LLM 攻击 |
| 有安全过滤但模型能对话 | 越狱 / token 走私 | LLM 攻击 |
| 模型能调工具/函数 | 工具调用利用 | LLM 攻击 |

## 1. 环境

```bash
pip install torch transformers numpy scipy Pillow safetensors scikit-learn
apt install python3-dev            # Linux
```

## 2. 五分钟体检

```bash
file model.*

# PyTorch
python3 -c "import torch; m=torch.load('model.pt',map_location='cpu'); print(type(m)); print(m.keys() if hasattr(m,'keys') else dir(m))"

# safetensors
python3 -c "from safetensors import safe_open; f=safe_open('model.safetensors',framework='pt'); print({k:f.get_tensor(k).shape for k in f.keys()})"

# HuggingFace 目录
python3 -c "from transformers import AutoModel; print(AutoModel.from_pretrained('./model_dir'))"

# LoRA 适配器
python3 -c "from safetensors import safe_open; f=safe_open('adapter_model.safetensors',framework='pt'); print(list(f.keys()))"
```

**权重差分是最省事的第一步**——两个模型一减就知道改了哪些层、改了多少：

```python
import torch
a = torch.load('original.pt', map_location='cpu')
b = torch.load('challenge.pt', map_location='cpu')
for k in a:
    if not torch.equal(a[k], b[k]):
        d = (a[k] - b[k]).abs()
        print(f'{k}: max={d.max():.6f} mean={d.mean():.6f}')
```

## 3. 权重层面

- **扰动否定**：微调把某个行为压制掉了，`2*W_orig - W_chal` 能把微调增量反向抵消。**前提是手里有原始权重**。
- **LoRA 合并**：`W_base + α * (B @ A)`，合并后直接看激活或生成结果。LoRA 通常只改少数层，`B`/`A` 的形状直接告诉你作用在哪个投影上。
- **梯度反演**：优化随机输入张量，最小化「模型输出与已知目标的距离」。
- **编码器碰撞**：联合优化两个输入，让编码器输出相同——这是找碰撞不是找逆。

## 4. 对抗样本

| 方法 | 一句话 | 什么时候用 |
|---|---|---|
| FGSM | `x_adv = x + ε·sign(∇_x loss)` | 一步够用的场景，先试它 |
| PGD | 迭代 FGSM + 每步投影回 ε 球 | 标准基准，FGSM 不够就上 |
| C&W | 优化扰动范数同时达成误分类 | 需要最小扰动 |
| 对抗补丁 | 场景里贴一块图就让分类错 | 图像要过缩放/压缩/拍照 |

**顺序**：FGSM → PGD → C&W。补丁只在「扰动会被预处理抹掉」时才值得上。

## 5. LLM 层面

- **直接注入**：用户输入覆盖系统指令。
- **间接注入**：注入藏在被检索的文档/网页/文件里——**这类题的关键是找「模型会读但不校验」的输入通道**。
- **越狱**：DAN、角色扮演、编码技巧、多轮升级。
- **token 走私**：利用分词器切分，让被过滤的词以子词形式通过。
- **工具调用利用**：滥用 function calling 让 agent 执行非预期动作。

## 6. 什么时候换方向

- 变成纯数学/格/数论、没有 ML 成分 → 去 [[crypto/格与后量子/格与 LWE 攻击.md|格与 LWE]] 或 [[crypto/高阶专题/高阶数学攻击.md|高阶数学]]
- 要逆编译后的模型二进制（ONNX loader / TensorRT engine / 自写推理器）→ 去 [[binary/逆向工程/逆向工程.md|逆向工程]]
- 只是用 ML 当壳（聊天机器人里套 Python jail）→ 去 [[misc/沙箱逃逸/Python 沙箱逃逸.md|Python 沙箱逃逸]]

## 7. 纪律

- **先做权重差分**。`original` + `challenge` 两个文件都在时，差分能直接指出改了哪几层，比读论文猜快一个数量级。
- **分清「打模型」和「打壳」**。大部分「AI 题」实际考的是提示词注入或解析器 bug，不是真的攻模型。
- **对抗样本从 FGSM 起手**。一上来写 PGD 循环，参数没调对反而比 FGSM 差。
- **换方向要早**。发现要做格约简或逆二进制，说明这题的本质不在 ML，立刻切篇。

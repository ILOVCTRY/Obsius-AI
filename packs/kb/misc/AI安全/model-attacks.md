---
title: 模型攻击（权重与梯度层面）
phase: misc
vuln_class: [ai, ml, model-extraction, model-inversion, lora, membership-inference]
summary: 模型题的判据是「你手里有没有原始权重」——有 orig+chal 就做差分否定，只有 adapter 就合并 LoRA，两者都没有就只能靠查询接口拟合。
---

# 模型攻击（权重与梯度层面）

对抗样本与数据投毒见 [[misc/AI安全/adversarial-ml.md|对抗样本]]；提示词与工具调用见 [[misc/AI安全/llm-attacks.md|LLM 攻击]]；总入口见 [[misc/AI安全/index.md|AI/ML 题总入口]]。

## 0. 判据速查

| 手上有什么 | 结论 | 去哪节 |
|---|---|---|
| `original.pt` + `challenge.pt` | 权重差分，取反把「压制」变「放大」 | §1 |
| 只有 `adapter_model.safetensors` | LoRA，`W_base + α·(B@A)` | §4 |
| 能拿到模型且可反传 | 梯度下降反演输入 | §2 |
| 只有一个编码器 + 要两个输入 | 编码器降维必然有碰撞 | §3 |
| 只有查询 API，拿不到权重 | 查询式提取 / 蒸馏 | §5 |
| 有样本集要判「是否训练过」 | 成员推断，置信度与损失差 | §6 |
| 权重里看不出文字但能画图 | `B@A` 或差分矩阵重排成图像 | §4 |
| 模型输出被离散成哈希 | 先松弛成连续再优化 | §3 |

## 1. 权重差分否定

微调把某行为压制掉了：`W_chal = W_orig + δ`，则 `W_recovered = W_orig - δ = 2*W_orig - W_chal` 把压制反向变成放大。

```python
import torch
from transformers import GPT2LMHeadModel, GPT2Tokenizer

orig = GPT2LMHeadModel.from_pretrained("gpt2")
chal = GPT2LMHeadModel.from_pretrained("./challenge_model")
rec  = GPT2LMHeadModel.from_pretrained("gpt2")

o, c, r = orig.state_dict(), chal.state_dict(), rec.state_dict()
for k in o:
    r[k] = 2 * o[k] - c[k]
rec.load_state_dict(r)

tok = GPT2Tokenizer.from_pretrained("gpt2")
tok.pad_token = tok.eos_token
out = rec.generate(**tok("The flag is", return_tensors="pt"),
                   max_new_tokens=100, temperature=0.7, do_sample=True,
                   num_return_sequences=5)
for s in out:
    print(tok.decode(s, skip_special_tokens=True))
```

**先做差分定位**：`(o[k] - c[k]).abs().max()` 排序，看微调打在哪几层。**改动稀疏时否定效果最强**——如果只有少数层变了，说明 δ 集中在这些层，纯否定就能精确反演；如果全层都小幅变化，纯否定会把无关噪声也放大。

**三个变体**（纯否定效果不好时按序试）：

- **缩放否定**：`W_orig + α*(W_orig - W_chal)`，`α` 从 1.0 到 3.0。`α=1` 就是纯否定；`α>1` 放大更多但可能崩坏语言能力。
- **分层否定**：只对 L2 范数差超过阈值的层取反，其余保留。
- **LoRA 感知**：微调若用了 LoRA，δ 是低秩的，先提取再单独取反，比整矩阵操作干净。

## 2. 梯度反演

模型可微，就对输入求梯度，把输出往目标上拉。

```python
import torch, torch.nn as nn, torch.optim as optim
from torchvision import transforms

model = torch.load("challenge_model.pt", map_location="cpu", weights_only=False)
model.eval()          # BN/Dropout 必须切 eval

target = torch.load("target_embedding.pt", weights_only=False)
x = torch.randn(1, 3, 224, 224, requires_grad=True)
opt = optim.Adam([x], lr=0.01)
mse = nn.MSELoss()

for step in range(2000):
    opt.zero_grad()
    loss = mse(model(x), target)
    tv = (torch.abs(x[:, :, :, :-1] - x[:, :, :, 1:]).sum()
          + torch.abs(x[:, :, :-1, :] - x[:, :, 1:, :]).sum())
    (loss + 1e-4 * tv).backward()
    opt.step()
    with torch.no_grad():
        x.clamp_(0, 1)
```

**`model.eval()` 不是可选项**：留着 train 模式时 BN 用 batch 统计量，单个样本下等于用自身均值方差归一化，梯度方向全错。

**TV 正则的作用**：不加会得到高频噪点图（模型确实匹配了目标，但视觉上一片雪花）。`1e-4` 量级是起点，噪点太多就调大。

**PyTorch 2.6+ 的坑**：`torch.load` 默认 `weights_only=True`，加载整模型会失败，必须显式 `weights_only=False`——但这会执行 pickle，只对可信文件这么做。

## 3. 编码器碰撞

编码器把高维压到低维，**由鸽巢原理碰撞必然存在**，问题是找。

```python
a = torch.randn(1, 3, 64, 64, requires_grad=True)
b = torch.randn(1, 3, 64, 64, requires_grad=True)
opt = optim.Adam([a, b], lr=0.005)

for step in range(5000):
    opt.zero_grad()
    ea, eb = encoder(a), encoder(b)
    collision = nn.MSELoss()(ea, eb)          # 嵌入距离 → 0
    diversity = -nn.MSELoss()(a, b)           # 输入距离 → 大（取负号）
    rng = (torch.relu(-a).sum() + torch.relu(a - 1).sum()
           + torch.relu(-b).sum() + torch.relu(b - 1).sum())
    (collision + 0.1 * diversity + 0.01 * rng).backward()
    opt.step()
    with torch.no_grad():
        a.clamp_(0, 1); b.clamp_(0, 1)
```

**两个损失必须同时存在**：只压 `collision` 会收敛到 `a ≈ b`（平凡解），加了 `diversity` 才逼出「不同输入同嵌入」。`diversity` 权重太大则永远碰不上，`0.1` 是常用起点。

**判据是 `emb_dist` 和 `input_dist` 同时看**：只看前者会以为成功，其实两个输入已经几乎一样。

## 4. LoRA 合并

```python
from safetensors import safe_open
adapter = safe_open("adapter_model.safetensors", framework="pt")
print(list(adapter.keys()))
# base_model.model.transformer.h.0.attn.c_attn.lora_A.weight / lora_B.weight
```

```python
alpha = lora_alpha / r          # 从 adapter_config.json 读
base_sd = base_model.state_dict()

for a_key in [k for k in adapter.keys() if "lora_A" in k]:
    b_key = a_key.replace("lora_A", "lora_B")
    base_key = a_key.replace("base_model.model.", "").replace(".lora_A.weight", ".weight")
    A = adapter.get_tensor(a_key)      # (r, in_features)
    B = adapter.get_tensor(b_key)      # (out_features, r)
    base_sd[base_key] = base_sd[base_key] + alpha * (B @ A)
base_model.load_state_dict(base_sd)
```

用 PEFT 更省事：`PeftModel.from_pretrained(base, dir).merge_and_unload()`。

**`alpha` 是 `lora_alpha / r` 而不是 `lora_alpha`**——这是最常见的错，差了 `r` 倍（通常 8 或 16），合并后模型行为完全不对但不会报错。

**flag 可能不在文本输出里**：把 `B @ A` 的 delta 矩阵 `reshape` 成二维图看，很多题的 flag 是**画在权重图案里**的。`target_modules` 告诉你作用在哪个投影上（`q_proj`/`v_proj`/`c_attn`）。

**多 adapter 叠加**要按 `adapter_config.json` 里的顺序依次合并，顺序错了结果不同。

## 5. 查询式模型提取

**线性模型可精确提取，只需 `dim+1` 次查询**：

```python
base = query_model(np.zeros(dim))
b0 = np.log(base["confidence"] / (1 - base["confidence"]))
w = np.zeros(dim)
for i in range(dim):
    e = np.zeros(dim); e[i] = 1.0
    lg = np.log(query_model(e)["confidence"] / (1 - query_model(e)["confidence"]))
    w[i] = lg - b0
```

**只给类别不给置信度时**：改用二分法在决策边界附近逼近，或直接训代理模型（决策树 / 小网络）拟合输入输出对，保真度 >99% 通常只需 1 万到 10 万次查询。

**API 有时延差异**：更深的模型响应更慢，可侧信道推架构。

## 6. 成员推断

训练样本上模型置信度更高、损失更低、熵更低。

```python
with torch.no_grad():
    logits = model(x.unsqueeze(0))
    p = F.softmax(logits, dim=1)
    conf = p[0, y].item()
    loss = F.cross_entropy(logits, torch.tensor([y])).item()
    ent  = -(p * torch.log(p + 1e-10)).sum().item()
score = conf - 0.5 * ent     # 高置信 + 低熵 = 更像成员
```

**只给类别标签时**：用扰动敏感性——成员对小幅扰动更稳（预测不易翻转）。

**增强一致性**：成员在多种数据增强下预测稳定，非成员会漂移。

**阈值必须标定**：绝对置信度阈值（如 0.9）在不同模型上完全不可移植，用一批已知成员/非成员算 ROC 再定阈值。

## 7. 纪律

- **先做差分，别先读论文**。两个权重文件都在时，`(orig - chal).abs()` 能直接指出改了哪几层，比理解训练方法快一个数量级。
- **区分「打模型」与「打壳」**。大量「AI 题」实际考的是提示词注入或解析器 bug，权重一行都没动。先 `file` 一下附件。
- **`model.eval()` 与 `alpha = lora_alpha/r` 是两个必错点**。前者影响所有梯度类方法，后者影响所有 LoRA 合并，写之前先确认。
- **flag 不一定在文本输出里**。权重图案、激活图、embedding 距离都能编码信息，文本生成不出东西时改去看矩阵。

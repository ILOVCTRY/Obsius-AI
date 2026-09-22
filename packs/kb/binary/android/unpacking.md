---
phase: unpacking
---

# 壳与脱壳（packed / unpacking）

> 来源：r0re 收编改编（2026-09-21）。UPX-shlib fold 与厂商加固的识别、unicorn 模拟
> 脱壳工程要点、脱壳后镜像的修复与阅读纪律。术语：脱壳（unpacking/dumping）。

## ① UPX-shlib fold 识别四征

1. `.text` 高熵（整体是压缩数据）；
2. 文件内嵌 `\x7fELF`——原始镜像被当数据折叠进壳内；
3. 节表矛盾——节头伪造、节大小与 program header 对不上；
4. `init_array` 里成簇裸 `svc #0` 系统调用 stub（自解压 loader）。

厂商加固（360/腾讯乐固/百度）方向不同：classes.dex 异常小或加密 + lib/ 带解壳
stub——那是 DEX 壳，dump 点在内存中还原出的 dex，不在 so 内 fold（特征词
`packed_so` 覆盖两类）。

## ② unicorn 模拟脱壳工程

照 loader 的真实行为模拟执行一遍，而不是静态猜解压算法：

- 环境：auxv/栈布局/memfd/mmap 都要装出来，loader 会真的去查；
- **MAP_SHARED 写回：munmap 时才同步回宿主文件——少模拟这一步 = 经典丢数据坑**，
  勿随手重实现，先抄对再改；
- dump 时机：loader 把原始镜像写完、跳转前后（对比 pc 落点）各留一份。

工具链（r0re 三件套，M2 落 tools/ registry 后直接用）：`upx_shlib_emu.py`
（unicorn 模拟 loader，处理 auxv/memfd/mmap）/ `nrv2b.py`（UPX arm64 stub 的
NRV2B 解压）/ `apply_relocs.py`（R_AARCH64_RELATIVE 修复）。工具缺席时按本节
要点自建——**能用现成的就不要重写脱壳器**。

## ③ 重定位修复

脱壳镜像必须跑 `R_AARCH64_RELATIVE` 修复——不修则 vtable/成员指针全读零，
看起来「算法空白」其实是重定位没修。修完 vaddr == 文件偏移，后续分析按偏移直读。

## ④ packed 字符串不可信

压缩/加密伪影会让 packed 文件里的 strings 全是噪声——**只信脱壳镜像里的字符串**。

## ⑤ 反汇编阅读纪律

capstone 线性扫描在嵌入数据上**失步**（数据被当代码解码，越走越歪）——字符串
交叉引用用 `adrp+add` / `adr` 逐字（per-word）手工解码，不信线性 sweep 给出的
函数边界；大文件先 `readelf` 定位段/符号再精读（沿 binary-rev 读取纪律）。

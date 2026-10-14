# cloud 能力域 · 知识库（packs/kb/cloud/）

云安全攻防模式深度手册库。按需读入上下文，不主动全量加载，共 74 篇。

> kb 全局单根（expert-pool M0，2026-09-21）：`packs/kb/` 一级目录=能力域，本目录=cloud 域
> （dsh-kb-sourcing 新建的第 6 包）。Agent 经 `kb_open(module=cloud/<模块>/<包内路径>)` 按需
> 打开单个文件，全局路径域前缀消歧。route.json 为任务导航（测试点 → 手册）。
>
> **树状化重构（2026-10-14）**：本域由旧的 knowledge/vendors/native/detection 四分类改为
> 中文类目模块树（映射：knowledge→云安全知识、vendors→云厂商、native→云原生、detection→
> 威胁检测）；各模块内部 README 保留逐文件索引与快速路由。本域为 dsh-redteam-model 原样
> 收编（不翻译、不就地改），发现问题走上游补回再重导。

| 模块 | 篇数 | 内容 |
|---|---|---|
| `云安全知识/` | 6 | 知识速查底座：ATT&CK Cloud Matrix、云 API 只读探测纪律、云安全工具卡、IAM 策略语言速查、六厂商元数据端点对照 |
| `云厂商/` | 42 | 六厂商云服务攻防（aliyun/aws/azure/gcp/huawei/tencent 各 7 篇）：ECS/EC2/VM 计算、VPC/安全组网络、OSS/S3/COS 对象存储、RAM/IAM/CAM 访问控制、RDS/Cloud SQL 数据库、SSRF 与控制台接管 |
| `云原生/` | 21 | 云原生攻防：`cicd/`（流水线攻击面/仓库权限/制品投毒/IaC 缺陷）、`container/`（逃逸路径/镜像供应链/网络运行时）、`k8s/`（暴露面测绘/RBAC 滥用/准入绕过/Secret 泄露/托管 K8s）、`serverless/`（函数权限触发器/环境密钥/供应链投毒/持久化） |
| `威胁检测/` | 5 | 检测侧：云审计日志体系（六厂商对照）、检测缺口分析方法论、检测规则设计、监控告警基线 |

## 使用约定

- 引用一律相对路径（cloud 内互引、playbook 引用本域）；禁止绝对路径。
- 每个利用路径条目自带检测侧对照（该攻击在云审计/监控里的可见性）。
- 正文无来源痕迹；技术事实（API 名/端点/策略语法/ATT&CK ID）正常保留。
- 附录 A=六厂商元数据端点与实例身份对照（`云安全知识/metadata-service-endpoints.md`）；
  附录 B=云 API 只读探测纪律与速率默认值（`云安全知识/cloud-api-readonly-probing.md`）。

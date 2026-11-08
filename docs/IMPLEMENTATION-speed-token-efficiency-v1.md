# 搜客速度与 Token 成本优化：实施记录

记录版本：1.6｜2026-11-03｜依据：[规格 1.0](specs/SPEC-speed-token-efficiency-v1.md)

本记录覆盖 M0 的部分准备及 M1 隔离开发，**G0 和 G1 未通过，规格未实施完成**。离线报告计算器的开发版本是 `0.1.0-dev`，报告合同版本是 `cost-baseline-1`；这些标记不是 Skill 或核心的发布版本，也不声明升级或安装。

## 实际版本与任务

2026-11-03 通过已安装入口 `capabilities` 核对：Skill/核心 **2.2.0**，规则 `b2b-policy-2`，schema `skill_v2_002`。源码工作区版本标记为 **2.3.1**，仍是未发布开发状态。用户指定的历史任务 `us-air-freight-20261102` 冻结的是 **2.3.0**、规则 `b2b-policy-2`，发布 ID `3d98bbb70431a27e1baabe961c2abbf3d7c34468d87153165a2f1a3e8d8b9994`；不得以当前安装版本替代其历史版本。

该任务的目标是美国总部空运货代，目标 200 家，任一公开商务邮箱、电话或表单即可满足渠道政策。读取 status 的结果为 partial、输入关闭、停止原因 time_limit；已发现 21 条、去重后 19 家、准入 19 家、资格匹配 14 家、可交付 9 家、未达目标 191 家。处理失败 3 家（访问限制 2、容量 1）；页面计数 62。状态返回的外部搜索和页面打开调用数均为 null。以上是现有快照，不能补写为完整发现漏斗或 30 家样本。

任务开始 `2026-11-02T07:11:00+08:00`，结束 `2026-11-03T01:10:01+08:00`。状态中的 `research.active_wall_seconds` 为 54688.174 秒，`stats.active_seconds` 为 382.653 秒；两者语义不同，尚未核对为本规格的活动时间，故不计算每活动小时交付数。快照中最早达到至少 5 行的记录是 6 行，不能当作精确的第 5 家时间。输入、输出和缓存 token、计费范围、工具返回字符数及 Agent 轮次尚无可审计记录，均为未知；不能用字符或调用次数替代 token。只读数据库 `skill_runs.spec_hash` 得到冻结配置 hash `96eb03bea382ce15e69cb5de5ceafcfa9324f361b404b9098b8534d1330f178f`。既有任务已关闭且达到时间限制，本次没有改动或续跑它。

为补足真实基线，另建了 `us-air-freight-cost-baseline-20261103`，复制原任务冻结客群配置，经已安装入口创建，冻结核心 2.2.0、规则 `b2b-policy-2`。该 run 已保存检查点并暂停，暂停时活动墙钟 1096.844 秒。检查点链保存 43 条按名称初步去重的目录／官网发现线索，主体、关联企业及跨来源重复尚未完整核查，不能称为 43 家已去重候选；目前准入和交付均为 0。记录的 Agent 搜索工具调用为 6 次、打开／点击请求为 50 次；这不是核心页面计数或物理 HTTP 次数，物理请求和 token 仍为未知。run 创建前进行过预搜索，不能作为冷启动速度对照。继续该实验应使用此新 run 的原检查点，不修改旧 run；资格事实及官网归属确认后才提交首批，旧规则无法识别的国家别名保持未知。

## 已完成的受控准备

- `scripts/cost_baseline.py` 是独立、离线的报告校验和计算工具，不接入正式研究流程；按版本化字段记录可观测量、来源、范围及未知原因。仅当输入和输出 token 的来源及范围一致，且缓存属于输入或缓存为零时计算两者之和；缓存不重复相加，零交付不计算单位 token。它不会自动认证 G0。
- `backend/tests/test_cost_baseline.py` 覆盖未知与零交付、缓存、不同范围、漏斗及非法额外字段；这些测试验证计算器，不代表真实业务质量验收。模型字段定义可用以下命令查看：

```powershell
.\.venv\Scripts\python.exe -c "import sys,json; sys.path.insert(0,'scripts'); from cost_baseline import Report; print(json.dumps(Report.model_json_schema(),ensure_ascii=False,indent=2))"
.\.venv\Scripts\python.exe -B scripts\cost_baseline.py <脱敏报告.json> --output <计算结果.json>
```

录入者必须人工检查脱敏；校验器不能识别所有自由文本中的凭据或客户名单。阶段并发耗时不得相加作为墙钟，调用、页面及物理请求分别记录。`first_five_seconds` 未达到或无法确定时填 null 和原因；缓存计数仅是输入子集时不得再加。模型无法证明来源真实性或成本口径等价，必须保留原始记录并人工审计。

- 运行现有 `scripts/benchmark_speed.py` 对源码做了一次离线合成测量：每批 6 家、3 次、模拟请求延迟 0、无外部请求；批次中位数 0.522 秒，36 次模拟页面请求，原始结果在 `.runtime/speed-token-m0-synthetic.json`。这一轮仅供检查本机运行，不是固定捕获样本的回放、真实成本基线或版本对照，也不能用于推断线上提速。

- `scripts/evidence_stage_prototype.py`（`evidence-stage-prototype-0.1`）是隔离开发原型：校验来源文本 SHA-256、时区、主体条目边界、字面摘录、搜索摘要及部分单值冲突；提供字节有界分页、按 ID 回读，以及将受支持的已观察字段整理为草稿、调用原有预检、返回子集和内容哈希。过期、未经核验或有冲突的观察使整个候选待核。固定的合成目录文本位于 `backend/tests/fixtures/speed_token_directory.txt`，SHA-256 为 `8d554b5d41b9a32c36ed06a8409256da6c33197b153ffb3b922e97c1e7cbd2fe`，测试中固定两条主体、邮箱及预期结果并重复回放。此原型不做语义证明或资格判断；字面匹配也不能证明陈述主体与事实的语义关系。
- `scripts/evidence_store_prototype.py`（`evidence-store-prototype-0.1`）增加了**仅供合成测试**的隔离 SQLite 持久化试验：同 run 与操作键可重放，不同输入冲突；跨 run 不读取；校验保存内容哈希；生成草稿的事务失败可重试；过期后拒绝读取并可清除正文，保留哈希墓碑防止误用旧键。草稿以来源到期时间为上限；仅保存配置哈希，不保存配置或排除名单。此试验**未**接入现有 PostgreSQL、访问控制、工件备份或维护清理，不适合存真实客户数据。SQLite 清除正文的测试也不证明备份、日志或底层存储的物理擦除。
- 一组合成观察的转换结果与手工构造输入逐字段相同，且离线预检和现有政策评估结果相同；这只是单夹具测试，不能替代 G1 的完整主体、渠道、交付及人工真值对照。
- 已对指定真实任务建立固定页面引用清单 `.runtime/cost-baseline-real-refs-20261103.json`（`cost-baseline-page-refs-1`）：按照企业 ID、快照 ID 排序，选取前 5 个不同企业各一份未过期且哈希匹配的工件；没有复制页面、URL、客群配置或排除名单。清单 SHA-256 为 `cbd5b4447419bc807cd5277b044453cd7bc2618aa699b438da8fa8387e2488aa`；首次核验 5/5 份匹配。`scripts/verify_baseline_refs.py` 可再次核验清单、冻结配置／发布及当前工件，过期、清理或修改会明确报错；哈希只是完整性校验，不证明来源真实性。快照元数据不直接给出采集时间，人工真值也尚未审核，不能把这份五家样本当作 G0 通过。
- [共享页面预算合同草案 v0.1](DESIGN-shared-page-budget-v1.md)记录了现有企业预算与 HTTP 观测的区别及未来验收要求，只是设计，不改写运行时。
- 本次针对原型、基线引用、报告、现有预检、版本、企业并发和请求观测的测试共 **40 passed**；补完原始／整理输入对照后，原型、基线、预检、版本与并发这组 8 个文件为 **36 passed**（含新增 3 项），包含项目现有的隔离 PostgreSQL 测试；不等于 M1 或共享目录的完整验收。

## 原始输入与整理输入对照（2026-11-03 完成）

- scripts/compare_stage_inputs.py（stage-comparison-1）把同一组观察分别交给两条路径：按现有习惯手工拼装的 EvidenceBundle v2 候选，以及 evidence_stage_prototype.draft 生成的草稿；两侧都走生产 alidate_input 预检，准入后再用同一冻结政策 ssess_policy 判定。无网络、无数据库、无准入或交付写操作。
- 固定矩阵 11 组、14 条候选：4 条两侧都准入且候选字典逐字节相同、decision 同为 qualified；1 条两侧都拒（search_snippet 只能作发现线索）；9 条整理侧单独扣留，理由为 MULTIPLE_SUBJECTS、DUPLICATE_SUBJECT_REVIEW、CONFLICTING_VALUES、EVIDENCE_OUT_OF_DATE、IDENTITY_MISMATCH、UNSUPPORTED_FIELD、EXCERPT_UNVERIFIED、WEBSITE_UNCONFIRMED（后两项由本次补的用例触发）。没有出现整理侧放行、手工侧拒绝的反向偏差，ok=true。
- 准入与交付等价（ackend/tests/test_stage_delivery_equivalence.py）：4 条通过预检的候选各建 manual/staged 两个隔离 run，经真实 
uns.create/dvance 准入；两侧企业记录的状态、渠道、条件、联系人和事实来源逐项相同，均为 qualified 且渠道同为 email；同一 chunk 重放不新增候选。SQLite 原型的重放、冲突、过期清除与事务重试仍由既有测试覆盖。
- 本次修正的一处实现缺口：draft() 原来会把只作为发现线索出现的 website_url 静默丢掉，WEBSITE_UNCONFIRMED 分支不可达；现在遇到该类线索整条扣留并返回 WEBSITE_UNCONFIRMED，不再静默改写成无官网。
- 仍未闭合：真实页面写 Germany、观察值写 ISO 代码时仍判 VALUE_NOT_LITERAL，需要人工复核而不是放宽；并发准入、中断恢复只在原型与单进程测试中验证过；整批固定样本的 token 对照没有可审计数据，不对 token 节省作任何声明。
- 结论：M1 的同一证据两种输入形态对照（含准入与记录的交付等价）已经完成并通过；G1 仍未宣称通过——真实 token、并发/中断的集成级验证和正式接入均未完成。

## 未完成的门槛## 未完成的门槛

1. G0：上述真实任务只有 19 家去重候选，缺少可比 token 和完整阶段数据；已有两主体合成文本夹具和 5 份真实工件引用，但真实样本的采集时间、完整人工真值及旧新版交付结果对照尚未完成。G0 未通过，不能进行依赖真实对照的正式验收。
2. M1/G1：当前是内存和合成数据 SQLite 原型；与实际权限、备份及保留清理的集成、完整字段转换、可提交子集文件和完整交付等价性／恢复测试均未完成，未接入正式路径。G1 未通过。
3. M2/G2：共享目录统一预算及 PostgreSQL 并发、恢复验收未完成；自动共享目录采集保持禁用。M3/G3 未实施。
4. 未完成 25% token、15% 首批时间和吞吐目标的可比测量；未冻结发布、安装或执行回滚验收。既有 run 的冻结设置保持原样。

## 2.5.0 接入（2026-11-03）

转换器从 `scripts/` 移入 `backend/app/evidence_stage.py`，并新增两个入口：`stage-draft`（离线生成可预检子集）与 `advance --artifact-json`（同进程整理后直接提交，chunk 缺省取内容哈希）。所有导入仍走现有 `validate-input`/`advance`，默认研究流程、冻结规则与交付口径不变。每次调用在 stderr 打印版本横幅，JSON 结果带 `versions` 字段。已打包安装为 2.5.0，release `da1377eec74cba79ded1093b33afd6df4286c2b4217da4b70abdaca24c9f98c1`；G1 的并发/中断集成与真实 token 对照仍未完成。

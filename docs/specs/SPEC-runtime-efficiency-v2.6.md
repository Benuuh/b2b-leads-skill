# SPEC：b2b-leads 2.6.0 运行效率优化

- 状态：待实施
- 适用基线：当前工作树 Skill/Core `2.6.0`，规则 `b2b-policy-2`，schema `skill_v2_003`
- 目标：在不改变证据、资格、渠道、去重、抑制、预算、恢复和交付口径的前提下，降低协调器、交付选择、导出及发现链路的耗时与资源消耗
- 非目标：提高 worker 上限、放宽质量规则、绕过 robots/限速、增加常驻服务、启用尚未具备统一预算的共享目录自动采集

## 1. 基线确认与结论边界

### 1.1 实际版本

根目录 `VERSION`、`backend/app/__init__.py`、`backend/pyproject.toml` 和 `skills/b2b-leads/SKILL.md` 均为 `2.6.0`。本次从源码入口执行 `capabilities`，返回 Skill/Core `2.6.0`、schema `skill_v2_003`、规则 `b2b-policy-2`，release 为 `development`、`installed=false`。因此本规格针对当前源码工作树，不把源码入口结果冒充已安装副本的独立验收。

当前 Git HEAD 为 `e656188`，但 2.6.0 大量文件是未提交工作树内容。实施前必须冻结一个可复现基线提交和 release manifest，否则性能对照与回滚都不可靠。

### 1.2 当前执行链

```text
Skill/control.py
  → scripts/b2b_leads.py → app.cli
  → prepare/maintenance
  → advance
      → append：预检、去重、原子写入、进度投影
      → process：claim → 单企业 pipeline → complete
      → snapshot/live
  → export：selection → metrics → openpyxl/CSV → artifact
```

2.6 已具备的有效提速能力，不重复建设：

- `advance` 合并预检、追加和有界处理，减少控制器启动。
- v2 默认 2、最大 4 个公司 worker；speed-first 可冻结为 4。
- 已存证据充分时零网络，只补缺项，满足渠道政策即停止。
- 单企业复用 HTML 解析和联系提取结果；网络层有连接池、robots cache、同 host 限速。
- live 已复用提交时 detail cache，热投影约毫秒级。
- 2.6 新增 `eu_ted`、`eurostat_comext`、`google_places` 顺序发现支路。

`native_inflight` 仅存在于 schema/capabilities，运行路径未读取，不是有效性能旋钮；本规格不以调整它作为优化方案。

### 1.3 已有测量及其限制

| 证据 | 当前记录 | 不能证明什么 |
|---|---:|---|
| `docs/IMPLEMENTATION-live-results-speed-v1.md` | 15 家正式导出约 6.40 s；热投影 p50 约 5.6 ms | 不能推导 200/600 家耗时；记录已指出主要成本在 selection 和 metrics |
| 同文件 | 15 家入库、页面、导出约 32.5 s | 受控小样本，不是在线吞吐或 200 家 SLA |
| `bench/2026-11-04-baseline.json` | 16 家、3 轮、最多 4 页；单 worker 三轮总耗时中位数 223.657 s | 仅 `fetch_company`，不含协调器、DB、提取、投影、导出 |
| `bench/2026-11-04-optimized-w4.json` | 同规模 4 workers 中位数 52.677 s | 与 baseline 的代码和页数同时变化，不是干净 A/B；实时页面也会变化 |

目前没有可信的 200/600/1000 家全链路基线。本规格中的相对降幅是发布门槛，不是对当前收益的预估或既成事实。

## 2. 现状问题定位

### P0：测量和正确性前置项

1. `backend/app/web/http.py:179-191` 重复定义 `_new_pinned_connection`，后定义覆盖前定义；HTTP 多地址回退合同不清。先合并实现并补测试，避免把行为差异误计为性能收益。
2. `scripts/bench.py:74-136` 接收并报告 `timeout_seconds`，但未绑定 `request_deadline`；`failures` 只统计异常，不统计 `failed_pages`。现有报告中的 `timeout_seconds=30` 和 `failures=0` 不能按字面解释。
3. `backend/app/discovery/service.py:60-89` 以“读取 source_ref + Python 集合 + 插入”去重；模型未提供 `(run_id, source, source_ref)` 唯一约束。发现并发化前必须补数据库约束与冲突测试。

### P1：run 级全量交付选择被反复执行

1. `runs/_core.py:62-64` 每次 `_rows()` 读取并深拷贝全 cohort；`_summary()` 又分别执行 qualified filter 和完整 `delivery_companies()`。
2. `runs/state.py:42,146-147` 一次 append 在准入前、准入后和返回视图时形成三轮投影；`state.py:311` 每次 claim 也执行 `_refresh()`。
3. `state.py:477` 每家公司 complete 都执行 `_refresh(detail=True)`；20 jobs 随 cohort 增长呈明显的 `O(J×N)` 读放大。`state.py:443-444` 还为同域关联再次扫描全 cohort。
4. `state.py:249-250` status 先 `_refresh()` 再 `_view()`，后者又 `_summary()`；`runs/query.py:18-20` list 对每个 run 都生成完整视图。
5. speed-first 每 5 家快照通过 `state.py:671-678` 调用完整 export，放大上述成本。

结论：当前首要热点是“全 cohort 的重复读取、深拷贝、交付选择与序列化”，不是单纯 HTML parser，也不是 worker 数不足。

### P1：selection、export 和 metrics 内部重复工作

1. `delivery/selection.py:598-645` 的 `channel_status()` 为单家公司再次进入完整 `filter_companies()`。
2. `selection.py:675-737` 的 `delivery_companies()` 先做全 cohort filter，之后对每家公司再次 `channel_status()`、单公司 filter，并可能再次 `channel_status()`。
3. `selection.py:764-791` 的 delivery-only 导出前后各执行一次 `delivery_companies()`，中间还执行 `filter_companies()`。
4. `runs/query.py:155-205` 冷导出先 selection 生成 key，再多次 `_summary()`；`delivery/exporting.py:70-73` 又 selection；`runs/query.py:96` 和 `delivery/metrics.py:235-247` 再重复 coalesce/filter。
5. metrics 对 IDs、policies、完整 rows 做稳定 JSON 哈希；export 在事务内构建完整 workbook 和 BytesIO。必须分阶段计时，不能把全部成本归因于 Excel。

### P2：单企业重复序列化和评估

1. `pipeline.py:561-572` 每增加一页都为当前全部页面执行 `json.dumps` 来构造 extraction key；P 页时存在 `O(P²×页面大小)` 的序列化趋势。
2. `rules/policy.py:347-375` 即使 LRU 命中，也需完整 stable JSON、SHA-256 和 deep copy；指纹还包含 300 秒窗口，不能简单跨窗口复用。
3. `pipeline.py:277-291` reassess 先 `assess_policy()`，随后 `qualify_v2()` 未传 assessment，可能再算一次。
4. 联系提取包含 HTML/text 拼接、邮箱和电话扫描、provenance 等多阶段；只有分项 profile 后才能决定是否替换 parser 或电话库。

### P2：2.6 发现支路

1. `discovery/service.py:96-104` 三源明确顺序执行，且缺少与 company process 一致的命令总 deadline/运行上下文。
2. `discovery/adapters.py:128-173` Eurostat 最多 5 国×6 月串行请求，每次 `delay=1`；仅等待可接近 30 秒，且单月异常被直接跳过，难以区分无数据和持续故障。
3. `discovery/envelopes.py:44-50` 每轮用 `sum(len(block) for block in blocks)` 重算 keystream 总长度，块数为 B 时是 `O(B²)`；大 raw payload 还产生多份完整 bytes/base64。
4. `discovery/service.py:121-136` 候选池一次读取全部记录并输出，缺少分页。

## 3. 优化目标与量化验收

### 3.1 统一测量口径

- 使用固定页面 replay 和固定数据库 fixture，覆盖已有 0/200/600/1000 家 cohort；每次追加 20、处理 20、导出全量。
- 同一机器、Python/PostgreSQL 版本、同一冻结 spec 和输入；旧新各至少 5 轮，报告 p50/p95、标准差、峰值 RSS、SQL 次数、Company 行读取数及各函数调用数。
- 分离冷/热缓存；分离网络等待、公司 pipeline、协调器、selection、metrics、Excel、fsync/hash。固定 replay 用于严格 A/B，在线 canary 只作独立观察。
- 物理 HTTP attempts、公司页面、robots/sitemap/redirect 分别计数；同时报告公司异常与 `failed_pages`。
- 基线尚未测得的绝对值填 `null`，不得用 15 家结果外推。以下相对目标均以同 fixture 的 2.6.0 冻结基线为分母。

### 3.2 发布门槛

| 指标 | 目标 |
|---|---|
| 已有 600 家时 append 20 家的协调器耗时 p95 | 降低 ≥70%；完整 delivery selection 最多 1 次，最终返回不重复 summary |
| 20 jobs 的 run 级全量 selection 次数 | 较基线降低 ≥60%；claim 路径为 0，最终状态必须重算或来自同 revision 的有效投影 |
| 200 家冷导出 wall time p95 | 降低 ≥70%；同一 cohort revision 只生成 1 份 selection，metrics 不重新 delivery selection |
| 200/600/1000 家导出耗时增长 | 拟合不得呈明显二次增长；1000 家无超时、无超过既有 100 MiB/10000 行限制之外的失败 |
| 600 家时单公司 complete 的边际协调成本 | 降低 ≥60%；live 最坏可见仍 ≤5 s，snapshot 计数与最终 export 一致 |
| 200 家冷导出峰值 RSS | 不高于基线；目标降低 ≥20%，不得靠驻留全量深拷贝副本换取耗时 |
| 单企业 1/2/3 页、64 KiB/512 KiB/2 MiB HTML | 页面内容序列化总量降为 `O(总页面字节)`；reassess 每轮 policy 评估次数减半 |
| 1 MiB envelope seal/open | 保持格式和解密结果不变，keystream 生成改为 `O(B)`；耗时目标降低 ≥50% |
| 正确性 | 企业、联系人、来源、渠道、排序、资格、去重、ownership、suppression、计数和 manifest 非时间字段 100% 等价 |
| 安全与恢复 | 页面/物理请求不超预算；robots、DNS 固定、host limiter、发送前 durable observation、中断恢复零回归 |

若相对性能目标未达到，但正确性通过，可合入低风险局部修复，不得以“整体提速完成”发布下一阶段。

## 4. 具体优化方案与取舍

### M0：冻结基线并修正测量（前置，不改变业务结果）

1. 合并 `_new_pinned_connection`，明确 HTTP 多地址回退；补 HTTP/HTTPS、首地址失败、连接复用测试。
2. 让 `bench.py::timeout_seconds` 真正进入 `request_deadline`，或删除该参数；汇总 `failed_pages`、物理 attempts 和缓存状态。
3. 新增 `benchmark_projection.py`：复用固定 fixture，埋点 `_rows`、`_summary`、`filter_companies`、`delivery_companies`、`channel_status`、`calculate_metrics`、workbook save 和 artifact put。
4. 保存基线 release、配置 hash、机器信息、原始 JSON 和结果等价快照。

取舍：先增加少量诊断代码和测试成本，换取可归因数据；不在同一变更中混入算法优化。

### M1：每次操作只构造一次 RunProjection（第一发布候选）

新增内部 `RunProjectionContext`，不作为外部 API：

```text
run_id + run.version + suppression_version + as_of
rows / qualified / ownership_view / contactable / pending / research / detail
```

- `_rows()` 每 context 只读取一次；`_summary()` 接收预加载 rows 和共享 `now`。
- `_refresh()` 返回完整 projection，而不只返回 detail；`_view(session, run, research=...)` 复用结果。
- append 使用持久化计数做准入前轻量检查，提交后只构造一次最终 projection。若 `as_of` 已跨过证据 fresh 边界、计数缺失或 revision/suppression 变化，必须重建，不能读陈旧计数。
- claim 使用已提交预算、goal、stop 和 time 计数，不做完整 delivery detail；process 开始及结束仍生成权威 projection。
- status 将 `_refresh()` 的 research 直接传给 `_view()`；list 改为持久化 counters 的轻量视图，详细 spec/plan/selection 仅单 run status 返回。
- 同域关联改为 `(run_id, domain)` 索引/辅助映射；若暂不加生成列或索引，先在单事务 rows context 建 domain map，禁止再全表查询。

取舍：request/transaction 内复用比跨命令缓存失效简单，不需要新 schema。claim/list 依赖持久化计数，因此必须保留 freshness 重建条件和最终权威重算。

### M2：选择内核单遍化 + 协调器检查点合批（核心收益）

1. 将 selection 拆为三个内部阶段：
   - `prepare_cohort`：一次 coalesce、rank、eligibility、suppression 和 property filter。
   - `freeze_ownership`：按原顺序冻结 domain/contact/form ownership，保持现有“先做全局 ownership，再做交付过滤”语义。
   - `project_delivery`：在已冻结视图上校验 channel policy、redact、重建 sources 和评分，不再递归进入完整 `filter_companies()`。
2. `channel_status()` 增加仅接受 prepared row 的内部实现；公共入口保留，避免改变其他调用方。不得在 prepared row 上省略证据、新鲜度或 suppression 检查。
3. `select_export_companies()` 接收共享 prepared context；delivery-only 不再前后执行两轮完整 delivery。
4. `_complete()` 先持久化 Company 和轻量统计；process 协调器在“收到完成事件后最多 1 s、快照检查前、命令结束前”生成一次 detail projection 并提交 live cache。无完成事件不刷新。
5. snapshot 判断、live 和 process 最终返回共享同一 checkpoint revision 的 projection；export 若需新事务，则以新 revision 重建一次，不能跨事务复用可变 rows。
6. 增加运行开关 `B2B_BATCH_PROJECTION`，默认可回退为逐公司 `_refresh(detail=True)`；它只改变投影时机，不改变最终交付结果。

取舍：合批会让运行中的计数最多滞后约 1 s，但保留现有 live ≤5 s 目标，并将 4 workers 的完成事件合并处理。若业务要求每次 DB commit 后立即可被独立 status 读取精确计数，则 status 检测 projection revision 落后时同步重建，不能展示旧计数冒充最新。

### M3：ExportContext 与 metrics 复用

- 在一次 export 事务内构造 `ExportContext(records, projection, research, observations, now, suppressions)`。
- export key、manifest、metrics、workbook 和返回 `_export_view` 共享 context；`build_export` 接受已选择结果，不再次调用 selection。
- `calculate_metrics` 接收 prepared cohort 和 output projection；保留 batch/view/output 三种口径，但共享 coalesce、filter 和稳定哈希输入。
- `_metrics_context` 复用 research/rows；观察指标优先用 SQL 聚合，无法简单表达的 completeness 逻辑可暂留 Python，但不得重复查询。
- 缓存导出仍验证 artifact/report hash；本轮不取消安全校验。大文件流式 hash 和 write-only workbook 只有在 profile 证明其占比后单列实施。

取舍：导出函数签名会增加内部参数，但无需改变 CLI/文件格式。暂不做异步导出，因为当前无常驻 worker，且事务/撤销/一致性成本高于收益。

### M4：单企业与发现链路的低风险局部优化

1. 页面加入 `current` 时计算一次内容 digest；extraction key 只组合已有 digest，不重复序列化历史页面。保留全文变化、同 URL 不同内容、页面顺序和离线/在线隔离测试。
2. reassess 将已算 assessment 显式传给 `qualify_v2`；为 policy cache 增加命中率和 fingerprint 耗时，再决定是否做内容增量指纹，不直接删除 300 秒 fresh 窗口。
3. envelope 用 `generated += len(block)` 替代每轮 sum；保持 PSV1、nonce、MAC、base64、异常合同和测试向量不变。
4. Eurostat 先做一次多 `time` 参数契约探测：若源支持，则每国最多 1 个多月请求；不支持则保持串行并增加月份缓存。连续失败必须分类，不再全部当作单月无数据。
5. 跨 provider 并行放在唯一约束、统一 deadline 和观测上下文补齐之后；同 host 请求不并行突破 limiter。`discover --list` 增加游标和上限。

取舍：优先减少逻辑请求和二次序列化，不引入新的异步采集框架；共享目录自动采集继续禁用，直至统一页面预算验收完成。

## 5. 改动范围与风险

| 范围 | 文件 | 主要风险与控制 |
|---|---|---|
| 基准/观测 | `scripts/bench.py`、`scripts/benchmark_live.py`、新增 projection benchmark | 防止参数虚设、冷热混杂和 failed_pages 漏报 |
| run 投影 | `backend/app/runs/_core.py`、`state.py`、`query.py` | 陈旧计数、重复/漏刷新；以 run revision、freshness transition 和最终重算兜底 |
| 选择内核 | `backend/app/delivery/selection.py` | ownership、抑制、排序和渠道政策变化；旧新实现全量 golden diff |
| 导出/指标 | `backend/app/delivery/exporting.py`、`metrics.py`、`runs/query.py` | manifest hash、batch/view/output 口径变化；共享 `now` 和输入快照 |
| live/快照 | `backend/app/live/projection.py`、`runs/state.py` | 中间状态滞后；1 s 合批上限、5 s 可见门槛、关闭开关回退 |
| 单企业 | `backend/app/pipeline.py`、`rules/policy.py` | 错误复用评估；键必须覆盖完整页面内容、政策和规则版本 |
| 发现支路 | `backend/app/discovery/*`、models/migration | 并发重复、源 API 语义变化；唯一约束单独 migration，API 探测失败则保留原路径 |
| 正确性前置 | `backend/app/web/http.py` 及测试 | 连接行为改变；独立测试和发布，不混入性能收益统计 |

M1–M4 的核心投影/导出/单企业改动优先不加数据库字段，使用现有 `plan_json/stats_json` 和事务内 context，降低回滚难度。发现唯一约束若实施，必须单独升为下一 schema revision，不与投影优化捆绑。

明确不做：提高 4 worker 上限、把 `native_inflight` 当成已实现并发、减少证据页但不改变规则、跳过 durable start、移除 freshness 时间窗、默认关闭快照、异步后台导出、未验证的 parser 替换。

## 6. 验证方案

### G0：基线可信

- 冻结 2.6.0 release；benchmark timeout 生效，异常和 failed_pages 分开。
- 固定 0/200/600/1000 cohort 和 30 家页面 replay；记录完整原始结果。
- 未满足 G0 不宣称任何相对提速。

### G1：结果等价与安全回归

- v1/v2、`any/all`、named person/roles、共享 domain/email/phone/form、suppression、freshness 边界、冲突/未知、分支主体全部做旧新 canonical JSON diff。
- export/live/progress/snapshot 的企业、联系人、来源、渠道、排序和计数一致；除生成时间、耗时、release 等白名单字段外 manifest 无差异。
- chunk/operation replay、暂停/恢复、租约过期、完成提交失败、物理发送前后崩溃、最后一份页面预算竞争通过 PostgreSQL 集成测试。
- robots、redirect 每跳检查、DNS 固定、429 cooldown、observation start/finish 和保守恢复不得回归。

### G2：性能与规模

- 执行 §3.1 矩阵，达到 §3.2 的 selection 次数、耗时、内存和 live 门槛。
- workers=1/2/4 分别测 coordinator+DB；snapshot 关闭、每 5 家、300 秒分别测，避免把快照配置差异算作代码收益。
- export 分冷/热；live 分提交 cache、文件 cache、冷构建和 suppression 变化；网络 replay 与在线 canary 分开报告。

### G3：灰度与发布

1. 只在隔离测试库和新 run 开启 `B2B_BATCH_PROJECTION`，旧 run 默认原路径。
2. 连续执行完整测试、固定 replay、200 家冷导出和中断恢复；保存旧新 release 与报告。
3. 小批在线 canary 只观察，不用实时网页变化推导固定倍数；出现等价差异立即停止。
4. 通过后再冻结新 release，并用安装入口校验完整性；未验证项在 capabilities/实施记录中保持未声明。

## 7. 回滚方式

### 7.1 回滚触发条件

任一条件触发即停止灰度：交付/证据/ownership 差异、预算或 observation 漏记、live 最坏可见 >5 s、导出错误率上升、同 fixture p95 回退 >10%、峰值 RSS 超过基线、数据库锁等待显著增加。

### 7.2 无 schema 变更的主路径

1. 将 `B2B_BATCH_PROJECTION` 切回关闭，恢复逐公司权威刷新；新开关只影响投影时机，不重写历史 Company/Export。
2. 记录当前 `prepare/status` 的 release ID，备份数据库与 artifacts，使用既有 `scripts/skill_release.py --destination <安装目录> --release <已验证的2.6.0基线release>` 回退。
3. 删除仅属于新版本的内存/临时 projection cache；已生成 export 按原 hash/manifest 验证，live projection 可由旧版重建，不作为权威数据源。
4. 回滚后重放同一 chunk、process operation 和 export key，确认不重复追加、不重复扣页面、不重复交付；运行 G1 核心回归。

### 7.3 若已应用 discovery migration

不得直接假设旧代码兼容新 schema。先保留 additive 约束并用当前代码做 forward fix；只有完成旧 release 读写、续跑、导出及约束兼容测试后才允许代码回退。无法满足时从升级前备份恢复到独立环境验证后切换，不在生产库临时drop约束或改历史 run。

## 8. 建议实施顺序与发布切片

1. **切片 A：M0 + 明确 bug 修复**——得到可信基线，不宣称提速。
2. **切片 B：M1 + M3**——先做事务内复用和导出单选，风险较低，可独立回滚。
3. **切片 C：M2**——选择单遍化和检查点合批，是主要吞吐收益来源，必须完整 golden diff 和并发恢复验收。
4. **切片 D：M4**——单企业 digest、reassess、envelope 先行；Eurostat/跨源并行和 DB migration 独立发布。

核心决策：先把“同一批数据反复读取和筛选”降下来，再讨论网络并发或替换解析器。否则增加 worker 只会让更多线程同时等待 Run 锁、数据库和重复 projection，可能放大尾延迟而非提高有效交付吞吐。

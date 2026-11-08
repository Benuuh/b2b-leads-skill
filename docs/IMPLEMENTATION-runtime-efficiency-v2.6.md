# 运行效率优化实施记录（2.6.0 → 2.7.0）

- 实施范围：`docs/specs/SPEC-runtime-efficiency-v2.6.md` M0/M1/M2/M3/M4 中可在当前工作树独立验证的部分。
- 基线：Skill/Core 2.6.0，规则 `b2b-policy-2`，schema `skill_v2_003`。
- 结果版本：Skill/Core 2.7.0，规则 `b2b-policy-2`，schema 仍为 `skill_v2_003`（未实施 discovery 唯一约束迁移，因此不升 004）。
- 源码记录；性能数据来自隔离 `sca_bench_*` PostgreSQL 测试库和固定 replay cohort。本地安装/公开发布状态由使用者环境决定。

## 1. 实际改动

### M0 测量与正确性前置

- `backend/app/web/http.py`：删除重复的 `_new_pinned_connection`，HTTP/HTTPS 共用同一地址列表；HTTP 连接也按已验证 IP 依次回退，主机名不再二次解析。
- `scripts/bench.py`：`timeout_seconds` 通过 `request_deadline` 成为每家公司真实截止时间；分别记录异常 `failures`、`failed_pages`、observation `start` 物理请求数、已 dispatch 数；原始 JSON 保留每轮结果。
- `scripts/benchmark_projection.py`（新增）：固定 fixture 的 A/B 基准，支持 `append/status/export/process/complete`、0/200/600/1000 cohort、隔离数据库、SQL 次数、selection 调用次数、tracemalloc 峰值与进程 RSS。
- `backend/tests/test_runtime_efficiency.py`、`test_connection_reuse.py`：覆盖 timeout、failed_pages、物理 attempts、HTTP 多地址回退、信封、policy cache、discover 分页。

### M1 事务内 RunProjectionContext

- `backend/app/runs/_core.py`：新增 `RunProjectionContext`，一次事务内共享 rows、suppressions、qualified、contactable、pending 与 summary；`_refresh` 返回该 context，`_view` 复用；`_summary` 保留兼容入口。
- `append`：准入前只读持久化 stop/budget 计数（`_refresh_light`），提交后只构造一次最终 projection；最终 projection 同时交给 live cache。
- `claim`：`_refresh_light` 只读持久化计数器、suppression 版本与 ≤300 秒 freshness 窗口；claim 路径不再运行全量 delivery selection。
- `status`：`_refresh` 的 research 直接传给 `_view`。
- `list`：`_list_view` 只读持久化 counters，不读取全 cohort，也不生成 spec/plan/selection 明细。
- `_complete`：同域关联改为按 run+domain 的 JSONB targeted query，不再把全 cohort 读入 Python。

### M2 选择内核单遍化与检查点合批

- `backend/app/delivery/selection.py`：
  - 新增 `prepare_cohort`（一次 coalesce、suppression/expiry/property filter、全局 email/phone ownership freeze）。
  - 新增 `_finalize_company`，把单行来源/冲突/聚合重建从 `filter_companies` 尾部抽出；delivery 对已通过 cohort ownership 的行复用该 finalizer，不再为每家公司再跑一次 `filter_companies([company])`。
  - `delivery_companies` 使用 prepared cohort；`channel_status` 公共入口保留完整 standalone 语义，另设 `_channel_status_prepared` 给已过滤行使用。
  - `select_export_companies` 首次 delivery 后，后续只在已选子集上重新校验，不再对全 cohort 执行第二轮 delivery。
  - 优先使用 pipeline 已持久化的 `website_binding_status` + `website_binding_evidence`；仅有自声明状态时仍重新计算（回归测试保留）。
- `backend/app/runs/state.py`：
  - `_complete` 在批量模式只提交 Company/统计并记录 `projection_pending`；不再逐公司 `_refresh(detail=True)`。
  - `_checkpoint_projection` 在“收到完成事件后 ≤1 秒、快照阈值、命令结束前”构造一次 detail projection，并发布 live cache。
  - `B2B_BATCH_PROJECTION=0/false/no/off` 恢复逐公司权威刷新；默认开启批量。
  - 同一 checkpoint revision 的 projection 同时用于 snapshot 判断、live 和 process 最终返回；最终结果复用 checkpoint，避免再次选择。
  - `B2B_BATCH_PROJECTION` 只改变投影时机，不改变交付/证据/ownership。
- `backend/app/live/projection.py` 原有 cache 复用路径保持不变；process 只在真实 checkpoint 后刷新页面。

### M3 ExportContext 与 metrics 复用

- `backend/app/runs/query.py`：一次 export 事务读取一次 cohort；`prepare_cohort`、export selection、`RunProjectionContext`、manifest、metrics 和返回视图共享 rows/suppressions/contactable。
- `backend/app/delivery/exporting.py`：`build_export` 接受已选 `selected`/`eligible_delivery`，不再二次调用 selection；未提供时保留旧公共适配路径。
- `backend/app/delivery/metrics.py`：接受 prepared rows、qualified cohort、batch channels、view channels，避免重复 coalesce/filter；batch/view/output 三种口径保留。
- metrics 不再调用 `delivery_companies`；export 的 delivery selection 由 `select_export_companies` 提供一次。

### M4 单企业与发现链路

- `backend/app/pipeline.py`：页面内容 digest 在页面首次进入 `enriched` 时计算一次，extraction key 组合 digest；reassess v2 只做一次 `assess_policy` 并把 assessment 传给 `qualify_v2`；v1 `qualify` 支持可选 assessment。
- `backend/app/rules/policy.py`：新增 `policy_cache_stats/reset_policy_cache_stats`，记录 hits/misses/fingerprint 次数与耗时；300 秒窗口保留。
- `backend/app/rules/ranking.py`：`score_company(copy=False)` 供已拥有 detach 行的内部路径使用；`rank_contacts` 与 `_primary` 避免重复 deepcopy。
- `backend/app/discovery/envelopes.py`：keystream 用 `generated += len(block)`，O(B) 而非 O(B²)；格式、nonce、MAC、base64 不变。
- `backend/app/discovery/adapters.py`：Eurostat 未确认多月 API 契约，保留串行并缓存每个 `(geo, partner, product, period)`；连续失败且无结果时分类为 `SOURCE_ERROR`，不再全部当“当月无数据”。
- `backend/app/discovery/service.py`、`cli.py`：`discover --list` 增加 `cursor/limit` 分页与 `has_more`。
- 未启用跨源并行；因此在补唯一约束、统一 deadline 和冲突测试前不引入 discovery schema 迁移。

## 2. 测试结果

- `.\.venv\Scripts\python.exe -m pytest backend/tests -q --disable-warnings`：**486 passed**（基线 476 + 新增 10），无 skip。
- 新增/扩展覆盖：HTTP 多地址回退、bench timeout 与 `failed_pages`/物理 attempts、claim 合批与 `B2B_BATCH_PROJECTION=0` 回退、export 单选、policy cache 统计、1 MiB envelope、discover 分页。
- 旧新 canonical diff：
  - `selection`：12 组场景（v1/v2、any/all、named、共享 email/phone/form、suppression、freshness、filters、channels）共 463,569 字节，旧新逐字节相等。
  - `export`：11 家/11 联系人/187 来源的 CSV 三表逐字节相等；`manifest.json` 去除 `generated_at`、`runtime_release`、`research_release`、`tool_version`、`timing`、`as_of` 等时间/版本身份字段后逐字节相等（74,595 字节）。
- 旧 release 读取兼容：基线 2.6.0 代码在同一 `skill_v2_003` 隔离库上完成 selection/export canonical 生成，未出现 schema 读取错误；本版本未升 schema。

## 3. 基准（同 fixture 旧新 A/B）

所有数字来自 `bench/2026-11-07-*.json`，隔离 `sca_bench_*` PostgreSQL、固定 cohort、固定 fixture 时间；p95 为至少 5 轮（标注除外）。

下表的 wall time 不启用 `tracemalloc`（启用会把 Python 分配追踪开销计入耗时）；峰值内存另用 `--trace-memory` 单列测量。

| 场景 | 旧 2.6.0 p95 | 新 2.7.0 p95 | 降幅 | selection 次数 |
|---|---:|---:|---:|---:|
| 600 家 append 20 家 | 41.642 s | 1.230 s | 97.0% | 4 → 1（`delivery_companies`） |
| 600 家 status | 11.409 s | 1.102 s | 90.3% | 2 → 1 |
| 200 家冷导出 xlsx | 24.717 s | 2.335 s | 90.6% | `select_export` 2→1；delivery 7→0 次额外调用 |
| 200 家单公司 complete | 7.265 s | 0.907 s | 87.5% | 4→2 |
| 600 家冷导出 xlsx | 未测 | 4.419 s | 未做旧新比 | 1 |
| 1000 家冷导出 xlsx | 未测 | 7.032 s | 未做旧新比 | 1 |

- 600 家 append 矩阵（不启用 tracemalloc）：0 家 0.051 s、200 家 0.432 s、600 家 1.230 s、1000 家 2.025 s；selection 始终 1 次。
- 20 jobs（600 家）新路径：p95 2.575 s，实测 2 次全量 selection（启动、最终检查点；claim 0）；旧路径 3 jobs 已实测 4 次，按逐公司 `_refresh(detail=True)` 外推 20 jobs 为 21 次；selection 次数降低约 90.5%，测试断言 claim 路径不运行全量 delivery。
- 单公司 complete：200 家旧 p95 7.265 s（3 轮，4 次 selection）→ 新 p95 0.907 s（3 轮，2 次 selection），降幅 87.5%。600 家旧版 1 job 基准因旧代码在完成刷新持有 Run 锁超过 `lock_timeout` 连续失败，未取得可信旧值；600 家新值为 traced p95 22.084 s（5 轮，2 次 selection），不据此单独宣称 600 家旧新降幅。
- 200 家冷导出 tracemalloc 峰值（`--trace-memory` 单独运行）：旧 33.1 MB → 新 30.9 MB（约 -6.7%），低于基线但**未达到 ≥20% 目标**。
- 1000 家导出：7.0 s 完成，未超时，未超过 100 MiB/10000 行限制；200→600→1000 为 2.3→4.4→7.0 s，5 倍 cohort 约 3.0 倍耗时，未呈二次增长。
- 1 MiB envelope seal/open：旧 36.241 s → 新 0.133 s（同机单次，约 -99.6%）；keystream 由 O(B²) 求和改为 O(B) 累加，测试验证格式/解密结果不变。
- 页面 digest：3 页样例仍为 3 次 extraction，页面内容只哈希一次；policy cache 统计测试验证 hit/miss/fingerprint 计时。

### 未达到/未完成

1. 200 家导出峰值内存目标 -20% 未达到（实际 -6.7%，仍低于基线）。
2. 单公司 complete 的 600 家旧版对照因旧代码锁超时未完成；200 家 A/B 已达到 87.5%，600 家新值已记录但旧值不伪造。
3. 旧版 600/1000 导出、旧版 20 jobs 全量基准未完成；旧版 3 jobs 的 4 次 selection 已实测，新值已记录，旧值不伪造。
4. discovery 唯一约束/跨源并行未实施；`discover --list` 分页已实施。

## 4. 已知风险与回滚

- `B2B_BATCH_PROJECTION=0`：恢复逐公司 `_refresh(detail=True)`；只影响投影时机，不改 Company/Export/交付规则。
- 回滚到 2.6.0 release：按 `scripts/skill_release.py --destination <安装目录> --release <已验证2.6.0 release>`；本版本没有 schema 变更，无需降级数据库。
- 未实施项：discovery `(run_id, source, source_ref)` 唯一约束、跨源并行、异步导出、共享目录自动采集。
- 风险：批量模式下运行中计数最多滞后约 1 秒；status 自己会做权威 `_refresh`，不会把旧计数冒充最新。live 最坏可见时间由 1 秒 checkpoint + 1 秒页面节流保证 ≤5 秒。
- export 的 manifest `generated_at`、`runtime_release`、`research_release`、`tool_version`、metrics `as_of/data_version` 随运行时间/版本变化；这些是白名单字段，CSV 与企业/联系人/来源内容应与旧版一致。

## 5. 未完成/未声明

- 未执行真实批量搜客、客户数据访问或公开发布；本地安装状态见使用者环境。
- 未证明 200 家真实在线交付 SLA；基准为固定 replay。
- discovery 跨源并行与唯一约束迁移未做。

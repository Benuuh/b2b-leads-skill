# Skill 性能与通用执行改造：实施记录

| 项目 | 内容 |
|---|---|
| 依据 | `docs/specs/SPEC-skill-performance-v1.md` |
| 实施日期 | 2026-10-27 |
| 范围 | M1–M4（合同与编排、证据与决策、并发与恢复、发布安装） |
| 未实施 | M5 在线试跑（需要明确的实时研究任务，本轮不自动恢复搜客） |
| 业务数据 | 未修改既有批次；离线验收使用临时数据库，结束后销毁 |

## 交付内容

### M1 合同与编排（FR-01、FR-02、FR-09、FR-11）

- 新增 `app/capabilities.py`：版本化能力注册表（字段、运算符、渠道类型、默认值、核心上限、失败分类）。`capabilities` 命令完全离线，无数据库写入、无 HTTP/DNS、无隐式维护。
- `app/schemas.py`：新增 `SegmentSpecV2`、`QualificationPolicy`、`Condition`、`ContactPolicy`、`EvidencePolicy`、`ExecutionPolicy`、`SupplementInput`；`parse_segment` 统一解析 v1/v2。无版本输入仍按 v1 解释且规范化结果与旧版逐字节一致，`create` 重试不会改变旧 run 的配置 hash。
- 新增 `app/runs.validate_input` 与 `validate-input` 命令：逐条返回 `index`、`admissible`、`reason_code`、`path`、`normalized_hash` 与可提交子集；不写库、不联网。
- `create` 冻结 `spec_version`、规则版本、证据版本、发布标识与执行政策。
- `_metrics_context` 新增 `metrics_version`（`b2b-research-metrics-1`）和 `research` 指标块；保留原 `discovered = 已准入数` 语义，新增 `discovery_seen`、`append_ready`、`qualified_by_origin`、`missing_items`、`failure_categories`、`snapshots`。未知值为 `null`。

### M2 证据与决策（FR-02 至 FR-06）

- 新增 `app/rules/policy.py`：主体绑定 + 原文依据 + 来源有效性的条件求值，输出 `pass`/`fail`/`unknown`/`conflict`；必要条件 fail 才排除，未知不写成负事实；排除条件的 `unknown_policy` 与冲突复核规则按 SPEC §5.2 执行。
- 新增 `app/rules/identity.py`：法律后缀规范化、已证实别名归并、总部/分支分离、同域不同法人不误并；v1 批次继续使用原身份算法。
- `pipeline.py` 新增 v2 处理路径：已保存证据充分时零网络完成（`saved_evidence_sufficient`），只为缺项抓取；`contact_target_reached` 按渠道政策判断最小充分停止。
- `delivery/selection.py` 新增 `channel_status`：`mode=any/all`、允许渠道类型、具名联系人与岗位要求；交付选择、完成计数、快照与导出共用同一判定。
- `runs.supplement` 与 `supplement` 命令：`expected_revision`、证据增量 hash、幂等键；保留旧证据与采集时间、离线重算、不新增候选、不改身份字段、不恢复派发；补证转合格计入 `qualified_by_origin.supplemented_evidence`。

### M3 并发与恢复（FR-07、FR-08）

- `process` 改为单命令协调的有界 worker（v2 默认 2，最大 4；v1 保持串行并拒绝 `--workers>1`）。每个 worker 独立租约与心跳，事务领取、联网外置、短事务保存。
- 派发前原子预留页面预算（`reserved_pages`），并按预留收敛该任务的每企业/批次页面上限；预算耗尽时停止领取、关闭输入、报告真实缺口。
- 命令截止时间传递到 `web/http.py`（连接/读取超时、限流等待、robots 请求），在途任务按有界规则完成；不宣称底层已取消。
- 失败分类（`app/failures.py`）写入任务错误、状态与指标；连续工具/网络故障打开熔断（`circuit_open`），`status --action retry` 显式关闭。
- 增量快照：`snapshot_every_new_qualified` 或 `snapshot_interval_seconds` 满足且投影有变化时，在协调器安全检查点导出；与最终交付共用选择规则；无变化不重复导出；演示批次跳过。

### M4 发布与安装（FR-11）

- 更新 `skills/b2b-leads/SKILL.md`、`references/controller.md`、`agents/openai.yaml`，新增 `references/segment-spec.md`、`references/evidence-contract.md`。
- 核心版本升至 `2.1.0`；冻结为 release `<release-2.1.0-id>`（59 个文件，含 `VERSION`），通过安装入口写入 `<skill-installation>`。
- 版本标记：根目录 `VERSION`、`backend/app/__init__.py` 的 `__version__`、`backend/pyproject.toml`、`SKILL.md` 头部与正文、`capabilities.skill_version`/`core_version` 与导出清单的 `runtime_release` 使用同一版本 `2.1.0`，`backend/tests/test_version.py` 防止漂移。
- 无 schema 迁移：v2 配置与决策保存在既有 JSONB 列（`spec_json`、`plan_json`、`data_json`），schema 仍为 `skill_v2_002`，旧 run 可读且不自动升级。
- 安装后从真实入口验证：`capabilities`、`validate-input`、`list`、`prepare` 均通过；篡改已安装副本的 `SKILL.md` 后入口返回 `RELEASE_INTEGRITY_ERROR`。
- 回滚点保留：上一发布 `<release-2.0.1-id>`（核心 2.0.1）。

## 验收对照

| 编号 | 覆盖方式 |
|---|---|
| AC-01 跨行业配置 | `test_spec_v2.py`：两份非货代配置走同一入口；未注册字段/运算符、冲突国家与重复口径被拒绝；能力表可查 |
| AC-02 证据与未知 | `test_evidence_policy.py`：逐条可追溯；客户新闻/仅声明不通过；未知与来源故障不判为失败 |
| AC-03 逐条预检 | `test_precheck.py`：4 条有效 + 1 条草稿 → 4 条可提交 + 1 条错误；有效子集原子提交；重放不重复；预检不写库不联网 |
| AC-04 证据优先 | `test_spec_v2.py`、`test_evidence_policy.py`、CLI 验收：充分证据零 HTTP/DNS；缺渠道只产生渠道缺项 |
| AC-05 最小充分停止 | `test_evidence_policy.py`：any 一个表单即完成；all/具名岗位继续缺项；无要求不追加社媒 |
| AC-06 身份与补证 | `test_identity_supplement.py`：同域两法人保留、后缀归并、已证实别名归并、补证改判、版本冲突、重放不新增公司 |
| AC-07 并发与预算 | `test_concurrency.py`：两 worker 并发、页预算预留不超发、暂停撤销在途租约、截止后停止领取 |
| AC-08 恢复与发布 | 既有恢复/重放测试 + 本轮安装入口校验与篡改检测（见下） |
| AC-09 快照与计数 | `test_snapshot_metrics.py`：阈值快照、行数与交付投影一致、无变化不重复、缺口按原目标 |
| AC-10 性能报告 | 本轮只报告离线结果与单位，未宣称固定倍数提速；在线指标待 M5 |
| AC-11 排除未知政策 | `test_evidence_policy.py`：默认待核；`allow_unconfirmed` 可交付并披露未知；冲突始终复核 |

## 测试与离线验收

- 自动测试：`359 passed`（改造前基线 317 passed，无跳过；新增 42 项覆盖 v2 合同、预检、证据优先、身份补证、并发预算、快照指标与 v1 兼容）。
- 真实入口离线验收（临时数据库，结束即销毁）：
  - `capabilities` → `spec_versions [1,2]`；
  - `validate-input` 3 条全部可提交；
  - `process` 处理 3 家、2 家可交付、1 个增量快照，`physical_http_attempts = 0`；
  - `supplement` 将待核企业改判为合格（revision 1→2，`network_requests = 0`）；
  - `export` 3 行，`metrics_version = b2b-research-metrics-1`，`spec_version = 2`；
  - 相同 chunk 重放结果完全一致。
- v1 兼容：`test_v1_compat.py` + 原有 317 项测试证明旧输入、旧 run、旧导出与 8 小时/600 候选预算语义未变。
- 升级前后等价性（AC-08）：把同一组固定 v1 样本分别交给上一发布（`ec1442…`，核心 2.0.1）与新核心（`a6f192…`，核心 2.1.0）计算，`assess_candidate` 的决策、置信度、理由与独立来源数完全一致；`select_export_companies` 的导出企业集合（`c1`、`c2`）、可交付计数（2）与每家企业渠道（`email`、`contact_form`）完全一致。脚本见 `.runtime/decision_compare.py`、`.runtime/projection_compare.py`。

## 性能结论

离线样本证明的是“重复计算与不必要请求减少”：已保存证据充分的样本零 HTTP/DNS 完成，缺项只补对应来源，并发不超发预算，评估结果按“实体 + 全部保留页面 + 冻结政策 + 规则版本 + 5 分钟窗口”的指纹在进程内有界复用（最多 128 项），不会跨实体或跨规则版本共享。`queue_wait_seconds`、阶段处理秒数与限流等待秒数为实测值；解析、数据库与导出耗时未单独埋点，保持未知而不事后推断。

固定离线样本的请求数对照（同一份已充分取证的证据、同一合格结论，`.runtime/requests_bench.py`）：旧核心 2.0.1 走 v1 路径仍抓取官网首页与联系页共 **2 次**物理页面请求；新核心 2.1.0 走 v2 证据优先路径为 **0 次**，两者都判定合格。该数字是“请求数”，不是耗时；没有实时网络延迟样本就不能换算成秒或百分比。

离线样本不能推出在线倍数提速，因此本轮不报告提速百分比。M5 需要具体实时任务，在 30–50 个新候选上记录首批延迟、每家请求数、P50/P95、缓存复用率与合格产出率，再决定 2→4 worker 与后续 P2 优化。

## 已知边界

- 发现池仍由 Agent 在检查点/忽略工件中维护；核心只接收通过预检的候选，`discovery_seen` 统计提交数，`append_ready` 统计可准入数，未新增独立发现表。
- `supplement` 为离线重算，不发起联网；如需继续抓取页面，应先 `append` 新观测或另行 `process`。
- `native_inflight` 作为冻结的执行政策被记录和校验，但当前抓取器本身串行访问同一企业页面，因此它是上限声明而非额外并行度。
- 快照由命令内的协调器触发；协调器停止或底层工具长时间不返回时不保证定时触发，`snapshot_checked_at`/`last_snapshot.error` 会记录原因。
- v2 条件只覆盖注册表内的字段与运算符；未注册的行业资质需要先实现校验器，不能只写在 Skill 检查点里。

## 回滚

1. 记录当前 release id（`status`/`prepare` 输出中的 `release.release_id`）。
2. 需要回退时执行 `scripts/skill_release.py --destination <安装目录> --release <已验证的旧 release_id>`，安装器会重新校验旧 release 的文件清单后覆盖 Skill 文件。
3. 旧 run 的配置、hash、预算与导出快照不受影响；不要用旧核心写入 v2 配置。

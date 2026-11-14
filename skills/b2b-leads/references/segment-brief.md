# 客群确认（segment brief）

新建批次在 `create` 之前必须走这一步。需求已经完整时也要走。产品版本号仍是 2.7.0；这里描述的是当前核心行为，不表示 2.8.0 已发布，也不表示共享目录预算、目录自动采集、新国家规则、JS 渲染或东南亚数据源已经可用。

## 命令

```powershell
.\.venv\Scripts\python.exe scripts/b2b_leads.py capabilities
.\.venv\Scripts\python.exe scripts/b2b_leads.py brief --spec-json .runtime/b2b-leads/spec.json
.\.venv\Scripts\python.exe scripts/b2b_leads.py create --run RUN --spec-json .runtime/b2b-leads/spec.json --confirm-hash <confirm_hash>
```

`brief` 只读输入文件和进程内的能力/规则，完全离线：不连接数据库，不执行 `prepare` 或维护，不发起 HTTP/DNS，不创建 run，不启动计时。成功时返回规范化 spec、`spec_hash`、`confirmation_summary`、`confirm_hash`、`defaults_applied`、`validation` 和 `pending_hints`。校验失败使用原有错误契约（字段路径、错误码、限额），不给出可用于创建的确认哈希。

`capabilities.segment_brief_version` 为 `segment-brief-2`。`default_spec_version` 为 2。确认范围包含 `page_budget_contract`：新 v2 批次冻结 `shared-page-budget-1`，显式 v1 不冻结。旧的 `segment-brief-1` 哈希不能继续创建。

## 确认状态

1. 展示当前这一版 `confirmation_summary` 的全部中文内容。不要增删条件，不要把建议写成未配置的硬条件，不要改写 v1/v2 的生效语义。
2. 展示之后，只有「确认」「就按这份建议执行」或「直接跑」才算确认这一版。沉默、含糊答复、只提问，都不算。
3. 展示之前说的「别问了直接跑」只是事先授权，不能确认还没看到的口径。仍要展示并等待下一次明确答复。
4. 同一次答复里如果改了任何确认范围内的值，先按修改处理：重新 `brief`，展示新的完整摘要，再等待。允许多次修改。每次确认只对应该次 `confirm_hash`。
5. 用户中断或只提问时不建 run、不计时。上下文无法恢复出完整确认范围时，重新生成并展示，不要推断已经确认。

`defaults_applied` 用来标注来源：用户明确给出的值标「用户原话」，核心填入的标「默认值」，Skill 自己补的标「推断」。无法判断就标「来源待核」。这份映射只留在当前会话，不写入业务库，也不是审计证据。

## 哈希局限

`spec_hash` 仍是既有规范化 spec 哈希。`confirm_hash` 覆盖 `brief_version`、`spec_hash`、创建时生效的规则版本，以及初始时限、候选倍数、页面上限等固定计划值。它不包含 run ID、来源标签、文案、当前时间或全局抑制名单。

核心只检查传入哈希是否等于当前配置的 `confirm_hash`。哈希可以由任何调用方算出来，不能证明确认人、原话或确认时间。直接调用核心可以绕过对话。`plan.segment_brief` 只表示调用方提交了匹配哈希，不是身份审计。

无语义的 JSON 格式或键顺序变化不需要重新确认。确认范围内任一有效值变化，包括规则版本或固定计划默认值变化，旧哈希失效，必须重新展示。

缺失或不匹配时，CLI 和 `runs.create` 都在打开数据库之前返回 `SEGMENT_NOT_CONFIRMED`（409），并在输入有效时给出当前 `confirm_hash`。不返回无法从哈希反推的差异字段。

## 记录与旧批次

成功创建后，`plan.segment_brief` 保存 `{brief_version, spec_hash, confirm_hash, confirmation_recorded_at}`。时间是创建命令写入的时间，不是用户确认时间。`plan.initial_limits` 保存确认过的初始时限和页面/候选上限；随后的 `extend-research` 只改当前时限，不改这条确认记录。`status` 同时给出 `time_limits.initial_seconds` 和 `time_limits.current_seconds`。

同一 `run_id`、同一规范和同一确认范围的重试返回原 run，不覆盖首次记录，也不重启计时。不同 spec，或同一 spec 但已保存的确认范围不同，返回 `IDEMPOTENCY_CONFLICT`，并提示使用 `status`。

没有 `segment_brief` 的旧 run：读取、续跑、补证、重评、追加、延长、导出和 live 页面都不需要补做新建确认。`status.segment_brief` 显示「历史批次，无确认记录」。对新版本重放 `create` 时仍要提供与所提交 spec 匹配的哈希；只有 spec 相同且初始固定计划仍能核对一致时才返回原 run，并且不会补写一条历史确认记录。否则冲突并提示 `status`。

## 全局抑制名单与客户文件

动态全局抑制名单不在本次哈希里。摘要写明「另适用当前全局抑制名单，运行中可能变化」。

现有客户文件不是独立排除通道。只有写入 `spec.exclusions` 的标识才随配置生效。摘要只显示条数和列表哈希，不显示具体企业、联系人、邮箱或电话。未写入的文件不得被说成已经排除。

## 取证限制

确认摘要和 `capabilities.discovery_limitations` 必须同时说明，且都还是当前事实：

- 无自动 JS 渲染（`automatic_js_rendering=unavailable`）
- 共享目录页面预算合同 `shared-page-budget-1` 只对冻结了它的新 v2 批次记账。预算可用不等于目录自动采集可用（`automatic_shared_directory_collection=unavailable`），也不等于自动 JS 渲染可用。没有该合同的批次拒绝自动共享目录采集
- 国家证据仍是旧版字面匹配（`country_evidence=legacy_literal_matching`）
- 结构化发现源未覆盖东南亚（`structured_discovery_southeast_asia=not_covered`）；三源不可用时走 `legacy-search`

目标数量、候选上限和初始 8 小时只是冻结的预算。确认步骤不承诺找满、做完或校准指标已经达标。搜索不可用、域名阻断或 JS 空正文不是「市场上没有公司」。

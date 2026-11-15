# 客群确认（segment brief）

新建批次在 `create` 之前必须走这一步。需求已经完整时也要走。当前版本标记是内部试用 2.9.0，不是已完成生产验收，也不是正式发布的 2.9.0。RT-01 真实小批 canary、200 家 / 8 小时和独立校准均未验证。目录自动采集、新国家规则、JS 渲染和东南亚结构化发现源仍然不可用。

## 命令

```powershell
.\.venv\Scripts\python.exe scripts/b2b_leads.py capabilities
.\.venv\Scripts\python.exe scripts/b2b_leads.py brief --spec-json .runtime/b2b-leads/spec.json
.\.venv\Scripts\python.exe scripts/b2b_leads.py brief --spec-json .runtime/b2b-leads/spec.json --method directory_batch --out-md .runtime/b2b-leads/brief.md
.\.venv\Scripts\python.exe scripts/b2b_leads.py create --run RUN --spec-json .runtime/b2b-leads/spec.json --confirm-hash <confirm_hash>
```

`brief` 只读输入文件和进程内的能力/规则，完全离线：不连接数据库，不执行 `prepare` 或维护，不发起 HTTP/DNS，不创建 run，不启动计时。成功时返回规范化 spec、`spec_hash`、`confirmation_summary`（含 `headline`、`fields`、`sections`）、`confirm_hash`、`defaults_applied`、`validation` 和 `pending_hints`。`--method` 只换估算方法，`--out-md` 只在同一份摘要外再写一个 Markdown 文件，两者都不改确认范围。校验失败使用原有错误契约（字段路径、错误码、限额），不给出可用于创建的确认哈希。

`capabilities.segment_brief_version` 为 `segment-brief-3`。`default_spec_version` 为 2。确认范围包含 `page_budget_contract`：新 v2 批次冻结 `shared-page-budget-1`，显式 v1 不冻结。旧版本（含 `segment-brief-1`/`segment-brief-2`）的确认哈希不能继续创建。

## 确认状态

1. 展示 `confirmation_summary.headline` 的全部内容：`目标`、`硬条件`（`must_have` 逐条原样照抄）、未知值与 `exclusions`、`数量`、`预算`、`限制`、`confirm_prompt`。不要增删条件，不要把建议写成未配置的硬条件，不要改写 v1/v2 的生效语义。随后用一行给出完整附录路径，用一行标注推断项来源（没有推断就不写）。
2. 展示之后，只有「确认」「就按这份建议执行」或「直接跑」才算确认这一版。沉默、含糊答复、只提问，都不算。
3. 展示之前说的「别问了直接跑」只是事先授权，不能确认还没看到的口径。仍要展示并等待下一次明确答复。
4. 同一次答复里如果改了任何确认范围内的值，先按修改处理：重新 `brief`，展示新的 headline 与附录入口，再等待。允许多次修改。每次确认只对应该次 `confirm_hash`。
5. 用户中断或只提问时不建 run、不计时。上下文无法恢复出完整确认范围时，重新 `brief` 并展示新的 headline，不要推断已经确认。

`defaults_applied` 用来标注来源：用户明确给出的值标「用户原话」，核心填入的标「默认值」，Skill 自己补的标「推断」。无法判断就标「来源待核」。这份映射只留在当前会话，不写入业务库，也不是审计证据。

## 分层展示（segment-brief-3）

`confirmation_summary` 分两层：`headline`（首屏决策卡）和 `fields`/`sections`（完整生效值与哈希附录）。默认展示 headline，完整值随时可查；这不降低完整性要求，只改变展示顺序。

### headline

对象键：`one_liner`、`must_have`、`unknown`、`exclusions`、`quantity`、`channels`、`budget`、`estimate`、`limits`、`confirm_prompt`；v1 另有 `field_sources`，任何版本都有 `card`（硬长度守卫结果）。

- `must_have` 与 `required_all` 一一对应、顺序一致、条数相等；每条一行，Skill 原样照抄，不改写、不合并。
- headline 不出现机器字段名、版本令牌、`shared-page-budget-1`、`evidence-2` 或 64 位哈希。
- 默认项（如「不要求具名联系人」）不单独占行；非默认值必须出现。
- `estimate` 按所选方法（`per_company` 或 `directory_batch`）给出，行首标注方法；Skill 必须同时说明「粗估，未校准，不是实测或承诺」。
- 首屏硬上限 13 行 / 450 字（含 Skill 追加的附录行与来源行）。核心在 `headline.card` 返回拼好的 `lines`、行数、字数与 `within_budget`；超出时先折叠「限制」行（全文仍在 `sections`），仍超出则 `within_budget: false` + `overflow_note`，硬条件不删减。
- v1 批次按历史字段生成 headline，不引入 v2 条件；`field_sources` 逐项标出该行来自「用户填写」还是「默认值」。

### 预计耗时与 token

`confirmation_summary.projection` 保存估算方法、依据和区间：

```json
{
  "version": "projection-2",
  "method": "per_company",
  "method_label": "逐家官网取证",
  "basis": "heuristic_uncalibrated",
  "companies": 200,
  "candidates": 600,
  "tokens": {"low": 2400000, "high": 4000000},
  "time_hours": {"low": 8.3, "high": 13.3},
  "time_minutes": {"low": 498, "high": 798},
  "note": "200 家 × 2.5–4 分钟 ≈ 8.3–13.3 小时；每家 1.2–2 万 token ≈ 240–400 万。未校准。"
}
```

`brief --method per_company|directory_batch` 选择系数，默认 `per_company`：逐家官网取证按每家 2.5–4 分钟、1.2–2 万 token；`directory_batch` 只在明确要求时用，按 10–30 分钟、60–160 万 token（以 2026-11-15 实测墙钟为锚点，token 未实测）。方法只影响展示的估算，不进入 `confirm_hash`，所以确认后换方法不需要重新确认。系数在 `capabilities.projection.profiles`，所有任务共用。估算只用于预算沟通，不进入成本基线、验收指标或 `confirm_hash`；报告实际成本时必须另行记录可审计的 token 来源与范围，不能用估算替代实测。

### 附录

Skill 把完整的 `confirmation_summary`（`fields`、`sections`、`spec_hash`、`confirm_hash`）写到忽略目录 `.runtime/b2b-leads/brief-<spec_hash[:8]>.json`，首屏后用一行给出路径，例如：

```text
完整口径与哈希：.runtime/b2b-leads/brief-7472a703.json
```

用户要求逐条核对时，逐字展示 `sections`，不得凭记忆复述。附录不是独立权威：交付与确认仍以核心返回的当前 `confirm_hash` 为准。

## 哈希局限

`spec_hash` 仍是既有规范化 spec 哈希。`confirm_hash` 覆盖 `brief_version`、`spec_hash`、创建时生效的规则版本，以及初始时限、候选倍数、页面上限等固定计划值。它不包含 run ID、来源标签、文案、当前时间或全局抑制名单。

核心只检查传入哈希是否等于当前配置的 `confirm_hash`。哈希可以由任何调用方算出来，不能证明确认人、原话或确认时间。直接调用核心可以绕过对话。`plan.segment_brief` 只表示调用方提交了匹配哈希，不是身份审计。

无语义的 JSON 格式或键顺序变化不需要重新确认。确认范围内任一有效值变化，包括规则版本或固定计划默认值变化，旧哈希失效，必须重新展示。

缺失或不匹配时，CLI 和 `runs.create` 都在打开数据库之前返回 `SEGMENT_NOT_CONFIRMED`（409），并在输入有效时给出当前 `confirm_hash`。不返回无法从哈希反推的差异字段。

## 记录与旧批次

成功创建后，`plan.segment_brief` 保存 `{brief_version, spec_hash, confirm_hash, confirmation_recorded_at}`。时间是创建命令写入的时间，不是用户确认时间。`plan.initial_limits` 保存确认过的初始时限和页面/候选上限；随后的 `extend-research` 只改当前时限，不改这条确认记录。`status` 同时给出 `time_limits.initial_seconds` 和 `time_limits.current_seconds`。spec 里写了 `time_limit_hours` 时，`initial_limits.time_limit_seconds` 就是该小时数（如 12 → 43200）；不写仍是 8 小时。

旧确认范围的 run：`status.segment_brief.note` 会写「由 segment-brief-2 生成；当前确认范围是 segment-brief-3，旧确认记录保留不改写」。旧记录不补写、不改写，续跑与导出照旧。

同一 `run_id`、同一规范和同一确认范围的重试返回原 run，不覆盖首次记录，也不重启计时。不同 spec，或同一 spec 但已保存的确认范围不同，返回 `IDEMPOTENCY_CONFLICT`，并提示使用 `status`。

没有 `segment_brief` 的旧 run：读取、续跑、补证、重评、追加、延长、导出和 live 页面都不需要补做新建确认。`status.segment_brief` 显示「历史批次，无确认记录」。对新版本重放 `create` 时仍要提供与所提交 spec 匹配的哈希；只有 spec 相同且初始固定计划仍能核对一致时才返回原 run，并且不会补写一条历史确认记录。否则冲突并提示 `status`。

## 全局抑制名单与客户文件

动态全局抑制名单不在本次哈希里。摘要写明「另适用当前全局抑制名单，运行中可能变化」。

现有客户文件不是独立排除通道。只有写入 `spec.exclusions` 的标识才随配置生效。摘要只显示条数和列表哈希，不显示具体企业、联系人、邮箱或电话。未写入的文件不得被说成已经排除。

## 取证限制

确认摘要和 `capabilities.discovery_limitations` 必须同时说明，且都还是当前事实：

- 下一批默认 `per_company`：目录和搜索结果只作发现线索，资格（国家、自身产品/服务）与联系渠道来自企业官网 About/Contact/产品页；没有可用官网的企业降级为待核或丢弃，不用目录行补资格、邮箱或电话
- 无自动 JS 渲染（`automatic_js_rendering=unavailable`）
- 共享目录页面预算合同 `shared-page-budget-1` 只对冻结了它的新 v2 批次记账。预算可用不等于目录自动采集可用（`automatic_shared_directory_collection=unavailable`），也不等于自动 JS 渲染可用。没有该合同的批次拒绝自动共享目录采集
- 国家证据仍是旧版字面匹配（`country_evidence=legacy_literal_matching`）
- 结构化发现源未覆盖东南亚（`structured_discovery_southeast_asia=not_covered`）；三源不可用时走 `legacy-search`

目标数量、候选上限和初始 8 小时只是冻结的预算。确认步骤不承诺找满、做完或校准指标已经达标。搜索不可用、域名阻断或 JS 空正文不是「市场上没有公司」。

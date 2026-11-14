---
name: b2b-leads
metadata:
  version: "2.7.0"
description: "Skill 2.7.0 · Research B2B target companies and their public business contact channels for any industry or country, continue saved acquisition batches, and deliver sourced Chinese Excel lead lists through the local Python acquisition core. Use for requests such as finding manufacturers, distributors, integrators or service providers, sourcing prospective business customers, or continuing a saved lead research run."
---

# B2B customer research

> 版本标记：Skill 2.7.0 · 核心 2.7.0 · 规则 b2b-policy-2。以 `capabilities` 返回的 `skill_version`/`core_version`/`rule_version` 和发布清单为准；`VERSION` 文件记录同一版本。

## 边界（每次启动先读）

Skill 只能引用已注册的 Tool；不得扩大数据范围、降低证据要求、绕过确认策略。每次启动先跑 `capabilities` 确认 tool 列表与密钥状态（`secrets` 字段），再执行任何动作。

| 不许做 | 具体表现 | 违反时 |
| --- | --- | --- |
| 扩大数据范围 | 读不属于当前 run 的公司、别的 workspace 的名单 | 拒绝并报告 |
| 降低证据要求 | 没证据就 `advance`；拿搜索摘要当事实 | 拒绝，要求补证据 |
| 绕过确认 | 未经确认就 `export` / `suppressions` / `delete-company` | 拒绝，先请求用户确认 |
| 自行扩表 | 往 spec 塞未注册字段或运算符 | core 报错；不得改 core 绕过 |
| 隐式重试 | 网络失败后换参数重试，同一条算两次 | 相同 chunk 重放；超出记 `unknown_outcome` |

被拒之后 run 怎么走：单次被拒 → 动作不执行、状态不变、继续；连续 3 次被拒 → 自动 `pause`，reason=`repeated_denied`。

并发与页数不算扩大数据范围：同一 run 内多开连接、多抓页面是"同一范围内的效率"；跨 run、跨 workspace 才算扩大。

## 结构化数据源（2.6，可选）

`discover` 把公开数据源的查询条件翻译成**未核实候选池**。硬约束（§3.1.3）：未核实候选不是候选公司——不进合格计数、不进去重池；只有带证据 `advance` 之后才算。`discover --list` 的数字永远不能加进 discovered/qualified。

```bash
control.py discover --run RUN --source eu_ted --limit 200   # 或 eurostat_comext | google_places | all
control.py discover --run RUN --list [--source X]
control.py discover --run RUN --reject <id> --reason "..."   # reason 必填，空串报错
control.py discover --run RUN --explain <id> --yes           # 解密原始记录信封，需人确认
control.py sources                                           # 各源可用性与凭据状态
```

- 先看 `capabilities` 的 `secrets`：信封密钥生产缺失会**拒绝启动**，开发降级明文并打 `dev-insecure`；游标密钥配了就必须 ≥32 字节。
- 单源失败不阻塞其他源（`source_error`/`source_empty`/`source_limit`/`source_parse_error`）；**三源全挂**时 run 置 `partial_no_source`，保留已有候选、不再空转。
- 覆盖不到东南亚（遗留）。三源都不可用时用 `discover --run RUN --legacy-search` 回到人工发现流程，run 标 `mode: legacy`，产出结构一致但没有 `source_ref`。

## 名单之后：开发信（下游 skill）

本 Skill 只产出名单与证据，不发信。需要写信时交给独立的 `outreach-letter` skill：输入 `company_id` + 证据 + 联系人角色 + 阶段，输出 `letters/<company_id>.md` 交人工发送。证据为空不生成信；每封信必须能指回一条证据。

Turn the user's customer requirements into a sourced company list. Reuse this project's durable Python core for qualification, channel checks, deduplication, suppression, scoring, progress and export. The Skill is the only entrypoint; commands call the core directly without an HTTP API or login. Do not automatically send outreach, write CRM records, buy sources or ask for a new API key.

## Start or continue

Read [the controller reference](references/controller.md) for commands. Read [the segment configuration](references/segment-spec.md) before writing a new customer spec, [the confirmation flow](references/segment-brief.md) before `brief` or `create`, and [the evidence contract](references/evidence-contract.md) before importing candidates. Use the checkout's `.venv/Scripts/python.exe` to execute this Skill's `scripts/control.py`. An installed `project.json` selects a verified, frozen Skill/core release while keeping database configuration and business artifacts in the original project. If release verification fails, reinstall or select a verified previous release; do not bypass the failure by calling development code. The controller prepares only PostgreSQL and the isolated Skill schema. Never expose database credentials.

- Extract the customer definition: industry, countries, product/service, customer type, exclusions, quantity and any channel requirement. Ask one grouped clarification only when the target country or the customer definition is missing.
- New research freezes as `spec_version: 2` even when the field is omitted. `capabilities.default_spec_version` is 2. Explicit `spec_version: 1` keeps v1 semantics and the historical stored shape. Saved runs keep the version already frozen on them; do not re-decide a v1 run with v2 conditions. For speed-first v2 tasks, explicitly set `execution_policy.company_workers: 4`, `snapshot_every_new_qualified: 5`, and `snapshot_interval_seconds: 300`. Keep the ordinary zero-paid-source defaults: quantity 200, candidate budget 600, cumulative nonpaused run time 8 hours, Chinese Excel delivery. Those are budget caps, not a promise that the run will finish that quantity or that clock time. Read the saved run's limits instead of restating another denominator.
- Run `capabilities` before inventing conditions. Only registered fields and operators are accepted; unsupported conditions fail with a path instead of being silently dropped. Do not hardcode one country's headquarters, phone format or business model for another task. Read `discovery_limitations` and repeat them in the confirmation: no automatic JS rendering, no shared-directory page budget, country evidence is still legacy literal matching, and structured discovery sources do not cover Southeast Asia.
- Before every new `create`, including when the requirements already look complete, run offline `brief --spec-json` and show the returned `confirmation_summary` in full Chinese. Do not add, drop, or reverse a condition, and do not turn a suggestion into a hard requirement. Then wait. Only 「确认」, 「就按这份建议执行」, or 「直接跑」 **after that summary was shown** confirms that version. Silence, a vague reply, a question, or 「直接跑」 said before the summary do not confirm it and must not create a run. If the same reply also changes any confirmed value, treat it as a revision: run `brief` again, show the new summary, and wait. Each confirmation applies only to that summary's `confirm_hash`.
- Pass that `confirm_hash` to `create --confirm-hash`. A missing or different hash returns `SEGMENT_NOT_CONFIRMED` and does not create a run. The hash only checks that the submitted config matches the summary. It is not an identity audit and does not prove who confirmed, what they wrote, or when. A direct core call can compute the hash; do not tell the user they confirmed unless you showed the summary and received an explicit reply afterward.
- If the user requests exclusion of existing customers, locate the supplied file or ask for its location together with any other missing requirements. Put observed company/domain/email/phone identifiers into the spec's `exclusions` and record the file hash and count in the checkpoint without echoing customer details. A customer file is not an independent exclusion mechanism: identifiers that were not written into `exclusions` are not excluded, and the summary shows only the count and list hash. The backend also applies its current global suppression list, which can change during the run and is not frozen by the confirmation hash.
- Create a new descriptive `--run` once per intentional new request and record it. Continue an existing request with exactly the same run; after an interrupted `create`, retry its original spec, run, and confirm hash. Do not infer continuation from two similar goals. Resume, supplement, reassess, append, advance, finish, export, live, and extend-research of an existing run do not need another confirmation. `extend-research` does not rewrite the original confirmation record. A run whose plan has no `segment_brief` is reported as 「历史批次，无确认记录」.
- `create` freezes the normalized configuration, configuration hash, confirmation record, rule version and release, and starts the research clock. Inspect `status` before resuming saved work; never resume unrelated batches. The product version remains 2.7.0 until a later release; this entry flow does not mean later slices (shared-directory budget, directory auto-collection, new country rules, JS rendering, Southeast Asia sources) are available.

## Research, precheck and admit

Use the current Agent's available native internet search and page-open tools. Start with information-rich directories and association member pages that can support multiple companies. Batch independent queries and page opens up to the available tool limits; use a tool's multi-query/multi-URL input when supported. Do not research one company from start to finish before discovering the next. Search snippets are discovery hints, not factual evidence. If tools are unavailable, save a checkpoint and pause; explain the missing capability without inventing results or falling back to a paid provider.

After the target is confirmed, look for relevant national and local (state, provincial, city or port) associations as well as other public directories and registries. When an accessible local directory exists, attempt at least one relevant to the target region; otherwise record why it was not found or could not be accessed. Keep a source ledger in the checkpoint or a referenced run artifact: name, URL, evidence of directory identity and relevance, check time, access/public status, expected fields, pages viewed, discovered entries and reason for unavailability. Distinguish untried, inaccessible and viewed with no relevant entries. Never treat membership or category alone as proof of qualification; bind each observed field to its own entry and retain its original excerpt. Confirm website ownership independently.

The current core does not reserve or charge externally opened shared directory pages against the run's page budget. Tool counters are not a substitute. Do not run an automatic shared-page collection path while that accounting is unavailable; record manual discovery requests and pages separately and disclose that the unified limit is unverified. Automatic JS rendering is unavailable; do not bypass access controls, robots, CAPTCHA or rate limits. When a source or search is unavailable, record the exact tool/path and reason, try an allowed alternative, and pause with a checkpoint if no usable source remains. A failure on one network path does not prove universal inaccessibility.

For each candidate establish identity and the requested qualification facts: headquarters or operating country, business model, own products/services, customer industries. Once necessary facts and a confirmed official website are ready, hand the candidate to `advance` even if its channel is missing: the core can fetch the missing channels concurrently while the Agent discovers more companies. Do not manually open Contact/Team pages first when the core can do that bounded work. Include channels already observed on a directory or qualification page; use native research for channels still missing after processing or when no usable official site exists. Facts must be bound to the candidate itself: an excerpt must name the company as its subject. Customer stories, partner capabilities, group pages, search summaries, assertions and confidence scores never prove the candidate's own offering. Confirm an official website belongs to this company before attaching it; a matching name alone is insufficient. A shared domain may hold distinct legal entities: keep entity evidence and do not merge on domain alone.

Record every fact and channel as its own observation with source URL, collection time, method and a short excerpt containing the actual value. Never infer an email. A contact form counts only after observing email/message inputs; a Contact link is not a form. Prefer official public business channels and keep directory origin visible.

Submit the first 5 evidence-ready companies with `advance --run ... --chunk ... --input-json ...`; then use chunks of 10–20. `advance` prechecks the complete chunk, appends atomically, and processes bounded work in one controller startup. While that bounded command is running, continue independent discovery when the environment permits. Use the returned progress instead of an extra unchanged `status` or `export` each round. A rejected draft never partially commits: use offline `validate-input` to isolate the admissible subset and resubmit it under a new chunk. When using separate `append`, run `validate-input` first. Never strip an unsupported condition to make it pass; replay the original input and chunk after an interruption.

## Evidence first, minimal sufficiency

The core reuses saved evidence before any request. A company whose necessary conditions and channel policy already have valid, sourced evidence finishes with zero network requests. Fetch only the missing items; do not re-crawl an official site when nothing is missing.

Stop at the delivery policy: with `mode: any` one qualifying observed channel (email, phone or form) is enough; `mode: all` and named-person policies require every listed item. Extract all needed observations from an opened page in one pass. Once evidence is sufficient, advance that company immediately; keep uncertain companies in a separate missing-evidence queue. Do not collect social profiles, employee counts or extra contacts that the task did not request. When a fact stays unknown, report it as unknown with its field name instead of downgrading the rule.

Use `supplement --company-id ... --operation-key ...` to add incremental evidence for a retained company. It preserves old evidence and times, recomputes only affected decisions, creates no new candidate and performs no HTTP/DNS. Never rename a company to force a new candidate.

## Pause, concurrency, finish and deliver

`status --action pause` preserves a run across breaks and stops the cumulative clock; resume only this requested run with `process --resume`. Save the checkpoint before pausing when possible. Committed operations record their input hashes; retry the original input, run and chunk/operation key after an interruption rather than creating a new batch.

`process` runs a bounded number of jobs; a v2 run uses its frozen `execution_policy.company_workers` (default 2) and `--workers` may narrow it. Page, target and time budgets are reserved before dispatch, so parallel workers cannot overspend. A command deadline bounds new claims and physical request timeouts; in-flight requests finish under their own bound. If consecutive tool/network failures trip the circuit breaker, stop dispatching, keep the checkpoint and deliver what exists with the blocking reason.

`finish` closes further input while retaining queued work. Continue bounded `process` calls until completed/partial or explicitly paused. During processing, incremental snapshots are exported at the configured new-qualified threshold or time interval and always use the same qualification, channel, dedup and suppression policy as the final export.

Export after processing using default `xlsx`, `balanced`, `zh`: four tables with Chinese headers. CSV is optional on request. Only qualified, unsuppressed, deduplicated companies meeting the frozen channel policy count toward delivery. The workbook, snapshots and completion count share that policy; never change the denominator.

Return the local Excel link and quality report with discovered, matched, contactable, excluded, uncertain, evidence-missing and shortfall counts, plus the effective spec/rule/release versions. State that public channels have source evidence but actual delivery has not been tested. If fewer than requested qualify, deliver the available results and name the remaining gap; do not claim the full batch or an 8-hour service level was proven. Report snapshot time separately from latest progress. A small live smoke test is distinct from independent human quality acceptance or a completed 200-company run.

## Live results page

When the user wants to watch a run or browse the list themselves, start `live --run RUN` through the controller. It writes one committed-state projection inside the managed artifact directory and serves a short-lived page bound only to `127.0.0.1` with a random per-process token. The command is foreground, exits on inactivity or Ctrl+C, never collects, never changes a run, never exports by itself and never opens a browser unless the user passes `--open`. Use `--rebuild-only` to refresh the saved page without serving.

The page shows the frozen target, admitted, deliverable, pending, shortfall, real status and observation time with searchable, filterable evidence. Treat it as a read-only view: deliverable counts and exports come from the same core selection. Never treat the projection file as an authority, never copy contacts from it into messages automatically, and never claim that a page refresh proves the run is still alive. Pending enterprises are not deliverable and do not enter the formal export.

## Saved runs and administration

Use `list` to find a specifically requested saved run and `status` to inspect it, its frozen policy, missing items and versioned metrics. `suppressions list/add/remove`, `delete-company`, and `maintenance` provide the former workbench administration; use changes only within the user's request. Legacy v1 runs keep their original qualification, channel, dedup and counting semantics; do not re-decide them with v2 conditions. Automatic retention runs on a database-backed throttle during commands. Legacy databases are archived separately and are not resumed.

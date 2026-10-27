---
name: b2b-leads
metadata:
  version: "2.2.0"
description: Research B2B target companies and their public business contact channels for any industry or country, continue saved acquisition batches, and deliver sourced Chinese Excel lead lists through the local Python acquisition core. Use for requests such as finding manufacturers, distributors, integrators or service providers, sourcing prospective business customers, or continuing a saved lead research run.
---

# B2B customer research

> 版本标记：Skill 2.2.0 · 核心 2.2.0 · 规则 b2b-policy-2。以 `capabilities` 返回的 `skill_version`/`core_version`/`rule_version` 和发布清单为准；`VERSION` 文件记录同一版本。

Turn the user's customer requirements into a sourced company list. Reuse this project's durable Python core for qualification, channel checks, deduplication, suppression, scoring, progress and export. The Skill is the only entrypoint; commands call the core directly without an HTTP API or login. Do not automatically send outreach, write CRM records, buy sources or ask for a new API key.

## Start or continue

Read [the controller reference](references/controller.md) for commands. Read [the segment configuration](references/segment-spec.md) before writing a new customer spec, and [the evidence contract](references/evidence-contract.md) before importing candidates. Use the checkout's `.venv/Scripts/python.exe` to execute this Skill's `scripts/control.py`. An installed `project.json` selects a verified, frozen Skill/core release while keeping database configuration and business artifacts in the original project. If release verification fails, reinstall or select a verified previous release; do not bypass the failure by calling development code. The controller prepares only PostgreSQL and the isolated Skill schema. Never expose database credentials.

- Extract the customer definition: industry, countries, product/service, customer type, exclusions, quantity and any channel requirement. Ask one grouped clarification only when the target country or the customer definition is missing.
- Use `spec_version: 2` for new research. For speed-first tasks, explicitly set `execution_policy.company_workers: 4`, `snapshot_every_new_qualified: 5`, and `snapshot_interval_seconds: 300`; existing runs keep their frozen settings. Keep the ordinary zero-paid-source defaults: quantity 200, candidate budget 600, cumulative nonpaused run time 8 hours, Chinese Excel delivery. Read the saved run's limits instead of restating another denominator.
- Run `capabilities` before inventing conditions. Only registered fields and operators are accepted; unsupported conditions fail with a path instead of being silently dropped. Do not hardcode one country's headquarters, phone format or business model for another task.
- If the user requests exclusion of existing customers, locate the supplied file or ask for its location together with any other missing requirements. Put observed company/domain/email/phone identifiers into the spec's `exclusions` and record the file hash and count in the checkpoint without echoing customer details. The backend also applies its existing workspace suppression list.
- Create a new descriptive `--run` once per intentional new request and record it. Continue an existing request with exactly the same run; after an interrupted `create`, retry its original spec and run. Do not infer continuation from two similar goals.
- Call `prepare`, then `create` from a JSON customer spec. `create` freezes the normalized configuration, configuration hash, rule version and release, and starts the research clock. Inspect `status` before resuming saved work; never resume unrelated batches.

## Research, precheck and admit

Use the current Agent's available native internet search and page-open tools. Start with information-rich directories and association member pages that can support multiple companies. Batch independent queries and page opens up to the available tool limits; use a tool's multi-query/multi-URL input when supported. Do not research one company from start to finish before discovering the next. Search snippets are discovery hints, not factual evidence. If tools are unavailable, save a checkpoint and pause; explain the missing capability without inventing results or falling back to a paid provider.

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

## Saved runs and administration

Use `list` to find a specifically requested saved run and `status` to inspect it, its frozen policy, missing items and versioned metrics. `suppressions list/add/remove`, `delete-company`, and `maintenance` provide the former workbench administration; use changes only within the user's request. Legacy v1 runs keep their original qualification, channel, dedup and counting semantics; do not re-decide them with v2 conditions. Automatic retention runs on a database-backed throttle during commands. Legacy databases are archived separately and are not resumed.

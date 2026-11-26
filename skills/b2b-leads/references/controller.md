# Local controller

Use the checkout `.venv/Scripts/python.exe` (Windows) or `.venv/bin/python` (other supported environments) to execute this Skill's `scripts/control.py`. Installed `project.json` records the original project and immutable release under `.runtime/skill-releases/<release_id>`; the wrapper verifies all packaged files and the installed Skill before importing that release. Project edits do not change an installed release. Reinstall to activate updated code. `RELEASE_INTEGRITY_ERROR` requires reinstalling or selecting a verified previous release, rather than falling back to the live checkout. The source-copy examples below run from the checkout; for an installed Skill substitute its actual `scripts/control.py` path. CLI output is JSON; run state, checkpoints and operation replay records live in PostgreSQL. Keep evidence input JSON under ignored `.runtime/b2b-leads/`; exports, reports and raw pages live under the independent `data/skill-v2/` artifact root (or SCA_ARTIFACT_ROOT).

```powershell
.\.venv\Scripts\python.exe scripts/b2b_leads.py capabilities
.\.venv\Scripts\python.exe scripts/b2b_leads.py brief --spec-json .runtime/b2b-leads/spec.json
.\.venv\Scripts\python.exe scripts/b2b_leads.py brief --spec-json .runtime/b2b-leads/spec.json --method directory_batch --out-md .runtime/b2b-leads/brief.md
.\.venv\Scripts\python.exe scripts/b2b_leads.py prepare
.\.venv\Scripts\python.exe scripts/b2b_leads.py create --run de-packaging-20261027 --spec-json .runtime/b2b-leads/spec.json --confirm-hash <confirm_hash>
.\.venv\Scripts\python.exe scripts/b2b_leads.py advance --run de-packaging-20261027 --chunk round-01 --input-json .runtime/b2b-leads/round-01.json --max-jobs 20 --max-seconds 60
.\.venv\Scripts\python.exe scripts/b2b_leads.py validate-input --spec-json .runtime/b2b-leads/spec.json --input-json .runtime/b2b-leads/round-01.json
.\.venv\Scripts\python.exe scripts/b2b_leads.py append --run de-packaging-20261027 --chunk round-01 --input-json .runtime/b2b-leads/round-01.json
.\.venv\Scripts\python.exe scripts/b2b_leads.py process --run de-packaging-20261027 --max-jobs 20 --max-seconds 60 --operation-key process-round-01
.\.venv\Scripts\python.exe scripts/b2b_leads.py supplement --run de-packaging-20261027 --company-id company_... --input-json .runtime/b2b-leads/evidence-01.json --operation-key supplement-01
.\.venv\Scripts\python.exe scripts/b2b_leads.py status --run de-packaging-20261027
.\.venv\Scripts\python.exe scripts/b2b_leads.py status --run de-packaging-20261027 --action pause
.\.venv\Scripts\python.exe scripts/b2b_leads.py process --run de-packaging-20261027 --resume
.\.venv\Scripts\python.exe scripts/b2b_leads.py finish --run de-packaging-20261027
.\.venv\Scripts\python.exe scripts/b2b_leads.py export --run de-packaging-20261027
.\.venv\Scripts\python.exe scripts/b2b_leads.py readable-export --input data/skill-v2/<export>.xlsx --output data/skill-v2/<batch>-readable.xlsx
.\.venv\Scripts\python.exe scripts/b2b_leads.py live --run de-packaging-20261027
.\.venv\Scripts\python.exe scripts/b2b_leads.py live --run de-packaging-20261027 --rebuild-only
```

Version is printed on stderr for every command (`b2b-leads Skill … · 核心 … · 规则 … · release …`) and every JSON result carries a `versions` object (skill/core/rule/schema/release_id/installed).

Staged evidence (offline converter, added in 2.5.0; same precheck and admission rules):

```powershell
.\.venv\Scripts\python.exe scripts/b2b_leads.py stage-draft --spec-json SPEC.json --artifact-json ARTIFACT.json --candidate-ids a,b --as-of 2026-11-03T00:00:00+00:00 --out subset.json
.\.venv\Scripts\python.exe scripts/b2b_leads.py advance --run RUN --artifact-json ARTIFACT.json --candidate-ids a,b --as-of 2026-11-03T00:00:00+00:00
```

`stage-draft` validates observed evidence, converts only supported fields and returns the submittable count plus per-candidate error codes; `advance --artifact-json` does the same conversion in-process before submitting and defaults the chunk to the content hash. Neither overrides precheck, channel or delivery rules.

`capabilities`, `brief`, `validate-input`, `stage-draft` and `readable-export` are offline: no database write, no HTTP/DNS and no implicit maintenance. `readable-export` reads one existing 39-column export and writes the 15-column readable workbook plus `来源`/`说明` sheets; it never contacts the core or re-fetches evidence, and the original export stays as the evidence file. `brief` returns the normalized spec, `spec_hash`, `confirmation_summary` (with `headline` for the first screen, `projection` for the rough time/token estimate, and `fields`/`sections` for the full values and hashes), `confirm_hash`, `defaults_applied`, `validation` and `pending_hints`. `--method per_company|directory_batch` (default `per_company`) only changes the estimate shown, and `--out-md` writes the same summary as Markdown; neither changes the confirmation scope or `confirm_hash`. It does not judge the market or promise a quantity or a finish time. Every other command prepares the core and invokes bounded database-backed maintenance. `create` checks `--confirm-hash` before that preparation; a missing or different hash is `SEGMENT_NOT_CONFIRMED` (409) and does not open the database. The hash shows that the submitted config matches the summary. It does not prove who confirmed. `prepare` connects to PostgreSQL, starts only the existing workspace Windows cluster if needed, creates the independent Skill database and validates its schema. There is no API, browser workbench, login or worker. `--url` has been removed. Existing legacy databases and runtime state are not upgraded or resumed; credentials are never printed.

`live` is a foreground viewing command: it writes the committed-state projection under the artifact root and serves a short-lived page bound only to `127.0.0.1` with a per-process token, printing one JSON line with the URL. It never collects, changes a run, runs maintenance from the page or exports by itself; the page's export button calls the core with the current filters. It exits on inactivity or Ctrl+C, and only `--open` opens a browser. `--rebuild-only` refreshes the saved projection without serving. Do not treat the projection file, a page refresh or the lack of a background process as proof that the run is active.

## Customer spec

`create` accepts a v1 or v2 configuration and requires `--confirm-hash` from `brief` for that same spec. A new spec with no `spec_version` is validated and frozen as v2 (`capabilities.default_spec_version` is 2). Explicit v1 keeps the historical fields and semantics; the example below is v1 only because it sets `spec_version` to 1:

```json
{"spec_version":1,"name":"美国货代公开商务联系方式","industry":"freight forwarding","countries":["US"],"quantity":200,"keywords":["freight forwarder"],"languages":["en"],"exclusions":[],"paid_sources":false,"budget":0}
```

v2 adds `qualification_policy`, `evidence_policy` and `execution_policy`; see [segment-spec.md](segment-spec.md) for the complete contract, field/operator table and two non-freight examples. See [segment-brief.md](segment-brief.md) for the confirmation flow. `create` freezes the normalized configuration, its hash, the confirmation record, the rule version and the executing release. An unknown version (`UNSUPPORTED_SPEC_VERSION`), an unregistered field/operator (`UNSUPPORTED_CONDITION`), two conflicting forms of one setting (`CONFLICTING_CONFIG`), or a missing/stale confirm hash (`SEGMENT_NOT_CONFIRMED`) is rejected rather than narrowed silently. Status, export, and resume read a stored v1 spec as v1. Replaying `create` with a different normalization conflicts and tells the caller to use `status`; it does not rewrite the frozen spec or invent a historical confirmation.

The customer spec accepts quantities 50–1000; the candidate cap is three times the target, so the default target 200 permits 600 candidates. Read `research.candidate_limit` from the saved run. For a 10-company smoke test retain the real target (normally 200), then explicitly finish and report partial fulfillment. Do not lower the denominator or call this a full delivery.

When the request excludes an existing customer list, read the user-specified CSV, Excel or JSON file first and populate `spec.exclusions` with its observed company names, domains, emails or international phone numbers (up to the existing 1,000-item limit). Use clearly named columns; ask about ambiguous mappings or an unspecified file location. The existing global suppression list is applied additionally. Keep the source file hash and number of exclusion identifiers in the run checkpoint so the scope is reviewable; do not copy customer details into chat. A larger list must be resolved before creation rather than silently truncated or ignored.

## Evidence input

Write append inputs using UTF-8 JSON. `candidates` has at most 20 records. Every record needs a page excerpt and a timezone-aware `retrieved_at`; every fact or channel needs its own `sources` row. Unknown top-level fields are rejected (`UNKNOWN_FIELD`); put non-qualifying notes in `extensions`. Search-snippet source types are rejected. See [evidence-contract.md](evidence-contract.md) for the full shape, versioned fact fields and admission rules.

```json
{
  "candidates": [{
    "company_name": "Observed Company GmbH",
    "country_code": "DE",
    "headquarters_country": "DE",
    "business_models": ["manufacturer"],
    "offering_products": ["reusable industrial packaging"],
    "industry_text": "industrial packaging",
    "source_url": "https://directory.example/member/observed-company",
    "source_type": "directory",
    "retrieved_at": "2026-10-27T02:00:00Z",
    "source_excerpt": "Observed Company GmbH, Düsseldorf, Germany, manufactures reusable industrial packaging.",
    "email": "sales@company.example",
    "sources": [
      {"field":"company_name","value":"Observed Company GmbH","source_url":"https://directory.example/member/observed-company","source_type":"directory","retrieved_at":"2026-10-27T02:00:00Z","source_excerpt":"Observed Company GmbH, Düsseldorf, Germany"},
      {"field":"entity.headquarters_country","value":"DE","source_url":"https://directory.example/member/observed-company","source_type":"directory","retrieved_at":"2026-10-27T02:00:00Z","source_excerpt":"Observed Company GmbH, headquartered in Düsseldorf, Germany"},
      {"field":"email","value":"sales@company.example","source_url":"https://directory.example/member/observed-company","source_type":"directory","retrieved_at":"2026-10-27T02:00:00Z","method":"agent_page_observation","source_excerpt":"Observed Company GmbH — Business email: sales@company.example"}
    ]
  }],
  "checkpoint": {"completed_queries":["industrial packaging Germany"],"next_queries":["reusable packaging manufacturer contact"],"next_step":"research missing official websites"},
  "external_tool_usage": {"search_calls":1,"open_calls":2,"unobserved_calls":null}
}
```

The `.example` values above explain shape and are not real candidates. Use the actual observed email/phone value or omit the field. Formatting differences in a phone number may be normalized. A general company description cannot substantiate an added email or phone. An unconfirmed or conflicting website cannot provide official channels. The core can still validate a directory-observed channel, but the default per-company protocol treats a directory row as discovery only.

Default batches run `per_company`: every record names the official website and one source row per fact, with the page URL, collection time, method and a literal excerpt that starts with the company name. Qualification (country, own products/services) and channels come from the company's own About, Contact or product pages. A company without a usable official website stays `needs_review` or is dropped; do not complete it from the directory row, and do not invent an email, phone or country. Directory pages are opened natively one by one; a local script must not batch-crawl them.

For an observed form use top-level `contact_form_url` and a source entry with `field:"contact_form_url"`, `value` and `source_url` set to the actual form URL, `method:"observed_contact_form"`, `source_type:"official_site"` (or `directory` when appropriate), `form_fields:["email","message"]`, a short excerpt, and `retrieved_at`. A mere link titled Contact does not establish that a form exists.

Directory discovery is recorded per page in the checkpoint as a `source_ledger`; the same entries can also live in a referenced run artifact:

```json
{"source_ledger":[{"url":"https://directory.example/list/outdoor?page=3","page":3,"entries":25,
 "relevant":11,"selected":8,"collected_at":"2026-11-15T03:20:00+00:00","failure":null}]}
```

`entries` counts rows on that page, `relevant` those matching the target, `selected` those taken forward. A failed page keeps the exact reason in `failure` and no invented counts. The ledger is descriptive evidence of discovery, not a page-budget counter.

`checkpoint` is an arbitrary JSON object; no secrets. Tool counters describe calls made by the Agent, not HTTP sends. Use null when not measured and do not fill unknown counters with zero. Backend `active_wall_seconds` counts running time including Agent research; pause between sessions.

## Search and fetch gates

Discovery queries combine category, country and channel words; a brand name alone is not a query, and 3–5 variants are expected per round. Snippets are discovery hints only.

Before a page is used as evidence, classify it: `readable` requires a 2xx status, at least 600 characters of visible text (scripts, styles, noscript and SVG removed), and no challenge page. A challenge page is recognized by its title or a short body (`Just a moment`, `Attention Required`, `Access Denied`, `Too Many Requests`, `Enable JavaScript and cookies`, `Checking your browser`, `Verify you are human`); a bare `captcha` or `cloudflare` mention is script noise and is not a challenge by itself. An unreadable page gets one retry through the company's Contact or About page; if it stays unreadable the company is `needs_review` with reason `page_unreadable`, uncounted, and no fact or channel is recorded from it. A non-2xx fetch is a result, not proof the company lacks a channel. A cross-origin redirect is refused by the fetch tool: retry the target URL it names. Keep `{url, status, chars, readable}` per page in the checkpoint and report page/readable/empty counts at delivery.

## Precheck, recovery and delivery

- Prefer `advance` for normal evidence-ready chunks: the existing atomic append precheck and a bounded process run share one controller startup and one preparation/maintenance pass. The response omits repeated full spec/plan data while retaining progress, missing items, admission, errors and snapshot references. Submit the first 5 companies, then chunks of 10–20. Invalid records reject the whole chunk; offline `validate-input` remains the tool for finding the valid subset. Invalid bounds or unsupported v1 worker settings reject before candidate admission.
- Evidence-ready does not require an Agent-observed channel when necessary qualification facts and a confirmed official website are available. Submit then let the core fill missing channels; avoid serial native Contact/Team page opens that duplicate the core's work. Already-observed directory channels still enable the zero-network path. Native research remains necessary for unresolved qualification facts, blocked sites or channel gaps left by the core.
- `advance` uses separate durable append/process ledger entries. After an interruption, retry the exact run, input, chunk and options; append replay cannot duplicate candidates and the derived process operation key replays completed work. Do not use the same chunk for a new observation. A paused run requires `--resume`. A later bounded step uses `process` or another new-input chunk; rerunning a completed chunk does not dispatch fresh work.
- New speed-first v2 specs may explicitly set `company_workers:4`, `snapshot_every_new_qualified:5`, `snapshot_interval_seconds:300`; defaults and saved runs remain unchanged. `--workers` only narrows the frozen setting. v2 company-channel research follows observed Contact links before Team/About; named-person research keeps Team priority. Parsing counters in each company's `pipeline` describe actual scoped HTML reuse and `contact_extractions`.

- `validate-input --spec-json ... --input-json ...` validates each record independently and returns `records[]` (`index`, `admissible`, `reason_code`, `path`, `normalized_hash`), `submittable_indexes` and counts. It validates structure and entry evidence only; it does not promise qualification. Submit the submittable subset as one atomic chunk.
- Reuse the original `--run`, `--chunk` and exact input after a lost response. The database operation ledger preserves the request hash and committed result. For process/finish, an optional `--operation-key` replays an explicitly identified invocation; repeated calls without that flag continue bounded work. Changing a chunk's input is rejected; use a new chunk for a new observation.
- `process` performs at most 20 jobs per call. A v2 run runs up to its frozen `execution_policy.company_workers` (default 2, maximum 4); `--workers N` may narrow that for one call. v1 runs stay serial and reject `--workers` above 1 (`UNSUPPORTED_OPTION`). The seconds limit stops new claims; an in-flight bounded job may run beyond it, and its HTTP timeout is clipped to the remaining command budget. It does not resume a paused batch unless `--resume` is explicit.
- Page budget is reserved atomically before dispatch, so parallel workers cannot exceed the batch or per-company page allowance. A batch whose page budget is exhausted stops claiming, closes input and reports the real gap.
- Source cooldowns survive separate process calls and are shared across runs. For a retryable limit, respect its saved retry time instead of starting another run to retry the same host.
- Consecutive tool/network failures open a circuit: dispatching stops, the checkpoint is kept, and the run reports `circuit_open`. `status --action retry` closes it explicitly.
- `finish` closes input and processes a bounded portion of queued work. If `requires_more_processing` is true, continue `process`; input closure never means queued work was discarded. An optional `--input-json` can save the final checkpoint and tool counters.
- `status --action pause|resume` controls only this saved batch. A busy/version-conflict response requires checking status, then repeating the operation; never create a replacement batch to work around contention.
- `supplement --run RUN --company-id ID --input-json FILE --operation-key KEY` appends incremental evidence to a retained company. The input carries `expected_revision` and `candidates` (same evidence shape). It preserves old evidence and collection times, re-decides offline (no HTTP/DNS), creates no new candidate and never resumes dispatch. A stale revision returns `VERSION_CONFLICT`; changing identity fields returns `IMMUTABLE_IDENTITY`; no new evidence returns `NO_NEW_EVIDENCE`; closed input returns `INPUT_CLOSED`. Retry the exact input and key after an interruption.
- `reassess --run RUN --company-id ID [--company-id ID ...] --operation-key KEY` re-evaluates at most 20 completed `needs_review`/`insufficient` records from their original inputs and retained, checksum-verified page snapshots after a rule fix. Wait for processing to finish first. It performs no HTTP or DNS, preserves original collection/verification times and page budgets, and records before/after hashes, snapshot hashes and the executing release in the audit ledger. Retry the exact IDs and key after interruption. Missing, expired or corrupt snapshots are rejected atomically; uncertain identity/country and unsupported channels remain subject to normal rules. This does not requeue failed jobs or supply new evidence.
- When the user explicitly requests research beyond the saved time limit, `extend-research --run RUN --hours 24 --operation-key KEY` raises the total cumulative active-time limit to the requested integer hours, up to 72. It only increases that limit before the existing time/candidate limit is reached and while input remains open; wait for processing to finish first. It preserves elapsed time, pause state, target quantity, candidate cap, source/page limits and paid-source budget, and records the change and executing release in the audit ledger. Retry the same hours and key after interruption. It does not reopen a finished/closed run or restart its clock.
- Default export reuses an unchanged delivery projection and creates a fresh snapshot after progress, suppression or channel quality changes. Retrying the same named export key replays that snapshot; revoked or expired named snapshots require a new key. Use an explicit `--export-key preview` to retain a named snapshot, or a new key for another one. `--format csv` requests the existing CSV bundle. No format switch silently replaces Excel.
- During processing a v2 run also writes incremental snapshots when `snapshot_every_new_qualified` new deliverable companies or `snapshot_interval_seconds` have passed with a changed projection. Snapshots use the same policy as the final export; status lists `stats.snapshots` and `stats.last_snapshot`. No change means no duplicate file.
- After an export, `readable-export --input EXPORT.xlsx --output READABLE.xlsx` writes the reading copy from that workbook alone, with no database, HTTP or DNS. `名单` has 序号、评级、客户性质、企业名称、品类、州、城市、地址、官网、邮箱、电话、渠道、线索分、置信度、备注; `来源` aggregates URL、企业数、来源条数、采集时间; `说明` states the 口径、数据来源、联系方式未验证、限制、版本、实际用时/token. Rating: A=high relevance with an email whose domain matches the website; B=high relevance with only phone/form, or medium relevance with email; C=low relevance, no channel, any obviously conflicting mailbox domain, or medium relevance without email. Keep the original export unchanged beside it as the evidence file.
- `finish` and every fresh `export` write `data/skill-v2/reports/run-<run_id>.json` and put the same object in `manifest.run_report`: `started_at`, `finished_at`, `elapsed_wall_seconds`, `active_seconds`, `paused_seconds`, `pages`, `candidates`, `admitted`, `qualified`, `contactable`, `exported`, `estimate` (method/hours/tokens, still uncalibrated) and `actual` (hours and tokens). Tokens appear only when the runtime publishes `B2B_TOKEN_USAGE_JSON` (object with `input`/`output`/`cache`/`total`/`token_scope`); otherwise `actual.tokens` is null and `token_source` is `unavailable`. Never fill tokens with the estimate. Reply with `run_report.summary_text` verbatim: `总用时 X；总 token Y` or `总 token 未取到（运行环境未提供…）`.
- Return `file` and `report_file` as local clickable links. `summary`, `snapshot_at`, `delivery_policy` and `policy` describe the actual export snapshot, including a cached export; `current_progress` separately describes the latest batch state. Unknown observations remain null. Contactable means public, sourced, quality-eligible channels that meet the frozen channel policy; delivery/reachability has not been exercised.
- `status` reports `discovered` (admitted enterprises, unchanged meaning), `append_ready`, `discovery_seen`, `qualified_by_origin` (live/offline evidence/supplement/reassess), field and channel `missing_items`, failure categories and versioned metrics. Tool invocations and physical HTTP attempts are separate; unknown stays null.
- The eight-hour budget counts every nonpaused wall-clock interval, including time after a closed chat or machine failure. Pause at session boundaries; a recovered expired run must report its remaining gap rather than claiming active research lasted eight hours.

## Pilot gate

Before scaling to the full quantity, run the first 10–15 companies with the default `per_company` protocol and judge the pilot before deciding the rest:

- `export --run RUN --export-key pilot`, then `readable-export` on that workbook.
- Pass gate: `说明` 页的「高相关」占比 ≥ 70%，渠道覆盖 ≥ 90%，且邮箱域名冲突能逐行解释。明显属于别家公司的邮箱域名算不合格。
- Do not start the 200-company run when the pilot fails; fix the spec, sources or per-company research first. Record the pilot counts, verdict and timestamp in the checkpoint (`checkpoint.pilot`).
- The 12-hour limit may not fit 200 companies at the measured per-company pace; decide explicitly between extending to 16 hours (`extend-research`) and accepting a named shortfall. Do not pad the numerator.

## Hourly progress report

For a task estimated above 30–60 minutes, after `create` start one hourly report to `this_session` and record its `schedule_id` in the checkpoint. Template: current deliverable / qualified / pending, gap to the target, elapsed, projected remaining time at the current pace, and blockers. Do not repeat unchanged numbers; cancel the schedule when the batch finishes or is paused. A user asking for a separate report channel gets `new_once` instead of sharing this session.

## Data sources (2.6)

`discover --run RUN [--source eu_ted|eurostat_comext|google_places|all] [--limit N]` runs the public adapters and stores **unverified** rows in the source pool plus an encrypted raw-record envelope. Unverified rows never count as discovered/qualified and never enter the dedup pool; only evidence-backed `advance` admits a company. `discover --run RUN --list [--source X]` reads the pool. `--reject <id> --reason "..."` requires a non-empty reason. `--explain <id> --yes` decrypts one envelope after explicit confirmation; a failed decryption returns `{"status":"undecryptable","reason":"key_mismatch"}` and keeps the ciphertext instead of raising. `sources` reports adapter availability and credentials. Single-source failures are recorded as `source_error` / `source_empty` / `source_limit` / `source_parse_error` without blocking other sources; when every source fails the run is marked `partial_no_source` and discovery stops instead of spinning. Use `discover --run RUN --legacy-search` to fall back to manual discovery (`mode: legacy`, no `source_ref`).

Secrets: `PROSPECT_SOURCE_RAW_ENVELOPE_SECRET` is required in production (startup refuses without it) and degrades to plaintext envelopes marked `dev-insecure` in development; `PROSPECT_CURSOR_SECRET` is optional but must be ≥32 bytes when set; `PROVIDER_CREDENTIAL_KEY` is only needed for paid sources. `capabilities` reports these under `secrets`.

## Administration

`list` lists saved runs. `status --action pause|resume|cancel|retry` controls only the named run. Use `suppressions list`, `suppressions add --type domain|company|email|phone --value ... --reason ...`, and `suppressions remove --id ...` for exclusions explicitly requested by the user. `delete-company --id ...` deletes that enterprise and revokes its exports. `maintenance` previews expired data; `maintenance --apply` cleans it. Automatic retention only runs when a command executes; idle machines perform no scheduled work.

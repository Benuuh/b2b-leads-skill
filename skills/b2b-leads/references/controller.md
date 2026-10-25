# Local controller

Use the checkout `.venv/Scripts/python.exe` (Windows) or `.venv/bin/python` (other supported environments) to execute this Skill's `scripts/control.py`. Installed `project.json` records the original project and immutable release under `.runtime/skill-releases/<release_id>`; the wrapper verifies all packaged files and the installed Skill before importing that release. Project edits do not change an installed release. Reinstall to activate updated code. `RELEASE_INTEGRITY_ERROR` requires reinstalling or selecting a verified previous release, rather than falling back to the live checkout. The source-copy examples below run from the checkout; for an installed Skill substitute its actual `scripts/control.py` path. CLI output is JSON; run state, checkpoints and operation replay records live in PostgreSQL. Keep evidence input JSON under ignored `.runtime/b2b-leads/`; exports, reports and raw pages live under the independent `data/skill-v2/` artifact root (or SCA_ARTIFACT_ROOT).

```powershell
.\.venv\Scripts\python.exe scripts/b2b_leads.py prepare
.\.venv\Scripts\python.exe scripts/b2b_leads.py create --run us-forwarders-20261024 --spec-json .runtime/b2b-leads/spec.json
.\.venv\Scripts\python.exe scripts/b2b_leads.py append --run us-forwarders-20261024 --chunk round-01 --input-json .runtime/b2b-leads/round-01.json
.\.venv\Scripts\python.exe scripts/b2b_leads.py process --run us-forwarders-20261024 --max-jobs 20 --max-seconds 60 --operation-key process-round-01
.\.venv\Scripts\python.exe scripts/b2b_leads.py status --run us-forwarders-20261024
.\.venv\Scripts\python.exe scripts/b2b_leads.py status --run us-forwarders-20261024 --action pause
.\.venv\Scripts\python.exe scripts/b2b_leads.py process --run us-forwarders-20261024 --resume
.\.venv\Scripts\python.exe scripts/b2b_leads.py finish --run us-forwarders-20261024
.\.venv\Scripts\python.exe scripts/b2b_leads.py export --run us-forwarders-20261024
```

`prepare` connects to PostgreSQL, starts only the existing workspace Windows cluster if needed, creates the independent Skill database and validates its schema. There is no API, browser workbench, login or worker. Every command prepares the core and invokes bounded database-backed maintenance. `--url` has been removed. Existing legacy databases and runtime state are not upgraded or resumed; credentials are never printed.

`create` uses a saved SegmentSpec, for example:

```json
{"name":"美国货代公开商务联系方式","industry":"freight forwarding","countries":["US"],"quantity":200,"keywords":["freight forwarder"],"languages":["en"],"exclusions":[],"paid_sources":false,"budget":0}
```

The current customer spec accepts quantities 50–1000; the candidate cap is three times the target, so the default target 200 permits 600 candidates. Read `research.candidate_limit` from the saved run. For a 10-company smoke test retain the real target (normally 200), then explicitly finish and report partial fulfillment. Do not lower the denominator or call this a full delivery.

When the request excludes an existing customer list, read the user-specified CSV, Excel or JSON file first and populate `spec.exclusions` with its observed company names, domains, emails or international phone numbers (up to the existing 1,000-item limit). Use clearly named columns; ask about ambiguous mappings or an unspecified file location. The existing global suppression list is applied additionally. Keep the source file hash and number of exclusion identifiers in the run checkpoint so the scope is reviewable; do not copy customer details into chat. A larger list must be resolved before creation rather than silently truncated or ignored.

## Evidence input

Write append inputs using UTF-8 JSON. `candidates` has at most 20 records. The backend validates eligibility and channels; opening a page does not guarantee acceptance. Keep raw excerpts short and relevant rather than copying entire pages.

```json
{
  "candidates": [{
    "company_name": "Observed company name",
    "country_code": "US",
    "industry_text": "freight forwarding",
    "website_url": "https://company.example/",
    "source_url": "https://company.example/contact/",
    "source_type": "official_site",
    "retrieved_at": "2026-10-24T04:00:00Z",
    "source_excerpt": "Observed company name provides freight forwarding services in the United States; business telephone +1 ...",
    "phone": "+1 ...",
    "sources": [
      {"field":"company_name","value":"Observed company name","source_url":"https://company.example/contact/","retrieved_at":"2026-10-24T04:00:00Z","source_excerpt":"Observed company name, United States"},
      {"field":"website_url","value":"https://company.example/","source_url":"https://directory.example/member/observed-company","retrieved_at":"2026-10-24T04:00:00Z","source_excerpt":"Observed company name; official website https://company.example/","method":"agent_page_observation","source_type":"directory"},
      {"field":"phone","value":"+1 ...","source_url":"https://company.example/contact/","retrieved_at":"2026-10-24T04:00:00Z","source_excerpt":"Business telephone +1 ...","method":"agent_page_observation"}
    ]
  }],
  "checkpoint": {"completed_queries":["freight forwarder United States"],"next_queries":["regional freight forwarder contact"],"next_step":"research missing official websites"},
  "external_tool_usage": {"search_calls":1,"open_calls":2,"unobserved_calls":null}
}
```

The `.example` values above explain shape and are not real candidates. Use the actual observed email/phone value or omit the field. Each imported channel needs its own `field`, `value`, `source_url`, `retrieved_at` and literal `source_excerpt`; formatting differences in a phone number may be normalized. A general company description cannot substantiate an added email or phone. `retrieved_at` must have a timezone and every sourced assertion needs a page URL. Search-snippet source types are rejected. Keep official website identity evidence distinct from business fit. The backend evaluates evidence; self-declared identity flags cannot establish an association. An unconfirmed or conflicting website cannot provide official channels; a directory can still support a channel when the same page explicitly identifies the company.

For an observed form use top-level `contact_form_url` and a source entry with `field:"contact_form_url"`, `value` and `source_url` set to the actual form URL, `method:"observed_contact_form"`, `source_type:"official_site"` (or `directory` when appropriate), `form_fields:["email","message"]`, a short excerpt, and `retrieved_at`. A mere link titled Contact does not establish that a form exists.

`checkpoint` is an arbitrary JSON object; no secrets. Tool counters describe calls made by the Agent, not HTTP sends. Use null when not measured and do not fill unknown counters with zero. Backend `active_wall_seconds` counts running time including Agent research; pause between sessions.

## Recovery and delivery

- Reuse the original `--run`, `--chunk` and exact input after a lost response. The database operation ledger preserves the request hash and committed result. For process/finish, an optional `--operation-key` replays an explicitly identified invocation; repeated calls without that flag continue bounded work. Changing a chunk's input is rejected; use a new chunk for a new observation.
- `process` performs at most 20 jobs serially. Its seconds limit is checked between jobs: one in-flight bounded job may run beyond it. Repeat when queued work remains. It does not resume a paused batch unless `--resume` is explicit.
- Source cooldowns survive separate process calls and are shared across runs. For a retryable limit, respect its saved retry time instead of starting another run to retry the same host.
- `finish` closes input and processes a bounded portion of queued work. If `requires_more_processing` is true, continue `process`; input closure never means queued work was discarded. An optional `--input-json` can save the final checkpoint and tool counters.
- `status --action pause|resume` controls only this saved batch. A busy/version-conflict response requires checking status, then repeating the operation; never create a replacement batch to work around contention.
- Default export reuses an unchanged delivery projection and creates a fresh snapshot after progress, suppression or channel quality changes. Retrying the same named export key replays that snapshot; revoked or expired named snapshots require a new key. Use an explicit `--export-key preview` to retain a named snapshot, or a new key for another one. `--format csv` requests the existing CSV bundle. No format switch silently replaces Excel.
- Return `file` and `report_file` as local clickable links. `summary`, `snapshot_at` and `delivery_policy` describe the actual export snapshot, including a cached export; `current_progress` separately describes the latest batch state. Unknown observations remain null. Contactable means public, sourced, quality-eligible channels; delivery/reachability has not been exercised.
- The eight-hour budget counts every nonpaused wall-clock interval, including time after a closed chat or machine failure. Pause at session boundaries; a recovered expired run must report its remaining gap rather than claiming active research lasted eight hours.


## Administration

`list` lists saved runs. `status --action pause|resume|cancel|retry` controls only the named run. Use `suppressions list`, `suppressions add --type domain|company|email|phone --value ... --reason ...`, and `suppressions remove --id ...` for exclusions explicitly requested by the user. `delete-company --id ...` deletes that enterprise and revokes its exports. `maintenance` previews expired data; `maintenance --apply` cleans it. Automatic retention only runs when a command executes; idle machines perform no scheduled work.

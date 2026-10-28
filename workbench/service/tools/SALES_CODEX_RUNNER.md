# N150 sales draft runner

`sales_codex_runner.py` is an inference child, not the durable queue, ERP
uploader, product creator, or procurement executor. Run it as the owner's
`ulcodex` user. The trusted host queue exports a private job and later checks
permissions, versions, sources, catalog identities and draft fields before any
adoption. A successful runner result is only a proposal for human review.

```sh
python3 service/tools/sales_codex_runner.py \
  --job-dir /var/lib/ul-sales-codex/jobs/JOB_ID/phase-1 \
  --rules-root /opt/ul-workbench/deploy/skills/ul-sales-order \
  --codex-binary /home/ulcodex/.local/bin/codex
```

The queue runs two independent, bound phases. `analyze` extracts supplied text,
rows and images. The host retrieves current catalog and NAS historical
candidates from the parsed attributes. `match_and_review` receives that first
pass and per-line evidence candidates. The model never receives access to the
live database, ERP credentials, NAS scripts, browser or shell tools.

## Files

The absolute job directory must be owned by the runner user and mode `0700`.
Input and copied attachments must be owned by that user (or root), mode `0600`,
regular files with one hard link. Paths cannot traverse symlinks or escape the
job directory. Rule files may be readable but cannot be group/world writable;
their root is supplied by trusted host configuration, not by business text.

`input.json` contains only these fields:

- `schema_version`: `ul_sales_codex_job_v1`.
- `job_id`, `session_id`, `base_version`, `input_hash`: queue-owned binding.
- `customer`, `profile_id`, `current_data_version`.
- `phase`: `analyze` or `match_and_review`.
- `messages`, `draft`, `sources`, `source_rows`.
- `attachments`: objects with `id`, `name`, `mime_type`, `sha256`, `path`.
- `matching_context`, and optional `first_pass` for the second phase.

Attachment `path` is relative to the job directory. PNG/JPEG/WebP images are
attached directly with `codex exec --image` after SHA and magic validation.
PDF/Excel source text and row/page provenance come from the bounded extractor;
the model cannot run an arbitrary parser. Unread pages must be recorded as
`needs_review`, rather than declared complete.

The runner reads `SKILL.md` and bounded Markdown/JSON/text files under the
migrated pack's `references` and `rules` directories. It records a hash of every
rule file in the bundle hash. It never runs the original NAS Windows scripts.

`result.json` is the exact schema in `sales_codex_output_schema.json`:
assistant message, proposed draft, questions, new-product suggestions and
source coverage. Unknown fields, missing fields, invalid types and oversized
results fail. New-product suggestions are never interpreted as created ERP
products. Source coverage is required; the host still proves coverage against
its own source manifest and Excel physical row identities.

Each proposed line includes `original_material`, and a product suggestion
includes `material`. They describe explicit source material, never a value
filled backwards from a candidate. The host separately checks the final
proposal's name/spec/standard/grade/surface/material against the current
candidate context. Explicit row coverage prevents merging repeated-SKU rows.

`sales_codex_worker.py` is the host queue entry point. It claims one durable
job, exports both phases with an allowlisted packet, checks every binding and
SHA plus tool count zero, and calls `finish` without adopting. Original Excel
row/sheet/parent hashes are stored as server-owned provenance. PDF pages are
extracted independently and unread pages are rendered only by a configured
Poppler executable; missing dependencies, formulas and unknown nonempty rows
force review. Limits are 500 data rows/lines and 10 images per job.

The root worker changes effective identity only during SQLite transactions to
the existing database owner, so WAL/SHM files keep service ownership. It restores
root for private exports and `runuser`; the inference child cannot read the
business runtime or pinned NAS history database. Production history uses the
configured read-only reference and verified SHA, with no historical prices.

`status.json` contains the queue binding plus input-file, schema, rule and
result hashes, time, phase and a stable operational status/code. It contains
no raw CLI event, reasoning, prompt, secret, or exception text. Both output
files are written atomically with `0600` permissions. Completed results can
be reused locally only when all bindings and hashes remain identical.

## Inference restrictions and recovery

The CLI uses its existing saved ChatGPT login; the runner never opens
`auth.json` and strips inherited API keys, ERP secrets and arbitrary environment
variables. It does not pass `--model` or `-m`. It ignores user config and rules,
disables host skill discovery, plugins, hooks, shell/exec, apps/MCP discovery,
browser, computer use, image generation and multi-agent tools. Supplied reviewed
rules are injected into the prompt and business text is marked as data.

Execution uses `--sandbox read-only`, `--ephemeral`, `--output-schema`, JSONL,
approval `never`, and a dedicated process group. Tool-execution events fail the
job. Partial turns cannot be adopted. A deadline (default 180 seconds, maximum
600) kills the process group and retains `timed_out`; stream size is bounded.
Re-running a failed read-only inference does not submit an ERP order.

The candidate `deploy/ul-sales-codex.service` is an outer trusted queue worker,
running one job under the shared heavy-task lane. It exports jobs and launches
the inference child as `ulcodex`; live DB access is not granted to that user.
The timer is not installed merely by adding these files. The service references
`sales_codex_worker.py`, reviewed with the durable queue before deployment.
Personal owner-only tests do not establish
subscription authorization for an employee-triggered shared service.

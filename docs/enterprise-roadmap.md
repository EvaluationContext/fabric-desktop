# Enterprise local execution roadmap

**Develop with Fabric Desktop. Execute on your infrastructure. Store and consume in Microsoft Fabric.**

This is the assessment and plan for growing Fabric Desktop from a desktop development app into a platform that runs Fabric-compatible data engineering on your own Windows and Linux servers. It was written on 2026-10-15 against `main` at `8edf88a`. Milestone M1, the [headless agent](agent.md), is now built.

Nothing in this document has been verified against a live Fabric tenant. Every Fabric API behaviour it relies on is marked as needing that check (milestone M0).

## Summary

The engine was reusable as it was; what runs it was not ready for unattended use. The metadata framework (`meta/engine.py`), the OneLake store and the Fabric runner already ran with no UI, and `fabric-desktop run-schedule` already ran unattended. What blocked an enterprise agent was everything around the engine: it signed in as one person, kept state in that person's `%LOCALAPPDATA%`, scheduled only through Windows Task Scheduler, and locked with files on one PC.

Two earlier decisions are reversed, as agreed on 2026-10-15:

- **Windows only** becomes Windows and Linux. The engine and the agent run on Linux; the desktop shell stays Windows.
- **Sign in as the user, no service principal** becomes service principal or managed identity for the agent. The desktop app keeps delegated sign-in.

The biggest correctness risk for more than one writer: every entity write reads the whole table, merges in memory and overwrites it (`meta/engine.py`, `_write`). That is safe with one writer and unsafe with two, so per-table write leases came first (now in M1).

## Feature inventory (before M1)

Paths are under `sidecar/fabric_desktop/` unless they start with `app/` or `.github/`. "Tested" means a unit test in `sidecar/tests/` covers it; none was tested on a live tenant.

| Area | What exists | Where | Tested |
| --- | --- | --- | --- |
| Process model | Tauri shell spawns one Python sidecar, JSON lines over stdin/stdout; one `Service` object; method allowlist `METHODS` | `app/src-tauri/src/main.rs`, `server.py`, `service.py` | yes |
| CLI | `serve`, `sign-in`, `preview`, `run`, `history`, `run-schedule <id>` | `cli.py` | yes |
| Unattended mode | `Service(headless=True)`: never opens a sign-in window, logs to a file, exits 1 on failure | `cli.py`, `service.py` | yes |
| Auth | MSAL public client, delegated, per-user token cache; no certificate, managed identity or workload identity | `auth.py` | partly |
| Job files | TOML jobs, DuckDB SQL or Polars script, overwrite or append to a OneLake Delta table | `jobs.py`, `publish.py` | yes |
| Delta writes | delta-rs to `abfss://` by GUID; local staging copy; Fabric-safe profile check | `publish.py`, `delta_profile.py`, `onelake.py` | yes |
| OneLake files | ADLS Gen2 DFS list, download, upload, delete | `onelake.py` | yes |
| Metadata engine | Entities in TOML, configurable layers, contracts, cleaning, checks, quarantine, SCD1/SCD2, incremental, hooks, replay, backfill, dependency order | `meta/engine.py`, `meta/project.py` | yes |
| Entity write | Reads the whole table, merges in Polars, overwrites it in one commit stamped with run id and metadata version | `meta/engine.py`, `meta/store.py` | yes |
| Run history | Delta tables in LH_Meta per environment: `run_log`, `dq_results`, `watermarks`, `schema_history`, `deployments`, `restores` | `meta/store.py` | yes |
| Surrogate keys | Central LH_Keys; Delta MERGE on business key, retried on `CommitFailedError`, duplicate-key detection | `meta/keys.py` | yes |
| Environments and promote | Dev/Test/Prod each with workspace, "Tables in" and "Runs on"; promote gates | `meta_service.py`, `meta/promote.py`, `meta/testing.py` | yes |
| Fabric compute | Bundle upload to LH_Meta, runner notebook, RunNotebook, result file | `fabric_compute.py`, `fabric_api.py` | unit only |
| Fabric REST client | 429 retry with `Retry-After`, long-running operations, paging; workspaces, lakehouses, notebooks, jobs, schedules, maintenance, models, roles, shortcuts, connections, variable libraries | `fabric_api.py` | unit only |
| Local schedules | Windows Task Scheduler; on other systems saved but never started; per-schedule lock file | `local_schedules.py`, `schedule_service.py` | yes |
| Fabric schedules | List, create, enable, delete Fabric job schedules | `schedule_service.py` | unit only |
| Alerts | Teams Workflows webhook for freshness misses and failed PC schedules | `dataops_service.py`, `meta/freshness.py` | yes |
| Compatibility check | Regex scan of notebook cells | `fabric_run.py` (`compute_check`) | yes |
| Spark on this PC | Spark SQL to DuckDB with sqlglot; DuckDB PySpark API; local `notebookutils` | `sql.py`, `spark_local.py`, `notebookutils_local.py` | yes |
| Cost | CU-seconds per run from run times, savings from local runs | `costs.py` | yes |
| Table maintenance | Delta log health, Fabric TableMaintenance, local compaction, retention | `maintenance.py`, `meta/restore.py` | yes |
| Secrets | Source config holds only Key Vault URL and secret name; validator rejects secret-looking fields | `meta/keys.py`, `meta/varlib.py` | yes |
| CI/CD | Windows-only CI: tests, engine build, Trusted Signing (waiting on the account), release to `latest`; app self-updates | `.github/workflows/ci.yml`, `app/src-tauri/src/updater.py` | n/a |

## Gap analysis

| # | Priority | State before M1 | What existed | What was missing | After M1 |
| --- | --- | --- | --- | --- | --- |
| 1 | Headless agent | Partial | `run-schedule` and `Service(headless=True)` | A long-running process; UI-free runtime; per-machine state | Built |
| 2 | Service principal and managed identity | Missing | Delegated MSAL only, but every consumer takes a token callable | Certificate, managed identity, workload identity; tenant setting; workspace roles | Certificate, secret and managed identity built; workload identity not yet |
| 3 | Durable scheduling, queues, retries, recovery | Partial | Task Scheduler; lock files; run logs | Crash-safe queue; retry policy; recovery; Linux scheduling | Built |
| 4 | Safe concurrent Delta writes | Partial | Atomic commits; LH_Keys MERGE retry | Leases for read-merge-overwrite; commit retry; meta-table compaction | Leases and commit retry built; compaction not yet |
| 5 | Centralized logging and history | Partial | `run_log` and friends in LH_Meta | Structured logs; worker and host on runs; export; heartbeat | JSON logs and run fields built; export is M4 |
| 6 | Fabric pipeline integration | Missing | Can trigger existing pipelines | Inbound trigger and status contract | M4 |
| 7 | Multiple workers, Windows and Linux | Missing | Engine runs on Linux | Worker registry, job claims, Linux CI, packaging | Linux CI and packaging built; multi-worker is M3 |
| 8 | Workload compatibility analyzer | Partial | Regex `compute_check`; sqlglot; PySpark shim | AST and SQL analysis with verdicts and reasons | M5 |
| 9 | Governance, secrets, deployment controls | Partial | Promote gates; secret-name rule | Agent Key Vault; resource limits; allowlists; audit; pinned releases | Environment and workspace allowlists, secret refusal, pinned (non-self-updating) agent built; rest M2 and M6 |
| 10 | Cost and performance comparison | Partial | `costs.py` estimates | Measured local cost; side-by-side timing | M5 |

## Architecture changes

Add an agent beside the desktop app; don't rebuild the engine. The desktop app, its RPC methods and its delegated sign-in keep working.

1. **A UI-free runtime.** `runtime.py` builds `Runtime` from the same mixins as `Service` (projects, stores, maintenance, alerts), without the notebook kernel. The project and identity come from `agent.toml`.
2. **Credential providers.** `credentials.py`: `ServicePrincipal` (certificate or secret) and `ManagedIdentity`, answering the same calls as the desktop `Authenticator`, so stores and clients work unchanged.
3. **Agent config and home.** `agent_config.py` reads `agent.toml`; the home folder is per machine (`%ProgramData%` or `/var/lib`), not per user. Secrets are refused.
4. **Durable queue.** `jobqueue.py`: SQLite in WAL mode, job leases, exponential backoff, recovery of jobs whose worker died.
5. **Per-table leases.** `leases.py`: conditional lease files in `LH_Meta/Files/_fabric_desktop/leases/`, renewed while writing; the engine holds one per entity run (`Engine(table_lock=...)`) and retries a lost commit race.
6. **Scheduling inside the agent.** `agent.py` evaluates `[[schedule]]` entries with the same `next_run` logic as PC schedules.
7. **Structured logs and run fields.** `logs.py` JSON lines; `run_log` gains `worker`, `host`, `attempt`, `job_id`.
8. **Fabric pipelines through a OneLake mailbox** (M4): a pipeline writes a request file to LH_Meta; the agent runs it and writes the result, as the Fabric runner already does. No inbound port.
9. **Packaging per OS.** systemd unit and container for Linux, a startup task for Windows; agent releases are installed deliberately, never self-updated.

## Roadmap

| Milestone | Priorities | Depends on | Exit gate | Status |
| --- | --- | --- | --- | --- |
| M0 Live verification | 2, 4 | Entra app with certificate; tenant setting | A service principal writes to OneLake; two concurrent writers to one table end cleanly | Needs Chris's tenant |
| M1 Headless agent, one node | 1, 2, 3, 4, 5 | M0 for live use | Runs a metadata environment on a schedule on Windows and Linux for 7 days unattended; a killed job retries; no lost rows with two concurrent jobs | Built; live soak pending |
| M2 Enterprise security | 2, 9 | M1 | Managed identity on an Azure VM; Key Vault by principal; audit row per job; signed agent release | Next |
| M3 Multiple workers | 7, 3, 4 | M1, M2 | Two workers on different OSes share one environment; a job runs exactly once | Planned |
| M4 Pipelines and observability | 6, 5 | M1 | A Fabric pipeline starts a local run and waits; logs reach Azure Monitor or OpenTelemetry | Planned |
| M5 Compatibility and cost | 8, 10 | M1 | Per-workload verdict with reasons; measured local cost vs Fabric CU | Planned |
| M6 Governance controls | 9 | M2, M3 | Per-job memory and concurrency limits; approvals for Test and Prod | Planned |

If time is short: M0, M1, M2, then M4 before M3, since most teams need pipeline hand-off before a second worker.

## Work items

Draft issues. FD-n ids let dependencies point at each other.

| Id | Milestone | Title | Depends on | Status |
| --- | --- | --- | --- | --- |
| FD-1 | M0 | Verify service principal access to Fabric and OneLake | Entra app, tenant setting | Open |
| FD-2 | M0 | Verify concurrent Delta commits and conditional requests on OneLake | FD-1 | Open |
| FD-3 | M1 | UI-free `Runtime` | none | Done |
| FD-4 | M1 | Credential providers (certificate, secret, managed identity) | FD-3 | Done |
| FD-5 | M1 | Agent home and `agent.toml` | FD-3 | Done |
| FD-6 | M1 | Durable job queue with retries and recovery | FD-5 | Done |
| FD-7 | M1 | Per-table write leases and commit retry | FD-2, FD-3 | Done (live check in FD-2) |
| FD-8 | M1 | `fabric-desktop agent` command | FD-4 to FD-7 | Done |
| FD-9 | M1 | JSON logs and run-row fields | FD-8 | Done |
| FD-10 | M1 | Linux CI and agent packaging | FD-8 | Done |
| FD-11 | M2 | Key Vault, audit and workload identity for the agent | FD-4 | Open |
| FD-12 | M3 | Shared job claims across workers | FD-6, FD-7, FD-11 | Open |
| FD-13 | M4 | Pipeline mailbox | FD-8 | Open |
| FD-14 | M5 | Workload compatibility analyzer | FD-3 | Open |
| FD-15 | M5 | Measured local cost and side-by-side comparison | FD-9 | Open |

### Acceptance criteria

**FD-1 Verify service principal access.** A script signs in with a certificate, lists workspaces, creates a lakehouse and writes a Delta table by GUID path. The result, with each role or setting that was needed, is recorded in `docs/live-verification.md`.

**FD-2 Verify concurrent commits and conditional requests.** Two processes write one OneLake table at once, 20 times; report conflicts raised, any lost commit, and the exception type. Confirm OneLake honours `If-None-Match: *` and `If-Match` on DFS create, flush and delete (the lease files depend on it).

**FD-3 UI-free Runtime.** Holds project, stores, credentials, run and alerts with no kernel or UI state; every RPC method still resolves and existing tests pass unchanged.

**FD-4 Credential providers.** Certificate, secret and managed identity; chosen by `agent.toml`; desktop unchanged; a clear error when sign-in fails.

**FD-5 Agent home and config.** `FABRIC_DESKTOP_HOME` and per-OS defaults; config validated at start with one line per problem; secrets refused.

**FD-6 Durable queue.** States queued, running, succeeded, failed, dead; backoff with a cap; a killed worker's job runs again once after restart.

**FD-7 Leases and commit retry.** Lease per table in LH_Meta, renewed while writing, released after, taken over once expired; entity write retries a lost commit race by re-reading and re-merging; two concurrent runs of one table keep every row.

**FD-8 Agent command.** Long-running loop with schedules, `concurrency`, clean shutdown on SIGTERM; `check`, `status` and `enqueue` commands; runs the existing `Engine`.

**FD-9 Logs and run fields.** JSON lines with job id, run id, worker, host; `run_log` gains `worker`, `host`, `attempt`, `job_id`; older rows still read.

**FD-10 Linux CI and packaging.** CI runs the tests on Ubuntu too; container image and systemd unit; Windows startup task; the agent never self-updates.

**FD-11 Key Vault, audit, workload identity.** Source secrets read by the agent's principal and never logged; every enqueue records who or what queued it; workload identity federation for Kubernetes and GitHub-hosted runners.

**FD-12 Shared job claims.** Workers register and heartbeat in LH_Meta; one worker claims a job via a conditional-create claim file; a stopped worker's claim expires and another runs the job; no job runs twice.

**FD-13 Pipeline mailbox.** Contract for `requests/<id>.json` and `results/<id>.json` plus a sample Fabric pipeline that writes a request and waits; duplicates ignored; malformed requests answered with the reason.

**FD-14 Compatibility analyzer.** Parses notebook Python (AST) and SQL (sqlglot), lists each unsupported Spark API or SQL feature with cell and line, and gives run here, run in Fabric or rewrite per workload; replaces `compute_check` keeping its output shape.

**FD-15 Measured cost.** Each local run records CPU-seconds and peak memory; a machine cost per hour turns that into money; the cost view compares local and Fabric runs of the same entity and labels estimates.

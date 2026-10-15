# Headless agent

The Fabric Desktop agent runs your metadata project on a server, unattended. It runs on Windows or Linux, signs in as a service principal or managed identity, and writes to OneLake exactly as the desktop app does. **Develop with Fabric Desktop. Execute on your infrastructure. Store and consume in Microsoft Fabric.**

The agent is the first milestone (M1) of the [enterprise local execution roadmap](enterprise-roadmap.md). It runs one machine's worth of work. Several agents sharing one queue comes in M3.

> **Status:** built and tested against local Delta tables on Linux and Windows CI. It has **not yet run against a live Fabric tenant.** Before you trust it with Prod, do the M0 checks in [Verify on your tenant](#verify-on-your-tenant).

## What it does

```
schedules in agent.toml ─┐
fabric-desktop agent enqueue ─┼─> durable queue (SQLite) ─> worker ─> metadata engine ─> OneLake
                          │        retries, crash recovery     │        (the same code the app runs)
                          │                                    └─> a write lease per table (LH_Meta)
                          └─ JSON logs, run_log rows with worker, host, attempt and job id
```

- **Same engine as the app.** A job runs an environment's entities through `fabric_desktop.meta.Engine`, in dependency order, with the same checks, quarantine, SCD, surrogate keys and run log. Runs show in the app's Monitor with batch ids starting `agent-`.
- **No one at the keyboard.** The agent signs in as an app identity. Nothing ever opens a sign-in window.
- **Durable queue.** Jobs are kept in `queue.sqlite3` in the agent's home folder. A job is `queued`, then `running`, then `succeeded`, `failed` or `dead`. A running job holds a lease that its worker renews; if the worker dies (crash, reboot, killed service), the lease runs out and the job is put back in the queue on the next start.
- **Retries that make sense.** Network errors, throttling (HTTP 429 and 5xx), a lost Delta commit race and a table busy with another writer are retried, waiting `backoff_seconds`, then twice that, up to `max_backoff_seconds`, for `max_attempts` tries in all. A retry runs only the tables that didn't finish. A job that still fails is `dead` and posts to Teams. A table that fails its checks is `failed` straight away, since running it again would fail the same way.
- **Safe concurrent writes.** The engine writes a table by reading it, merging new rows and committing the result. Before it starts an entity, the agent takes a **write lease** on that table: a small file in the environment's `LH_Meta` lakehouse under `Files/_fabric_desktop/leases/`, created with `If-None-Match: *`, renewed with `If-Match`, and deleted when done. A second writer waits for it (up to `table_lease_seconds`), then retries later. An abandoned lease expires after `table_lease_seconds`. If a commit still loses a race, the engine re-reads the table and merges again (up to 3 times).
- **Schedules.** `[[schedule]]` entries fire in the machine's local time: every N minutes, daily at set times, or weekly. A schedule that comes due while its last run is still queued or running doesn't add a second job. A schedule missed while the agent was down runs once when it starts.
- **Guard rails.** The agent runs only the environments listed in `agent.environments`, writes only to the workspaces in `agent.workspaces` when you set it, and never creates lakehouses: an environment is set up by promoting to it from the app.
- **Logs.** One JSON object per line on stdout (journald, `docker logs`) and in `<home>/logs/agent.log` (20 MB, 10 files kept). Every line has the worker and host; job lines add `job_id`, `env`, `attempt` and `source`. Every `run_log` row the agent writes has `worker`, `host`, `attempt` and `job_id`.

## Before you start

You need:

1. **A metadata project** in a folder the agent can read, usually a Git checkout of the project you develop in the app. The agent reads `layers.toml`, `environments.toml` and the entity files from it, so check out the version you promoted.
2. **Environments set up in Fabric.** Promote to each environment from the app first. The agent checks that the environment's deployed metadata version matches the project, as the app does.
3. **An identity for the agent.** One of:
   - **Service principal with a certificate** (any server). Recommended for on-premises.
   - **Managed identity** (Azure VM, or an Arc-enabled server). Recommended on Azure: there is no secret on the machine.
   - `user`: the desktop sign-in cached for the account the agent runs as. For trying it out only; the refresh token expires.

### Set up the identity

1. **Create an app registration** for the agent (keep it separate from the desktop app's registration): Entra admin center > **App registrations > New registration**, no redirect URI.
2. **Give it a certificate.** For example, on Linux:

   ```bash
   openssl req -x509 -newkey rsa:2048 -nodes -days 365 -subj "/CN=fabric-desktop-agent" \
     -keyout key.pem -out cert.pem
   cat key.pem cert.pem > /etc/fabric-desktop/agent.pem && chmod 600 /etc/fabric-desktop/agent.pem
   ```

   Upload `cert.pem` under **Certificates & secrets > Certificates**. A `.pfx` works too (`certificate_path` ending `.pfx`, with `certificate_password_env` naming the variable that holds its password). A client secret works (`client_secret_env`), but a certificate is safer.
3. **Let service principals use Fabric.** In the Fabric admin portal, **Tenant settings > Developer settings > Service principals can use Fabric APIs**: enable it for a security group, and add the agent's app (or managed identity) to that group.
4. **Add the identity to each workspace** it runs, as **Contributor** or higher: the environment's workspace (its layer lakehouses and `LH_Meta`) and, if the project uses surrogate keys, the Keys workspace (`LH_Keys`). It needs no Entra API permissions: an app identity gets what its workspace roles allow.

## Configure

Copy [deploy/agent.example.toml](../deploy/agent.example.toml) to:

| OS | Config | Home (queue, logs, caches) |
| --- | --- | --- |
| Linux | `/etc/fabric-desktop/agent.toml` | `/var/lib/fabric-desktop` |
| Windows | `C:\ProgramData\FabricDesktop\agent.toml` | `C:\ProgramData\FabricDesktop\agent` |

Set `FABRIC_DESKTOP_AGENT_CONFIG` or pass `--config` to use another file; set `FABRIC_DESKTOP_HOME` or `agent.home` for another home.

Secrets never go in `agent.toml`, and the agent refuses to start if one does. A certificate is a path; a client secret, certificate password or Teams webhook address is the **name of an environment variable** (`client_secret_env`, `certificate_password_env`, `teams_webhook_env`).

| Setting | Default | What it does |
| --- | --- | --- |
| `agent.name` | host name | The worker's name in logs and run rows |
| `agent.project` | required | The metadata project folder |
| `agent.environments` | required | The environments this agent may run |
| `agent.workspaces` | any | Workspace ids this agent may write to |
| `agent.concurrency` | 1 | Jobs at once |
| `agent.poll_seconds` | 5 | How often the loop looks for work |
| `agent.job_lease_seconds` | 300 | How long a silent worker keeps a job before it runs again |
| `agent.table_lease_seconds` | 120 | Write lease length, and how long to wait for a busy table |
| `auth.type` | required | `service_principal`, `managed_identity` or `user` |
| `retry.max_attempts` | 3 | Tries for retryable errors |
| `retry.backoff_seconds` | 60 | First wait; doubles each try |
| `retry.max_backoff_seconds` | 1800 | Longest wait |
| `alerts.teams_webhook_env` | none | Variable holding a Teams Workflows webhook address |

Then check it. This validates the file and the project and gets a token for Fabric and for OneLake:

```bash
fabric-desktop agent check
```

## Install

### Linux (systemd)

```bash
sudo useradd --system --home /var/lib/fabric-desktop fabric-desktop
sudo python3.12 -m venv /opt/fabric-desktop/venv
sudo /opt/fabric-desktop/venv/bin/pip install ./sidecar          # from a checkout of this repository
sudo mkdir -p /etc/fabric-desktop && sudo cp deploy/agent.example.toml /etc/fabric-desktop/agent.toml
sudo chown root:fabric-desktop /etc/fabric-desktop/agent.toml /etc/fabric-desktop/agent.pem
sudo chmod 640 /etc/fabric-desktop/agent.toml /etc/fabric-desktop/agent.pem
sudo cp deploy/linux/fabric-desktop-agent.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now fabric-desktop-agent
journalctl -u fabric-desktop-agent -f
```

Variables named by `*_env` settings go in `/etc/fabric-desktop/agent.env` (`NAME=value` lines, mode 600). The unit file is [deploy/linux/fabric-desktop-agent.service](../deploy/linux/fabric-desktop-agent.service).

### Linux (container)

```bash
docker build -f deploy/linux/Dockerfile -t fabric-desktop-agent .
docker run -d --name fd-agent --restart unless-stopped --stop-timeout 900 \
  -v /etc/fabric-desktop:/etc/fabric-desktop:ro \
  -v /srv/fabric/sales-metadata:/srv/fabric/sales-metadata:ro \
  -v fd-agent-home:/var/lib/fabric-desktop \
  fabric-desktop-agent
docker exec fd-agent fabric-desktop agent status
```

See [deploy/linux/Dockerfile](../deploy/linux/Dockerfile).

### Windows

From an elevated PowerShell in a checkout of this repository, with Python 3.12 installed:

```powershell
.\deploy\windows\install-agent.ps1 -Account 'CONTOSO\fd-agent$'
notepad C:\ProgramData\FabricDesktop\agent.toml
& "C:\Program Files\FabricDesktopAgent\venv\Scripts\fabric-desktop.exe" agent check
Start-ScheduledTask -TaskPath '\FabricDesktop\' -TaskName Agent
```

[The script](../deploy/windows/install-agent.ps1) installs the agent into a virtual environment and registers the Task Scheduler task `\FabricDesktop\Agent`. The task starts at boot with no one signed in and restarts every minute if it stops. A group managed service account (gMSA) is best, since there is no password to manage. Set `*_env` variables as machine environment variables.

## Operate

```bash
fabric-desktop agent status                          # queue counts, next schedule times, the last 20 jobs
fabric-desktop agent enqueue prod                     # run every Prod entity now
fabric-desktop agent enqueue prod silver.orders gold.fact_sales
fabric-desktop agent enqueue prod silver.orders --cascade --backfill full   # rebuild it and everything downstream
```

`enqueue` and `status` work while the service runs; they share the queue file. Stopping the service (SIGTERM, `systemctl stop`, `docker stop`, ending the task) stops taking new jobs and waits for running ones. A job cut off anyway runs again at the next start.

| Job state | Meaning | What to do |
| --- | --- | --- |
| `queued` | Waiting, or waiting to retry (`error` says why) | Nothing |
| `running` | A worker holds it | Nothing |
| `succeeded` | Every table ran | Nothing |
| `failed` | A table failed its checks, its config, or permissions | Read `error`, fix, enqueue again |
| `dead` | Retryable errors on every try | Read `error`; usually network, throttling or a table held by another writer |

## Verify on your tenant

Nothing here has run against a live Fabric tenant yet. Before Prod, check these on a Dev workspace (roadmap M0):

1. `fabric-desktop agent check` succeeds with your service principal (tokens for Fabric and OneLake).
2. `fabric-desktop agent enqueue dev` writes the Dev tables, and they show in the lakehouse after a refresh.
3. A lease file appears in `LH_Meta/Files/_fabric_desktop/leases/` while a table runs and is gone after. OneLake's support for `If-None-Match` and `If-Match` on these requests is the key thing to confirm.
4. Two agents, or the agent and the app, running the same table at once: one waits for the other and no rows are lost.

## Limits in this version

- One agent per queue. Two agents on different machines each keep their own queue; table leases still stop them from writing the same table at once, but nothing stops both from running the same schedule. Give each environment's schedules to one agent. A shared queue is roadmap M3.
- The desktop app doesn't take write leases yet. Don't run an environment from the app while the agent runs it.
- Notebooks and job files don't run in the agent yet; it runs metadata environments.
- Agent environments run on the machine, whatever their **Runs on** setting in the app.
- Lease expiry uses the holder's clock. Keep agent clocks in sync (NTP).
- The agent doesn't update itself. Install a new version with `pip install` and restart the service.

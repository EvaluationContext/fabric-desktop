"""The headless agent: config, unattended sign-in, the durable queue, table leases, and real runs."""

import json
import threading
from pathlib import Path

import pytest
import requests

from fabric_desktop import agent_config, cli
from fabric_desktop.agent import Agent
from fabric_desktop.credentials import ManagedIdentity, ServicePrincipal, default_scope, resource_of
from fabric_desktop.jobqueue import JobQueue
from fabric_desktop.leases import LeaseBusy, LeaseLost, Leases, LocalBackend, OneLakeBackend
from fabric_desktop.meta import LocalStore
from fabric_desktop.meta.templates import init_project
from fabric_desktop.retrying import is_retryable
from fabric_desktop.runtime import Runtime


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def config_text(project: Path, home: Path, extra: str = "") -> str:
    return f"""
[agent]
name = "etl-01"
home = {json.dumps(str(home))}
project = {json.dumps(str(project))}
environments = ["dev"]
poll_seconds = 0.1

[auth]
type = "user"

[retry]
max_attempts = 3
backoff_seconds = 10
{extra}
"""


@pytest.fixture
def setup(tmp_path):
    project = tmp_path / "proj"
    init_project(project, "3", "Chris", True)
    path = tmp_path / "agent.toml"
    path.write_text(config_text(project, tmp_path / "home"), encoding="utf-8")
    return agent_config.load(path)


def finish(agent: Agent) -> None:
    for f in list(agent._running.values()):
        f.result(timeout=120)


# ---- agent.toml

def test_config_reads_schedules_and_defaults(tmp_path, setup):
    cfg = setup
    assert cfg.name == "etl-01" and cfg.concurrency == 1 and cfg.max_attempts == 3
    path = tmp_path / "with-schedules.toml"
    path.write_text(config_text(cfg.project, tmp_path / "h", """
[[schedule]]
id = "dev-hourly"
env = "dev"
every_minutes = 60

[[schedule]]
id = "dev-morning"
env = "dev"
entities = ["gold.fact_sales"]
weekly = { days = ["Monday", "Friday"], times = ["06:00"] }
"""), encoding="utf-8")
    cfg = agent_config.load(path)
    assert [s.id for s in cfg.schedules] == ["dev-hourly", "dev-morning"]
    assert cfg.schedules[0].config["type"] == "Cron" and cfg.schedules[1].entities == ["gold.fact_sales"]


def test_config_refuses_secrets_and_lists_every_problem(tmp_path, setup):
    raw = {"agent": {"project": str(tmp_path / "nowhere"), "environments": ["prod", "qa"]},
           "auth": {"type": "service_principal", "client_secret": "s3cret"},
           "alerts": {"teams_webhook": "https://example.invalid/hook"},
           "schedule": [{"id": "x", "env": "prod", "daily": ["25:00"]}, {"id": "y", "env": "dev", "every_minutes": 5}]}
    with pytest.raises(agent_config.ConfigError) as err:
        agent_config.parse(raw)
    text = str(err.value)
    for expected in ("auth.client_secret: secrets don't go", "alerts.teams_webhook: secrets don't go",
                     "isn't a metadata project", "'qa' isn't one of", "tenant_id and client_id",
                     "schedule x: Times are HH:MM", "schedule y: dev isn't in agent.environments"):
        assert expected in text
    assert "s3cret" not in text


def test_webhook_comes_from_an_environment_variable(tmp_path, setup, monkeypatch):
    path = tmp_path / "a.toml"
    path.write_text(config_text(setup.project, tmp_path / "h", '[alerts]\nteams_webhook_env = "FD_HOOK"'), encoding="utf-8")
    monkeypatch.setenv("FD_HOOK", "https://example.invalid/hook")
    assert agent_config.load(path).teams_webhook == "https://example.invalid/hook"


# ---- sign-in without a person

def test_scopes_map_to_their_resource():
    assert resource_of("https://api.fabric.microsoft.com/Item.ReadWrite.All") == "https://api.fabric.microsoft.com"
    assert resource_of("https://storage.azure.com/user_impersonation") == "https://storage.azure.com"
    assert resource_of("https://analysis.windows.net/powerbi/api/Tenant.Read.All") == "https://analysis.windows.net/powerbi/api"
    assert default_scope(["https://api.fabric.microsoft.com/Workspace.ReadWrite.All",
                          "https://api.fabric.microsoft.com/Item.Execute.All"]) == "https://api.fabric.microsoft.com/.default"
    with pytest.raises(ValueError):
        default_scope(["https://api.fabric.microsoft.com/a", "https://storage.azure.com/b"])


class FakeApp:
    def __init__(self, result=None):
        self.asked, self.result = [], result

    def acquire_token_for_client(self, scopes=None, resource=None):
        self.asked.append(scopes or resource)
        return self.result or {"access_token": f"token-for-{scopes or resource}"}


def test_service_principal_asks_for_each_resource_default():
    app = FakeApp()
    sp = ServicePrincipal("tenant", "app-id", app=app)
    assert sp.fabric_token() == "token-for-['https://api.fabric.microsoft.com/.default']"
    sp.storage_token()
    assert app.asked[1] == ["https://storage.azure.com/.default"]
    assert sp.signed_in_tenant() == "tenant" and "app-id" in sp.signed_in_user()
    bad = ServicePrincipal("tenant", "app-id", app=FakeApp({"error": "invalid_client", "error_description": "no cert"}))
    with pytest.raises(RuntimeError, match="invalid_client"):
        bad.fabric_token()
    assert bad.silent_token(["https://api.fabric.microsoft.com/x"]) is None
    with pytest.raises(ValueError, match="certificate_path or client_secret_env"):
        ServicePrincipal("tenant", "app-id")


def test_service_principal_loads_a_pem_certificate(tmp_path):
    import datetime as dt

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    from fabric_desktop.credentials import _certificate
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "agent")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now).not_valid_after(now + dt.timedelta(days=1)).sign(key, hashes.SHA256()))
    pem = tmp_path / "agent.pem"
    pem.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()) + cert.public_bytes(serialization.Encoding.PEM))
    cred = _certificate(pem, None)
    assert cred["thumbprint"] == cert.fingerprint(hashes.SHA1()).hex() and "PRIVATE KEY" in cred["private_key"]


def test_managed_identity_asks_for_the_resource():
    client = FakeApp()
    mi = ManagedIdentity(client=client)
    mi.storage_token()
    assert client.asked == ["https://storage.azure.com"]


# ---- the queue

def test_queue_retries_with_backoff_then_gives_up(tmp_path):
    clock = Clock()
    q = JobQueue(tmp_path / "q.sqlite3", clock=clock, backoff_seconds=10, max_backoff_seconds=15)
    job = q.enqueue("meta", {"env": "dev"}, max_attempts=3)
    assert q.claim("w1", 60)["attempts"] == 1
    assert q.retry_later(job["id"], "w1", "timeout")["next_at"] == clock.t + 10
    assert q.claim("w1", 60) is None  # not due yet
    clock.t += 10
    assert q.claim("w1", 60)["attempts"] == 2
    assert q.retry_later(job["id"], "w1", "timeout")["next_at"] == clock.t + 15  # 20, capped at 15
    clock.t += 15
    q.claim("w1", 60)
    dead = q.retry_later(job["id"], "w1", "timeout")
    assert dead["state"] == "dead" and "Gave up after 3 attempts" in dead["error"]
    assert q.counts()["dead"] == 1


def test_queue_puts_back_a_job_whose_worker_died(tmp_path):
    clock = Clock()
    q = JobQueue(tmp_path / "q.sqlite3", clock=clock, backoff_seconds=0)
    job = q.enqueue("meta", {"env": "dev"})
    q.claim("crashed-worker", 60)
    clock.t += 30
    assert q.heartbeat(job["id"], "crashed-worker", 60)  # still alive: renewed to t+90
    clock.t += 59  # t+89: the renewed lease still holds
    assert q.recover() == []
    clock.t += 2  # t+91: it ran out
    assert q.recover() == [job["id"]]
    again = q.claim("w2", 60)
    assert again["id"] == job["id"] and again["attempts"] == 2 and again["owner"] == "w2"
    # The old worker comes back: it can't finish or renew a job it no longer holds.
    assert not q.heartbeat(job["id"], "crashed-worker", 60)
    assert not q.succeed(job["id"], "crashed-worker")
    assert q.succeed(job["id"], "w2", {"ok": 1}) and q.get(job["id"])["result"] == {"ok": 1}


def test_queue_survives_a_new_process_and_dedupes(tmp_path):
    q = JobQueue(tmp_path / "q.sqlite3")
    first = q.enqueue("meta", {"env": "dev"}, dedupe_key="schedule:a")
    q.close()
    q2 = JobQueue(tmp_path / "q.sqlite3")  # as after a restart
    again = q2.enqueue("meta", {"env": "dev"}, dedupe_key="schedule:a")
    assert again["id"] == first["id"] and again["deduplicated"]
    assert q2.counts()["queued"] == 1


# ---- table leases

def test_leases_block_a_second_writer_until_released_or_expired(tmp_path):
    clock = Clock()
    backend = LocalBackend(tmp_path / "leases")
    a = Leases(backend, "agent-a", ttl=60, wait=0, clock=clock)
    b = Leases(backend, "agent-b", ttl=60, wait=0, clock=clock)
    assert a.try_acquire("LH_Silver/dbo.orders")
    assert b.try_acquire("LH_Silver/dbo.orders") is None and b.holder("LH_Silver/dbo.orders") == "agent-a"
    assert b.try_acquire("LH_Silver/dbo.customers")  # other tables are free
    clock.t += 61  # agent-a died without releasing
    assert b.try_acquire("LH_Silver/dbo.orders")
    with pytest.raises(LeaseBusy, match="agent-b"):
        with a.hold("LH_Silver/dbo.orders"):
            pass


def test_hold_waits_for_the_lease_and_releases_it(tmp_path):
    backend = LocalBackend(tmp_path / "leases")
    order = []
    first = Leases(backend, "a", ttl=60, wait=30)
    second = Leases(backend, "b", ttl=60, wait=30, sleep=lambda s: order.append("waited") or threading.Event().wait(0.05))
    with first.hold("t"):
        done = []

        def run():
            with second.hold("t"):
                done.append("b wrote")
        t = threading.Thread(target=run)
        t.start()
        threading.Event().wait(0.2)
        order.append("a wrote")
    t.join(5)
    assert done == ["b wrote"] and "waited" in order
    assert backend.read("t") is None  # released


def test_a_lease_taken_over_mid_write_is_reported(tmp_path):
    backend = LocalBackend(tmp_path / "leases")
    leases = Leases(backend, "a", ttl=0.3, wait=0)
    with pytest.raises(LeaseLost):
        with leases.hold("t"):
            stored = backend.read("t")
            backend.replace("t", b'{"owner": "b", "expires": 9e12}', stored.etag)
            threading.Event().wait(0.3)


class FakeDfs:
    """Just enough of the OneLake DFS API for conditional lease files."""

    def __init__(self):
        self.files, self.calls, self.n = {}, [], 0

    def _resp(self, status, etag="", content=b""):
        r = requests.Response()
        r.status_code, r._content = status, content
        r.headers.update({"ETag": etag, "Last-Modified": "Thu, 15 Oct 2026 20:00:00 GMT"} if etag else {})
        return r

    def put(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("PUT", headers.get("If-None-Match") or headers.get("If-Match")))
        cur = self.files.get(url)
        if headers.get("If-None-Match") == "*" and cur:
            return self._resp(409)
        if headers.get("If-Match") and (not cur or cur[1] != headers["If-Match"]):
            return self._resp(412)
        self.n += 1
        self.files[url] = (b"", f'"{self.n}"')
        return self._resp(201, f'"{self.n}"')

    def patch(self, url, params=None, data=None, headers=None, timeout=None):
        body, etag = self.files[url]
        if params["action"] == "append":
            self.files[url] = (data, etag)
            return self._resp(202)
        if headers.get("If-Match") and headers["If-Match"] != etag:
            return self._resp(412)
        self.n += 1
        self.files[url] = (body, f'"{self.n}"')
        return self._resp(200, f'"{self.n}"')

    def get(self, url, headers=None, timeout=None):
        cur = self.files.get(url)
        return self._resp(200, cur[1], cur[0]) if cur else self._resp(404)

    def delete(self, url, headers=None, timeout=None):
        cur = self.files.get(url)
        if not cur or cur[1] != headers.get("If-Match"):
            return self._resp(412)
        del self.files[url]
        return self._resp(200)


def test_onelake_leases_use_conditional_requests():
    dfs = FakeDfs()
    backend = OneLakeBackend("ws", "meta-lh", lambda: "tok", session=dfs)
    a, b = Leases(backend, "a", wait=0), Leases(backend, "b", wait=0)
    etag = a.try_acquire("LH_Gold/dbo.fact_sales")
    url = "https://onelake.dfs.fabric.microsoft.com/ws/meta-lh/Files/_fabric_desktop/leases/LH_Gold/dbo.fact_sales.json"
    assert etag and json.loads(dfs.files[url][0])["owner"] == "a"
    assert ("PUT", "*") in dfs.calls
    assert b.try_acquire("LH_Gold/dbo.fact_sales") is None
    assert backend.replace("LH_Gold/dbo.fact_sales", b"{}", '"stale"') is None
    assert backend.delete("LH_Gold/dbo.fact_sales", etag) and url not in dfs.files


def test_retryable_errors():
    busy = requests.Response()
    busy.status_code = 429
    assert is_retryable(LeaseBusy("x")) and is_retryable(requests.ConnectionError())
    assert is_retryable(requests.HTTPError(response=busy))
    assert not is_retryable(ValueError("bad config")) and not is_retryable(KeyError("no entity"))


# ---- the agent, running the sample project for real

def test_agent_runs_a_queued_environment_with_run_details(setup):
    agent = Agent(setup)
    job = agent.enqueue("dev")
    agent.step()
    finish(agent)
    done = agent.queue.get(job["id"])
    assert done["state"] == "succeeded", done["error"]
    assert done["result"]["tables"] == 6 and done["result"]["failed"] == 0
    log = LocalStore(setup.project / ".local" / "dev", "dev").meta("run_log")
    assert set(log["worker"]) == {"etl-01"} and set(log["job_id"]) == {job["id"]}
    assert set(log["attempt"]) == {1} and all(b.startswith("agent-") for b in log["batch_id"])
    assert not list((setup.project / ".local" / "dev" / "_leases").glob("*.json"))  # every lease released


def test_agent_refuses_environments_it_was_not_given(setup):
    with pytest.raises(PermissionError, match="may run dev, not prod"):
        Agent(setup).enqueue("prod")
    with pytest.raises(PermissionError):
        Runtime(setup).run_environment("prod")


def test_a_busy_table_is_retried_for_just_the_tables_that_did_not_run(setup):
    import dataclasses
    Leases(LocalBackend(setup.project / ".local" / "dev" / "_leases"), "someone-else", ttl=3600) \
        .try_acquire("LH_Gold/dbo.fact_sales")
    agent = Agent(dataclasses.replace(setup, table_lease_seconds=0.2))  # wait 0.2 s for a busy table
    job = agent.enqueue("dev")
    agent.step()
    finish(agent)
    again = agent.queue.get(job["id"])
    assert again["state"] == "queued" and "LeaseBusy" in again["error"] and "someone-else" in again["error"]
    assert again["payload"]["entities"] == ["gold.fact_sales"]


def test_a_failed_check_fails_the_job_without_retrying(setup, tmp_path):
    for f in (setup.project / ".local" / "dev" / "LH_Bronze" / "Files").rglob("*.csv"):
        f.unlink()  # no landing files: bronze fails, and so does everything after it
    agent = Agent(setup)
    job = agent.enqueue("dev", ["bronze.orders"])
    agent.step()
    finish(agent)
    assert agent.queue.get(job["id"])["state"] == "failed"


def test_a_job_from_a_crashed_agent_runs_again_after_restart(setup):
    clock = Clock()
    q = JobQueue(setup.home / "queue.sqlite3", clock=clock, backoff_seconds=0)
    job = q.enqueue("meta", {"env": "dev", "entities": ["bronze.orders"]})
    q.claim("etl-01-before-crash", 300)
    clock.t += 301
    agent = Agent(setup, queue=q, clock=clock)
    agent.step()  # recover, then claim it again
    finish(agent)
    done = q.get(job["id"])
    assert done["state"] == "succeeded" and done["attempts"] == 2 and done["owner"] == "etl-01"


def test_schedules_fire_once_when_due_and_do_not_pile_up(tmp_path, setup):
    path = tmp_path / "s.toml"
    path.write_text(config_text(setup.project, tmp_path / "h2", '[[schedule]]\nid = "every-15"\nenv = "dev"\nevery_minutes = 15'),
                    encoding="utf-8")
    cfg = agent_config.load(path)
    clock = Clock()
    agent = Agent(cfg, queue=JobQueue(cfg.home / "q.sqlite3", clock=clock), clock=clock)
    assert agent.tick_schedules() == []  # the first tick only works out the next time
    nxt = agent.queue.schedule_next("every-15")
    assert nxt > clock.t
    clock.t = nxt + 1
    assert agent.tick_schedules() == ["every-15"]
    clock.t = agent.queue.schedule_next("every-15") + 1
    agent.tick_schedules()  # due again while the first is still queued: no second job
    assert agent.queue.counts()["queued"] == 1


def test_cli_check_enqueue_and_status(setup, capsys):
    assert cli._agent(cli_args(setup, "check")) == 0
    checked = json.loads(capsys.readouterr().out)
    assert checked["entities"] == 6 and checked["environments"]["dev"]["target"] == "local"
    assert cli._agent(cli_args(setup, "enqueue", env="dev", entities=["silver.orders"])) == 0
    queued = json.loads(capsys.readouterr().out)
    assert queued["state"] == "queued" and queued["payload"]["entities"] == ["silver.orders"]
    assert cli._agent(cli_args(setup, "status")) == 0
    assert json.loads(capsys.readouterr().out)["counts"]["queued"] == 1


def test_cli_reports_a_bad_config(tmp_path, capsys):
    bad = tmp_path / "bad.toml"
    bad.write_text("[agent]\n", encoding="utf-8")
    assert cli._agent(cli_args(None, "status", config=str(bad))) == 2
    assert "agent.project" in capsys.readouterr().out


def cli_args(cfg, command, config=None, env="", entities=None):
    import argparse
    return argparse.Namespace(config=config or str(cfg.path), agent_command=command, env=env,
                              entities=entities or [], backfill="", cascade=False)


def test_a_lost_commit_race_merges_again_and_succeeds(setup, monkeypatch):
    from deltalake.exceptions import CommitFailedError

    from fabric_desktop.meta import Engine, Project
    project = Project(setup.project)
    store = LocalStore(setup.project / ".local" / "dev", "dev")
    real, calls = store.write, []

    def flaky(lakehouse, schema, table, df, mode="overwrite", metadata=None):
        calls.append(table)
        if table == "orders" and lakehouse == "LH_Bronze" and calls.count("orders") == 1:
            raise CommitFailedError("another writer committed version 1 first")
        return real(lakehouse, schema, table, df, mode, metadata)
    monkeypatch.setattr(store, "write", flaky)
    monkeypatch.setattr("fabric_desktop.meta.engine.time.sleep", lambda s: None)
    res = Engine(project, store).run("bronze.orders")
    assert res.status == "ok" and calls.count("orders") == 2

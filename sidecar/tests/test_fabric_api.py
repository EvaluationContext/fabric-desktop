import pytest

from fabric_desktop.fabric_api import FabricClient


class Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body or {}
        self.headers = headers or {}
        self.content = b"x" if body is not None else b""
        self.text = str(body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs.get("json")))
        return self.responses.pop(0)


def client(responses):
    session = FakeSession(responses)
    return FabricClient(lambda: "tok", session=session, sleep=lambda s: None), session


def test_create_lakehouse_enables_schemas_and_follows_lro():
    loc = "https://api.fabric.microsoft.com/v1/operations/op1"
    c, s = client([
        Resp(202, headers={"Location": loc, "Retry-After": "1"}),
        Resp(200, {"status": "Running"}),
        Resp(200, {"status": "Succeeded"}),
        Resp(200, {"id": "lh1", "displayName": "Sales"}),
    ])
    assert c.create_lakehouse("ws1", "Sales")["id"] == "lh1"
    method, url, body = s.calls[0]
    assert (method, url) == ("POST", "https://api.fabric.microsoft.com/v1/workspaces/ws1/lakehouses")
    assert body == {"displayName": "Sales", "creationPayload": {"enableSchemas": True}}
    assert s.calls[-1][1] == f"{loc}/result"


def test_list_follows_continuation_and_retries_429():
    c, s = client([
        Resp(429, {}, headers={"Retry-After": "0"}),
        Resp(200, {"value": [{"id": "a"}], "continuationUri": "https://api.fabric.microsoft.com/v1/workspaces?ct=2"}),
        Resp(200, {"value": [{"id": "b"}]}),
    ])
    assert [w["id"] for w in c.list_workspaces()] == ["a", "b"]
    assert len(s.calls) == 3


def test_capacity_not_active_gets_a_plain_message():
    c, _ = client([Resp(404, {"errorCode": "CapacityNotActive", "message": "Capacity X is not active"})])
    try:
        c.list_lakehouses("ws1")
    except Exception as exc:
        assert exc.code == "CapacityNotActive"
        assert "capacity is paused" in str(exc)
    else:
        raise AssertionError("expected FabricError")


def test_scan_endorsements_reads_every_item_list():
    c, s = client([
        Resp(202, {"id": "scan1"}),
        Resp(200, {"status": "Running"}),
        Resp(200, {"status": "Succeeded"}),
        Resp(200, {"workspaces": [{"id": "ws1", "name": "Sales", "Lakehouse": [
            {"id": "lh1", "endorsementDetails": {"endorsement": "Certified"}},
            {"id": "lh2", "endorsementDetails": {"endorsement": "None"}},
            {"id": "lh3"},
        ], "reports": [{"id": "r1", "endorsementDetails": {"endorsement": "Promoted"}}], "users": "x"}]}),
    ])
    assert c.scan_endorsements(["ws1"]) == {"lh1": "Certified", "r1": "Promoted"}
    assert s.calls[0] == ("POST", "https://api.powerbi.com/v1.0/myorg/admin/workspaces/getInfo", {"workspaces": ["ws1"]})
    assert s.calls[-1][1].endswith("/workspaces/scanResult/scan1")


def test_create_workspace_puts_it_on_a_capacity_and_explains_a_taken_name():
    c, s = client([Resp(201, {"id": "ws9", "displayName": "Sales - Dev"})])
    assert c.create_workspace("Sales - Dev", "cap1", "Dev")["id"] == "ws9"
    assert s.calls[0] == ("POST", "https://api.fabric.microsoft.com/v1/workspaces",
                          {"displayName": "Sales - Dev", "capacityId": "cap1", "description": "Dev"})
    c, s = client([Resp(409, {"errorCode": "WorkspaceNameAlreadyExists"})])
    with pytest.raises(Exception, match="already exists. Pick it from the list"):
        c.create_workspace("Sales - Dev")
    assert s.calls[0][2] == {"displayName": "Sales - Dev"}

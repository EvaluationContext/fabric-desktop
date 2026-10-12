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

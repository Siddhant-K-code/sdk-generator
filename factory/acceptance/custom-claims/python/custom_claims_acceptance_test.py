"""Black-box acceptance tests for custom token-request fields (customClaims).

Copied into an SDK checkout by run.sh. Contract: ../contract.md.
"""

import asyncio
import copy
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from openfga_sdk.client import OpenFgaClient
from openfga_sdk.client.configuration import ClientConfiguration
from openfga_sdk.client.models import ClientCheckRequest
from openfga_sdk.configuration import RetryParams
from openfga_sdk.credentials import CredentialConfiguration, Credentials
from openfga_sdk.sync.client.client import OpenFgaClient as SyncOpenFgaClient

TOKEN = "factory-synthetic-oauth-token"
STORE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
MODEL_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAW"
ORG = "factory team/one & two"

RESERVED = {
    "organization": ORG,
    "client_id": "wrong",
    "client_secret": "wrong",
    "grant_type": "wrong",
    "audience": "wrong",
}
CUSTOM = {"organization": ORG, "scope": "read:tuples"}

# case id -> (custom claims or None, scopes or unset, audience configured, extra form fields)
UNSET = object()
CASES = {
    "default": (None, UNSET, True, {}),
    "empty": ({}, UNSET, True, {}),
    "custom": (CUSTOM, UNSET, True, {"organization": ORG, "scope": "read:tuples"}),
    "reserved": (RESERVED, UNSET, True, {"organization": ORG}),
    "denied": (None, UNSET, True, {}),
    "scopes": (
        CUSTOM,
        "write:tuples list:objects",
        True,
        {"organization": ORG, "scope": "write:tuples list:objects"},
    ),
    "empty-scopes": (CUSTOM, "", True, {"organization": ORG, "scope": "read:tuples"}),
    "no-audience": (RESERVED, UNSET, False, {"organization": ORG}),
}


class Fixture(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, deny_token):
        super().__init__(("127.0.0.1", 0), Handler)
        self.deny_token = deny_token
        self.token_requests = []
        self.api_requests = []
        self.problems = []
        self.lock = threading.Lock()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_address[1]}"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def reply(self, status, value):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode()
        path = urllib.parse.urlsplit(self.path).path
        with self.server.lock:
            if path == "/oauth/token":
                pairs = urllib.parse.parse_qsl(body, keep_blank_values=True)
                form = dict(pairs)
                if len(form) != len(pairs):
                    self.server.problems.append(f"duplicate token form key: {body}")
                self.server.token_requests.append(
                    {
                        "form": form,
                        "content_type": self.headers.get("Content-Type", ""),
                    }
                )
                if self.server.deny_token:
                    return self.reply(401, {"error": "invalid_client"})
                return self.reply(
                    200,
                    {"access_token": TOKEN, "expires_in": 3600, "token_type": "Bearer"},
                )
            if path == f"/stores/{STORE_ID}/check":
                user = json.loads(body)["tuple_key"]["user"]
                bearer = self.headers.get("Authorization") == f"Bearer {TOKEN}"
                self.server.api_requests.append({"user": user, "bearer_valid": bearer})
                if not bearer:
                    return self.reply(401, {"code": "unauthenticated"})
                return self.reply(200, {"allowed": user == "user:anne"})
            self.server.problems.append(f"unexpected route {self.path}")
            return self.reply(404, {"code": "not_found"})

    def do_GET(self):
        with self.server.lock:
            self.server.problems.append(f"unexpected GET {self.path}")
        self.reply(404, {"code": "not_found"})


def build_configuration(case, server_url):
    claims, scopes, audience, _ = CASES[case]
    params = {
        "client_id": "factory-client",
        "client_secret": "factory-secret",
        "api_issuer": f"{server_url}/oauth/token",
        "api_audience": "factory-audience" if audience else None,
    }
    if scopes is not UNSET:
        params["scopes"] = scopes
    if claims is not None:
        params["custom_claims"] = claims
    try:
        credential = CredentialConfiguration(**params)
    except TypeError as error:
        pytest.fail(
            f"option missing: CredentialConfiguration(custom_claims=...) ({error})"
        )
    return ClientConfiguration(
        api_url=server_url,
        store_id=STORE_ID,
        authorization_model_id=MODEL_ID,
        credentials=Credentials(method="client_credentials", configuration=credential),
        retry_params=RetryParams(max_retry=0),
        timeout_millisec=2000,
    )


def observe(call):
    try:
        response = call()
        return {"kind": "value", "allowed": response.allowed}
    except Exception as error:  # noqa: BLE001 - record any SDK error shape
        return {
            "kind": "error",
            "status": getattr(error, "status", None),
            "type": type(error).__name__,
        }


def request(user):
    return ClientCheckRequest(user=user, relation="viewer", object="document:factory")


async def run_async(config, denied):
    async with OpenFgaClient(config) as client:
        results = [
            await observe_async(lambda: client.check(request("user:anne")))
        ]
        if not denied:
            results.append(
                await observe_async(lambda: client.check(request("user:bob")))
            )
        return results


async def observe_async(call):
    try:
        response = await call()
        return {"kind": "value", "allowed": response.allowed}
    except Exception as error:  # noqa: BLE001
        return {
            "kind": "error",
            "status": getattr(error, "status", None),
            "type": type(error).__name__,
        }


def run_sync(config, denied):
    with SyncOpenFgaClient(config) as client:
        results = [observe(lambda: client.check(request("user:anne")))]
        if not denied:
            results.append(observe(lambda: client.check(request("user:bob"))))
        return results


def expected_form(case):
    _, scopes, audience, extra = CASES[case]
    form = {
        "client_id": "factory-client",
        "client_secret": "factory-secret",
        "grant_type": "client_credentials",
    }
    if audience:
        form["audience"] = "factory-audience"
    if scopes not in (UNSET, ""):
        form["scope"] = scopes
    form.update(extra)
    return form


@pytest.mark.parametrize("api", ["async", "sync"])
@pytest.mark.parametrize("case", list(CASES))
def test_custom_claims(case, api):
    denied = case == "denied"
    claims = CASES[case][0]
    snapshot = copy.deepcopy(claims)
    server = Fixture(deny_token=denied)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = build_configuration(case, server.url)
        if api == "async":
            results = asyncio.run(run_async(config, denied))
        else:
            results = run_sync(config, denied)
    finally:
        server.shutdown()
        server.server_close()

    assert server.problems == []
    assert claims == snapshot, "caller's custom claims map was mutated"

    if denied:
        assert results[0]["kind"] == "error", results
        assert results[0]["status"] == 401, results
        assert server.api_requests == []
        assert len(server.token_requests) >= 1
    else:
        assert results == [
            {"kind": "value", "allowed": True},
            {"kind": "value", "allowed": False},
        ]
        assert server.api_requests == [
            {"user": "user:anne", "bearer_valid": True},
            {"user": "user:bob", "bearer_valid": True},
        ]
        assert len(server.token_requests) == 1, "token must be exchanged once and cached"

    for token_request in server.token_requests:
        assert token_request["content_type"].startswith(
            "application/x-www-form-urlencoded"
        )
        assert token_request["form"] == expected_form(case)

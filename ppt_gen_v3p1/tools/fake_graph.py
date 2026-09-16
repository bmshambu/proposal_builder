"""A recorded Microsoft Graph, for testing the graph backend without a tenant.

This is a `transport` in the sense `engine/graph.py` means it: one callable
that takes a request and returns a `Response`. Everything above it — msal's
token request, the >4 MB switch, the 302 to a pre-authed URL, the retries, and
the delete in the `finally` — is the real code, running for real. Only the wire
is invented.

What it is *not* is a mock of our own module. Nothing here knows about
`Client`; it answers URLs the way the service does, which is why it can catch
us sending a bearer token somewhere it should not go.

    fake = FakeGraph(pdf=open("templates/demo/library.pdf", "rb").read())
    graph.TRANSPORT = fake
    ...
    assert fake.deleted                # the copy did not stay in SharePoint

Failures are asked for by name, so a check reads as the thing it is proving:

    FakeGraph(fail="convert")          # conversion 500s mid-render
    FakeGraph(fail="delete")           # the copy cannot be removed
    FakeGraph(fail="purge")            # it is removed but stays in the bin
    FakeGraph(throttle=2)              # two 429s, then success
    FakeGraph(offline=True)            # nothing answers at all
"""
import json
import os
import sys
from urllib.parse import unquote

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from engine.graph import GraphError, Response                     # noqa: E402

TOKEN = "fake-access-token-not-a-real-one"
SITE = "fake-site-guid"
ITEM = "01FAKEITEMID"
UPLOAD_URL = "https://fake.sharepoint.example/_api/uploadSession?guid=abc123"
DOWNLOAD_URL = ("https://fake.sharepoint.example/_layouts/15/download.aspx"
                "?tempauth=fake-pre-authed-credential")


def _bin_id(name):
    return "bin-" + name


class FakeGraph(object):
    """Answers like Graph does. Records everything for the checks to read."""

    def __init__(self, pdf=b"%PDF-1.4 fake\n%%EOF\n", fail=None, throttle=0,
                 retry_after=1, offline=False, item=ITEM):
        self.pdf = pdf
        self.fail = fail
        self.throttle = int(throttle)
        self.retry_after = retry_after
        self.offline = offline
        self.item = item
        self.bin = []               # names SharePoint would still be holding
        self._last_name = None
        self.requests = []          # (method, url, headers, len(body))
        self.chunks = []            # Content-Range of every session chunk
        self.slept = []             # seconds the client was asked to wait

    # ------------------------------------------------------------ recording
    def __call__(self, method, url, headers=None, body=None, timeout=None):
        headers = headers or {}
        self.requests.append((method, url, dict(headers),
                              len(body or b"")))
        if self.offline:
            # What a downed network looks like from inside graph.py.
            raise GraphError("could not reach Graph: URLError: "
                             "<urlopen error [Errno 11001] getaddrinfo failed>")

        if "login.microsoftonline.com" in url:
            return self._entra(url)
        if self.throttle > 0 and "graph.microsoft.com" in url:
            self.throttle -= 1
            return Response(429, {"Retry-After": str(self.retry_after)},
                            b'{"error":{"code":"activityLimitReached",'
                            b'"message":"Too many requests"}}')
        if "/recycleBin/items" in url:
            return self._bin(method, url, body)
        if "sharepointIds" in url:
            return Response(200, {}, json.dumps({
                "sharepointIds": {"siteId": SITE}}).encode())
        if method == "DELETE":
            return self._delete()
        if url.startswith(UPLOAD_URL.split("?")[0]):
            return self._chunk(headers, body)
        if url.startswith(DOWNLOAD_URL.split("?")[0]):
            return self._download()
        if "createUploadSession" in url:
            self._last_name = unquote(url.split("/")[-2].rstrip(":"))
            return self._session()
        if method == "PUT" and url.endswith(":/content"):
            self._last_name = unquote(url.split("/")[-2].rstrip(":"))
            return self._simple_upload()
        if "format=pdf" in url:
            return self._convert()
        return Response(404, {}, b'{"error":{"code":"itemNotFound",'
                                 b'"message":"no such route in the fake"}}')

    # ------------------------------------------------------------ the routes
    def _entra(self, url):
        """Entra answers more than a token. msal fetches the tenant's OpenID
        configuration first, and on some paths the instance metadata too, so a
        cold client costs two round trips before a single byte reaches Graph.
        That is a finding about how long a first preview takes, not a detail -
        and it is why `graph.py` keeps the msal app between renders."""
        if ".well-known/openid-configuration" in url:
            issuer = "https://login.microsoftonline.com/fake-tenant/v2.0"
            return Response(200, {}, json.dumps({
                "issuer": issuer,
                "authorization_endpoint": issuer + "/authorize",
                "token_endpoint": issuer + "/token",
                "device_authorization_endpoint": issuer + "/devicecode",
                "response_modes_supported": ["query", "fragment", "form_post"],
                "grant_types_supported": ["client_credentials"],
            }).encode())
        if "discovery/instance" in url:
            # Not asked for on every msal path, but asked for on some: which
            # hostnames are aliases of this one, before it will trust a token
            # cached against any of them.
            return Response(200, {}, json.dumps({
                "tenant_discovery_endpoint":
                    "https://login.microsoftonline.com/fake-tenant/v2.0"
                    "/.well-known/openid-configuration",
                "api-version": "1.1",
                "metadata": [{
                    "preferred_network": "login.microsoftonline.com",
                    "preferred_cache": "login.windows.net",
                    "aliases": ["login.microsoftonline.com",
                                "login.windows.net", "login.microsoft.com",
                                "sts.windows.net"]}]}).encode())
        return self._token()

    def _token(self):
        if self.fail == "token":
            return Response(401, {}, json.dumps({
                "error": "invalid_client",
                "error_description": "AADSTS7000215: Invalid client secret "
                                     "provided.\r\nTrace ID: fake"}).encode())
        return Response(200, {"Content-Type": "application/json"},
                        json.dumps({"token_type": "Bearer", "expires_in": 3599,
                                    "ext_expires_in": 3599,
                                    "access_token": TOKEN}).encode())

    def _bin(self, method, url, body):
        """The site recycle bin, which is where a DELETE actually puts things.

        Modelled because it was not, once: every check passed while twenty-two
        real decks sat in a real bin, because nothing here knew the bin
        existed.
        """
        if method == "POST":
            if self.fail == "purge":
                return Response(500, {}, json.dumps({"error": {
                    "code": "generalException",
                    "message": "The recycle bin could not be emptied."}
                }).encode())
            ids = set(json.loads((body or b"{}").decode("utf-8")).get("ids")
                      or [])
            self.bin = [n for n in self.bin if _bin_id(n) not in ids]
            return Response(204, {}, b"")
        want = None
        if "$filter=" in url:
            want = unquote(url.split("name+eq+'")[-1].split("'")[0])
        names = [n for n in self.bin if want is None or n == want]
        return Response(200, {}, json.dumps({"value": [
            {"name": n, "id": _bin_id(n), "size": 1024} for n in names]
        }).encode())

    def _simple_upload(self):
        if self.fail == "upload":
            return Response(403, {}, json.dumps({"error": {
                "code": "accessDenied",
                "message": "The app is not granted access to this site. "
                           "Sites.Selected requires a per-site grant."}
            }).encode())
        return self._item()

    def _session(self):
        return Response(200, {}, json.dumps({
            "uploadUrl": UPLOAD_URL,
            "expirationDateTime": "2026-01-01T00:00:00Z"}).encode())

    def _chunk(self, headers, body):
        rng = headers.get("Content-Range") or headers.get("content-range") or ""
        self.chunks.append(rng)
        # "bytes 0-4914175/5242880" — the last chunk is the one that finishes
        # at size-1, and only that one comes back with the item.
        try:
            span, total = rng.split()[1].split("/")
            done = int(span.split("-")[1]) + 1 >= int(total)
        except (IndexError, ValueError):
            done = False
        return self._item() if done else Response(202, {}, b"{}")

    def _convert(self):
        if self.fail == "convert":
            return Response(500, {}, json.dumps({"error": {
                "code": "generalException",
                "message": "An unexpected error occurred during conversion."}
            }).encode())
        return Response(302, {"Location": DOWNLOAD_URL}, b"")

    def _download(self):
        return Response(200, {"Content-Type": "application/pdf"}, self.pdf)

    def _delete(self):
        # SharePoint does not delete on DELETE. It recycles.
        if self._last_name and self.fail != "delete":
            self.bin.append(self._last_name)
        if self.fail == "delete":
            return Response(423, {}, json.dumps({"error": {
                "code": "resourceLocked",
                "message": "The resource is locked and cannot be deleted."}
            }).encode())
        return Response(204, {}, b"")

    def _item(self):
        return Response(201, {"Content-Type": "application/json"},
                        json.dumps({"id": self.item, "name": "deck.pptx",
                                    "size": 1024}).encode())

    # ------------------------------------------------------------ what to ask
    @property
    def deleted(self):
        """Did the uploaded copy get removed? The question §6 cares about."""
        return any(m == "DELETE" and self.item in u for m, u, _h, _n
                   in self.requests)

    @property
    def uploaded(self):
        return any(m in ("PUT", "POST")
                   and (u.endswith(":/content") or "createUploadSession" in u)
                   for m, u, _h, _n in self.requests)

    @property
    def used_session(self):
        return any("createUploadSession" in u for _m, u, _h, _n
                   in self.requests)

    def calls(self, method=None, contains=None):
        return [(m, u) for m, u, _h, _n in self.requests
                if (method is None or m == method)
                and (contains is None or contains in u)]

    def auth_leaked_to(self, fragment):
        """Was our bearer token sent to a URL that already carries its own
        credential? Sending it there would hand a token to a host we did not
        choose, so the checks assert this is never true."""
        return any(fragment in u and "Authorization" in h
                   for _m, u, h, _n in self.requests)

    def body_bytes(self, method="PUT"):
        return sum(n for m, _u, _h, n in self.requests if m == method)


def no_sleeping(fake):
    """Stop the retry waits from actually costing the check suite seconds, but
    record them: how long we were willing to wait is the interesting part."""
    from engine import graph as graphmod

    real = graphmod._sleep
    graphmod._sleep = fake.slept.append

    def undo():
        graphmod._sleep = real

    return undo

"""Office Online's own PDF converter, reached through Microsoft Graph.

`engine/render.py` says why a deck is converted at all. This module is about
the awkward part of doing it through Graph: **`format=pdf` is an operation on a
file that already lives in a drive.** There is no endpoint that takes bytes and
gives back a PDF, so a deck that exists only on this machine has to be put into
SharePoint, converted there, and taken out again.

That makes the delete the most important thing in this file, not the
conversion. A confidential proposal left behind in SharePoint because a render
failed halfway is the worst outcome this experiment can produce, so `to_pdf`
deletes in a `finally` and records what it did in `journal` - which is what the
checks read to prove the item is gone after a success *and* after a failure.

**And a DELETE is not a deletion.** SharePoint moves the item to the site
recycle bin, where it sits for the retention period - 93 days by default -
fully readable by anyone who can open the bin. This was found by looking, after
twenty-two "deleted" decks turned out to still be there. So `delete` is two
operations: remove the item, then purge it from the recycle bin. A purge that
fails is raised, exactly like a delete that fails.

The purge has no v1.0 endpoint. `POST /beta/sites/{site}/recycleBin/items
/delete` is the only way to do it through Graph, and being on beta it can
change without notice - if it ever stops working, this backend must fail loudly
rather than quietly go back to leaving decks in the bin.

Everything, the token request included, goes through one `transport` callable.
That is what lets the retries, the >4 MB switch, the redirect and the delete be
exercised without a tenant: `tools/fake_graph.py` replays recorded responses
through this same code.

Nothing here is ever logged, and errors name no URL. A bearer token, the client
secret and the pre-authed URL Graph redirects to are all credentials, and
`RenderError` text ends up on somebody's screen.

Configuration is environment only:

    GRAPH_TENANT_ID      the directory the app registration lives in
    GRAPH_CLIENT_ID      the app registration
    GRAPH_CLIENT_SECRET  a certificate would be better; a secret is fine here
    GRAPH_DRIVE_ID       the one document library this is allowed to touch
    GRAPH_FOLDER         a folder within it, default `pptgen-render`, so a
                         human can see at a glance whether anything was left
"""
import json
import os
import threading
import time
import uuid

RESOURCE = "https://graph.microsoft.com"
SCOPE = RESOURCE + "/.default"
V1 = RESOURCE + "/v1.0"
BETA = RESOURCE + "/beta"          # only for the recycle bin; see purge()

ENV = ("GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET",
       "GRAPH_DRIVE_ID")
FOLDER = "pptgen-render"

# Graph's documented ceiling for a plain PUT. Over it the upload has to be a
# session, and the session's chunks must be a multiple of 320 KiB - that is not
# a suggestion, it rejects anything else.
SIMPLE_MAX = 4 * 1024 * 1024
CHUNK = 15 * 320 * 1024

RETRIES = 4              # on 429/503 only; a 4xx is an answer, not a blip
RETRY_CAP = 30           # seconds. A Retry-After longer than this is a reason
                         # to fall back to another renderer, not to hold a
                         # page load open waiting for Graph to calm down.

TRANSPORT = None         # the checks put a fake here; None means the wire

# msal fetches the tenant's OpenID configuration before it will ask for a
# token, so a cold client costs two round trips to Entra before the first byte
# goes to Graph. Keeping the app alive across renders keeps msal's own token
# cache alive with it, and the second preview pays for neither.
#
# The transport is part of the key so that a client built on a fake can never
# be handed an app - or a token - that belongs to the real service.
_APPS = {}                          # (tenant, client id, transport) -> msal app
_APPS_GUARD = threading.Lock()

# The recycle bin is addressed by site, and we are configured with a drive.
# Discovered once rather than made a fifth environment variable: one more thing
# to set wrong is worse than one cached request.
_SITES = {}                                    # (drive, transport) -> site id

PPTX_TYPE = ("application/vnd.openxmlformats-officedocument"
             ".presentationml.presentation")


class GraphError(RuntimeError):
    """Graph could not do this. `render.py` turns it into a RenderError."""


def _sleep(seconds):
    """Its own function so a check can watch how long we were willing to wait
    for a throttled tenant without the suite actually waiting it."""
    time.sleep(seconds)


# ---------------------------------------------------------------- the seam
class Response(object):
    """What a transport returns. Small on purpose: this is the whole contract
    a fake has to honour."""

    __slots__ = ("status", "headers", "body")

    def __init__(self, status, headers=None, body=b""):
        self.status = int(status)
        # Header case is guaranteed by nothing, and `Retry-After` arriving once
        # as `retry-after` is the kind of thing that costs an afternoon.
        self.headers = {str(k).lower(): v for k, v in (headers or {}).items()}
        self.body = body or b""

    def json(self):
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise GraphError("Graph answered %d with a body that is not JSON"
                             % self.status)


def _wire(method, url, headers=None, body=None, timeout=300):
    """The real transport: one HTTP request, redirects not followed.

    Redirects are ours to follow because the 302 from `format=pdf` points at a
    pre-authed URL. Letting urllib follow it would send our bearer token on to
    a host we did not choose.
    """
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, data=body, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)

    class _NoFollow(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    opener = urllib.request.build_opener(_NoFollow)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return Response(resp.status, dict(resp.headers), resp.read())
    except urllib.error.HTTPError as exc:      # 3xx and 4xx arrive as errors
        return Response(exc.code, dict(exc.headers or {}), exc.read())
    except Exception as exc:
        # The network being down has to degrade to another backend, so it is a
        # GraphError like any other rather than something that escapes.
        raise GraphError("could not reach Graph: %s: %s"
                         % (type(exc).__name__, exc))


def transport():
    return TRANSPORT or _wire


# ---------------------------------------------------------------- msal glue
class _MsalHttp(object):
    """msal talking through our transport, so the token request is faked along
    with everything else instead of being the one untested step."""

    def __init__(self, send):
        self._send = send

    def _call(self, method, url, params=None, data=None, headers=None,
              **kwargs):
        from urllib.parse import urlencode

        if params:
            url = "%s%s%s" % (url, "&" if "?" in url else "?",
                              urlencode(params))
        body = None
        head = dict(headers or {})
        if data is not None:
            body = urlencode(data).encode("utf-8")
            head.setdefault("Content-Type",
                            "application/x-www-form-urlencoded")
        return _MsalResponse(self._send(method, url, head, body, 300))

    def get(self, url, **kwargs):
        return self._call("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._call("POST", url, **kwargs)

    def close(self):
        pass


class _MsalResponse(object):
    def __init__(self, resp):
        self.status_code = resp.status
        self.headers = resp.headers
        self.text = resp.body.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise GraphError("the token endpoint answered %d"
                             % self.status_code)


# ---------------------------------------------------------------- the client
class Client(object):
    """Upload, convert, delete. One drive, one folder, nothing else."""

    def __init__(self, tenant, client_id, secret, drive, folder=None,
                 send=None, timeout=300):
        self.tenant = tenant
        self.client_id = client_id
        self._secret = secret            # never in a message, never logged
        self.drive = drive
        self.folder = folder or FOLDER
        self.timeout = timeout
        self._send = send or transport()
        self._token = None
        self.journal = []                # (method, path) - the checks read it

    # ------------------------------------------------------------ plumbing
    def _request(self, method, url, headers=None, body=None, auth=True):
        head = dict(headers or {})
        if auth:
            head["Authorization"] = "Bearer %s" % self.token()
        self.journal.append((method, _path_of(url)))

        for attempt in range(RETRIES + 1):
            resp = self._send(method, url, head, body, self.timeout)
            if resp.status not in (429, 503) or attempt == RETRIES:
                return resp
            wait = _retry_after(resp)
            if wait > RETRY_CAP:
                raise GraphError("Graph is throttling this tenant and asked "
                                 "for %ds - giving up so the preview can fall "
                                 "back to another renderer" % wait)
            _sleep(wait)
        raise GraphError("unreachable")                    # pragma: no cover

    def token(self):
        """A bearer token for the app itself. Asked for once per client: a
        render is seconds long, and msal caches within its own lifetime."""
        if self._token:
            return self._token
        got = self._app().acquire_token_for_client(scopes=[SCOPE]) or {}
        if not got.get("access_token"):
            # The description is where "admin consent was never granted" shows
            # up, and that is the failure everybody hits first.
            raise GraphError("no access token: %s (%s)"
                             % (got.get("error") or "unknown",
                                (got.get("error_description") or "")
                                .splitlines()[0][:160]))
        self._token = got["access_token"]
        return self._token

    def _app(self):
        """The msal application for this tenant, kept between renders so the
        second one costs no Entra round trips at all."""
        try:
            import msal
        except ImportError:
            raise GraphError("msal is not installed (pip install msal)")
        key = (self.tenant, self.client_id, self._send)
        with _APPS_GUARD:
            app = _APPS.get(key)
        if app is not None:
            return app
        app = msal.ConfidentialClientApplication(
            self.client_id,
            authority="https://login.microsoftonline.com/%s" % self.tenant,
            client_credential=self._secret,
            http_client=_MsalHttp(self._send))
        with _APPS_GUARD:
            _APPS[key] = app
        return app

    # ------------------------------------------------------------ the steps
    def upload(self, path, name=None):
        """Put a local file in the drive. -> item id."""
        name = name or os.path.basename(path)
        size = os.path.getsize(path)
        if size > SIMPLE_MAX:
            return self._upload_session(path, name, size)
        with open(path, "rb") as fh:
            body = fh.read()
        resp = self._request(
            "PUT", "%s/drives/%s/items/root:/%s/%s:/content"
            % (V1, self.drive, self.folder, name),
            {"Content-Type": PPTX_TYPE}, body)
        if resp.status not in (200, 201):
            raise GraphError("the upload was refused (%d): %s"
                             % (resp.status, _why(resp)))
        return _id_of(resp)

    def _upload_session(self, path, name, size):
        """The >4 MB path. Worth having: a 60-slide deck with images passes
        4 MB easily, and a simple PUT of one stops working."""
        resp = self._request(
            "POST", "%s/drives/%s/items/root:/%s/%s:/createUploadSession"
            % (V1, self.drive, self.folder, name),
            {"Content-Type": "application/json"},
            json.dumps({"item": {"@microsoft.graph.conflictBehavior":
                                 "replace"}}).encode("utf-8"))
        if resp.status not in (200, 201):
            raise GraphError("could not start an upload session (%d): %s"
                             % (resp.status, _why(resp)))
        url = (resp.json() or {}).get("uploadUrl")
        if not url:
            raise GraphError("Graph started an upload session without a URL")

        with open(path, "rb") as fh:
            sent = 0
            while sent < size:
                piece = fh.read(CHUNK)
                if not piece:
                    break
                last = sent + len(piece) - 1
                # No Authorization header: the session URL carries its own
                # credential, and ours has no business being sent to it.
                part = self._request(
                    "PUT", url,
                    {"Content-Length": str(len(piece)),
                     "Content-Range": "bytes %d-%d/%d" % (sent, last, size)},
                    piece, auth=False)
                if part.status not in (200, 201, 202):
                    raise GraphError("an upload chunk was refused (%d): %s"
                                     % (part.status, _why(part)))
                sent = last + 1
                if part.status in (200, 201):       # the last chunk answers
                    return _id_of(part)             # with the finished item
        raise GraphError("the upload session ended without an item")

    def convert(self, item_id):
        """The point of all this. -> PDF bytes, rendered by Office Online."""
        resp = self._request("GET", "%s/drives/%s/items/%s/content?format=pdf"
                             % (V1, self.drive, item_id))
        if resp.status in (301, 302, 303, 307, 308):
            where = resp.headers.get("location")
            if not where:
                raise GraphError("Graph redirected the conversion to nowhere")
            # Deliberately unauthenticated, and the URL is never quoted in an
            # error: it is pre-authed, which makes it a credential itself.
            resp = self._request("GET", where, auth=False)
        if resp.status != 200:
            raise GraphError("the PDF conversion failed (%d): %s"
                             % (resp.status, _why(resp)))
        if not resp.body.startswith(b"%PDF"):
            raise GraphError("the conversion returned %d byte(s) that are not "
                             "a PDF" % len(resp.body))
        return resp.body

    def site_id(self):
        """Which site this drive belongs to. Cached: it cannot change."""
        key = (self.drive, self._send)
        with _APPS_GUARD:
            known = _SITES.get(key)
        if known:
            return known
        resp = self._request("GET", "%s/drives/%s/root?$select=sharepointIds"
                             % (V1, self.drive))
        if resp.status != 200:
            raise GraphError("could not find the site behind this drive "
                             "(%d): %s" % (resp.status, _why(resp)))
        found = ((resp.json() or {}).get("sharepointIds") or {}).get("siteId")
        if not found:
            raise GraphError("the drive did not say which site it is in, so "
                             "the recycle bin cannot be emptied")
        with _APPS_GUARD:
            _SITES[key] = found
        return found

    def recycled(self, name=None):
        """What is in the site recycle bin - all of it, or just what matches
        `name`. The other half of "is anything left behind?"."""
        from urllib.parse import quote
        url = "%s/sites/%s/recycleBin/items" % (BETA, self.site_id())
        url += "?$filter=name+eq+'%s'" % quote(name) if name else "?$top=200"
        resp = self._request("GET", url)
        if resp.status != 200:
            raise GraphError("could not read the recycle bin (%d): %s"
                             % (resp.status, _why(resp)))
        return [(i.get("name"), i.get("id"), i.get("size"))
                for i in (resp.json() or {}).get("value") or []]

    def purge(self, name):
        """Take `name` out of the recycle bin for good.

        Without this the deck is still in SharePoint after every render, just
        one click further away. Silence here would be the worst kind of bug in
        this project: a confidentiality guarantee that reads as kept and is not.
        """
        found = self.recycled(name)
        if not found:
            return                       # never reached the bin, or already out
        resp = self._request(
            "POST", "%s/sites/%s/recycleBin/items/delete"
            % (BETA, self.site_id()), {"Content-Type": "application/json"},
            json.dumps({"ids": [i for _n, i, _s in found]}).encode("utf-8"))
        if resp.status not in (200, 202, 204):
            raise GraphError(
                "the deck was removed but is STILL IN THE RECYCLE BIN and "
                "could not be purged (%d): %s. Empty it by hand, and check "
                "whether the beta recycleBin endpoint has changed."
                % (resp.status, _why(resp)))

    def delete(self, item_id, name=None):
        """Take the deck back out, properly. A 404 on the item is success: it
        is already gone. The purge still runs, because a previous attempt may
        have got as far as the bin and no further."""
        resp = self._request("DELETE", "%s/drives/%s/items/%s"
                             % (V1, self.drive, item_id))
        if resp.status not in (200, 204, 404):
            raise GraphError("the uploaded copy could NOT be deleted (%d): %s"
                             % (resp.status, _why(resp)))
        if name:
            self.purge(name)

    def site(self, path):
        """host.sharepoint.com:/sites/name -> site id.

        Only works once the per-site grant exists. Before it, Graph answers
        accessDenied - which is the clearest possible demonstration that
        Sites.Selected really does mean no access to anything at all.
        """
        resp = self._request("GET", "%s/sites/%s" % (V1, path))
        if resp.status != 200:
            raise GraphError("could not resolve that site (%d): %s"
                             % (resp.status, _why(resp)))
        site_id = (resp.json() or {}).get("id")
        if not site_id:
            raise GraphError("Graph found the site but gave it no id")
        return site_id

    def drives(self, site_id):
        """-> [(name, id)] for a site's document libraries."""
        resp = self._request("GET", "%s/sites/%s/drives" % (V1, site_id))
        if resp.status != 200:
            raise GraphError("could not list the libraries (%d): %s"
                             % (resp.status, _why(resp)))
        return [(d.get("name"), d.get("id"))
                for d in (resp.json() or {}).get("value") or []]

    def children(self, folder=None):
        """What is in our folder right now. Nothing in the render path calls
        this: it exists so `tools/graph_setup.py sweep` can answer the question
        §6 actually cares about, against a real tenant, after a real failure.
        """
        folder = self.folder if folder is None else folder
        resp = self._request("GET", "%s/drives/%s/items/root:/%s:/children"
                             % (V1, self.drive, folder))
        if resp.status == 404:
            return []                  # the folder was never made: nothing in it
        if resp.status != 200:
            raise GraphError("could not list the folder (%d): %s"
                             % (resp.status, _why(resp)))
        return [(item.get("name"), item.get("id"), item.get("size"))
                for item in (resp.json() or {}).get("value") or []]

    # ------------------------------------------------------------ the door
    def to_pdf(self, pptx):
        """Upload, convert, delete. -> PDF bytes.

        The delete is in a `finally`, and when the conversion succeeded but the
        delete did not, the delete's failure is what the caller gets. A preview
        is not worth leaving a client's proposal in SharePoint, so "it rendered
        but the copy is still there" has to be an error somebody sees.
        """
        # A name nothing else can collide with: two renders of the same deck
        # start the moment two people open the same proposal.
        name = "%s-%s.pptx" % (os.path.splitext(os.path.basename(pptx))[0],
                               uuid.uuid4().hex[:12])
        item = self.upload(pptx, name)
        try:
            return self.convert(item)
        finally:
            self.delete(item, name)


# ---------------------------------------------------------------- helpers
def _path_of(url):
    """Just the path, for the journal. Query strings carry credentials."""
    from urllib.parse import urlsplit
    return urlsplit(url).path or url


def _retry_after(resp):
    try:
        return max(1, int(float(resp.headers.get("retry-after") or 5)))
    except (TypeError, ValueError):
        return 5


def _why(resp):
    """Graph's own explanation, trimmed. Never the body wholesale: a failed
    download answers with a page of HTML, and this text reaches a screen."""
    try:
        err = (resp.json() or {}).get("error") or {}
        msg = err.get("message") or err.get("code") or ""
    except GraphError:
        msg = resp.body[:200].decode("utf-8", "replace")
    return " ".join(str(msg).split())[:200] or "no reason given"


def _id_of(resp):
    item = resp.json() or {}
    if not item.get("id"):
        raise GraphError("Graph accepted the upload without returning an id")
    return item["id"]


def load_dotenv(path=None):
    """Put `.env` into the environment, if there is one.

    This backend is configured by environment variables and nothing else,
    which is right for a deployment and awkward on a laptop where uvicorn is
    started by hand. A dozen lines beats a dependency. A real environment
    variable always wins - the file never overrides what is already set, so
    the deployment cannot be surprised by a file somebody left lying around.
    """
    if path is None:
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), ".env")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(),
                              value.strip().strip('"').strip("'"))


def forget_apps():
    """Drop every cached msal app, and the tokens they hold. For checks that
    need a cold start, and for a credential rotation."""
    with _APPS_GUARD:
        _APPS.clear()


def missing_config():
    """-> [names] of the environment variables that are not set."""
    return [name for name in ENV if not (os.environ.get(name) or "").strip()]


def from_env(send=None, timeout=300):
    gone = missing_config()
    if gone:
        raise GraphError("not configured: %s" % ", ".join(gone))
    return Client(os.environ["GRAPH_TENANT_ID"], os.environ["GRAPH_CLIENT_ID"],
                  os.environ["GRAPH_CLIENT_SECRET"],
                  os.environ["GRAPH_DRIVE_ID"],
                  os.environ.get("GRAPH_FOLDER") or FOLDER,
                  send=send, timeout=timeout)

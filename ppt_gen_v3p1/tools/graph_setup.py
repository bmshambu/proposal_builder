#!/usr/bin/env python
"""Set up, verify and sweep the SharePoint side of the graph render backend.

    python tools/graph_setup.py grant    --site contoso.sharepoint.com:/sites/x
    python tools/graph_setup.py discover --site contoso.sharepoint.com:/sites/x
    python tools/graph_setup.py check
    python tools/graph_setup.py sweep [--delete]

Four jobs, in the order you need them.

**grant** prints the requests that give the app access to exactly one site.
It does not make them, and it cannot: `Sites.Selected` means the app has no
access to anything until an administrator grants it per site, and an app with
no access cannot grant itself any. Somebody with `Sites.FullControl.All` has to
do it, from Graph Explorer or PnP PowerShell. That is the awkward step of the
least-privilege route, and printing it exactly is the most this tool can do.

**discover** finds `GRAPH_DRIVE_ID` using the app's own credentials, which is
also the first proof that the per-site grant landed: an app that can see this
site and no other is exactly what `Sites.Selected` is supposed to produce.

**check** proves the four variables actually work: a token, the drive, and what
is in the render folder. It prints how long the token took, because on a cold
client that is two round trips to Entra before Graph is touched at all.

**sweep** answers the question §6 of CLAUDE.md cares about, against the real
tenant: is anything still sitting in SharePoint? It looks in the render folder
**and in the site recycle bin**, because a DELETE only moves a file to the
latter. Run it after a deliberate mid-render failure. `--delete` empties both.

Nothing here prints a secret, a token or a signed URL.
"""
import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from engine import graph                                          # noqa: E402

# displayName is a label, not a lookup key - Graph matches the app on `id`.
# But it is the label an administrator sees when they later ask who has access
# to this site, so it has to be the real app registration's name. It used to be
# hardcoded to the name of the app in the spike tenant, which would have
# stamped somebody else's site with a name that exists nowhere in their
# directory.
# Libraries SharePoint makes for its own use. A site can have these and no
# real document library at all - a Team site whose Documents link was never
# added looks exactly like that. Uploading into one of them would work: Graph
# will put a client deck in Style Library without complaint, and nobody would
# notice until somebody asked why proposals passed through the site's
# stylesheet folder. So they are never picked automatically.
SYSTEM_LIBS = frozenset((
    "form templates", "style library", "site assets", "site pages",
    "preservation hold library", "customized reports", "images",
))

GRANT_BODY = """{
  "roles": ["write"],
  "grantedToIdentities": [{
    "application": { "id": "%s", "displayName": "%s" }
  }]
}"""

NO_NAME = "<the app registration's display name>"


def say(*bits):
    print(*bits)


def cmd_grant(args):
    """Print what an administrator has to run. Filled in, so it can be pasted
    rather than retyped with a typo in the site path."""
    graph.load_dotenv()
    app_id = os.environ.get("GRAPH_CLIENT_ID") or "<GRAPH_CLIENT_ID>"
    site = args.site or "<host>.sharepoint.com:/sites/<site>"
    app_name = args.app_name or os.environ.get("GRAPH_APP_NAME") or NO_NAME

    say("")
    say("Sites.Selected grants nothing on its own. Three requests, all in")
    say("Graph Explorer (https://developer.microsoft.com/graph/graph-explorer)")
    say("signed in as a tenant administrator, not as the app.")
    say("")
    say("Graph Explorer needs consent to Sites.FullControl.All for itself")
    say("before request 2 works. That consent is Graph Explorer's, not our")
    say("app's - our app stays on Sites.Selected.")
    say("")
    say("Its `Modify Permissions` tab only ever lists the permissions the")
    say("URL currently in the address bar needs. So put each request in the")
    say("bar FIRST, then consent, then Run. Looking for Sites.FullControl.All")
    say("while the bar still says /me will not find it.")
    say("")
    say("1. Find the site id")
    say("   GET https://graph.microsoft.com/v1.0/sites/%s" % site)
    say("   Take the `id` from the answer. It looks like")
    say("   contoso.sharepoint.com,<guid>,<guid> - all three parts.")
    say("")
    say("2. Grant this app write access to that site, and only that site")
    say("   POST https://graph.microsoft.com/v1.0/sites/<site id>/permissions")
    say("   Content-Type: application/json")
    say("")
    for line in (GRANT_BODY % (app_id, app_name)).splitlines():
        say("   " + line)
    say("")
    say("3. Find the drive id, and put it in .env as GRAPH_DRIVE_ID")
    say("   GET https://graph.microsoft.com/v1.0/sites/<site id>/drives")
    say("   The default document library is the one named Documents.")
    say("")
    say("Some tenants block Graph Explorer outright. Request 2 in the Graph")
    say("PowerShell SDK, which needs no app registration of its own:")
    say("   Connect-MgGraph -Scopes \"Sites.FullControl.All\"")
    say("   $params = @{ roles = @(\"write\"); grantedToIdentities = @(")
    say("       @{ application = @{ id = \"%s\";" % app_id)
    say("                          displayName = \"%s\" } }) }" % app_name)
    say("   New-MgSitePermission -SiteId \"<site id>\" -BodyParameter $params")
    say("")
    say("Or in PnP, which takes the site URL and needs no site id at all.")
    say("Note that since late 2024 PnP needs its own app registration in the")
    say("tenant, so this is the harder route for anyone not already using it:")
    say("   Connect-PnPOnline -Url https://<host>/sites/<name> -Interactive")
    say("   Grant-PnPAzureADAppSitePermission -AppId %s \\" % app_id)
    say("       -DisplayName '%s' -Permissions Write" % app_name)
    say("")
    say("Then: python tools/graph_setup.py check")
    say("")
    if app_name == NO_NAME:
        say("Re-run with --app-name 'Whatever IT called the app registration'")
        say("to get that filled in too. It is only a label, but it is the one")
        say("an administrator reads when auditing who can reach this site.")
        say("")
    if app_id.startswith("<"):
        say("(GRAPH_CLIENT_ID is not set, so the app id above is a placeholder.")
        say(" Fill in .env first and run this again to get it filled in.)")
        say("")
    return 0


def _client(need_drive=True):
    graph.load_dotenv()
    missing = [name for name in graph.missing_config()
               if need_drive or name != "GRAPH_DRIVE_ID"]
    if missing:
        say("not configured: %s" % ", ".join(missing))
        say("Copy .env.example to .env and fill it in.")
        return None
    if need_drive:
        return graph.from_env()
    # `discover` is the command that finds GRAPH_DRIVE_ID, so it is the one
    # command that cannot require it.
    return graph.Client(os.environ["GRAPH_TENANT_ID"],
                        os.environ["GRAPH_CLIENT_ID"],
                        os.environ["GRAPH_CLIENT_SECRET"],
                        os.environ.get("GRAPH_DRIVE_ID") or "")


def cmd_discover(args):
    """Find the drive id with the app's own credentials, which also proves the
    per-site grant landed - the app can see this site and no other."""
    client = _client(need_drive=False)
    if client is None:
        return 1
    if not args.site:
        say("which site? --site host.sharepoint.com:/sites/name")
        return 2
    try:
        site_id = client.site(args.site)
    except graph.GraphError as exc:
        say("FAIL  %s" % exc)
        say("")
        say("accessDenied means the per-site grant has not been made yet.")
        say("Run: python tools/graph_setup.py grant --site %s" % args.site)
        return 1
    say("site    %s" % site_id)
    try:
        libraries = client.drives(site_id)
    except graph.GraphError as exc:
        say("FAIL  %s" % exc)
        return 1
    say("")
    for name, drive_id in libraries:
        mark = "  (SharePoint's own)" if (name or "").lower() in SYSTEM_LIBS             else ""
        say("  %-24s %s%s" % (name, drive_id, mark))
    say("")

    usable = [(n, d) for n, d in libraries
              if (n or "").lower() not in SYSTEM_LIBS]
    if not usable:
        say("This site has no document library of its own - everything above")
        say("is one SharePoint made for itself. Create one before going any")
        say("further: Site contents -> + New -> Document library. A site")
        say("owner can normally do that without help from an administrator.")
        say("")
        say("Point GRAPH_DRIVE_ID at the new one, empty and used for nothing")
        say("else, so that anything `sweep` finds in it is ours by")
        say("definition.")
        return 1

    named = [d for n, d in usable if (n or "").lower() == "documents"]
    if len(usable) == 1:
        chosen = usable[0][1]
    elif named:
        chosen = named[0]
        say("Several to choose from; taking the default Documents library.")
        say("If one of the others was made for this, use that instead - an")
        say("empty library used for nothing else makes `sweep` meaningful.")
        say("")
    else:
        say("More than one library and none of them is the default, so this")
        say("is your choice to make, not mine. Take the id of whichever was")
        say("made for the renders and put it in .env as GRAPH_DRIVE_ID.")
        return 1

    say("Put this in .env:")
    say("")
    say("GRAPH_DRIVE_ID=%s" % chosen)
    say("")
    say("Then: python tools/graph_setup.py check")
    return 0


def cmd_check(args):
    client = _client()
    if client is None:
        return 1
    say("tenant  %s" % client.tenant)
    say("app     %s" % client.client_id)
    say("drive   %s" % client.drive)
    say("folder  %s" % client.folder)
    say("secret  set (%d characters, not shown)"
        % len(os.environ.get("GRAPH_CLIENT_SECRET") or ""))
    say("")

    began = time.time()
    try:
        client.token()
    except graph.GraphError as exc:
        say("FAIL  no token: %s" % exc)
        return 1
    say("ok    got a token in %.2fs (cold: that is Entra's OpenID document "
        "and then the token itself)" % (time.time() - began))

    began = time.time()
    try:
        items = client.children()
    except graph.GraphError as exc:
        say("FAIL  the drive is not reachable: %s" % exc)
        say("")
        say("accessDenied here almost always means step 2 of `grant` was")
        say("never done: the app is consented for Sites.Selected but has no")
        say("grant on this site.")
        return 1
    say("ok    reached the drive in %.2fs" % (time.time() - began))
    say("")
    if items:
        say("%d item(s) in %s - see `sweep`" % (len(items), client.folder))
    else:
        say("%s is empty, which is how it should look at rest"
            % client.folder)
    return 0


def cmd_sweep(args):
    """Two places, not one. A file removed from the folder is in the site
    recycle bin until something purges it, so a sweep that only looked at the
    folder would report "clean" over a bin full of client decks. It did,
    once."""
    client = _client()
    if client is None:
        return 1
    try:
        items = client.children()
        binned = client.recycled()
    except graph.GraphError as exc:
        say("could not sweep: %s" % exc)
        return 1

    if not items and not binned:
        say("nothing in %s, nothing in the site recycle bin." % client.folder)
        say("No deck was left behind.")
        return 0

    if items:
        say("IN THE FOLDER: %d item(s) in %s" % (len(items), client.folder))
        for name, _id, size in items:
            say("  %-44s %10s bytes" % (name, size if size is not None else "?"))
    if binned:
        say("IN THE RECYCLE BIN: %d item(s) - still readable by anyone who "
            "can open it" % len(binned))
        for name, _id, size in binned:
            say("  %-44s %10s bytes" % (name, size if size is not None else "?"))
    if not args.delete:
        say("")
        say("Run again with --delete to remove them, from both.")
        return 1

    stuck = []
    for name, item_id, _size in items:
        try:
            client.delete(item_id, name)          # removes and purges
        except graph.GraphError as exc:
            stuck.append((name, str(exc)))
    for name, _id, _size in binned:
        try:
            client.purge(name)
        except graph.GraphError as exc:
            stuck.append((name, str(exc)))

    left = len(client.children()) + len(client.recycled())
    say("")
    say("swept. %d item(s) remain." % left)
    for name, why in stuck:
        say("  still there: %s - %s" % (name, why))
    return 1 if left or stuck else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    subs = parser.add_subparsers(dest="cmd")

    grant = subs.add_parser("grant", help="print the per-site grant to make")
    grant.add_argument("--site", help="host.sharepoint.com:/sites/name")
    grant.add_argument("--app-name", dest="app_name",
                       help="the app registration's display name, as IT "
                            "created it (or set GRAPH_APP_NAME)")
    grant.set_defaults(run=cmd_grant)

    disc = subs.add_parser("discover", help="find GRAPH_DRIVE_ID")
    disc.add_argument("--site", help="host.sharepoint.com:/sites/name")
    disc.set_defaults(run=cmd_discover)

    check = subs.add_parser("check", help="prove the credentials work")
    check.set_defaults(run=cmd_check)

    sweep = subs.add_parser("sweep", help="is anything still in SharePoint?")
    sweep.add_argument("--delete", action="store_true",
                       help="remove what is found")
    sweep.set_defaults(run=cmd_sweep)

    args = parser.parse_args(argv)
    if not getattr(args, "run", None):
        parser.print_help()
        return 2
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())

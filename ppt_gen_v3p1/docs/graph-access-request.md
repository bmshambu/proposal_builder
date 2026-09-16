# Request: Entra ID app registration for proposal deck previews

**Requested by:** <your name>, <team>
**Business owner:** <partner / director who owns the proposal tool>
**Application:** proposal deck builder (`ppt_gen`), internal, <where it is
hosted — e.g. Azure App Service in <region>>
**Date:** <date>

---

## 1. What we are asking for, in one line

One single-tenant app registration with **`Sites.Selected`** on Microsoft
Graph, granted **write access to exactly one SharePoint document library** that
we would like created for this purpose — and nothing else.

## 2. Why

Our proposal builder assembles PowerPoint decks from an approved slide library
and a set of answers. Authors need to see the finished deck on screen before it
goes to a client.

Today the preview is assembled from a PDF of the slide library, so it shows the
right slides in the right order and the correct branding, but it **cannot show
the filled-in values** — the cover reads `{{ClientName}}` rather than the
client's name. Authors currently have to download the file and open PowerPoint
to check the deck is correct, which is exactly the error-prone step the tool
exists to remove.

Microsoft Graph can convert a file to PDF using Office Online's own renderer.
That renders the *built* deck, so the values appear, at Office fidelity. It is
the only option available to us:

- **LibreOffice** would work and is blocked by firm endpoint policy.
- **Server-side PowerPoint automation** is not licensed by Microsoft for
  unattended server use and cannot run on Azure in any case.

So this needs no third-party software approved or installed anywhere — which is
why we are asking for a Graph permission instead of a software exception.

## 3. Exactly what the application does

Per preview, six HTTP requests (nine on the first render after a
restart), typically 6–8 seconds end to end for a short deck:

| # | call | note |
|---|---|---|
| 1–2 | token from `login.microsoftonline.com/<tenant>/oauth2/v2.0/token` | client credentials, scope `https://graph.microsoft.com/.default` |
| 3 | `PUT /drives/{drive}/items/root:/pptgen-render/<name>.pptx:/content` | uploads the built deck |
| 4 | `GET /drives/{drive}/items/{id}/content?format=pdf` | the conversion |
| 5 | `GET <pre-authed URL Graph redirects to>` | retrieves the PDF |
| 6 | `DELETE /drives/{drive}/items/{id}` | removes the uploaded copy from the folder |
| 7 | `GET /beta/sites/{site}/recycleBin/items?$filter=name eq ...` | finds it in the recycle bin |
| 8 | `POST /beta/sites/{site}/recycleBin/items/delete` | purges it from the recycle bin |

**Steps 6 to 8 run in a `finally` block**, so the uploaded copy is removed
whether the conversion succeeded, failed, or errored part-way. If any of them
fails, that is raised as an error rather than ignored, and the render reports
failure. The file's residency in SharePoint is seconds.

Steps 7 and 8 exist because **a Graph `DELETE` is not a deletion** - SharePoint
moves the item to the site recycle bin, where it remains readable for the
retention period. We found this the hard way in testing and it is the single
most important correction we made. There is no v1.0 endpoint that empties a
recycle bin, so those two calls are on Microsoft's `beta` endpoint. If you have
a preferred alternative - a retention policy on the library, or a scheduled
purge you already run - we would rather use yours than depend on a beta API.

Steps 3 and 6-8 exist only because Graph has no endpoint that converts a
stream of bytes: `format=pdf` operates on a file that is already in a drive.
There is no way to do this without the file landing in SharePoint briefly.

## 4. Exactly what we are asking you to do

**a. Create a SharePoint site and document library** used for nothing else.
Suggested name: `ppt-render-spike` (or whatever fits your naming standard). It
will hold no permanent content — files exist in it for seconds at a time.

**b. Create an app registration**
- Name: `ppt-gen-graph-render` (or per your standard)
- **Single tenant** — accounts in this organizational directory only
- **No redirect URI** — this is a daemon/confidential client, app-only
- **No delegated permissions of any kind**

**c. Add one application permission and grant admin consent**
- Microsoft Graph → **Application permissions** → **`Sites.Selected`**
- Grant admin consent

**d. Grant that app write access to that one site.** `Sites.Selected` confers
no access at all on its own; it requires a per-site grant, which has no portal
UI. Either:

```
POST https://graph.microsoft.com/v1.0/sites/{site-id}/permissions
Content-Type: application/json

{
  "roles": ["write"],
  "grantedToIdentities": [
    { "application": { "id": "<application (client) id>",
                       "displayName": "ppt-gen-graph-render" } }
  ]
}
```

run by an identity holding `Sites.FullControl.All`, or the PnP equivalent:

```powershell
Connect-PnPOnline -Url https://<tenant>.sharepoint.com/sites/<site> -Interactive
Grant-PnPAzureADAppSitePermission -AppId <client id> `
    -DisplayName "ppt-gen-graph-render" -Permissions Write
```

**e. Credential.** We would prefer a **certificate** over a client secret. If a
secret is simpler for you, we will take a secret with your standard expiry and
rotation. Either way we need it delivered by whatever secure channel you use —
not by email.

## 5. What we are explicitly NOT asking for

We want this on record, because these are the permissions a request like this
is often assumed to need:

- ❌ `Files.ReadWrite.All` — access to every file in the tenant
- ❌ `Sites.ReadWrite.All` / `Sites.FullControl.All` — every site collection
- ❌ Any delegated permission, or any ability to act as a user
- ❌ `User.Read.All`, directory read, mail, calendar, Teams — none of it

`Sites.Selected` plus a single per-site grant is the minimum that works, and it
is what we are asking for. We have verified the restriction is real: **before
the per-site grant, the application with valid credentials received
`403 Access denied` on every site in the tenant, including reading its own.**

## 6. Data handling

- **What transits:** built proposal decks. These are confidential client
  material. They are uploaded to the named library, converted, and deleted.
- **Where it goes:** the tenant's own Microsoft 365 service. Nothing leaves
  Microsoft's boundary and nothing reaches any third party.
- **Retention:** none intended. The item is removed from the folder and purged
  from the site recycle bin, both inside a `finally`. We have a tool
  (`graph_setup.py sweep`) that lists the folder **and the recycle bin** and
  reports anything left behind; we would run it as a scheduled check, and are
  happy to report the results to you.
- **Second-stage recycle bin:** this is our one open question. SharePoint keeps
  a site-collection recycle bin behind the site one, and we have not been able
  to inspect it through Graph. **Please tell us whether purged items land
  there, and what retention applies.** If they do, we would like to agree a
  policy on this library rather than leave a copy sitting for 93 days.
- **Logging:** bearer tokens, the client secret and the pre-authed download URL
  are never written to logs or error messages. This is enforced by automated
  tests, not convention.
- **Secret storage:** environment variables, from <Key Vault / your standard>.
  Never in source control.

## 7. Risk, honestly

- **Blast radius** is one document library. A compromised credential could
  read and write that library and nothing else in the tenant.
- **Availability:** if the grant is revoked, the credential expires, or Graph is
  unreachable, the tool falls back to its existing preview automatically and
  reports why. Nothing breaks — the feature simply goes away. This is additive.
- **Volume:** one upload and one delete per preview. We measured ten previews
  back to back with no throttling, but that was a test tenant; if you have a
  view on expected load we will rate-limit accordingly.
- **What this does not change:** which slides are selected or what they say.
  That is decided entirely by the rules and the answers, before any of this
  happens. This is a renderer.

## 8. What we need back

| | |
|---|---|
| Directory (tenant) ID | |
| Application (client) ID | |
| Certificate, or client secret | via <secure channel> |
| Drive ID of the library | or just the site URL — we can resolve the drive ID ourselves once the grant exists |

## 9. Questions for you

1. Does your **app consent policy** permit `Sites.Selected` for an
   internally-developed application, and is there a process we should follow?
2. Does **conditional access** apply to app-only tokens in a way that affects
   this, and do we need the app added to any exclusion or allow list?
3. Do you require a **certificate** rather than a client secret? What rotation
   period should we plan for?
4. Is there an existing site collection you would rather we use than a new one?
5. Does anything in **DLP or retention policy** apply to a file that exists in
   a library for a few seconds?
6. **Second-stage recycle bin** - see section 6. What retention applies, and
   would you rather set a policy on this library than have us call a beta API?
7. Is **versioning** on by default for new libraries in your tenant? If so we
   would like it off for this one: a version history of deleted decks would
   defeat the point of purging them.

## 10. If the answer is no

That is a workable outcome. The current preview continues to work exactly as it
does today; authors keep opening the file in PowerPoint for the final check.
We would rather know that than have this sit in a queue, so please do say so.

---

*This was built and tested end to end against a separate, isolated Microsoft
365 tenant using synthetic data only. No client material was used at any point.
The full test results, including timings and what remains unproven, are in
`docs/graph-findings.md` and can be shared on request.*

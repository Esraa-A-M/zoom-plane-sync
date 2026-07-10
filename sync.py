#!/usr/bin/env python3
"""
Zoom AI Companion meeting summaries -> Plane project pages.

Poller: run on a schedule (cron/launchd). For each new Zoom meeting summary
since the last run, classifies the meeting to a DCDial project and creates a
Plane page in that project (same layout as the migrated Notion meetings:
overview + action items + link back to Zoom).

Idempotent: synced Zoom meeting UUIDs are recorded in ledger.json next to this
script. Plane pages cannot be edited or deleted via API, so the ledger is the
only duplicate guard — do not delete it.

Usage:
  python3 sync.py --check          # verify Zoom + Plane credentials, list projects
  python3 sync.py --dry-run        # fetch + classify, print what would be created
  python3 sync.py                  # real run
  python3 sync.py --since 2026-07-01   # override lookback start (default: last run, else 7 days)

Config: environment variables or a .env file next to this script (KEY=value lines):
  ZOOM_ACCOUNT_ID, ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET   (Server-to-Server OAuth app)
  PLANE_TOKEN_FILE   (default ~/.plane_token)
  ANTHROPIC_API_KEY  (optional — enables LLM classification; falls back to keywords)
"""
import json, os, re, sys, time, html, subprocess, base64
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(HERE, "ledger.json")

# ---------- config ----------
def load_env():
    p = os.path.join(HERE, ".env")
    if os.path.exists(p):
        for line in open(p):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
load_env()

WS = "dcdial"
PLANE_TOKEN = os.environ.get("PLANE_TOKEN") or re.sub(r"[‘’“”\s]", "", open(os.path.expanduser(
    os.environ.get("PLANE_TOKEN_FILE", "~/.plane_token"))).read())

# Projects are resolved LIVE from the Plane API at each run (by identifier),
# so adding/removing projects in Plane never breaks the sync. Router rules
# whose identifier doesn't exist in the workspace are skipped with a warning.
def resolve_projects():
    d = curl("GET", f"https://api.plane.so/api/v1/workspaces/{WS}/projects/?per_page=100",
             headers=[f"X-API-Key: {PLANE_TOKEN}"])
    return {p["identifier"]: p["id"] for p in d["results"]}

# Meetings that classify to nothing land in the fallback with an [unsorted]
# marker so they are never silently dropped; re-file them in the UI.
FALLBACK = "PLAT"

# keyword router (checked against title + summary text; every matching
# project gets a page — same convention as the migrated meetings)
RULES = [
    ("RAI",       r"recov\s*ai|recovai|debt\s*recovery|collections\b|textract|enigma|kyb"),
    ("CPX",       r"chrome\s*extension|copilot|co-pilot|extension\b"),
    ("MOB",       r"mobile\s*app|react\s*native|\bios\b|\bandroid\b|\bapk\b|app\s*store"),
    ("CP",        r"customer\s*portal|self[- ]service portal|portal\s*(login|signup|dashboard)"),
    ("DIAL",      r"\bdialer\b|\bvici\b|\bhopper\b|voice\s*box|voicebox|\brvm\b|campaign|inbound\s*call|broadcast|lead\s*(recycl|filter|list)"),
    ("NEWWEBAPP", r"web\s*app|modernization|modernisation|legacy\s*(code|portal)|copy-web"),
    ("PLAT",      r"admin\s*portal|apiv2|billing|stripe|stax|payment|agreement|email\s*template|timezone|subscription|invoice"),
]

# ---------- tiny curl client (avoids macOS python SSL cert issues) ----------
def curl(method, url, headers=None, data=None, retries=6):
    args = ["curl", "-s", "-w", "\n%{http_code}", "-X", method, url]
    for h in (headers or []):
        args += ["-H", h]
    if data is not None:
        args += ["-H", "Content-Type: application/json", "-d", json.dumps(data)]
    for _ in range(retries):
        r = subprocess.run(args, capture_output=True, text=True)
        body, code = r.stdout.rsplit("\n", 1)
        if code in ("200", "201", "204"):
            return json.loads(body) if body.strip() else {}
        try:
            if json.loads(body).get("error_code") == 5900:  # Plane rate limit
                time.sleep(6); continue
        except Exception:
            pass
        if code in ("429", "500", "502", "503", "504"):
            time.sleep(6); continue
        raise RuntimeError(f"{method} {url} -> {code}: {body[:300]}")
    raise RuntimeError(f"retries exhausted: {url}")

# ---------- zoom ----------
def zoom_token():
    for k in ("ZOOM_ACCOUNT_ID", "ZOOM_CLIENT_ID", "ZOOM_CLIENT_SECRET"):
        if not os.environ.get(k):
            sys.exit(f"Missing {k} (set env var or .env next to sync.py)")
    basic = base64.b64encode(
        f"{os.environ['ZOOM_CLIENT_ID']}:{os.environ['ZOOM_CLIENT_SECRET']}".encode()).decode()
    d = curl("POST",
             f"https://zoom.us/oauth/token?grant_type=account_credentials&account_id={os.environ['ZOOM_ACCOUNT_ID']}",
             headers=[f"Authorization: Basic {basic}"], data=None)
    return d["access_token"]

def zoom_list_summaries(token, since_iso, until_iso):
    """List meeting summaries in window. Paginates.
    NOTE: Zoom ignores from/to on this endpoint, so we fetch and filter
    client-side on meeting_start_time."""
    out, npt = [], ""
    while True:
        url = "https://api.zoom.us/v2/meetings/meeting_summaries?page_size=100"
        if npt:
            url += f"&next_page_token={npt}"
        d = curl("GET", url, headers=[f"Authorization: Bearer {token}"])
        out += d.get("summaries", [])
        npt = d.get("next_page_token", "")
        if not npt:
            break
        time.sleep(0.5)
    return [s for s in out
            if since_iso <= (s.get("meeting_start_time") or "")[:10] <= until_iso]

def zoom_get_summary(token, meeting_uuid):
    # double-encode uuid if it contains / or starts with =
    uid = meeting_uuid
    if uid.startswith("/") or "//" in uid:
        from urllib.parse import quote
        uid = quote(quote(uid, safe=""), safe="")
    return curl("GET", f"https://api.zoom.us/v2/meetings/{uid}/meeting_summary",
                headers=[f"Authorization: Bearer {token}"])

# ---------- classification ----------
def classify_keywords(title, body):
    """Tightened routing: the title is the strongest signal — projects matching
    the TITLE win outright. Otherwise score body keyword hits per project and
    keep only clear leaders (>=4 hits, max 2 projects). Passing mentions in a
    multi-topic standup no longer fan the meeting out to every project."""
    tl = title.lower()
    title_hits = [p for p, pat in RULES if re.search(pat, tl)]
    if title_hits:
        return title_hits[:2]
    low = body.lower()
    scores = {p: len(re.findall(pat, low)) for p, pat in RULES}
    ranked = sorted(((n, p) for p, n in scores.items() if n >= 4), reverse=True)
    return [p for _, p in ranked[:2]]

def classify_llm(title, summary_text):
    """Optional: use Claude for routing when ANTHROPIC_API_KEY is set."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    prompt = (
        "Classify this meeting into DCDial project buckets. Reply with ONLY a JSON "
        "array of project codes from: PLAT (core platform/admin/billing), CPX (Chrome "
        "extension), MOB (mobile app), RAI (RecovAI), CP (customer portal), DIAL "
        "(dialer/campaigns), NEWWEBAPP (web app modernization). Pick "
        "every project with a substantive segment; [] if none.\n\n"
        f"Title: {title}\n\nSummary:\n{summary_text[:4000]}")
    try:
        d = curl("POST", "https://api.anthropic.com/v1/messages",
                 headers=[f"x-api-key: {key}", "anthropic-version: 2023-06-01"],
                 data={"model": "claude-haiku-4-5-20251001", "max_tokens": 100,
                       "messages": [{"role": "user", "content": prompt}]})
        txt = d["content"][0]["text"]
        arr = json.loads(re.search(r"\[.*\]", txt, re.S).group(0))
        return [p for p in arr if p in dict(RULES) or p == FALLBACK]
    except Exception as e:
        print(f"  llm classify failed ({e}); falling back to keywords", file=sys.stderr)
        return None

# ---------- page rendering ----------
def esc(s): return html.escape(s or "")

def render(meta, summary):
    """summary: Zoom meeting_summary object."""
    day = (meta.get("meeting_start_time") or "")[:10]
    title = meta.get("meeting_topic") or summary.get("meeting_topic") or "Meeting"
    parts = [f"<p><strong>Date:</strong> {day} &nbsp;·&nbsp; <strong>Source:</strong> Zoom AI Companion</p>"]
    overview = (summary.get("summary_overview") or "").strip()
    if overview:
        parts.append(f"<p>{esc(overview)}</p>")
    details = summary.get("summary_details") or []
    if details:
        parts.append("<h3>Discussion</h3>")
        for dsec in details:
            lab = (dsec.get("label") or "").strip()
            body = (dsec.get("summary") or "").strip()
            if lab:
                parts.append(f"<p><strong>{esc(lab)}</strong></p>")
            if body:
                parts.append(f"<p>{esc(body)}</p>")
    steps = summary.get("next_steps") or []
    if steps:
        parts.append("<h3>Action items</h3><ul>" +
                     "".join(f"<li>☐ {esc(s)}</li>" for s in steps) + "</ul>")
    if not overview and not details and not steps:
        parts.append("<p><em>No AI summary content was generated for this meeting.</em></p>")
    return title, day, "".join(parts)

def plane_create_page(project_uuid, name, body):
    return curl("POST",
                f"https://api.plane.so/api/v1/workspaces/{WS}/projects/{project_uuid}/pages/",
                headers=[f"X-API-Key: {PLANE_TOKEN}"],
                data={"name": name[:120], "description_html": body})

# ---------- main ----------
def main():
    argv = sys.argv[1:]
    projects = resolve_projects()
    if FALLBACK not in projects:
        sys.exit(f"Fallback project '{FALLBACK}' not found in workspace — fix FALLBACK in sync.py")
    if "--check" in argv:
        print(f"Plane: OK — {len(projects)} projects: {sorted(projects)}")
        missing = [p for p, _ in RULES if p not in projects]
        if missing:
            print(f"  note: router rules for absent projects (will be skipped): {missing}")
        print("Zoom:", end=" ")
        tok = zoom_token()
        print("OK — token acquired")
        return

    ledger = json.load(open(LEDGER)) if os.path.exists(LEDGER) else {"synced": {}, "last_run": None}
    dry = "--dry-run" in argv

    if "--since" in argv:
        since = argv[argv.index("--since") + 1]
    elif ledger.get("last_run"):
        since = ledger["last_run"][:10]
    else:
        since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    until = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    tok = zoom_token()
    summaries = zoom_list_summaries(tok, since, until)
    print(f"window {since}..{until}: {len(summaries)} summaries listed")

    created = 0
    for meta in summaries:
        uuid = meta.get("meeting_uuid") or meta.get("uuid")
        if not uuid or uuid in ledger["synced"]:
            continue
        try:
            summ = zoom_get_summary(tok, uuid)
        except Exception as e:
            print(f"  ! could not fetch summary for {meta.get('meeting_topic','?')}: {e}")
            continue
        title, day, body = render(meta, summ)
        text = f"{summ.get('summary_overview','')}\n" + \
               " ".join((d.get("summary") or "") for d in (summ.get("summary_details") or []))
        projs = classify_llm(title, text)
        if projs is None:
            projs = classify_keywords(title, text)
        projs = [p for p in projs if p in projects]  # drop rules for absent projects
        unsorted = not projs
        if unsorted:
            projs = [FALLBACK]
        name = f"{day} — {title}" + (" [unsorted]" if unsorted else "")
        print(f"  {name}  ->  {projs}")
        if not dry:
            done_projects = []
            try:
                for p in projs:
                    plane_create_page(projects[p], name, body)
                    done_projects.append(p)
                    time.sleep(1.2)
                ledger["synced"][uuid] = {"title": title, "date": day, "projects": projs}
                json.dump(ledger, open(LEDGER, "w"), indent=1)
                created += 1
            except Exception as e:
                # partial-create: record what landed so a re-run doesn't duplicate
                if done_projects:
                    ledger["synced"][uuid] = {"title": title, "date": day,
                                              "projects": done_projects, "partial": True}
                    json.dump(ledger, open(LEDGER, "w"), indent=1)
                print(f"  ! create failed for {name}: {e}")
    if not dry:
        ledger["last_run"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        json.dump(ledger, open(LEDGER, "w"), indent=1)
    print(f"{'DRY RUN — nothing created' if dry else f'created pages for {created} meetings'}")

if __name__ == "__main__":
    main()

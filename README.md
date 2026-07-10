# Zoom → Plane meeting-notes sync

Polls Zoom AI Companion meeting summaries and creates a page in the right
Plane project for each new meeting — same layout as the migrated Notion
meetings (overview, discussion, action items).

## One-time setup

### 1. Zoom Server-to-Server OAuth app (admin does this once)
1. Go to https://marketplace.zoom.us → **Develop → Build App → Server-to-Server OAuth**.
2. Name it e.g. `plane-meeting-sync`, activate it.
3. Under **Scopes**, add BOTH granular scopes (they only appear in the picker
   if "Meeting Summary with AI Companion" is enabled in Account Settings → AI
   Companion — enable that first if you can't find them):
   - `meeting:read:list_summaries:admin` (list all users' meeting summaries)
   - `meeting:read:summary:admin` (view a meeting's summary)
4. Copy **Account ID / Client ID / Client Secret** from the app credentials page.

Requirements on the Zoom side: **AI Companion meeting summaries must be
enabled** for the account/users — the API only returns summaries that AI
Companion actually generated during the meeting.

### 2. Configure
Create `.env` next to `sync.py`:

```
ZOOM_ACCOUNT_ID=...
ZOOM_CLIENT_ID=...
ZOOM_CLIENT_SECRET=...
# optional: better meeting→project routing via Claude (else keyword rules)
# ANTHROPIC_API_KEY=sk-ant-...
```

The Plane token is read from `~/.plane_token` (already in place on this Mac).

### 3. Verify, then dry-run
```bash
python3 sync.py --check      # confirms Plane + Zoom credentials work
python3 sync.py --dry-run    # lists what would be created, touches nothing
python3 sync.py              # real run
```

### 4. Schedule (every hour, or daily)
```bash
crontab -e
# add:
0 * * * * cd "/Users/esraa/Claude Figma/zoom-plane-sync" && /usr/bin/python3 sync.py >> sync.log 2>&1
```

## How routing works
Each meeting is classified into project(s): RAI, CPX, MOB, CP, DIAL, MKT, ML,
PLAT. With `ANTHROPIC_API_KEY` set it asks Claude (haiku) per meeting;
otherwise keyword rules (see `RULES` in sync.py). A meeting matching several
projects gets a page in each — same convention as the migrated meetings.
Unclassifiable meetings land in **PLAT** with an `[unsorted]` suffix so
nothing is silently dropped; re-file those in the UI.

## Important constraints
- **Plane pages cannot be edited or deleted via the API.** `ledger.json` is
  the only duplicate guard — never delete it. If a page comes out wrong,
  delete it in the Plane UI; to re-sync one meeting, remove its UUID from
  `ledger.json` and re-run.
- First-ever run looks back 7 days (override with `--since 2026-07-01`).
- MKT and ML currently reject page creation for this API token (403) — if a
  meeting routes there the create fails and is logged; fix those projects'
  permissions in Plane to unblock.

## Files
- `sync.py` — the poller (self-contained, stdlib + curl only)
- `.env` — credentials (create it; never commit)
- `ledger.json` — synced meeting UUIDs + last-run timestamp (auto-created)
- `sync.log` — cron output

# API collection (Bruno)

Runnable collection covering every Week 2 workflow.

## Run it

1. Install [Bruno](https://www.usebruno.com/) (free, open source, no account)
2. **Open Collection** → select this `api-collection/` folder
3. Pick an environment: **production** or **local**
4. Run **01 - Login** first — it stores the session token every other request uses
5. Run the rest in order

## Why Bruno and not Postman

The `.bru` files are plain text and live in git, so the collection is reviewable
in a diff and versioned with the code that it documents. Postman collections are
JSON blobs synced to a cloud account — they drift, and you cannot review a change
to one.

## What each request demonstrates

| # | Request | Demonstrates |
|---|---|---|
| 01 | Login | session token; everything else 401s without it |
| 02 | Health | liveness |
| 03 | Ready | per-dependency readiness + degraded state |
| 04 | Respond (document path) | supervisor → 2 workers → grounded cited answer |
| 05 | Respond (chart fallback) | no documents → supervisor routes to chart_reader |
| 06 | Respond (safe refusal) | off-scope refused before any worker runs |
| 07 | Respond (unauthenticated) | 401 — the Week 1 authorization finding, closed |
| 08 | Ask (Week 1) | Week 1 baseline, unchanged |
| 09 | Audit trail | user, correlation ID, action, patient, outcome, denial reason |
| 10 | BAA status | fail-closed deployment control |

Credentials and patient IDs are in `environments/`. All data is synthetic.

# eth-explorer

A Bitwise-branded Ethereum consensus-layer analyst dashboard: validator exit/entry
queue waits, churn limits, validator lookup, withdrawal prediction, pending
partial-withdrawals, a spec-faithful methodology trace, and **daily historical
time-series back to the Pectra fork**.

The backend is a FastAPI app that queries a Beacon Chain node and also serves the
static frontend. There is no build step — the frontend is plain HTML/CSS/JS.

## Structure

```
explorer/
├── backend/          # FastAPI app (Python)
│   ├── main.py       # routes; mounts the frontend; daily-snapshot lifespan task
│   ├── beacon.py     # Beacon REST client + cache (auto-loads .env)
│   ├── metrics.py    # state-addressable compute_* for every headline metric
│   ├── snapshot.py   # daily snapshot collector + backfill CLI
│   ├── history_db.py # SQLite store for daily snapshots
│   ├── churn.py / cursors.py / spec.py / trace.py / models.py
├── frontend/         # static dashboard (HTML + CSS + vanilla JS)
│   ├── index.html  app.js  site.css  assets/  styles/
├── requirements.txt
├── .env.example      # copy to .env and fill in
└── data/             # SQLite history DB (gitignored, rebuilt by backfill)
```

## Quickstart

```bash
git clone <this-repo> eth-explorer && cd eth-explorer

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env        # then edit .env: set BEACON_URL and BEACON_TOKEN

uvicorn backend.main:app --port 8000
```

Open **http://127.0.0.1:8000**. Interactive API docs at `/docs`.

`.env` is auto-loaded (python-dotenv); no need to `source` it.

## Historical snapshots

The **History** tab plots a daily time-series of every headline metric back to
Pectra. Data lives in `data/history.db` (SQLite). While the server runs, a
background task self-heals recent missing days automatically.

The one-time historical **backfill** is a separate, heavy job (each day pulls the
full validator set), so run it deliberately — ideally on a server, not a laptop:

```bash
# Full backfill to Pectra (resumable — safe to re-run; skips days already stored)
python -m backend.snapshot backfill

# Bounded test first
python -m backend.snapshot backfill --days 5

# Fill only recent missing days
python -m backend.snapshot catch-up
```

**Backfill requires an archive beacon node** (one that serves finalized state for
past slots, back to the Pectra activation epoch 364032). A pruned node will only
answer recent slots; forward collection still works without an archive node.

## Key endpoints

| Endpoint | Description |
|---|---|
| `GET /network/stats` | active validators, total stake, compounding share, churn, pending consolidations |
| `GET /exit-queue` · `GET /entry-queue` | queue depth, wait/drain estimates, severity |
| `GET /pending-partial-withdrawals` | pending partials list + totals |
| `GET /validator/{id}/lookup` | bundled validator detail (incl. pending-deposit fallback) |
| `GET /predict/exit` · `GET /predict/partial-withdrawal` | spec-faithful predictions |
| `GET /history/daily` | daily scalar snapshots (oldest→newest), optional `?from=&to=` |
| `GET /methodology/{...}` | full spec trace with simulator output |

## Configuration

| Variable | Description |
|---|---|
| `BEACON_URL` | Beacon Chain (consensus) node REST URL |
| `BEACON_TOKEN` | Bearer token for the node, if required |

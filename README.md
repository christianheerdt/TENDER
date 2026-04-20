# TERN Tender Monitor

Flask app that searches EU TED for German healthcare/nursing public tenders and scores them for relevance to TERN's business.

## Quick start (local)

```bash
pip install -r requirements.txt
python app.py
# → http://localhost:5000/app
```

## Endpoints

| Route | Description |
|---|---|
| `GET /` | Health check → `OK` |
| `GET /app` | Web UI |
| `GET /api/tenders` | JSON API |

### API params (`/api/tenders`)

| Param | Default | Description |
|---|---|---|
| `min_score` | `60` | Minimum relevance score (0–100) |
| `q` | `` | Free-text filter (title / buyer / description) |
| `refresh` | `0` | Set to `1` to bypass cache |

## Deploy to Vercel

```bash
npm i -g vercel
vercel
```

No environment variables required. The app works out of the box with the public TED API.
Falls back to demo data automatically if TED is unreachable.

## How scoring works

Tenders are scored 0–100 across four dimensions:

- **Core** (max 40): nursing/healthcare terms in title + description
- **Recruiting** (max 25): staffing/recruitment terms
- **Qualification** (max 20): recognition, language courses, integration
- **Buyer fit** (max 15): buyer is a hospital, care home, health authority etc.
- **Penalty**: −20 to −60 for building maintenance / cleaning false positives

Categories:
- **Direct nursing recruitment opportunity** → score ≥ 80
- **Training / recognition / qualification** → qualification ≥ 10 & core ≥ 10
- **Healthcare staffing adjacent** → score ≥ 60
- **Low relevance** → everything else (filtered out by default)

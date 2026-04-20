import os
import time
from datetime import datetime, timedelta
from typing import Any

import requests
from flask import Flask, jsonify, request, render_template_string

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CACHE_TTL_SECONDS = 30 * 60          # 30 min (serverless: best-effort only)
DEFAULT_DAYS_BACK  = 30
DEFAULT_LIMIT      = 200

TED_SEARCH_URL = "https://api.ted.europa.eu/v3/notices/search"

# CPV codes: healthcare / nursing / social services
HEALTHCARE_CPV_PREFIXES = [
    "851",   # Medical and paramedical services
    "853",   # Residential care services
    "854",   # Miscellaneous social services
    "791",   # Recruitment services
    "803",   # Education / training (incl. language courses)
]

# In-memory cache (single process only; refreshed per cold start on serverless)
_cache: dict[str, Any] = {"ts": 0, "data": []}


# ---------------------------------------------------------------------------
# Scoring model
# ---------------------------------------------------------------------------

HARD_KEYWORDS = [
    # German nursing / healthcare
    "pflege", "pflegekraft", "pflegekräfte", "pflegefachkraft", "pflegefachkräfte",
    "pflegefachfrau", "pflegefachmann", "altenpflege", "seniorenpflege",
    "stationäre pflege", "langzeitpflege", "pflegeheim", "pflegedienst",
    "krankenhaus", "klinik", "universitätsklinikum", "gesundheitswesen",
    "medizinisches personal", "rettungsdienst", "hebamme",
    # Recruiting / staffing
    "rekrutierung", "recruiting", "personalgewinnung", "personalvermittlung",
    "personaldienstleistung", "fachkräftegewinnung", "fachkräftemangel",
    "ausländische fachkräfte", "ausland", "international",
    # Recognition / training / integration
    "anerkennung", "kenntnisprüfung", "anpassungslehrgang", "qualifizierung",
    "sprachkurs", "deutsch b2", "integration",
    # English
    "nursing", "nurse", "nurses", "geriatric care", "healthcare staffing",
    "recruitment", "recognition of qualifications",
]

FALSE_POSITIVE_CONTEXT = [
    "gebäudepflege", "reinigung", "unterhaltsreinigung", "grünpflege",
    "straßenpflege", "wartung", "instandhaltung", "facility management",
    "hausmeister", "gebäudemanagement", "winterdienst", "grünflächenpflege",
]

BUYER_SIGNALS = [
    "krankenhaus", "klinik", "universitätsklinikum", "pflegeheim", "pflegedienst",
    "senioren", "gesundheitsamt", "landesamt", "sozial", "gesundheit",
    "diakonie", "caritas", "wohlfahrt", "awo", "drk",
]

CORE_TERMS      = ["pflege", "pflegefach", "krankenhaus", "klinik", "altenpflege",
                   "langzeitpflege", "pflegeheim", "pflegedienst", "nursing", "nurse", "geriatric"]
RECRUITING_TERMS = ["rekrutierung", "recruit", "personal", "vermittlung",
                    "personaldienst", "staffing", "fachkräfte"]
QUAL_TERMS      = ["anerkennung", "kenntnisprüfung", "anpassungslehrgang",
                   "qualifizierung", "sprachkurs", "deutsch b2", "integration", "recognition"]
NURSING_TERMS   = ["pflegefach", "krankenhaus", "klinik", "nursing", "nurse", "altenpflege"]


def _norm(s: str) -> str:
    return (s or "").lower()


def _find_matches(text: str, keywords: list[str]) -> list[str]:
    t = _norm(text)
    return sorted({kw for kw in keywords if kw in t})


def score_tender(title: str, desc: str, buyer: str) -> tuple[int, dict, str, list[str]]:
    blob   = " ".join([title or "", desc or "", buyer or ""])
    blob_l = _norm(blob)

    matched = _find_matches(blob, HARD_KEYWORDS)
    if not matched:
        return 0, {"core": 0, "recruiting": 0, "qualification": 0, "buyer": 0, "penalty": 0}, "Low relevance (no match)", []

    core         = min(40, sum(1 for t in CORE_TERMS      if t in blob_l) * 10)
    recruiting   = min(25, sum(1 for t in RECRUITING_TERMS if t in blob_l) * 6)
    qualification = min(20, sum(1 for t in QUAL_TERMS      if t in blob_l) * 5)
    buyer_fit    = min(15, sum(1 for t in BUYER_SIGNALS    if t in _norm(buyer) or t in blob_l) * 5)

    fp_hits = sum(1 for t in FALSE_POSITIVE_CONTEXT if t in blob_l)
    nursing_present = any(t in blob_l for t in NURSING_TERMS)
    penalty = (-20 if nursing_present else -60) if fp_hits > 0 else 0

    total = max(0, min(100, core + recruiting + qualification + buyer_fit + penalty))

    if total >= 80 and core >= 20 and recruiting >= 10:
        category = "Direct nursing recruitment opportunity"
    elif qualification >= 10 and core >= 10:
        category = "Training / recognition / qualification"
    elif total >= 60:
        category = "Healthcare staffing adjacent"
    else:
        category = "Low relevance (likely false positive)"

    breakdown = {"core": core, "recruiting": recruiting, "qualification": qualification,
                 "buyer": buyer_fit, "penalty": penalty}
    return total, breakdown, category, matched


# ---------------------------------------------------------------------------
# TED API fetch
# ---------------------------------------------------------------------------

def _build_ted_query(days_back: int) -> dict:
    """
    Build TED v3 search payload.
    Targets German healthcare/nursing CPV codes published within `days_back`.
    """
    since = (datetime.utcnow() - timedelta(days=days_back)).strftime("%Y%m%d")
    cpv_filter = " OR ".join(f"cpv-codes:{p}*" for p in HEALTHCARE_CPV_PREFIXES)

    return {
        "query": f"({cpv_filter}) AND publication-date>={since} AND buyer-country-sub:DE",
        "fields": [
            "notice-id",
            "title-glo",
            "description-glo",
            "organisation-name-buyer",
            "publication-date",
            "deadline-date-lot",
            "cpv-codes",
            "notice-version",
        ],
        "page": 1,
        "limit": 250,
        "sort": {"publication-date": "desc"},
    }


def fetch_ted_items(days_back: int = DEFAULT_DAYS_BACK,
                    limit: int = DEFAULT_LIMIT) -> list[dict]:
    try:
        payload = _build_ted_query(days_back)
        resp = requests.post(
            TED_SEARCH_URL,
            json=payload,
            timeout=20,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        print(f"[TED] fetch failed: {exc} – falling back to demo data")
        return _demo_items()

    notices = data.get("notices") or data.get("results") or data.get("items") or []
    if not notices:
        print("[TED] empty response, using demo data")
        return _demo_items()

    items: list[dict] = []
    for n in notices[:limit]:
        # TED returns multilingual dicts; prefer "DEU" or "ENG" or first available
        def pick(field: str) -> str:
            val = n.get(field)
            if isinstance(val, dict):
                return val.get("DEU") or val.get("ENG") or next(iter(val.values()), "")
            if isinstance(val, list):
                return " ".join(str(v) for v in val)
            return str(val) if val else ""

        notice_id = n.get("notice-id") or n.get("id") or ""
        items.append({
            "id": notice_id,
            "title": pick("title-glo"),
            "description_short": (pick("description-glo") or "")[:400],
            "buyer": pick("organisation-name-buyer"),
            "place_of_performance": "Deutschland",
            "publication_date": pick("publication-date"),
            "deadline_date": pick("deadline-date-lot"),
            "cpv_codes": n.get("cpv-codes") if isinstance(n.get("cpv-codes"), list) else [],
            "url": f"https://ted.europa.eu/en/notice/-/detail/{notice_id}" if notice_id else "",
        })

    return items


def _demo_items() -> list[dict]:
    """Fallback demo data when TED is unreachable."""
    today = datetime.utcnow()
    return [
        {
            "id": "DEMO-1",
            "title": "Personalgewinnung Pflegefachkräfte (international) – Rahmenvertrag",
            "description_short": "Rekrutierung und Integration internationaler Pflegefachkräfte inkl. Anerkennungsvorbereitung.",
            "buyer": "Städtisches Krankenhaus Beispielstadt",
            "place_of_performance": "Deutschland",
            "publication_date": (today - timedelta(days=2)).strftime("%Y-%m-%d"),
            "deadline_date": (today + timedelta(days=14)).strftime("%Y-%m-%d"),
            "cpv_codes": ["79620000", "85140000"],
            "url": "https://example.com/notice/DEMO-1",
        },
        {
            "id": "DEMO-2",
            "title": "Sprachkurse Deutsch B2 für internationale Pflegekräfte",
            "description_short": "Anpassungsqualifizierung und Sprachförderung für ausländische Pflegefachkräfte.",
            "buyer": "Landesamt für Gesundheit",
            "place_of_performance": "Deutschland",
            "publication_date": (today - timedelta(days=3)).strftime("%Y-%m-%d"),
            "deadline_date": (today + timedelta(days=21)).strftime("%Y-%m-%d"),
            "cpv_codes": ["80580000"],
            "url": "https://example.com/notice/DEMO-2",
        },
        {
            "id": "DEMO-3",
            "title": "Unterhaltsreinigung und Gebäudepflege – Los 3",
            "description_short": "Gebäudepflege und Reinigung diverser Standorte.",
            "buyer": "Landratsamt Beispielkreis",
            "place_of_performance": "Deutschland",
            "publication_date": (today - timedelta(days=5)).strftime("%Y-%m-%d"),
            "deadline_date": (today + timedelta(days=10)).strftime("%Y-%m-%d"),
            "cpv_codes": ["90910000"],
            "url": "https://example.com/notice/DEMO-3",
        },
    ]


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def get_cached_tenders(force_refresh: bool = False,
                       days_back: int = DEFAULT_DAYS_BACK,
                       limit: int = DEFAULT_LIMIT) -> list[dict]:
    now = time.time()
    if force_refresh or (now - _cache["ts"] > CACHE_TTL_SECONDS) or not _cache["data"]:
        items = fetch_ted_items(days_back=days_back, limit=limit)
        scored = []
        for it in items:
            score, breakdown, category, matched = score_tender(
                it.get("title", ""), it.get("description_short", ""), it.get("buyer", "")
            )
            if score > 0:
                scored.append({**it, "matched_keywords": matched, "score": score,
                                "score_breakdown": breakdown, "category_label": category})
        scored.sort(key=lambda x: x["score"], reverse=True)
        _cache["data"] = scored
        _cache["ts"] = now
    return _cache["data"]


# ---------------------------------------------------------------------------
# Frontend HTML
# ---------------------------------------------------------------------------

APP_HTML = """<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>TERN Tender Monitor</title>
  <style>
    :root {
      --bg: #f6f7fb;
      --card: #ffffff;
      --border: #e4e7ef;
      --accent: #2563eb;
      --accent-light: #dbeafe;
      --text: #1e2330;
      --muted: #6b7280;
      --green: #16a34a;
      --amber: #d97706;
      --red: #dc2626;
      --radius: 12px;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
           background: var(--bg); color: var(--text); min-height: 100vh; }

    /* ---- Header ---- */
    header {
      background: var(--card);
      border-bottom: 1px solid var(--border);
      padding: 16px 28px;
      display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
    }
    header h1 { font-size: 1.2rem; font-weight: 700; letter-spacing: -0.3px; }
    .badge { background: var(--accent-light); color: var(--accent);
             font-size: 11px; font-weight: 600; padding: 3px 8px; border-radius: 999px; }
    .data-source { font-size: 12px; color: var(--muted); margin-left: auto; }

    /* ---- Toolbar ---- */
    .toolbar {
      padding: 16px 28px;
      display: flex; gap: 10px; align-items: center; flex-wrap: wrap;
    }
    .toolbar input, .toolbar select {
      padding: 9px 13px; border: 1px solid var(--border); border-radius: 8px;
      font-size: 14px; background: var(--card); color: var(--text); outline: none;
      transition: border-color .15s;
    }
    .toolbar input:focus, .toolbar select:focus { border-color: var(--accent); }
    .toolbar input { min-width: 260px; }
    .btn {
      padding: 9px 16px; border: none; border-radius: 8px; cursor: pointer;
      font-size: 14px; font-weight: 600; transition: opacity .15s;
    }
    .btn:hover { opacity: .85; }
    .btn-primary { background: var(--accent); color: #fff; }
    .btn-ghost { background: var(--card); border: 1px solid var(--border); color: var(--text); }
    .meta { font-size: 13px; color: var(--muted); margin-left: auto; }
    .source-tag { font-size: 11px; padding: 2px 8px; border-radius: 999px;
                  background: #dcfce7; color: #166534; font-weight: 600; }
    .source-demo { background: #fef9c3; color: #854d0e; }

    /* ---- Main content ---- */
    main { padding: 0 28px 40px; }

    /* ---- Cards ---- */
    .cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); gap: 14px; }
    .card {
      background: var(--card); border: 1px solid var(--border);
      border-radius: var(--radius); padding: 18px 20px;
      cursor: pointer; transition: box-shadow .15s, border-color .15s;
    }
    .card:hover { box-shadow: 0 4px 18px rgba(0,0,0,.08); border-color: #c7d3f0; }
    .card.active { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-light); }

    .card-top { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; margin-bottom: 10px; }
    .card-title { font-size: 14px; font-weight: 600; line-height: 1.4; }
    .score-circle {
      min-width: 42px; height: 42px; border-radius: 50%;
      display: flex; align-items: center; justify-content: center;
      font-size: 13px; font-weight: 700; flex-shrink: 0;
    }
    .score-high   { background: #dcfce7; color: #166534; }
    .score-medium { background: #fef9c3; color: #854d0e; }
    .score-low    { background: #fee2e2; color: #991b1b; }

    .card-buyer { font-size: 12px; color: var(--muted); margin-bottom: 8px; }
    .card-meta { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
    .pill {
      display: inline-flex; align-items: center; gap: 4px;
      padding: 3px 9px; border-radius: 999px;
      font-size: 11px; font-weight: 600; background: #f1f5f9; color: #475569;
    }
    .pill-blue   { background: var(--accent-light); color: var(--accent); }
    .pill-green  { background: #dcfce7; color: #166534; }
    .pill-amber  { background: #fef9c3; color: #854d0e; }
    .pill-gray   { background: #f1f5f9; color: #475569; }

    /* ---- Detail panel ---- */
    .detail-panel {
      background: var(--card); border: 1px solid var(--border);
      border-radius: var(--radius); padding: 24px 28px; margin-bottom: 20px;
      display: none;
    }
    .detail-panel h2 { font-size: 1.05rem; font-weight: 700; margin-bottom: 12px; line-height: 1.4; }
    .detail-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; margin-top: 14px; }
    @media (max-width: 700px) { .detail-grid { grid-template-columns: 1fr; } }
    .detail-label { font-size: 11px; font-weight: 700; text-transform: uppercase;
                    letter-spacing: .5px; color: var(--muted); margin-bottom: 4px; }
    .detail-value { font-size: 14px; line-height: 1.5; }
    .score-bar-wrap { display: flex; gap: 6px; flex-direction: column; margin-top: 6px; }
    .score-bar-row { display: flex; align-items: center; gap: 8px; font-size: 12px; }
    .score-bar-label { width: 90px; color: var(--muted); }
    .score-bar-track { flex: 1; height: 6px; background: #e5e7eb; border-radius: 99px; overflow: hidden; }
    .score-bar-fill  { height: 100%; border-radius: 99px; background: var(--accent); }
    .kw-list { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 6px; }

    /* ---- Empty / loading ---- */
    .empty { text-align: center; padding: 60px 20px; color: var(--muted); }
    .spinner {
      width: 32px; height: 32px; border: 3px solid var(--border);
      border-top-color: var(--accent); border-radius: 50%;
      animation: spin .7s linear infinite; margin: 0 auto 12px;
    }
    @keyframes spin { to { transform: rotate(360deg); } }

    a { color: var(--accent); text-decoration: none; }
    a:hover { text-decoration: underline; }
  </style>
</head>
<body>

<header>
  <h1>TERN Tender Monitor</h1>
  <span class="badge">Germany · EU TED</span>
  <span class="data-source" id="dataSourceTag"></span>
</header>

<div class="toolbar">
  <input id="q" type="search" placeholder="Suche nach Titel, Käufer, Beschreibung …" />
  <select id="minScore">
    <option value="40">Min. Score 40</option>
    <option value="60" selected>Min. Score 60</option>
    <option value="70">Min. Score 70</option>
    <option value="80">Min. Score 80</option>
  </select>
  <select id="catFilter">
    <option value="">Alle Kategorien</option>
    <option value="Direct nursing recruitment opportunity">Nursing Recruitment</option>
    <option value="Training / recognition / qualification">Training / Anerkennung</option>
    <option value="Healthcare staffing adjacent">Healthcare Adjacent</option>
  </select>
  <button class="btn btn-primary" id="refreshBtn">↻ Aktualisieren</button>
  <span class="meta" id="metaEl"></span>
</div>

<main>
  <div class="detail-panel" id="detailPanel"></div>
  <div id="cardContainer"><div class="empty"><div class="spinner"></div>Lade Ausschreibungen …</div></div>
</main>

<script>
const qEl        = document.getElementById('q');
const minScoreEl = document.getElementById('minScore');
const catEl      = document.getElementById('catFilter');
const metaEl     = document.getElementById('metaEl');
const detailEl   = document.getElementById('detailPanel');
const container  = document.getElementById('cardContainer');
const sourceTag  = document.getElementById('dataSourceTag');

function esc(s){ return (s||'').toString().replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

function scoreClass(s){ return s >= 75 ? 'score-high' : s >= 55 ? 'score-medium' : 'score-low'; }
function pillClass(cat){
  if (cat.includes('nursing') || cat.includes('Nursing')) return 'pill-green';
  if (cat.includes('Training') || cat.includes('qualif')) return 'pill-blue';
  return 'pill-amber';
}

function formatDate(s){
  if (!s) return '–';
  const d = new Date(s);
  return isNaN(d) ? s : d.toLocaleDateString('de-DE',{day:'2-digit',month:'2-digit',year:'numeric'});
}

function renderBar(label, val, max){
  const pct = Math.round(Math.min(100, (val / max) * 100));
  return `<div class="score-bar-row">
    <span class="score-bar-label">${label}</span>
    <div class="score-bar-track"><div class="score-bar-fill" style="width:${pct}%"></div></div>
    <span style="font-size:11px;color:#6b7280;width:24px;text-align:right">${val}</span>
  </div>`;
}

let _allItems = [];
let _activeId = null;

async function load(refresh = false){
  const q        = qEl.value.trim();
  const minScore = minScoreEl.value;
  const cat      = catEl.value;
  const url      = `/api/tenders?min_score=${encodeURIComponent(minScore)}&q=${encodeURIComponent(q)}&refresh=${refresh?1:0}`;

  container.innerHTML = '<div class="empty"><div class="spinner"></div>Lade …</div>';
  detailEl.style.display = 'none';
  _activeId = null;

  try {
    const res  = await fetch(url);
    const data = await res.json();

    _allItems = data.items || [];

    // filter by category client-side
    const filtered = cat ? _allItems.filter(t => t.category_label === cat) : _allItems;

    const age = data.cache_age_seconds;
    metaEl.textContent = `${filtered.length} Ausschreibungen · Cache: ${age != null ? Math.round(age/60)+'min' : 'fresh'}`;

    const isDemo = filtered.some(t => (t.id||'').startsWith('DEMO'));
    sourceTag.innerHTML = isDemo
      ? '<span class="source-tag source-demo">Demo-Daten (TED nicht erreichbar)</span>'
      : '<span class="source-tag">Live: EU TED</span>';

    if (filtered.length === 0){
      container.innerHTML = '<div class="empty">Keine Ausschreibungen gefunden. Versuche einen niedrigeren Min. Score.</div>';
      return;
    }

    container.innerHTML = '<div class="cards" id="cards"></div>';
    const cardsEl = document.getElementById('cards');

    filtered.forEach(t => {
      const div = document.createElement('div');
      div.className = 'card';
      div.dataset.id = t.id;
      div.innerHTML = `
        <div class="card-top">
          <div class="card-title">${esc(t.title)}</div>
          <div class="score-circle ${scoreClass(t.score)}">${t.score}</div>
        </div>
        <div class="card-buyer">🏛 ${esc(t.buyer || '–')}</div>
        <div class="card-meta">
          <span class="pill ${pillClass(t.category_label)}">${esc(t.category_label)}</span>
          <span class="pill pill-gray">📅 ${formatDate(t.publication_date)}</span>
          ${t.deadline_date ? `<span class="pill pill-gray">⏱ ${formatDate(t.deadline_date)}</span>` : ''}
        </div>
      `;
      div.addEventListener('click', () => showDetail(t, div));
      cardsEl.appendChild(div);
    });

  } catch(e) {
    container.innerHTML = `<div class="empty">Fehler beim Laden: ${esc(e.message)}</div>`;
  }
}

function showDetail(t, cardEl){
  // toggle
  if (_activeId === t.id){
    detailEl.style.display = 'none';
    _activeId = null;
    document.querySelectorAll('.card.active').forEach(c => c.classList.remove('active'));
    return;
  }
  _activeId = t.id;
  document.querySelectorAll('.card.active').forEach(c => c.classList.remove('active'));
  cardEl.classList.add('active');

  const kw = (t.matched_keywords || []);
  const bd = t.score_breakdown || {};

  detailEl.style.display = 'block';
  detailEl.innerHTML = `
    <h2>${esc(t.title)}</h2>
    <div class="detail-grid">
      <div>
        <div class="detail-label">Auftraggeber</div>
        <div class="detail-value">${esc(t.buyer || '–')}</div>
      </div>
      <div>
        <div class="detail-label">Ort</div>
        <div class="detail-value">${esc(t.place_of_performance || '–')}</div>
      </div>
      <div>
        <div class="detail-label">Veröffentlicht</div>
        <div class="detail-value">${formatDate(t.publication_date)}</div>
      </div>
      <div>
        <div class="detail-label">Frist</div>
        <div class="detail-value">${formatDate(t.deadline_date)}</div>
      </div>
      <div>
        <div class="detail-label">Kategorie</div>
        <div class="detail-value"><span class="pill ${pillClass(t.category_label)}">${esc(t.category_label)}</span></div>
      </div>
      <div>
        <div class="detail-label">CPV-Codes</div>
        <div class="detail-value">${(t.cpv_codes||[]).map(c=>`<span class="pill">${esc(c)}</span>`).join(' ') || '–'}</div>
      </div>
      <div>
        <div class="detail-label">Score-Aufschlüsselung</div>
        <div class="score-bar-wrap">
          ${renderBar('Core',         bd.core         || 0, 40)}
          ${renderBar('Recruiting',   bd.recruiting   || 0, 25)}
          ${renderBar('Qualif.',      bd.qualification|| 0, 20)}
          ${renderBar('Buyer Fit',    bd.buyer        || 0, 15)}
          ${bd.penalty ? `<div class="score-bar-row" style="color:#dc2626">Penalty: ${bd.penalty}</div>` : ''}
        </div>
      </div>
      <div>
        <div class="detail-label">Matched Keywords</div>
        <div class="kw-list">
          ${kw.length ? kw.map(k=>`<span class="pill pill-blue">${esc(k)}</span>`).join('') : '<span style="color:#6b7280;font-size:13px">–</span>'}
        </div>
      </div>
      <div style="grid-column:1/-1">
        <div class="detail-label">Beschreibung</div>
        <div class="detail-value" style="line-height:1.6">${esc(t.description_short || '–')}</div>
      </div>
    </div>
    ${t.url ? `<div style="margin-top:16px"><a href="${esc(t.url)}" target="_blank" rel="noopener">→ Ausschreibung öffnen (TED)</a></div>` : ''}
  `;

  detailEl.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

qEl.addEventListener('input',          () => load(false));
minScoreEl.addEventListener('change',  () => load(false));
catEl.addEventListener('change',       () => load(false));
document.getElementById('refreshBtn').addEventListener('click', () => load(true));

load(false);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/", methods=["GET"])
def health():
    return "OK", 200


@app.route("/api/tenders", methods=["GET"])
def api_tenders():
    min_score = int(request.args.get("min_score", "60"))
    q         = _norm(request.args.get("q", ""))
    refresh   = request.args.get("refresh", "0") == "1"

    tenders = get_cached_tenders(force_refresh=refresh)

    filtered = [
        t for t in tenders
        if t["score"] >= min_score and (
            not q or q in _norm(" ".join([t.get("title",""), t.get("description_short",""), t.get("buyer","")]))
        )
    ]

    return jsonify({
        "count": len(filtered),
        "items": filtered,
        "cache_age_seconds": int(time.time() - _cache["ts"]) if _cache["ts"] else None,
    })


@app.route("/app", methods=["GET"])
def web_app():
    return render_template_string(APP_HTML)


# ---------------------------------------------------------------------------
# Local dev entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)

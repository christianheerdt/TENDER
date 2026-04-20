import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import requests
from flask import Flask, jsonify, request, render_template_string

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CACHE_TTL_SECONDS = 30 * 60
DEFAULT_DAYS_BACK = 30
DEFAULT_LIMIT = 250

# TED API v3 – JSON search (primary)
TED_API_URL = "https://api.ted.europa.eu/v3/notices/search"

# TED RSS feed – fallback (different server, highly available)
# Returns Atom XML with notice summaries
TED_RSS_URL = "https://ted.europa.eu/api/v3/notices/rss"

# CPV prefixes to query
HEALTHCARE_CPV_PREFIXES = ["851", "853", "854", "791", "803"]

# Browser-like headers to avoid bot-blocks
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; TERN-TenderMonitor/2.0; "
        "+https://github.com/tern/tender-monitor)"
    ),
    "Accept": "application/json",
    "Content-Type": "application/json",
}

_cache: dict[str, Any] = {"ts": 0, "data": [], "source": "none"}


# ---------------------------------------------------------------------------
# Scoring model
# ---------------------------------------------------------------------------

HARD_KEYWORDS = [
    "pflege", "pflegekraft", "pflegekräfte", "pflegefachkraft", "pflegefachkräfte",
    "pflegefachfrau", "pflegefachmann", "altenpflege", "seniorenpflege",
    "stationäre pflege", "langzeitpflege", "pflegeheim", "pflegedienst",
    "krankenhaus", "klinik", "universitätsklinikum", "gesundheitswesen",
    "medizinisches personal", "rettungsdienst", "hebamme",
    "rekrutierung", "recruiting", "personalgewinnung", "personalvermittlung",
    "personaldienstleistung", "fachkräftegewinnung", "fachkräftemangel",
    "ausländische fachkräfte", "ausland", "international",
    "anerkennung", "kenntnisprüfung", "anpassungslehrgang", "qualifizierung",
    "sprachkurs", "deutsch b2", "integration",
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

_CORE      = ["pflege", "pflegefach", "krankenhaus", "klinik", "altenpflege",
              "langzeitpflege", "pflegeheim", "pflegedienst", "nursing", "nurse", "geriatric"]
_RECRUIT   = ["rekrutierung", "recruit", "personal", "vermittlung",
              "personaldienst", "staffing", "fachkräfte"]
_QUAL      = ["anerkennung", "kenntnisprüfung", "anpassungslehrgang",
              "qualifizierung", "sprachkurs", "deutsch b2", "integration", "recognition"]
_NURSING   = ["pflegefach", "krankenhaus", "klinik", "nursing", "nurse", "altenpflege"]


def _norm(s: str) -> str:
    return (s or "").lower()


def _matches(text: str, kws: list) -> list:
    t = _norm(text)
    return sorted({k for k in kws if k in t})


def score_tender(title: str, desc: str, buyer: str) -> tuple:
    blob   = " ".join([title or "", desc or "", buyer or ""])
    blob_l = _norm(blob)

    matched = _matches(blob, HARD_KEYWORDS)
    zero    = {"core": 0, "recruiting": 0, "qualification": 0, "buyer": 0, "penalty": 0}
    if not matched:
        return 0, zero, "Low relevance (no match)", []

    core         = min(40, sum(1 for t in _CORE    if t in blob_l) * 10)
    recruiting   = min(25, sum(1 for t in _RECRUIT if t in blob_l) * 6)
    qualification = min(20, sum(1 for t in _QUAL   if t in blob_l) * 5)
    buyer_fit    = min(15, sum(1 for t in BUYER_SIGNALS if t in _norm(buyer) or t in blob_l) * 5)

    fp_hits = sum(1 for t in FALSE_POSITIVE_CONTEXT if t in blob_l)
    nursing_present = any(t in blob_l for t in _NURSING)
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

    bd = {"core": core, "recruiting": recruiting, "qualification": qualification,
          "buyer": buyer_fit, "penalty": penalty}
    return total, bd, category, matched


# ---------------------------------------------------------------------------
# TED API  –  primary (JSON)
# ---------------------------------------------------------------------------

def _ted_query_payload(days_back: int, limit: int) -> dict:
    since = (datetime.utcnow() - timedelta(days=days_back)).strftime("%Y%m%d")
    cpv_q = " OR ".join(f"cpv-codes:{p}*" for p in HEALTHCARE_CPV_PREFIXES)
    return {
        "query": f"({cpv_q}) AND publication-date>={since} AND buyer-country-sub:DE",
        "fields": [
            "notice-id", "title-glo", "description-glo",
            "organisation-name-buyer", "publication-date",
            "deadline-date-lot", "cpv-codes",
        ],
        "page": 1,
        "limit": min(limit, 250),
        "sort": {"publication-date": "desc"},
    }


def _pick(val: Any) -> str:
    """Extract string from TED multilingual dict or list."""
    if isinstance(val, dict):
        return val.get("DEU") or val.get("ENG") or next(iter(val.values()), "") or ""
    if isinstance(val, list):
        return " ".join(str(v) for v in val if v)
    return str(val) if val else ""


def fetch_ted_json(days_back: int, limit: int) -> list[dict] | None:
    """
    Call TED v3 JSON API.  Returns list or None on failure.
    Vercel note: deploy to 'fra1' region (Frankfurt) for best connectivity to TED.
    """
    try:
        resp = requests.post(
            TED_API_URL,
            json=_ted_query_payload(days_back, limit),
            headers=_HEADERS,
            timeout=25,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        print(f"[TED-JSON] failed: {exc}")
        return None

    notices = (
        data.get("notices")
        or data.get("results")
        or data.get("items")
        or []
    )
    if not notices:
        print("[TED-JSON] empty result set")
        return None

    items = []
    for n in notices:
        nid = n.get("notice-id") or n.get("id") or ""
        items.append({
            "id": nid,
            "title": _pick(n.get("title-glo")),
            "description_short": (_pick(n.get("description-glo")) or "")[:500],
            "buyer": _pick(n.get("organisation-name-buyer")),
            "place_of_performance": "Deutschland",
            "publication_date": _pick(n.get("publication-date")),
            "deadline_date": _pick(n.get("deadline-date-lot")),
            "cpv_codes": n.get("cpv-codes") if isinstance(n.get("cpv-codes"), list) else [],
            "url": f"https://ted.europa.eu/en/notice/-/detail/{nid}" if nid else "",
            "_source": "ted-api",
        })
    print(f"[TED-JSON] fetched {len(items)} notices")
    return items


# ---------------------------------------------------------------------------
# TED RSS  –  fallback
# ---------------------------------------------------------------------------

def fetch_ted_rss(days_back: int, limit: int) -> list[dict] | None:
    """
    TED RSS/Atom feed – different server, less IP-restricted.
    Filters for healthcare CPV codes and DE country.
    """
    # Build one request per CPV prefix and merge
    items_by_id: dict[str, dict] = {}
    since = (datetime.utcnow() - timedelta(days=days_back)).strftime("%Y%m%d")

    cpv_groups = ["85", "79", "80"]  # top-level grouping for RSS
    for cpv in cpv_groups:
        params = {
            "q": f"cpv-codes:{cpv}* AND buyer-country-sub:DE AND publication-date>={since}",
            "limit": min(limit, 100),
        }
        url = f"{TED_RSS_URL}?{urlencode(params)}"
        try:
            r = requests.get(url, headers={**_HEADERS, "Accept": "application/rss+xml,application/xml,text/xml"}, timeout=20)
            r.raise_for_status()
        except Exception as exc:
            print(f"[TED-RSS] CPV={cpv} failed: {exc}")
            continue

        try:
            root = ET.fromstring(r.content)
        except ET.ParseError as exc:
            print(f"[TED-RSS] XML parse error: {exc}")
            continue

        # Handle both RSS 2.0 and Atom
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        entries = root.findall(".//item") or root.findall(".//atom:entry", ns)
        for entry in entries:
            def g(tag: str) -> str:
                el = entry.find(tag) or entry.find(f"atom:{tag}", ns)
                return (el.text or "").strip() if el is not None else ""

            nid    = g("guid") or g("id") or g("link")
            title  = g("title")
            desc   = g("description") or g("atom:summary")
            buyer  = g("author") or ""
            pubdate = g("pubDate") or g("published") or g("updated")
            link   = g("link")

            # Normalize date
            try:
                pub_parsed = datetime.strptime(pubdate[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
            except Exception:
                pub_parsed = pubdate[:10] if pubdate else ""

            if nid not in items_by_id:
                items_by_id[nid] = {
                    "id": nid,
                    "title": title,
                    "description_short": desc[:500],
                    "buyer": buyer,
                    "place_of_performance": "Deutschland",
                    "publication_date": pub_parsed,
                    "deadline_date": "",
                    "cpv_codes": [],
                    "url": link or nid,
                    "_source": "ted-rss",
                }

    result = list(items_by_id.values())
    if not result:
        return None
    print(f"[TED-RSS] fetched {len(result)} notices via RSS")
    return result


# ---------------------------------------------------------------------------
# Demo fallback
# ---------------------------------------------------------------------------

def _demo_items() -> list[dict]:
    today = datetime.utcnow()
    return [
        {
            "id": "DEMO-1",
            "title": "Personalgewinnung Pflegefachkräfte (international) – Rahmenvertrag",
            "description_short": (
                "Rekrutierung und Integration internationaler Pflegefachkräfte inkl. "
                "Anerkennungsvorbereitung und Begleitung bis zur Kenntnisprüfung. "
                "Sprachkurse Deutsch B2 inklusive. Altenpflege und Krankenhaus."
            ),
            "buyer": "Städtisches Krankenhaus Beispielstadt",
            "place_of_performance": "Deutschland",
            "publication_date": (today - timedelta(days=2)).strftime("%Y-%m-%d"),
            "deadline_date": (today + timedelta(days=14)).strftime("%Y-%m-%d"),
            "cpv_codes": ["79620000", "85140000"],
            "url": "https://ted.europa.eu",
            "_source": "demo",
        },
        {
            "id": "DEMO-2",
            "title": "Sprachkurse Deutsch B2 für internationale Pflegekräfte – Rahmenvereinbarung",
            "description_short": (
                "Anpassungsqualifizierung, Integration und Sprachförderung für ausländische "
                "Pflegefachkräfte. Anerkennung ausländischer Berufsabschlüsse, Qualifizierung."
            ),
            "buyer": "Landesamt für Gesundheit und Pflege",
            "place_of_performance": "Deutschland",
            "publication_date": (today - timedelta(days=3)).strftime("%Y-%m-%d"),
            "deadline_date": (today + timedelta(days=21)).strftime("%Y-%m-%d"),
            "cpv_codes": ["80580000"],
            "url": "https://ted.europa.eu",
            "_source": "demo",
        },
        {
            "id": "DEMO-3",
            "title": "Unterhaltsreinigung und Gebäudepflege – Los 3",
            "description_short": "Gebäudepflege, Reinigung und Wartung diverser Standorte.",
            "buyer": "Landratsamt Beispielkreis",
            "place_of_performance": "Deutschland",
            "publication_date": (today - timedelta(days=5)).strftime("%Y-%m-%d"),
            "deadline_date": (today + timedelta(days=10)).strftime("%Y-%m-%d"),
            "cpv_codes": ["90910000"],
            "url": "https://ted.europa.eu",
            "_source": "demo",
        },
    ]


# ---------------------------------------------------------------------------
# Cache + orchestration
# ---------------------------------------------------------------------------

def fetch_items(days_back: int, limit: int) -> tuple[list[dict], str]:
    """Try TED JSON → TED RSS → Demo. Returns (items, source_label)."""
    items = fetch_ted_json(days_back, limit)
    if items:
        return items, "EU TED (Live)"

    items = fetch_ted_rss(days_back, limit)
    if items:
        return items, "EU TED RSS (Live)"

    print("[FETCH] all live sources failed – using demo data")
    return _demo_items(), "Demo"


def get_cached_tenders(
    force_refresh: bool = False,
    days_back: int = DEFAULT_DAYS_BACK,
    limit: int = DEFAULT_LIMIT,
) -> list[dict]:
    now = time.time()
    stale = (now - _cache["ts"]) > CACHE_TTL_SECONDS

    if force_refresh or stale or not _cache["data"]:
        raw, source_label = fetch_items(days_back, limit)
        scored = []
        for it in raw:
            score, breakdown, category, matched = score_tender(
                it.get("title", ""), it.get("description_short", ""), it.get("buyer", "")
            )
            if score > 0:
                scored.append({
                    **it,
                    "matched_keywords": matched,
                    "score": score,
                    "score_breakdown": breakdown,
                    "category_label": category,
                })
        scored.sort(key=lambda x: x["score"], reverse=True)
        _cache["data"]   = scored
        _cache["ts"]     = now
        _cache["source"] = source_label

    return _cache["data"]


# ---------------------------------------------------------------------------
# Frontend
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
      --radius: 12px;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
           background: var(--bg); color: var(--text); min-height: 100vh; }

    header {
      background: var(--card); border-bottom: 1px solid var(--border);
      padding: 14px 28px; display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
    }
    header h1 { font-size: 1.15rem; font-weight: 700; }
    .badge { background: var(--accent-light); color: var(--accent);
             font-size: 11px; font-weight: 600; padding: 3px 9px; border-radius: 999px; }
    .source-pill {
      margin-left: auto; font-size: 12px; font-weight: 600;
      padding: 4px 10px; border-radius: 999px;
    }
    .src-live  { background: #dcfce7; color: #166534; }
    .src-rss   { background: #e0f2fe; color: #0369a1; }
    .src-demo  { background: #fef9c3; color: #854d0e; }

    .toolbar {
      padding: 14px 28px; display: flex; gap: 10px; align-items: center; flex-wrap: wrap;
      background: var(--card); border-bottom: 1px solid var(--border);
    }
    .toolbar input, .toolbar select {
      padding: 8px 12px; border: 1px solid var(--border); border-radius: 8px;
      font-size: 14px; background: #fff; color: var(--text); outline: none;
    }
    .toolbar input:focus, .toolbar select:focus { border-color: var(--accent); }
    .toolbar input { min-width: 260px; }
    .btn { padding: 8px 16px; border: none; border-radius: 8px; cursor: pointer;
           font-size: 14px; font-weight: 600; }
    .btn-primary { background: var(--accent); color: #fff; }
    .btn-primary:hover { background: #1d4ed8; }
    .meta { font-size: 13px; color: var(--muted); margin-left: auto; }

    main { padding: 20px 28px 60px; }

    /* Detail panel */
    .detail-panel {
      background: var(--card); border: 1px solid var(--border);
      border-radius: var(--radius); padding: 22px 26px; margin-bottom: 18px; display: none;
    }
    .detail-panel h2 { font-size: 1rem; font-weight: 700; margin-bottom: 14px; line-height: 1.45; }
    .detail-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
    @media(max-width:700px){ .detail-grid { grid-template-columns: 1fr; } }
    .dl { font-size: 11px; font-weight: 700; text-transform: uppercase;
          letter-spacing: .4px; color: var(--muted); margin-bottom: 3px; }
    .dv { font-size: 14px; line-height: 1.5; }
    .bar-row { display: flex; align-items: center; gap: 8px; font-size: 12px; margin-bottom: 4px; }
    .bar-lbl  { width: 88px; color: var(--muted); }
    .bar-track { flex: 1; height: 5px; background: #e5e7eb; border-radius: 99px; overflow: hidden; }
    .bar-fill  { height: 100%; border-radius: 99px; background: var(--accent); }
    .kw-wrap { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 5px; }

    /* Cards grid */
    .cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 14px; }

    .card {
      background: var(--card); border: 1px solid var(--border);
      border-radius: var(--radius); padding: 16px 18px; cursor: pointer;
      transition: box-shadow .15s, border-color .15s;
    }
    .card:hover { box-shadow: 0 3px 14px rgba(0,0,0,.08); border-color: #c7d3f0; }
    .card.active { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-light); }

    .card-top { display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; margin-bottom: 9px; }
    .card-title { font-size: 13.5px; font-weight: 600; line-height: 1.4; }

    .score-circle {
      min-width: 40px; height: 40px; border-radius: 50%;
      display: flex; align-items: center; justify-content: center;
      font-size: 13px; font-weight: 700; flex-shrink: 0;
    }
    .sc-hi  { background: #dcfce7; color: #166534; }
    .sc-mid { background: #fef9c3; color: #854d0e; }
    .sc-lo  { background: #fee2e2; color: #991b1b; }

    .card-buyer { font-size: 12px; color: var(--muted); margin-bottom: 8px; }
    .card-meta  { display: flex; gap: 6px; flex-wrap: wrap; }

    .pill {
      display: inline-flex; align-items: center;
      padding: 3px 8px; border-radius: 999px;
      font-size: 11px; font-weight: 600;
    }
    .p-blue   { background: var(--accent-light); color: var(--accent); }
    .p-green  { background: #dcfce7; color: #166534; }
    .p-amber  { background: #fef9c3; color: #854d0e; }
    .p-gray   { background: #f1f5f9; color: #475569; }
    .p-red    { background: #fee2e2; color: #991b1b; }

    .empty { text-align: center; padding: 60px 20px; color: var(--muted); }
    .spinner { width: 30px; height: 30px; border: 3px solid var(--border);
               border-top-color: var(--accent); border-radius: 50%;
               animation: spin .7s linear infinite; margin: 0 auto 10px; }
    @keyframes spin { to { transform: rotate(360deg); } }
    a { color: var(--accent); text-decoration: none; }
    a:hover { text-decoration: underline; }
  </style>
</head>
<body>

<header>
  <h1>TERN Tender Monitor</h1>
  <span class="badge">Germany · EU TED</span>
  <span class="source-pill src-demo" id="srcPill">Lade …</span>
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
  <div id="root"><div class="empty"><div class="spinner"></div>Lade Ausschreibungen …</div></div>
</main>

<script>
const qEl     = document.getElementById('q');
const scoreEl = document.getElementById('minScore');
const catEl   = document.getElementById('catFilter');
const metaEl  = document.getElementById('metaEl');
const detail  = document.getElementById('detailPanel');
const root    = document.getElementById('root');
const srcPill = document.getElementById('srcPill');

function esc(s){ return (s||'').toString().replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function fmtDate(s){
  if(!s) return '–';
  try{ return new Date(s).toLocaleDateString('de-DE',{day:'2-digit',month:'2-digit',year:'numeric'}); }
  catch(e){ return s; }
}
function scClass(n){ return n>=75?'sc-hi':n>=55?'sc-mid':'sc-lo'; }
function pillClass(cat){
  if(cat.includes('nursing')||cat.includes('Nursing')) return 'p-green';
  if(cat.includes('Training')||cat.includes('qualif'))  return 'p-blue';
  return 'p-amber';
}

function bar(label, val, max){
  const pct = Math.round(Math.min(100,(val/max)*100));
  return `<div class="bar-row">
    <span class="bar-lbl">${label}</span>
    <div class="bar-track"><div class="bar-fill" style="width:${pct}%"></div></div>
    <span style="font-size:11px;color:#6b7280;width:22px;text-align:right">${val}</span>
  </div>`;
}

let _active = null;

async function load(refresh=false){
  const q  = qEl.value.trim();
  const ms = scoreEl.value;
  const ct = catEl.value;
  const url = `/api/tenders?min_score=${ms}&q=${encodeURIComponent(q)}&refresh=${refresh?1:0}`;

  root.innerHTML = '<div class="empty"><div class="spinner"></div>Lade …</div>';
  detail.style.display='none'; _active=null;

  let data;
  try {
    const res = await fetch(url);
    data = await res.json();
  } catch(e) {
    root.innerHTML = `<div class="empty">Fehler: ${esc(e.message)}</div>`;
    return;
  }

  // Source pill
  const src = data.source || '';
  if(src.includes('Live')){
    srcPill.className='source-pill src-live';
    srcPill.textContent = src;
  } else if(src.includes('RSS')){
    srcPill.className='source-pill src-rss';
    srcPill.textContent = src;
  } else {
    srcPill.className='source-pill src-demo';
    srcPill.textContent = 'Demo-Daten (TED nicht erreichbar)';
  }

  const items = (data.items||[]).filter(t => !ct || t.category_label===ct);
  const age   = data.cache_age_seconds;
  metaEl.textContent = `${items.length} Ausschreibungen · Cache: ${age!=null?Math.round(age/60)+'min':'fresh'}`;

  if(!items.length){
    root.innerHTML='<div class="empty">Keine Ausschreibungen gefunden. Versuche einen niedrigeren Min. Score.</div>';
    return;
  }

  root.innerHTML='<div class="cards" id="cards"></div>';
  const cards = document.getElementById('cards');

  items.forEach(t => {
    const d = document.createElement('div');
    d.className='card'; d.dataset.id=t.id;
    d.innerHTML=`
      <div class="card-top">
        <div class="card-title">${esc(t.title)}</div>
        <div class="score-circle ${scClass(t.score)}">${t.score}</div>
      </div>
      <div class="card-buyer">🏛 ${esc(t.buyer||'–')}</div>
      <div class="card-meta">
        <span class="pill ${pillClass(t.category_label)}">${esc(t.category_label)}</span>
        <span class="pill p-gray">📅 ${fmtDate(t.publication_date)}</span>
        ${t.deadline_date?`<span class="pill p-gray">⏱ ${fmtDate(t.deadline_date)}</span>`:''}
      </div>`;
    d.addEventListener('click',()=>showDetail(t,d));
    cards.appendChild(d);
  });
}

function showDetail(t, card){
  if(_active===t.id){ detail.style.display='none'; _active=null;
    document.querySelectorAll('.card.active').forEach(c=>c.classList.remove('active')); return; }
  _active=t.id;
  document.querySelectorAll('.card.active').forEach(c=>c.classList.remove('active'));
  card.classList.add('active');

  const bd=t.score_breakdown||{};
  const kw=t.matched_keywords||[];

  detail.style.display='block';
  detail.innerHTML=`
    <h2>${esc(t.title)}</h2>
    <div class="detail-grid">
      <div><div class="dl">Auftraggeber</div><div class="dv">${esc(t.buyer||'–')}</div></div>
      <div><div class="dl">Ort</div><div class="dv">${esc(t.place_of_performance||'–')}</div></div>
      <div><div class="dl">Veröffentlicht</div><div class="dv">${fmtDate(t.publication_date)}</div></div>
      <div><div class="dl">Frist</div><div class="dv">${fmtDate(t.deadline_date)}</div></div>
      <div><div class="dl">Kategorie</div>
           <div class="dv"><span class="pill ${pillClass(t.category_label)}">${esc(t.category_label)}</span></div></div>
      <div><div class="dl">CPV-Codes</div>
           <div class="dv">${(t.cpv_codes||[]).map(c=>`<span class="pill p-gray">${esc(c)}</span>`).join(' ')||'–'}</div></div>
      <div>
        <div class="dl">Score-Aufschlüsselung</div>
        <div style="margin-top:4px">
          ${bar('Core', bd.core||0, 40)}
          ${bar('Recruiting', bd.recruiting||0, 25)}
          ${bar('Qualif.', bd.qualification||0, 20)}
          ${bar('Buyer Fit', bd.buyer||0, 15)}
          ${bd.penalty?`<div style="color:#dc2626;font-size:12px;margin-top:2px">Penalty: ${bd.penalty}</div>`:''}
        </div>
      </div>
      <div>
        <div class="dl">Matched Keywords</div>
        <div class="kw-wrap">
          ${kw.length?kw.map(k=>`<span class="pill p-blue">${esc(k)}</span>`).join(''):'<span style="color:#6b7280;font-size:13px">–</span>'}
        </div>
      </div>
      <div style="grid-column:1/-1">
        <div class="dl">Beschreibung</div>
        <div class="dv" style="line-height:1.6;margin-top:4px">${esc(t.description_short||'–')}</div>
      </div>
    </div>
    ${t.url&&!t.url.includes('example.com')?`<div style="margin-top:14px"><a href="${esc(t.url)}" target="_blank" rel="noopener">→ Ausschreibung auf TED öffnen</a></div>`:''}
  `;
  detail.scrollIntoView({behavior:'smooth',block:'start'});
}

qEl.addEventListener('input',   ()=>load(false));
scoreEl.addEventListener('change',()=>load(false));
catEl.addEventListener('change', ()=>load(false));
document.getElementById('refreshBtn').addEventListener('click',()=>load(true));
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
            not q or q in _norm(" ".join([
                t.get("title", ""), t.get("description_short", ""), t.get("buyer", "")
            ]))
        )
    ]

    return jsonify({
        "count": len(filtered),
        "items": filtered,
        "source": _cache.get("source", "unknown"),
        "cache_age_seconds": int(time.time() - _cache["ts"]) if _cache["ts"] else None,
    })


@app.route("/app", methods=["GET"])
def web_app():
    return render_template_string(APP_HTML)


# ---------------------------------------------------------------------------
# Local dev
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)

"""
Famma Dhaw Monitor - Backend
Scrape famma-dhaw.com et sert les données au dashboard.
Refresh à la demande (pas de scheduler) — compatible Render free tier.
"""

from flask import Flask, jsonify, render_template, request, Response
import requests
from bs4 import BeautifulSoup
import json
import sqlite3
import os
import re
import threading
import logging
from datetime import datetime, timedelta

# ─── Config ───────────────────────────────────────────────
app = Flask(__name__)
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
DB_PATH = os.path.join(DATA_DIR, 'famma_dhaw.db')
SCRAPE_INTERVAL_MIN = 5
SOURCE_URL = 'https://famma-dhaw.com'
PRIORITY_ZONES = [
    'route menzel chaker', 'sfax ville',
    'kairouan nord', 'gabès médina', 'gabes medina'
]

os.makedirs(DATA_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# ─── État global ──────────────────────────────────────────
scrape_lock = threading.Lock()
latest_data = {
    'zones': [], 'stats': {}, 'timestamp': None,
    'success': False, 'error': None,
    'scrape_log': []  # pour debug
}

# ─── Base de données ─────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        total_zones INTEGER, cut_zones INTEGER, ok_zones INTEGER,
        total_reports INTEGER, data_json TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS zone_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        zone_name TEXT, governorate TEXT,
        status TEXT, reports INTEGER DEFAULT 0
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_snap_ts ON snapshots(timestamp)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_zh_ts ON zone_history(timestamp)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_zh_zone ON zone_history(zone_name)')
    conn.commit()
    conn.close()

def save_snapshot(ts, zones):
    cut = sum(1 for z in zones if z['status'] == 'cut')
    ok = sum(1 for z in zones if z['status'] == 'ok')
    reps = sum(z['reports'] for z in zones)
    conn = sqlite3.connect(DB_PATH)
    conn.execute('INSERT INTO snapshots (timestamp,total_zones,cut_zones,ok_zones,total_reports,data_json) VALUES (?,?,?,?,?,?)',
                 (ts, len(zones), cut, ok, reps, json.dumps(zones, ensure_ascii=False)))
    conn.commit()
    conn.close()

def save_zone_history(ts, zones):
    conn = sqlite3.connect(DB_PATH)
    for z in zones:
        conn.execute('INSERT INTO zone_history (timestamp,zone_name,governorate,status,reports) VALUES (?,?,?,?,?)',
                     (ts, z['name'], z['governorate'], z['status'], z['reports']))
    conn.commit()
    conn.close()

def clean_old_data():
    cutoff = (datetime.now() - timedelta(days=30)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.execute('DELETE FROM snapshots WHERE timestamp < ?', (cutoff,))
    conn.execute('DELETE FROM zone_history WHERE timestamp < ?', (cutoff,))
    conn.commit()
    conn.close()

# ─── Scraper multi-stratégie ─────────────────────────────
def scrape_famma_dhaw():
    """
    Stratégies de scraping, par ordre de priorité :
    1. Découverte automatique d'API dans le HTML
    2. Endpoints API courants
    3. Parsing HTML du DOM
    """
    global latest_data
    log = []
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'fr-FR,fr;q=0.9,en-US;q=0.8',
    }

    try:
        # ── Étape 1 : Fetch page principale ──
        log.append(f"FETCH {SOURCE_URL}")
        resp = requests.get(SOURCE_URL, headers=headers, timeout=20, verify=False)
        log.append(f"Status: {resp.status_code}, Size: {len(resp.text)} chars")
        resp.raise_for_status()
        html = resp.text

        zones = None

        # ── Étape 2 : Chercher données JSON inline (Next.js, React, etc.) ──
        log.append("Phase 1: Recherche JSON inline...")
        zones = extract_inline_json(html, log)

        # ── Étape 3 : Découvrir et appeler les APIs ──
        if not zones:
            log.append("Phase 2: Découverte d'API endpoints...")
            api_urls = discover_api_urls(html, log)
            zones = try_api_endpoints(api_urls, headers, log)

        # ── Étape 4 : Essayer endpoints courants ──
        if not zones:
            log.append("Phase 3: Endpoints API courants...")
            common = [
                f'{SOURCE_URL}/api/zones',
                f'{SOURCE_URL}/api/outages',
                f'{SOURCE_URL}/api/data',
                f'{SOURCE_URL}/api/v1/zones',
                f'{SOURCE_URL}/api/v1/outages',
                'https://api.famma-dhaw.com/zones',
                'https://api.famma-dhaw.com/outages',
            ]
            zones = try_api_endpoints(common, headers, log)

        # ── Étape 5 : Parsing HTML du DOM ──
        if not zones:
            log.append("Phase 4: Parsing HTML DOM...")
            zones = parse_html_dom(html, log)

        # ── Résultat ──
        if not zones or len(zones) == 0:
            log.append("ÉCHEC: Aucune zone trouvée avec aucune stratégie")
            latest_data['success'] = False
            latest_data['error'] = 'Aucune zone trouvée sur le site source'
            latest_data['scrape_log'] = log
            return False

        # Normaliser
        processed = normalize_zones(zones)
        log.append(f"SUCCÈS: {len(processed)} zones extraites")

        now = datetime.now().isoformat()
        cut_count = sum(1 for z in processed if z['status'] == 'cut')
        ok_count = sum(1 for z in processed if z['status'] == 'ok')
        total_reports = sum(z['reports'] for z in processed)

        latest_data = {
            'zones': processed,
            'stats': {
                'total_zones': len(processed),
                'cut_zones': cut_count,
                'ok_zones': ok_count,
                'total_reports': total_reports,
                'cut_percentage': round(cut_count / len(processed) * 100, 1) if processed else 0,
            },
            'timestamp': now,
            'success': True,
            'error': None,
            'scrape_log': log
        }

        save_snapshot(now, processed)
        save_zone_history(now, processed)
        clean_old_data()
        return True

    except Exception as e:
        log.append(f"EXCEPTION: {type(e).__name__}: {e}")
        latest_data['success'] = False
        latest_data['error'] = str(e)
        latest_data['scrape_log'] = log
        logger.error(f"Scrape failed: {e}")
        return False


def extract_inline_json(html, log):
    """Cherche des données JSON dans les balises script (Next.js __NEXT_DATA__, etc.)"""
    soup = BeautifulSoup(html, 'html.parser')
    for script in soup.find_all('script'):
        text = script.string or ''
        if not text:
            continue

        # Next.js
        if '__NEXT_DATA__' in text:
            log.append("  Trouvé __NEXT_DATA__")
            try:
                m = re.search(r'__NEXT_DATA__\s*=\s*({.*?})\s*;?\s*$', text, re.DOTALL)
                if m:
                    data = json.loads(m.group(1))
                    zones = deep_find_zones(data)
                    if zones:
                        log.append(f"  {len(zones)} zones depuis __NEXT_DATA__")
                        return zones
            except Exception as e:
                log.append(f"  Parse __NEXT_DATA__ échoué: {e}")

        # window.__INITIAL_STATE__ ou window.__DATA__
        for var in ['__INITIAL_STATE__', '__DATA__', '__APP_DATA__', 'initialData', 'window.zones']:
            if var in text:
                log.append(f"  Trouvé {var}")
                try:
                    m = re.search(r'(?:window\.)?' + re.escape(var) + r'\s*=\s*(\{.*?\}|\[.*?\])\s*;?', text, re.DOTALL)
                    if m:
                        data = json.loads(m.group(1))
                        zones = deep_find_zones(data)
                        if zones:
                            log.append(f"  {len(zones)} zones depuis {var}")
                            return zones
                except Exception as e:
                    log.append(f"  Parse {var} échoué: {e}")

        # Pattern générique: zones = [...] ou zones: [...]
        for pattern in [
            r'(?:var|const|let)\s+zones?\s*=\s*(\[[\s\S]*?\])\s*;',
            r'zones?\s*:\s*(\[[\s\S]*?\])\s*[,}]',
            r'(?:var|const|let)\s+data\s*=\s*(\[[\s\S]*?\])\s*;',
            r'data\s*:\s*(\[[\s\S]*?\])\s*[,}]',
        ]:
            matches = re.findall(pattern, text)
            for match in matches:
                try:
                    data = json.loads(match)
                    if isinstance(data, list) and len(data) > 0:
                        if isinstance(data[0], dict):
                            log.append(f"  {len(data)} zones depuis pattern générique")
                            return data
                except:
                    continue
    return None


def deep_find_zones(obj, depth=0):
    """Cherche récursivement une liste de dicts ressemblant à des zones"""
    if depth > 8:
        return None
    if isinstance(obj, list) and len(obj) > 0 and isinstance(obj[0], dict):
        # Vérifier si ça ressemble à des zones
        sample = obj[0]
        keys = set(sample.keys())
        name_keys = {'name', 'zone', 'zone_name', 'title', 'label', 'nom', 'zoneName'}
        status_keys = {'status', 'etat', 'state', 'is_cut', 'cut', 'outage', 'has_outage', 'hasOutage'}
        if keys & name_keys:
            return obj
        if keys & status_keys and len(obj) > 5:
            return obj
    if isinstance(obj, dict):
        # Priorité: clés courantes pour les zones
        for key in ['zones', 'data', 'results', 'outages', 'regions', 'areas', 'items', 'features']:
            if key in obj:
                result = deep_find_zones(obj[key], depth + 1)
                if result:
                    return result
        # Sinon essayer toutes les valeurs
        for v in obj.values():
            result = deep_find_zones(v, depth + 1)
            if result:
                return result
    return None


def discover_api_urls(html, log):
    """Trouve des URLs d'API dans le code JavaScript"""
    urls = set()
    # Chercher des fetch() ou axios calls
    patterns = [
        r'fetch\s*\(\s*["\']([^"\']+)["\']',
        r'axios\.\w+\s*\(\s*["\']([^"\']+)["\']',
        r'["\'](?:/api/[^"\']+)["\']',
        r'baseURL\s*:\s*["\']([^"\']+)["\']',
        r'api[_-]?url\s*:\s*["\']([^"\']+)["\']',
    ]
    for p in patterns:
        for m in re.findall(p, html):
            if m.startswith('/'):
                m = SOURCE_URL + m
            if 'famma' in m.lower() or m.startswith(SOURCE_URL):
                urls.add(m)
    log.append(f"  {len(urls)} URLs découvertes: {list(urls)[:5]}")
    return list(urls)


def try_api_endpoints(urls, headers, log):
    """Essaie chaque URL et retourne les zones si trouvées"""
    for url in urls:
        try:
            log.append(f"  GET {url}")
            r = requests.get(url, headers=headers, timeout=10, verify=False)
            log.append(f"    → {r.status_code}")
            if r.status_code != 200:
                continue
            data = r.json()
            zones = None
            if isinstance(data, list):
                zones = data
            elif isinstance(data, dict):
                for key in ['zones', 'data', 'results', 'outages', 'items', 'features']:
                    if key in data:
                        val = data[key]
                        if isinstance(val, list):
                            zones = val
                            break
                        elif isinstance(val, dict):
                            # Peut être groupé par gouvernorat
                            all_zones = []
                            for k, v in val.items():
                                if isinstance(v, list):
                                    for item in v:
                                        if isinstance(item, dict):
                                            item['_gov_from_key'] = k
                                            all_zones.append(item)
                            if all_zones:
                                zones = all_zones
                                break
            if zones and len(zones) > 0:
                log.append(f"    → {len(zones)} zones trouvées!")
                return zones
        except Exception as e:
            log.append(f"    → Erreur: {e}")
    return None


def parse_html_dom(html, log):
    """Parse le DOM HTML pour trouver des zones"""
    soup = BeautifulSoup(html, 'html.parser')
    zones = []

    # Chercher des éléments avec des classes/attributs suggestifs
    selectors = [
        {'tag': 'div', 'class_re': r'zone|region|area|marker|pin|outage|coupure'},
        {'tag': 'li', 'class_re': r'zone|region|area|outage'},
        {'tag': 'span', 'class_re': r'zone|region|status|badge'},
        {'tag': 'tr', 'class_re': r'zone|region|row'},
    ]

    seen = set()
    for sel in selectors:
        elements = soup.find_all(sel['tag'], class_=re.compile(sel['class_re'], re.I))
        for el in elements:
            name = el.get_text(strip=True)
            name = re.sub(r'\s+', ' ', name).strip()
            if not name or len(name) < 3 or name in seen:
                continue
            seen.add(name)

            status = 'ok'
            el_class = ' '.join(el.get('class', []))
            el_html = str(el)
            if re.search(r'cut|coup|outage|red|off|alert|danger', el_class + el_html, re.I):
                status = 'cut'

            # Chercher nombre de signalements
            reports = 0
            num_match = re.search(r'(\d+)\s*(?:signalement|vote|report|signal)', el_html, re.I)
            if num_match:
                reports = int(num_match.group(1))

            zones.append({'name': name, 'status': status, 'reports': reports})
            if len(zones) > 500:
                break
        if len(zones) > 10:
            break

    log.append(f"  {len(zones)} zones depuis le DOM")
    return zones if zones else None


def normalize_zones(raw_zones):
    """Normalise les zones dans un format uniforme"""
    processed = []
    for z in raw_zones:
        if not isinstance(z, dict):
            continue

        # Nom
        gov_from_key = z.pop('_gov_from_key', '')
        name = (z.get('name') or z.get('zone') or z.get('zone_name') or
                z.get('title') or z.get('label') or z.get('nom') or
                z.get('zoneName') or '').strip()
        if not name:
            continue

        # Gouvernorat
        gov = (z.get('governorate') or z.get('gov') or z.get('region') or
               z.get('gouvernorat') or z.get('state') or gov_from_key or '').strip()

        # Statut
        status_raw = str(z.get('status') or z.get('etat') or z.get('state') or '').lower()
        is_cut_val = z.get('is_cut') or z.get('cut') or z.get('outage') or z.get('has_outage') or z.get('hasOutage')

        if status_raw in ('cut', 'coupé', 'coupe', 'outage', 'off', '1', 'true', 'coupée'):
            status = 'cut'
        elif status_raw in ('ok', 'normal', 'on', '0', 'false', 'active'):
            status = 'ok'
        elif is_cut_val in (True, 'true', '1', 1):
            status = 'cut'
        elif is_cut_val in (False, 'false', '0', 0):
            status = 'ok'
        else:
            # Heuristique: chercher des mots-clés
            if any(kw in status_raw for kw in ['coup', 'cut', 'off', 'panne']):
                status = 'cut'
            else:
                status = 'ok'

        # Signalements
        reports = z.get('reports') or z.get('votes') or z.get('count') or z.get('signalements') or z.get('nbSignalement') or 0
        try:
            reports = int(reports)
        except (ValueError, TypeError):
            reports = 0

        processed.append({
            'name': name,
            'governorate': gov,
            'status': status,
            'reports': max(0, reports),
            'is_priority': any(pz in name.lower() for pz in PRIORITY_ZONES)
        })

    return processed


# ─── Refresh à la demande ────────────────────────────────
def ensure_fresh():
    """Vérifie si les données sont périmées et scrape si nécessaire"""
    with scrape_lock:
        if latest_data.get('timestamp'):
            try:
                last = datetime.fromisoformat(latest_data['timestamp'])
                age = (datetime.now() - last).total_seconds()
                if age > SCRAPE_INTERVAL_MIN * 60:
                    logger.info(f"Data stale ({age:.0f}s old), refreshing...")
                    scrape_famma_dhaw()
            except:
                scrape_famma_dhaw()
        else:
            logger.info("No data yet, scraping...")
            scrape_famma_dhaw()


# ─── Routes API ───────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/zones')
def api_zones():
    ensure_fresh()
    return jsonify({
        'zones': latest_data.get('zones', []),
        'timestamp': latest_data.get('timestamp'),
        'success': latest_data.get('success', False),
        'error': latest_data.get('scrape_error')
    })

@app.route('/api/stats')
def api_stats():
    ensure_fresh()
    stats = dict(latest_data.get('stats', {}))
    stats['timestamp'] = latest_data.get('timestamp')
    stats['success'] = latest_data.get('success', False)
    stats['error'] = latest_data.get('error')

    zones = latest_data.get('zones', [])

    # Stats par gouvernorat
    gov_stats = {}
    for z in zones:
        gov = z.get('governorate') or 'Non spécifié'
        if gov not in gov_stats:
            gov_stats[gov] = {'total': 0, 'cut': 0, 'ok': 0, 'reports': 0}
        gov_stats[gov]['total'] += 1
        gov_stats[gov][z['status']] += 1
        gov_stats[gov]['reports'] += z['reports']

    govs = {k: v for k, v in gov_stats.items() if v['total'] > 0}
    if govs:
        most = max(govs.items(), key=lambda x: x[1]['cut'])
        least = min(govs.items(), key=lambda x: x[1]['cut'])
        stats['most_cut_gov'] = {'name': most[0], **most[1]}
        stats['least_cut_gov'] = {'name': least[0], **least[1]}

    # Route Menzel Chaker
    mc = [z for z in zones if 'menzel chaker' in z['name'].lower()]
    if mc:
        stats['menzel_chaker'] = {'name': mc[0]['name'], 'status': mc[0]['status'],
                                   'reports': mc[0]['reports'], 'is_cut': mc[0]['status'] == 'cut'}
    stats['governorates'] = gov_stats
    return jsonify(stats)

@app.route('/api/history')
def api_history():
    ensure_fresh()
    hours = request.args.get('hours', 24, type=int)
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        'SELECT timestamp, total_zones, cut_zones, ok_zones, total_reports FROM snapshots WHERE timestamp >= ? ORDER BY timestamp',
        (cutoff,)).fetchall()
    conn.close()
    return jsonify({'history': [{'timestamp': r[0], 'total': r[1], 'cut': r[2], 'ok': r[3], 'reports': r[4]} for r in rows]})

@app.route('/api/top-regions')
def api_top_regions():
    ensure_fresh()
    hours = request.args.get('hours', 24, type=int)
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute('''
        SELECT zone_name, COUNT(*) as total, SUM(CASE WHEN status='cut' THEN 1 ELSE 0 END) as cuts
        FROM zone_history WHERE timestamp >= ? GROUP BY zone_name HAVING total > 1 ORDER BY cuts DESC
    ''', (cutoff,)).fetchall()
    conn.close()

    regions = [{'name': r[0], 'cut_hours': round(r[2] * SCRAPE_INTERVAL_MIN / 60, 1),
                'cut_checks': r[2], 'total': r[1],
                'pct': round(r[2] / r[1] * 100, 1) if r[1] > 0 else 0} for r in rows]
    return jsonify({'most': regions[:10], 'least': sorted(regions, key=lambda x: x['cut_hours'])[:10]})

@app.route('/api/heatmap')
def api_heatmap():
    ensure_fresh()
    hours = request.args.get('hours', 24, type=int)
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute('''
        SELECT COALESCE(governorate,'Non spécifié') as gov,
               CAST(strftime('%H',timestamp) AS INTEGER) as hr,
               COUNT(*) as t, SUM(CASE WHEN status='cut' THEN 1 ELSE 0 END) as c
        FROM zone_history WHERE timestamp >= ? GROUP BY gov, hr
    ''', (cutoff,)).fetchall()
    conn.close()

    heatmap = {}
    for r in rows:
        heatmap.setdefault(r[0], {})[r[1]] = round(r[3] / r[2], 2) if r[2] > 0 else 0
    return jsonify({'heatmap': heatmap})

@app.route('/api/hourly')
def api_hourly():
    ensure_fresh()
    hours = request.args.get('hours', 24, type=int)
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute('''
        SELECT CAST(strftime('%H',timestamp) AS INTEGER) as hr,
               COUNT(*) as t, SUM(CASE WHEN status='cut' THEN 1 ELSE 0 END) as c
        FROM zone_history WHERE timestamp >= ? GROUP BY hr ORDER BY hr
    ''', (cutoff,)).fetchall()
    conn.close()
    return jsonify({'pattern': [{'hour': r[0], 'total': r[1], 'cut': r[2],
                                 'pct': round(r[2]/r[1]*100, 1) if r[1] > 0 else 0,
                                 'period': 'pointe' if 14 <= r[0] <= 22 else 'creuse'} for r in rows]})

@app.route('/api/forecast')
def api_forecast():
    ensure_fresh()
    zone = request.args.get('zone', 'Menzel Chaker')
    cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute('''
        SELECT timestamp, status FROM zone_history
        WHERE zone_name LIKE ? AND timestamp >= ? ORDER BY timestamp
    ''', (f'%{zone}%', cutoff)).fetchall()
    conn.close()

    if not rows:
        return jsonify({'zone': zone, 'error': 'Pas de données historiques', 'observed': [], 'forecast': []})

    # Agréger par heure
    hourly = {}
    for ts, st in rows:
        key = datetime.fromisoformat(ts).strftime('%Y-%m-%d %H:00')
        hourly.setdefault(key, []).append(1 if st == 'cut' else 0)

    avg_hourly = {k: sum(v)/len(v) for k, v in hourly.items()}
    sorted_h = sorted(avg_hourly.items())

    # EWMA α=0.3
    alpha = 0.3
    ewma = sorted_h[0][1]
    observed = []
    for i, (ts, val) in enumerate(sorted_h):
        ewma = alpha * val + (1 - alpha) * ewma
        observed.append({'time': ts, 'value': round(val, 3), 'ewma': round(ewma, 3), 'type': 'observed'})

    # Prédiction 6h
    now = datetime.now()
    forecast = []
    last_ewma = ewma
    for h in range(1, 7):
        fhr = (now.hour + h) % 24
        factor = 1.35 if 14 <= fhr <= 22 else 0.78
        pred = min(1.0, max(0.0, last_ewma * factor))
        ft = (now + timedelta(hours=h)).strftime('%Y-%m-%d %H:00')
        forecast.append({'time': ft, 'value': round(pred, 3), 'type': 'forecast'})
        last_ewma = alpha * pred + (1 - alpha) * last_ewma

    cut_entries = [r for r in rows if r[1] == 'cut']
    cut_hours = round(len(cut_entries) * SCRAPE_INTERVAL_MIN / 60, 1)

    next_cut = next((f['time'] for f in forecast if f['value'] > 0.5), None)
    conf = 'high' if len(sorted_h) >= 48 else ('medium' if len(sorted_h) >= 12 else 'low')

    return jsonify({'zone': zone, 'observed': observed, 'forecast': forecast,
                    'cut_hours_24h': cut_hours, 'next_cut': next_cut,
                    'confidence': conf, 'margin': '±15%' if conf != 'low' else '±22%'})

@app.route('/api/alerts')
def api_alerts():
    ensure_fresh()
    zones = latest_data.get('zones', [])
    stats = latest_data.get('stats', {})
    alerts = []
    pct = stats.get('cut_percentage', 0)

    if pct >= 60:
        alerts.append({'type': 'critical', 'msg': f'ALERTE CRITIQUE : {pct}% des zones sont coupées !', 'ts': latest_data.get('timestamp')})
    elif pct >= 40:
        alerts.append({'type': 'warning', 'msg': f'Attention : {pct}% des zones sont coupées', 'ts': latest_data.get('timestamp')})

    for z in zones:
        if z['is_priority'] and z['status'] == 'cut':
            alerts.append({'type': 'priority', 'msg': f'⚡ {z["name"]} est COUPÉE ({z["reports"]} signalements)', 'ts': latest_data.get('timestamp')})

    return jsonify({'alerts': alerts})

@app.route('/api/report')
def api_report():
    ensure_fresh()
    cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute('SELECT AVG(cut_zones), MAX(cut_zones), MIN(cut_zones), COUNT(*) FROM snapshots WHERE timestamp >= ?', (cutoff,)).fetchone()
    worst = conn.execute('SELECT governorate, SUM(CASE WHEN status=\'cut\' THEN 1 ELSE 0 END), COUNT(*) FROM zone_history WHERE timestamp >= ? AND governorate IS NOT NULL AND governorate != \'\' GROUP BY governorate ORDER BY 2 DESC LIMIT 1', (cutoff,)).fetchone()
    best = conn.execute('SELECT governorate, SUM(CASE WHEN status=\'cut\' THEN 1 ELSE 0 END), COUNT(*) FROM zone_history WHERE timestamp >= ? AND governorate IS NOT NULL AND governorate != \'\' GROUP BY governorate ORDER BY 2 ASC LIMIT 1', (cutoff,)).fetchone()
    conn.close()

    avg = round(row[0], 1) if row[0] else 0
    if avg >= 60:
        sev, col, rec = 'critique', '#e8392b', 'Situation critique. Évitez les déplacements. Préparez des solutions de secours.'
    elif avg >= 35:
        sev, col, rec = 'modérée', '#f5c518', 'Coupures fréquentes. Planifiez en dehors des heures de pointe (14h-22h).'
    else:
        sev, col, rec = 'faible', '#1db954', 'Situation normale. Quelques coupures localisées possibles.'

    return jsonify({
        'generated': datetime.now().isoformat(), 'severity': sev, 'color': col,
        'avg_cut': avg, 'max_cut': row[1] or 0, 'min_cut': row[2] or 0, 'snapshots': row[3] or 0,
        'worst': {'name': worst[0], 'cuts': worst[1], 'total': worst[2]} if worst else None,
        'best': {'name': best[0], 'cuts': best[1], 'total': best[2]} if best else None,
        'recommendation': rec,
        'mc_advice': 'Évitez la Route Menzel Chaker entre 14h et 22h.' if sev in ('critique', 'modérée') else 'Passage possible sans risque majeur.'
    })

@app.route('/api/export')
def api_export():
    ensure_fresh()
    zones = latest_data.get('zones', [])
    lines = ['Zone,Gouvernorat,Statut,Signalements,Prioritaire']
    for z in zones:
        lines.append(f'"{z["name"]}","{z["governorate"]}","{z["status"]}",{z["reports"]},{"Oui" if z["is_priority"] else "Non"}')
    return Response('\n'.join(lines), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename=famma-dhaw-export.csv'})

@app.route('/api/refresh', methods=['POST'])
def api_refresh():
    with scrape_lock:
        success = scrape_famma_dhaw()
    return jsonify({'success': success, 'timestamp': latest_data.get('timestamp'),
                    'zones': len(latest_data.get('zones', [])), 'error': latest_data.get('error')})

@app.route('/api/debug')
def api_debug():
    """Endpoint de debug — montre le détail du dernier scrape"""
    return jsonify({
        'timestamp': latest_data.get('timestamp'),
        'success': latest_data.get('success'),
        'error': latest_data.get('error'),
        'zones_count': len(latest_data.get('zones', [])),
        'stats': latest_data.get('stats'),
        'scrape_log': latest_data.get('scrape_log', []),
        'sample_zones': latest_data.get('zones', [])[:5]
    })


# ─── Startup ──────────────────────────────────────────────
if __name__ == '__main__':
    init_db()
    # Premier scrape au démarrage
    logger.info("Starting Famma Dhaw Monitor...")
    scrape_famma_dhaw()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
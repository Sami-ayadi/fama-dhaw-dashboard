"""
Famma Dhaw Monitor - Backend
Interroge directement l'API Supabase publique utilisée par famma-dhaw.com.
Aucun scraping HTML nécessaire — rapide et fiable.
"""

from flask import Flask, jsonify, render_template, request, Response
import requests
import json
import sqlite3
import os
import numpy as np
from sklearn.ensemble import RandomForestClassifier
import threading
import logging
import time as time_module
from datetime import datetime, timedelta, timezone

# ─── Config ───────────────────────────────────────────────
app = Flask(__name__)
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
DB_PATH = os.path.join(DATA_DIR, 'famma_dhaw.db')
SCRAPE_INTERVAL_MIN = 5

# Tunisie = UTC+1 (pas de DST)
TZ_TUNIS = timezone(timedelta(hours=1))

def now_tn():
    """Heure actuelle en Tunisie (datetime naïf pour compat SQLite)."""
    return datetime.now(TZ_TUNIS).replace(tzinfo=None)

# ─── API Supabase (publique, lecture seule) ───────────────
SUPABASE_URL = 'https://njfulpklvqezflxiozhn.supabase.co'
SUPABASE_API_KEY = 'sb_publishable_C_7rg0jf6-e925Tji5n-qA_mLYruFUp'
ZONES_ENDPOINT = f'{SUPABASE_URL}/rest/v1/zone_board_weighted?select=*'
STATS_ENDPOINT = f'{SUPABASE_URL}/rest/v1/platform_stats?select=total_reports'

PRIORITY_KEYWORDS = [
    'menzel chaker', 'sfax ville', 'kairouan',
    'gabès', 'gabes'
]

os.makedirs(DATA_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# ─── État global ──────────────────────────────────────────
scrape_lock = threading.Lock()
latest_data = {
    'zones': [], 'stats': {}, 'timestamp': None,
    'success': False, 'error': None,
    'scrape_log': [], 'last_attempt': None
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
    conn.execute(
        'INSERT INTO snapshots (timestamp,total_zones,cut_zones,ok_zones,total_reports,data_json) VALUES (?,?,?,?,?,?)',
        (ts, len(zones), cut, ok, reps, json.dumps(zones, ensure_ascii=False))
    )
    conn.commit()
    conn.close()

def save_zone_history(ts, zones):
    conn = sqlite3.connect(DB_PATH)
    for z in zones:
        conn.execute(
            'INSERT INTO zone_history (timestamp,zone_name,governorate,status,reports) VALUES (?,?,?,?,?)',
            (ts, z['name'], z['governorate'], z['status'], z['reports'])
        )
    conn.commit()
    conn.close()

def clean_old_data():
    cutoff = (now_tn() - timedelta(days=30)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.execute('DELETE FROM snapshots WHERE timestamp < ?', (cutoff,))
    conn.execute('DELETE FROM zone_history WHERE timestamp < ?', (cutoff,))
    conn.commit()
    conn.close()

# ─── Scraper (appel direct API Supabase) ──────────────────
def scrape_famma_dhaw():
    """Interroge l'API Supabase — même source que famma-dhaw.com."""
    global latest_data
    log = []

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
        'Authorization': f'Bearer {SUPABASE_API_KEY}',
        'Apikey': SUPABASE_API_KEY,
        'Accept': '*/*',
        'Accept-Profile': 'public',
    }

    try:
        # ── 1. Récupérer les zones ──
        log.append(f"GET {ZONES_ENDPOINT}")
        resp = requests.get(ZONES_ENDPOINT, headers=headers, timeout=15)
        log.append(f"Status: {resp.status_code}, Size: {len(resp.text)} chars")
        resp.raise_for_status()

        raw_zones = resp.json()
        log.append(f"Received {len(raw_zones)} zones from Supabase")

        if not raw_zones or not isinstance(raw_zones, list):
            raise Exception(f"Réponse invalide: type={type(raw_zones)}, len={len(raw_zones) if isinstance(raw_zones, list) else 'N/A'}")

        # ── 2. Normaliser ──
        processed = []
        for z in raw_zones:
            if not isinstance(z, dict):
                continue

            name = (z.get('name') or '').strip()
            if not name:
                continue

            gov = (z.get('gov') or '').strip()
            off_count = int(z.get('off_count') or 0)
            on_count = int(z.get('on_count') or 0)

            # Statut : coupé si plus de signalements "off" que "on"
            status = 'cut' if off_count > on_count else 'ok'
            reports = off_count + on_count

            processed.append({
                'name': name,
                'slug': z.get('slug', ''),
                'governorate': gov,
                'status': status,
                'reports': reports,
                'off_count': off_count,
                'on_count': on_count,
                'last_report': z.get('last_report'),
                'is_priority': any(kw in name.lower() for kw in PRIORITY_KEYWORDS)
            })

        if not processed:
            raise Exception("Aucune zone extraite de la réponse API")

        # ── 3. Calculer les stats ──
        cut_count = sum(1 for z in processed if z['status'] == 'cut')
        ok_count = sum(1 for z in processed if z['status'] == 'ok')
        total_reports = sum(z['reports'] for z in processed)

        now = now_tn().isoformat()

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
            'scrape_log': log,
            'last_attempt': now
        }

        # ── 4. Sauvegarder en DB ──
        save_snapshot(now, processed)
        save_zone_history(now, processed)
        clean_old_data()

        logger.info(f"Scrape OK: {len(processed)} zones, {cut_count} coupées ({latest_data['stats']['cut_percentage']}%)")
        return True

    except Exception as e:
        log.append(f"ERROR: {type(e).__name__}: {e}")
        latest_data['success'] = False
        latest_data['error'] = str(e)
        latest_data['scrape_log'] = log
        latest_data['last_attempt'] = now_tn().isoformat()
        logger.error(f"Scrape échoué: {e}")
        return False

# ─── Refresh à la demande ────────────────────────────────
def ensure_fresh():
    """Scrape si les données ont plus de 5 minutes."""
    with scrape_lock:
        now = now_tn()

        # Données encore fraîches ?
        if latest_data.get('timestamp'):
            try:
                last = datetime.fromisoformat(latest_data['timestamp'])
                age = (now - last).total_seconds()
                if age < SCRAPE_INTERVAL_MIN * 60:
                    return
            except:
                pass

        # Ne pas réessayer trop vite après un échec
        if latest_data.get('last_attempt'):
            try:
                attempt = datetime.fromisoformat(latest_data['last_attempt'])
                if (now - attempt).total_seconds() < 60:
                    return
            except:
                pass

        scrape_famma_dhaw()

# ─── Scheduler en arrière-plan ────────────────────────────
def start_background_scheduler():
    """Démarre un thread qui scrape toutes les 5 minutes."""
    def run():
        # Attendre 10s au démarrage pour laisser l'app se lancer
        time_module.sleep(10)
        while True:
            try:
                with scrape_lock:
                    scrape_famma_dhaw()
            except Exception as e:
                logger.error(f"Scheduler error: {e}")
            time_module.sleep(SCRAPE_INTERVAL_MIN * 60)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    logger.info(f"Background scheduler démarré (toutes les {SCRAPE_INTERVAL_MIN} min)")

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
        'error': latest_data.get('error')
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
        most = max(govs.items(), key=lambda x: x[1]['cut'] / x[1]['total'] if x[1]['total'] > 0 else 0)
        least = min(govs.items(), key=lambda x: x[1]['cut'] / x[1]['total'] if x[1]['total'] > 0 else 0)
        stats['most_cut_gov'] = {'name': most[0], **most[1]}
        stats['least_cut_gov'] = {'name': least[0], **least[1]}

    # Route Menzel Chaker
    mc = [z for z in zones if 'menzel chaker' in z['name'].lower()]
    if mc:
        stats['menzel_chaker'] = {
            'name': mc[0]['name'],
            'status': mc[0]['status'],
            'reports': mc[0]['reports'],
            'off_count': mc[0].get('off_count', 0),
            'on_count': mc[0].get('on_count', 0),
            'is_cut': mc[0]['status'] == 'cut'
        }

    stats['governorates'] = gov_stats
    return jsonify(stats)

@app.route('/api/history')
def api_history():
    ensure_fresh()
    hours = request.args.get('hours', 24, type=int)
    cutoff = (now_tn() - timedelta(hours=hours)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        'SELECT timestamp, total_zones, cut_zones, ok_zones, total_reports FROM snapshots WHERE timestamp >= ? ORDER BY timestamp',
        (cutoff,)).fetchall()
    conn.close()
    return jsonify({'history': [
        {'timestamp': r[0], 'total': r[1], 'cut': r[2], 'ok': r[3], 'reports': r[4]}
        for r in rows
    ]})

@app.route('/api/top-regions')
def api_top_regions():
    ensure_fresh()
    hours = request.args.get('hours', 24, type=int)
    cutoff = (now_tn() - timedelta(hours=hours)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute('''
        SELECT zone_name, COUNT(*) as total,
               SUM(CASE WHEN status='cut' THEN 1 ELSE 0 END) as cuts
        FROM zone_history WHERE timestamp >= ?
        GROUP BY zone_name HAVING total > 1 ORDER BY cuts DESC
    ''', (cutoff,)).fetchall()
    conn.close()

    regions = [{
        'name': r[0],
        'cut_hours': round(r[2] * SCRAPE_INTERVAL_MIN / 60, 1),
        'cut_checks': r[2], 'total': r[1],
        'pct': round(r[2] / r[1] * 100, 1) if r[1] > 0 else 0
    } for r in rows]
    return jsonify({
        'most': regions[:10],
        'least': sorted(regions, key=lambda x: x['cut_hours'])[:10]
    })

@app.route('/api/heatmap')
def api_heatmap():
    ensure_fresh()
    hours = request.args.get('hours', 24, type=int)
    cutoff = (now_tn() - timedelta(hours=hours)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute('''
        SELECT COALESCE(governorate,'Non spécifié') as gov,
               CAST(strftime('%H',timestamp) AS INTEGER) as hr,
               COUNT(*) as t,
               SUM(CASE WHEN status='cut' THEN 1 ELSE 0 END) as c
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
    cutoff = (now_tn() - timedelta(hours=hours)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute('''
        SELECT CAST(strftime('%H',timestamp) AS INTEGER) as hr,
               COUNT(*) as t,
               SUM(CASE WHEN status='cut' THEN 1 ELSE 0 END) as c
        FROM zone_history WHERE timestamp >= ? GROUP BY hr ORDER BY hr
    ''', (cutoff,)).fetchall()
    conn.close()
    return jsonify({'pattern': [{
        'hour': r[0], 'total': r[1], 'cut': r[2],
        'pct': round(r[2]/r[1]*100, 1) if r[1] > 0 else 0,
        'period': 'pointe' if 14 <= r[0] <= 22 else 'creuse'
    } for r in rows]})



@app.route('/api/forecast')
def api_forecast():
    """
    Modèle de Machine Learning (Random Forest) entraîné pour chaque zone.
    Apprend de l'historique pour prédire la probabilité de coupure.
    """
    ensure_fresh()
    
    zone_query = request.args.get('zone', 'Menzel Chaker')
    # On prend 7 jours d'historique pour entraîner le modèle
    cutoff = (now_tn() - timedelta(days=7)).isoformat()
    
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute('''
        SELECT timestamp, status, reports FROM zone_history
        WHERE zone_name LIKE ? AND timestamp >= ? ORDER BY timestamp ASC
    ''', (f'%{zone_query}%', cutoff)).fetchall()
    conn.close()

    # ── 1. Préparation des données (Feature Engineering) ──
    if not rows or len(rows) < 24:
        # Pas assez de données pour entraîner un ML, on utilise un fallback heuristique
        now = now_tn()
        forecast = []
        for h in range(1, 7):
            fhr = (now.hour + h) % 24
            prob = 0.65 if 14 <= fhr <= 22 else (0.35 if 6 <= fhr < 14 else 0.15)
            if any(kw in zone_query.lower() for kw in PRIORITY_KEYWORDS):
                prob = min(0.95, prob * 1.4)
            ft = (now + timedelta(hours=h)).strftime('%Y-%m-%d %H:00')
            forecast.append({'time': ft, 'probability': round(prob * 100, 1), 'predicted_status': 'COUPE' if prob > 0.5 else 'OK', 'type': 'forecast'})
            
        return jsonify({
            'zone': zone_query, 'observed': [], 'forecast': forecast,
            'cut_hours_24h': 0, 'confidence': 'low', 'margin': '±22%',
            'model_info': 'Données insuffisantes pour ML (fallback utilisé)'
        })

    # Agréger les données par heure pour avoir un signal clair
    hourly_data = {}
    for ts, st, reps in rows:
        try:
            dt = datetime.fromisoformat(ts)
            key = dt.strftime('%Y-%m-%d %H:00')
            if key not in hourly_data:
                hourly_data[key] = {'cut_votes': 0, 'ok_votes': 0, 'reports': 0, 'dt': dt}
            if st == 'cut':
                hourly_data[key]['cut_votes'] += 1
            else:
                hourly_data[key]['ok_votes'] += 1
            hourly_data[key]['reports'] += reps
        except:
            continue

    sorted_hours = sorted(hourly_data.items())
    
    # Création des features (X) et de la cible (y)
    X = []
    y = []
    
    for i in range(1, len(sorted_hours)):
        curr_key, curr_data = sorted_hours[i]
        prev_key, prev_data = sorted_hours[i-1]
        
        dt = curr_data['dt']
        
        # La cible : la zone était-elle coupée à cette heure ? (majorité de votes)
        target = 1 if curr_data['cut_votes'] > curr_data['ok_votes'] else 0
        
        # Les features :
        # 1. Heure (0-23)
        # 2. Jour de la semaine (0-6)
        # 3. Est-ce l'heure de pointe ? (14h-22h)
        # 4. Statut précédent (1 si coupé, 0 sinon)
        # 5. Nombre de signalements précédents
        features = [
            dt.hour,
            dt.weekday(),
            1 if 14 <= dt.hour <= 22 else 0,
            1 if prev_data['cut_votes'] > prev_data['ok_votes'] else 0,
            prev_data['reports']
        ]
        
        X.append(features)
        y.append(target)

    X = np.array(X)
    y = np.array(y)

    # ── 2. Entraînement du modèle ML ──
    # Random Forest est parfait pour ça : il capture les non-linéarités et les règles temporelles
    model = RandomForestClassifier(n_estimators=100, max_depth=5, random_state=42, class_weight='balanced')
    
    # Si le modèle n'a vu qu'une seule classe (toujours OK ou toujours coupé), on ne peut pas apprendre
    if len(np.unique(y)) < 2:
        # Fallback si une seule classe existe
        unique_class = int(y[0])
        prob_func = lambda h: 0.95 if unique_class == 1 else 0.05
    else:
        model.fit(X, y)
        prob_func = lambda features: model.predict_proba([features])[0][1] # Probabilité d'être coupé (classe 1)

    # ── 3. Prédiction pour les 6 prochaines heures ──
    now = now_tn()
    forecast = []
    observed = []
    
    # Récupérer le dernier statut connu pour la prédiction
    last_data = sorted_hours[-1][1]
    last_status = 1 if last_data['cut_votes'] > last_data['ok_votes'] else 0
    last_reports = last_data['reports']

    # Données observées pour le graphique
    for key, data in sorted_hours:
        if (now - data['dt']).total_seconds() <= 24 * 3600:
            observed.append({
                'time': key, 
                'value': 1 if data['cut_votes'] > data['ok_votes'] else 0, 
                'type': 'observed'
            })

    # Génération prédictive
    current_status = last_status
    current_reports = last_reports
    
    for h in range(1, 7):
        f_dt = now + timedelta(hours=h)
        
        features = [
            f_dt.hour,
            f_dt.weekday(),
            1 if 14 <= f_dt.hour <= 22 else 0,
            current_status,
            current_reports
        ]
        
        # Prédiction ML
        prob = float(prob_func(features))
        prob = max(0.02, min(0.98, prob)) # Bornage
        
        # Mise à jour pour le step suivant (le modèle prédit l'état futur, qui devient l'état précédent du step d'après)
        current_status = 1 if prob > 0.5 else 0
        # On estime l'évolution des signalements
        current_reports = int(current_reports * (1.2 if current_status == 1 else 0.8))
        
        ft = f_dt.strftime('%Y-%m-%d %H:00')
        forecast.append({
            'time': ft, 
            'value': round(prob, 3),
            'probability': round(prob * 100, 1),
            'predicted_status': 'COUPE' if prob > 0.5 else 'OK',
            'type': 'forecast'
        })

    # ── 4. Statistiques finales ──
    cut_hours_24h = round(sum(1 for o in observed if o['value'] == 1), 1)
    next_cut = next((f['time'] for f in forecast if f['value'] > 0.5), None)
    
    if len(sorted_hours) >= 100:
        conf, margin = 'high', '±15%'
    elif len(sorted_hours) >= 48:
        conf, margin = 'medium', '±18%'
    else:
        conf, margin = 'low', '±22%'

    return jsonify({
        'zone': zone_query, 
        'observed': observed, 
        'forecast': forecast,
        'cut_hours_24h': cut_hours_24h, 
        'next_cut': next_cut,
        'confidence': conf, 
        'margin': margin,
        'model_info': f'Random Forest (100 arbres) - Entraîné sur {len(X)} points historiques'
    })
@app.route('/api/alerts')
def api_alerts():
    ensure_fresh()
    zones = latest_data.get('zones', [])
    stats = latest_data.get('stats', {})
    alerts = []
    pct = stats.get('cut_percentage', 0)
    ts = latest_data.get('timestamp')

    if pct >= 60:
        alerts.append({
            'type': 'critical',
            'msg': f'🚨 ALERTE CRITIQUE : {pct}% des zones sont coupées !',
            'ts': ts
        })
    elif pct >= 40:
        alerts.append({
            'type': 'warning',
            'msg': f'⚠️ Attention : {pct}% des zones sont coupées',
            'ts': ts
        })

    for z in zones:
        if z.get('is_priority') and z['status'] == 'cut':
            alerts.append({
                'type': 'priority',
                'msg': f'⚡ {z["name"]} est COUPÉE ({z["reports"]} signalements)',
                'ts': ts
            })

    return jsonify({'alerts': alerts})

@app.route('/api/report')
def api_report():
    ensure_fresh()
    cutoff = (now_tn() - timedelta(hours=24)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        'SELECT AVG(cut_zones), MAX(cut_zones), MIN(cut_zones), COUNT(*) FROM snapshots WHERE timestamp >= ?',
        (cutoff,)
    ).fetchone()
    worst = conn.execute('''
        SELECT governorate,
               SUM(CASE WHEN status='cut' THEN 1 ELSE 0 END),
               COUNT(*)
        FROM zone_history WHERE timestamp >= ?
        AND governorate IS NOT NULL AND governorate != ''
        GROUP BY governorate ORDER BY 2 DESC LIMIT 1
    ''', (cutoff,)).fetchone()
    best = conn.execute('''
        SELECT governorate,
               SUM(CASE WHEN status='cut' THEN 1 ELSE 0 END),
               COUNT(*)
        FROM zone_history WHERE timestamp >= ?
        AND governorate IS NOT NULL AND governorate != ''
        GROUP BY governorate ORDER BY 2 ASC LIMIT 1
    ''', (cutoff,)).fetchone()
    conn.close()

    avg = round(row[0], 1) if row[0] else 0
    if avg >= 60:
        sev, col, rec = 'critique', '#e8392b', 'Situation critique. Évitez les déplacements non essentiels.'
    elif avg >= 35:
        sev, col, rec = 'modérée', '#f5c518', 'Coupures fréquentes. Planifiez en dehors des heures de pointe (14h-22h).'
    else:
        sev, col, rec = 'faible', '#1db954', 'Situation normale. Quelques coupures localisées possibles.'

    return jsonify({
        'generated': now_tn().isoformat(),
        'severity': sev, 'color': col,
        'avg_cut': avg, 'max_cut': row[1] or 0, 'min_cut': row[2] or 0,
        'snapshots': row[3] or 0,
        'worst': {'name': worst[0], 'cuts': worst[1], 'total': worst[2]} if worst else None,
        'best': {'name': best[0], 'cuts': best[1], 'total': best[2]} if best else None,
        'recommendation': rec,
        'mc_advice': 'Évitez la Route Menzel Chaker entre 14h et 22h.' if sev in ('critique', 'modérée') else 'Passage possible sans risque majeur.'
    })
@app.route('/api/zones/list')
def api_zones_list():
    """Retourne la liste de toutes les zones disponibles pour la prédiction"""
    ensure_fresh()
    zones = latest_data.get('zones', [])
    return jsonify({
        'zones': sorted([z['name'] for z in zones]),
        'count': len(zones)
    })
@app.route('/api/export')
def api_export():
    ensure_fresh()
    zones = latest_data.get('zones', [])
    lines = ['Zone,Gouvernorat,Statut,Signalements_Coupure,Signalements_OK,Total,Prioritaire']
    for z in zones:
        lines.append(
            f'"{z["name"]}","{z["governorate"]}","{z["status"]}",'
            f'{z.get("off_count",0)},{z.get("on_count",0)},{z["reports"]},'
            f'{"Oui" if z.get("is_priority") else "Non"}'
        )
    return Response('\n'.join(lines), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename=famma-dhaw-export.csv'})

@app.route('/api/refresh', methods=['POST'])
def api_refresh():
    with scrape_lock:
        success = scrape_famma_dhaw()
    return jsonify({
        'success': success,
        'timestamp': latest_data.get('timestamp'),
        'zones': len(latest_data.get('zones', [])),
        'error': latest_data.get('error')
    })

@app.route('/api/debug')
def api_debug():
    return jsonify({
        'timestamp': latest_data.get('timestamp'),
        'success': latest_data.get('success'),
        'error': latest_data.get('error'),
        'zones_count': len(latest_data.get('zones', [])),
        'stats': latest_data.get('stats'),
        'scrape_log': latest_data.get('scrape_log', []),
        'sample_zones': latest_data.get('zones', [])[:3]
    })

# ─── Démarrage ────────────────────────────────────────────
# Initialiser même si importé par gunicorn
init_db()

# Premier scrape au démarrage
scrape_famma_dhaw()

# Scheduler en arrière-plan
start_background_scheduler()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
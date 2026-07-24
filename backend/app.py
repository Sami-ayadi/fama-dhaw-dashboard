"""
Famma Dhaw Monitor - Combined App (API + Static Files)
For easy deployment on Render/Railway
"""
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import sqlite3
import csv
import io
import os
import sys
from datetime import datetime, timedelta
import numpy as np
from sklearn.linear_model import LinearRegression

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scraper import FammaScraper, DB_PATH, PRIORITY_ZONES

app = FastAPI(
    title="Famma Dhaw Monitor",
    description="Monitoring des coupures d'electricite en Tunisie",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve static frontend
frontend_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")
if os.path.exists(frontend_path):
    app.mount("/static", StaticFiles(directory=frontend_path), name="static")

scraper = FammaScraper()

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def get_current_stats():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) as total FROM zones")
    total = c.fetchone()["total"]
    c.execute("SELECT COUNT(*) as cut FROM zones WHERE status = 'COUPE'")
    cut = c.fetchone()["cut"]
    c.execute("SELECT COUNT(*) as partial FROM zones WHERE status = 'PARTIEL'")
    partial = c.fetchone()["partial"]
    c.execute("SELECT SUM(reports_dark) as dark, SUM(reports_light) as light FROM zones")
    reports = c.fetchone()
    c.execute("SELECT MAX(last_updated) as last_update FROM zones")
    last_update = c.fetchone()["last_update"]
    conn.close()
    return {
        "total_zones": total,
        "cut_zones": cut,
        "partial_zones": partial,
        "ok_zones": total - cut - partial,
        "total_reports_dark": reports["dark"] or 0,
        "total_reports_light": reports["light"] or 0,
        "cut_percentage": round(cut / total * 100, 1) if total > 0 else 0,
        "last_update": last_update,
        "data_source": "simulation" if not scraper.last_scrape_success else "famma-dhaw.com"
    }

@app.get("/", response_class=HTMLResponse)
def serve_frontend():
    index_path = os.path.join(frontend_path, "index.html")
    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            return f.read()
    return "<h1>Famma Dhaw Monitor API</h1><p>Frontend not found. API is running at /api/</p>"

@app.get("/api/stats")
def get_stats():
    return get_current_stats()

@app.get("/api/zones")
def get_zones(governorate: str = None, status: str = None, search: str = None):
    conn = get_db_connection()
    c = conn.cursor()
    query = "SELECT * FROM zones WHERE 1=1"
    params = []
    if governorate:
        query += " AND governorate = ?"
        params.append(governorate)
    if status:
        query += " AND status = ?"
        params.append(status)
    if search:
        query += " AND name LIKE ?"
        params.append(f"%{search}%")
    query += " ORDER BY governorate, name"
    c.execute(query, params)
    zones = [dict(row) for row in c.fetchall()]
    conn.close()
    return {"zones": zones, "count": len(zones)}

@app.get("/api/zones/{zone_name}")
def get_zone(zone_name: str):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM zones WHERE name = ?", (zone_name,))
    row = c.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Zone not found")
    return dict(row)

@app.get("/api/governorates")
def get_governorates():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
        SELECT governorate, COUNT(*) as total,
               SUM(CASE WHEN status = 'COUPE' THEN 1 ELSE 0 END) as cut,
               SUM(CASE WHEN status = 'PARTIEL' THEN 1 ELSE 0 END) as partial,
               SUM(reports_dark) as reports_dark,
               SUM(reports_light) as reports_light
        FROM zones GROUP BY governorate ORDER BY cut DESC
    """)
    rows = [dict(row) for row in c.fetchall()]
    conn.close()
    return {"governorates": rows}

@app.get("/api/history")
def get_history(zone_name: str = None, hours: int = 24):
    conn = get_db_connection()
    c = conn.cursor()
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    if zone_name:
        c.execute("SELECT * FROM history WHERE zone_name = ? AND timestamp > ? ORDER BY timestamp DESC", (zone_name, cutoff))
    else:
        c.execute("SELECT * FROM history WHERE timestamp > ? ORDER BY timestamp DESC", (cutoff,))
    rows = [dict(row) for row in c.fetchall()]
    conn.close()
    return {"history": rows, "count": len(rows)}

@app.get("/api/timeline")
def get_timeline(hours: int = 24):
    conn = get_db_connection()
    c = conn.cursor()
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    c.execute("""
        SELECT strftime('%Y-%m-%d %H:00:00', timestamp) as hour,
               COUNT(DISTINCT zone_name) as total_zones,
               SUM(CASE WHEN status = 'COUPE' THEN 1 ELSE 0 END) as cut_count
        FROM history WHERE timestamp > ? GROUP BY hour ORDER BY hour
    """, (cutoff,))
    rows = [dict(row) for row in c.fetchall()]
    conn.close()
    return {"timeline": rows}

@app.get("/api/heatmap")
def get_heatmap(hours: int = 24):
    conn = get_db_connection()
    c = conn.cursor()
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    c.execute("""
        SELECT governorate, strftime('%H', timestamp) as hour,
               COUNT(CASE WHEN status = 'COUPE' THEN 1 END) * 1.0 / COUNT(*) as ratio
        FROM history WHERE timestamp > ? GROUP BY governorate, hour ORDER BY governorate, hour
    """, (cutoff,))
    rows = [dict(row) for row in c.fetchall()]
    conn.close()
    return {"heatmap": rows}

@app.get("/api/top-cut")
def get_top_cut(limit: int = 10, hours: int = 24):
    conn = get_db_connection()
    c = conn.cursor()
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    c.execute("""
        SELECT zone_name, governorate,
               COUNT(CASE WHEN status = 'COUPE' THEN 1 END) as cut_hours,
               COUNT(*) as total_hours
        FROM history WHERE timestamp > ? GROUP BY zone_name HAVING total_hours > 0
        ORDER BY cut_hours DESC LIMIT ?
    """, (cutoff, limit))
    rows = [dict(row) for row in c.fetchall()]
    conn.close()
    return {"top_cut": rows}

@app.get("/api/top-ok")
def get_top_ok(limit: int = 10, hours: int = 24):
    conn = get_db_connection()
    c = conn.cursor()
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    c.execute("""
        SELECT zone_name, governorate,
               COUNT(CASE WHEN status = 'COUPE' THEN 1 END) as cut_hours,
               COUNT(*) as total_hours
        FROM history WHERE timestamp > ? GROUP BY zone_name HAVING total_hours > 0
        ORDER BY cut_hours ASC LIMIT ?
    """, (cutoff, limit))
    rows = [dict(row) for row in c.fetchall()]
    conn.close()
    return {"top_ok": rows}

@app.get("/api/hourly-distribution")
def get_hourly_distribution(hours: int = 24):
    conn = get_db_connection()
    c = conn.cursor()
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    c.execute("""
        SELECT CAST(strftime('%H', timestamp) AS INTEGER) as hour,
               COUNT(CASE WHEN status = 'COUPE' THEN 1 END) as cut_count
        FROM history WHERE timestamp > ? GROUP BY hour ORDER BY hour
    """, (cutoff,))
    rows = [dict(row) for row in c.fetchall()]
    conn.close()
    return {"hourly": rows}

@app.get("/api/peak-vs-offpeak")
def get_peak_vs_offpeak(hours: int = 24):
    conn = get_db_connection()
    c = conn.cursor()
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    c.execute("""
        SELECT 
            SUM(CASE WHEN CAST(strftime('%H', timestamp) AS INTEGER) BETWEEN 14 AND 22 THEN 1 ELSE 0 END) as peak_total,
            SUM(CASE WHEN CAST(strftime('%H', timestamp) AS INTEGER) BETWEEN 14 AND 22 AND status = 'COUPE' THEN 1 ELSE 0 END) as peak_cut,
            SUM(CASE WHEN CAST(strftime('%H', timestamp) AS INTEGER) NOT BETWEEN 14 AND 22 THEN 1 ELSE 0 END) as offpeak_total,
            SUM(CASE WHEN CAST(strftime('%H', timestamp) AS INTEGER) NOT BETWEEN 14 AND 22 AND status = 'COUPE' THEN 1 ELSE 0 END) as offpeak_cut
        FROM history WHERE timestamp > ?
    """, (cutoff,))
    row = dict(c.fetchone())
    conn.close()
    peak_ratio = row["peak_cut"] / row["peak_total"] if row["peak_total"] > 0 else 0
    offpeak_ratio = row["offpeak_cut"] / row["offpeak_total"] if row["offpeak_total"] > 0 else 0
    return {
        "peak": {"total": row["peak_total"], "cut": row["peak_cut"], "ratio": round(peak_ratio * 100, 1)},
        "offpeak": {"total": row["offpeak_total"], "cut": row["offpeak_cut"], "ratio": round(offpeak_ratio * 100, 1)}
    }

@app.get("/api/forecast/{zone_name}")
def get_forecast(zone_name: str, hours: int = 6):
    conn = get_db_connection()
    c = conn.cursor()
    cutoff = (datetime.now() - timedelta(days=7)).isoformat()
    c.execute("""
        SELECT strftime('%Y-%m-%d %H:00:00', timestamp) as hour,
               CASE WHEN status = 'COUPE' THEN 1 ELSE 0 END as is_cut
        FROM history WHERE zone_name = ? AND timestamp > ? GROUP BY hour ORDER BY hour
    """, (zone_name, cutoff))
    rows = c.fetchall()
    conn.close()

    if len(rows) < 12:
        current_hour = datetime.now().hour
        forecast = []
        for i in range(1, hours + 1):
            fh = (current_hour + i) % 24
            if 14 <= fh <= 22:
                prob = 0.65
            elif 6 <= fh < 14:
                prob = 0.35
            else:
                prob = 0.15
            if zone_name == "Route Menzel Chaker":
                prob = min(0.95, prob * 1.5)
            forecast.append({
                "hour": fh,
                "probability": round(prob * 100, 1),
                "predicted_status": "COUPE" if prob > 0.5 else "OK",
                "confidence": "low"
            })
        return {"zone": zone_name, "forecast": forecast, "method": "heuristic"}

    X, y = [], []
    for i, row in enumerate(rows):
        hour = datetime.fromisoformat(row["hour"].replace(" ", "T")).hour
        X.append([i, hour, 1 if 14 <= hour <= 22 else 0])
        y.append(row["is_cut"])
    X, y = np.array(X), np.array(y)
    model = LinearRegression()
    model.fit(X, y)

    forecast = []
    current_hour = datetime.now().hour
    for i in range(1, hours + 1):
        fh = (current_hour + i) % 24
        is_peak = 1 if 14 <= fh <= 22 else 0
        pred = model.predict([[len(rows) + i, fh, is_peak]])[0]
        if is_peak:
            pred *= 1.35
        else:
            pred *= 0.78
        prob = max(0, min(1, pred))
        forecast.append({
            "hour": fh,
            "probability": round(prob * 100, 1),
            "predicted_status": "COUPE" if prob > 0.5 else "OK",
            "confidence": "medium" if len(rows) > 48 else "low"
        })
    return {"zone": zone_name, "forecast": forecast, "method": "linear_regression"}

@app.get("/api/alerts")
def get_alerts(limit: int = 50, unacknowledged_only: bool = False):
    conn = get_db_connection()
    c = conn.cursor()
    query = "SELECT * FROM alerts WHERE 1=1"
    params = []
    if unacknowledged_only:
        query += " AND acknowledged = 0"
    query += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)
    c.execute(query, params)
    rows = [dict(row) for row in c.fetchall()]
    conn.close()
    return {"alerts": rows}

@app.get("/api/priority-zones")
def get_priority_zones():
    conn = get_db_connection()
    c = conn.cursor()
    zones = []
    for zone_name in PRIORITY_ZONES:
        c.execute("SELECT * FROM zones WHERE name = ?", (zone_name,))
        row = c.fetchone()
        if row:
            zones.append(dict(row))
    conn.close()
    return {"priority_zones": zones}

@app.get("/api/report")
def get_report():
    conn = get_db_connection()
    c = conn.cursor()
    cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
    c.execute("""
        SELECT COUNT(DISTINCT zone_name) as total_zones,
               AVG(CASE WHEN status = 'COUPE' THEN 1.0 ELSE 0.0 END) as avg_cut_rate,
               MAX(CASE WHEN status = 'COUPE' THEN 1 ELSE 0 END) as max_cut,
               MIN(CASE WHEN status = 'COUPE' THEN 1 ELSE 0 END) as min_cut
        FROM history WHERE timestamp > ?
    """, (cutoff,))
    stats = dict(c.fetchone())
    c.execute("""
        SELECT governorate, AVG(CASE WHEN status = 'COUPE' THEN 1.0 ELSE 0.0 END) as rate
        FROM history WHERE timestamp > ? GROUP BY governorate ORDER BY rate DESC LIMIT 1
    """, (cutoff,))
    worst = dict(c.fetchone()) if c.fetchone() else {}
    c.execute("""
        SELECT governorate, AVG(CASE WHEN status = 'COUPE' THEN 1.0 ELSE 0.0 END) as rate
        FROM history WHERE timestamp > ? GROUP BY governorate ORDER BY rate ASC LIMIT 1
    """, (cutoff,))
    best = dict(c.fetchone()) if c.fetchone() else {}
    c.execute("SELECT COUNT(*) FROM zones WHERE status = 'COUPE'")
    current_cut = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM zones")
    total = c.fetchone()[0]
    pct = current_cut / total * 100 if total > 0 else 0
    if pct >= 60:
        severity = "CRITIQUE"
    elif pct >= 40:
        severity = "ELEVEE"
    elif pct >= 20:
        severity = "MODEREE"
    else:
        severity = "FAIBLE"
    conn.close()
    return {
        "generated_at": datetime.now().isoformat(),
        "severity": severity,
        "stats_24h": stats,
        "worst_governorate": worst,
        "best_governorate": best,
        "current_cut_percentage": round(pct, 1),
        "recommendations": [
            "Eviter la Route Menzel Chaker entre 14h et 22h" if pct > 30 else "Situation normale",
            "Surveiller les zones prioritaires" if current_cut > 50 else "Aucune alerte particuliere"
        ]
    }

@app.get("/api/export/csv")
def export_csv():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM zones ORDER BY governorate, name")
    rows = c.fetchall()
    conn.close()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Zone", "Gouvernorat", "Statut", "Signalements Sombre", "Signalements Lumiere", "Derniere MAJ"])
    for row in rows:
        writer.writerow([row["name"], row["governorate"], row["status"], row["reports_dark"], row["reports_light"], row["last_updated"]])
    output.seek(0)
    return StreamingResponse(
        io.BytesIO(output.getvalue().encode('utf-8-sig')),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=famma-dhaw-export.csv"}
    )

@app.post("/api/scrape")
def trigger_scrape(background_tasks: BackgroundTasks):
    background_tasks.add_task(scraper.run_scrape)
    return {"message": "Scrape triggered in background"}

@app.get("/api/health")
def health_check():
    return {
        "status": "healthy",
        "last_scrape": scraper.last_scrape.isoformat() if scraper.last_scrape else None,
        "last_scrape_success": scraper.last_scrape_success
    }

if __name__ == "__main__":
    import uvicorn
    # Run initial scrape
    scraper.run_scrape()
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

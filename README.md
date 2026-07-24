# ⚡ Famma Dhaw Monitor

Application web complete de monitoring des coupures d'electricite en Tunisie, basee sur les donnees de [Famma Dhaw](https://famma-dhaw.com).

## 🎯 Fonctionnalites

- **Collecte automatique** : Scraping toutes les 5 minutes de famma-dhaw.com
- **Dashboard live** : KPIs en temps reel, visualisations interactives
- **Previsions ML** : Modele de regression lineaire pour la Route Menzel Chaker
- **Rapports automatiques** : Generation quotidienne avec recommandations
- **Alertes** : Seuils configurables, notifications visuelles
- **Export CSV** : Telechargement des donnees brutes

## 🏗️ Architecture

```
famma-dhaw-monitor/
├── backend/
│   ├── api.py           # API REST FastAPI
│   ├── scraper.py       # Scraper + simulation realiste
│   ├── scheduler.py     # Taches planifiees (5min)
│   └── requirements.txt
├── frontend/
│   └── index.html       # SPA complete (Chart.js)
├── data/
│   └── outages.db       # SQLite
├── Procfile             # Render deployment
└── render.yaml          # Render configuration
```

## 🚀 Installation Locale

```bash
# 1. Cloner le projet
cd famma-dhaw-monitor

# 2. Installer les dependances
pip install -r backend/requirements.txt

# 3. Initialiser la base de donnees
python backend/scraper.py

# 4. Lancer l'API
python backend/api.py

# 5. Lancer le scheduler (dans un autre terminal)
python backend/scheduler.py

# 6. Ouvrir le frontend
# Ouvrir frontend/index.html dans votre navigateur
# ou utiliser un serveur statique:
cd frontend && python -m http.server 3000
```

## 🌐 Deploiement sur Render (Gratuit)

### Option 1: Deploy via Render Dashboard

1. Creer un compte sur [render.com](https://render.com)
2. **New Web Service** → Connecter votre repo GitHub
3. Configurer:
   - **Build Command**: `pip install -r backend/requirements.txt`
   - **Start Command**: `cd backend && uvicorn api:app --host 0.0.0.0 --port $PORT`
   - **Plan**: Free
4. **New Background Worker**:
   - **Build Command**: `pip install -r backend/requirements.txt`
   - **Start Command**: `cd backend && python scheduler.py`
   - **Plan**: Free

### Option 2: Deploy via Blueprint (render.yaml)

1. Pousser le code sur GitHub
2. Sur Render: **New** → **Blueprint**
3. Selectionner votre repo
4. Render cree automatiquement les services

## 📡 API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/stats` | Statistiques globales |
| `GET /api/zones` | Liste des zones (filtres: governorate, status, search) |
| `GET /api/zones/{name}` | Detail d'une zone |
| `GET /api/governorates` | Stats par gouvernorat |
| `GET /api/history` | Historique (param: hours, zone_name) |
| `GET /api/timeline` | Timeline horaire |
| `GET /api/heatmap` | Heatmap gouvernorat x heure |
| `GET /api/top-cut` | Top zones coupees |
| `GET /api/top-ok` | Top zones OK |
| `GET /api/hourly-distribution` | Distribution par heure |
| `GET /api/peak-vs-offpeak` | Comparaison pointe/creuse |
| `GET /api/forecast/{zone}` | Prevision ML sur 6h |
| `GET /api/alerts` | Liste des alertes |
| `GET /api/report` | Rapport quotidien |
| `GET /api/export/csv` | Export CSV |
| `POST /api/scrape` | Declencher un scrape manuel |

## 🎨 Design System

- **Theme**: Dark mode
- **Couleurs**: Rouge (#e8392b) = coupe, Vert (#1db954) = OK, Jaune (#f5c518) = accent
- **Polices**: IBM Plex Mono (donnees), Noto Sans (texte)
- **Responsive**: Mobile + Desktop

## ⚠️ Notes

- Le site famma-dhaw.com peut bloquer les requetes directes. L'application utilise alors des donnees simulees realistes basees sur les patterns historiques du reseau tunisien.
- Le scraper tente toujours de recuperer les vraies donnees en premier.
- Les previsions sont plus precises avec plus d'historique (30 jours recommandes).

## 📄 License

MIT

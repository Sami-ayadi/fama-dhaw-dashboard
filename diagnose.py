"""
Script de diagnostic ONE-SHOT pour comprendre comment famma-dhaw.com charge ses données.
Lance-le UNE FOIS, puis montre-moi le résultat.
"""

import json
import os
import re
from playwright.sync_api import sync_playwright

SOURCE_URL = 'https://famma-dhaw.com'
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'diag')
os.makedirs(OUTPUT_DIR, exist_ok=True)


def save(name, content):
    path = os.path.join(OUTPUT_DIR, name)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    print(f"  💾 Sauvé : {path} ({len(content)} chars)")
    return path


def deep_find_zones(obj, depth=0, path="root"):
    """Cherche récursivement une liste de dicts ressemblant à des zones"""
    if depth > 10:
        return None

    if isinstance(obj, list) and len(obj) > 0 and isinstance(obj[0], dict):
        sample = obj[0]
        keys = set(sample.keys())
        name_keys = {'name', 'zone', 'zone_name', 'title', 'label', 'nom', 'zoneName', 'id'}
        status_keys = {'status', 'etat', 'state', 'is_cut', 'cut', 'outage', 'has_outage', 'hasOutage', 'power'}
        loc_keys = {'lat', 'lng', 'latitude', 'longitude', 'coordinates', 'position'}

        has_name = bool(keys & name_keys)
        has_status = bool(keys & status_keys)
        has_loc = bool(keys & loc_keys)

        print(f"  📋 Liste trouvée à {path} — {len(obj)} items, keys={list(keys)[:8]}...  name={has_name} status={has_status} loc={has_loc}")

        if has_name or (has_status and len(obj) > 3) or (has_loc and len(obj) > 3):
            return {'path': path, 'count': len(obj), 'sample_keys': list(keys), 'sample': obj[:3]}

    if isinstance(obj, dict):
        # Priorité aux clés évidentes
        priority = ['zones', 'data', 'results', 'outages', 'regions', 'areas', 'items',
                     'features', 'markers', 'points', 'locations', 'cuts', 'power_outages']
        for key in priority:
            if key in obj:
                result = deep_find_zones(obj[key], depth + 1, f"{path}.{key}")
                if result:
                    return result
        # Puis toutes les autres clés
        for k, v in obj.items():
            if isinstance(v, (dict, list)) and k not in priority:
                result = deep_find_zones(v, depth + 1, f"{path}.{k}")
                if result:
                    return result
    return None


def main():
    print("=" * 70)
    print("DIAGNOSTIC FAMMA-DHAW.COM")
    print("=" * 70)

    network_log = []

    with sync_playwright() as p:
        print("\n🌐 Lancement Chromium headless...")
        browser = p.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-setuid-sandbox']
        )
        context = browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
            viewport={'width': 1920, 'height': 1080},
            locale='fr-TN'
        )
        page = context.new_page()

        # ── Intercepter TOUT le trafic ──
        def on_request(request):
            entry = {
                'method': request.method,
                'url': request.url,
                'resource_type': request.resource_type,
            }
            network_log.append(entry)
            # N'afficher que les requêtes utiles
            if request.resource_type in ('xhr', 'fetch', 'document'):
                print(f"  → {request.method} [{request.resource_type}] {request.url[:120]}")

        def on_response(response):
            url = response.url
            ct = response.headers.get('content-type', '')
            status = response.status

            if response.request.resource_type in ('xhr', 'fetch'):
                try:
                    body = response.text()
                    print(f"  ← {status} [{ct[:30]}] {url[:100]} ({len(body)} chars)")
                    # Sauvegarder les réponses JSON
                    if 'json' in ct:
                        fname = re.sub(r'[^\w]', '_', url.split('/')[-1]) or 'data'
                        save(f"api_{fname}_{status}.json", body)
                except Exception as e:
                    print(f"  ← {status} {url[:100]} (lecture échouée: {e})")

        page.on('request', on_request)
        page.on('response', on_response)

        # ── Navigation ──
        print(f"\n📍 Navigation vers {SOURCE_URL} ...")
        try:
            page.goto(SOURCE_URL, wait_until='networkidle', timeout=30000)
        except Exception as e:
            print(f"  ⚠ networkidle timeout (normal), on continue: {e}")

        print(f"\n⏳ Attente 5s pour le rendu JS...")
        page.wait_for_timeout(5000)

        # ── HTML rendu ──
        html = page.content()
        save("page_rendue.html", html)
        print(f"\n📄 HTML rendu : {len(html)} chars")

        # ── Chercher dans les variables JS globales ──
        print("\n🔍 Variables JS globales (objets > 100 chars)...")
        try:
            js_vars = page.evaluate('''() => {
                const results = {};
                const skip = ['chrome','webkit','speechSynthesis','performance','navigator',
                              'screen','window','self','document','location','history',
                              'customElements','visualViewport','cssVariables'];
                for (const key of Object.keys(window)) {
                    if (skip.includes(key) || key.startsWith('__') || key.startsWith('on')) continue;
                    try {
                        const val = window[key];
                        if (val && typeof val === 'object') {
                            const s = JSON.stringify(val);
                            if (s.length > 100 && s.length < 2000000) {
                                results[key] = { size: s.length, preview: s.substring(0, 500) };
                            }
                        } else if (typeof val === 'function' && val.toString().length > 200) {
                            // Skip functions
                        }
                    } catch(e) {}
                }
                return results;
            }''')

            for name, info in sorted(js_vars.items(), key=lambda x: -x[1]['size']):
                print(f"  window.{name} ({info['size']} chars) : {info['preview'][:120]}...")
        except Exception as e:
            print(f"  Erreur: {e}")

        # ── Chercher spécifiquement des données de zones ──
        print("\n🎯 Recherche profonde de données de zones...")
        try:
            js_search = page.evaluate('''() => {
                const results = {};
                // Check common patterns
                const checks = [
                    () => window.__NEXT_DATA__,
                    () => window.__INITIAL_STATE__,
                    () => window.__DATA__,
                    () => window.__APP_DATA__,
                    () => window.zones,
                    () => window.outages,
                    () => window.markers,
                    () => window.mapData,
                    () => window.appData,
                    () => window.store?.getState?.(),
                ];
                const names = ['__NEXT_DATA__', '__INITIAL_STATE__', '__DATA__', '__APP_DATA__',
                               'zones', 'outages', 'markers', 'mapData', 'appData', 'store.getState()'];
                checks.forEach((fn, i) => {
                    try {
                        const val = fn();
                        if (val) {
                            results[names[i]] = JSON.stringify(val).substring(0, 2000);
                        }
                    } catch(e) {}
                });
                return results;
            }''')

            for name, preview in js_search.items():
                print(f"\n  ✅ {name} trouvé ! Preview :")
                print(f"     {preview[:300]}...")
                try:
                    data = json.loads(preview)
                    found = deep_find_zones(data, path=name)
                    if found:
                        print(f"\n  🎉 ZONES TROUVÉES dans {name} !")
                        print(f"     Chemin : {found['path']}")
                        print(f"     Nombre : {found['count']}")
                        print(f"     Clés : {found['sample_keys']}")
                        print(f"     Exemples : {json.dumps(found['sample'], ensure_ascii=False, indent=2)[:800]}")
                except:
                    pass
        except Exception as e:
            print(f"  Erreur: {e}")

        # ── Parser le DOM rendu ──
        print("\n🗺️ Analyse du DOM rendu...")
        try:
            dom_info = page.evaluate('''() => {
                const info = {};
                // Compter les éléments par tag
                info.tags = {};
                for (const tag of ['div','span','li','tr','td','a','button','svg','path','circle','marker']) {
                    info.tags[tag] = document.querySelectorAll(tag).length;
                }
                // Chercher des éléments avec des classes suggestives
                info.suggestive = [];
                const selectors = [
                    '[class*="zone"]', '[class*="Zone"]', '[class*="region"]', '[class*="Region"]',
                    '[class*="outage"]', '[class*="Outage"]', '[class*="cut"]', '[class*="coup"]',
                    '[class*="marker"]', '[class*="pin"]', '[class*="area"]',
                    '[id*="zone"]', '[id*="map"]', '[id*="marker"]',
                    '.leaflet-marker', '.leaflet-popup', '[data-zone]', '[data-id]'
                ];
                for (const sel of selectors) {
                    const els = document.querySelectorAll(sel);
                    if (els.length > 0) {
                        info.suggestive.push({
                            selector: sel,
                            count: els.length,
                            samples: Array.from(els).slice(0, 3).map(el => ({
                                tag: el.tagName,
                                text: el.textContent?.trim().substring(0, 100),
                                class: el.className?.toString()?.substring(0, 100),
                                id: el.id,
                                dataAttrs: Object.keys(el.dataset)
                            }))
                        });
                    }
                }
                // Leaflet ?
                info.hasLeaflet = typeof L !== 'undefined';
                info.hasMap = typeof map !== 'undefined';
                // Chercher des SVG markers (Leaflet utilise souvent des SVG)
                const svgs = document.querySelectorAll('svg');
                info.svgCount = svgs.length;
                // Textes dans les popups
                const popups = document.querySelectorAll('.leaflet-popup-content, [class*="popup"], [class*="tooltip"]');
                info.popupCount = popups.length;
                info.popupSamples = Array.from(popups).slice(0, 5).map(p => p.textContent?.trim().substring(0, 100));
                return info;
            }''')

            print(f"  Tags : {json.dumps(dom_info.get('tags', {}))}")
            print(f"  Leaflet : {dom_info.get('hasLeaflet')}, Map object : {dom_info.get('hasMap')}")
            print(f"  SVG count : {dom_info.get('svgCount')}")
            print(f"  Popup count : {dom_info.get('popupCount')}")

            if dom_info.get('suggestive'):
                print(f"\n  Éléments suggestifs trouvés :")
                for s in dom_info['suggestive']:
                    print(f"    {s['selector']} → {s['count']} éléments")
                    for sample in s['samples']:
                        txt = sample.get('text', '')
                        cls = sample.get('class', '')
                        if txt:
                            print(f"      text: \"{txt[:80]}\"")
                        if cls:
                            print(f"      class: {cls[:80]}")
                        if sample.get('dataAttrs'):
                            print(f"      data: {sample['dataAttrs']}")

            if dom_info.get('popupSamples'):
                print(f"\n  Contenu des popups/tooltip :")
                for t in dom_info['popupSamples']:
                    if t:
                        print(f"    \"{t}\"")

            save("dom_analysis.json", json.dumps(dom_info, ensure_ascii=False, indent=2))

        except Exception as e:
            print(f"  Erreur DOM: {e}")

        # ── Résumé réseau ──
        print(f"\n📡 Résumé réseau : {len(network_log)} requêtes totales")
        xhr_fetch = [r for r in network_log if r['resource_type'] in ('xhr', 'fetch')]
        print(f"   XHR/Fetch : {len(xhr_fetch)}")
        for r in xhr_fetch:
            print(f"    {r['method']} {r['url'][:120]}")

        save("network_log.json", json.dumps(network_log, indent=2))

        browser.close()

    print("\n" + "=" * 70)
    print("DIAGNOSTIC TERMINÉ")
    print(f"Tous les fichiers sont dans : {OUTPUT_DIR}/")
    print("=" * 70)
    print("\n👉 Envoie-moi TOUT ce qui s'affiche ci-dessus," )
    print("   surtout les lignes avec ← (réponses) et 🎉 (zones trouvées)")
    print("=" * 70)


if __name__ == '__main__':
    main()
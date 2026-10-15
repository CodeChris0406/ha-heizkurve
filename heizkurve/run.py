"""Einstieg für das Home-Assistant-Add-on: regelmäßig auswerten, Bericht über Ingress anzeigen."""
import datetime as dt
import http.server
import json
import os
import threading
import time
import traceback
from pathlib import Path

import pandas as pd

import heizkurve as hk
from pv_regler import PVRegler

OPTIONS = Path("/data/options.json")
KURVE = Path("/data/kurve.json")
BERICHT = Path("/data/bericht.html")
jetzt_neu = threading.Event()
letzter_bericht = None  # für das Wiederherstellen der Sensoren nach einem HA-Neustart


def kurve_seit(o):
    """Zeitpunkt der letzten Änderung von Fußpunkt/Endpunkt in den Optionen."""
    akt = {"fusspunkt": o["fusspunkt"], "endpunkt": o["endpunkt"]}
    z = json.loads(KURVE.read_text()) if KURVE.exists() else {}
    if {k: z.get(k) for k in akt} != akt:
        z = {**akt, "seit": dt.datetime.now(dt.timezone.utc).isoformat()}
        KURVE.write_text(json.dumps(z))
    return z["seit"]


def build_config(o):
    cfg = {k: (dict(v) if isinstance(v, dict) else list(v)) for k, v in hk.DEFAULTS.items()}
    cfg["homeassistant"] = {"url": "http://supervisor/core", "token": os.environ["SUPERVISOR_TOKEN"],
                            "zeitzone": "Europe/Berlin"}
    for k in ("heizsystem", "raum_soll", "heizgrenze", "komfort_von", "komfort_bis", "kompressor_schwelle_w"):
        cfg["anlage"][k] = o[k]
    cfg["anlage"]["wp_komfort"] = o["pv_basis"]  # normaler Komfort-Sollwert der WP
    cfg["heizkurve"] = {k: o[k] for k in ("fusspunkt", "endpunkt", "norm_aussen", "max_vorlauf")
                        if not (k in ("fusspunkt", "endpunkt") and o[k] <= 0)}  # 0 = aus Messdaten schätzen
    cfg["heizkurve"]["seit"] = kurve_seit(o)
    cfg["sensoren"] = {k: (o["sensoren"].get(k) or "").strip() for k in hk.SENSOR_KEYS}
    cfg["raum"] = [{k: v for k, v in r.items() if v not in (None, "")} for r in o["raeume"]]
    try:
        sess, base = hk.ha_session(cfg)
        cfg["homeassistant"]["zeitzone"] = sess.get(f"{base}/api/config", timeout=30).json()["time_zone"]
    except Exception:
        pass
    return cfg


def auswertung(o, cfg):
    raw = hk.load_store()
    tage = 10
    if len(raw):
        seit = (pd.Timestamp.now(tz="UTC") - raw["ts"].max()).total_seconds() / 86400
        tage = max(1, min(10, int(seit) + 1))
    print(f"Hole {tage} Tag(e) aus Home Assistant …")
    raw = hk.merge_store(raw, hk.fetch_history(cfg, tage), o["aufbewahren_tage"])
    if raw["value"].notna().sum() == 0:
        print("Keine Daten gefunden – stimmen die Entity-IDs in der Konfiguration?")
        return
    end = dt.datetime.now(dt.timezone.utc)
    start = max(end - dt.timedelta(days=o["auswerten_tage"]), raw["ts"].min().to_pydatetime())
    B, _ = hk.analyse(raw, cfg, pd.Timestamp(start), pd.Timestamp(end))
    hk.write_report(B, cfg, BERICHT)
    hk.print_summary(B)
    hk.publish_sensors(cfg, B)
    return B


ZUSTAND = Path("/data/benachrichtigung.json")
WOCHENTAGE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def benachrichtigen(o, cfg, B):
    """Push bei neuen Empfehlungen/Warnungen, Beginn einer Kältephase und als Wochenbericht."""
    ziel = (o.get("benachrichtigung") or "").strip()
    if not ziel:
        return
    zustand = json.loads(ZUSTAND.read_text()) if ZUSTAND.exists() else {}
    jetzt = pd.Timestamp.now(tz=cfg["homeassistant"]["zeitzone"])
    meldungen = []

    offen = [h["titel"] for h in B.hinweise if h["stufe"] in ("aktion", "warnung")]
    neu = [t for t in offen if t not in zustand.get("gemeldet", [])]
    if neu:
        meldungen.append(("Heizkurve: neue Empfehlung", "\n".join(neu)))
    zustand["gemeldet"] = offen

    kaelte = B.meta.get("kaeltephase", False)
    if kaelte and not zustand.get("kaelte"):
        meldungen.append(("Heizkurve: Kältephase", "Jetzt lässt sich beurteilen, ob der Endpunkt reicht – "
                                                    "Bericht in den nächsten Tagen ansehen."))
    zustand["kaelte"] = kaelte

    tag = jetzt.strftime("%Y-%m-%d")
    woche = f"{jetzt.isocalendar().year}-W{jetzt.isocalendar().week}"
    if o["push_taeglich"] and jetzt.hour >= o["auswerten_um"] and zustand.get("tag") != tag:
        meldungen.append(("Heizkurve: Tagesbericht", hk.kurzfassung(B)))
        zustand["tag"] = tag
    elif (WOCHENTAGE[jetzt.weekday()] == o["wochenbericht_tag"] and jetzt.hour >= o["auswerten_um"]
            and zustand.get("woche") != woche):
        meldungen.append(("Heizkurve: Wochenbericht", hk.kurzfassung(B)))
        zustand["woche"] = woche

    sess, base = hk.ha_session(cfg)
    dienst = ziel.removeprefix("notify.")
    for titel, text in meldungen:
        sess.post(f"{base}/api/services/notify/{dienst}", timeout=30,
                  json={"title": titel, "message": text,
                        "data": {"clickAction": "/local_heizkurve", "url": "/local_heizkurve"}}).raise_for_status()
        print(f"Benachrichtigung gesendet: {titel}")
    ZUSTAND.write_text(json.dumps(zustand))


def naechster_lauf(o, tz):
    jetzt = pd.Timestamp.now(tz=tz)
    lauf = jetzt.normalize() + pd.Timedelta(hours=o["auswerten_um"])
    return lauf if lauf > jetzt else lauf + pd.Timedelta(days=1)


PLOTLY = Path(__file__).with_name("plotly.min.js")
PLOTLY_CDN = b"https://cdn.jsdelivr.net/npm/plotly.js-dist-min@2.35.2/plotly.min.js"


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0].endswith("/plotly.min.js") and PLOTLY.exists():
            self._send(PLOTLY.read_bytes(), "application/javascript", cache=True)
            return
        if self.path.rstrip("/").endswith("/neu"):
            jetzt_neu.set()
            body = ('<meta http-equiv="refresh" content="20;url=./">'
                    '<p style="font-family:sans-serif">Auswertung läuft … Seite lädt gleich neu.</p>').encode()
        elif BERICHT.exists():
            body = BERICHT.read_bytes()
            body = body.replace(b"<main>", b'<main><p style="text-align:right;margin:0"><a href="neu" '
                                b'style="color:inherit">Jetzt neu auswerten</a></p>', 1)
            if PLOTLY.exists():
                body = body.replace(PLOTLY_CDN, b"plotly.min.js")
        else:
            body = ('<meta http-equiv="refresh" content="30">'
                    '<p style="font-family:sans-serif">Noch kein Bericht – erste Auswertung läuft. '
                    'Fortschritt im Protokoll des Add-ons.</p>').encode()
        self._send(body, "text/html; charset=utf-8")

    def _send(self, body, ctype, cache=False):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def sensor_waechter(cfg):
    """Legt die Ergebnis-Sensoren neu an, wenn HA sie bei einem Neustart verworfen hat."""
    while True:
        time.sleep(600)
        try:
            sess, base = hk.ha_session(cfg)
            fehlt = sess.get(f"{base}/api/states/sensor.heizkurve_empfehlungen", timeout=20).status_code == 404
            if letzter_bericht is not None and fehlt:
                hk.publish_sensors(cfg, letzter_bericht)
                print("Sensoren nach HA-Neustart neu angelegt")
        except Exception:
            pass


def pv_schleife(o, cfg):
    """Prüft jede Minute, ob der Komfort-Sollwert wegen PV-Überschuss angehoben werden soll."""
    regler = PVRegler(o, cfg)
    letzter_fehler = None
    while True:
        try:
            regler.schritt()
            letzter_fehler = None
        except Exception as e:
            if str(e) != letzter_fehler:  # gleiche Fehler nicht jede Minute wiederholen
                print(f"PV-Regler: Fehler: {e}")
                letzter_fehler = str(e)
        time.sleep(60)


def main():
    threading.Thread(target=http.server.ThreadingHTTPServer(("0.0.0.0", 8099), Handler).serve_forever,
                     daemon=True).start()
    o = json.loads(OPTIONS.read_text())
    cfg = build_config(o)
    if o["pv_steuerung"]:
        print(f"PV-Regler aktiv: Akku ≥ {o['pv_akku_min']} % und Überschuss ≥ {o['pv_ueberschuss_w']} W → "
              f"Sollwert {o['pv_basis']:g} °C + {o['pv_min_anhebung']}…{o['pv_max_anhebung']} K "
              f"(nächste Stufe ab {o['pv_schritt_w']} W Überschuss, eine Stufe zurück bei Netzbezug > {o['pv_bezug_w']} W)")
        threading.Thread(target=pv_schleife, args=(o, cfg), daemon=True).start()
    if not cfg["sensoren"]["aussen"]:
        print("Noch keine Sensoren konfiguriert – suche passende Entitäten:")
        hk.entdecken(cfg)
        print("\nBitte die Entity-IDs im Reiter 'Konfiguration' eintragen und das Add-on neu starten.")
    threading.Thread(target=sensor_waechter, args=(cfg,), daemon=True).start()
    global letzter_bericht
    while True:
        try:
            B = auswertung(o, cfg)
            if B is not None:
                letzter_bericht = B
                benachrichtigen(o, cfg, B)
        except Exception:
            traceback.print_exc()
        lauf = naechster_lauf(o, cfg["homeassistant"]["zeitzone"])
        print(f"Nächste Auswertung: {lauf:%d.%m. %H:%M}")
        jetzt_neu.wait(timeout=(lauf - pd.Timestamp.now(tz=lauf.tz)).total_seconds())
        jetzt_neu.clear()


if __name__ == "__main__":
    main()

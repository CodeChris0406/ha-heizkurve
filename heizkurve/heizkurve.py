#!/usr/bin/env python3
"""Heizkurven-Analyse für eine Buderus-Wärmepumpe mit AVM-Thermostaten über Home Assistant.

Befehle:
  einrichten  – Entitäten in Home Assistant suchen und config.toml vorschlagen
  analyse     – Verlauf aus Home Assistant holen, auswerten, Bericht erzeugen
  demo        – Bericht mit simulierten Daten erzeugen (zum Ausprobieren)
"""
import argparse
import datetime as dt
import json
import math
import os
import re
import sys
import tomllib
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent
DATEN = Path(os.environ.get("HEIZKURVE_DATEN", BASE / "daten"))
STORE = DATEN / "verlauf.csv"

SENSOR_KEYS = ["aussen", "vorlauf", "vorlauf_soll", "ruecklauf", "kompressor",
               "kompressor_starts", "strom_leistung", "heizstab_leistung", "strom_energie", "waerme_energie",
               "wp_raum_soll"]
OFF_WORDS = {"off", "aus", "false", "none", "idle", "standby", "0", "inactive", "stop",
             "stopped", "kein", "keine", "no", "nein", "aus/off"}
NA_WORDS = {"unavailable", "unknown", "", "none_", "nan"}
MIN_STUNDEN = 45  # Raumdaten (Komfortzeit, Heiztage), ab denen eine Kurvenänderung empfohlen wird

DEFAULTS = {
    "homeassistant": {"url": "http://homeassistant.local:8123", "token": "", "zeitzone": "Europe/Berlin"},
    "anlage": {"heizsystem": "heizkoerper", "raum_soll": 21.0, "heizgrenze": 15.0,
               "komfort_von": 9, "komfort_bis": 20, "kompressor_schwelle_w": 150.0, "wp_komfort": None},
    "heizkurve": {},
    "sensoren": {k: "" for k in SENSOR_KEYS},
    "raum": [],
}


# --------------------------------------------------------------------------- Konfiguration

def load_config(path):
    path = Path(path)
    if not path.exists():
        sys.exit(f"Konfiguration {path.name} fehlt. Erst 'py heizkurve.py einrichten' ausführen "
                 f"oder config.example.toml nach config.toml kopieren.")
    with open(path, "rb") as f:
        user = tomllib.load(f)
    cfg = {k: (dict(v) if isinstance(v, dict) else list(v)) for k, v in DEFAULTS.items()}
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    return cfg


# --------------------------------------------------------------------------- Home Assistant

def ha_session(cfg):
    import requests
    token = cfg["homeassistant"].get("token") or os.environ.get("HA_TOKEN", "")
    if not token:
        sys.exit("Kein Home-Assistant-Token gefunden. In config.toml unter [homeassistant] token = \"...\" "
                 "eintragen oder Umgebungsvariable HA_TOKEN setzen.\n"
                 "Token erstellen: HA → Profil → Sicherheit → Langlebige Zugangstoken.")
    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {token}"
    return s, cfg["homeassistant"]["url"].rstrip("/")


def to_value(state, allow_text=False):
    if state is None:
        return np.nan
    txt = str(state).strip().lower()
    if txt in NA_WORDS:
        return np.nan
    try:
        return float(txt)
    except ValueError:
        pass
    if not allow_text:
        return np.nan
    return 0.0 if txt in OFF_WORDS else 1.0


def iso(t):
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def fetch_history(cfg, days):
    sess, base = ha_session(cfg)
    end = dt.datetime.now(dt.timezone.utc)
    start = end - dt.timedelta(days=days)
    sens = cfg["sensoren"]
    text_ids = {sens.get("kompressor")} - {"", None}
    rooms = cfg["raum"]
    # Schreibweise "entity#attribut" liest ein Attribut statt des Zustands
    specs = [v for v in sens.values() if v] + [r["entity"] for r in rooms if not r["entity"].startswith("climate.")]
    plain = sorted({s for s in specs if "#" not in s})
    attrs = {}
    for s in specs:
        if "#" in s:
            e, a = s.split("#", 1)
            attrs.setdefault(e, set()).add(a)
    for r in rooms:
        if r["entity"].startswith("climate."):
            attrs.setdefault(r["entity"], set()).update({"current_temperature", "temperature"})
    clim = sorted(attrs)
    rows = []
    t = start
    while t < end:
        t2 = min(t + dt.timedelta(days=2), end)
        common = {"end_time": iso(t2), "significant_changes_only": "0"}
        if plain:
            r = sess.get(f"{base}/api/history/period/{iso(t)}", timeout=180,
                         params={**common, "filter_entity_id": ",".join(plain),
                                 "minimal_response": "", "no_attributes": ""})
            r.raise_for_status()
            for lst in r.json():
                if not lst:
                    continue
                eid = lst[0]["entity_id"]
                for it in lst:
                    ts = it.get("last_changed") or it.get("last_updated")
                    rows.append((ts, eid, to_value(it.get("state"), eid in text_ids)))
        if clim:
            r = sess.get(f"{base}/api/history/period/{iso(t)}", timeout=180,
                         params={**common, "filter_entity_id": ",".join(clim)})
            r.raise_for_status()
            for lst in r.json():
                if not lst:
                    continue
                eid = lst[0]["entity_id"]
                for it in lst:
                    ts = it.get("last_updated") or it.get("last_changed")
                    a = it.get("attributes") or {}
                    for name in attrs.get(eid, ()):
                        rows.append((ts, f"{eid}#{name}", to_value(a.get(name), f"{eid}#{name}" in text_ids)))
                    if eid.startswith("climate."):
                        rows.append((ts, f"{eid}#ist", to_value(a.get("current_temperature"))))
                        rows.append((ts, f"{eid}#soll", to_value(a.get("temperature"))))
        print(f"  geladen bis {t2.astimezone():%d.%m. %H:%M}")
        t = t2
    df = pd.DataFrame(rows, columns=["ts", "key", "value"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True, format="ISO8601")
    return df


def load_store():
    if not STORE.exists():
        return pd.DataFrame(columns=["ts", "key", "value"])
    df = pd.read_csv(STORE)
    df["ts"] = pd.to_datetime(df["ts"], utc=True, format="ISO8601")
    return df


def merge_store(old, new, aufbewahren_tage=90):
    """Neue Werte anhängen; höchstens ein Wert je Entität und 2 Minuten, ältere Daten verwerfen."""
    df = pd.concat([x for x in (old, new) if len(x)], ignore_index=True) if len(old) or len(new) else new
    df["ts"] = pd.to_datetime(df["ts"], utc=True, format="ISO8601")
    df = df[df["ts"] >= pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=aufbewahren_tage)]
    df = df.sort_values(["key", "ts"])
    df = df.loc[~df.assign(_b=df["ts"].dt.floor("2min")).duplicated(["key", "_b"], keep="last")]
    DATEN.mkdir(exist_ok=True)
    out = df.copy()
    out["ts"] = out["ts"].dt.strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")
    out.to_csv(STORE, index=False)
    return df


# --------------------------------------------------------------------------- Aufbereitung

def step_series(raw, key, idx, hold=pd.Timedelta("49h")):
    """Zustandsänderungen → Treppenfunktion auf dem Raster idx."""
    s = raw.loc[raw["key"] == key].set_index("ts")["value"].sort_index()
    s = s[~s.index.duplicated(keep="last")]
    if s.empty:
        return pd.Series(np.nan, index=idx)
    u = s.index.union(idx)
    vals = s.fillna(-99999.0).reindex(u).ffill().replace(-99999.0, np.nan)
    last = pd.Series(s.index, index=s.index).reindex(u).ffill()
    age = pd.Series(u, index=u) - last
    vals[(age > hold).to_numpy()] = np.nan
    return vals.reindex(idx)


def room_keys(room):
    e = room["entity"]
    if e.startswith("climate."):
        return f"{e}#ist", f"{e}#soll"
    return e, None


def build_frame(raw, cfg, start, end):
    idx = pd.date_range(start.ceil("5min"), end.floor("5min"), freq="5min")
    F = pd.DataFrame(index=idx)
    for k in SENSOR_KEYS:
        e = cfg["sensoren"].get(k)
        if e:
            F[k] = step_series(raw, e, idx)
    for room in cfg["raum"]:
        ik, sk = room_keys(room)
        F[f"{room['name']}|ist"] = step_series(raw, ik, idx)
        F[f"{room['name']}|soll_avm"] = step_series(raw, sk, idx) if sk else np.nan
    if "strom_leistung" in F and "heizstab_leistung" in F:
        # Der Leistungsmesser erfasst Heizstab und WP gemeinsam → Heizstab abziehen
        F["strom_leistung"] = (F["strom_leistung"] - F["heizstab_leistung"].fillna(0)).clip(lower=0)
    F.index = F.index.tz_convert(cfg["homeassistant"]["zeitzone"])
    if "aussen" in F:
        F["aussen_12h"] = F["aussen"].rolling("12h", min_periods=12).mean()
        F["aussen_24h"] = F["aussen"].rolling("24h", min_periods=24).mean()
    return F


def compressor_runs(raw, cfg, tz):
    """Exakte Laufphasen des Verdichters aus den Rohdaten (Start, Ende)."""
    sens, anl = cfg["sensoren"], cfg["anlage"]
    def reihe(key):
        s = raw.loc[raw["key"] == key].set_index("ts")["value"].sort_index()
        return s[~s.index.duplicated(keep="last")]

    if sens.get("kompressor"):
        s, thr = reihe(sens["kompressor"]).dropna(), 0.0
    elif sens.get("strom_leistung"):
        s, thr = reihe(sens["strom_leistung"]), float(anl["kompressor_schwelle_w"])
        if sens.get("heizstab_leistung"):
            h = reihe(sens["heizstab_leistung"])
            u = s.index.union(h.index)
            s = s.reindex(u).ffill() - h.reindex(u).ffill().fillna(0)
        s = s.dropna()
    else:
        return None
    if s.empty:
        return None
    on = (s > thr).astype(int)
    edge = on.diff().fillna(0)
    starts = list(on.index[edge > 0])
    stops = list(on.index[edge < 0])
    runs = []
    for st in starts:
        nxt = next((x for x in stops if x > st), None)
        if nxt is not None:
            runs.append((st.tz_convert(tz), nxt.tz_convert(tz)))
    return pd.DataFrame(runs, columns=["start", "ende"])


# --------------------------------------------------------------------------- Auswertung

def linfit(x, y):
    m = np.isfinite(x) & np.isfinite(y)
    x, y = np.asarray(x)[m], np.asarray(y)[m]
    if len(x) < 10 or np.ptp(x) < 1:
        return None
    b, a = np.polyfit(x, y, 1)
    pred = a + b * x
    ss = ((y - y.mean()) ** 2).sum()
    r2 = 1 - ((y - pred) ** 2).sum() / ss if ss > 0 else 0.0
    return {"a": float(a), "b": float(b), "r2": float(r2), "n": int(len(x)),
            "xmin": float(x.min()), "xmax": float(x.max()), "xmean": float(x.mean())}


def kurve(fp, ep, norm, t):
    return fp + (ep - fp) * (20.0 - np.asarray(t, dtype=float)) / (20.0 - norm)


def r05(x):
    return round(x * 2) / 2


class Bericht:
    def __init__(self):
        self.hinweise = []   # (stufe, titel, text)
        self.charts = {}
        self.tabellen = {}
        self.meta = {}

    def add(self, stufe, titel, text):
        self.hinweise.append({"stufe": stufe, "titel": titel, "text": text})


def analyse(raw, cfg, start, end):
    B = Bericht()
    anl, hk = cfg["anlage"], cfg["heizkurve"]
    tz = cfg["homeassistant"]["zeitzone"]
    F = build_frame(raw, cfg, start, end)
    H = F.resample("h").mean()
    k_faktor = 3.0 if anl["heizsystem"] == "heizkoerper" else 2.0
    norm = float(hk.get("norm_aussen", -12.0))
    B.meta = {"von": F.index[0].strftime("%d.%m.%Y %H:%M"), "bis": F.index[-1].strftime("%d.%m.%Y %H:%M"),
              "tage": round((F.index[-1] - F.index[0]).total_seconds() / 86400, 1),
              "heizsystem": anl["heizsystem"]}

    if "aussen" not in F or F["aussen"].notna().sum() < 50:
        B.add("warnung", "Keine Außentemperatur", "Ohne Außentemperatur-Sensor ist keine Heizkurven-Analyse möglich. "
              "Bitte [sensoren] aussen in config.toml setzen.")
        return B, F
    heiz = H["aussen_24h"] < anl["heizgrenze"]
    B.meta["aussen_min"] = round(float(H["aussen"].min()), 1)
    B.meta["aussen_max"] = round(float(H["aussen"].max()), 1)
    B.meta["heizstunden"] = int(heiz.sum())

    # ---- Verdichter-Laufphasen
    runs = compressor_runs(raw, cfg, tz)
    laeuft = None
    if runs is not None and len(runs):
        laeuft = pd.Series(False, index=F.index)
        for st, en in runs.itertuples(index=False):
            laeuft[(F.index >= st) & (F.index < en)] = True
    elif "vorlauf" in F and "ruecklauf" in F:
        laeuft = (F["vorlauf"] - F["ruecklauf"]) > 2.0

    # ---- Ist-Heizkurve
    ist_fit = None
    if "vorlauf_soll" in F and F["vorlauf_soll"].notna().sum() > 50:
        y = F["vorlauf_soll"].where(F["vorlauf_soll"] > 15)
        quelle = "Vorlauf-Soll"
    elif "vorlauf" in F and laeuft is not None:
        y = F["vorlauf"].where(laeuft)
        quelle = "Vorlauf (Verdichter an)"
    else:
        y, quelle = None, None
    if y is not None:
        Y = pd.DataFrame({"x": F["aussen_12h"], "y": y})[F["aussen_24h"] < anl["heizgrenze"]]
        Y = Y.resample("h").median().dropna()
        if fp_cfg_ok(hk):
            # Warmwasser-Ladungen ausfiltern (deutlich über der Heizkurve)
            Y = Y[Y["y"] < kurve(hk["fusspunkt"], hk["endpunkt"], norm, Y["x"]) + 8]
        ist_fit = linfit(Y["x"].to_numpy(), Y["y"].to_numpy())
        B.charts["kurve_punkte"] = {"x": Y["x"].round(2).tolist(), "y": Y["y"].round(2).tolist(),
                                    "t": [t.strftime("%d.%m. %H:%M") for t in Y.index], "quelle": quelle}
        if ist_fit:
            fp_i = ist_fit["a"] + ist_fit["b"] * 20
            ep_i = ist_fit["a"] + ist_fit["b"] * norm
            B.meta["ist_kurve"] = {"fusspunkt": round(fp_i, 1), "endpunkt": round(ep_i, 1), "quelle": quelle}
            if fp_cfg_ok(hk):
                d20 = fp_i - hk["fusspunkt"]
                dn = ep_i - hk["endpunkt"]
                if max(abs(d20), abs(dn)) > 3 and quelle == "Vorlauf-Soll":
                    B.add("info", "Gemessene Kurve weicht von der Einstellung ab",
                          f"Aus dem Vorlauf-Soll ergibt sich Fußpunkt ≈ {fp_i:.1f} °C / Endpunkt ≈ {ep_i:.1f} °C, "
                          f"eingetragen sind {hk['fusspunkt']} / {hk['endpunkt']} °C. Prüfe, ob Raumeinfluss, "
                          f"Parallelverschiebung (Offset) oder ein anderer Kurventyp aktiv ist, oder ob die Werte "
                          f"in config.toml veraltet sind.")

    akt = None
    if fp_cfg_ok(hk):
        akt = (float(hk["fusspunkt"]), float(hk["endpunkt"]), "Einstellung (config.toml)")
    elif "ist_kurve" in B.meta:
        akt = (B.meta["ist_kurve"]["fusspunkt"], B.meta["ist_kurve"]["endpunkt"], "aus Messdaten geschätzt")

    # ---- Stunden mit angehobenem WP-Sollwert (PV-Überschuss, Tests) samt 3 h Nachlauf nicht bewerten
    angehoben = np.zeros(len(H), bool)
    if "wp_raum_soll" in H and anl.get("wp_komfort") is not None:
        a = (H["wp_raum_soll"] > anl["wp_komfort"] + 0.1).astype(float)
        angehoben = a.rolling(4, min_periods=1).max().fillna(0).to_numpy() > 0
        if angehoben.any():
            B.meta["angehoben_h"] = int(angehoben.sum())
            B.add("info", f"{int(angehoben.sum())} Stunden mit angehobenem Sollwert ausgeklammert",
                  "In diesen Stunden (PV-Überschuss oder manuelle Anhebung, jeweils plus 3 h Nachlauf) "
                  "stand der Komfort-Sollwert der Wärmepumpe über dem Normalwert. Sie fließen nicht in die "
                  "Bewertung der Heizkurve ein.")

    # ---- Nur Raumdaten seit der letzten Kurvenänderung (plus 12 h Einschwingen) bewerten
    ausblenden = angehoben
    if hk.get("seit"):
        ab = pd.Timestamp(hk["seit"]).tz_convert(tz) + pd.Timedelta("12h")
        vorher = H.index < ab
        if vorher.any():
            ausblenden = angehoben | vorher
            B.meta["bewertet_ab"] = ab.strftime("%d.%m.%Y %H:%M")
            B.add("info", f"Heizkurve bewertet ab {ab:%d.%m. %H:%M}",
                  f"Die Kurve wurde am {pd.Timestamp(hk['seit']).tz_convert(tz):%d.%m. um %H:%M} geändert. "
                  "Ältere Raumdaten gehören zur alten Einstellung und fließen nicht in die Bewertung ein; "
                  "die ersten 12 Stunden danach schwingt das Haus ein.")

    # ---- Räume
    raeume = []
    nacht_rooms = []
    for room in cfg["raum"]:
        n = room["name"]
        ist = H[f"{n}|ist"]
        soll_avm = H[f"{n}|soll_avm"].where((H[f"{n}|soll_avm"] >= 12) & (H[f"{n}|soll_avm"] <= 27))
        if "soll" in room:
            soll = pd.Series(float(room["soll"]), index=H.index)
        else:
            soll = soll_avm.fillna(float(anl["raum_soll"]))
        h = H.index.hour
        komfort = (h >= anl["komfort_von"]) & (h < anl["komfort_bis"])
        m = komfort & heiz.to_numpy() & ist.notna().to_numpy() & ~ausblenden
        dev = (ist - soll)[m]
        info = {"name": n, "n": int(m.sum()), "fuehrend": room.get("fuehrend", True)}
        if info["n"] >= 12:
            info.update({
                "ist_mittel": round(float(ist[m].mean()), 2),
                "soll_mittel": round(float(soll[m].mean()), 2),
                "abw": round(float(dev.mean()), 2),
                "zu_kalt": round(float((dev < -0.5).mean() * 100)),
                "zu_warm": round(float((dev > 0.5).mean() * 100)),
            })
            info["fit"] = linfit(H["aussen_12h"][m].to_numpy(), dev.to_numpy())
        # Nachtabsenkung am AVM-Thermostat?
        if soll_avm.notna().sum() > 24:
            tag = soll_avm[(h >= 17) & (h < 21)].median()
            nacht = soll_avm[(h >= 1) & (h < 5)].median()
            if np.isfinite(tag) and np.isfinite(nacht) and tag - nacht >= 1.0:
                info["absenkung"] = round(float(tag - nacht), 1)
                nacht_rooms.append(f"{n} (−{tag - nacht:.1f} K)")
        raeume.append(info)
    B.tabellen["raeume"] = raeume

    # Diagramm-Daten Räume
    B.charts["zeit"] = {"t": [t.strftime("%Y-%m-%d %H:%M") for t in H.index],
                        "aussen": nan2none(H["aussen"]),
                        "vorlauf": nan2none(H["vorlauf"]) if "vorlauf" in H else None,
                        "raeume": [{"name": r["name"], "ist": nan2none(H[f"{r['name']}|ist"]),
                                    "soll": nan2none(H[f"{r['name']}|soll_avm"])} for r in cfg["raum"]]}
    streu = []
    for room in cfg["raum"]:
        n = room["name"]
        soll = (pd.Series(float(room["soll"]), index=H.index) if "soll" in room
                else H[f"{n}|soll_avm"].where(H[f"{n}|soll_avm"].between(12, 27)).fillna(anl["raum_soll"]))
        h = H.index.hour
        m = (h >= anl["komfort_von"]) & (h < anl["komfort_bis"]) & heiz.to_numpy() & ~ausblenden
        d = pd.DataFrame({"x": H["aussen_12h"], "y": H[f"{n}|ist"] - soll})[m].dropna()
        streu.append({"name": n, "x": d["x"].round(2).tolist(), "y": d["y"].round(2).tolist()})
    B.charts["abweichung"] = streu

    # ---- Heizkurven-Vorschlag
    kandidaten = [r for r in raeume if r.get("fit") and r["fuehrend"]]
    if not kandidaten:
        if "bewertet_ab" in B.meta:
            B.add("info", "Daten zur neuen Kurve werden gesammelt",
                  f"Seit der Kurvenänderung zählen nur Raumdaten ab {B.meta['bewertet_ab']}. "
                  f"Ein Vorschlag kommt, sobald genug Stunden zusammen sind (vorläufig ab 12, belastbar ab {MIN_STUNDEN}).")
        else:
            B.add("warnung", "Zu wenig Raumdaten",
                  "Für einen Heizkurven-Vorschlag braucht es mindestens einige Heiztage mit Raumtemperaturen "
                  "(Komfortzeit, Außentemperatur unter Heizgrenze). Einfach in ein paar Tagen erneut ausführen – "
                  "das Tool sammelt die Daten in daten/verlauf.csv.")
    elif akt is None:
        B.add("warnung", "Aktuelle Heizkurve unbekannt",
              "Bitte Fußpunkt/Endpunkt aus dem Buderus-Regler in config.toml unter [heizkurve] eintragen "
              "oder einen Vorlauf-Soll-Sensor angeben.")
    else:
        fuehr = min(kandidaten, key=lambda r: r["abw"])
        f = fuehr["fit"]
        B.meta["fuehrungsraum"] = fuehr["name"]
        tmid = f["xmean"]
        dev_mid = f["a"] + f["b"] * tmid
        spann = f["xmax"] - f["xmin"]
        steig_sig = spann >= 6 and abs(f["b"]) >= 0.04 and f["r2"] >= 0.15
        shift = -k_faktor * dev_mid if abs(dev_mid) > 0.25 else 0.0
        dsteig = -k_faktor * f["b"] if steig_sig else 0.0
        d20 = shift + dsteig * (20 - tmid)
        dn = shift + dsteig * (norm - tmid)
        roh = (d20, dn)
        d20c, dnc = (max(-2.0, min(2.0, v)) for v in roh)
        fp_akt, ep_akt, akt_quelle = akt
        fp_neu, ep_neu = r05(fp_akt + d20c), r05(ep_akt + dnc)
        maxv = hk.get("max_vorlauf")
        if maxv and ep_neu > maxv:
            ep_neu = float(maxv)
        txt_basis = (f"Grundlage ist der am schlechtesten versorgte Raum <b>{fuehr['name']}</b> "
                     f"(Ø {fuehr['abw']:+.2f} K zum Soll, {f['n']} Stunden bei {f['xmin']:.0f} … {f['xmax']:.0f} °C außen). "
                     f"Faustregel {anl['heizsystem']}: 1 K Raumtemperatur ≈ {k_faktor:.0f} K Vorlauf.")
        vorlaeufig = f["n"] < MIN_STUNDEN
        nur_trend = vorlaeufig and (fp_neu != fp_akt or ep_neu != ep_akt)
        if nur_trend:
            B.add("info", "Vorläufiger Trend zur Heizkurve",
                  f"Erst {f['n']} von mindestens {MIN_STUNDEN} Stunden Raumdaten – noch nichts ändern. "
                  f"Tendenz: Fußpunkt {fp_akt:g} → {fp_neu:g} °C, Endpunkt {ep_akt:g} → {ep_neu:g} °C. " + txt_basis)
            fp_neu, ep_neu = fp_akt, ep_akt
        B.meta["kurve"] = {"fp_akt": fp_akt, "ep_akt": ep_akt, "quelle": akt_quelle,
                           "fp_neu": fp_neu, "ep_neu": ep_neu, "norm": norm, "vorlaeufig": vorlaeufig,
                           "tmin": round(f["xmin"], 1), "tmax": round(f["xmax"], 1)}
        if nur_trend:
            pass
        elif fp_neu == fp_akt and ep_neu == ep_akt:
            B.add("ok", "Heizkurve passt" + (" (vorläufig)" if vorlaeufig else ""),
                  txt_basis + " Keine Änderung nötig – der Führungsraum erreicht sein Soll im Rahmen von ±0,3 K."
                  + (f" Bewertung wird belastbar ab {MIN_STUNDEN} Stunden Raumdaten." if vorlaeufig else ""))
        else:
            teile = []
            if shift:
                teile.append(f"Räume sind im Mittel {'zu kalt' if dev_mid < 0 else 'zu warm'} "
                             f"({dev_mid:+.1f} K) → Kurve {'anheben' if dev_mid < 0 else 'absenken'}.")
            if dsteig:
                teile.append("Die Abweichung hängt von der Außentemperatur ab: bei Kälte wird es "
                             f"{'kälter' if f['b'] > 0 else 'wärmer'} → Kurve "
                             f"{'steiler' if f['b'] > 0 else 'flacher'} stellen.")
            elif spann < 6:
                teile.append(f"Steilheit noch nicht beurteilbar (nur {spann:.0f} K Außentemperatur-Spanne; "
                             f"mind. 6 K nötig) – vorerst nur Parallelverschiebung.")
            gedeckelt = any(abs(v) > 2.0 for v in roh)
            B.add("aktion", f"Heizkurve: Fußpunkt {fp_akt:g} → {fp_neu:g} °C, Endpunkt {ep_akt:g} → {ep_neu:g} °C",
                  " ".join(teile) + " " + txt_basis +
                  (" Die rechnerische Änderung war größer; begrenzt auf max. 2 K pro Schritt." if gedeckelt else "") +
                  " Nach der Änderung 3–5 Tage laufen lassen und erneut auswerten.")
        # Räume, die deutlich wärmer sind als der Führungsraum
        warm = [r for r in raeume if r.get("abw") is not None and r["abw"] - fuehr["abw"] > 1.0]
        if warm:
            B.add("info", "Räume mit Überversorgung",
                  ", ".join(f"{r['name']} ({r['abw']:+.1f} K)" for r in warm) +
                  " sind deutlich wärmer als der Führungsraum. Dort regelt das AVM-Thermostat ab – die WP "
                  "liefert aber trotzdem die hohe Vorlauftemperatur. Besser: hydraulischen Abgleich prüfen "
                  "(Durchfluss an diesen Heizkörpern/Heizkreisen reduzieren), damit die Thermostate offen "
                  "bleiben können.")

    if nacht_rooms:
        B.add("info", "Nachtabsenkung an AVM-Thermostaten",
              "Gefunden in: " + ", ".join(nacht_rooms) + ". Bei Wärmepumpen spart Absenkung meist wenig: "
              "morgens muss mit höherer Vorlauftemperatur (schlechtere Effizienz, mehr Takten) nachgeheizt werden. "
              "Empfehlung: Absenkung auf max. 1–2 K begrenzen oder ganz weglassen und lieber die Kurve "
              "insgesamt etwas niedriger fahren.")

    # ---- Takten
    if runs is not None and len(runs):
        r = runs.copy()
        r["dauer"] = (r["ende"] - r["start"]).dt.total_seconds() / 60
        r["tag"] = r["start"].dt.floor("D")
        tage = r.groupby("tag").agg(starts=("dauer", "size"), laufzeit_h=("dauer", lambda x: x.sum() / 60),
                                    median_min=("dauer", "median"))
        if "kompressor_starts" in F and F["kompressor_starts"].notna().sum() > 10:
            z = F["kompressor_starts"].resample("D").agg(["first", "last"])
            zd = (z["last"] - z["first"]).clip(lower=0)
            tage["starts"] = zd.reindex(tage.index).fillna(tage["starts"])
        tage["aussen"] = F["aussen"].resample("D").mean().reindex(tage.index)
        tage = tage.dropna(subset=["aussen"])
        B.charts["takten"] = {"tag": [t.strftime("%d.%m.") for t in tage.index],
                              "starts": tage["starts"].round(0).tolist(),
                              "laufzeit": tage["laufzeit_h"].round(1).tolist(),
                              "median": tage["median_min"].round(0).tolist(),
                              "aussen": tage["aussen"].round(1).tolist()}
        hz = tage[tage["aussen"] < anl["heizgrenze"]]
        if len(hz):
            med_lauf = float(r[r["tag"].isin(hz.index)]["dauer"].median())
            starts_tag = float(hz["starts"].median())
            B.meta["takten"] = {"starts_tag": round(starts_tag), "lauf_min": round(med_lauf)}
            mild = hz[hz["aussen"] >= 7]
            if med_lauf < 20 or starts_tag > 24:
                B.add("aktion" if med_lauf < 12 else "warnung", "Verdichter taktet",
                      f"Median {starts_tag:.0f} Starts/Tag, typische Laufzeit {med_lauf:.0f} min (Ziel: ≥ 30 min, "
                      f"möglichst < 3 Starts/h). "
                      + (f"Besonders an milden Tagen (≥ 7 °C: Ø {mild['starts'].mean():.0f} Starts/Tag). " if len(mild) else "")
                      + "Maßnahmen: Fußpunkt senken (WP liefert an milden Tagen zu viel Leistung), "
                      "Thermostate im Führungsraum ganz öffnen, Nachtabsenkung reduzieren; ggf. im Fachmann-Menü "
                      "Schaltdifferenz/Hysterese bzw. Mindestlaufzeit erhöhen lassen.")
            else:
                B.add("ok", "Taktverhalten unauffällig",
                      f"Median {starts_tag:.0f} Starts/Tag, typische Laufzeit {med_lauf:.0f} min.")
        warm_tage = tage[(tage["aussen"] >= max(anl["heizgrenze"] + 1, 16)) & (tage["laufzeit_h"] > 2)]
        if len(warm_tage) >= 2:
            B.add("info", "Heizbetrieb an warmen Tagen",
                  f"An {len(warm_tage)} Tagen mit ≥ {max(anl['heizgrenze'] + 1, 16):.0f} °C Tagesmittel lief der "
                  "Verdichter über 2 h (kann auch Warmwasser sein). Falls die Räume dabei zu warm sind: "
                  "Sommer/Winter-Umschaltung (Heizgrenze) im Buderus-Regler senken.")
    elif runs is None:
        B.add("info", "Kein Verdichter-Signal",
              "Für die Takt-Analyse in [sensoren] 'kompressor' (Status/Frequenz) oder 'strom_leistung' (W) eintragen.")

    # ---- Spreizung
    if "vorlauf" in F and "ruecklauf" in F and laeuft is not None:
        sp = (F["vorlauf"] - F["ruecklauf"])[laeuft & (F["aussen_24h"] < anl["heizgrenze"])]
        if akt:
            sp = sp[(F["vorlauf"] < kurve(akt[0], akt[1], norm, F["aussen_12h"]) + 8)[sp.index]]
        sp = sp.dropna()
        if len(sp) > 50:
            med = float(sp.median())
            B.meta["spreizung"] = round(med, 1)
            if med > 7:
                B.add("warnung", f"Hohe Spreizung ({med:.1f} K)",
                      "Der Volumenstrom ist eher niedrig – oft weil Thermostate drosseln oder die Pumpe zu schwach "
                      "eingestellt ist. Die WP muss dann mit höherer Vorlauftemperatur fahren. Thermostate öffnen, "
                      "Pumpenstufe/Abgleich prüfen.")
            elif med < 2.5:
                B.add("info", f"Sehr kleine Spreizung ({med:.1f} K)",
                      "Hoher Volumenstrom – für die WP effizient, aber ggf. unnötiger Pumpenstrom. Unkritisch.")
            else:
                B.add("ok", f"Spreizung im Rahmen ({med:.1f} K)", "Vorlauf/Rücklauf-Differenz bei laufendem Verdichter ist gut.")

    # ---- Strom der Wärmepumpe (ohne Heizstab, aus der Leistung integriert)
    def tagesdiff(s):
        z = s.resample("D").agg(["first", "last"])
        return (z["last"] - z["first"]).clip(lower=0)

    wp_tag = None
    if "strom_leistung" in F and "heizstab_leistung" in F:
        wp_tag = (F["strom_leistung"].resample("D").mean() * 24 / 1000)[F["strom_leistung"].resample("D").count() > 200]
        hs_tag = (F["heizstab_leistung"].fillna(0).resample("D").mean() * 24 / 1000).reindex(wp_tag.index)
        if len(wp_tag):
            B.meta["strom_tag"] = round(float(wp_tag.mean()), 1)
            B.add("info", f"Strom Wärmepumpe: Ø {wp_tag.mean():.1f} kWh/Tag (ohne Heizstab)",
                  f"Der Leistungsmesser erfasst Wärmepumpe und PV-Heizstab gemeinsam; der Heizstab "
                  f"(Ø {hs_tag.mean():.1f} kWh/Tag) wird abgezogen. Letzter voller Tag: "
                  f"WP {wp_tag.iloc[-1]:.1f} kWh, Heizstab {hs_tag.iloc[-1]:.1f} kWh.")

    # ---- Effizienz
    if ("strom_energie" in F or wp_tag is not None) and "waerme_energie" in F:
        st = wp_tag if wp_tag is not None else tagesdiff(F["strom_energie"])
        wa = tagesdiff(F["waerme_energie"]).reindex(st.index)
        ok = (st > 0.5) & wa.notna()
        if ok.sum() >= 2:
            cop = (wa[ok] / st[ok])
            B.meta["cop"] = round(float(wa[ok].sum() / st[ok].sum()), 2)
            B.charts["cop"] = {"tag": [t.strftime("%d.%m.") for t in cop.index], "cop": cop.round(2).tolist(),
                               "aussen": F["aussen"].resample("D").mean().reindex(cop.index).round(1).tolist()}
            B.add("info", f"Arbeitszahl im Zeitraum: {B.meta['cop']:.2f}",
                  f"Wärme {wa[ok].sum():.0f} kWh / Strom {st[ok].sum():.0f} kWh. Faustregel: jedes Kelvin weniger "
                  "Vorlauf verbessert die Effizienz um ca. 2–3 %.")

    pruefe_sensoren(B, raw, cfg, end, runs)

    # ---- Kältephase: erst dann lässt sich der Endpunkt beurteilen
    a72 = F["aussen"][F.index >= F.index[-1] - pd.Timedelta("72h")].mean()
    B.meta["kaeltephase"] = bool(np.isfinite(a72) and a72 < 3.0)
    if B.meta["kaeltephase"]:
        B.add("info", f"Kältephase: Ø {a72:.1f} °C in den letzten 3 Tagen",
              "Jetzt lässt sich beurteilen, ob der Endpunkt reicht. Achte in den nächsten Tagen darauf, ob die "
              "Räume bei Kälte kühler werden – das Tool schlägt dann einen steileren Verlauf vor.")

    # Kurven für das Diagramm
    xs = list(np.round(np.linspace(min(norm, B.meta.get("aussen_min", 0)), 20, 30), 2))
    if akt:
        B.charts["kurve_akt"] = {"x": xs, "y": list(np.round(kurve(akt[0], akt[1], norm, xs), 2))}
    if "kurve" in B.meta:
        k = B.meta["kurve"]
        B.charts["kurve_neu"] = {"x": xs, "y": list(np.round(kurve(k["fp_neu"], k["ep_neu"], norm, xs), 2))}
    return B, F


SENSOR_NAMEN = {"aussen": "Außentemperatur", "vorlauf": "Vorlauf", "ruecklauf": "Rücklauf",
                "kompressor": "Verdichter", "strom_leistung": "Leistung WP", "strom_energie": "Stromzähler WP",
                "waerme_energie": "Wärmemenge", "kompressor_starts": "Startzähler",
                "heizstab_leistung": "Leistung Heizstab"}


def pruefe_sensoren(B, raw, cfg, end, runs):
    """Warnt bei Sensoren, die nicht verfügbar sind oder deren Wert hängt."""
    jetzt = pd.Timestamp(end)
    sens = cfg["sensoren"]

    def reihe(key):
        s = raw.loc[raw["key"] == key].set_index("ts")["value"].sort_index()
        return s[~s.index.duplicated(keep="last")]

    def letzte_aenderung(s):
        s = s.dropna()
        wechsel = s[s.diff().fillna(0) != 0]
        return wechsel.index[-1] if len(wechsel) else (s.index[0] if len(s) else None)

    probleme = []
    # Messwerte, die sich ständig ändern sollten
    pruef = [(SENSOR_NAMEN[k], sens[k], 24) for k in ("aussen", "vorlauf", "ruecklauf") if sens.get(k)]
    pruef += [(f"Raum {r['name']}", room_keys(r)[0], 36) for r in cfg["raum"]]
    for name, key, stunden in pruef:
        s = reihe(key)
        if s.empty:
            probleme.append(f"{name}: keine Daten")
        elif pd.isna(s.iloc[-1]) and jetzt - s.index[-1] > pd.Timedelta("6h"):
            probleme.append(f"{name}: nicht verfügbar seit {s.index[-1].tz_convert(cfg['homeassistant']['zeitzone']):%d.%m. %H:%M}")
        else:
            la = letzte_aenderung(s)
            if la is not None and s.index[0] <= jetzt - pd.Timedelta(hours=stunden) \
                    and jetzt - la > pd.Timedelta(hours=stunden):
                probleme.append(f"{name}: Wert seit über {stunden} h unverändert ({s.dropna().iloc[-1]:g})")
    # Zähler, die bei laufendem Verdichter steigen müssen
    if runs is not None and len(runs):
        r48 = runs[runs["ende"] >= jetzt - pd.Timedelta("48h")]
        laufzeit_h = (r48["ende"] - r48["start"]).dt.total_seconds().sum() / 3600
        for k in ("strom_energie", "waerme_energie", "kompressor_starts"):
            if not sens.get(k):
                continue
            s = reihe(sens[k]).dropna()
            s48 = s[s.index >= jetzt - pd.Timedelta("48h")]
            vorher = s[s.index < jetzt - pd.Timedelta("48h")]
            werte = pd.concat([vorher.iloc[-1:], s48])
            schwelle = len(r48) >= 2 if k == "kompressor_starts" else laufzeit_h >= 3
            if schwelle and len(werte) and werte.max() - werte.min() <= 0:
                probleme.append(f"{SENSOR_NAMEN[k]}: steigt nicht, obwohl der Verdichter in den letzten 48 h lief")
    B.meta["sensorprobleme"] = probleme
    if probleme:
        B.add("warnung", "Sensor-Problem: " + ", ".join(p.split(":")[0] for p in probleme), "; ".join(probleme) +
              ". Betroffene Auswertungen sind dadurch unvollständig – Integration bzw. Gerät prüfen.")


def fp_cfg_ok(hk):
    return isinstance(hk.get("fusspunkt"), (int, float)) and isinstance(hk.get("endpunkt"), (int, float))


def nan2none(s):
    return [None if not np.isfinite(v) else round(float(v), 2) for v in s.to_numpy()]


# --------------------------------------------------------------------------- Bericht (HTML)

def write_report(B, cfg, path):
    data = {"hinweise": B.hinweise, "charts": B.charts, "tabellen": B.tabellen, "meta": B.meta,
            "erstellt": dt.datetime.now().strftime("%d.%m.%Y %H:%M")}
    html = (BASE / "bericht_vorlage.html").read_text(encoding="utf-8")
    html = html.replace("/*__DATEN__*/null", json.dumps(data, ensure_ascii=False, allow_nan=False, default=_json_default))
    Path(path).write_text(html, encoding="utf-8")


def _json_default(o):
    if isinstance(o, (np.floating,)):
        return None if not math.isfinite(o) else float(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    raise TypeError(type(o))


def kurzfassung(B):
    """Einzeilige Zusammenfassung für Benachrichtigungen."""
    m = B.meta
    teile = []
    kurve_titel = next((h["titel"] for h in B.hinweise
                        if h["titel"].startswith(("Heizkurve:", "Heizkurve passt", "Vorläufiger Trend",
                                                  "Daten zur neuen Kurve"))), None)
    if kurve_titel:
        teile.append(kurve_titel)
    if m.get("fuehrungsraum"):
        r = next(r for r in B.tabellen["raeume"] if r["name"] == m["fuehrungsraum"])
        teile.append(f"{r['name']} {r['abw']:+.1f} K zum Soll")
    if m.get("takten"):
        teile.append(f"{m['takten']['starts_tag']} Starts/Tag à {m['takten']['lauf_min']} min")
    if "aussen_min" in m:
        teile.append(f"außen {m['aussen_min']:.0f} … {m['aussen_max']:.0f} °C")
    warnungen = [h["titel"] for h in B.hinweise if h["stufe"] == "warnung"]
    if warnungen:
        teile.append("Achtung: " + "; ".join(warnungen))
    return " · ".join(teile)


def publish_sensors(cfg, B):
    """Ergebnis als Sensoren in Home Assistant anlegen (für Dashboard / Benachrichtigungen)."""
    sess, base = ha_session(cfg)
    k = B.meta.get("kurve", {})
    aktionen = [h["titel"] for h in B.hinweise if h["stufe"] == "aktion"]
    werte = {
        "sensor.heizkurve_fusspunkt_vorschlag": (k.get("fp_neu"), "°C", "Heizkurve Fußpunkt Vorschlag",
                                                 {"aktuell": k.get("fp_akt")}),
        "sensor.heizkurve_endpunkt_vorschlag": (k.get("ep_neu"), "°C", "Heizkurve Endpunkt Vorschlag",
                                                {"aktuell": k.get("ep_akt")}),
        "sensor.heizkurve_empfehlungen": (len(aktionen), None, "Heizkurve offene Empfehlungen",
                                          {"liste": aktionen, "warnungen": [h["titel"] for h in B.hinweise
                                                                            if h["stufe"] == "warnung"],
                                           "zusammenfassung": kurzfassung(B)}),
    }
    for eid, (val, unit, name, attrs) in werte.items():
        a = {"friendly_name": name, "icon": "mdi:heat-pump-outline", **attrs}
        if unit:
            a["unit_of_measurement"] = unit
        sess.post(f"{base}/api/states/{eid}", json={"state": "unknown" if val is None else val, "attributes": a},
                  timeout=30).raise_for_status()


def print_summary(B):
    sym = {"aktion": "►", "warnung": "!", "info": "i", "ok": "✓"}
    print()
    for h in sorted(B.hinweise, key=lambda h: ["aktion", "warnung", "info", "ok"].index(h["stufe"])):
        txt = re.sub(r"<[^>]+>", "", h["text"])
        print(f" [{sym[h['stufe']]}] {h['titel']}\n     {txt}\n")


# --------------------------------------------------------------------------- Einrichten

GUESS = {
    "aussen": (r"(outdoor|outside|au(ss|ß)en)", r"(damp|gedämpft|min|max)", "temperature"),
    "vorlauf_soll": (r"((flow|vorlauf).*(set|soll|target))|((set|soll|target|sel).*(flow|vorlauf))", None, "temperature"),
    "vorlauf": (r"(curflow|flow.?temp|vorlauf|supply)", r"(set|soll|target|sel|return|rück|rueck|max|min|design|ausleg)", "temperature"),
    "ruecklauf": (r"(return|rück|rueck|rettemp)", r"(set|soll)", "temperature"),
    "kompressor_starts": (r"(compressor|kompressor|verdichter|comp).*start|start.*(compressor|kompressor|verdichter)", None, None),
    "kompressor": (r"(compressor|kompressor|verdichter)", r"(start|hour|stunde|time|zeit|energy|energie|temp|count)", None),
    "strom_leistung": (r"(heat.?pump|wärmepumpe|waermepumpe|\bwp\b|_wp_|hp|compressor|buderus)", None, "power"),
    "strom_energie": (r"(heat.?pump|wärmepumpe|waermepumpe|\bwp\b|_wp_|buderus).*(consum|verbrauch|strom|electric|import)|(consum|verbrauch|strom|electric).*(heat.?pump|wärmepumpe|wp|buderus)", r"(heat.?output|wärme|waerme|supplied)", "energy"),
    "waerme_energie": (r"(heat.?(output|generated|produced|supplied)|wärmemenge|waermemenge|nrgsupp|meter.?heat|erzeugt)", None, "energy"),
}


def einrichten(args):
    cfg_path = BASE / "config.toml"
    cfg = load_config(cfg_path) if cfg_path.exists() else {k: (dict(v) if isinstance(v, dict) else list(v)) for k, v in DEFAULTS.items()}
    if args.url:
        cfg["homeassistant"]["url"] = args.url
    found, rooms = entdecken(cfg)
    for k in SENSOR_KEYS:
        if not cfg["sensoren"].get(k) and k in found:
            cfg["sensoren"][k] = found[k]
    if not cfg["raum"]:
        cfg["raum"] = rooms
    out = cfg_path if not cfg_path.exists() else BASE / "config.vorschlag.toml"
    out.write_text(render_toml(cfg), encoding="utf-8")
    print(f"\nKonfiguration geschrieben: {out.name}")
    print("Bitte prüfen: Sensor-Zuordnung, Räume, und unter [heizkurve] die aktuellen Werte aus dem Buderus-Regler eintragen.")


def entdecken(cfg):
    """Listet passende Entitäten auf und rät die Zuordnung. Rückgabe: (sensoren, räume)."""
    sess, base = ha_session(cfg)
    r = sess.get(f"{base}/api/states", timeout=60)
    r.raise_for_status()
    states = r.json()

    def label(s):
        a = s.get("attributes", {})
        return f"{s['entity_id']} {a.get('friendly_name', '')}".lower()

    relevant = re.compile(r"(buderus|bosch|ems|km200|wärmepumpe|waermepumpe|heat.?pump|\bwp\b|boiler|thermostat|hc1|hk1|"
                          r"vorlauf|rücklauf|ruecklauf|flow|return|outdoor|außen|aussen|compressor|kompressor|verdichter|fritz|avm|heiz)")
    print("\nGefundene Kandidaten:\n")
    cands = []
    for s in states:
        eid = s["entity_id"]
        dom = eid.split(".")[0]
        if dom not in ("sensor", "binary_sensor", "climate", "number", "select"):
            continue
        a = s.get("attributes", {})
        if dom == "climate" or relevant.search(label(s)):
            cands.append(s)
            unit = a.get("unit_of_measurement", "")
            extra = f"  ist={a.get('current_temperature')} soll={a.get('temperature')}" if dom == "climate" else ""
            print(f"  {eid:<60} {str(s['state'])[:18]:>18} {unit}{extra}")
            if dom != "climate":
                for an, av in a.items():
                    if isinstance(av, (int, float)) and not isinstance(av, bool):
                        print(f"      {eid}#{an} = {av}")

    found = {}
    for key, (inc, exc, dclass) in GUESS.items():
        for s in cands:
            if s["entity_id"].split(".")[0] not in ("sensor", "binary_sensor"):
                continue
            lab = label(s)
            dc = s.get("attributes", {}).get("device_class")
            if dclass and dc not in (dclass, None):
                continue
            if dclass == "temperature" and dc is None and s.get("attributes", {}).get("unit_of_measurement") not in ("°C",):
                continue
            if re.search(inc, lab) and not (exc and re.search(exc, lab)) and s["entity_id"] not in found.values():
                found[key] = s["entity_id"]
                break
    rooms = [{"name": s.get("attributes", {}).get("friendly_name", s["entity_id"]), "entity": s["entity_id"]}
             for s in cands if s["entity_id"].startswith("climate.")
             and not re.search(r"(ems|buderus|bosch|homecom|hc\d|heizkreis|heating.?circuit|dhw|warmwasser)", label(s))]
    print("\nVermutete Zuordnung:")
    for k in SENSOR_KEYS:
        print(f"  {k:<18} {found.get(k, '–')}")
    for r in rooms:
        print(f"  Raum: {r['name']} → {r['entity']}")
    return found, rooms


def render_toml(cfg):
    ha, anl, hk, se = cfg["homeassistant"], cfg["anlage"], cfg["heizkurve"], cfg["sensoren"]
    q = lambda v: json.dumps(v, ensure_ascii=False)
    lines = [
        "[homeassistant]",
        f"url = {q(ha['url'])}",
        f"token = {q(ha.get('token', ''))}   # leer lassen → Umgebungsvariable HA_TOKEN",
        f"zeitzone = {q(ha['zeitzone'])}",
        "",
        "[anlage]",
        f"heizsystem = {q(anl['heizsystem'])}   # \"fussboden\" oder \"heizkoerper\"",
        f"raum_soll = {anl['raum_soll']}            # Standard-Wunschtemperatur",
        f"heizgrenze = {anl['heizgrenze']}          # Tagesmittel außen, ab dem nicht mehr geheizt wird",
        f"komfort_von = {anl['komfort_von']}             # nur diese Stunden werden für die Raumbewertung genutzt",
        f"komfort_bis = {anl['komfort_bis']}",
        f"kompressor_schwelle_w = {anl['kompressor_schwelle_w']}  # nur wenn Verdichter über Leistung erkannt wird",
        "",
        "[heizkurve]   # aktuelle Werte aus dem Buderus-Regler (Heizkreis 1)",
        f"fusspunkt = {hk.get('fusspunkt', 25.0)}       # Vorlauf bei +20 °C außen",
        f"endpunkt = {hk.get('endpunkt', 38.0)}        # Vorlauf bei Norm-Außentemperatur (\"Endpunkt\" bzw. \"Auslegungstemperatur\")",
        f"norm_aussen = {hk.get('norm_aussen', -12.0)}    # minimale Außentemperatur der Auslegung",
        f"max_vorlauf = {hk.get('max_vorlauf', 45.0)}",
        "",
        "[sensoren]   # Entity-IDs aus Home Assistant, leer = nicht vorhanden",
    ]
    hilfe = {"aussen": "Außentemperatur", "vorlauf": "Vorlauf-Ist", "vorlauf_soll": "Vorlauf-Soll Heizkreis",
             "ruecklauf": "Rücklauf", "kompressor": "Verdichter Status/Frequenz/Aktivität",
             "kompressor_starts": "Zähler Verdichterstarts", "strom_leistung": "elektr. Leistung WP in W",
             "strom_energie": "Stromverbrauch WP gesamt kWh", "waerme_energie": "erzeugte Wärme gesamt kWh"}
    for k in SENSOR_KEYS:
        lines.append(f"{k} = {q(se.get(k, ''))}   # {hilfe[k]}")
    for r in cfg["raum"]:
        lines += ["", "[[raum]]", f"name = {q(r['name'])}", f"entity = {q(r['entity'])}"]
        lines.append(f"soll = {r['soll']}" if "soll" in r else "# soll = 21.0      # optional: feste Wunschtemperatur statt AVM-Soll")
        if r.get("fuehrend") is False:
            lines.append("fuehrend = false")
        else:
            lines.append("# fuehrend = false  # Raum nicht für die Heizkurve berücksichtigen")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- Demo-Daten

def demo_data(days=21):
    rng = np.random.default_rng(7)
    end = pd.Timestamp.now(tz="UTC").floor("5min")
    idx = pd.date_range(end - pd.Timedelta(days=days), end, freq="5min")
    tt = (idx - idx[0]).total_seconds().to_numpy() / 86400
    hour = idx.tz_convert("Europe/Berlin").hour.to_numpy() + idx.minute.to_numpy() / 60
    aussen = 7 - 0.35 * tt + 4 * np.sin(2 * np.pi * tt / 9) + 3 * np.sin(2 * np.pi * (hour - 9) / 24) + rng.normal(0, .3, len(idx))
    a12 = pd.Series(aussen).rolling(144, min_periods=1).mean().to_numpy()
    soll_vl = kurve(27, 42, -12, a12)
    on = np.zeros(len(idx), bool)
    state, left = False, 0
    for i, t in enumerate(a12):
        if t > 16:
            state, left = False, 1
        elif left <= 0:
            state = not state
            left = (15 + 2.5 * max(0, 10 - t)) / 5 if state else (10 + 5 * max(0, t)) / 5
        left -= 1
        on[i] = state
    vl = np.where(on, soll_vl + rng.normal(0, .5, len(idx)), np.nan)
    vl = pd.Series(vl).ffill().fillna(25).to_numpy()
    vl = np.where(on, vl, np.maximum(24, vl - 0.0))
    rl = vl - np.where(on, 4 + rng.normal(0, .4, len(idx)), 1)
    starts = np.cumsum(np.r_[0, (np.diff(on.astype(int)) > 0)]) + 15000
    p = np.where(on, 1400 + rng.normal(0, 80, len(idx)), 8)
    cop = 3.1 + 0.08 * aussen
    e_el = np.cumsum(p / 1000 / 12) + 5000
    e_th = np.cumsum(np.where(on, p * cop, 0) / 1000 / 12) + 15000

    def room(fp, ep, lag=36, cap=None, soll=None):
        eq = 21 + (soll_vl - kurve(fp, ep, -12, a12)) / 2.0
        r = pd.Series(eq).ewm(span=lag).mean().to_numpy() + rng.normal(0, .08, len(idx))
        if cap is not None:
            r = np.minimum(r, cap + 0.25 + rng.normal(0, .05, len(idx)))
        return r
    soll_lz = np.where((hour >= 22) | (hour < 6), 17.0, 20.0)
    rows = {
        "sensor.wp_aussentemperatur": aussen, "sensor.wp_vorlauf": vl, "sensor.wp_vorlauf_soll": soll_vl,
        "sensor.wp_ruecklauf": rl, "binary_sensor.wp_verdichter": on.astype(float),
        "sensor.wp_verdichter_starts": starts.astype(float), "sensor.wp_leistung": p,
        "sensor.wp_strom_gesamt": e_el, "sensor.wp_waerme_gesamt": e_th,
        "climate.wohnzimmer#ist": room(26, 46), "climate.wohnzimmer#soll": np.full(len(idx), 21.0),
        "climate.esszimmer#ist": room(23, 39, cap=21.0), "climate.esszimmer#soll": np.full(len(idx), 21.0),
        "climate.lesezimmer#ist": room(25.5, 44, cap=None) - 0.6 - np.where(soll_lz < 18, 0.8, 0), "climate.lesezimmer#soll": soll_lz,
    }
    df = pd.concat([pd.DataFrame({"ts": idx, "key": k, "value": v}) for k, v in rows.items()], ignore_index=True)
    cfg = {k: (dict(v) if isinstance(v, dict) else list(v)) for k, v in DEFAULTS.items()}
    cfg["heizkurve"] = {"fusspunkt": 27.0, "endpunkt": 42.0, "norm_aussen": -12.0, "max_vorlauf": 50.0}
    cfg["sensoren"] = {"aussen": "sensor.wp_aussentemperatur", "vorlauf": "sensor.wp_vorlauf",
                       "vorlauf_soll": "sensor.wp_vorlauf_soll", "ruecklauf": "sensor.wp_ruecklauf",
                       "kompressor": "binary_sensor.wp_verdichter", "kompressor_starts": "sensor.wp_verdichter_starts",
                       "strom_leistung": "sensor.wp_leistung", "strom_energie": "sensor.wp_strom_gesamt",
                       "waerme_energie": "sensor.wp_waerme_gesamt"}
    cfg["raum"] = [{"name": "Wohnzimmer", "entity": "climate.wohnzimmer"},
                   {"name": "Esszimmer", "entity": "climate.esszimmer"},
                   {"name": "Lesezimmer", "entity": "climate.lesezimmer"}]
    return df, cfg, idx[0].to_pydatetime(), idx[-1].to_pydatetime()


# --------------------------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("einrichten", help="Entitäten suchen, config.toml erzeugen")
    p.add_argument("--url", help="Home-Assistant-URL, z. B. http://homeassistant.local:8123")
    p = sub.add_parser("analyse", help="Daten holen und Bericht erzeugen")
    p.add_argument("--tage", type=int, default=10, help="Zeitraum für den Abruf aus HA (Standard 10)")
    p.add_argument("--auswerten", type=int, default=21, help="Zeitraum für die Auswertung inkl. gesammelter Daten (Standard 21)")
    p.add_argument("--offline", action="store_true", help="nur gespeicherte Daten verwenden")
    p.add_argument("--config", default=str(BASE / "config.toml"))
    p.add_argument("--bericht", default=str(BASE / "bericht.html"))
    p = sub.add_parser("demo", help="Bericht mit simulierten Daten")
    p.add_argument("--bericht", default=str(BASE / "bericht_demo.html"))
    a = ap.parse_args()

    if a.cmd == "einrichten":
        einrichten(a)
        return
    if a.cmd == "demo":
        raw, cfg, start, end = demo_data()
    else:
        cfg = load_config(a.config)
        if not cfg["raum"]:
            print("Hinweis: keine [[raum]]-Einträge in config.toml.")
        raw = load_store()
        if not a.offline:
            print(f"Hole {a.tage} Tage aus Home Assistant …")
            raw = merge_store(raw, fetch_history(cfg, a.tage))
        end = dt.datetime.now(dt.timezone.utc)
        start = end - dt.timedelta(days=a.auswerten)
        if len(raw):
            start = max(start, raw["ts"].min().to_pydatetime())
    B, _ = analyse(raw, cfg, pd.Timestamp(start), pd.Timestamp(end))
    write_report(B, cfg, a.bericht)
    print_summary(B)
    print(f"Bericht: {a.bericht}")


if __name__ == "__main__":
    main()

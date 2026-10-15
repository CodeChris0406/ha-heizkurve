"""PV-Überschuss: Komfort-Sollwert der Wärmepumpe stufenweise anheben, solange Solarstrom übrig ist."""
import json
from pathlib import Path

import pandas as pd

import heizkurve as hk

ZUSTAND = Path("/data/pv_regler.json")
STATUS_SENSOR = "sensor.heizkurve_pv_anhebung"


class PVRegler:
    """Hebt den Komfort-Sollwert in 1-K-Stufen an, solange Überschuss anliegt, und nimmt ihn
    bei Netzbezug stufenweise zurück.

    Der Regler hat das letzte Wort: Weicht der Sollwert von seiner Stufe ab (z. B. weil er in
    der App geändert wurde), setzt er ihn zurück.
    """

    EIN_MIN = 10        # so lange muss der Start-Überschuss anliegen
    HOCH_MIN = 10       # so lange muss der Überschuss für die nächste Stufe anliegen
    RUNTER_MIN = 5      # so lange muss Netzbezug anliegen
    SCHRITT_MIN = 15    # Mindestabstand zwischen zwei Änderungen (WP muss reagieren)
    MIN_LAUF_MIN = 30   # Anhebung mindestens so lange halten, bevor zurück auf den Normalwert
    PAUSE_MIN = 30      # nach dem Zurücksetzen frühestens wieder anheben
    SOC_AUS = 85        # Akku darunter → sofort zurück auf den Normalwert
    CLOUD_MIN = 3       # nach einem Befehl so lange warten, bis die Bosch-Cloud den Wert meldet

    def __init__(self, o, cfg, session=None, jetzt=None):
        self.o, self.cfg = o, cfg
        self.sess, self.base = session or hk.ha_session(cfg)
        self._jetzt = jetzt or (lambda: pd.Timestamp.now(tz="UTC"))
        z = json.loads(ZUSTAND.read_text()) if ZUSTAND.exists() else {}
        self.stufe = z.get("stufe", 0)
        ts = lambda k: pd.Timestamp(z[k]) if z.get(k) else None
        self.geaendert, self.aktiv_seit = ts("geaendert"), ts("aktiv_seit")
        self.ein_seit = self.hoch_seit = self.runter_seit = None
        self.gesendet = self.status_zeit = None
        self.grund = z.get("grund", "Regler gestartet")

    # ---- Home Assistant
    def _wert(self, spec):
        eid, _, attr = spec.partition("#")
        r = self.sess.get(f"{self.base}/api/states/{eid}", timeout=20)
        r.raise_for_status()
        s = r.json()
        return hk.to_value(s["attributes"].get(attr) if attr else s["state"])

    def _senden(self, wert, jetzt):
        self.sess.post(f"{self.base}/api/services/number/set_value", timeout=30,
                       json={"entity_id": self.o["pv_sollwert"], "value": wert}).raise_for_status()
        self.gesendet = jetzt

    def _setzen(self, stufe, jetzt, grund, werte):
        basis = float(self.o["pv_basis"])
        self._senden(basis + stufe, jetzt)
        if self.stufe == 0 and stufe > 0:
            self.aktiv_seit = jetzt
        self.stufe, self.geaendert = stufe, jetzt
        self.ein_seit = self.hoch_seit = self.runter_seit = None
        self._melden(f"Sollwert {basis + stufe:g} °C (+{stufe} K): {grund}", werte)

    def _melden(self, grund, werte):
        print(f"PV-Regler: {grund}")
        self.grund = grund
        iso = lambda t: t.isoformat() if t is not None else None
        ZUSTAND.write_text(json.dumps({"stufe": self.stufe, "geaendert": iso(self.geaendert),
                                       "aktiv_seit": iso(self.aktiv_seit), "grund": grund}))
        self._status(werte)

    def _status(self, werte):
        """Status-Sensor schreiben – auch regelmäßig, weil HA ihn bei einem Neustart verwirft."""
        iso = lambda t: t.isoformat() if t is not None else None
        self.status_zeit = self._jetzt()
        self.sess.post(f"{self.base}/api/states/{STATUS_SENSOR}", timeout=30, json={
            "state": "an" if self.stufe > 0 else "aus",
            "attributes": {"friendly_name": "Heizkurve PV-Anhebung", "icon": "mdi:solar-power-variant",
                           "stufe_k": self.stufe, "sollwert": float(self.o["pv_basis"]) + self.stufe,
                           "grund": self.grund, "seit": iso(self.geaendert), **werte}})

    @staticmethod
    def _seit(start, jetzt):
        return (jetzt - start).total_seconds() / 60 if start is not None else 0.0

    # ---- Regelung
    def schritt(self):
        o, sens = self.o, self.cfg["sensoren"]
        jetzt = self._jetzt()
        basis = float(o["pv_basis"])
        stufe_min, stufe_max = int(o["pv_min_anhebung"]), int(o["pv_max_anhebung"])
        soll = self._wert(o["pv_sollwert"])
        phase = self._wert(sens["wp_raum_soll"])  # aktueller Raum-Sollwert: Heizen oder Absenken
        soc = self._wert(o["pv_akku_soc"])
        netz = self._wert(o["pv_netzleistung"])   # + Bezug, − Einspeisung
        aussen = self._wert(sens["aussen"])
        heizstab = self._wert(sens["heizstab_leistung"]) if sens.get("heizstab_leistung") else 0.0
        heizstab = 0.0 if pd.isna(heizstab) else heizstab
        if any(pd.isna(v) for v in (soll, phase, soc, netz)):
            return
        ueberschuss = heizstab - netz  # der Heizstab läuft nur mit Überschuss, zählt also dazu
        werte = {"ueberschuss_w": round(ueberschuss), "akku_soc": soc, "netz_w": round(netz)}
        seit_aenderung = self._seit(self.geaendert, jetzt) if self.geaendert is not None else float("inf")
        if self.status_zeit is None or self._seit(self.status_zeit, jetzt) >= 10:
            self._status(werte)

        ziel = basis + self.stufe
        if abs(soll - ziel) >= 0.05:
            # Abweichung (z. B. in der App geändert) → zurück auf die Stufe des Reglers
            if self._seit(self.gesendet, jetzt) >= self.CLOUD_MIN or self.gesendet is None:
                self._senden(ziel, jetzt)
                self._melden(f"Sollwert {soll:g} °C → {ziel:g} °C korrigiert (+{self.stufe} K)", werte)
            return

        if self.stufe == 0:
            bereit = (phase >= basis - 0.05 and soc >= o["pv_akku_min"]
                      and ueberschuss >= o["pv_ueberschuss_w"]
                      and not (aussen >= o.get("pv_heizgrenze", self.cfg["anlage"]["heizgrenze"])))
            self.ein_seit = (self.ein_seit or jetzt) if bereit else None
            if bereit and self._seit(self.ein_seit, jetzt) >= self.EIN_MIN and seit_aenderung >= self.PAUSE_MIN:
                self._setzen(stufe_min, jetzt, f"Überschuss {ueberschuss:.0f} W, Akku {soc:.0f} %", werte)
            return

        # angehoben: sofort zurück?
        if phase < basis - 0.05:
            return self._setzen(0, jetzt, "Absenkbetrieb begonnen", werte)
        if soc < self.SOC_AUS:
            return self._setzen(0, jetzt, f"Akku unter {self.SOC_AUS} %", werte)

        self.runter_seit = (self.runter_seit or jetzt) if netz > o["pv_bezug_w"] else None
        self.hoch_seit = (self.hoch_seit or jetzt) if ueberschuss >= o["pv_schritt_w"] else None
        if seit_aenderung < self.SCHRITT_MIN:
            return
        if self.runter_seit is not None and self._seit(self.runter_seit, jetzt) >= self.RUNTER_MIN:
            neu = self.stufe - 1 if self.stufe > stufe_min else 0
            if neu == 0 and self._seit(self.aktiv_seit, jetzt) < self.MIN_LAUF_MIN:
                return
            self._setzen(neu, jetzt, f"Netzbezug {netz:.0f} W", werte)
        elif (self.hoch_seit is not None and self._seit(self.hoch_seit, jetzt) >= self.HOCH_MIN
              and self.stufe < stufe_max):
            self._setzen(self.stufe + 1, jetzt, f"weiter Überschuss {ueberschuss:.0f} W", werte)

# Heizkurven-Analyse (Buderus-WP + AVM-Thermostate)

Home-Assistant-Add-on, das den Verlauf von Wärmepumpe und Raumthermostaten auswertet und
Vorschläge für **Fußpunkt** und **Endpunkt** der Heizkurve macht. Außerdem prüft es Takten,
Spreizung, Nachtabsenkung und Arbeitszahl, überwacht die Sensoren und schickt Push-Nachrichten.

Die Änderungen am Regler macht man selbst – das Add-on steuert nichts.

## Funktionen

- Bericht als eigener Menüpunkt **Heizkurve** in der HA-Seitenleiste (Diagramme, Kennzahlen, Empfehlungen)
- Auswertung täglich zur eingestellten Uhrzeit, sofort über „Jetzt neu auswerten“
- Eigener Datenspeicher (Standard 90 Tage), unabhängig von der Recorder-Aufbewahrung von HA
- Kurvenänderungen erst ab 45 Stunden Raumdaten, vorher nur „vorläufiger Trend“
- Nach einer Änderung von Fußpunkt/Endpunkt werden nur Raumdaten ab 12 h danach bewertet
- Sensor-Wächter: nicht verfügbare oder hängende Werte, Zähler die trotz Verdichterlauf nicht steigen
- Push-Nachrichten: neue Empfehlung/Warnung, Beginn einer Kältephase, Tages- oder Wochenbericht
- Sensoren in HA:
  - `sensor.heizkurve_fusspunkt_vorschlag`, `sensor.heizkurve_endpunkt_vorschlag` (Attribut `aktuell`)
  - `sensor.heizkurve_empfehlungen` – Anzahl offener Empfehlungen, Attribute `liste`, `warnungen`, `zusammenfassung`

## PV-Überschuss (optional, `pv_steuerung: true`)

Hebt den Komfort-Sollwert der Wärmepumpe in 1-K-Stufen an, solange Solarstrom übrig ist – das Haus speichert die Wärme.

- **Start** (`pv_basis` + `pv_min_anhebung`): Akku ≥ `pv_akku_min`, Überschuss ≥ `pv_ueberschuss_w` seit 10 min,
  Wärmepumpe im Heizbetrieb, außen unter `pv_heizgrenze` (Standard 18 °C). Überschuss = Einspeisung + Heizstab-Leistung.
- **Nächste Stufe** (bis `pv_max_anhebung`): weiterhin ≥ `pv_schritt_w` Überschuss seit 10 min.
- **Eine Stufe zurück**: Netzbezug > `pv_bezug_w` seit 5 min. Zwischen zwei Änderungen mindestens 15 min,
  zurück auf den Normalwert frühestens nach 30 min. Sofort zurück bei Absenkbetrieb oder Akku < 85 %.
- Der Regler hat das letzte Wort: Weicht der Sollwert von seiner Stufe ab (z. B. in der App geändert), setzt er ihn
  zurück. Für manuelle Tests `pv_steuerung` vorübergehend ausschalten.
- Status: `sensor.heizkurve_pv_anhebung` (`an`/`aus`, Attribut `grund`). Angehobene Stunden werden
  (samt 3 h Nachlauf) nicht für die Heizkurven-Bewertung verwendet.

## Installation (lokales Add-on)

Voraussetzung: Home Assistant OS oder Supervised.

1. Den Ordner `heizkurve/` nach `/addons/heizkurve/` kopieren, z. B. über Studio Code Server oder Samba.
   Liegen die Dateien in `/share/heizkurve`, im Terminal des Studio Code Servers:
   ```bash
   cp -r /share/heizkurve/. /addons/heizkurve/
   ```
2. **Einstellungen → Add-ons → Add-on-Store → ⋮ → Nach Updates suchen** → unter „Lokale Add-ons“
   **Heizkurven-Analyse** installieren bzw. aktualisieren.
3. Reiter **Konfiguration** prüfen, starten, „In Seitenleiste anzeigen“ aktivieren.

Wird ein Sensor `aussen` leer gelassen, listet das Protokoll beim Start alle passenden Entitäten auf.

## Optionen

| Option | Bedeutung |
|---|---|
| `auswerten_um` | Stunde der täglichen Auswertung |
| `wochenbericht_tag` | Tag des Wochenberichts (Mo … So) |
| `push_taeglich` | Tagesbericht nach jeder Auswertung (ersetzt dann den Wochenbericht) |
| `benachrichtigung` | Notify-Dienst, z. B. `notify.mobile_app_mein_handy`; leer = keine Push-Nachrichten |
| `auswerten_tage` / `aufbewahren_tage` | Auswertungszeitraum / Speicherdauer |
| `heizsystem` | `heizkoerper` oder `fussboden` (Faustregel 1 K Raum ≈ 3 bzw. 2 K Vorlauf) |
| `raum_soll`, `heizgrenze`, `komfort_von`, `komfort_bis` | Bewertung der Räume |
| `fusspunkt`, `endpunkt`, `norm_aussen`, `max_vorlauf` | aktuelle Reglereinstellung (0 = aus Messdaten schätzen) |
| `sensoren` | Entity-IDs; `entity#attribut` liest ein Attribut statt des Zustands |
| `raeume` | `name`, `entity` (climate oder Temperatursensor), optional `soll`, `fuehrend: false` |

### Sensoren

| Schlüssel | Bedeutung | nötig |
|---|---|---|
| aussen | Außentemperatur | ja |
| vorlauf_soll | Vorlauf-Soll Heizkreis 1 | optional |
| vorlauf / ruecklauf | Vorlauf-/Rücklauftemperatur | empfohlen |
| kompressor | Verdichter an/aus, Frequenz oder Modulation | für Takt-Analyse |
| kompressor_starts | Zähler Verdichterstarts | optional |
| strom_leistung | elektrische Leistung in W (Alternative zu `kompressor`) | optional |
| heizstab_leistung | Leistung eines Heizstabs, den `strom_leistung` mitmisst – wird abgezogen | optional |
| strom_energie / waerme_energie | Zählerstände in kWh | für Arbeitszahl |

## So entsteht der Vorschlag

- Ausgewertet werden nur Stunden in der Komfortzeit an Heiztagen.
- **Führungsraum** ist der Raum, der im Mittel am weitesten unter seinem Soll liegt.
- Liegt er insgesamt zu hoch oder zu tief → Parallelverschiebung.
- Hängt die Abweichung von der Außentemperatur ab → Steigung anpassen (erst ab 6 K Spanne).
- Höchstens 2 K pro Schritt; danach 3–5 Tage laufen lassen und erneut auswerten.

## Lokal am PC

```bash
py heizkurve/heizkurve.py demo
```
erzeugt einen Beispielbericht mit simulierten Daten. Mit echten Daten (langlebiges Token in `HA_TOKEN`):
```bash
py heizkurve/heizkurve.py einrichten --url http://homeassistant:8123
py heizkurve/heizkurve.py analyse
```

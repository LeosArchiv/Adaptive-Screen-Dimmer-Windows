# Adaptive Screen Dimmer (Windows)

Dunkelt den Bildschirm automatisch ab, sobald er zu hell wird: eine weiße Webseite in einer
dunklen Umgebung, ein Blitz im Spiel, eine Taschenlampe im dunklen Film. Das schont die Augen
und blendet nicht. Läuft leise im Hintergrund, auf beliebig vielen Monitoren, ohne zu flackern.

## Was er kann

- **Abdunkeln ohne Flackern.** Wird es hell, dunkelt er weich und schnell ab. Heller wird es erst
  nach einer kurzen Pause und langsam. Kleine Schwankungen ignoriert er, damit nichts pumpt.
- **Helle Flecken.** Eine kleine, sehr helle Stelle in einem dunklen Bild wird über ihren
  Kontrast zur Umgebung erkannt. Mit „Lokal“ wird nur diese Stelle abgedunkelt, der Rest des
  Bildes bleibt, wie er ist.
- **Blaulichtfilter.** Macht Weiß wärmer, auch auf externen Monitoren. Schwarz bleibt schwarz,
  weil der Filter direkt die Farbkurve des Monitors ändert und keine Farbschicht über das Bild
  legt. Auf Wunsch nur nachts.
- **Wenig Last.** Die Helligkeit wird auf der Grafikkarte gemessen, und nur wenn sich das Bild
  ändert. Im Leerlauf braucht die App etwa 1 % CPU.
- **Ein Profil für alles.** Alle Regler wirken sofort, so lässt sich alles direkt am Bild
  einstellen.
- **Pause jederzeit** mit Strg+Alt+D, über das Symbol im Infobereich oder den Schalter im Fenster.

## Starten

Fertige Programmdateien gibt es nicht, du baust die EXE selbst. Dafür braucht es Python 3.10
oder neuer. Das Fenster nutzt WebView2, das in Windows 11 schon enthalten ist (für Windows 10
gibt es die WebView2 Runtime kostenlos bei Microsoft). Administratorrechte sind nicht nötig.

EXE bauen:
```powershell
./build_exe.bat
```
Danach liegt `dist\AdaptiveScreenDimmer.exe` bereit. Sie läuft ohne Python und wird beim Bauen
kurz testweise gestartet.

Ohne EXE direkt aus dem Quellcode:
```powershell
py -3 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.\adaptive_dimmer_START.bat
```

## Bedienung

- **Oben:** Der große Schalter pausiert und setzt fort, darunter steht der Zustand.
- **Bildschirme:** Für jeden Monitor eine Live-Anzeige. Der weiße Strich zeigt die aktuelle
  Bildhelligkeit, die orange Linie zeigt, wo das Abdunkeln beginnt und wo es voll greift.
  Daneben Abdunkelung und Filter in Prozent und ein Schalter, ob der Monitor mitmacht.
  „Nummern zeigen“ blendet kurz die Nummer auf jedem Monitor ein.
- **Abdunkeln:** Beginn, Volle Stärke ab, Stärkste Abdunkelung (höchstens 94 %, nie ganz
  schwarz), und wie schnell es dunkler und wieder heller wird.
- **Helle Flecken:** Aus, Normal, Stark (ganzer Bildschirm) oder Lokal (nur die grelle Stelle).
  Bei Lokal zusätzlich:
  - Empfindlichkeit: wie viel heller als die Umgebung eine Stelle sein muss.
  - Stärke: wie stark ein Fleck höchstens abgedunkelt wird.
  - Rand: Eng, Normal oder Weit.
  - Ausblenden: wie lange die Abdunkelung braucht, bis sie weg ist.
- **Blaulichtfilter:** an oder aus, Farbtemperatur (weniger Kelvin ist wärmer), Stärke bis 60 %
  und „Nur nachts“ mit zwei Uhrzeiten.
- **Optionen:** Messrate, Tastenkürzel, pausiert starten, Schließen blendet nur aus, minimiert
  starten. Darunter das Protokoll.

Jeder Abschnitt hat einen Knopf „Standardwerte“, der nur diesen Abschnitt zurücksetzt. Alle
Änderungen werden sofort gespeichert, in `%APPDATA%\AdaptiveScreenDimmer\settings.json`.
Das Protokoll liegt daneben in `dimmer.log`.

Optionen beim Start: `--paused` (pausiert starten), `--exit-after SEK` (beendet sich nach so
vielen Sekunden), `--verbose` (ausführliches Protokoll).

## Grenzen

- Spiele im exklusiven Vollbild (ältere DirectX-Titel) liegen über jedem Fenster, dort kann
  nichts abdunkeln. Randloses Fenster oder Fenstermodus funktioniert.
- Geschützte Bildschirme (Benutzerkontensteuerung, Sperrbildschirm) lassen sich nicht messen.
  Dort bleibt der letzte Zustand.
- Kopiergeschützte Videos (etwa Netflix im Browser) erscheinen in der Bildschirmaufnahme schwarz
  und lassen sich deshalb nicht messen. YouTube und lokale Dateien betrifft das nicht.
- Helle Flecken werden in Kacheln von 16 × 16 Pixeln gemessen. Ab etwa 20 bis 30 Pixeln wird ein
  Fleck sicher erkannt, sehr kleine Punkte und dünne Linien nur teilweise.
- Die lokale Abdunkelung hängt etwa zwei Bilder hinter dem Bild her. Bei sehr schnellen Lichtern
  kann kurz ein Rand durchblitzen, „Rand: Weit“ hilft dagegen.

## Entwicklung

```powershell
.venv\Scripts\python -m pip install -r requirements-dev.txt
.\tools\check.ps1          # ruff, Formatierung, mypy, Tests
.\tools\check.ps1 -Live    # zusätzlich Tests mit echten, unsichtbaren Overlays
.\tools\check.ps1 -Build   # zusätzlich EXE bauen und kurz starten
.venv\Scripts\python tools\bench_live.py --monitor 0   # Reaktionszeit und CPU mit künstlichem Blitz
```
`bench_live.py` zeichnet das Testbild mit tkinter und braucht dafür ein Python mit tkinter.
`tools\gui_snapshot.py out.png` speichert ein Bild des Fensters.

Aufbau des Codes in `dimmer/`:

| Datei | Aufgabe |
| --- | --- |
| `logic.py` | Messung, Glättung, Helle Flecken (ohne Windows-Abhängigkeiten) |
| `profiles.py` | das Profil und die Nachtzeit |
| `settings.py` | Einstellungen laden und speichern |
| `engine.py` | ein Thread steuert Messung und alle Overlay-Fenster |
| `gpu.py`, `wgc.py` | Aufnahme mit Windows.Graphics.Capture, Auswertung auf der Grafikkarte |
| `localdim.py` | Maske für die lokale Abdunkelung (DirectComposition) |
| `gamma.py` | Blaulichtfilter über die Farbkurve des Monitors |
| `overlay.py`, `winapi.py` | Overlay-Fenster, Monitore, GDI-Aufnahme als Rückfallebene |
| `gui.py`, `web/` | Fenster mit pywebview |
| `tray.py`, `app.py` | Symbol im Infobereich, Programmstart |

## Lizenz

MIT, siehe [LICENSE](LICENSE).

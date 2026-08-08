# Glasfaser Watchdog

> **[Deutsch](#deutsch) | [English](#english)**

---

## Deutsch

### Was macht das Skript?

Der Glasfaser Watchdog läuft auf einem Raspberry Pi und überwacht die Glasfaser-Internetverbindung. Liegt die externe WAN-IP nicht mehr im erwarteten IP-Bereich des Providers (Standard: `94.134.x.x` für 1&1 Glasfaser), führt er einen VLAN-Reset am Router durch:

1. **Verbindung prüfen** – externe IPv4 abfragen, mehrere Quellen der Reihe nach
2. **Bestätigen** – beim *ersten* Ausfall-Befund passiert **nichts** außer einer Notiz. Erst wenn der nächste Lauf den Ausfall bestätigt, wird eingegriffen (siehe [Karenzzeit](#karenzzeit-und-timer-takt))
3. **Config sichern** – die komplette WAN-Konfiguration als JSON wegschreiben
4. **VLAN deaktivieren** – Router-Weboberfläche per Playwright, VLAN-ID deaktivieren, speichern
5. **30 Sekunden warten** – damit der Provider die PPPoE-Session beendet
6. **VLAN reaktivieren** – VLAN-ID wieder aktivieren, speichern
7. **Verifizieren** – prüfen, dass die WAN-Konfiguration den Toggle überlebt hat (siehe [Absicherung](#absicherung-gegen-config-verlust))
8. **Auf die Verbindung warten** – bis zu 180 s, alle 15 s nachsehen statt einmal blind zu warten
9. **Melden** – Telegram-Nachricht bei Erfolg oder Fehler

Ein systemd-Timer startet das Skript alle 5 Minuten. Ist die Verbindung aktiv, beendet es sich sofort ohne Eingriff.

### Karenzzeit und Timer-Takt

Der Watchdog toggelt **nie** aufgrund einer einzelnen Messung. Beim ersten Ausfall-Befund merkt er sich nur den Zeitpunkt und bricht ab; erst der nächste Lauf kann eingreifen.

Das ist Absicht: Wenn die externe IP gar nicht ermittelbar ist, gilt das als Ausfall — eine Störung des Abfragedienstes würde sonst einen Toggle auslösen, obwohl die Leitung steht. Deshalb auch der Rückfall auf mehrere IP-Quellen.

> ⚠️ **Kopplung über Dateigrenzen**: `GRACE_PERIOD_SEC` muss **kleiner** sein als `OnUnitActiveSec` in der Timer-Unit. Ist sie größer, ist beim zweiten Lauf die Karenzzeit noch nicht abgelaufen und der Toggle verschiebt sich um einen weiteren Lauf. Vorgabe: 5 min Takt, 120 s Karenz.

Praktische Folge: **Der Takt bestimmt die Wartezeit, nicht die Karenzzeit.** Wer schneller eingreifen will, senkt `OnUnitActiveSec` — und zieht `GRACE_PERIOD_SEC` mit nach.

### Absicherung gegen Config-Verlust

Auf dem Testgerät (Archer NX600 v2.0, Firmware Build 260311) ist zweimal die komplette WAN-/PPPoE-Konfiguration aus dem Router verschwunden. Vorausgegangen war jeweils ein vollständiger VLAN-Toggle, bei dem die anschließende PPPoE-Neueinwahl fehlschlug. Das Skript klickt an keiner Stelle „Löschen"; die Löschung erfolgt router-seitig.

Beweislage: **n = 2**, aus Zeitstempeln erschlossen, nie direkt beobachtet, kein reproduzierbares Rezept. Die Absicherung zielt deshalb auf Schadensbegrenzung und Nachweisbarkeit, nicht auf ein Ausweichen um die Ursache herum:

| Schutz | Was er tut |
|---|---|
| Destruktiv-Blacklist | zentraler `safe_click()` blockiert alles, was nach Löschen/Zurücksetzen aussieht |
| Strikte Selektoren | vor jedem Klick muss genau **ein** Treffer existieren |
| Kontext-Guard | Seite/Panel wird vor jeder Aktion anhand eines Markers verifiziert |
| Post-Toggle-Verifikation | nach dem Toggle wird geprüft, ob die WAN-Config noch da und befüllt ist |
| Kill-Switch | schlägt die Verifikation durchgehend fehl, legt sich der Watchdog selbst still |
| Config-Backup | vollständige WAN-Config als JSON vor und nach jedem Toggle (Passwortfelder geschwärzt) |
| Forensik | Screenshot, DOM und Playwright-Trace bei jeder unerwarteten Ausnahme |
| Dry-Run | `--dry-run` protokolliert alle Aktionen und führt keine aus |

**Warum die Verifikation mehrfach misst**: Die Router-Oberfläche baut die Verbindungstabelle bei jedem Statuswechsel neu auf. Währenddessen ist sie kurz leer — einmal gemessen sah das aus wie eine gelöschte Konfiguration, obwohl 11 Sekunden später alles unverändert dastand. Eine Löschung bleibt bestehen, ein Neuaufbau nicht; deshalb zählt erst ein durchgehend negatives Ergebnis. Ein fester `sleep()` würde genau die interessanten Fälle weiter treffen.

### Backoff bei unerwarteten Fehlern

Nicht jeder Ausfall lässt sich durch einen VLAN-Reset beheben. Zwei Fälle werden gesondert behandelt, damit **nicht alle 5 Minuten** vergeblich versucht (und alarmiert) wird:

- **WAN-Verbindung fehlt** – ist die konfigurierte Verbindung (`WAN_NAME`) gar nicht mehr in der Router-Übersicht, hilft kein Reset. Einmalige Telegram-Aufforderung zur manuellen Prüfung.
- **Reset erfolglos** – schlägt der VLAN-Reset `MAX_RESET_FAILURES`-mal in Folge fehl, wird ebenfalls alarmiert.

Danach pausiert der Watchdog für `ALERT_COOLDOWN_SEC` (Standard 6 h): kein weiterer Reset, kein Wiederhol-Alarm. Der günstige IP-Check läuft weiter — sobald die Verbindung zurück ist, wird der Backoff aufgehoben und eine Entwarnung gesendet.

### Voraussetzungen

- Raspberry Pi (getestet auf **Raspberry Pi 5** mit Raspberry Pi OS)
- Python 3.10+ (auf RPi OS vorinstalliert)
- Router: **TP-Link Archer NX600 v2.0** mit deutschsprachiger Weboberfläche
- Provider: **1&1 Glasfaser** (VLAN-ID 7, PPPoE)
- Telegram-Bot (optional, für Benachrichtigungen)

### Installation

```bash
# 1. Repository klonen
git clone https://github.com/<KONTO>/<REPO>.git
cd <REPO>

# 2. Konfiguration anlegen
cp .env.example .env
nano .env   # Werte eintragen (siehe Konfiguration)

# 3. Erst trocken testen – ändert nichts am Router
python3 glasfaser_watchdog.py --dry-run

# 4. Installationsscript ausführen (benötigt sudo für systemd)
chmod +x install.sh
sudo ./install.sh
```

### Konfiguration

Alle Einstellungen über die `.env`-Datei (**niemals committen**, `.gitignore` deckt sie ab). Vollständige Vorlage mit Erläuterungen: [`.env.example`](.env.example).

| Variable | Standard | Beschreibung |
|---|---|---|
| `ROUTER_URL` | `http://192.168.1.1` | IP/URL der Router-Weboberfläche |
| `ROUTER_PASSWORD` | – | Admin-Passwort des Routers (**Pflichtfeld**) |
| `GW_PREFIX` | `94.134.` | IPv4-Präfix des Providers (zur Verbindungserkennung) |
| `VLAN_ID` | `7` | VLAN-ID für die Glasfaser-Verbindung |
| `WAN_NAME` | `1&1` | Name der WAN-Verbindung in der Router-Oberfläche |
| `CHECK_WAIT_SEC` | `180` | Maximale Wartezeit auf den PPPoE-Aufbau (Sekunden) |
| `CHECK_POLL_SEC` | `15` | Prüfabstand innerhalb dieser Wartezeit |
| `IP_QUELLEN` | 3 Dienste | Kommagetrennte Quellen für die externe IP |
| `IP_TIMEOUT_SEC` | `8` | Zeitlimit je IP-Quelle |
| `GRACE_PERIOD_SEC` | `120` | Karenzzeit – **muss kleiner als der Timer-Takt sein** |
| `ALERT_COOLDOWN_SEC` | `21600` | Backoff-Sperrzeit nach unerwartetem Fehler (6 h) |
| `MAX_RESET_FAILURES` | `1` | Fehl-Resets in Folge bis Backoff + Alarm |
| `VERIFY_ATTEMPTS` | `3` | Messungen, bevor „Config zerstört" gilt |
| `VERIFY_RETRY_SEC` | `5` | Abstand zwischen diesen Messungen |
| `WAN_ROW_PROBES` | `3` | Abtastungen beim Zählen der WAN-Zeilen |
| `POST_TOGGLE_WINDOW_SEC` | `900` | Fenster, in dem ein Config-Verlust dem Toggle zugerechnet wird |
| `NOTIFY_FIX_HIT` | `true` | Telegram-Hinweis, wenn ein Neuaufbau abgefangen wurde |
| `DISABLE_FLAG` | neben dem Skript | Kill-Switch-Datei; existiert sie, greift der Watchdog nicht ein |
| `FORENSIC_DIR` | `./forensik` | Ablage für Screenshots, Traces und Config-Backups |
| `KEEP_BACKUPS` | `30` | Anzahl aufbewahrter Config-Backups |
| `STATE_FILE` | neben dem Skript | Laufzeit-Statusdatei |
| `LOG_FILE` | neben dem Skript | Pfad zur Logdatei |
| `TELEGRAM_TOKEN` | – | Telegram-Bot-Token (leer = keine Meldungen) |
| `TELEGRAM_CHAT` | – | Telegram-Chat-ID |

> ⚠️ **IPv4 wird erzwungen** (`curl -4`). Auf Anschlüssen mit IPv6 antworten `ifconfig.me` und `icanhazip.com` sonst mit der IPv6-Adresse. Die fällt durch die IPv4-Prüfung, und die gesamte Erkennung hängt an `GW_PREFIX` — der Rückfall auf weitere Quellen wäre also genau dann wirkungslos, wenn er gebraucht wird.

#### Telegram-Bot einrichten

1. Bei `@BotFather` auf Telegram `/newbot` eingeben → Token kopieren
2. Einmal eine Nachricht an den Bot schicken
3. Chat-ID ermitteln: `https://api.telegram.org/bot<TOKEN>/getUpdates`

### Anpassung für andere Router

Das Skript steuert die Router-Weboberfläche per **Playwright** (Headless-Browser). Für andere Router sind diese Funktionen anzupassen:

| Funktion | Was sie tut |
|---|---|
| `login(page)` | Passwort eingeben, Anmelden-Button klicken |
| `navigate_to_internet(page)` | Zur WAN/Internet-Seite navigieren |
| `click_edit(page)` | Bearbeiten-Icon der richtigen WAN-Verbindung klicken |
| `set_vlan(page, enable)` | VLAN-Checkbox und VLAN-ID-Feld setzen |
| `click_save(page)` | Speichern-Button klicken |
| `verify_wan_intact(...)` | nach dem Toggle prüfen, ob die Config noch existiert |

**Besonderheiten des TP-Link Archer NX600 v2.0** (Firmware Build 260311):

- **Speichern**: `#saveConnBtn`. Ältere Firmware nutzte `button.T_ok`; `button.T_save` speichert WAN-Einstellungen **nicht**.
- **VLAN-Checkbox** (`#vidEn`): CSS-verstecktes Input → per `label[for='vidEn']` klicken. Als Kontext-Marker taugt `#vidEn` deshalb nicht, wohl aber `label[for='vidEn']`.
- **VLAN-ID-Feld** (`#vid`): erst nach Checkbox-Toggle sichtbar
- **Marker der Internet-Seite**: `#addConnIcon` — existiert auch dann, wenn die WAN-Liste leer ist, und ist damit der einzige belastbare Guard
- **Session-Konflikt**: Dialog mit Klasse `btn-msg` → `button.btn-msg:has-text('Anmelden')` klicken
- **Neuaufbau der Tabelle**: Die Verbindungsliste wird bei jedem Statuswechsel neu gerendert und ist dabei kurz leer. `count()` ist eine Momentaufnahme ohne Auto-Wait — einmal zu messen genügt nicht.

### Manueller Test

```bash
# Trockenlauf – ändert nichts
python3 glasfaser_watchdog.py --dry-run

# Einzellauf
python3 glasfaser_watchdog.py

# Systemd-Status
systemctl status glasfaser_watchdog.timer
journalctl -u glasfaser_watchdog.service -f

# Logdatei
tail -f glasfaser_watchdog.log
```

### Fragen, Fehler, Rückmeldungen

Bitte über die **[Issues](../../issues)** dieses Repos — nicht per Mail. Dort sind Fragen
und Antworten für alle sichtbar, und wer dasselbe Problem hat, findet sie wieder.

Hilfreich für eine Rückfrage: Router-Modell und Firmware-Stand, der passende Auszug aus
`glasfaser_watchdog.log` sowie die Ausgabe von `python3 glasfaser_watchdog.py --dry-run`.

> ⚠️ **Was du prüfen solltest, bevor du etwas einfügst**
>
> - **Logdatei und Dry-Run-Ausgabe** sind unbedenklich: Es werden nur Feldnamen
>   protokolliert (`['connname', 'username']`), keine Werte. Weder Passwort noch
>   PPPoE-Benutzername stehen darin. Enthalten ist allerdings die externe IP.
> - **Config-Backups** (`FORENSIC_DIR/wan-config/*.json`): Passwort **und**
>   PPPoE-Benutzername sind geschwärzt. Der Benutzername behält Länge und einen nur
>   lokal vergleichbaren Fingerabdruck (`<redacted:27chars:a1b2c3d4>`), damit beim
>   Vergleich zweier Backups erkennbar bleibt, ob der Router einen Wert *verändert* hat.
> - **Screenshots, DOM-Kopien und Playwright-Traces** aus dem Forensik-Ordner werden
>   *nicht* geschwärzt und können den Benutzernamen enthalten — vor dem Teilen prüfen.

---

## English

### What does the script do?

The Glasfaser Watchdog runs on a Raspberry Pi and monitors a fiber internet connection. If the external WAN IP is no longer in the expected ISP range (default `94.134.x.x` for 1&1 fiber), it performs a VLAN reset on the router:

1. **Check connection** – query the external IPv4 address, several sources in order
2. **Confirm** – the *first* outage reading triggers **nothing** but a note. Only when the next run confirms it does the script act (see [grace period](#grace-period-and-timer-interval))
3. **Back up config** – write the full WAN configuration to JSON
4. **Disable VLAN** – drive the router web UI via Playwright, disable the VLAN ID, save
5. **Wait 30 seconds** – let the ISP tear down the PPPoE session
6. **Re-enable VLAN** – re-enable the VLAN ID, save
7. **Verify** – check that the WAN configuration survived the toggle (see [safeguards](#safeguards-against-config-loss))
8. **Wait for the link** – up to 180 s, polling every 15 s rather than sleeping blindly
9. **Report** – Telegram message on success or failure

A systemd timer runs the script every 5 minutes. If the connection is up, it exits immediately.

### Grace period and timer interval

The watchdog **never** toggles on a single measurement. The first outage reading only records a timestamp and returns; the next run is the earliest that can act.

That is deliberate: if the external IP cannot be determined at all, this counts as an outage — an outage of the *lookup service* would otherwise trigger a toggle while the line is perfectly fine. Hence also the fallback across several IP sources.

> ⚠️ **Cross-file coupling**: `GRACE_PERIOD_SEC` must be **smaller** than `OnUnitActiveSec` in the timer unit. If it is larger, the grace period has not elapsed on the second run and the toggle slips by another interval. Defaults: 5 min interval, 120 s grace.

Practical consequence: **the interval drives the wait, not the grace period.** To act faster, lower `OnUnitActiveSec` — and bring `GRACE_PERIOD_SEC` along.

### Safeguards against config loss

On the test device (Archer NX600 v2.0, firmware Build 260311) the entire WAN/PPPoE configuration vanished from the router twice. Each time it was preceded by a complete VLAN toggle whose subsequent PPPoE re-dial failed. The script never clicks "delete"; the deletion happens router-side.

Evidence: **n = 2**, inferred from timestamps, never directly observed, no reproduction recipe. The safeguards therefore aim at damage limitation and traceability:

| Safeguard | What it does |
|---|---|
| Destructive blacklist | a central `safe_click()` blocks anything resembling delete/reset |
| Strict selectors | exactly **one** match required before any click |
| Context guard | the page/panel is verified against a marker before every action |
| Post-toggle verification | after the toggle, check the WAN config still exists and is populated |
| Kill switch | if verification fails throughout, the watchdog disables itself |
| Config backup | full WAN config as JSON before and after each toggle (password fields redacted) |
| Forensics | screenshot, DOM and Playwright trace on any unexpected exception |
| Dry run | `--dry-run` logs every action and performs none |

**Why verification samples repeatedly**: the router UI rebuilds the connection table on every status change, and it is briefly empty during the rebuild. Measured once, that looked exactly like a deleted configuration — while 11 seconds later everything was still there. A deletion persists, a repaint does not; so only a consistently negative result counts. A fixed `sleep()` would keep hitting precisely the interesting cases.

### Backoff on unexpected errors

Not every outage can be fixed by a VLAN reset. Two cases are handled specially so the script does **not** keep retrying (and alerting) every 5 minutes:

- **WAN connection missing** – if the configured connection (`WAN_NAME`) is gone from the router overview, a reset cannot help. A **single** Telegram request to check manually is sent.
- **Reset unsuccessful** – if the VLAN reset fails `MAX_RESET_FAILURES` times in a row, it alerts as well.

Afterwards the watchdog pauses for `ALERT_COOLDOWN_SEC` (default 6 h). The cheap IP check keeps running — once the connection is back, the backoff is lifted and an all-clear is sent.

### Prerequisites

- Raspberry Pi (tested on **Raspberry Pi 5** with Raspberry Pi OS)
- Python 3.10+ (pre-installed on Raspberry Pi OS)
- Router: **TP-Link Archer NX600 v2.0** with German language UI
- ISP: **1&1 fiber** (VLAN ID 7, PPPoE)
- Telegram bot (optional, for notifications)

### Installation

```bash
# 1. Clone the repository
git clone https://github.com/<KONTO>/<REPO>.git
cd <REPO>

# 2. Create configuration
cp .env.example .env
nano .env   # fill in your values (see Configuration)

# 3. Try it dry first – changes nothing on the router
python3 glasfaser_watchdog.py --dry-run

# 4. Run the install script (requires sudo for systemd)
chmod +x install.sh
sudo ./install.sh
```

### Configuration

All settings live in the `.env` file (**never commit it**; `.gitignore` covers it). Full annotated template: [`.env.example`](.env.example).

| Variable | Default | Description |
|---|---|---|
| `ROUTER_URL` | `http://192.168.1.1` | Router web interface URL |
| `ROUTER_PASSWORD` | – | Router admin password (**required**) |
| `GW_PREFIX` | `94.134.` | ISP IPv4 prefix (for connection detection) |
| `VLAN_ID` | `7` | VLAN ID for the fiber connection |
| `WAN_NAME` | `1&1` | WAN connection name in the router UI |
| `CHECK_WAIT_SEC` | `180` | Maximum wait for PPPoE re-negotiation (seconds) |
| `CHECK_POLL_SEC` | `15` | Polling interval within that wait |
| `IP_QUELLEN` | 3 services | Comma-separated sources for the external IP |
| `IP_TIMEOUT_SEC` | `8` | Timeout per IP source |
| `GRACE_PERIOD_SEC` | `120` | Grace period – **must be smaller than the timer interval** |
| `ALERT_COOLDOWN_SEC` | `21600` | Backoff pause after an unexpected error (6 h) |
| `MAX_RESET_FAILURES` | `1` | Consecutive failed resets before backoff + alert |
| `VERIFY_ATTEMPTS` | `3` | Measurements before declaring the config destroyed |
| `VERIFY_RETRY_SEC` | `5` | Delay between those measurements |
| `WAN_ROW_PROBES` | `3` | Samples when counting WAN rows |
| `POST_TOGGLE_WINDOW_SEC` | `900` | Window in which a config loss is attributed to the toggle |
| `NOTIFY_FIX_HIT` | `true` | Telegram note when a table repaint was caught |
| `DISABLE_FLAG` | next to the script | Kill-switch file; while it exists, the watchdog does not act |
| `FORENSIC_DIR` | `./forensik` | Storage for screenshots, traces and config backups |
| `KEEP_BACKUPS` | `30` | Number of config backups retained |
| `STATE_FILE` | next to the script | Runtime state file |
| `LOG_FILE` | next to the script | Path to the log file |
| `TELEGRAM_TOKEN` | – | Telegram bot token (empty = notifications off) |
| `TELEGRAM_CHAT` | – | Telegram chat ID |

> ⚠️ **IPv4 is forced** (`curl -4`). On IPv6-enabled lines, `ifconfig.me` and `icanhazip.com` otherwise answer with the IPv6 address. That fails the IPv4 check, and the whole detection hinges on `GW_PREFIX` — so the fallback would be useless exactly when it is needed.

#### Setting up a Telegram bot

1. Send `/newbot` to `@BotFather` on Telegram → copy the token
2. Send any message to your new bot
3. Get your chat ID: `https://api.telegram.org/bot<TOKEN>/getUpdates`

### Adapting for other routers

The script drives the router web interface via **Playwright**. For other routers, adapt these functions:

| Function | What it does |
|---|---|
| `login(page)` | Enter password, click the login button |
| `navigate_to_internet(page)` | Navigate to the WAN/Internet settings page |
| `click_edit(page)` | Click the edit icon of the correct WAN connection |
| `set_vlan(page, enable)` | Set the VLAN checkbox and VLAN ID field |
| `click_save(page)` | Click the save button |
| `verify_wan_intact(...)` | Check after the toggle that the config still exists |

**TP-Link Archer NX600 v2.0 quirks** (firmware Build 260311):

- **Save**: `#saveConnBtn`. Older firmware used `button.T_ok`; `button.T_save` does **not** save WAN settings.
- **VLAN checkbox** (`#vidEn`): CSS-hidden input → click via `label[for='vidEn']`. For the same reason `#vidEn` is unusable as a context marker, while `label[for='vidEn']` works.
- **VLAN ID field** (`#vid`): only visible after toggling the checkbox
- **Internet page marker**: `#addConnIcon` — present even when the WAN list is empty, and therefore the only dependable guard
- **Session conflict**: dialog with class `btn-msg` → click `button.btn-msg:has-text('Anmelden')`
- **Table repaint**: the connection list is re-rendered on every status change and briefly empty. `count()` is a snapshot with no auto-wait — measuring once is not enough.

### Manual testing

```bash
# Dry run – changes nothing
python3 glasfaser_watchdog.py --dry-run

# Single run
python3 glasfaser_watchdog.py

# Systemd status
systemctl status glasfaser_watchdog.timer
journalctl -u glasfaser_watchdog.service -f

# Log file
tail -f glasfaser_watchdog.log
```

### Questions, bugs, feedback

Please use this repo's **[Issues](../../issues)** rather than email — that way questions and
answers stay visible, and the next person with the same problem can find them.

Useful when asking: router model and firmware build, the relevant excerpt from
`glasfaser_watchdog.log`, and the output of `python3 glasfaser_watchdog.py --dry-run`.

> ⚠️ **Check this before pasting anything**
>
> - **Log file and dry-run output** are safe to share: only field *names* are logged
>   (`['connname', 'username']`), never values. Neither the password nor the PPPoE
>   username appears. Your external IP does.
> - **Config backups** (`FORENSIC_DIR/wan-config/*.json`): both the password and the
>   PPPoE username are redacted. The username keeps its length plus a fingerprint that is
>   only comparable on this machine (`<redacted:27chars:a1b2c3d4>`), so comparing two
>   backups still reveals whether the router *changed* a value.
> - **Screenshots, DOM dumps and Playwright traces** from the forensics folder are *not*
>   redacted and may contain the username — check before sharing.

---

## License

MIT

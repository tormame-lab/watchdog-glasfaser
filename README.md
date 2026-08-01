# Glasfaser Watchdog

> **[Deutsch](#deutsch) | [English](#english)**

---

## Deutsch

### Was macht das Skript?

Der Glasfaser Watchdog läuft auf einem Raspberry Pi und überwacht kontinuierlich die Glasfaser-Internetverbindung. Wenn die externe WAN-IP nicht mehr im erwarteten IP-Bereich des ISP liegt (Standard: `94.134.x.x` für 1&1 Glasfaser), führt er automatisch einen VLAN-Reset am Router durch:

1. **Verbindung prüfen** – externe IP via `api.ipify.org` abfragen
2. **VLAN deaktivieren** – Router-Weboberfläche per Playwright aufrufen, VLAN-ID deaktivieren, speichern
3. **30 Sekunden warten** – damit der ISP die PPPoE-Session beendet
4. **VLAN reaktivieren** – VLAN-ID 7 wieder aktivieren, speichern
5. **120 Sekunden warten** – Glasfaser braucht ~90s für den PPPoE-Aufbau
6. **Ergebnis prüfen** – Telegram-Benachrichtigung senden (Erfolg oder Fehler)

Ein systemd-Timer startet das Skript alle 5 Minuten. Wenn die Verbindung aktiv ist, beendet sich das Skript sofort ohne Eingriff.

#### Backoff bei unerwarteten Fehlern

Nicht jeder Ausfall lässt sich durch einen VLAN-Reset beheben. Zwei Fälle werden gesondert behandelt, damit **nicht alle 5 Minuten** vergeblich versucht (und alarmiert) wird:

- **WAN-Verbindung fehlt** – ist die konfigurierte Verbindung (`WAN_NAME`) gar nicht mehr in der Router-Übersicht (z. B. Konfig gelöscht), hilft kein Reset. Der Watchdog sendet **einmalig** eine Telegram-Aufforderung zur manuellen Prüfung.
- **Reset wiederholt erfolglos** – schlägt der VLAN-Reset `MAX_RESET_FAILURES`-mal in Folge fehl (mögliches Leitungsproblem), wird ebenfalls alarmiert.

In beiden Fällen pausiert der Watchdog anschließend für `ALERT_COOLDOWN_SEC` (Standard 6 h): kein weiterer Reset, kein Wiederhol-Alarm. Der günstige IP-Check läuft weiter – sobald die Verbindung zurück ist, wird der Backoff automatisch aufgehoben und eine Entwarnung gesendet.

### Voraussetzungen

- Raspberry Pi (getestet auf **Raspberry Pi 5** mit Raspberry Pi OS)
- Python 3.10+ (auf RPi OS vorinstalliert)
- Router: **TP-Link Archer NX600 v2.0** mit deutschsprachiger Weboberfläche
- ISP: **Deutsche Glasfaser / 1&1** (VLAN-ID 7, PPPoE)
- Telegram-Bot (optional, für Benachrichtigungen)

### Installation

```bash
# 1. Repository klonen
git clone https://github.com/DEINNAME/glasfaser-watchdog.git
cd glasfaser-watchdog

# 2. Konfiguration anlegen
cp .env.example .env
nano .env   # Werte eintragen (siehe Abschnitt Konfiguration)

# 3. Installationsscript ausführen (benötigt sudo für systemd)
chmod +x install.sh
sudo ./install.sh
```

### Konfiguration

Alle Einstellungen erfolgen über die `.env`-Datei (niemals committen!):

| Variable | Standard | Beschreibung |
|---|---|---|
| `ROUTER_URL` | `http://192.168.1.1` | IP/URL der Router-Weboberfläche |
| `ROUTER_PASSWORD` | – | Admin-Passwort des Routers (**Pflichtfeld**) |
| `GW_PREFIX` | `94.134.` | IP-Präfix des ISP (zur Verbindungserkennung) |
| `VLAN_ID` | `7` | VLAN-ID für die Glasfaser-Verbindung |
| `WAN_NAME` | `1&1` | Name der WAN-Verbindung in der Router-UI |
| `CHECK_WAIT_SEC` | `120` | Wartezeit nach VLAN-Reset (Sekunden) |
| `TELEGRAM_TOKEN` | – | Telegram Bot-Token (leer = deaktiviert) |
| `TELEGRAM_CHAT` | – | Telegram Chat-ID für Benachrichtigungen |
| `LOG_FILE` | `glasfaser_watchdog.log` | Pfad zur Logdatei |
| `ALERT_COOLDOWN_SEC` | `21600` | Backoff-Sperrzeit nach unerwartetem Fehler (Sek., Standard 6 h) |
| `MAX_RESET_FAILURES` | `2` | Fehl-Resets in Folge bis Backoff + Alarm |
| `STATE_FILE` | `glasfaser_watchdog_state.json` | Pfad zur Laufzeit-Statusdatei |

#### Telegram-Bot einrichten

1. Bei `@BotFather` auf Telegram `/newbot` eingeben → Token kopieren
2. Einmal eine Nachricht an den Bot schicken
3. Chat-ID ermitteln: `https://api.telegram.org/bot<TOKEN>/getUpdates`

### Anpassung für andere Router

Das Skript steuert die Router-Weboberfläche per **Playwright** (Headless-Browser). Für andere Router müssen die folgenden Funktionen in `glasfaser_watchdog.py` angepasst werden:

| Funktion | Was sie tut |
|---|---|
| `login(page)` | Passwort eingeben, Anmelden-Button klicken |
| `navigate_to_internet(page)` | Zur WAN/Internet-Seite navigieren |
| `click_edit(page)` | Bearbeiten-Icon der richtigen WAN-Verbindung klicken |
| `set_vlan(page, enable)` | VLAN-Checkbox und VLAN-ID-Feld setzen |
| `click_save(page)` | Speichern-Button klicken |

**Besonderheiten des TP-Link Archer NX600 v2.0** (wichtig für Anpassungen):

- **Speichern**: Nur `button.T_ok` (nicht `button.T_save`) speichert WAN-Einstellungen
- **VLAN-Checkbox** (`#vidEn`): CSS-verstecktes Input → per `label[for='vidEn']` klicken
- **VLAN-ID-Feld** (`#vid`): erst nach Checkbox-Toggle sichtbar
- **Session-Konflikt**: Dialog mit Klasse `btn-msg` → `button.btn-msg:has-text('Anmelden')` klicken

### Manueller Test

```bash
# Einzellauf
python3 glasfaser_watchdog.py

# Systemd-Status
systemctl status glasfaser_watchdog.timer
journalctl -u glasfaser_watchdog.service -f

# Logdatei
tail -f glasfaser_watchdog.log
```

---

## English

### What does the script do?

The Glasfaser Watchdog runs on a Raspberry Pi and continuously monitors the fiber internet connection. If the external WAN IP is no longer in the expected ISP range (default: `94.134.x.x` for 1&1/Deutsche Glasfaser), it automatically performs a VLAN reset on the router:

1. **Check connection** – query external IP via `api.ipify.org`
2. **Disable VLAN** – open router web UI via Playwright, disable VLAN ID, save
3. **Wait 30 seconds** – to allow the ISP to terminate the PPPoE session
4. **Re-enable VLAN** – re-enable VLAN ID 7, save
5. **Wait 120 seconds** – fiber needs ~90s for PPPoE re-negotiation
6. **Check result** – send Telegram notification (success or failure)

A systemd timer runs the script every 5 minutes. If the connection is active, the script exits immediately without any action.

#### Backoff on unexpected errors

Not every outage can be fixed by a VLAN reset. Two cases are handled specially so the script does **not** keep retrying (and alerting) every 5 minutes:

- **WAN connection missing** – if the configured connection (`WAN_NAME`) is no longer present in the router overview (e.g. config deleted), a reset cannot help. The watchdog sends a **single** Telegram request to check manually.
- **Reset repeatedly unsuccessful** – if the VLAN reset fails `MAX_RESET_FAILURES` times in a row (possible line issue), it alerts as well.

In both cases the watchdog then pauses for `ALERT_COOLDOWN_SEC` (default 6 h): no further reset, no repeated alerts. The cheap IP check keeps running – once the connection is back, the backoff is lifted automatically and an all-clear is sent.

### Prerequisites

- Raspberry Pi (tested on **Raspberry Pi 5** with Raspberry Pi OS)
- Python 3.10+ (pre-installed on Raspberry Pi OS)
- Router: **TP-Link Archer NX600 v2.0** with German language UI
- ISP: **Deutsche Glasfaser / 1&1** (VLAN ID 7, PPPoE)
- Telegram bot (optional, for notifications)

### Installation

```bash
# 1. Clone the repository
git clone https://github.com/YOURNAME/glasfaser-watchdog.git
cd glasfaser-watchdog

# 2. Create configuration
cp .env.example .env
nano .env   # Fill in your values (see Configuration section)

# 3. Run the install script (requires sudo for systemd)
chmod +x install.sh
sudo ./install.sh
```

### Configuration

All settings are stored in the `.env` file (never commit this file!):

| Variable | Default | Description |
|---|---|---|
| `ROUTER_URL` | `http://192.168.1.1` | Router web interface URL |
| `ROUTER_PASSWORD` | – | Router admin password (**required**) |
| `GW_PREFIX` | `94.134.` | ISP IP prefix (for connection detection) |
| `VLAN_ID` | `7` | VLAN ID for fiber connection |
| `WAN_NAME` | `1&1` | WAN connection name in router UI |
| `CHECK_WAIT_SEC` | `120` | Wait time after VLAN reset (seconds) |
| `TELEGRAM_TOKEN` | – | Telegram bot token (empty = disabled) |
| `TELEGRAM_CHAT` | – | Telegram chat ID for notifications |
| `LOG_FILE` | `glasfaser_watchdog.log` | Path to log file |
| `ALERT_COOLDOWN_SEC` | `21600` | Backoff pause after an unexpected error (sec, default 6 h) |
| `MAX_RESET_FAILURES` | `2` | Consecutive failed resets before backoff + alert |
| `STATE_FILE` | `glasfaser_watchdog_state.json` | Path to the runtime state file |

#### Setting up a Telegram bot

1. Send `/newbot` to `@BotFather` on Telegram → copy the token
2. Send any message to your new bot
3. Get your chat ID: `https://api.telegram.org/bot<TOKEN>/getUpdates`

### Adapting for other routers

The script controls the router web interface via **Playwright** (headless browser). For other routers, adapt the following functions in `glasfaser_watchdog.py`:

| Function | What it does |
|---|---|
| `login(page)` | Enter password, click login button |
| `navigate_to_internet(page)` | Navigate to WAN/Internet settings page |
| `click_edit(page)` | Click the edit icon for the correct WAN connection |
| `set_vlan(page, enable)` | Set VLAN checkbox and VLAN ID field |
| `click_save(page)` | Click save button |

**TP-Link Archer NX600 v2.0 quirks** (important for adapting to other routers):

- **Save button**: Only `button.T_ok` (not `button.T_save`) saves WAN connection settings
- **VLAN checkbox** (`#vidEn`): CSS-hidden input → click via `label[for='vidEn']`
- **VLAN ID field** (`#vid`): only visible after toggling the checkbox
- **Session conflict dialog**: class `btn-msg` → click `button.btn-msg:has-text('Anmelden')`

### Manual testing

```bash
# Single run
python3 glasfaser_watchdog.py

# Systemd status
systemctl status glasfaser_watchdog.timer
journalctl -u glasfaser_watchdog.service -f

# Log file
tail -f glasfaser_watchdog.log
```

---

## License

MIT

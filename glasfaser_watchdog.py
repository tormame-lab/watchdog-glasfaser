#!/usr/bin/env python3
"""
Glasfaser Watchdog – Stellt sicher dass die 1&1 Glasfaser-Verbindung aktiv ist.
Wenn die externe WAN-IP nicht auf dem konfigurierten GW_PREFIX liegt, wird die
VLAN-ID am Router zurückgesetzt: deaktivieren → aktivieren.

Bei einem UNERWARTETEN Fehler (z. B. wenn die WAN-Verbindung komplett aus der
Router-Übersicht verschwunden ist) oder wenn der VLAN-Reset wiederholt nicht
hilft, wird NICHT alle paar Minuten neu versucht. Stattdessen fordert der
Watchdog per Telegram einmalig zur manuellen Prüfung auf und pausiert danach für
ALERT_COOLDOWN_SEC (Backoff). Der günstige IP-Check läuft weiter, damit eine
Erholung sofort erkannt und der Backoff aufgehoben wird.

Router: TP-Link Archer NX600 v2.0
ISP:    Deutsche Glasfaser / 1&1
"""

import json
import os
import subprocess
import sys
import time
import logging
import re
import urllib.request
import urllib.parse
from datetime import datetime

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

ROUTER_URL      = os.environ.get("ROUTER_URL", "http://192.168.1.1")
ROUTER_PASSWORD = os.environ["ROUTER_PASSWORD"]
GW_PREFIX       = os.environ.get("GW_PREFIX", "94.134.")
VLAN_ID         = os.environ.get("VLAN_ID", "7")
WAN_NAME        = os.environ.get("WAN_NAME", "1&1")
CHECK_WAIT_SEC  = int(os.environ.get("CHECK_WAIT_SEC", "120"))
LOG_FILE        = os.environ.get("LOG_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "glasfaser_watchdog.log"))

TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT   = os.environ.get("TELEGRAM_CHAT", "")

# ─── Backoff / Alarm-Steuerung ────────────────────────────────────────────────
# STATE_FILE hält Fehlerzähler + Zeitstempel persistent, damit bei einem
# unerwarteten Fehler nicht endlos alle paar Minuten neu versucht (und alarmiert)
# wird. Alle Werte über Umgebungsvariablen überschreibbar.
STATE_FILE         = os.environ.get("STATE_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "glasfaser_watchdog_state.json"))
ALERT_COOLDOWN_SEC = int(os.environ.get("ALERT_COOLDOWN_SEC", str(6 * 3600)))  # Sperrzeit für erneute Versuche + Wiederhol-Alarme
MAX_RESET_FAILURES = int(os.environ.get("MAX_RESET_FAILURES", "2"))            # nach so vielen Fehl-Resets in Folge: Backoff + Alarm

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE),
    ],
)
log = logging.getLogger(__name__)


# ─── Telegram ────────────────────────────────────────────────────────────────

def send_telegram(text: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT, "text": text}).encode()
        urllib.request.urlopen(url, data=data, timeout=10)
    except Exception as e:
        log.warning("Telegram-Benachrichtigung fehlgeschlagen: %s", e)


# ─── Backoff-Status (persistent) ──────────────────────────────────────────────

class WanConnectionMissing(Exception):
    """Die erwartete WAN-Verbindung existiert nicht (mehr) in der Router-Übersicht."""


def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state: dict) -> None:
    # Atomar schreiben (temp + os.replace), damit ein paralleler load_state() nie
    # eine halb geschriebene Datei liest und fälschlich {} zurückgibt.
    try:
        tmp = f"{STATE_FILE}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log.warning("Backoff-Status speichern fehlgeschlagen: %s", e)


def clear_state() -> None:
    try:
        os.remove(STATE_FILE)
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("Backoff-Status löschen fehlgeschlagen: %s", e)


# ─── Netzwerk-Prüfung ────────────────────────────────────────────────────────

def get_external_ip() -> str | None:
    """Fragt die externe WAN-IP via curl ab (Timeout 10s)."""
    try:
        r = subprocess.run(
            ["curl", "-s", "--max-time", "10", "https://api.ipify.org"],
            capture_output=True, text=True, timeout=15
        )
        ip = r.stdout.strip()
        if re.match(r"^\d+\.\d+\.\d+\.\d+$", ip):
            return ip
    except Exception as e:
        log.warning("Fehler beim Abrufen der externen IP: %s", e)
    return None


def is_glasfaser_active() -> bool:
    """Prüft ob die externe WAN-IP im konfigurierten GW_PREFIX-Bereich liegt."""
    ip = get_external_ip()
    if ip:
        log.info("Externe WAN-IP: %s", ip)
        return ip.startswith(GW_PREFIX)
    log.warning("Externe IP nicht ermittelbar – nehme Glasfaser als inaktiv an.")
    return False


# ─── Router-Automation ───────────────────────────────────────────────────────

def login(page) -> None:
    log.info("Öffne Router-Login: %s", ROUTER_URL)
    page.goto(ROUTER_URL, wait_until="domcontentloaded", timeout=30_000)
    page.fill("input[type='password']", ROUTER_PASSWORD)

    # Ersten Anmelden-Button klicken (normaler Login-Button)
    page.locator("button:has-text('Anmelden')").first.click()

    # Falls Session-Konflikt-Dialog erscheint: hat Klasse btn-msg
    try:
        page.wait_for_selector("button.btn-msg:has-text('Anmelden')", timeout=5_000)
        log.info("Session-Konflikt-Dialog – klicke Anmelden (btn-msg)")
        page.locator("button.btn-msg:has-text('Anmelden')").click()
    except PWTimeout:
        pass  # Kein Dialog → direkter Login

    page.wait_for_load_state("networkidle", timeout=25_000)
    page.wait_for_timeout(1000)
    log.info("Login erfolgreich.")


def navigate_to_internet(page) -> None:
    """Navigiert zu Erweiterte Einstellungen → Netzwerk → Internet."""
    log.info("Navigiere zu Erweiterte Einstellungen → Netzwerk → Internet")

    page.locator(
        "a:has-text('Erweiterte Einstellungen'), "
        "span:has-text('Erweiterte Einstellungen')"
    ).first.click()
    page.wait_for_load_state("networkidle", timeout=15_000)

    page.locator("a:has-text('Netzwerk'), span:has-text('Netzwerk')").first.click()
    page.wait_for_load_state("networkidle", timeout=15_000)

    page.locator("a:has-text('Internet')").filter(has_not_text="Provider").first.click()
    page.wait_for_load_state("networkidle", timeout=15_000)
    log.info("Internet-Seite geöffnet.")


def click_edit(page) -> None:
    """Klickt das Bearbeiten-Icon (span.edit-modify-icon) in der WAN-Zeile."""
    log.info("Suche WAN-Verbindung '%s' und öffne Edit-Formular", WAN_NAME)
    row = page.locator("tr").filter(has_text=WAN_NAME).first
    try:
        row.wait_for(timeout=10_000)
    except PWTimeout:
        # WAN-Verbindung fehlt in der Übersicht → anderer als der erwartete Fehler.
        raise WanConnectionMissing(
            f"WAN-Verbindung '{WAN_NAME}' nicht in der Router-Übersicht gefunden"
        )
    row.locator("span.edit-modify-icon").click()
    page.wait_for_load_state("networkidle", timeout=15_000)
    log.info("Edit-Formular geöffnet.")


def click_save(page) -> None:
    """Klickt den OK-Button (T_ok) des WAN-Edit-Formulars.

    T_ok ist der einzige tatsächliche Save-Button für WAN-Verbindungseinstellungen.
    Er speichert UND testet die Verbindung. T_save (id='t_save') gehört zu einer
    anderen Sektion und speichert NICHT die WAN-Verbindungseinstellungen.
    """
    for btn in page.locator("button.T_ok").all():
        try:
            if btn.is_visible():
                btn.click()
                page.wait_for_timeout(2_000)
                log.info("OK (T_ok) geklickt – Router baut Verbindung auf.")
                return
        except Exception:
            pass
    raise RuntimeError("Kein sichtbarer OK-Button (T_ok) gefunden")


def set_vlan(page, enable: bool) -> None:
    """Aktiviert oder deaktiviert die VLAN-ID Checkbox (#vidEn) und trägt die ID ein.

    TP-Link blendet die echten <input>-Elemente per CSS aus:
    - Checkbox: Interaktion über label[for='vidEn'] statt über das Input-Element
    - VLAN-ID-Feld (#vid): erst nach Checkbox-Toggle sichtbar
    """
    vlan_cb = page.locator("#vidEn")
    vlan_cb.wait_for(state="attached", timeout=10_000)
    is_checked = vlan_cb.is_checked()
    log.info("VLAN-Checkbox aktuell: %s", is_checked)

    if enable and not is_checked:
        log.info("VLAN-Checkbox aktivieren")
        page.locator("label[for='vidEn']").click()
        page.locator("#vid").wait_for(state="visible", timeout=5_000)
    elif not enable and is_checked:
        log.info("VLAN-Checkbox deaktivieren")
        page.locator("label[for='vidEn']").click()
        page.wait_for_timeout(300)
    elif enable and is_checked:
        log.info("VLAN-Checkbox bereits aktiviert")
        page.locator("#vid").wait_for(state="visible", timeout=5_000)
    else:
        log.info("VLAN-Checkbox bereits deaktiviert")

    if enable:
        page.locator("#vid").fill(VLAN_ID)
        actual = page.evaluate("() => document.getElementById('vid')?.value")
        log.info("VLAN-ID gesetzt: %s (erwartet: %s)", actual, VLAN_ID)

    click_save(page)


def reset_vlan(page) -> None:
    """Führt den VLAN-Reset-Zyklus durch: deaktivieren → warten → aktivieren."""
    navigate_to_internet(page)

    log.info("=== Schritt 1: VLAN-ID deaktivieren ===")
    click_edit(page)
    set_vlan(page, enable=False)

    log.info("Warte 30s damit ISP die VLAN-Session zurücksetzen kann…")
    page.wait_for_timeout(30_000)

    navigate_to_internet(page)

    log.info("=== Schritt 2: VLAN-ID %s aktivieren ===", VLAN_ID)
    click_edit(page)
    set_vlan(page, enable=True)

    log.info("VLAN-Reset abgeschlossen.")


# ─── Hauptprogramm ───────────────────────────────────────────────────────────

def _enter_backoff(state: dict, now: float, reason: str, message: str) -> None:
    """Setzt Backoff-Fenster und sendet höchstens einmal pro Cooldown eine Nachricht."""
    if now - state.get("last_alert_ts", 0) >= ALERT_COOLDOWN_SEC:
        send_telegram(message)
        state["last_alert_ts"] = now
    else:
        log.info("Alarm unterdrückt (Cooldown aktiv) – Grund: %s", reason)
    state["suppress_until"] = now + ALERT_COOLDOWN_SEC
    state["reason"] = reason
    save_state(state)


def main() -> None:
    log.info("=== Glasfaser Watchdog (%s) ===", datetime.now().isoformat())

    if is_glasfaser_active():
        log.info("Glasfaser-Gateway aktiv – kein Eingriff nötig.")
        # Erholung erkannt → Backoff-/Fehlerstatus zurücksetzen
        if load_state():
            log.info("Verbindung wieder aktiv – hebe Backoff auf.")
            send_telegram("✅ Glasfaser wieder aktiv – Watchdog-Backoff aufgehoben.")
            clear_state()
        return

    state = load_state()
    now = time.time()

    # Backoff aktiv? Dann keinen weiteren Reset – nur still beenden (IP-Check lief bereits).
    suppress_until = state.get("suppress_until", 0)
    if now < suppress_until:
        mins = int((suppress_until - now) / 60)
        log.warning(
            "Backoff aktiv (Grund: %s) – überspringe VLAN-Reset, manuelle Prüfung "
            "ausstehend (noch ~%d min).", state.get("reason", "?"), mins
        )
        return

    log.info("Glasfaser-Gateway NICHT aktiv – starte Router-Konfiguration.")
    # "Starte Reset"-Hinweis nur einmal pro Fehler-Serie (kein Spam pro Lauf)
    if state.get("fail_count", 0) == 0:
        send_telegram("⚠️ Glasfaser nicht aktiv – starte VLAN-Reset am Router…")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            ignore_https_errors=True,
            viewport={"width": 1280, "height": 900}
        )
        page = context.new_page()

        try:
            login(page)
            reset_vlan(page)
        except WanConnectionMissing as e:
            # UNERWARTETER Fehler: Verbindung fehlt → per VLAN-Reset nicht reparierbar.
            log.error("Unerwarteter Fehler – manuelle Prüfung nötig: %s", e)
            try:
                page.screenshot(path="/tmp/glasfaser_watchdog_error.png")
                log.info("Fehler-Screenshot: /tmp/glasfaser_watchdog_error.png")
            except Exception:
                pass
            browser.close()
            _enter_backoff(
                state, now, "wan_missing",
                f"❗ Watchdog: WAN-Verbindung '{WAN_NAME}' ist im Router NICHT vorhanden.\n"
                f"Das ist ein ANDERER als der erwartete Fehler – ein automatischer "
                f"VLAN-Reset hilft hier nicht (Router-Konfig fehlt oder Leitungsproblem).\n"
                f"➡️ Bitte manuell am Router ({ROUTER_URL}) nachschauen.\n"
                f"Automatik für {ALERT_COOLDOWN_SEC // 3600}h ausgesetzt."
            )
            return
        except Exception as e:
            log.error("Fehler bei der Router-Konfiguration: %s", e)
            try:
                page.screenshot(path="/tmp/glasfaser_watchdog_error.png")
                log.info("Fehler-Screenshot: /tmp/glasfaser_watchdog_error.png")
            except Exception:
                pass
            browser.close()
            state["fail_count"] = state.get("fail_count", 0) + 1
            if state["fail_count"] >= MAX_RESET_FAILURES:
                _enter_backoff(
                    state, now, "reset_error",
                    f"❌ Watchdog: VLAN-Reset {state['fail_count']}x in Folge fehlgeschlagen "
                    f"({e}).\nMöglicherweise ein Leitungs-/Router-Problem statt der üblichen "
                    f"PPPoE-Trennung.\n➡️ Bitte manuell nachschauen. "
                    f"Automatik für {ALERT_COOLDOWN_SEC // 3600}h ausgesetzt."
                )
            else:
                save_state(state)
            return

        browser.close()

    log.info("Warte %d Sekunden auf Verbindungsaufbau…", CHECK_WAIT_SEC)
    time.sleep(CHECK_WAIT_SEC)

    if is_glasfaser_active():
        ip = get_external_ip()
        log.info("Glasfaser aktiv (externe IP). Reparatur erfolgreich.")
        send_telegram(f"✅ Glasfaser wieder aktiv! Externe IP: {ip}")
        clear_state()
    else:
        ip = get_external_ip()
        log.error(
            "Glasfaser nach %ds noch nicht aktiv. Externe IP aktuell: %s",
            CHECK_WAIT_SEC, ip
        )
        # Reset lief technisch durch, brachte aber keine Verbindung → als Fehlversuch werten.
        state["fail_count"] = state.get("fail_count", 0) + 1
        if state["fail_count"] >= MAX_RESET_FAILURES:
            _enter_backoff(
                state, now, "no_connection_after_reset",
                f"❌ VLAN-Reset {state['fail_count']}x ohne Erfolg – Glasfaser weiterhin "
                f"inaktiv (IP: {ip}). Evtl. Leitungsproblem.\n"
                f"➡️ Bitte manuell prüfen. Automatik für {ALERT_COOLDOWN_SEC // 3600}h ausgesetzt."
            )
        else:
            send_telegram(
                f"❌ VLAN-Reset fehlgeschlagen – Glasfaser nach {CHECK_WAIT_SEC}s "
                f"noch nicht aktiv. IP: {ip}. Neuer Versuch beim nächsten Lauf."
            )
            save_state(state)


if __name__ == "__main__":
    main()

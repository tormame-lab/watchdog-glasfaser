#!/usr/bin/env python3
"""
Glasfaser Watchdog – Stellt sicher dass die 1&1 Glasfaser-Verbindung aktiv ist.
Wenn die externe WAN-IP nicht auf dem konfigurierten GW_PREFIX liegt, wird die
VLAN-ID am Router zurückgesetzt: deaktivieren → aktivieren.

Router: TP-Link Archer NX600 v2.0
ISP:    Deutsche Glasfaser / 1&1
"""

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
    row.wait_for(timeout=10_000)
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

def main() -> None:
    log.info("=== Glasfaser Watchdog (%s) ===", datetime.now().isoformat())

    if is_glasfaser_active():
        log.info("Glasfaser-Gateway aktiv – kein Eingriff nötig.")
        return

    log.info("Glasfaser-Gateway NICHT aktiv – starte Router-Konfiguration.")
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
        except Exception as e:
            log.error("Fehler bei der Router-Konfiguration: %s", e)
            try:
                page.screenshot(path="/tmp/glasfaser_watchdog_error.png")
                log.info("Fehler-Screenshot: /tmp/glasfaser_watchdog_error.png")
            except Exception:
                pass
            browser.close()
            sys.exit(1)

        browser.close()

    log.info("Warte %d Sekunden auf Verbindungsaufbau…", CHECK_WAIT_SEC)
    time.sleep(CHECK_WAIT_SEC)

    if is_glasfaser_active():
        ip = get_external_ip()
        log.info("Glasfaser aktiv (externe IP). Reparatur erfolgreich.")
        send_telegram(f"✅ Glasfaser wieder aktiv! Externe IP: {ip}")
    else:
        ip = get_external_ip()
        log.error(
            "Glasfaser nach %ds noch nicht aktiv. Externe IP aktuell: %s",
            CHECK_WAIT_SEC, ip
        )
        send_telegram(f"❌ VLAN-Reset fehlgeschlagen – Glasfaser nach {CHECK_WAIT_SEC}s noch nicht aktiv. IP: {ip}")
        sys.exit(2)


if __name__ == "__main__":
    main()

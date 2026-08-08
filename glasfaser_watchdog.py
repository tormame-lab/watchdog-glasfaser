#!/usr/bin/env python3
"""
Glasfaser Watchdog – Stellt sicher, dass die Glasfaser-Verbindung aktiv ist.
Liegt die externe IP nicht im erwarteten Präfix (GW_PREFIX), wird die VLAN-ID
am Router zurückgesetzt: deaktivieren → aktivieren.

Konfiguration ausschließlich über Umgebungsvariablen, siehe .env.example.
Dieses Repo enthält KEINE Zugangsdaten.

Router: TP-Link Archer NX600

────────────────────────────────────────────────────────────────────────────────
SICHERHEITS-HÄRTUNG (2026-08-04)
────────────────────────────────────────────────────────────────────────────────
Hintergrund: Am 2026-07-31 und 2026-08-04 verschwand die komplette 1&1-WAN-/
PPPoE-Konfiguration aus dem Router. In BEIDEN Fällen war das unmittelbar
vorausgehende Ereignis ein vollständig durchgelaufener VLAN-Toggle-Zyklus,
bei dem der anschließende PPPoE-Neuaufbau fehlschlug. Vor dem Firmware-Update
(Build 260311) blieben identische Fehlschläge 9x folgenlos.

Das Skript klickt an KEINER Stelle „Löschen“. Die Löschung erfolgt router-seitig.
Der Toggle ist der Auslöser, nicht der Löscher. Die Härtung zielt deshalb primär
auf Schadensbegrenzung und Nachweisbarkeit (D/E/F), nicht auf Klick-Vermeidung.

  A) Destruktiv-Blacklist  – zentraler safe_click(), blockiert Lösch-Elemente
  B) Strikte Selektoren    – exakt-ein-Treffer-Pflicht vor jedem Klick
  C) Kontext-Guard         – Seite/Panel vor jeder Aktion verifizieren
  D) Post-Toggle-Verify    – WAN-Config nach Toggle prüfen, sonst Kill-Switch
  E) Forensik              – Screenshot + Playwright-Trace bei jeder Exception
  F) Config-Backup         – WAN-Config vor jedem Toggle als JSON sichern
  G) Dry-Run               – --dry-run loggt alle Aktionen, führt keine aus
"""

import argparse
import glob
import hashlib
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

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout


# ─── Konfiguration ───────────────────────────────────────────────────────────
# Alles über Umgebungsvariablen, Vorgaben in Klammern. Vorlage: .env.example.
# Im Repo stehen bewusst KEINE Zugangsdaten – die Produktivfassung auf dem Pi
# trägt sie direkt im Skript, diese Fassung nicht.
HIER = os.path.dirname(os.path.abspath(__file__))


def _env_int(name: str, vorgabe: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or vorgabe)
    except ValueError:
        raise SystemExit(f"{name} muss eine ganze Zahl sein, ist aber "
                         f"{os.environ[name]!r}")


def _env_bool(name: str, vorgabe: bool) -> bool:
    wert = os.environ.get(name, "").strip().lower()
    if not wert:
        return vorgabe
    if wert in ("1", "true", "yes", "ja", "on"):
        return True
    if wert in ("0", "false", "no", "nein", "off"):
        return False
    raise SystemExit(f"{name} muss wahr/falsch sein, ist aber {wert!r}")

ROUTER_URL      = os.environ.get("ROUTER_URL", "http://192.168.1.1")
# Einzige Pflichtangabe. Wird in main() geprüft, damit statt eines KeyError-
# Rückverfolgungsprotokolls eine verständliche Meldung erscheint.
ROUTER_PASSWORD = os.environ.get("ROUTER_PASSWORD", "")
GW_PREFIX       = os.environ.get("GW_PREFIX", "94.134.")
VLAN_ID         = os.environ.get("VLAN_ID", "7")
WAN_NAME        = os.environ.get("WAN_NAME", "1&1")   # Teilstring genügt
# Maximale Wartezeit auf den PPPoE-Neuaufbau nach dem Toggle (vorher 120s).
# Wird in Schritten von CHECK_POLL_SEC abgefragt statt einmal blind abgewartet.
CHECK_WAIT_SEC  = _env_int("CHECK_WAIT_SEC", 180)
CHECK_POLL_SEC  = _env_int("CHECK_POLL_SEC", 15)

# Quellen für die externe IP, der Reihe nach. Erst wenn ALLE schweigen, gilt die
# IP als nicht ermittelbar – und das zählt als Ausfall. Details in
# get_external_ip(). Alle drei liefern die nackte IP als Klartext, ohne JSON.
#
# curl wird mit -4 aufgerufen, und das ist NICHT optional: Der Anschluss hat
# IPv6, und ifconfig.me wie icanhazip.com antworten dann mit der IPv6-Adresse
# (am 2026-08-08 nachgemessen). Die fällt durch die IPv4-Prüfung und die
# ganze Erkennung hängt ohnehin am IPv4-Präfix GW_PREFIX. Ohne -4 wäre der
# Rückfall also wirkungslos – er würde genau dann nichts liefern, wenn er
# gebraucht wird.
IP_QUELLEN = [q.strip() for q in os.environ.get(
    "IP_QUELLEN",
    "https://api.ipify.org,https://ifconfig.me/ip,https://icanhazip.com"
).split(",") if q.strip()]
# Pro Quelle. Schlimmster Fall bei drei toten Quellen: ~24 s – unkritisch bei
# einem 5-Minuten-Takt, und dieser Fall bedeutet ohnehin, dass etwas kaputt ist.
IP_TIMEOUT_SEC = _env_int("IP_TIMEOUT_SEC", 8)

# Leer = keine Benachrichtigung. send_telegram() prüft das und schweigt still.
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT   = os.environ.get("TELEGRAM_CHAT", "")

# ─── Backoff / Alarm-Steuerung ────────────────────────────────────────────────
STATE_FILE         = os.environ.get(
    "STATE_FILE", os.path.join(HIER, "glasfaser_watchdog_state.json"))
ALERT_COOLDOWN_SEC = _env_int("ALERT_COOLDOWN_SEC", 6 * 3600)  # Sperrzeit
# Auf 1 gesetzt (2026-08-04, Entscheidung des Nutzers): Ein Toggle, und wenn der
# PPPoE-Reconnect danach scheitert → sofort Backoff + Alarm statt eines zweiten
# Zyklus. Beide WAN-Verluste traten zwar bereits beim ERSTEN Toggle auf – das
# verhindert die Zerstörung also nicht –, aber es unterbindet einen weiteren
# riskanten Toggle, solange die Lage ungeklärt ist.
MAX_RESET_FAILURES = _env_int("MAX_RESET_FAILURES", 1)

# (D) Fenster, in dem ein fehlender WAN-Eintrag dem vorherigen Toggle
# zugerechnet wird. Am 2026-08-04 lag zwischen dem letzten T_ok (12:43:20) und
# der bestätigten Löschung (12:47:51) über 4 Minuten – die Löschung erfolgt also
# verzögert. Findet die Sofort- und die späte Verifikation den Eintrag noch vor,
# schlägt der Verlust erst beim NÄCHSTEN Lauf auf. Ohne diese Zuordnung liefe er
# dort in den generischen 6h-Backoff, und der Kill-Switch (D) würde ausgerechnet
# im Zielszenario nicht greifen.
POST_TOGGLE_WINDOW_SEC = _env_int("POST_TOGGLE_WINDOW_SEC", 15 * 60)

# (D) Bestätigungs-Verifikation. Am 2026-08-07 03:46:39 meldete die Verifikation
# eine zerstörte WAN-Config, obwohl die Zeile 11 Sekunden später (Forensik-DOM
# und Screenshot) unverändert und mit Status „Verbunden" dastand: Die Zeilenzahl
# fiel innerhalb von 129 ms von 1 auf 0. Ursache ist die Router-Oberfläche, die
# die Verbindungstabelle bei jedem Statuswechsel neu aufbaut – das tbody ist
# dabei kurz leer, und count() ist eine Momentaufnahme ohne Auto-Wait.
# Verschärfend: Der Repaint ist an den ERFOLGSFALL gekoppelt (Getrennt →
# Verbunden), nicht seltener Zufall. Ob er die Verifikation trifft, hängt nur
# daran, wie schnell der Reconnect kommt: Am 07.08. stand die Leitung nach 24s,
# die Prüfung lief bei +13s – Treffer. Bei den Toggles davor kam sie erst nach
# 31/44/46s, der Repaint landete also nach der Messung. Alle vier Toggles waren
# erfolgreich; der Unterschied war reines Timing.
# Deshalb zählt erst ein durchgehend negatives Ergebnis: Eine Löschung bleibt
# bestehen, ein Repaint nicht. Diese Asymmetrie ist der ganze Fix – ein fester
# sleep() würde genau die interessanten Fälle weiter treffen.
VERIFY_ATTEMPTS  = _env_int("VERIFY_ATTEMPTS", 3)
VERIFY_RETRY_SEC = _env_int("VERIFY_RETRY_SEC", 5)

# Diagnose-Meldung, wenn der Retry im Feld wirklich einen Repaint abgefangen hat.
# Auf Wunsch des Nutzers (2026-08-07) eingebaut, um den Fix im Echtbetrieb zu
# belegen. Feuert nur im Trefferfall, ist also nach ein paar Ausfällen erledigt –
# dann hier auf False setzen.
NOTIFY_FIX_HIT = _env_bool("NOTIFY_FIX_HIT", True)

# Auto-Wait vor dem Zählen der WAN-Zeilen (gleiche Absicherung wie in
# resolve_unique()): wartet einen laufenden Repaint aus, statt mitten hinein
# zu messen.
WAN_ROW_WAIT_MS = _env_int("WAN_ROW_WAIT_MS", 10_000)

# wait_for() und count() sind zwei getrennte Runden zur Seite; faellt der Repaint
# genau dazwischen, misst count() trotz erfolgreichem Warten 0. Gemessen: 1–2 von
# 40 Messungen unter aggressivem Repaint. Deshalb mehrfach abtasten – dieselbe
# Asymmetrie wie oben: Eine Löschung liefert in JEDER Messung 0, ein Repaint nicht.
WAN_ROW_PROBES   = _env_int("WAN_ROW_PROBES", 3)
WAN_ROW_RETRY_MS = _env_int("WAN_ROW_RETRY_MS", 2_000)

# Karenzzeit: Ein Ausfall muss SO LANGE ununterbrochen bestehen, bevor überhaupt
# getoggelt wird. Trennt „hinschauen" von „eingreifen" – die IP-Prüfung bleibt
# billig und häufig, der riskante Toggle wird seltener.
# Bewusst zeitbasiert statt zählerbasiert: bleibt korrekt, wenn das Timer-
# Intervall geändert wird.
#
# WICHTIG zum Verständnis: Die eigentliche Schutzwirkung kommt NICHT von dieser
# Zahl, sondern daraus, dass beim ersten Ausfall-Befund unbedingt abgebrochen
# wird (siehe main()). Es braucht immer ZWEI Läufe. Diese Zahl muss deshalb nur
# kleiner als das Timer-Intervall sein – sonst verzögert sie zusätzlich.
# 2026-08-08 von 10 min auf 2 min gesenkt, zusammen mit dem Timer 15 min → 5 min.
# Bei 10 min wäre die Karenz jetzt größer als der Takt und würde den Toggle um
# einen weiteren Lauf verschleppen.
GRACE_PERIOD_SEC = _env_int("GRACE_PERIOD_SEC", 2 * 60)

# ─── Härtung: Pfade & Schalter ────────────────────────────────────────────────
# Kill-Switch: BEWUSST eine eigene Datei und NICHT im STATE_FILE, denn
# clear_state() löscht das STATE_FILE bei IP-Erholung – ein dort abgelegtes
# Deaktivierungs-Flag würde sich dadurch stillschweigend selbst aufheben.
DISABLE_FLAG   = os.environ.get(
    "DISABLE_FLAG", os.path.join(HIER, "glasfaser_watchdog_disabled"))
FORENSIC_DIR   = os.environ.get("FORENSIC_DIR", os.path.join(HIER, "forensik"))
BACKUP_DIR     = os.path.join(FORENSIC_DIR, "wan-config")
KEEP_BACKUPS   = _env_int("KEEP_BACKUPS", 30)

DRY_RUN = False   # wird in main() aus --dry-run gesetzt

# (A) Destruktiv-Blacklist – Substring-Treffer in Text/aria-label/title/value
DESTRUCTIVE_TEXT_PATTERNS = [
    "löschen", "loeschen", "delete", "entfernen", "remove",
    "zurücksetzen", "zuruecksetzen", "reset to factory", "factory reset",
    "factory default", "werkseinstellung", "werkseinstellungen",
    "restore default", "standardwerte", "auf werkszustand",
    "papierkorb", "trash",
]
# Zusätzlich für Bezeichner (id/class/name): Token-Treffer, damit auch
# kompakte TP-Link-Namen wie `T_del`, `edit-delete-icon`, `btn-reset` greifen.
#
# WICHTIG – aus dem DOM-Dump vom 2026-08-04 verifiziert: Der Löschen-Button
# dieser Firmware heißt `edit-trash-icon` und ist TEXTLOS:
#   <span class="a table-grid-icon edit-modify-icon"  data-index="1"></span>
#   <span class="a table-grid-icon edit-trash-icon"   data-connnum="1"></span>
# Er enthält KEINES der üblichen Wörter (löschen/delete/remove/reset). Ohne
# `trash` im Muster wäre er durch die Blacklist gerutscht. Der Klassenname ist
# bei textlosen Icon-Spans das einzige verwertbare Signal.
DESTRUCTIVE_TOKEN_RE = re.compile(
    r"(^|[-_\s])(del|delete|rm|remove|reset|factory|erase|clear|trash|bin)([-_\s]|$)",
    re.IGNORECASE,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.environ.get(
            "LOG_FILE", os.path.join(HIER, "glasfaser_watchdog.log"))),
    ],
)
log = logging.getLogger(__name__)


# ─── Exceptions ──────────────────────────────────────────────────────────────

class WanConnectionMissing(Exception):
    """Die erwartete WAN-Verbindung existiert nicht (mehr) in der Router-Übersicht."""


class VerificationInconclusive(Exception):
    """Die Verifikation konnte den Router gar nicht erst erreichen.

    Unklar ist NICHT zerstört: Ist die Router-Oberfläche nicht erreichbar (Reboot,
    Netzwerkfehler), liegt über die WAN-Config schlicht keine Aussage vor. Ohne
    diese Unterscheidung würde ein Router-Neustart als „Config gelöscht" gewertet
    und der Watchdog per Kill-Switch stillgelegt. Alle Aufrufer behandeln diese
    Ausnahme über ihre generischen Fehlerpfade → Backoff statt Kill-Switch.
    """


class DestructiveActionBlocked(Exception):
    """Ein Klick wurde als potenziell destruktiv erkannt und NICHT ausgeführt."""


class ContextMismatch(Exception):
    """Die erwartete Seite / das erwartete Panel ist nicht aktiv."""


class SelectorAmbiguous(Exception):
    """Ein Selektor traf 0 oder >1 Elemente – Blindklick wird vermieden."""


class WanConfigDestroyed(Exception):
    """Nach dem Toggle fehlt die WAN-/PPPoE-Konfiguration oder ist unvollständig."""


# ─── Telegram ────────────────────────────────────────────────────────────────

def send_telegram(text: str) -> None:
    if DRY_RUN:
        log.info("[DRY-RUN] Telegram würde gesendet: %s", text.replace("\n", " | "))
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT, "text": text}).encode()
        urllib.request.urlopen(url, data=data, timeout=10)
    except Exception as e:
        log.warning("Telegram-Benachrichtigung fehlgeschlagen: %s", e)


# ─── Backoff-Status (persistent) ──────────────────────────────────────────────

def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state: dict) -> None:
    # Atomar schreiben (temp + os.replace), damit ein paralleler load_state() nie
    # eine halb geschriebene Datei liest und fälschlich {} zurückgibt.
    if DRY_RUN:
        log.info("[DRY-RUN] State würde geschrieben: %s", state)
        return
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
    if DRY_RUN:
        log.info("[DRY-RUN] State würde gelöscht.")
        return
    try:
        os.remove(STATE_FILE)
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("Backoff-Status löschen fehlgeschlagen: %s", e)


# ─── (D) Kill-Switch ─────────────────────────────────────────────────────────

def watchdog_disabled() -> str | None:
    """Gibt den Deaktivierungs-Grund zurück, falls der Kill-Switch gesetzt ist."""
    try:
        with open(DISABLE_FLAG) as f:
            return f.read().strip() or "unbekannt"
    except FileNotFoundError:
        return None
    except Exception as e:
        # Im Zweifel als deaktiviert behandeln – Sicherheit vor Verfügbarkeit.
        log.warning("Kill-Switch nicht lesbar (%s) – behandle als DEAKTIVIERT.", e)
        return "flag-datei nicht lesbar"


def disable_watchdog(reason: str) -> None:
    """Setzt den Kill-Switch. Muss manuell wieder entfernt werden."""
    if DRY_RUN:
        log.info("[DRY-RUN] Kill-Switch würde gesetzt: %s", reason)
        return
    try:
        with open(DISABLE_FLAG, "w") as f:
            f.write(f"{datetime.now().isoformat()} – {reason}\n")
        log.critical("KILL-SWITCH GESETZT (%s) – Datei: %s", reason, DISABLE_FLAG)
    except Exception as e:
        log.error("Kill-Switch konnte NICHT gesetzt werden: %s", e)


# ─── (E) Forensik ────────────────────────────────────────────────────────────
# Alle Forensik-Schreibvorgänge sind bewusst exception-sicher: sie laufen im
# Fehlerpfad und dürfen die ursprüngliche Exception niemals überdecken.

def _forensic_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _ensure_dir(path: str) -> bool:
    try:
        os.makedirs(path, exist_ok=True)
        return True
    except Exception as e:
        log.warning("Verzeichnis %s nicht anlegbar: %s", path, e)
        return False


def save_forensics(page, context, tag: str) -> None:
    """Screenshot + Playwright-Trace mit Zeitstempel sichern. Wirft nie."""
    stamp = _forensic_stamp()
    if not _ensure_dir(FORENSIC_DIR):
        return

    shot = os.path.join(FORENSIC_DIR, f"{stamp}_{tag}.png")
    try:
        page.screenshot(path=shot, full_page=True)
        log.info("Forensik-Screenshot: %s", shot)
    except Exception as e:
        log.warning("Screenshot fehlgeschlagen: %s", e)

    html = os.path.join(FORENSIC_DIR, f"{stamp}_{tag}.html")
    try:
        with open(html, "w") as f:
            f.write(page.content())
        log.info("Forensik-DOM: %s", html)
    except Exception as e:
        log.warning("DOM-Dump fehlgeschlagen: %s", e)

    trace = os.path.join(FORENSIC_DIR, f"{stamp}_{tag}.trace.zip")
    try:
        # Tracing muss bei der Context-Erstellung gestartet worden sein –
        # nachträglich lässt sich kein Trace erzeugen.
        context.tracing.stop(path=trace)
        log.info("Forensik-Trace: %s", trace)
    except Exception as e:
        log.warning("Trace-Export fehlgeschlagen: %s", e)


# ─── (F) Config-Backup ───────────────────────────────────────────────────────

def _fingerprint(value: str) -> str:
    """Kurzer, nur lokal vergleichbarer Fingerabdruck eines redigierten Werts.

    Zweck: Beim Vergleich von pre-toggle- und post-toggle-Backup soll erkennbar
    bleiben, ob der Router einen Wert VERÄNDERT hat – am 2026-08-01 stand an
    Stelle des 1&1-Eintrags plötzlich ein „Starlink"-Eintrag. Ohne Fingerabdruck
    sähen zwei verschiedene Werte gleicher Länge identisch aus.

    Gesalzen mit der machine-id, damit der Abdruck außerhalb dieses Geräts
    nichts preisgibt: Das Format des PPPoE-Benutzernamens ist bekannt, ein
    ungesalzener Kurz-Hash ließe sich sonst gegen eine überschaubare
    Kandidatenliste durchprobieren.
    """
    try:
        with open("/etc/machine-id", "r", encoding="utf-8") as fh:
            salz = fh.read().strip()
    except Exception:
        salz = "kein-salz"
    return hashlib.sha256((salz + value).encode("utf-8")).hexdigest()[:8]


# Identitätsfelder: Der PPPoE-Benutzername ist kein Passwort, aber die zweite
# Hälfte der Zugangsdaten und identifiziert den Anschluss eindeutig. Er hat in
# Backups und Forensik-Dumps nichts verloren – zumal diese Dateien der einzige
# Grund sind, warum man sie jemandem schickt (Support, Bugreport).
#
# Wiederherstellbarkeit geht dadurch nicht verloren: Das Passwort ist ohnehin
# redigiert, das Backup taugte also nie zum Wiederanlegen der Verbindung. Dafür
# sind die 1&1-Zugangsdaten aus dem Control-Center nötig.
#
# WICHTIG: Der Rückgabewert darf NIE leer sein. verify_wan_intact() prüft mit
# `if f.get("value")`, ob ein Identitätsfeld befüllt ist – ein leerer Platzhalter
# würde dort als „Konfiguration zerstört" gelesen und den Kill-Switch auslösen.
IDENTITY_FIELD_PATTERN = r"(user|account|login|kennung)"
# Auffangnetz für unbekannte Feldnamen anderer Firmware: Ein Wert in Kontoform
# (etwas@etwas.tld) in einem Textfeld eines WAN-Formulars ist praktisch immer
# eine Zugangskennung.
IDENTITY_VALUE_PATTERN = r"^\S+@\S+\.\S+$"


def _redact(field_name: str, field_type: str, value: str) -> str:
    """PPPoE-Passwort, Benutzername & Co. niemals in Backups oder Dumps schreiben."""
    if not value:
        return value
    if field_type == "password" or re.search(r"(pass|pwd|secret|key|token)", field_name, re.I):
        return f"<redacted:{len(value)}chars>"
    if re.search(IDENTITY_FIELD_PATTERN, field_name, re.I) or (
        field_type == "text" and re.match(IDENTITY_VALUE_PATTERN, value)
    ):
        return f"<redacted:{len(value)}chars:{_fingerprint(value)}>"
    return value


def capture_wan_config(page) -> dict:
    """Liest alle Formularfelder des geöffneten WAN-Edit-Formulars aus.

    Rein lesend – klickt und speichert nichts. Passwortfelder werden redigiert.
    """
    try:
        raw = page.evaluate("""() => {
            const out = [];
            document.querySelectorAll('input, select').forEach(el => {
                const id = el.id || '', name = el.name || '';
                if (!id && !name) return;
                const t = (el.type || el.tagName).toLowerCase();
                out.push({
                    id: id,
                    name: name,
                    type: t,
                    visible: !!(el.offsetParent !== null || t === 'hidden'),
                    value: (t === 'checkbox' || t === 'radio')
                             ? String(el.checked) : String(el.value ?? ''),
                });
            });
            return out;
        }""")
    except Exception as e:
        log.warning("WAN-Config konnte nicht ausgelesen werden: %s", e)
        return {}

    fields = []
    for f in raw:
        f["value"] = _redact(f["id"] or f["name"], f["type"], f["value"])
        fields.append(f)
    return {"captured_at": datetime.now().isoformat(), "url": page.url, "fields": fields}


def backup_wan_config(cfg: dict, tag: str = "pre-toggle") -> None:
    """Sichert die WAN-Config als JSON; behält die letzten KEEP_BACKUPS Stück."""
    if not cfg:
        log.warning("Kein Config-Backup möglich (leere Config).")
        return
    if DRY_RUN:
        log.info("[DRY-RUN] Config-Backup würde geschrieben (%d Felder, tag=%s)",
                 len(cfg.get("fields", [])), tag)
        return
    if not _ensure_dir(BACKUP_DIR):
        return
    path = os.path.join(BACKUP_DIR, f"{_forensic_stamp()}_{tag}.json")
    try:
        with open(path, "w") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        log.info("WAN-Config gesichert: %s (%d Felder)", path, len(cfg.get("fields", [])))
    except Exception as e:
        log.warning("Config-Backup fehlgeschlagen: %s", e)
        return

    try:
        files = sorted(glob.glob(os.path.join(BACKUP_DIR, "*.json")))
        for old in files[:-KEEP_BACKUPS]:
            os.remove(old)
            log.info("Altes Config-Backup entfernt: %s", os.path.basename(old))
    except Exception as e:
        log.warning("Backup-Rotation fehlgeschlagen: %s", e)


# ─── (A/B) Sichere Interaktion ───────────────────────────────────────────────

def _describe_element(locator) -> dict:
    """Liest NUR die Eigenschaften des Ziel-Elements selbst aus.

    Bewusst nicht die des Eltern-<tr>: dort steht typischerweise auch das
    Lösch-Icon der Zeile, was jeden legitimen Edit-Klick blockieren würde.
    """
    try:
        return locator.evaluate("""el => ({
            text: (el.innerText || el.textContent || '').trim().slice(0, 200),
            aria: el.getAttribute('aria-label') || '',
            title: el.getAttribute('title') || '',
            value: el.getAttribute('value') || '',
            cls: el.getAttribute('class') || '',
            id: el.id || '',
            name: el.getAttribute('name') || '',
            data: Array.from(el.attributes)
                       .filter(a => a.name.startsWith('data-'))
                       .map(a => a.name + '=' + a.value).join(' '),
        })""")
    except Exception as e:
        log.warning("Element-Beschreibung nicht lesbar: %s", e)
        return {}


def assert_not_destructive(desc: dict, what: str) -> None:
    """(A) Harte Sperre: blockiert Klicks auf Lösch-/Reset-Elemente."""
    text_fields = {k: desc.get(k, "") for k in ("text", "aria", "title", "value")}
    ident_fields = {k: desc.get(k, "") for k in ("cls", "id", "name", "data")}

    for key, val in text_fields.items():
        low = (val or "").lower()
        for pat in DESTRUCTIVE_TEXT_PATTERNS:
            if pat in low:
                raise DestructiveActionBlocked(
                    f"Klick auf '{what}' BLOCKIERT – {key}='{val}' enthält '{pat}'"
                )

    for key, val in ident_fields.items():
        low = (val or "").lower()
        for pat in DESTRUCTIVE_TEXT_PATTERNS:
            if pat in low:
                raise DestructiveActionBlocked(
                    f"Klick auf '{what}' BLOCKIERT – {key}='{val}' enthält '{pat}'"
                )
        if DESTRUCTIVE_TOKEN_RE.search(val or ""):
            raise DestructiveActionBlocked(
                f"Klick auf '{what}' BLOCKIERT – {key}='{val}' enthält ein Lösch-/Reset-Token"
            )


def resolve_unique(page_or_locator, selector: str, what: str, timeout: int = 10_000):
    """(B) Liefert den Locator nur, wenn GENAU EIN Element matcht.

    Wichtig: `count()` ist eine Momentaufnahme OHNE Auto-Wait. Ohne das
    vorgeschaltete wait_for() wäre dieser Wrapper anfälliger als das alte
    direkte `.click()` (das automatisch wartet) – ein langsam rendernder
    SPA-Aufbau würde 0 Treffer liefern und den Kill-Switch auslösen.
    """
    loc = page_or_locator.locator(selector)
    try:
        loc.first.wait_for(state="attached", timeout=timeout)
    except PWTimeout:
        pass   # 0 Treffer wird unten sauber als SelectorAmbiguous gemeldet
    except Exception:
        pass
    try:
        count = loc.count()
    except Exception as e:
        raise SelectorAmbiguous(f"Selektor '{selector}' ({what}) nicht auswertbar: {e}")

    log.info("Selektor-Check [%s]: '%s' → %d Treffer", what, selector, count)
    if count == 0:
        raise SelectorAmbiguous(f"Selektor '{selector}' ({what}) traf KEIN Element – Abbruch")
    if count > 1:
        raise SelectorAmbiguous(
            f"Selektor '{selector}' ({what}) traf {count} Elemente – "
            f"Blindklick vermieden, Abbruch"
        )
    return loc.first


def safe_click(page_or_locator, selector: str, what: str,
               mutating: bool = True, timeout: int = 15_000):
    """Zentraler Klick-Wrapper: eindeutig (B) + nicht destruktiv (A) + Dry-Run (G).

    mutating=False kennzeichnet rein lesende Klicks (z. B. Edit-Formular öffnen –
    beim Archer wird nichts persistiert, bis T_ok gedrückt wird). Diese werden
    AUCH im Dry-Run ausgeführt; sonst könnte der Dry-Run die Formular-Selektoren
    (#vidEn, #vid, T_ok) nie prüfen – also genau die IDs, die TP-Link-Updates
    erfahrungsgemäß umbenennen.
    """
    target = resolve_unique(page_or_locator, selector, what, timeout=timeout)
    target.wait_for(state="visible", timeout=timeout)

    desc = _describe_element(target)
    log.info("Klick-Ziel [%s]: text='%s' cls='%s' id='%s' title='%s'",
             what, desc.get("text", ""), desc.get("cls", ""),
             desc.get("id", ""), desc.get("title", ""))

    assert_not_destructive(desc, what)

    if DRY_RUN and mutating:
        log.info("[DRY-RUN] Klick auf '%s' würde ausgeführt – übersprungen (verändernd).", what)
        return target

    if DRY_RUN:
        log.info("[DRY-RUN] Klick auf '%s' wird ausgeführt (rein lesend).", what)

    target.click(timeout=timeout)
    return target


def safe_fill(page, selector: str, value: str, what: str, timeout: int = 10_000):
    """Formularwert setzen – ebenfalls eindeutigkeitsgeprüft und dry-run-fähig."""
    target = resolve_unique(page, selector, what, timeout=timeout)
    if DRY_RUN:
        # Im Dry-Run wurde die Checkbox nicht getoggelt, das Feld kann also
        # noch ausgeblendet sein – dann nur den Ist-Zustand protokollieren.
        try:
            target.wait_for(state="visible", timeout=2_000)
            current = target.input_value()
        except Exception:
            current = "<nicht sichtbar – Checkbox wurde im Dry-Run nicht getoggelt>"
        log.info("[DRY-RUN] '%s' würde von '%s' auf '%s' gesetzt – übersprungen.",
                 what, current, value)
        return
    target.wait_for(state="visible", timeout=timeout)
    target.fill(value)


# ─── (C) Kontext-Guard ───────────────────────────────────────────────────────

def assert_context(page, name: str, markers: list[str], timeout: int = 10_000) -> None:
    """Prüft vor jeder Aktion, dass die erwartete Seite/das Panel aktiv ist."""
    # state="visible", nicht "attached": Die Router-Oberfläche ist eine SPA, in
    # der alle Panels dauerhaft im DOM hängen – "attached" wäre praktisch immer
    # wahr und damit gar kein Guard.
    per_marker = max(int(timeout / max(len(markers), 1)), 1_500)
    for sel in markers:
        try:
            page.locator(sel).first.wait_for(state="visible", timeout=per_marker)
            log.info("Kontext-Guard OK [%s]: Marker '%s' vorhanden (URL: %s)",
                     name, sel, page.url)
            return
        except PWTimeout:
            continue
    raise ContextMismatch(
        f"Erwarteter Kontext '{name}' NICHT aktiv (URL: {page.url}). "
        f"Keiner dieser Marker gefunden: {markers} – Abbruch statt Blindklick."
    )


# ─── Netzwerk-Prüfung ────────────────────────────────────────────────────────

def get_external_ip() -> str | None:
    """Fragt die externe WAN-IP ab, mit Rückfall auf weitere Quellen.

    Warum mehrere Quellen: Eine nicht ermittelbare IP gilt hier als Ausfall
    (is_glasfaser_active → False). Mit nur einer Quelle war jede Störung dieses
    einen Dienstes ein Ausfall-Befund – im Log stehen 10 solcher Fälle bei
    14.837 Abfragen, vier davon haben vor Einführung der Karenzzeit direkt einen
    Toggle ausgelöst (30.06., 03.07., 12.07., 22.07.).

    Die Karenzzeit fängt das inzwischen ab, weil zwei Läufe in Folge nötig sind.
    Seit der Takt aber von 15 auf 5 Minuten steht, genügt dafür eine 5 Minuten
    lange Störung statt einer 15 Minuten langen. Der Rückfall schließt die Lücke
    wieder: Erst wenn ALLE Quellen schweigen, gilt die IP als nicht ermittelbar.
    """
    letzter_fehler = None
    for i, url in enumerate(IP_QUELLEN):
        try:
            r = subprocess.run(
                ["curl", "-4", "-s", "--max-time", str(IP_TIMEOUT_SEC), url],
                capture_output=True, text=True, timeout=IP_TIMEOUT_SEC + 5
            )
            ip = r.stdout.strip()
            if re.match(r"^\d+\.\d+\.\d+\.\d+$", ip):
                # Nur melden, wenn tatsächlich ausgewichen wurde – sonst würde
                # jeder der ~288 Läufe pro Tag eine Zeile erzeugen.
                if i > 0:
                    log.warning("IP-Quelle(n) vor %s ausgefallen – Wert von dort: %s",
                                url, ip)
                return ip
            letzter_fehler = f"unbrauchbare Antwort von {url}: {ip[:60]!r}"
        except Exception as e:
            letzter_fehler = f"{url}: {e}"
    log.warning("Keine der %d IP-Quellen hat geantwortet (zuletzt: %s).",
                len(IP_QUELLEN), letzter_fehler)
    return None


def wait_for_glasfaser(max_wait: int = CHECK_WAIT_SEC, poll: int = CHECK_POLL_SEC) -> bool:
    """Wartet bis zu max_wait Sekunden auf den PPPoE-Aufbau, prüft alle poll Sekunden.

    Pollen statt eines einzelnen blinden sleep(): Ein schneller Aufbau wird
    sofort erkannt, ein langsamer bekommt trotzdem die volle Zeit. Vorher wurde
    exakt einmal bei 120s geprüft – kam PPPoE bei 130s hoch, galt der Versuch als
    gescheitert und löste unnötig Alarm und Backoff aus.
    """
    deadline = time.time() + max_wait
    attempt = 0
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            log.info("Verbindungsaufbau nach %ds nicht bestätigt.", max_wait)
            return False
        time.sleep(min(poll, remaining))
        attempt += 1
        elapsed = int(max_wait - max(deadline - time.time(), 0))
        log.info("Verbindungsprüfung %d (nach %ds)…", attempt, elapsed)
        if is_glasfaser_active():
            log.info("Verbindung steht nach ca. %ds.", elapsed)
            return True


def is_glasfaser_active() -> bool:
    """Prüft ob die externe WAN-IP im 94.134.x.x-Bereich liegt (1&1 Glasfaser)."""
    ip = get_external_ip()
    if ip:
        log.info("Externe WAN-IP: %s", ip)
        return ip.startswith(GW_PREFIX)
    log.warning("Externe IP nicht ermittelbar – nehme Glasfaser als inaktiv an.")
    return False


# ─── Router-Automation ───────────────────────────────────────────────────────

# Kontext-Marker (aus dem DOM-Dump 2026-08-04 verifiziert).
# #addConnIcon ist der stabilste Marker der Internet-Seite: er existiert
# unabhängig davon, ob die WAN-Liste Einträge enthält. Ein generisches "table"
# wäre kein Guard – das matcht fast jede Router-Seite.
# #vidEn taugt NICHT als Edit-Formular-Marker: TP-Link blendet die echten
# <input>-Elemente per CSS aus, sichtbar ist nur das zugehörige <label>.
LOGIN_MARKERS    = ["input[type='password']"]
INTERNET_MARKERS = ["#addConnIcon", "span.edit-modify-icon"]
EDITFORM_MARKERS = ["label[for='vidEn']", "button.T_ok"]

# Zeilen-Selektor: auf Zeilen mit Edit-Icon eingegrenzt, damit Kopf-/Layout-
# Zeilen und verschachtelte <tr> nicht mitzählen.
WAN_ROW_SELECTOR = f"tr:has(span.edit-modify-icon):has-text('{WAN_NAME}')"


def login(page) -> None:
    log.info("Öffne Router-Login: %s", ROUTER_URL)
    page.goto(ROUTER_URL, wait_until="domcontentloaded", timeout=30_000)
    assert_context(page, "Login-Seite", LOGIN_MARKERS)

    if DRY_RUN:
        # Ohne Login kommen wir an keine Folgeseite – Login IST im Dry-Run nötig,
        # er ist lesend und verändert keine Konfiguration.
        log.info("[DRY-RUN] Login wird ausgeführt (lesend, ändert keine Konfiguration).")
    page.fill("input[type='password']", ROUTER_PASSWORD)
    page.locator("button:has-text('Anmelden')").first.click()

    # Falls Session-Konflikt-Dialog erscheint: hat Klasse btn-msg
    try:
        page.wait_for_selector("button.btn-msg:has-text('Anmelden')", timeout=5_000)
        log.info("Session-Konflikt-Dialog – klicke Anmelden (btn-msg)")
        page.locator("button.btn-msg:has-text('Anmelden')").click()
    except PWTimeout:
        pass  # Kein Dialog → direkter Login

    page.wait_for_load_state("networkidle", timeout=25_000)
    page.wait_for_timeout(1000)   # SPA braucht kurz zum Rendern
    log.info("Login erfolgreich.")


def navigate_to_internet(page) -> None:
    """Navigiert zu Erweiterte Einstellungen → Netzwerk → Internet."""
    log.info("Navigiere zu Erweiterte Einstellungen → Netzwerk → Internet")

    # Navigation ist nicht destruktiv; die Menü-Selektoren sind bewusst
    # unverändert (mehrdeutig per .first), da ein Menüklick nichts speichert.
    page.locator(
        "a:has-text('Erweiterte Einstellungen'), "
        "span:has-text('Erweiterte Einstellungen')"
    ).first.click()
    page.wait_for_load_state("networkidle", timeout=15_000)

    page.locator("a:has-text('Netzwerk'), span:has-text('Netzwerk')").first.click()
    page.wait_for_load_state("networkidle", timeout=15_000)

    page.locator("a:has-text('Internet')").filter(has_not_text="Provider").first.click()
    page.wait_for_load_state("networkidle", timeout=15_000)

    assert_context(page, "Internet-Seite", INTERNET_MARKERS)
    log.info("Internet-Seite geöffnet.")


def wan_row_count(page) -> int:
    """Zählt die WAN-Zeilen mit dem erwarteten Namen (rein lesend).

    count() ist eine Momentaufnahme ohne Auto-Wait. Da der Router die Tabelle
    bei Statuswechseln neu zeichnet und das tbody dabei kurz leer ist, wird
    vorher auf das Erscheinen einer passenden Zeile gewartet. Der Locator wird
    dabei bei jedem Poll neu gegen das aktuelle DOM ausgewertet, greift also
    nicht auf ein veraltetes Element-Handle zurück.

    Warten allein genügt nicht: Zwischen wait_for() und count() liegt eine
    weitere Runde zur Seite, in die der Repaint fallen kann. Erst das mehrfache
    Abtasten (WAN_ROW_PROBES) schließt die Lücke. Rückgabe 0 heißt also: in
    allen Messungen keine Zeile – dann ist sie wirklich weg. -1 heißt: nicht
    zählbar (Seite tot), das ist etwas anderes als „nicht vorhanden".
    """
    loc = page.locator(WAN_ROW_SELECTOR)
    for probe in range(1, WAN_ROW_PROBES + 1):
        # Erster Versuch wartet lang (die Seite kann noch laden), die
        # Wiederholungen kurz – sonst würde der Fall „wirklich gelöscht"
        # unnötig lange blockieren, obwohl er schon feststeht.
        timeout = WAN_ROW_WAIT_MS if probe == 1 else WAN_ROW_RETRY_MS
        try:
            loc.first.wait_for(state="attached", timeout=timeout)
        except PWTimeout:
            pass          # Wirklich keine Zeile da – das stellt count() gleich fest.
        except Exception:
            pass
        try:
            n = loc.count()
        except Exception as e:
            log.warning("WAN-Zeilen nicht zählbar: %s", e)
            return -1
        if n > 0:
            if probe > 1:
                log.info("WAN-Zeile erst in Messung %d sichtbar (Repaint).", probe)
            return n
    return 0


def click_edit(page) -> None:
    """Öffnet das Bearbeiten-Formular der 1&1-Zeile (streng eindeutig)."""
    log.info("Suche WAN-Verbindung '%s' und öffne Edit-Formular", WAN_NAME)
    assert_context(page, "Internet-Seite", INTERNET_MARKERS)

    count = wan_row_count(page)
    log.info("WAN-Zeilen mit '%s': %d", WAN_NAME, count)
    if count == 0:
        raise WanConnectionMissing(
            f"WAN-Verbindung '{WAN_NAME}' nicht in der Router-Übersicht gefunden"
        )
    if count > 1:
        raise SelectorAmbiguous(
            f"{count} WAN-Zeilen matchen '{WAN_NAME}' – uneindeutig, Abbruch "
            f"(Selektor: {WAN_ROW_SELECTOR})"
        )

    row = page.locator(WAN_ROW_SELECTOR).first
    try:
        row.wait_for(timeout=10_000)
    except PWTimeout:
        raise WanConnectionMissing(
            f"WAN-Verbindung '{WAN_NAME}' nicht sichtbar in der Router-Übersicht"
        )

    # mutating=False: Das Öffnen des Formulars persistiert nichts (erst T_ok
    # speichert). Wird deshalb auch im Dry-Run ausgeführt, damit die
    # Formular-Selektoren überhaupt prüfbar sind.
    safe_click(row, "span.edit-modify-icon", "Edit-Icon der 1&1-Zeile", mutating=False)
    page.wait_for_load_state("networkidle", timeout=15_000)

    assert_context(page, "WAN-Edit-Formular", EDITFORM_MARKERS)
    log.info("Edit-Formular geöffnet.")


def click_save(page) -> None:
    """Klickt den OK-Button (T_ok) des WAN-Edit-Formulars.

    T_ok ist der EINZIGE tatsächliche Save-Button für die WAN-Verbindungs-
    konfiguration. Er speichert UND testet die Verbindung.
    """
    assert_context(page, "WAN-Edit-Formular", EDITFORM_MARKERS)

    # Bevorzugt die eindeutige ID (aus dem DOM verifiziert):
    #   <button type="submit" class="green T_ok pure-button" id="saveConnBtn">OK</button>
    # Der Klassen-Selektor bleibt als Fallback, falls die ID umbenannt wird.
    try:
        target = resolve_unique(page, "#saveConnBtn", "OK/Speichern (#saveConnBtn)")
        target.wait_for(state="visible", timeout=10_000)
        desc = _describe_element(target)
        log.info("Klick-Ziel [OK/Speichern]: id='saveConnBtn' text='%s' cls='%s'",
                 desc.get("text", ""), desc.get("cls", ""))
        assert_not_destructive(desc, "OK/Speichern (#saveConnBtn)")
        if DRY_RUN:
            log.info("[DRY-RUN] OK (#saveConnBtn) würde geklickt – "
                     "übersprungen, nichts gespeichert.")
            return
        target.click(timeout=15_000)
        page.wait_for_timeout(2_000)
        log.info("OK (#saveConnBtn) geklickt – Router baut Verbindung auf.")
        return
    except (SelectorAmbiguous, PWTimeout) as e:
        log.warning("#saveConnBtn nicht eindeutig/sichtbar (%s) – "
                    "weiche auf button.T_ok aus.", e)

    # Fallback: unter allen T_ok-Buttons darf genau EINER sichtbar sein.
    all_ok = page.locator("button.T_ok")
    total = all_ok.count()
    visible_idx = [i for i in range(total) if all_ok.nth(i).is_visible()]
    log.info("Save-Button-Check: %d x button.T_ok im DOM, davon %d sichtbar",
             total, len(visible_idx))

    if len(visible_idx) == 0:
        raise SelectorAmbiguous("Kein sichtbarer OK-Button (T_ok) gefunden – Abbruch")
    if len(visible_idx) > 1:
        raise SelectorAmbiguous(
            f"{len(visible_idx)} sichtbare T_ok-Buttons – uneindeutig, Abbruch"
        )

    target = all_ok.nth(visible_idx[0])
    desc = _describe_element(target)
    log.info("Klick-Ziel [OK/Speichern]: text='%s' cls='%s' id='%s'",
             desc.get("text", ""), desc.get("cls", ""), desc.get("id", ""))
    assert_not_destructive(desc, "OK/Speichern (T_ok)")

    if DRY_RUN:
        log.info("[DRY-RUN] OK (T_ok) würde geklickt – übersprungen, nichts gespeichert.")
        return

    target.click(timeout=15_000)
    page.wait_for_timeout(2_000)
    log.info("OK (T_ok) geklickt – Router baut Verbindung auf.")


def set_vlan(page, enable: bool) -> None:
    """Aktiviert oder deaktiviert die VLAN-ID Checkbox (id='vidEn') und trägt ID ein."""
    assert_context(page, "WAN-Edit-Formular", EDITFORM_MARKERS)

    vlan_cb = page.locator("#vidEn")
    vlan_cb.wait_for(state="attached", timeout=10_000)
    is_checked = vlan_cb.is_checked()
    log.info("VLAN-Checkbox aktuell: %s", is_checked)

    if enable and not is_checked:
        log.info("VLAN-Checkbox aktivieren")
        safe_click(page, "label[for='vidEn']", "VLAN-Checkbox-Label")
        if not DRY_RUN:
            page.locator("#vid").wait_for(state="visible", timeout=5_000)
    elif not enable and is_checked:
        log.info("VLAN-Checkbox deaktivieren")
        safe_click(page, "label[for='vidEn']", "VLAN-Checkbox-Label")
        page.wait_for_timeout(300)
    elif enable and is_checked:
        log.info("VLAN-Checkbox bereits aktiviert")
        page.locator("#vid").wait_for(state="visible", timeout=5_000)
    else:
        log.info("VLAN-Checkbox bereits deaktiviert")

    if enable:
        safe_fill(page, "#vid", VLAN_ID, "VLAN-ID-Feld")
        if not DRY_RUN:
            actual = page.evaluate("() => document.getElementById('vid')?.value")
            log.info("VLAN-ID gesetzt: %s (erwartet: %s)", actual, VLAN_ID)

    click_save(page)


# ─── (D) Post-Toggle-Verifikation ────────────────────────────────────────────

# Felder, die eine vollständige PPPoE-Config mindestens enthalten muss.
REQUIRED_CONFIG_HINTS = ("user", "acc", "name")


def verify_wan_intact(page, phase: str, deep: bool = True) -> tuple[bool, dict]:
    """Bestätigende Verifikation – erst ein durchgehend negatives Ergebnis zählt.

    Wiederholt wird die KOMPLETTE Prüfung (Navigation, Zählung, Edit-Formular,
    Feldprüfung), nicht nur das Zählen: Der Fehlalarm vom 2026-08-07 entstand
    unterhalb der Zählung, in click_edit(). Eine Absicherung nur um
    wan_row_count() würde den Fehlalarm verschieben statt ihn zu beseitigen.
    """
    last_details: dict = {"phase": phase}
    all_navigation_failures = True
    reason_prev = "unbekannt"   # Grund des letzten Fehlschlags, für die Meldung

    for attempt in range(1, VERIFY_ATTEMPTS + 1):
        ok, details = _verify_wan_intact_once(
            page, f"{phase} – Versuch {attempt}/{VERIFY_ATTEMPTS}", deep=deep
        )
        if ok:
            if attempt > 1:
                log.warning(
                    "Verifikation erst im Versuch %d erfolgreich – der vorherige "
                    "Fehlschlag war ein Rendering-Effekt, KEINE gelöschte Config.",
                    attempt,
                )
                if NOTIFY_FIX_HIT:
                    # Das ist der Beleg, dass der Fix im Feld greift: Vor dem
                    # 2026-08-07 hätte genau dieser Fehlschlag den Kill-Switch
                    # gesetzt und den Watchdog stillgelegt.
                    send_telegram(
                        "🛡️ Repaint-Race abgefangen\n\n"
                        f"Die Verifikation ({phase}) schlug im Versuch "
                        f"{attempt - 1} fehl und war im Versuch {attempt} in "
                        f"Ordnung – die WAN-Config war durchgehend da.\n"
                        f"Grund des Fehlschlags: {reason_prev}\n\n"
                        "Vor dem Fix vom 07.08. wäre hier ein Fehlalarm "
                        "gelaufen und der Watchdog hätte sich abgeschaltet.\n"
                        "Es wurde nichts unternommen, der Lauf geht normal weiter."
                    )
            details["attempts"] = attempt
            return True, details

        last_details = details
        reason = details.get("error") or f"wan_rows={details.get('wan_rows')}"
        reason_prev = reason
        if not str(details.get("error", "")).startswith("navigation:"):
            all_navigation_failures = False
        if attempt < VERIFY_ATTEMPTS:
            log.warning(
                "Verifikation Versuch %d/%d fehlgeschlagen (%s) – noch kein Urteil, "
                "wiederhole in %ds.",
                attempt, VERIFY_ATTEMPTS, reason, VERIFY_RETRY_SEC,
            )
            try:
                page.wait_for_timeout(VERIFY_RETRY_SEC * 1000)
            except Exception:
                time.sleep(VERIFY_RETRY_SEC)

    last_details["attempts"] = VERIFY_ATTEMPTS

    if all_navigation_failures:
        raise VerificationInconclusive(
            f"Router-Oberfläche in {VERIFY_ATTEMPTS} Versuchen nicht erreichbar "
            f"({last_details.get('error')}) – über die WAN-Config ist damit nichts ausgesagt."
        )

    log.critical(
        "Verifikation in allen %d Versuchen fehlgeschlagen – WAN-Config gilt als zerstört.",
        VERIFY_ATTEMPTS,
    )
    return False, last_details


def _verify_wan_intact_once(page, phase: str, deep: bool = True) -> tuple[bool, dict]:
    """Ein einzelner Prüfdurchgang. Ein Fehlschlag hier ist noch kein Urteil –
    das fällt verify_wan_intact() nach mehreren Versuchen.

    phase  – Beschriftung für das Log
    deep   – zusätzlich das Edit-Formular öffnen und Pflichtfelder prüfen
    """
    log.info("=== Post-Toggle-Verifikation (%s) ===", phase)
    details: dict = {"phase": phase}

    try:
        navigate_to_internet(page)
    except Exception as e:
        log.error("Verifikation: Internet-Seite nicht erreichbar: %s", e)
        details["error"] = f"navigation: {e}"
        return False, details

    count = wan_row_count(page)
    details["wan_rows"] = count
    log.info("Verifikation: %d WAN-Zeile(n) mit '%s'", count, WAN_NAME)

    if count <= 0:
        log.warning("Verifikation: WAN-Zeile '%s' nicht gefunden (%d).", WAN_NAME, count)
        return False, details

    if not deep:
        return True, details

    try:
        click_edit(page)
    except Exception as e:
        log.warning("Verifikation: Edit-Formular nicht öffenbar: %s", e)
        details["error"] = f"edit-form: {e}"
        return False, details

    cfg = capture_wan_config(page)
    details["config"] = cfg

    # Vollständigkeit: mindestens ein befülltes Benutzer-/Kontofeld muss da sein.
    filled = [
        f for f in cfg.get("fields", [])
        if f.get("value") and f.get("type") in ("text", "password")
        and any(h in (f.get("id", "") + f.get("name", "")).lower()
                for h in REQUIRED_CONFIG_HINTS)
    ]
    details["filled_identity_fields"] = [f.get("id") or f.get("name") for f in filled]
    log.info("Verifikation: befüllte Identitätsfelder: %s",
             details["filled_identity_fields"] or "KEINE")

    if not filled:
        log.warning("Verifikation: PPPoE-Konfiguration wirkt leer.")
        return False, details

    log.info("Verifikation OK – WAN-Config vorhanden und befüllt.")
    return True, details


def handle_config_destroyed(details: dict) -> None:
    """(D) Klartext-Alarm + Kill-Switch, damit kein weiterer Schaden entsteht."""
    log.critical("WAN-CONFIG FEHLT NACH TOGGLE – Watchdog wird deaktiviert!")
    disable_watchdog("WAN-Config nach VLAN-Toggle fehlend/unvollständig")
    # Grund im Klartext mitschicken: Die Fehlmeldung vom 2026-08-07 nannte
    # „WAN-Zeilen: 1" unter der Überschrift „Config fehlt" und widersprach sich
    # damit selbst. Der eigentliche Fehlschlag lag eine Ebene tiefer.
    reason = details.get("error") or f"WAN-Zeilen: {details.get('wan_rows', '?')}"
    send_telegram(
        "🚨 WAN-Config fehlt nach Toggle!\n\n"
        f"Nach dem VLAN-Toggle ist die 1&1-WAN-/PPPoE-Konfiguration im Router "
        f"nicht mehr vollständig vorhanden.\n"
        f"Grund: {reason}\n"
        f"Bestätigt in {details.get('attempts', '?')} aufeinanderfolgenden Prüfungen.\n\n"
        "Der Watchdog hat sich SELBST DEAKTIVIERT, um weiteren Schaden zu verhindern.\n"
        f"➡️ Router prüfen: {ROUTER_URL}\n"
        f"➡️ Config-Backups: {BACKUP_DIR}\n"
        f"➡️ Forensik (Screenshot/Trace): {FORENSIC_DIR}\n\n"
        f"Wieder scharf schalten mit:  rm {DISABLE_FLAG}"
    )


def reset_vlan(page) -> None:
    """Führt den VLAN-Reset-Zyklus durch: deaktivieren → warten → aktivieren mit ID 7."""
    navigate_to_internet(page)

    log.info("=== Schritt 1: VLAN-ID deaktivieren ===")
    click_edit(page)

    # (F) Config-Backup VOR jeder Änderung
    backup_wan_config(capture_wan_config(page), tag="pre-toggle")

    set_vlan(page, enable=False)

    if DRY_RUN:
        log.info("[DRY-RUN] 30s-Wartezeit übersprungen (kein Toggle erfolgt).")
    else:
        log.info("Warte 30s damit ISP die VLAN-Session zurücksetzen kann…")
        page.wait_for_timeout(30_000)

    navigate_to_internet(page)

    log.info("=== Schritt 2: VLAN-ID 7 aktivieren ===")
    click_edit(page)
    set_vlan(page, enable=True)

    log.info("VLAN-Reset abgeschlossen.")

    if DRY_RUN:
        log.info("[DRY-RUN] Post-Toggle-Verifikation übersprungen (kein Toggle erfolgt).")
        return

    # (D) Sofort-Prüfung: fängt eine unmittelbare Zerstörung ab.
    ok, details = verify_wan_intact(page, "sofort nach Toggle", deep=True)
    if not ok:
        handle_config_destroyed(details)
        raise WanConfigDestroyed("WAN-Config direkt nach dem Toggle nicht mehr intakt")
    backup_wan_config(details.get("config", {}), tag="post-toggle")


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


def _new_browser(pw):
    """Browser + Context mit aktivem Tracing (E) – Trace muss vorab starten."""
    browser = pw.chromium.launch(headless=True)
    context = browser.new_context(
        ignore_https_errors=True,
        viewport={"width": 1280, "height": 900},
    )
    try:
        context.tracing.start(screenshots=True, snapshots=True, sources=True)
    except Exception as e:
        log.warning("Tracing nicht startbar: %s", e)
    return browser, context


def _late_verification(pw) -> None:
    """(D) Späte Prüfung nach fehlgeschlagenem Reconnect.

    Genau in diesem Zustand traten beide WAN-Verluste auf: Der Toggle lief durch,
    der PPPoE-Neuaufbau scheiterte, und der Router verwarf den Eintrag erst
    danach. Eine Prüfung direkt nach dem Toggle allein würde das verpassen.
    """
    log.info("Reconnect fehlgeschlagen – prüfe erneut, ob die WAN-Config noch existiert.")
    browser, context = _new_browser(pw)
    page = context.new_page()
    try:
        login(page)
        ok, details = verify_wan_intact(page, "nach fehlgeschlagenem Reconnect", deep=True)
        if not ok:
            save_forensics(page, context, "wan-config-destroyed")
            handle_config_destroyed(details)
        else:
            log.info("WAN-Config nach fehlgeschlagenem Reconnect noch intakt.")
            backup_wan_config(details.get("config", {}), tag="post-failed-reconnect")
    except Exception as e:
        log.error("Späte Verifikation nicht durchführbar: %s", e)
        save_forensics(page, context, "late-verify-error")
    finally:
        try:
            browser.close()
        except Exception:
            pass


def _confirm_wan_missing(pw, phase: str) -> bool:
    """Zweitmeinung in einer frischen Browser-Sitzung, bevor der Kill-Switch greift.

    Ein einzelner Fehlschlag in reset_vlan() ist kein Beweis für eine gelöschte
    Config – genau daran scheiterte die Verifikation am 2026-08-07. Gibt True
    zurück, wenn die Zerstörung bestätigt ist; der Kill-Switch ist dann bereits
    gesetzt.
    """
    # Browser-Aufbau bewusst INNERHALB des try: Scheitert schon der Start, darf
    # die Ausnahme nicht entkommen – der Aufrufer steht mitten im
    # Fehlerbehandlungspfad und würde sonst ohne Backoff und ohne Alarm abstürzen.
    browser = context = page = None
    try:
        browser, context = _new_browser(pw)
        page = context.new_page()
        login(page)
        ok, details = verify_wan_intact(page, f"Bestätigung – {phase}", deep=True)
        if ok:
            log.warning(
                "Bestätigungsprüfung: WAN-Config vorhanden und befüllt – der "
                "vorherige Fehlschlag war transient. KEIN Kill-Switch."
            )
            if NOTIFY_FIX_HIT:
                # Zweite Tür: Hier wäre der Watchdog vor dem Fix ohne jede
                # Gegenprüfung stillgelegt worden.
                send_telegram(
                    "🛡️ Fehlalarm verhindert\n\n"
                    f"Beim Lauf ({phase}) war die WAN-Zeile kurzzeitig nicht "
                    "auffindbar. Die Gegenprüfung in frischer Browser-Sitzung "
                    "zeigt die 1&1-Config vollständig vorhanden.\n\n"
                    "Vor dem Fix vom 07.08. hätte sich der Watchdog hier "
                    "abgeschaltet.\n"
                    "Kein Kill-Switch, es wurde nichts verändert."
                )
            return False
        save_forensics(page, context, "wan-config-destroyed")
        handle_config_destroyed(details)
        return True
    except Exception as e:
        # Nicht prüfbar heißt nicht zerstört: Lieber in den Backoff als den
        # Watchdog auf Basis eines unklaren Zustands stilllegen.
        log.error("Bestätigungsprüfung nicht durchführbar: %s – kein Kill-Switch.", e)
        if page is not None and context is not None:
            try:
                save_forensics(page, context, "confirm-error")
            except Exception as fe:
                log.warning("Forensik nach Fehlschlag nicht sicherbar: %s", fe)
        return False
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass


def run_dry_run() -> None:
    """(G) Führt alle Leseschritte aus und protokolliert jede Aktion, die
    im Scharf-Betrieb ausgeführt würde – ohne etwas zu verändern."""
    log.info("=== DRY-RUN: keine Klicks, keine Speicherung, keine Telegram-Nachrichten ===")
    log.info("Backoff- und IP-Prüfung werden im Dry-Run bewusst übersprungen.")

    ip = get_external_ip()
    log.info("Aktuelle externe IP: %s (Glasfaser aktiv: %s)",
             ip, bool(ip and ip.startswith(GW_PREFIX)))

    flag = watchdog_disabled()
    log.info("Kill-Switch: %s", f"GESETZT ({flag})" if flag else "nicht gesetzt")

    with sync_playwright() as pw:
        browser, context = _new_browser(pw)
        page = context.new_page()
        try:
            login(page)
            navigate_to_internet(page)

            count = wan_row_count(page)
            log.info("WAN-Zeilen mit '%s': %d", WAN_NAME, count)
            try:
                rows = page.locator("tr:has(span.edit-modify-icon)").all_inner_texts()
                for i, t in enumerate(rows):
                    log.info("  Übersichtszeile %d: %s", i, " | ".join(t.split("\n")))
            except Exception as e:
                log.warning("Übersichtszeilen nicht lesbar: %s", e)

            reset_vlan(page)
            log.info("=== DRY-RUN erfolgreich abgeschlossen – nichts verändert. ===")
        except Exception as e:
            log.error("DRY-RUN gestoppt: %s: %s", type(e).__name__, e)
            save_forensics(page, context, "dry-run")
        finally:
            try:
                browser.close()
            except Exception:
                pass


def main() -> None:
    global DRY_RUN
    parser = argparse.ArgumentParser(description="Glasfaser Watchdog")
    parser.add_argument("--dry-run", action="store_true",
                        help="Alle Aktionen nur loggen, nichts ausführen oder speichern.")
    args = parser.parse_args()
    DRY_RUN = args.dry_run

    # Auch der Dry-Run meldet sich am Router an und braucht das Passwort.
    if not ROUTER_PASSWORD:
        raise SystemExit(
            "ROUTER_PASSWORD ist nicht gesetzt.\n"
            "Setze es als Umgebungsvariable oder lege eine .env nach dem "
            "Muster von .env.example an (siehe README)."
        )

    log.info("=== Glasfaser Watchdog (%s)%s ===",
             datetime.now().isoformat(), " [DRY-RUN]" if DRY_RUN else "")

    if DRY_RUN:
        run_dry_run()
        return

    # (D) Kill-Switch – blockiert jeden schreibenden Zugriff.
    reason = watchdog_disabled()
    if reason:
        log.critical("Watchdog ist DEAKTIVIERT (%s). Kein Eingriff. "
                     "Zum Reaktivieren: rm %s", reason, DISABLE_FLAG)
        return

    if is_glasfaser_active():
        log.info("Glasfaser-Gateway aktiv – kein Eingriff nötig.")
        st = load_state()
        if st:
            # Nur entwarnen, wenn es vorher wirklich einen Alarm/Backoff gab –
            # ein reiner Karenz-Marker ist kein Vorfall und braucht kein Telegram.
            if st.get("suppress_until") or st.get("fail_count"):
                log.info("Verbindung wieder aktiv – hebe Backoff auf.")
                send_telegram("✅ Glasfaser wieder aktiv – Watchdog-Backoff aufgehoben.")
            else:
                down_for = time.time() - st.get("down_since", 0) if st.get("down_since") else 0
                log.info("Kurzer Aussetzer (%.0fs) hat sich von selbst erledigt – "
                         "kein Toggle nötig, Karenz-Marker zurückgesetzt.", down_for)
            clear_state()
        return

    state = load_state()
    now = time.time()

    # Backoff ist das stärkere Gate und wird zuerst geprüft – sonst würde die
    # Karenz-Logik während eines laufenden Backoffs Zustand schreiben und die
    # Backoff-Meldung verdecken.
    suppress_until = state.get("suppress_until", 0)
    if now < suppress_until:
        mins = int((suppress_until - now) / 60)
        log.warning(
            "Backoff aktiv (Grund: %s) – überspringe VLAN-Reset, manuelle Prüfung "
            "ausstehend (noch ~%d min).", state.get("reason", "?"), mins
        )
        return

    # ─── Karenzzeit ──────────────────────────────────────────────────────────
    # Erst ab dem zweiten aufeinanderfolgenden Ausfall-Befund wird getoggelt.
    down_since = state.get("down_since")
    if not down_since:
        state["down_since"] = now
        save_state(state)
        log.info("Ausfall erstmals erkannt – Karenzzeit läuft (%d min). "
                 "Noch kein Eingriff.", GRACE_PERIOD_SEC // 60)
        return

    down_for = now - down_since
    if down_for < GRACE_PERIOD_SEC:
        log.info("Ausfall besteht seit %.0fs – Karenzzeit (%ds) noch nicht "
                 "abgelaufen, kein Toggle.", down_for, GRACE_PERIOD_SEC)
        return

    log.info("Ausfall besteht durchgehend seit %.0f min – Karenzzeit abgelaufen.",
             down_for / 60)

    log.info("Glasfaser-Gateway NICHT aktiv – starte Router-Konfiguration.")
    if state.get("fail_count", 0) == 0:
        send_telegram("⚠️ Glasfaser nicht aktiv – starte VLAN-Reset am Router…")

    with sync_playwright() as pw:
        browser, context = _new_browser(pw)
        page = context.new_page()

        try:
            login(page)
            reset_vlan(page)
            # Toggle-Zeitpunkt sofort persistieren – ein späterer Lauf braucht ihn,
            # um eine verzögerte Löschung dem Toggle zuordnen zu können.
            state["last_toggle_ts"] = time.time()
            save_state(state)
        except WanConnectionMissing as e:
            log.error("Unerwarteter Fehler – manuelle Prüfung nötig: %s", e)
            save_forensics(page, context, "wan-missing")
            browser.close()

            # (D) Fehlt der Eintrag kurz nach einem eigenen Toggle, ist das keine
            # unerklärte Störung, sondern die verzögerte Zerstörung → Kill-Switch.
            since_toggle = now - state.get("last_toggle_ts", 0)
            if since_toggle < POST_TOGGLE_WINDOW_SEC:
                log.critical("WAN-Eintrag fehlt %.0fs nach eigenem Toggle – "
                             "bestätige in frischer Sitzung, bevor der Kill-Switch greift.",
                             since_toggle)
                if _confirm_wan_missing(pw, f"{int(since_toggle)}s nach Toggle"):
                    return
                # Bestätigung sagt: Config ist da. Trotzdem ist der Lauf
                # gescheitert – Backoff statt Kill-Switch, mit ehrlicher Meldung.
                _enter_backoff(
                    state, now, "wan_row_transient",
                    f"⚠️ Watchdog: WAN-Zeile '{WAN_NAME}' war kurzzeitig nicht auffindbar, "
                    f"die Konfiguration ist bei der Nachprüfung aber vollständig vorhanden.\n"
                    f"Es wurde NICHTS verändert und kein Toggle gefahren.\n"
                    f"Forensik: {FORENSIC_DIR}\n"
                    f"Automatik für {ALERT_COOLDOWN_SEC // 3600}h ausgesetzt."
                )
                return

            _enter_backoff(
                state, now, "wan_missing",
                f"❗ Watchdog: WAN-Verbindung '{WAN_NAME}' ist im Router NICHT vorhanden.\n"
                f"Das ist ein ANDERER als der erwartete Fehler – ein automatischer "
                f"VLAN-Reset hilft hier nicht (Router-Konfig fehlt oder Leitungsproblem).\n"
                f"➡️ Bitte manuell am Router ({ROUTER_URL}) nachschauen.\n"
                f"Forensik: {FORENSIC_DIR}\n"
                f"Automatik für {ALERT_COOLDOWN_SEC // 3600}h ausgesetzt."
            )
            return
        except WanConfigDestroyed as e:
            # Kill-Switch + Alarm sind bereits in handle_config_destroyed() erfolgt.
            log.critical("Abbruch nach zerstörter WAN-Config: %s", e)
            save_forensics(page, context, "wan-config-destroyed")
            browser.close()
            return
        except (DestructiveActionBlocked, SelectorAmbiguous, ContextMismatch) as e:
            # (A/B/C) Sicherheitsabbruch – Skript beenden statt blind weiterlaufen.
            log.critical("SICHERHEITSABBRUCH (%s): %s", type(e).__name__, e)
            save_forensics(page, context, "safety-abort")
            browser.close()
            disable_watchdog(f"{type(e).__name__}: {e}")
            send_telegram(
                f"🛑 Watchdog-Sicherheitsabbruch: {type(e).__name__}\n\n{e}\n\n"
                f"Wahrscheinliche Ursache: Router-Oberfläche hat sich geändert "
                f"(z. B. nach Firmware-Update).\n"
                f"Der Watchdog hat sich SELBST DEAKTIVIERT.\n"
                f"➡️ Mit --dry-run prüfen, dann: rm {DISABLE_FLAG}"
            )
            return
        except Exception as e:
            log.error("Fehler bei der Router-Konfiguration: %s", e)
            save_forensics(page, context, "router-error")
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

        log.info("Warte bis zu %d Sekunden auf Verbindungsaufbau "
                 "(Prüfung alle %ds)…", CHECK_WAIT_SEC, CHECK_POLL_SEC)

        if wait_for_glasfaser():
            ip = get_external_ip()
            log.info("Glasfaser aktiv (externe IP 94.134.x.x). Reparatur erfolgreich.")
            send_telegram(f"✅ Glasfaser wieder aktiv! Externe IP: {ip}")
            clear_state()
            return

        ip = get_external_ip()
        log.error(
            "Glasfaser nach %ds noch nicht aktiv. Externe IP aktuell: %s",
            CHECK_WAIT_SEC, ip
        )

        # (D) Genau hier traten beide WAN-Verluste auf – nachsehen statt annehmen.
        _late_verification(pw)

    if watchdog_disabled():
        return   # Kill-Switch wurde soeben gesetzt – kein Backoff-Alarm mehr nötig.

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

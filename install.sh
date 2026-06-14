#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CURRENT_USER="$(whoami)"

echo "=== Glasfaser Watchdog Installation ==="
echo "Verzeichnis: $SCRIPT_DIR"
echo "Benutzer:    $CURRENT_USER"
echo ""

# .env prüfen
if [ ! -f "$SCRIPT_DIR/.env" ]; then
    echo "FEHLER: $SCRIPT_DIR/.env nicht gefunden."
    echo "Bitte erst: cp $SCRIPT_DIR/.env.example $SCRIPT_DIR/.env"
    echo "Dann .env mit deinen Werten befüllen."
    exit 1
fi

# Python-Abhängigkeiten installieren
pip3 install playwright python-dotenv
playwright install chromium
playwright install-deps chromium

# systemd-Units anpassen (YOURUSER → aktueller Benutzer + Pfad)
SERVICE_FILE="/tmp/glasfaser_watchdog.service"
sed "s|YOURUSER|$CURRENT_USER|g; s|/home/YOURUSER/glasfaser-watchdog|$SCRIPT_DIR|g" \
    "$SCRIPT_DIR/glasfaser_watchdog.service" > "$SERVICE_FILE"

cp "$SERVICE_FILE" /etc/systemd/system/glasfaser_watchdog.service
cp "$SCRIPT_DIR/glasfaser_watchdog.timer" /etc/systemd/system/glasfaser_watchdog.timer

systemctl daemon-reload
systemctl enable --now glasfaser_watchdog.timer

echo ""
echo "=== Installation abgeschlossen ==="
echo ""
echo "Status:"
systemctl status glasfaser_watchdog.timer --no-pager
echo ""
echo "Laufende Timer:"
systemctl list-timers glasfaser_watchdog.timer --no-pager
echo ""
echo "Manuell testen: python3 $SCRIPT_DIR/glasfaser_watchdog.py"
echo "Logs (systemd): journalctl -u glasfaser_watchdog.service -f"
echo "Logs (Datei):   tail -f $SCRIPT_DIR/glasfaser_watchdog.log"

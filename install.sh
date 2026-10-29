#!/bin/zsh
# Paigaldab piletivalvuri ~/Library/Application Support/Piletivalvur alla ja käivitab
# selle launchd kaudu iga 5 minuti järel. Documents kausta launchd taustal lugeda ei saa
# (macOS-i privaatsuskaitse), seepärast töötab koopia Application Supportis.
#   ./install.sh            – paigalda või uuenda (config.json jääb alles)
#   ./install.sh --uninstall – peata ja eemalda ajastus (failid jäävad alles)
set -euo pipefail

SRC="${0:A:h}"
DEST="$HOME/Library/Application Support/Piletivalvur"
LABEL="local.piletivalvur"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true

if [[ "${1:-}" == "--uninstall" ]]; then
  rm -f "$PLIST"
  echo "Piletivalvur peatatud. Failid on alles: $DEST"
  exit 0
fi

mkdir -p "$DEST" "$HOME/Library/LaunchAgents"
cp "$SRC/piletivalvur.py" "$DEST/"
if [[ ! -f "$DEST/config.json" ]]; then
  topic="draamateater-$(/usr/bin/python3 -c 'import secrets; print(secrets.token_hex(8))')"
  sed "s/__NTFY_TOPIC__/$topic/" "$SRC/config.example.json" > "$DEST/config.json"
fi

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>$DEST/piletivalvur.py</string>
  </array>
  <key>StartInterval</key><integer>300</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>/dev/null</string>
  <key>StandardErrorPath</key><string>$DEST/stderr.log</string>
</dict>
</plist>
EOF

launchctl bootstrap "$DOMAIN" "$PLIST"
echo "Piletivalvur töötab. Seaded: $DEST/config.json"
echo "ntfy teema: $(/usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["ntfy_topic"] or "– (telefoniteavitused saadab GitHub Actions)")' "$DEST/config.json")"

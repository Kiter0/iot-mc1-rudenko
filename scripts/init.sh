#!/usr/bin/env bash
# .env з випадковими паролями, сертифікати, passwd брокера, broker.things
set -euo pipefail
cd "$(dirname "$0")/.."
export MSYS_NO_PATHCONV=1

if [ ! -f .env ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    case "$line" in
      *=change_me) echo "${line%=change_me}=$(openssl rand -hex 12)" ;;
      *) echo "$line" ;;
    esac
  done < .env.example > .env
  echo "[init] створено .env"
fi
set -a; . ./.env; set +a

./certs/generate.sh

: > mosquitto/config/passwd
for u in admin openhab motion_livingroom motion_bedroom temperature humidity lux power light_livingroom light_bedroom heater; do
  var="MQTT_PASS_$(echo "$u" | tr a-z A-Z)"
  docker run --rm -v "$PWD/mosquitto/config:/work" eclipse-mosquitto:2.0.20 \
    mosquitto_passwd -b /work/passwd "$u" "${!var}"
done
chmod 644 mosquitto/config/passwd
echo "[init] створено passwd"

sed "s/__MQTT_PASS_OPENHAB__/${MQTT_PASS_OPENHAB}/" openhab/conf/things/broker.things.template > openhab/conf/things/broker.things
echo "[init] створено broker.things. Далі: docker compose up -d"
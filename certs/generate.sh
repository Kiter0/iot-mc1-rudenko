#!/usr/bin/env bash
# Генерація власного CA та серверного сертифіката брокера. Ключі в .gitignore.
set -euo pipefail
export MSYS_NO_PATHCONV=1
cd "$(dirname "$0")"
if [ -f ca.crt ] && [ -f server.crt ] && [ -f server.key ]; then
  echo "[certs] сертифікати вже існують"; exit 0
fi
openssl genrsa -out ca.key 4096
openssl req -x509 -new -nodes -key ca.key -sha256 -days 365 -subj "/CN=Flat IoT CA/O=MNTU" -out ca.crt
openssl genrsa -out server.key 2048
openssl req -new -key server.key -subj "/CN=mosquitto" -out server.csr
cat > server.ext <<EXT
subjectAltName = DNS:mosquitto, DNS:localhost, IP:127.0.0.1
extendedKeyUsage = serverAuth
basicConstraints = CA:FALSE
EXT
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -out server.crt -days 365 -sha256 -extfile server.ext
chmod 644 server.key ca.key
rm -f server.csr server.ext
echo "[certs] готово"
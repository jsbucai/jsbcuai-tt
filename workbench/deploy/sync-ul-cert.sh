#!/bin/sh
set -eu
umask 077
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT INT TERM
ssh -i /root/.ssh/ul-cert-sync -o BatchMode=yes -o ConnectTimeout=10 \
    -o StrictHostKeyChecking=yes root@192.168.100.1 > "$TMP_DIR/cert.tar.gz"
tar -xzf "$TMP_DIR/cert.tar.gz" -C "$TMP_DIR"
openssl x509 -checkend 86400 -noout -in "$TMP_DIR/fullchain.cer"
openssl x509 -in "$TMP_DIR/fullchain.cer" -noout -checkhost ul.yingyinghome.com
cert_public="$(openssl x509 -in "$TMP_DIR/fullchain.cer" -pubkey -noout | openssl sha256)"
key_public="$(openssl pkey -in "$TMP_DIR/ul.yingyinghome.com.key" -pubout | openssl sha256)"
[ "$cert_public" = "$key_public" ] || exit 1
install -d -m 0750 -o root -g caddy /etc/caddy/tls
install -m 0644 -o root -g caddy "$TMP_DIR/fullchain.cer" /etc/caddy/tls/ul-fullchain.cer
install -m 0640 -o root -g caddy "$TMP_DIR/ul.yingyinghome.com.key" /etc/caddy/tls/ul.key
caddy validate --config /etc/caddy/Caddyfile
systemctl reload caddy

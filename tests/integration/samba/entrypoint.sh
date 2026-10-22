#!/bin/sh
set -e

SMB_USER="${SMB_USER:-degreaser}"
SMB_PASS="${SMB_PASS:-degreaser}"

if ! id "$SMB_USER" >/dev/null 2>&1; then
    adduser -D -H -s /sbin/nologin "$SMB_USER"
fi
printf '%s\n%s\n' "$SMB_PASS" "$SMB_PASS" | smbpasswd -a -s "$SMB_USER" >/dev/null

mkdir -p /srv/data /srv/bench
chown "$SMB_USER" /srv/data /srv/bench

# Optional artificial latency (added to every packet leaving the container, i.e. to the RTT).
if [ -n "$LATENCY_MS" ] && [ "$LATENCY_MS" != "0" ]; then
    tc qdisc replace dev eth0 root netem delay "${LATENCY_MS}ms"
fi

exec smbd --foreground --no-process-group --debug-stdout

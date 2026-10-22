#!/bin/sh
# Usage: set-latency.sh <ms>   (0 removes the delay)
set -e
ms="${1:?usage: set-latency.sh <ms>}"
if [ "$ms" = "0" ]; then
    docker exec degreaser-samba tc qdisc del dev eth0 root 2>/dev/null || true
else
    docker exec degreaser-samba tc qdisc replace dev eth0 root netem delay "${ms}ms"
fi

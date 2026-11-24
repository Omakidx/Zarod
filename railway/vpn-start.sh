#!/bin/sh
# Starts one wireproxy per VPN location in vpn-locations.txt: the 1st line listens on 8881, the 2nd on 8882,
# and so on. The bot reaches them over Railway's private network, e.g. http://bot:PASS@vpn.railway.internal:8881
set -eu
: "${SURFSHARK_WG_PRIVATE_KEY:?set SURFSHARK_WG_PRIVATE_KEY}"
: "${VPN_PROXY_PASSWORD:?set VPN_PROXY_PASSWORD (the bot uses it in VPN_PROXY_BASE / PROXIES)}"
# One line per location: name country server_ip server_public_key [# city]. Your one WireGuard key works on
# every Surfshark server; each server cluster has its own public key (Surfshark publishes them).
LOCATIONS="${VPN_LOCATIONS_FILE:-/usr/local/share/vpn-locations.txt}"
# Railway drops tunnel packets above this: tested there, 1420 and 1280 lose every full-size reply, 1200 works.
MTU="${VPN_MTU:-1200}"
umask 077
pids=""
port=8881
while read -r name country ip key _rest; do
case "$name" in ''|'#'*) continue ;; esac
conf="/tmp/wg-$port.conf"
printf '[Interface]\nPrivateKey = %s\nAddress = 10.14.0.2/16\nDNS = 162.252.172.57, 149.154.159.92\nMTU = %s\n\n[Peer]\nPublicKey = %s\nEndpoint = %s:51820\nAllowedIPs = 0.0.0.0/0\nPersistentKeepalive = 25\n\n[http]\nBindAddress = [::]:%s\nUsername = bot\nPassword = %s\n' "$SURFSHARK_WG_PRIVATE_KEY" "$MTU" "$key" "$ip" "$port" "$VPN_PROXY_PASSWORD" > "$conf"
echo "port $port -> $name ($country) $ip"
wireproxy -c "$conf" >/dev/null 2>&1 &
pids="$pids $!"
port=$((port + 1))
done < "$LOCATIONS"
# If any tunnel exits, exit too so Railway restarts the whole service.
while :; do
for pid in $pids; do
kill -0 "$pid" 2>/dev/null || { echo "wireproxy (pid $pid) exited; restarting service" >&2; exit 1; }
done
sleep 10
done

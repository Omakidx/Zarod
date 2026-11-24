#!/bin/sh
# Starts one wireproxy per Surfshark endpoint IP: the 1st listens on 8881, the 2nd on 8882, and so on.
# The bot reaches them over Railway's private network, e.g. http://bot:PASS@vpn.railway.internal:8881
set -eu
: "${SURFSHARK_WG_PRIVATE_KEY:?set SURFSHARK_WG_PRIVATE_KEY}"
: "${VPN_PROXY_PASSWORD:?set VPN_PROXY_PASSWORD (the bot uses it in PROXIES)}"
# us-ash.prod.surfshark.com: one public key for all its IPs (from gluetun v3.40.0's server list).
SERVER_PUBKEY="${VPN_SERVER_PUBKEY:-9ef2f8mXHnAxx06lx4OxhtzASfJgv6YfEimuVyef6QE=}"
# Location order: main, ash2 … ash20 (ports 8881 … 8900); the bot's PROXIES + PROXIES_EXTRA must match.
# Keep existing entries in place (an account keeps its location, so its IP). ash3 was 37.19.206.34 until
# Surfshark retired it (Nov 2026: no reply at all). `dig us-ash.prod.surfshark.com` rotates through the
# current servers (65+ seen); each one gives its own exit IP (the server address + 1). ash6-ash20 were
# tested from this service before being added.
ENDPOINT_IPS="${VPN_ENDPOINT_IPS:-45.144.115.26 45.144.115.34 151.240.254.12 149.102.227.103 185.156.46.102 \
37.19.206.39 37.19.220.194 45.144.115.3 45.144.115.138 45.144.115.217 \
149.102.227.98 149.102.227.113 151.240.254.5 151.240.254.25 151.240.254.45 \
185.156.46.97 185.156.46.117 37.19.206.46 37.19.220.199 45.144.115.195}"
# Railway drops tunnel packets above this: tested there, 1420 and 1280 lose every full-size reply, 1200 works.
MTU="${VPN_MTU:-1200}"
umask 077
pids=""
port=8881
for ip in $ENDPOINT_IPS; do
conf="/tmp/wg-$port.conf"
printf '[Interface]\nPrivateKey = %s\nAddress = 10.14.0.2/16\nDNS = 162.252.172.57, 149.154.159.92\nMTU = %s\n\n[Peer]\nPublicKey = %s\nEndpoint = %s:51820\nAllowedIPs = 0.0.0.0/0\nPersistentKeepalive = 25\n\n[http]\nBindAddress = [::]:%s\nUsername = bot\nPassword = %s\n' "$SURFSHARK_WG_PRIVATE_KEY" "$MTU" "$SERVER_PUBKEY" "$ip" "$port" "$VPN_PROXY_PASSWORD" > "$conf"
echo "port $port -> $ip"
wireproxy -c "$conf" &
pids="$pids $!"
port=$((port + 1))
done
# If any tunnel exits, exit too so Railway restarts the whole service.
while :; do
for pid in $pids; do
kill -0 "$pid" 2>/dev/null || { echo "wireproxy (pid $pid) exited; restarting service" >&2; exit 1; }
done
sleep 10
done

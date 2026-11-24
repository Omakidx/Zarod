# Running on Railway

Railway can't run gluetun (it needs `NET_ADMIN` and `/dev/net/tun`), so there the VPN is a
userspace WireGuard service (`railway/vpn.Dockerfile`, [wireproxy](https://github.com/windtf/wireproxy)).
It opens one tunnel per pinned Surfshark Ashburn IP, in the same order as `docker-compose.yml`,
each as an HTTP proxy on its own port, one per line of `railway/vpn-locations.txt` (line 1 = 8881; 40 locations today: 30 in the US, 10 abroad). The bot still sends only X/Instagram traffic
through them. A tunnel only connects when traffic first goes through it.

One Railway project, two services from this repo. Both redeploy on every push to `main`.

| Service | Build | Variables |
|---|---|---|
| `vpn` | `railway/vpn.Dockerfile` | `RAILWAY_DOCKERFILE_PATH`, `SURFSHARK_WG_PRIVATE_KEY`, `VPN_PROXY_PASSWORD` (optional: `VPN_MTU`) |
| `bot` | root `Dockerfile` | the ones below. Volume mounted at `/data` |

The `vpn` service has the watch path `/railway/**` (Settings → Build → Watch Paths), so it only rebuilds when its own files change. Without it, every push restarted the tunnels and briefly cut the bot's accounts off.

`vpn` variables:

```
RAILWAY_DOCKERFILE_PATH="railway/vpn.Dockerfile"
VPN_PROXY_PASSWORD="${{secret(40, "abcdef0123456789")}}"
SURFSHARK_WG_PRIVATE_KEY=...
```

Railway fills in `VPN_PROXY_PASSWORD` with a random value when it is saved.

`bot` variables: `TELEGRAM_BOT_TOKEN`, `OWNER_TELEGRAM_ID`, `ENCRYPTION_KEY` and the user agents
from `.env.example` (the rate limits are optional and default to the values shown there), plus:

```
PROXIES=main=http://bot:${{vpn.VPN_PROXY_PASSWORD}}@vpn.railway.internal:8881,ash2=http://bot:${{vpn.VPN_PROXY_PASSWORD}}@vpn.railway.internal:8882,ash3=http://bot:${{vpn.VPN_PROXY_PASSWORD}}@vpn.railway.internal:8883,ash4=http://bot:${{vpn.VPN_PROXY_PASSWORD}}@vpn.railway.internal:8884,ash5=http://bot:${{vpn.VPN_PROXY_PASSWORD}}@vpn.railway.internal:8885
PROXIES_EXTRA=ash6-ash20=http://bot:${{vpn.VPN_PROXY_PASSWORD}}@vpn.railway.internal:8886
VPN_PROXY_BASE=http://bot:${{vpn.VPN_PROXY_PASSWORD}}@vpn.railway.internal
MAX_ACCOUNTS_PER_IP=3   # optional, 3 is the default
REQUIRE_PROXY=true
DB_PATH=/data/followbot.db
RAILWAY_RUN_UID=0
```

Notes:

- The service must be named `vpn`: `PROXIES` reaches it at `vpn.railway.internal` and reads its password.
- `RAILWAY_RUN_UID=0` lets the bot write to the volume, which Railway mounts as root.
- Railway rejects `VOLUME` in a Dockerfile, so the root `Dockerfile` has none. Compose mounts `./data` itself.
- Keep `ENCRYPTION_KEY` with the database. A new key means reconnecting every account with `/connect`.
- The tunnels use MTU 1200: at 1280 or more, Railway drops full-size replies and requests through the VPN time out (curl error 28).
- **Locations live in `railway/vpn-locations.txt`** (`name country server_ip server_public_key # city`), which both services read: the vpn service starts a tunnel per line, and the bot (with `VPN_PROXY_BASE`) gets every location and its country from it. **Append only**: accounts keep their location name, so reordering lines would move them to other IPs. `PROXIES` / `PROXIES_EXTRA` still work and override single locations.
- **Adding locations**: no Surfshark login needed. The one WireGuard key works on every Surfshark server; each server cluster's hostname and public key are in Surfshark's public list (`https://api.surfshark.com/v4/server/clusters/generic`), and `dig +short <hostname>` gives its server IPs. Test a server from the vpn console before adding it. Each tunnel costs ~17 MB of memory.
- **Spreading**: on start the bot evens accounts out so at most `MAX_ACCOUNTS_PER_IP` (3) share an IP, **only moving an account within its country** (a jump abroad invites "is this you?" checks). New accounts get the least-used location, which can be abroad. It reports how many it moved, and warns if a location is over the limit.
- Surfshark retires server IPs now and then; a retired one gives no reply at all (ReadTimeout). `dig +short us-ash.prod.surfshark.com` lists current ones: put a replacement IP on that location's line in `railway/vpn-locations.txt` and keep the location name, so its accounts keep a stable location. ash3 moved from 37.19.206.34 to 151.240.254.12 in Nov 2026.
- Neither service needs a public domain. Run only one copy of the bot per Telegram token.

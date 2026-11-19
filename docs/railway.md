# Running on Railway

Railway can't run gluetun (it needs `NET_ADMIN` and `/dev/net/tun`), so there the VPN is a
userspace WireGuard service (`railway/vpn.Dockerfile`, [wireproxy](https://github.com/windtf/wireproxy)).
It opens one tunnel per pinned Surfshark Ashburn IP, in the same order as `docker-compose.yml`,
each as an HTTP proxy on its own port (8881 to 8885). The bot still sends only X/Instagram traffic
through them. A tunnel only connects when traffic first goes through it.

One Railway project, two services from this repo. Both redeploy on every push to `main`.

| Service | Build | Variables |
|---|---|---|
| `vpn` | `railway/vpn.Dockerfile` | `RAILWAY_DOCKERFILE_PATH`, `SURFSHARK_WG_PRIVATE_KEY`, `VPN_PROXY_PASSWORD` (optional: `VPN_ENDPOINT_IPS`, `VPN_SERVER_PUBKEY`) |
| `bot` | root `Dockerfile` | the ones below. Volume mounted at `/data` |

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
REQUIRE_PROXY=true
DB_PATH=/data/followbot.db
RAILWAY_RUN_UID=0
```

Notes:

- The service must be named `vpn`: `PROXIES` reaches it at `vpn.railway.internal` and reads its password.
- `RAILWAY_RUN_UID=0` lets the bot write to the volume, which Railway mounts as root.
- Railway rejects `VOLUME` in a Dockerfile, so the root `Dockerfile` has none. Compose mounts `./data` itself.
- Keep `ENCRYPTION_KEY` with the database. A new key means reconnecting every account with `/connect`.
- Neither service needs a public domain. Run only one copy of the bot per Telegram token.

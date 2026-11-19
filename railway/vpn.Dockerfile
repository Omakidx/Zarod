# Userspace WireGuard for Railway, which can't run gluetun (no NET_ADMIN, no /dev/net/tun).
# One wireproxy per pinned Surfshark Ashburn IP, each an HTTP proxy on its own port (8881…).
FROM alpine:3.20

ARG WIREPROXY_VERSION=v1.1.3
ARG WIREPROXY_SHA256=e88c1d090740373fc606c1bafd81d9a5eadc642cce5667616e20e9d7a444f51c

RUN apk add --no-cache ca-certificates curl \
 && curl -fsSL -o /tmp/wp.tgz \
      "https://github.com/windtf/wireproxy/releases/download/${WIREPROXY_VERSION}/wireproxy_linux_amd64.tar.gz" \
 && echo "${WIREPROXY_SHA256}  /tmp/wp.tgz" | sha256sum -c - \
 && tar -xzf /tmp/wp.tgz -C /usr/local/bin wireproxy \
 && rm /tmp/wp.tgz \
 && apk del curl \
 && adduser -D -u 10001 vpn

COPY railway/vpn-start.sh /usr/local/bin/vpn-start
USER vpn
CMD ["/bin/sh", "/usr/local/bin/vpn-start"]

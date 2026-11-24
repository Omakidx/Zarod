FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY followbot ./followbot
# The VPN location list the vpn service also uses: names, countries and port order.
COPY railway/vpn-locations.txt ./railway/vpn-locations.txt

RUN useradd --uid 10001 --create-home bot && mkdir -p /data && chown bot:bot /data
USER bot

CMD ["python", "-m", "followbot"]

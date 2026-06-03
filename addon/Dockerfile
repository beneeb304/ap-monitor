# Home Assistant add-on image for AP Monitor.
# The app code is pulled from GitHub at build time so this add-on folder stays
# tiny and never duplicates the source. Point REPO/REF at your fork if needed.
FROM python:3.12-slim

ARG REPO=https://github.com/YOURUSER/ap-monitor
ARG REF=main

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 --branch "${REF}" "${REPO}" /app
WORKDIR /app
RUN pip install --no-cache-dir -r requirements.txt

# Read runtime config from /share, persist history under the add-on's /data.
ENV AP_MONITOR_CONFIG=/share/ap-monitor/config.yaml

CMD ["python", "app.py"]

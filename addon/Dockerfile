# Home Assistant add-on image for AP Monitor.
# The app code is pulled from GitHub at build time so this add-on folder stays
# tiny and never duplicates the source. Point REPO/REF at your fork if needed.
FROM python:3.12-slim

ARG REPO=https://github.com/beneeb304/ap-monitor
ARG REF=main

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Cache-bust the clone: ADD of a URL is re-fetched on every build, so this
# layer (and everything after it) invalidates whenever the branch moves.
# Without it, Docker layer caching can reuse a stale clone from a previous
# build and ship old code under a new version number.
ADD https://api.github.com/repos/beneeb304/ap-monitor/git/refs/heads/main /tmp/git-ref.json
RUN git clone --depth 1 --branch "${REF}" "${REPO}" /app
WORKDIR /app
RUN pip install --no-cache-dir -r requirements.txt

# Read runtime config from /share, persist history under the add-on's /data.
ENV AP_MONITOR_CONFIG=/share/ap-monitor/config.yaml

CMD ["python", "app.py"]

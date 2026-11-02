# Build: docker build --pull --build-arg PATCHED_AT=$(date +%s) --build-arg REVISION=$(git rev-parse HEAD) -t sensi-agent .
# Run:   docker run -d --name sensi-agent --restart unless-stopped \
#     --env-file agent.env \
#     -v sensi-agent-data:/var/lib/sensi-agent \
#     -p 8765:8765 \
#     sensi-agent
#
# agent.env holds the settings in .env.example (the deploy workflow renders it from repo variables and secrets).

FROM python:3.13-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
ENV TZ=America/New_York

ARG DEBIAN_FRONTEND=noninteractive
ARG PATCHED_AT=unknown
RUN echo "Patching the base OS ($PATCHED_AT)" \
 && apt-get update \
 && apt-get -y full-upgrade \
 && apt-get install -y --no-install-recommends ca-certificates tzdata \
 && ln -snf "/usr/share/zoneinfo/$TZ" /etc/localtime && echo "$TZ" > /etc/timezone \
 && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN groupadd --system sensi-agent \
 && useradd --system --gid sensi-agent --home-dir /var/lib/sensi-agent --shell /usr/sbin/nologin sensi-agent \
 && install -d -o sensi-agent -g sensi-agent /var/lib/sensi-agent

WORKDIR /opt/sensi-agent/app
COPY requirements.txt ./
RUN pip install -r requirements.txt
COPY sensi_agent/ ./sensi_agent/
ARG REVISION=unknown
RUN echo "$REVISION" > REVISION

ENV STATE_PATH=/var/lib/sensi-agent/state.json TOKENS_PATH=/var/lib/sensi-agent/tokens.json
USER sensi-agent
VOLUME ["/var/lib/sensi-agent"]
EXPOSE 8765
HEALTHCHECK --interval=60s --timeout=15s --start-period=2m --retries=5 \
  CMD ["python", "-m", "sensi_agent.health"]
CMD ["python", "-m", "sensi_agent"]

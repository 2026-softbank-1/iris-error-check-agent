# syntax=docker/dockerfile:1

FROM node:22-bookworm-slim AS opencode
WORKDIR /opt/opencode
RUN npm install --save-exact --no-audit --no-fund opencode-ai@1.18.34 \
    && test "$(node_modules/opencode-ai/bin/opencode.exe --version)" = "1.18.34"

FROM python:3.12-slim-bookworm AS python-build
WORKDIR /build
COPY requirements.lock ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.lock setuptools==84.0.0
COPY pyproject.toml README.md ./
COPY src ./src
RUN /opt/venv/bin/pip install --no-cache-dir --no-build-isolation --no-deps . \
    && /opt/venv/bin/pip check

FROM python:3.12-slim-bookworm AS runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git libstdc++6 tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 agent \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/agent agent

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AGENT_RUNTIME=opencode

WORKDIR /app
COPY --from=python-build /opt/venv /opt/venv
# OpenCode 1.18.34 installs its standalone ELF binary with this filename on Linux too.
COPY --from=opencode --chmod=755 /opt/opencode/node_modules/opencode-ai/bin/opencode.exe /app/.runtime/opencode-tooling/node_modules/opencode-ai/bin/opencode.exe
COPY --from=opencode /opt/opencode/node_modules/opencode-ai/LICENSE /usr/share/licenses/opencode/LICENSE
RUN mkdir -p /app/.runtime/opencode-runs \
    && chown agent:agent /app/.runtime/opencode-runs

USER 10001:10001
RUN test "$(/app/.runtime/opencode-tooling/node_modules/opencode-ai/bin/opencode.exe --version)" = "1.18.34"
EXPOSE 8001
HEALTHCHECK --interval=30s --timeout=3s --start-period=45s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/healthz', timeout=2).close()"]
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/bin/tini", "-e", "143", "--", "python", "-m", "ai_error_check_agent.api"]
CMD ["--host", "0.0.0.0", "--port", "8001"]

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    AGENT_DB=/app/data/agent.db AGENT_CORPUS=/app/corpus
WORKDIR /app
COPY requirements-runtime.lock pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir -r requirements-runtime.lock . \
    && groupadd --gid 10001 agent \
    && useradd --uid 10001 --gid agent --no-create-home agent \
    && mkdir /app/data && chown agent:agent /app/data
COPY corpus ./corpus
USER 10001:10001
EXPOSE 8090
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/healthz', timeout=2)"
CMD ["evidence-agent", "serve", "--host", "0.0.0.0"]

# auth container. Build context: repository root (docker compose does this).
# The image holds dependencies and a copy of the code; compose.yaml bind-mounts common/ and auth/ over
# the copies for development, so code changes need no rebuild.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app/common:/app/auth
WORKDIR /app
COPY common/requirements.txt /tmp/common.txt
COPY auth/requirements.txt /tmp/service.txt
RUN pip install --no-cache-dir -r /tmp/common.txt -r /tmp/service.txt \
    && useradd --create-home --uid 10001 easyq \
    && mkdir -p /app/var/objects && chown -R easyq:easyq /app
COPY --chown=easyq:easyq common /app/common
COPY --chown=easyq:easyq auth /app/auth
USER easyq
EXPOSE 8000
CMD ["uvicorn", "--factory", "easyq_auth.main:create_app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--proxy-headers"]

FROM python:3.12-slim
WORKDIR /app
COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 easyq \
    && mkdir -p /app/var/objects \
    && chown -R easyq:easyq /app
COPY --chown=easyq:easyq backend/easyq /app/easyq
USER easyq
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "easyq.backend_app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]

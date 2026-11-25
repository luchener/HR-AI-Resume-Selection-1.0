FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
WORKDIR /app
COPY apps/backend/requirements.txt ./
RUN pip install --no-cache-dir --timeout 30 --retries 2 -r requirements.txt
COPY apps/backend/*.py ./
COPY apps/backend/school_tiers.json ./
COPY apps/backend/tools/ ./tools/
COPY scripts/database-drain-review/*.py ./
COPY scripts/database-drain-review/start.sh /review-start.sh
RUN mkdir -p /app/data /app/logs /review && chown -R 10001:10001 /app /review
ENV ENV=local SOURCE_DRAIN_STATE_PATH=/review/control.sqlite PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
ENTRYPOINT ["sh", "/review-start.sh"]

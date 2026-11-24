FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN --mount=type=secret,id=pypi_ca \
    if [ -f /run/secrets/pypi_ca ]; then export PIP_CERT=/run/secrets/pypi_ca; fi; \
    pip install --no-cache-dir -r requirements.txt \
    && groupadd -g 10001 gup && useradd -u 10001 -g gup -M gup \
    && install -d -o gup -g gup -m 0700 /data
COPY --chown=10001:10001 . /app/server
USER 10001:10001
CMD ["sh", "-c", "umask 077 && exec uvicorn server.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips 172.29.55.3 --no-access-log"]

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/data
WORKDIR /app
RUN groupadd --gid 10001 scanner && useradd --uid 10001 --gid scanner --no-create-home scanner \
    && mkdir /data && chown scanner:scanner /data
COPY requirements-stream.txt .
RUN pip install --no-cache-dir -r requirements-stream.txt
COPY *.py paper-policy.json profiles.json ./
COPY tests/ ./tests/
USER 10001:10001
STOPSIGNAL SIGINT
ENTRYPOINT ["python", "server.py"]
CMD ["paper"]

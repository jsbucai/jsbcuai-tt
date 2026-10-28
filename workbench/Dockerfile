FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Asia/Shanghai
WORKDIR /app/service
COPY service/requirements-api.txt /tmp/requirements.txt
ARG APT_MIRROR=https://deb.debian.org
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN sed -i "s|http://deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --index-url "$PIP_INDEX_URL" -r /tmp/requirements.txt \
    && groupadd --gid 10001 ul && useradd --uid 10001 --gid ul --no-create-home ul
COPY service /app/service
RUN mkdir -p /app/service/runtime/data /app/releases && chown -R ul:ul /app
USER 10001:10001
EXPOSE 5191
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5191/livez',timeout=3)"
CMD ["python", "run.py", "--host", "0.0.0.0", "--port", "5191"]

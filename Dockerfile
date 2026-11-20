FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY demo ./demo

RUN mkdir -p /data/archive /data/recovery && chmod +x demo/holder.py demo/entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/srv/demo/entrypoint.sh"]
CMD ["api"]

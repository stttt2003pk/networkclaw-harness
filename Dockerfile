FROM python:3.12.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/networkclaw/src:/opt/networkclaw/vendor/hermes

WORKDIR /opt/networkclaw

COPY requirements.lock ./requirements.lock
COPY offline/wheels/ ./offline/wheels/
RUN python -m pip install --no-index --find-links offline/wheels --require-hashes -r requirements.lock

COPY src/ ./src/
COPY vendor/hermes/ ./vendor/hermes/
COPY upstream/ ./upstream/

ENTRYPOINT ["python", "-m", "networkclaw_harness.host"]

FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7

ARG SOURCE_COMMIT=unknown
ARG SOURCE_DATE_EPOCH=0
ARG VENDOR_COMMIT=unknown
ARG HARNESS_VERSION=unknown
ARG PROTOCOL_VERSION=1.0

LABEL org.opencontainers.image.title="NetworkClaw Harness" \
    org.opencontainers.image.version="$HARNESS_VERSION" \
    org.opencontainers.image.revision="$SOURCE_COMMIT" \
    io.networkclaw.hermes.vendor-commit="$VENDOR_COMMIT" \
    io.networkclaw.harness.protocol-version="$PROTOCOL_VERSION"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/networkclaw/src:/opt/networkclaw/vendor/hermes

WORKDIR /opt/networkclaw

COPY requirements.lock ./requirements.lock
COPY offline/wheels/ ./offline/wheels/
RUN python -m pip install --no-index --find-links offline/wheels --require-hashes -r requirements.lock

RUN groupadd --system --gid 65532 networkclaw \
    && useradd --system --uid 65532 --gid networkclaw --home-dir /nonexistent --shell /usr/sbin/nologin networkclaw

COPY --chown=networkclaw:networkclaw src/ ./src/
COPY --chown=networkclaw:networkclaw vendor/hermes/ ./vendor/hermes/
COPY --chown=networkclaw:networkclaw upstream/ ./upstream/

USER 65532:65532

ENTRYPOINT ["python", "-m", "networkclaw_harness.host"]

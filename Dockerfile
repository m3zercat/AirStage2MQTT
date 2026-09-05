FROM python:3.14-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

FROM base AS test

COPY requirements.lock requirements-dev.lock pyproject.toml README.md ./
RUN python -m pip install --no-cache-dir -r requirements-dev.lock
COPY src ./src
COPY tests ./tests
RUN python -m ruff check src tests \
    && python -m ruff format --check src tests \
    && python -m mypy src \
    && python -m pytest --verbose

FROM base AS runtime

ARG A2M_VERSION=0.1.0
ENV A2M_VERSION=${A2M_VERSION}

RUN groupadd --gid 1000 airstage2mqtt \
    && useradd --uid 1000 --gid 1000 --create-home --home-dir /home/airstage2mqtt airstage2mqtt \
    && mkdir -p /config /data \
    && chown -R 1000:1000 /config /data

COPY requirements.lock pyproject.toml README.md ./
RUN python -m pip install --no-cache-dir -r requirements.lock
COPY src ./src
RUN python -m pip install --no-cache-dir --no-deps .

USER 1000:1000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-m", "airstage2mqtt", "healthcheck"]

ENTRYPOINT ["python", "-m", "airstage2mqtt"]
CMD ["run"]

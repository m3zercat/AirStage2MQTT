FROM python:3.14-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

FROM base AS runtime-dependencies

COPY requirements.lock ./
RUN python -m pip install --no-cache-dir -r requirements.lock

FROM runtime-dependencies AS test-dependencies

COPY requirements-dev.lock ./
RUN python -m pip install --no-cache-dir -r requirements-dev.lock

FROM test-dependencies AS quality-source

COPY pyproject.toml ./
COPY src ./src
COPY tests ./tests

FROM quality-source AS lint

RUN python -m ruff check src tests

FROM lint AS format-check

RUN python -m ruff format --check src tests

FROM format-check AS type-check

RUN python -m mypy src

FROM type-check AS test

ARG TEST_RUN_ID=local
RUN echo "Test run: ${TEST_RUN_ID}" \
    && python -m pytest --verbose

FROM runtime-dependencies AS runtime

RUN groupadd --gid 1000 airstage2mqtt \
    && useradd --uid 1000 --gid 1000 --create-home --home-dir /home/airstage2mqtt airstage2mqtt \
    && mkdir -p /config /data \
    && chown -R 1000:1000 /config /data

COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir --no-deps .

ARG A2M_VERSION=0.1.0
ENV A2M_VERSION=${A2M_VERSION}

USER 1000:1000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-m", "airstage2mqtt", "healthcheck"]

ENTRYPOINT ["python", "-m", "airstage2mqtt"]
CMD ["run"]

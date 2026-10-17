FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Only what the wheel target ships: citeindex/ + README.md (see pyproject.toml).
COPY pyproject.toml README.md ./
COPY citeindex ./citeindex
RUN pip install .

# Non-root runtime user.
RUN useradd --create-home --shell /usr/sbin/nologin appuser
USER appuser

# Mount a host directory at /data; corpus outputs land in /data/corpus.
WORKDIR /data
ENTRYPOINT ["citeindex"]

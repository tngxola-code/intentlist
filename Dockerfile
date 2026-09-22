FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN useradd --create-home --uid 10001 app
COPY pyproject.toml README.md alembic.ini ./
COPY src ./src
COPY migrations ./migrations
COPY config ./config
RUN pip install -e . && mkdir -p /app/data && chown -R app /app/data
USER app
EXPOSE 8000
CMD ["intentlist", "--help"]

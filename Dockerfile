# Stage 1: install dependencies
FROM python:3.13.1-slim AS builder
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install .

# Stage 2: minimal runtime image
FROM python:3.13.1-slim
RUN adduser --disabled-password --gecos "" appuser
COPY --from=builder /install /usr/local
COPY --chmod=755 entrypoint.sh /entrypoint.sh
EXPOSE 8091
USER appuser
ENTRYPOINT ["/entrypoint.sh"]
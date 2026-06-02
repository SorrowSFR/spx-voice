# Multi-stage Dockerfile
# Stage 1: Builder - Install Python dependencies
FROM python:3.12-slim AS builder

WORKDIR /app

# Copy and install requirements
COPY api/requirements.txt .

# Install dependencies to user directory for easy copying
RUN pip install --user --no-cache-dir -r requirements.txt && \
    # Clean up pip cache after installation
    rm -rf /root/.cache/pip

# Strip cache files, test/example dirs, and type stubs from installed packages
RUN find /root/.local -type f -name '*.pyc' -delete && \
    find /root/.local -type d -name '__pycache__' -prune -exec rm -rf {} + && \
    find /root/.local -type f -name '*.pyo' -delete && \
    find /root/.local -type d \( -name tests -o -name test -o -name examples \) -prune -exec rm -rf {} + && \
    find /root/.local -name '*.pyi' -delete

# Stage 2: Node deps for ts_validator (built with full node:22-slim, only
# node_modules is copied into the runner).
FROM node:22-slim AS ts-deps
WORKDIR /ts_validator
COPY api/mcp_server/ts_validator/package*.json ./
RUN npm ci --omit=dev && npm cache clean --force

# Stage 3: Static ffmpeg binary (avoids apt ffmpeg pulling mesa/libllvm for
# hardware acceleration we don't use server-side).
FROM debian:trixie-slim AS ffmpeg-static
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates xz-utils \
    && curl -fsSL -o /tmp/ffmpeg.tar.xz https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz \
    && mkdir -p /tmp/ffmpeg \
    && tar -xJf /tmp/ffmpeg.tar.xz -C /tmp/ffmpeg --strip-components=1 \
    && mv /tmp/ffmpeg/ffmpeg /tmp/ffmpeg/ffprobe /usr/local/bin/ \
    && chmod +x /usr/local/bin/ffmpeg /usr/local/bin/ffprobe

# Stage 4: Runtime - Minimal image with only runtime dependencies
FROM python:3.12-slim AS runner

WORKDIR /app

# Static ffmpeg + ffprobe (used by audio_converter, audio_file_cache, etc.)
COPY --from=ffmpeg-static /usr/local/bin/ffmpeg /usr/local/bin/ffmpeg
COPY --from=ffmpeg-static /usr/local/bin/ffprobe /usr/local/bin/ffprobe

# Node.js 22 binary only (ts_validator subprocess needs node >=22.6 for
# native TypeScript stripping; see api/mcp_server/ts_bridge.py). python:3.12-slim
# already provides libstdc++6, libgcc-s1, and ca-certificates that node needs.
COPY --from=node:22-slim /usr/local/bin/node /usr/local/bin/node

# Copy Python packages from builder stage
COPY --from=builder /root/.local /root/.local

# Make sure scripts in .local are available
ENV PATH=/root/.local/bin:$PATH

# Set Python to not generate .pyc files in runtime
ENV PYTHONDONTWRITEBYTECODE=1
# Unbuffered output for better container logging
ENV PYTHONUNBUFFERED=1

# Copy application code
COPY ./api ./api
COPY ./scripts/start_services_docker.sh ./scripts/start_services_docker.sh
RUN sed -i 's/\r$//' ./scripts/start_services_docker.sh && \
    chmod +x ./scripts/start_services_docker.sh && \
    bash -n ./scripts/start_services_docker.sh

# ts_validator Node deps (built in ts-deps stage with full node:22-slim image).
# The validator runs as a short-lived subprocess from api/mcp_server/ts_bridge.py.
COPY --from=ts-deps /ts_validator/node_modules ./api/mcp_server/ts_validator/node_modules

# Product documentation read at runtime by the MCP docs tools.
COPY ./docs ./docs

ENV PYTHONPATH=/app

# Disable file logging in Docker - logs go to stdout for docker logs
ENV LOG_TO_FILE=false

# Expose the port FastAPI will run on
EXPOSE 8000

# Run the service supervisor. Use bash explicitly so startup does not depend on
# copied file mode behavior from the host filesystem.
CMD ["bash", "./scripts/start_services_docker.sh"]

# Public demo of R2 for Nebius Serverless Endpoints (or any container host).
# It runs the locked-down "demo" profile: memory, skills and web search only. No files, shell, apps,
# screen or browser tools exist in this build, so there is nothing on the host for a visitor to reach.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    R2_PROFILE=demo \
    R2_HOST=0.0.0.0 \
    R2_PORT=8080 \
    R2_DATA_DIR=/data \
    R2_CLAUDE=0 \
    R2_BRAIN=openai \
    R2_OPENAI_BASE_URL=

WORKDIR /app
COPY requirements-demo.txt .
RUN pip install --no-cache-dir -r requirements-demo.txt

COPY r2 ./r2
COPY skills ./skills
RUN useradd --create-home --uid 10001 r2 && mkdir -p /data /app/data && chown -R r2:r2 /app /data
USER r2
EXPOSE 8080

# Secrets come from the platform, never from the image:
#   R2_OPENAI_API_KEY   Nebius Token Factory key
#   R2_OPENAI_MODEL     e.g. the Nemotron Super model id; optional R2_FAST_MODEL and R2_DEEP_MODEL
#   R2_DEMO_CODE        the access code printed in the README testing instructions
#   TAVILY_API_KEY      optional, for Tavily search
CMD ["python", "-c", "import sys; from r2.cli import main; sys.exit(main(['serve']))"]

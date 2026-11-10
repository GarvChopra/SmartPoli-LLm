#!/usr/bin/env bash
# UNTESTED SKETCH. The default OpenShell sandbox image is a minimal Ubuntu with no Python and no R2,
# so this needs a sandbox image that contains the r2-demo image's contents; see the OpenShell docs on
# sandbox images. The policy file it uses is the part that is written against the documented schema.
#
# Run R2's demo build inside an NVIDIA OpenShell sandbox (Linux, macOS, or Windows via WSL 2).
# Requires the openshell CLI: curl -LsSf https://raw.githubusercontent.com/NVIDIA/OpenShell/main/install.sh | sh
# Secrets are read from your environment (never written to this repo):
#   R2_OPENAI_API_KEY  R2_OPENAI_MODEL  R2_DEMO_CODE  [TAVILY_API_KEY]
set -euo pipefail
cd "$(dirname "$0")/.."

: "${R2_OPENAI_API_KEY:?set R2_OPENAI_API_KEY (Nebius Token Factory key)}"
: "${R2_OPENAI_MODEL:?set R2_OPENAI_MODEL (a Nemotron model id)}"
: "${R2_DEMO_CODE:?set R2_DEMO_CODE (the access code for the page)}"

docker build -t r2-demo .
openshell sandbox create --name r2 --policy sandbox/openshell-policy.yaml -- \
  env R2_PROFILE=demo R2_HOST=0.0.0.0 R2_PORT=8080 R2_DATA_DIR=/data \
      R2_OPENAI_API_KEY="$R2_OPENAI_API_KEY" R2_OPENAI_MODEL="$R2_OPENAI_MODEL" \
      R2_DEMO_CODE="$R2_DEMO_CODE" TAVILY_API_KEY="${TAVILY_API_KEY:-}" \
      python -c "import sys; from r2.cli import main; sys.exit(main(['serve']))"

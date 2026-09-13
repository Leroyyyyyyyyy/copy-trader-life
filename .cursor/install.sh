#!/usr/bin/env bash
# Idempotent Cloud Agent bootstrap for Tradelife.
# Installs the Python toolchain and project dependencies into a local .venv.
set -euo pipefail

cd "$(dirname "$0")/.."

# The default image ships python3.12 but not the venv/pip stdlib extras.
if ! python3 -c "import ensurepip" >/dev/null 2>&1; then
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    python3-venv python3-pip
fi

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi

./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -r requirements.txt

echo "Tradelife environment ready. Activate with: source .venv/bin/activate"

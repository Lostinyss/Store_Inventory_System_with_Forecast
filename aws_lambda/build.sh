#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
staging="$(mktemp -d)"
trap 'rm -rf "$staging"' EXIT

"${UV_BIN:-$HOME/.local/bin/uv}" pip install --python 3.13 \
  --target "$staging" -r "$script_dir/requirements.txt"
cp "$script_dir/handler.py" "$staging/handler.py"
STAGING_DIR="$staging" OUTPUT_ZIP="$script_dir/sales-import-lambda.zip" python3 - <<'PY'
import os
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

root = Path(os.environ["STAGING_DIR"])
with ZipFile(os.environ["OUTPUT_ZIP"], "w", ZIP_DEFLATED) as archive:
    for path in root.rglob("*"):
        if path.is_file():
            archive.write(path, path.relative_to(root))
print(os.environ["OUTPUT_ZIP"])
PY

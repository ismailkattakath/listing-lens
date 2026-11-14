#!/usr/bin/env bash
# The site source plug-ins' own tests, kept with them in DATA (the site file's [pipeline.plugins]): run on the
# HOST by `ship` (flake.nix shipLocalChecks), against this checkout's llens and its tests/conftest.py. No bytecode
# or pytest cache lands in DATA (Drive). No plug-in tests collected = pytest exits 5 = the check refuses.
set -euo pipefail
: "${LLENS_DATA:?set LLENS_DATA (the DATA root)}"
export PYTHONDONTWRITEBYTECODE=1
exec pytest -q -p no:cacheprovider --rootdir "$LLENS_DATA/plugins" "$LLENS_DATA/plugins"

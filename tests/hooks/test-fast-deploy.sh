#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)

grep -q 'azd hooks run postprovision' "$ROOT/deploy.sh"
grep -q -- '--code-only' "$ROOT/deploy.sh"
grep -q 'azd hooks run postprovision' "$ROOT/deploy.ps1"
grep -q 'CodeOnly' "$ROOT/deploy.ps1"

for hook in "$ROOT/hooks/postprovision.sh" "$ROOT/hooks/postprovision.ps1"; do
  grep -q 'OMNIVEC_BUILD_CONCURRENCY' "$hook"
  grep -q 'src-' "$hook"
  grep -q 'docgrok-pipeline-worker' "$hook"
  grep -q 'omnivec-onelake-iceberg-watcher' "$hook"
  grep -q 'mcp_servers.*cosmos' "$hook"
done

if grep -q 'Build-Image -Name "omnivec-api".*-Context \$RootDir' "$ROOT/hooks/postprovision.ps1"; then
  echo "FAIL API source build still uploads repository root" >&2
  exit 1
fi
if grep -q 'build_image "omnivec-api".*"\$ROOT_DIR"' "$ROOT/hooks/postprovision.sh"; then
  echo "FAIL API source build still uploads repository root" >&2
  exit 1
fi

echo "13 fast deployment contracts passed"

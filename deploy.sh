#!/bin/sh
# Compatibility entrypoint: keep installation on the maintained azd path.
set -eu

if ! command -v azd >/dev/null 2>&1; then
  printf 'Azure Developer CLI (azd) is required. See https://aka.ms/azd-install\n' >&2
  exit 1
fi

cd "$(dirname "$0")"
if [ -f terraform/terraform.tfstate ] || [ -d terraform/terraform.tfstate.d ]; then
  printf 'Terraform state detected. Review migration to azd before using this wrapper; refusing to provision a second stack.\n' >&2
  exit 1
fi
if [ "${1:-}" = "--code-only" ]; then
  shift
  printf 'Deploying application images and Helm release only (skipping Bicep provisioning).\n'
  printf 'Use this only after the Azure environment has been provisioned successfully at least once.\n'
  exec azd hooks run postprovision "$@"
fi
printf 'Deploying with azd up (azure.yaml + Bicep + platform hooks).\n'
printf 'This entrypoint does not apply terraform/. Existing Terraform installations require a reviewed migration; do not deploy both stacks over the same resources.\n'
exec azd up "$@"

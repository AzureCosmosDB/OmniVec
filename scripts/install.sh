#!/usr/bin/env bash
# OmniVec CLI installer (macOS + Linux)
#
# Usage:
#   curl -fsSL https://github.com/AzureCosmosDB/OmniVec/raw/main/scripts/install.sh | bash
#   curl -fsSL https://github.com/AzureCosmosDB/OmniVec/raw/main/scripts/install.sh | bash -s -- v1.1.3
#
# Env vars:
#   OMNIVEC_VERSION   pin a specific version (default: latest release)
#   OMNIVEC_INSTALL_DIR  install location (default: $HOME/.local/bin)
#
# What this does:
#   1. Detects OS (darwin/linux) + arch (amd64/arm64)
#   2. Downloads the matching omnivec binary from the GitHub release
#   3. Strips the macOS quarantine xattr (so Gatekeeper doesn't block it)
#   4. chmod +x and moves to INSTALL_DIR
#   5. Prints PATH guidance if INSTALL_DIR isn't on $PATH

set -euo pipefail

REPO="AzureCosmosDB/OmniVec"
VERSION="${1:-${OMNIVEC_VERSION:-latest}}"
INSTALL_DIR="${OMNIVEC_INSTALL_DIR:-$HOME/.local/bin}"

bold()  { printf "\033[1m%s\033[0m\n" "$*"; }
green() { printf "\033[32m%s\033[0m\n" "$*"; }
red()   { printf "\033[31m%s\033[0m\n" "$*" >&2; }
warn()  { printf "\033[33m%s\033[0m\n" "$*" >&2; }

# ---------- detect OS/arch ----------
OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
case "$OS" in
  darwin) OS="darwin" ;;
  linux)  OS="linux" ;;
  *) red "Unsupported OS: $OS. Use the Windows installer or download manually."; exit 1 ;;
esac

ARCH_RAW="$(uname -m)"
case "$ARCH_RAW" in
  x86_64|amd64) ARCH="amd64" ;;
  arm64|aarch64) ARCH="arm64" ;;
  *) red "Unsupported architecture: $ARCH_RAW"; exit 1 ;;
esac

# ---------- resolve version ----------
if [ "$VERSION" = "latest" ]; then
  VERSION="$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" \
    | grep '"tag_name"' | head -1 | sed -E 's/.*"tag_name": *"([^"]+)".*/\1/')"
  if [ -z "$VERSION" ]; then
    red "Could not resolve latest release tag. Set OMNIVEC_VERSION explicitly."
    exit 1
  fi
fi

ASSET="omnivec-${VERSION}-${OS}-${ARCH}"
URL="https://github.com/${REPO}/releases/download/${VERSION}/${ASSET}"

bold "Installing OmniVec CLI ${VERSION} for ${OS}/${ARCH}"
echo "  source : ${URL}"
echo "  target : ${INSTALL_DIR}/omnivec"
echo

# ---------- download ----------
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

if ! curl -fL --progress-bar -o "${TMP}/omnivec" "$URL"; then
  red "Download failed. Asset may not exist for this platform: $ASSET"
  exit 1
fi

# ---------- strip macOS quarantine ----------
if [ "$OS" = "darwin" ] && command -v xattr >/dev/null 2>&1; then
  xattr -dr com.apple.quarantine "${TMP}/omnivec" 2>/dev/null || true
fi

chmod +x "${TMP}/omnivec"

# ---------- install ----------
mkdir -p "$INSTALL_DIR"
mv "${TMP}/omnivec" "${INSTALL_DIR}/omnivec"

green "Installed: ${INSTALL_DIR}/omnivec"

# ---------- PATH guidance ----------
case ":$PATH:" in
  *":${INSTALL_DIR}:"*) ;;
  *)
    echo
    warn "${INSTALL_DIR} is not on your PATH."
    echo "  Add this to your shell profile (e.g. ~/.zshrc, ~/.bashrc):"
    echo
    echo "    export PATH=\"${INSTALL_DIR}:\$PATH\""
    echo
    ;;
esac

echo
"${INSTALL_DIR}/omnivec" --help >/dev/null 2>&1 && green "Verified — try: omnivec --help"

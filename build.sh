#!/usr/bin/env bash
# build.sh — validate and pack the Apple Notes MCP desktop extension
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${SCRIPT_DIR}/dist"

echo "=== Apple Notes MCP — build ==="

# Check for mcpb CLI
if ! command -v mcpb &>/dev/null; then
    echo "Installing mcpb CLI…"
    npm install -g @anthropic-ai/mcpb
fi

# Validate
echo ""
echo "Validating manifest…"
mcpb validate "${SCRIPT_DIR}/manifest.json"
echo "✓ Manifest valid."

# Pack
echo ""
mkdir -p "${OUT}"
VERSION=$(grep '^version' "${SCRIPT_DIR}/pyproject.toml" | head -1 | sed 's/version = "\(.*\)"/\1/')
STABLE="${OUT}/apple-notes.mcpb"
VERSIONED="${OUT}/apple-notes-${VERSION}.mcpb"
mcpb pack "${SCRIPT_DIR}" "${STABLE}"

# Make the version visible in Finder: Spotlight comment, kMDItemVersion,
# and a versioned filename copy (the bulletproof display that survives
# copy/upload/quarantine stripping the xattrs).
osascript -e "tell application \"Finder\" to set comment of (POSIX file \"${STABLE}\" as alias) to \"Apple Notes MCP — v${VERSION}\"" >/dev/null 2>&1 || true
xattr -w "com.apple.metadata:kMDItemVersion" "${VERSION}" "${STABLE}" 2>/dev/null || true
mdimport "${STABLE}" 2>/dev/null || true
cp -f "${STABLE}" "${VERSIONED}"
osascript -e "tell application \"Finder\" to set comment of (POSIX file \"${VERSIONED}\" as alias) to \"Apple Notes MCP — v${VERSION}\"" >/dev/null 2>&1 || true
xattr -w "com.apple.metadata:kMDItemVersion" "${VERSION}" "${VERSIONED}" 2>/dev/null || true
mdimport "${VERSIONED}" 2>/dev/null || true

echo ""
echo "✓ Built:"
echo "    ${STABLE}        (stable name — drag-install target)"
echo "    ${VERSIONED}    (versioned name — visible version in filename)"
echo ""
echo "To install: double-click the .mcpb file, or drag it into Claude Desktop."
echo ""
echo "ℹ  Writes need Notes.app running; macOS prompts for Automation permission on first use."
echo "ℹ  For millisecond reads, grant Full Disk Access to 'uv' — see README."

#!/bin/bash
# Preview script - generates static previews of logo-based assets
# Run: bash preview-logo.sh
# Output: previews/favicon.svg, previews/og-image.svg, previews/og-image-api.svg
#
# This script regenerates public/favicon.svg and public/og-image.svg from
# public/logo.svg (via generate-assets.sh), then ensures the dynamic OG image
# preview is generated (starting the server temporarily if needed).

set -e

# Regenerate the actual assets + static previews from the single source of truth (logo.svg)
bash generate-assets.sh

# Ensure the dynamic OG image preview exists (start server temporarily if needed)
if [ ! -f previews/og-image-api.svg ] || ! curl -sf "http://localhost:8080/api/og-image?from=group_0&to=group_10&mode=short" -o previews/og-image-api.svg 2>/dev/null; then
    echo ""
    echo "⚠ Server not running. Starting temporarily to generate dynamic OG preview..."
    python3 server.py &
    SERVER_PID=$!
    # Wait for server to be ready (up to 30s)
    for i in $(seq 1 30); do
        if curl -sf "http://localhost:8080/api/health" >/dev/null 2>&1; then
            break
        fi
        sleep 1
    done
    if curl -sf "http://localhost:8080/api/og-image?from=group_0&to=group_10&mode=short" -o previews/og-image-api.svg 2>/dev/null; then
        echo "✓ previews/og-image-api.svg"
    else
        echo "✗ Failed to generate previews/og-image-api.svg"
    fi
    kill $SERVER_PID 2>/dev/null
    wait $SERVER_PID 2>/dev/null
fi

echo ""
echo "=== Done ==="
echo "Files in previews/:"
ls -la previews/
echo ""
echo "Open in browser to preview:"
echo "  previews/favicon.svg"
echo "  previews/og-image.svg"
echo "  previews/og-image-api.svg"
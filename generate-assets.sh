#!/bin/bash
# Generate favicon.svg and og-image.svg from the single source of truth: public/logo.svg
# Run: bash generate-assets.sh
# This ensures that editing public/logo.svg propagates to:
#   - public/favicon.svg (favicon)
#   - public/og-image.svg (static OG image)
#   - dynamic OG image (server.py already injects logo.svg at runtime)

set -e

LOGO="public/logo.svg"
FAVICON="public/favicon.svg"
OG_IMAGE="public/og-image.svg"

if [ ! -f "$LOGO" ]; then
    echo "✗ Error: $LOGO not found"
    exit 1
fi

echo "=== Generating assets from $LOGO ==="

# Extract and clean the inner content of logo.svg (strip the <svg> wrapper
# and remove Inkscape/editor metadata that is not needed in production assets)
LOGO_INNER=$(python3 -c "
import re, sys
with open('$LOGO', 'r', encoding='utf-8') as f:
    content = f.read()
# Remove XML declaration (must be first)
content = re.sub(r'<\?xml[^>]*\?>', '', content, count=1)
# Remove the <svg ...> opening tag and </svg> closing tag
content = re.sub(r'<svg[^>]*>', '', content, count=1)
content = re.sub(r'</svg>', '', content)
# Remove Inkscape/Sodipodi editor metadata (namedview, grid, empty defs)
content = re.sub(r'<sodipodi:namedview.*?</sodipodi:namedview>', '', content, flags=re.S)
content = re.sub(r'<inkscape:grid[^>]*/>', '', content)
# Remove any <defs>...</defs> or self-closing <defs ... /> (with or without attributes)
content = re.sub(r'<defs[^>]*>\s*</defs>', '', content)
content = re.sub(r'<defs[^>]*/>', '', content)
# Remove id= attributes (not needed, keeps output clean)
# NOTE: style= attributes are KEPT because they may override fill colors
# (e.g. the bus headlights use style="fill:#ff5555" to override the base fill)
content = re.sub(r'\s+id=\"[^\"]*\"', '', content)
# Collapse multiple blank lines
content = re.sub(r'\n\s*\n+', '\n', content)
print(content.strip())
")

# 1. Generate favicon.svg - embed logo inline (no external <image> reference)
#    This works offline and in all browsers (some don't load external images in favicons)
cat > "$FAVICON" <<EOF
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
$LOGO_INNER
</svg>
EOF
echo "✓ $FAVICON"

# 2. Generate og-image.svg - static OG image with logo embedded inline
#    The logo is placed in the same position/scale as the dynamic OG image
cat > "$OG_IMAGE" <<EOF
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 630">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#0d2137"/>
      <stop offset="100%" stop-color="#1a3a52"/>
    </linearGradient>
  </defs>
  <rect width="1200" height="630" fill="url(#bg)"/>

  <text x="80" y="77" font-family="Arial, Helvetica, sans-serif" font-size="32" font-weight="bold" fill="#5dade2">ZA ILE PRZEJADĘ?</text>
  <text x="80" y="155" font-family="Arial, Helvetica, sans-serif" font-size="55" font-weight="bold" fill="#ecf0f1">Kalkulator cen biletów</text>
  <text x="80" y="225" font-family="Arial, Helvetica, sans-serif" font-size="55" font-weight="bold" fill="#ecf0f1">MPK Kraków 2027</text>
  <line x1="80" y1="270" x2="240" y2="270" stroke="#5dade2" stroke-width="4"/>
  <text x="80" y="340" font-family="Arial, Helvetica, sans-serif" font-size="46" font-weight="normal" fill="#bdc3c7">Oblicz koszt przejazdu</text>
  <text x="80" y="395" font-family="Arial, Helvetica, sans-serif" font-size="46" font-weight="normal" fill="#bdc3c7">w nowym systemie biletów</text>

  <!-- Bus logo (from logo.svg, scaled) -->
  <g transform="translate(930,400) scale(2.5)">
$LOGO_INNER
  </g>
  <text x="1010" y="577" font-family="Arial, Helvetica, sans-serif" font-size="32" font-weight="bold" fill="#7f8c8d" text-anchor="middle">zaileprzeja.de</text>
</svg>
EOF
echo "✓ $OG_IMAGE"

echo ""
echo "=== Generating previews ==="

mkdir -p previews

# 1. Favicon preview (copy generated favicon.svg)
cp public/favicon.svg previews/favicon.svg
echo "✓ previews/favicon.svg"

# 2. Static OG image preview
cp public/og-image.svg previews/og-image.svg
echo "✓ previews/og-image.svg"

# 3. Dynamic OG image preview (from running server, if available)
if curl -sf "http://localhost:8080/api/og-image?from=group_0&to=group_10&mode=short" -o previews/og-image-api.svg 2>/dev/null; then
    echo "✓ previews/og-image-api.svg"
else
    echo "⚠ Server not running - skipping dynamic OG preview (start server and run preview-logo.sh)"
fi

echo ""
echo "=== Done ==="
echo "Assets regenerated from $LOGO"
echo "  $FAVICON"
echo "  $OG_IMAGE"
echo "Previews in previews/:"
ls -la previews/
echo ""
echo "Note: The dynamic OG image (/api/og-image) already reads logo.svg at runtime,"
echo "so it updates automatically without running this script."

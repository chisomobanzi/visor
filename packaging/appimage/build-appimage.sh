#!/usr/bin/env bash
# Build Visor-x86_64.AppImage with python-appimage.
#
#   ./packaging/appimage/build-appimage.sh            # build
#   ./packaging/appimage/build-appimage.sh --install  # build + install to ~/Applications
#
# Produces packaging/appimage/dist/Visor-x86_64.AppImage. Needs network
# access (PyPI wheels + a relocatable CPython runtime) and a couple of GB
# of scratch space. FUSE is not required to build.
#
# adb and ffmpeg stay external: the AppImage uses whatever is on the host
# PATH, exactly like a pip install does.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
DIST="$HERE/dist"
PYVER="${VISOR_APPIMAGE_PYTHON:-3.12}"

# recipe directory: the .desktop Name= field (Visor) names the final AppImage
RECIPE="$DIST/visor"
rm -rf "$RECIPE"
mkdir -p "$RECIPE"

cp "$HERE/visor.desktop" "$RECIPE/"
# Keep the launcher icon in step with the in-app one; the committed PNG is
# the fallback when no SVG rasterizer is installed.
if command -v rsvg-convert > /dev/null 2>&1; then
    rsvg-convert -w 256 -h 256 "$ROOT/visor/ui/static/icon.svg" -o "$RECIPE/visor.png"
else
    cp "$HERE/visor.png" "$RECIPE/"
fi

# {{ python-executable }} is substituted by python-appimage at build time.
# -P: without it the launch directory goes first on sys.path, so starting the
# AppImage from anywhere containing a visor/ folder — a checkout, say — would
# silently run that code instead of the bundled package.
# Bare launch (double-click from the desktop) opens the dashboard window;
# arguments pass straight through:  ./Visor-x86_64.AppImage devices
cat > "$RECIPE/entrypoint.sh" << 'EOF'
{{ python-executable }} -P -m visor "${@:-app}"
EOF

# no spaces: python-appimage word-splits requirement lines when invoking pip
printf 'visor-dev[app,agent,voice]@file://%s\n' "$ROOT" > "$RECIPE/requirements.txt"

if command -v uvx > /dev/null 2>&1; then
    BUILDER=(uvx python-appimage)
else
    python3 -m pip install --user --upgrade python-appimage
    BUILDER=(python3 -m python_appimage)
fi

# pip keys its wheel cache on name+version, and our version only moves at
# release time — so without this a rebuild between releases quietly reuses the
# wheel from the last one and bundles yesterday's code while the source tree
# looks correct. Only ours: the third-party wheels are worth caching.
CACHE="${PIP_CACHE_DIR:-$HOME/.cache/pip}/wheels"
if [ -d "$CACHE" ]; then
    find "$CACHE" -name 'visor_dev-*.whl' -delete
fi

cd "$DIST"
"${BUILDER[@]}" build app --python-version "$PYVER" "$RECIPE"

BUILT="$(ls "$DIST"/Visor-*.AppImage 2>/dev/null | head -1)"
[ -n "$BUILT" ] || { echo "ERROR: python-appimage produced no AppImage" >&2; exit 1; }
chmod +x "$BUILT"

# Smoke test: the bundle has to actually run, and it has to be running the
# code in this tree rather than a stale wheel out of pip's cache — the wheel
# purge above prevents the known cause, this catches the next one.
echo "==> smoke test"
"$BUILT" --help > /dev/null
PROBE="$(mktemp -d)"
trap 'rm -rf "$PROBE"' EXIT
( cd "$PROBE" && "$BUILT" --appimage-extract \
    'opt/python*/lib/python*/site-packages/visor/ui/*' > /dev/null 2>&1 ) || true
BUNDLED="$(find "$PROBE/squashfs-root" -path '*/visor/ui/server.py' | head -1)"
for f in ui/server.py ui/static/index.html; do
    if ! cmp -s "${BUNDLED%/ui/server.py}/$f" "$ROOT/visor/$f"; then
        echo "ERROR: bundled visor/$f differs from the source tree —" >&2
        echo "the build picked up a stale wheel." >&2
        exit 1
    fi
done
echo "bundle matches the source tree"
ls -lh "$BUILT"

if [ "${1:-}" = "--install" ]; then
    echo "==> installing to ~/Applications + desktop entry"
    APPS="$HOME/Applications"
    ICONS="$HOME/.local/share/icons/hicolor/256x256/apps"
    mkdir -p "$APPS" "$HOME/.local/share/applications" "$ICONS"
    # Copy + rename so an upgrade works even while Visor is running
    # (overwriting an executing binary fails with "text file busy").
    cp "$BUILT" "$APPS/.Visor.AppImage.new"
    chmod +x "$APPS/.Visor.AppImage.new"
    mv -f "$APPS/.Visor.AppImage.new" "$APPS/Visor.AppImage"
    cp "$RECIPE/visor.png" "$ICONS/visor.png"
    sed "s|^Exec=.*|Exec=$APPS/Visor.AppImage|" "$HERE/visor.desktop" \
        > "$HOME/.local/share/applications/visor.desktop"
    command -v update-desktop-database > /dev/null &&
        update-desktop-database "$HOME/.local/share/applications" || true
    command -v gtk-update-icon-cache > /dev/null &&
        gtk-update-icon-cache -t "$HOME/.local/share/icons/hicolor" 2> /dev/null || true
    echo "installed: $APPS/Visor.AppImage (search for 'Visor' in your app launcher)"
fi

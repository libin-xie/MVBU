#!/bin/sh
# Registers the MVBU helper (MultiValue Basic Utility) with Chrome, Chromium, Edge and Brave on macOS and Linux.
# It runs install_chrome_host.py, which also picks the Python that has uopy.
#   ./install-chrome-host.sh                     use the first Python that has uopy
#   ./install-chrome-host.sh /path/to/python3    use this Python (same as --python /path/to/python3)
#   ./install-chrome-host.sh --extension-id ID   also allow another extension ID
#   ./install-chrome-host.sh --uninstall         remove the registration
HERE=$(cd "$(dirname "$0")" && pwd)
case "$1" in
  ""|-*) ;;
  *) first="$1"; shift; set -- --python "$first" "$@" ;;
esac
for c in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3 python; do
  p=$(command -v "$c" 2>/dev/null) || continue
  if "$p" -c "import sys; sys.exit(sys.version_info < (3, 8))" 2>/dev/null; then
    exec "$p" "$HERE/install_chrome_host.py" "$@"
  fi
done
echo "Python 3.8 or later was not found. Install it, then run: python3 -m pip install uopy"
exit 1

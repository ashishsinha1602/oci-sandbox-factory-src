#!/usr/bin/env bash
# Find (or install) a Python that can import the OCI SDK on a DevOps build
# runner, and export it as $PY. The runner may sit behind a proxy that pip
# cannot reach while the OCI CLI (which bundles the SDK) is already installed;
# try the cheap options first and print what was found.
set -u
try() { "$1" -c "import oci" >/dev/null 2>&1 && PY="$1" && return 0; return 1; }
PY=""
try python3 || true
if [ -z "$PY" ]; then
  # the OCI CLI ships its own interpreter with the SDK
  for c in "$(command -v oci 2>/dev/null)" /usr/lib/oracle/*/oci-cli/bin/oci "$HOME/bin/oci" "$HOME/lib/oracle-cli/bin/oci"; do
    [ -n "$c" ] && [ -e "$c" ] || continue
    d="$(dirname "$(readlink -f "$c")")"
    for p in "$d/python" "$d/python3"; do [ -x "$p" ] && try "$p" && break 2; done
  done
fi
if [ -z "$PY" ]; then
  echo "proxy settings: $(env | grep -i proxy | tr '\n' ' ')"
  env -u HTTPS_PROXY -u https_proxy -u HTTP_PROXY -u http_proxy python3 -m pip install --quiet --user oci && try python3 || true
fi
if [ -z "$PY" ]; then
  python3 -m pip install --quiet --user oci && try python3 || true
fi
if [ -z "$PY" ]; then
  echo "no Python with the OCI SDK could be found or installed on this runner" >&2
  exit 1
fi
echo "using $PY ($("$PY" -c 'import oci; print(oci.__version__)'))"
export PY

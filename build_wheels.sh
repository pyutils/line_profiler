#!/usr/bin/env bash
set -euo pipefail

__doc__="
Runs cibuildwheel to create linux binary wheels.

Requirements:
    pip install cibuildwheel

SeeAlso:
    pyproject.toml
"

if ! command -v docker >/dev/null 2>&1 ; then
    echo "Missing requirement: docker. Please install docker before running build_wheels.sh"
    exit 1
fi
if ! command -v cibuildwheel >/dev/null 2>&1 ; then
    echo "The cibuildwheel module is not installed. Please pip install cibuildwheel before running build_wheels.sh"
    exit 1
fi

LOCAL_CP_VERSION=$(python3 -c "import sys; print('cp' + ''.join(map(str, sys.version_info[0:2])))")
echo "LOCAL_CP_VERSION = $LOCAL_CP_VERSION"

# Build for only the current version of Python.
export CIBW_BUILD="${CIBW_BUILD:-${LOCAL_CP_VERSION}-*}"
echo "CIBW_BUILD = $CIBW_BUILD"

# Recreate the output directory so stale wheels cannot satisfy
# post-build validation or be mistaken for this build.
rm -rf wheelhouse
mkdir -p wheelhouse

cibuildwheel --config-file pyproject.toml --platform linux --archs x86_64

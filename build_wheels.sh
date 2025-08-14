#!/usr/bin/env bash
__doc__="
Runs cibuildwheel to create linux binary wheels.

Requirements:
    pip install cibuildwheel

SeeAlso:
    pyproject.toml
"

if ! which docker ; then
    echo "Missing requirement: docker. Please install docker before running build_wheels.sh"
    exit 1
fi
if ! which cibuildwheel ; then
    echo "The cibuildwheel module is not installed. Please pip install cibuildwheel before running build_wheels.sh"
    exit 1
fi


# Build ABI3 wheels
CIBW_CONFIG_SETTINGS="--build-option=--py-limited-api=cp38" CIBW_BUILD="cp38-*" cibuildwheel --config-file pyproject.toml --platform linux --archs x86_64

# Build version-pinned wheels
cibuildwheel --config-file pyproject.toml --platform linux --archs x86_64

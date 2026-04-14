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

LOCAL_CP_VERSION=$(python3 -c "import sys; print('cp' + ''.join(list(map(str, sys.version_info[0:2]))))")
echo "LOCAL_CP_VERSION = $LOCAL_CP_VERSION"

# Build for only the current version of Python
export CIBW_BUILD="${LOCAL_CP_VERSION}-*"


#pip wheel -w wheelhouse .
# python -m build --wheel -o wheelhouse  #  line_profiler: +COMMENT_IF(binpy)
cibuildwheel --config-file pyproject.toml --platform linux --archs x86_64  #  line_profiler: +UNCOMMENT_IF(binpy)

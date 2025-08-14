#!/bin/bash
echo "start clean"

rm -rf _skbuild
rm -rf _line_profiler.c
rm -rf *.so
rm -rf line_profiler/_line_profiler.c
rm -rf line_profiler/_line_profiler.cpp
rm -rf line_profiler/*.html
rm -rf line_profiler/*.so
rm -rf build
rm -rf line_profiler.egg-info
rm -rf dist
rm -rf mb_work
rm -rf wheelhouse
rm -rf pip-wheel-metadata
rm -rf htmlcov
rm -rf tests/htmlcov
rm -rf CMakeCache.txt
rm -rf CMakeTmp
rm -rf CMakeFiles
rm -rf tests/htmlcov

rm -rf demo_primes*
rm -rf docs/demo.py*
rm -rf docs/script_to_profile.py*
rm -rf tests/complex_example.py.lprof
rm -rf tests/complex_example.py.prof
rm -rf script_to_profile.py*


if [ -f "distutils.errors" ]; then
    rm distutils.errors || echo "skip rm"
fi

CLEAN_PYTHON='find . -regex ".*\(__pycache__\|\.py[co]\)" -delete || find . -iname *.pyc -delete || find . -iname *.pyo -delete'
bash -c "$CLEAN_PYTHON"

echo "finish clean"

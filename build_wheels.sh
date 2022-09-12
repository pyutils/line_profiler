#!/bin/bash
__doc__="""
SeeAlso:
    pyproject.toml
"""
#pip wheel -w wheelhouse .
# python -m build --wheel -o wheelhouse  #  line_profiler: +COMMENT_IF(binpy)
cibuildwheel --config-file pyproject.toml --platform linux --arch x86_64  #  line_profiler: +UNCOMMENT_IF(binpy)

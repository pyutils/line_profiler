line_profiler and kernprof
--------------------------

|Pypi| |ReadTheDocs| |Downloads| |CircleCI| |GithubActions| |Codecov|


This is the official ``line_profiler`` repository. The most recent version of
`line-profiler <https://pypi.org/project/line_profiler/>`_ on pypi points to
this repo.
The original `line_profiler <https://github.com/rkern/line_profiler/>`_ package
by `@rkern <https://github.com/rkern/>`_ is unmaintained.
This fork is the official continuation of the project.

+---------------+--------------------------------------------+
| Github        | https://github.com/pyutils/line_profiler   |
+---------------+--------------------------------------------+
| Pypi          | https://pypi.org/project/line_profiler     |
+---------------+--------------------------------------------+
| ReadTheDocs   | https://kernprof.readthedocs.io/en/latest/ |
+---------------+--------------------------------------------+

----


``line_profiler`` is a module for doing line-by-line profiling of functions.
Use it when function-level profiling identifies a slow function, but you need
to see which individual source lines account for the time. ``kernprof`` is the
command-line helper included with the package.

They are available under a `BSD license`_.

.. _BSD license: https://raw.githubusercontent.com/pyutils/line_profiler/master/LICENSE.txt

.. contents::


Installation
============

Releases of ``line_profiler`` can be installed using pip::

    $ pip install line_profiler

Current releases require Python 3.10 or newer. Older Python versions require
older ``line_profiler`` releases.

Installation while ensuring a compatible IPython version can also be installed
using pip::

    $ pip install line_profiler[ipython]

Source installs may require a C compiler. Git checkouts also require Cython.
Wheels are published for common platforms. If no wheel is available for your
platform or Python version, installation may build from source.


Quick Start
===========

The recommended way to use ``line_profiler`` is to import ``profile`` and enable
profiling with the ``LINE_PROFILE`` environment variable.

To profile a python script:

* Install line_profiler: ``pip install line_profiler``.

* In the relevant file(s), import ``profile`` and decorate function(s) you want
  to profile::

      from line_profiler import profile


      @profile
      def slow_function():
          ...

* Set the environment variable ``LINE_PROFILE=1`` and run your script as normal.
  When the script ends a summary of profile results, files written to disk, and
  instructions for inspecting details will be written to stdout.

For more details and a short tutorial see `Line Profiler Basic Usage <https://kernprof.readthedocs.io/en/latest/#line-profiler-basic-usage>`_.


Older ``kernprof`` workflow
===========================

The older ``kernprof -l`` workflow is still supported, but it is no longer the
main README quick start. See `Usage Notes and FAQ <docs/source/manual/legacy_readme.rst>`_
for the previous README material covering ``kernprof``, IPython, lower-level API
usage, related tools, and FAQ entries.

The short version is::

    $ kernprof -lv script_to_profile.py


Documentation
=============

* `Full documentation <https://kernprof.readthedocs.io/en/latest/>`_
* `Examples <https://kernprof.readthedocs.io/en/latest/manual/examples/>`_
* `Usage Notes and FAQ <docs/source/manual/legacy_readme.rst>`_
* `Changelog <CHANGELOG.rst>`_


Bugs and Such
=============

Bugs and pull requests can be submitted on GitHub_.

.. _GitHub: https://github.com/pyutils/line_profiler


.. |CircleCI| image:: https://circleci.com/gh/pyutils/line_profiler.svg?style=svg
    :target: https://circleci.com/gh/pyutils/line_profiler
.. |Travis| image:: https://img.shields.io/travis/pyutils/line_profiler/master.svg?label=Travis%20CI
   :target: https://travis-ci.org/pyutils/line_profiler?branch=master
.. |Appveyor| image:: https://ci.appveyor.com/api/projects/status/github/pyutils/line_profiler?branch=master&svg=True
   :target: https://ci.appveyor.com/project/pyutils/line_profiler/branch/master
.. |Codecov| image:: https://codecov.io/github/pyutils/line_profiler/badge.svg?branch=master&service=github
   :target: https://codecov.io/github/pyutils/line_profiler?branch=master
.. |Pypi| image:: https://img.shields.io/pypi/v/line_profiler.svg
   :target: https://pypi.python.org/pypi/line_profiler
.. |Downloads| image:: https://img.shields.io/pypi/dm/line_profiler.svg
   :target: https://pypistats.org/packages/line-profiler
.. |GithubActions| image:: https://github.com/pyutils/line_profiler/actions/workflows/tests.yml/badge.svg?branch=main
   :target: https://github.com/pyutils/line_profiler/actions?query=branch%3Amain
.. |ReadTheDocs| image:: https://readthedocs.org/projects/kernprof/badge/?version=latest
    :target: http://kernprof.readthedocs.io/en/latest/

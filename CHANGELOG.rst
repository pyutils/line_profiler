Changes
=======

4.1.2
~~~~
* ENH: Add support for Python 3.12 #246
* ENH: Add osx universal2 and arm64 wheels
* ENH: Fix issue with integer overflow on 32 bit systems

4.1.1
~~~~
* FIX: ``get_stats`` is no longer slowed down when profiling many code sections #236

4.1.0
~~~~
* FIX: skipzeros now checks for zero hits instead of zero time
* FIX: Fixed errors in Python 3.11 with duplicate functions.
* FIX: ``show_text`` now increases column sizes or switches to scientific notation to maintain alignment
* ENH: ``show_text`` now has new options: sort and summarize
* ENH: Added new CLI arguments ``-srm`` to ``line_profiler`` to control sorting, rich printing, and summary printing.
* ENH: New global ``profile`` function that can be enabled by ``--profile`` or ``LINE_PROFILE=1``.
* ENH: New auto-profile feature in ``kernprof`` that will profile all functions in specified modules.
* ENH: Kernprof now outputs instructions on how to view results.
* ENH: Added readthedocs integration: https://kernprof.readthedocs.io/en/latest/index.html

4.0.3
~~~~
* FIX: Stop requiring bleeding-edge Cython unless necesasry (for Python 3.12).  #206

4.0.2
~~~~~
* FIX: AttributeError on certain methods. #191

4.0.1
~~~~~
* FIX: Profiling classmethods works again. #183

4.0.0
~~~~~
* ENH: Python 3.11 is now supported.
* ENH: Profiling overhead is now drastically smaller, thanks to reimplementing almost all of the tracing callback in C++. You can expect to see reductions of between 0.3 and 1 microseconds per line hit, resulting in a speedup of up to 4x for codebases with many lines of Python that only do a little work per line.
* ENH: Added the ``-i <# of seconds>`` option to the ``kernprof`` script. This uses the threading module to output profiling data to the output file every n seconds, and is useful for long-running tasks that shouldn't be stopped in the middle of processing.
* CHANGE: Cython's native cythonize function is now used to compile the project, instead of scikit-build's convoluted process.
* CHANGE: Due to optimizations done while reimplementing the callback in C++, the profiler's code_map and last_time attributes now are indexed by a hash of the code block's bytecode and its line number. Any code that directly reads (and processes) or edits the code_map and/or last_time attributes will likely break.

3.5.2
~~~~~
* FIX: filepath test in is_ipython_kernel_cell for Windows #161
* ADD: setup.py now checks LINE_PROFILER_BUILD_METHOD to determine how to build binaries
* ADD: LineProfiler.add_function warns if an added function has a __wrapped__ attribute

3.5.1
~~~~~
* FIX: #19 line profiler now works on async functions again

3.5.0
~~~~~
* FIX: #109 kernprof fails to write to stdout if stdout was replaced
* FIX: Fixes max of an empty sequence error #118
* Make IPython optional
* FIX: #100 Exception raise ZeroDivisionError

3.4.0
~~~~~
* Drop support for Python <= 3.5.x
* FIX: #104 issue with new IPython kernels

3.3.1
~~~~~
* FIX: Fix bug where lines were not displayed in Jupyter>=6.0 via #93
* CHANGE: moving forward, new pypi releases will be signed with the GPG key 2A290272C174D28EA9CA48E9D7224DAF0347B114 for PyUtils-CI <openpyutils@gmail.com>. For reference, older versions were signed with either 262A1DF005BE5D2D5210237C85CD61514641325F or 1636DAF294BA22B89DBB354374F166CFA2F39C18.

3.3.0
~~~~~
* New CI for building wheels.

3.2.6
~~~~~
* FIX: Update MANIFEST.in to package pyproj.toml and missing pyx file
* CHANGE: Removed version experimental augmentation.

3.2.5
~~~~~
* FIX: Update MANIFEST.in to package nested c source files in the sdist

3.2.4
~~~~~
* FIX: Update MANIFEST.in to package nested CMakeLists.txt in the sdist

3.2.3
~~~~~
* FIX: Use ImportError instead of ModuleNotFoundError while 3.5 is being supported
* FIX: Add MANIFEST.in to package CMakeLists.txt in the sdist

3.2.2
~~~~~
* ENH: Added better error message when c-extension is not compiled.
* FIX: Kernprof no longer imports line_profiler to avoid side effects.

3.2.0
~~~~~
* Dropped 2.7 support, manylinux docker images no longer support 2.7
* ENH: Add command line option to specify time unit and skip displaying
  functions which have not been profiled.
* ENH: Unified versions of line_profiler and kernprof: kernprof version is now
  identical to line_profiler version.

3.1.0
~~~~~
* ENH: fix Python 3.9

3.0.2
~~~~~
* BUG: fix ``__version__`` attribute in Python 2 CLI.

3.0.1
~~~~~
* BUG: fix calling the package from the command line

3.0.0
~~~~~
* ENH: Fix Python 3.7
* ENH: Restructure into package

2.1
~~~
* ENH: Add support for Python 3.5 coroutines
* ENH: Documentation updates
* ENH: CI for most recent Python versions (3.5, 3.6, 3.6-dev, 3.7-dev, nightly)
* ENH: Add timer unit argument for output time granularity spec

2.0
~~~
* BUG: Added support for IPython 5.0+, removed support for IPython <=0.12

1.1
~~~
* BUG: Read source files as bytes.

1.0
~~~
* ENH: `kernprof.py` is now installed as `kernprof`.
* ENH: Python 3 support. Thanks to the long-suffering Mikhail Korobov for being
  patient.
* Dropped 2.6 as it was too annoying.
* ENH: The `stripzeros` and `add_module` options. Thanks to Erik Tollerud for
  contributing it.
* ENH: Support for IPython cell blocks. Thanks to Michael Forbes for adding
  this feature.
* ENH: Better warnings when building without Cython. Thanks to David Cournapeau
  for spotting this.

1.0b3
~~~~~

* ENH: Profile generators.
* BUG: Update for compatibility with newer versions of Cython. Thanks to Ondrej
  Certik for spotting the bug.
* BUG: Update IPython compatibility for 0.11+. Thanks to Yaroslav Halchenko and
  others for providing the updated imports.

1.0b2
~~~~~

* BUG: fixed line timing overflow on Windows.
* DOC: improved the README.

1.0b1
~~~~~

* Initial release.

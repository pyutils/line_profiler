"""
Line Profiler
=============

The line_profiler module for doing line-by-line profiling of functions

+---------------+--------------------------------------------+
| Github        | https://github.com/pyutils/line_profiler   |
+---------------+--------------------------------------------+
| Pypi          | https://pypi.org/project/line_profiler     |
+---------------+--------------------------------------------+
| ReadTheDocs   | https://kernprof.readthedocs.io/en/latest/ |
+---------------+--------------------------------------------+


Installation
============

Releases of :py:mod:`line_profiler` and :py:mod:`kernprof` can be installed
using pip

.. code:: bash

    pip install line_profiler


The package also provides extras for optional dependencies, which can be
installed via:


.. code:: bash

    pip install line_profiler[all]


Line Profiler Basic Usage
=========================

To demonstrate line profiling, we first need to generate a Python script to
profile. Write the following code to a file called ``demo_primes.py``.

.. code:: python

    from line_profiler import profile


    @profile
    def is_prime(n):
        '''
        Check if the number "n" is prime, with n > 1.

        Returns a boolean, True if n is prime.
        '''
        max_val = n ** 0.5
        stop = int(max_val + 1)
        for i in range(2, stop):
            if n % i == 0:
                return False
        return True


    @profile
    def find_primes(size):
        primes = []
        for n in range(size):
            flag = is_prime(n)
            if flag:
                primes.append(n)
        return primes


    @profile
    def main():
        print('start calculating')
        primes = find_primes(100000)
        print(f'done calculating. Found {len(primes)} primes.')


    if __name__ == '__main__':
        main()


In this script we explicitly import the ``profile`` function from
``line_profiler``, and then we decorate function of interest with ``@profile``.

By default nothing is profiled when running the script.

.. code:: bash

    python demo_primes.py


The output will be

.. code::

    start calculating
    done calculating. Found 9594 primes.


The quickest way to enable profiling is to set the environment variable
``LINE_PROFILE=1`` and running your script as normal.


.. code:: bash

    LINE_PROFILE=1 python demo_primes.py

This will output 3 files: profile_output.txt, profile_output_<timestamp>.txt,
and profile_output.lprof and stdout will look something like:


.. code::

    start calculating
    done calculating. Found 9594 primes.
    Timer unit: 1e-09 s

      0.65 seconds - demo_primes.py:4 - is_prime
      1.47 seconds - demo_primes.py:19 - find_primes
      1.51 seconds - demo_primes.py:29 - main
    Wrote profile results to profile_output.txt
    Wrote profile results to profile_output_2023-08-12T193302.txt
    Wrote profile results to profile_output.lprof
    To view details run:
    python -m line_profiler -rtmz profile_output.lprof


For more control over the outputs, run your script using :py:mod:`kernprof`.
The following invocation will run your script, dump results to
``demo_primes.py.lprof``, and display results.

.. code:: bash

    python -m kernprof -lvr demo_primes.py


Note: the ``-r`` flag will use "rich-output" if you have the :py:mod:`rich`
module installed.

See Also:

    * autoprofiling usage in: :py:mod:`line_profiler.autoprofile`
"""
# Note: there are better ways to generate primes
# https://github.com/Sylhare/nprime

__submodules__ = [
    'line_profiler',
    'ipython_extension',
]

__autogen__ = """
mkinit ./line_profiler/__init__.py --relative
mkinit ./line_profiler/__init__.py --relative -w
"""


# from .line_profiler import __version__

# NOTE: This needs to be in sync with ../kernprof.py and line_profiler.py
__version__ = '4.1.2'

from .line_profiler import (LineProfiler,
                            load_ipython_extension, load_stats, main,
                            show_func, show_text,)


from .explicit_profiler import profile


__all__ = ['LineProfiler', 'line_profiler',
           'load_ipython_extension', 'load_stats', 'main', 'show_func',
           'show_text', '__version__', 'profile']

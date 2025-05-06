``kernprof`` invocations
========================

The module (and installed script) :py:mod:`kernprof` can be used to run
and profile Python code in various forms.

For the following, we assume that we have:

* the below file ``fib.py`` in the current directory,
* the current directory in ``${PYTHONPATH}``, and
* :py:mod:`line_profiler` and :py:mod:`kernprof` installed.

.. code:: python

    import functools
    import sys
    from argparse import ArgumentParser
    from typing import Callable, Optional, Sequence


    @functools.lru_cache()
    def fib(n: int) -> int:
        return _run_fib(fib, n)


    def fib_no_cache(n: int) -> int:
        return _run_fib(fib_no_cache, n)


    def _run_fib(fib: Callable[[int], int], n: int) -> int:
        if n < 0:
            raise ValueError(f'{n = !r}: expected non-negative integer')
        if n < 2:
            return 1
        prev_prev = fib(n - 2)
        prev = fib(n - 1)
        return prev_prev + prev


    def main(args: Optional[Sequence[str]] = None) -> None:
        parser = ArgumentParser()
        parser.add_argument('n', nargs='+', type=int)
        parser.add_argument('--verbose', action='store_true')
        parser.add_argument('--no-cache', action='store_true')
        arguments = parser.parse_args(args)

        pattern = 'fib({!r}) = {!r}' if arguments.verbose else '{1!r}'
        func = fib_no_cache if arguments.no_cache else fib

        for n in arguments.n:
            result = func(n)
            print(pattern.format(n, result))


    if __name__ == '__main__':
        main()


Script execution
----------------

In the most basic form, one passes the path to the executed script and
its arguments to ``kernprof``:

.. code:: console

    $ kernprof --prof-mod fib.py --line-by-line --view \
    > fib.py --verbose 10 20 30
    fib(10) = 89
    fib(20) = 10946
    fib(30) = 1346269
    Wrote profile results to fib.py.lprof
    Timer unit: 1e-06 s

    Total time: 5.6e-05 s
    File: fib.py
    Function: fib at line 7

    Line #      Hits         Time  Per Hit   % Time  Line Contents
    ==============================================================
         7                                           @functools.lru_cache()
         8                                           def fib(n: int) -> int:
         9        31         56.0      1.8    100.0      return _run_fib(fib, n)

    Total time: 0 s
    File: fib.py
    Function: fib_no_cache at line 12

    Line #      Hits         Time  Per Hit   % Time  Line Contents
    ==============================================================
        12                                           def fib_no_cache(n: int) -> int:
        13                                               return _run_fib(fib_no_cache, n)

    Total time: 3.8e-05 s
    File: fib.py
    Function: _run_fib at line 16

    Line #      Hits         Time  Per Hit   % Time  Line Contents
    ==============================================================
        16                                           def _run_fib(fib: Callable[[int], int], n: int) -> int:
        17        31          3.0      0.1      7.9      if n < 0:
        18                                                   raise ValueError(f'{n = !r}: expected non-negative integer')
        19        31          2.0      0.1      5.3      if n < 2:
        20         2          0.0      0.0      0.0          return 1
        21        29         18.0      0.6     47.4      prev_prev = fib(n - 2)
        22        29         12.0      0.4     31.6      prev = fib(n - 1)
        23        29          3.0      0.1      7.9      return prev_prev + prev

    Total time: 0.000486 s
    File: fib.py
    Function: main at line 26

    Line #      Hits         Time  Per Hit   % Time  Line Contents
    ==============================================================
        26                                           def main(args: Optional[Sequence[str]] = None) -> None:
        27         1        184.0    184.0     37.9      parser = ArgumentParser()
        28         1         17.0     17.0      3.5      parser.add_argument('n', nargs='+', type=int)
        29         1         16.0     16.0      3.3      parser.add_argument('--verbose', action='store_true')
        30         1         14.0     14.0      2.9      parser.add_argument('--no-cache', action='store_true')
        31         1        144.0    144.0     29.6      arguments = parser.parse_args(args)
        32                                           
        33         1          0.0      0.0      0.0      pattern = 'fib({!r}) = {!r}' if arguments.verbose else '{1!r}'
        34         1          0.0      0.0      0.0      func = fib_no_cache if arguments.no_cache else fib
        35                                           
        36         4          0.0      0.0      0.0      for n in arguments.n:
        37         3         91.0     30.3     18.7          result = func(n)
        38         3         20.0      6.7      4.1          print(pattern.format(n, result))

.. _kernprof-script-note:
.. note::

   Instead of passing the ``--view`` flag to ``kernprof`` to view the
   profiling results immediately, sometimes it can be more convenient to
   just generate the profiling results and view them later by running
   the :py:mod:`line_profiler` module (``python -m line_profiler``).


Module execution
----------------

It is also possible to use ``kernprof -m`` to run installed modules and
packages:

.. code:: console

    $ kernprof --prof-mod fib --line-by-line --view -m \
    > fib --verbose 10 20 30
    fib(10) = 89
    fib(20) = 10946
    fib(30) = 1346269
    Wrote profile results to fib.lprof
    ...

.. _kernprof-m-note:
.. note::

    As with ``python -m``, the ``-m`` option terminates further parsing
    of arguments by ``kernprof`` and passes them all to the argument
    thereafter (the run module).
    If there isn't one, an error is raised:

    .. code:: console

        $ kernprof -m
        Traceback (most recent call last):
          ...
        ValueError: argument expected for the -m option


Literal-code execution
----------------------

Like how ``kernprof -m`` parallels ``python -m``, ``kernprof -c`` can be
used to run and profile literal snippets supplied on the command line
like ``python -c``:

.. code:: console

    $ code="import sys; "
    $ code+="from fib import _run_fib, fib_no_cache as fib; "
    $ code+="for n in sys.argv[1:]: print(f'fib({n})', '=', fib(int(n)))"
    $ kernprof --prof-mod fib._run_fib --line-by-line --view -c "${code}" 10 20
    fib(10) = 89
    fib(20) = 10946
    Wrote profile results to <...>/kernprof-command-imuhz89_.lprof
    Timer unit: 1e-06 s

    Total time: 0.007666 s
    File: <...>/fib.py
    Function: _run_fib at line 16

    Line #      Hits         Time  Per Hit   % Time  Line Contents
    ==============================================================
        16                                           def _run_fib(fib: Callable[[int], int], n: int) -> int:
        17     22068       1656.0      0.1     20.6      if n < 0:
        18                                                   raise ValueError(f'{n = !r}: expected non-negative integer')
        19     22068       1663.0      0.1     20.7      if n < 2:
        20     11035        814.0      0.1     10.1          return 1
        21     11033       1668.0      0.2     20.7      prev_prev = fib(n - 2)
        22     11033       1477.0      0.1     18.4      prev = fib(n - 1)
        23     11033        770.0      0.1      9.6      return prev_prev + prev

.. note::

    * As with ``python -c``, the ``-c`` option terminates further
      parsing of arguments by ``kernprof`` and passes them all to the
      argument thereafter (the executed code).
      If there isn't one, an error is raised as
      :ref:`above <kernprof-m-note>` with ``kernprof -m``.
    * .. _kernprof-c-note:
      Since the temporary file containing the executed code will not
      exist beyond the ``kernprof`` process, profiling results
      pertaining to targets (function definitions) local to said code
      :ref:`will not be accessible later <kernprof-script-note>` by
      ``python -m line_profiler`` and has to be ``--view``-ed
      immediately:

      .. code:: console

          $ read -d '' -r code <<-'!'
          > from fib import fib
          >
          > def my_func(n=50):
          >     result = fib(n)
          >     print(n, '->', result)
          >
          > my_func()
          > !
          $ kernprof -lv -c "${code}"
          50 -> 20365011074
          Wrote profile results to <...>/kernprof-command-ni6nis6t.lprof
          Timer unit: 1e-06 s

          Total time: 3.8e-05 s
          File: <...>/kernprof-command.py
          Function: my_func at line 3

          Line #      Hits         Time  Per Hit   % Time  Line Contents
          ==============================================================
               3                                           def my_func(n=50):
               4         1         26.0     26.0     68.4      result = fib(n)
               5         1         12.0     12.0     31.6      print(n, '->', result)

          $ python -m line_profiler kernprof-command-ni6nis6t.lprof 
          Timer unit: 1e-06 s
          
          Total time: 3.6e-05 s
          
          Could not find file <...>/kernprof-command.py
          Are you sure you are running this program from the same directory
          that you ran the profiler from?
          Continuing without the function's contents.

          Line #      Hits         Time  Per Hit   % Time  Line Contents
          ==============================================================
               3                                           
               4         1         26.0     26.0     72.2  
               5         1         10.0     10.0     27.8  


Executing code read from ``stdin``
----------------------------------

It is also possible to read, run, and profile code from ``stdin``, by
passing ``-`` to ``kernprof`` in place of a filename:

.. code:: console

    $ kernprof --prof-mod fib._run_fib --line-by-line --view - 10 20 <<-'!'
    > import sys
    > from fib import _run_fib, fib_no_cache as fib
    > for n in sys.argv[1:]:
    >     print(f"fib({n})", "=", fib(int(n)))
    > !
    fib(10) = 89
    fib(20) = 10946
    Wrote profile results to <...>/kernprof-stdin-kntk2lo1.lprof
    ...

.. note::

    Since the temporary file containing the executed code will not exist
    beyond the ``kernprof`` process, profiling results pertaining to
    targets (function definitions) local to said code will not be
    accessible later and has to be ``--view``-ed immediately
    (see :ref:`above note <kernprof-c-note>` on ``kernprof -c``).

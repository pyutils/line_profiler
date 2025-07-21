Using the line-profiler TOML configuration
------------------------------------------

This tutorial walks the user through setting up a toy Python project and then
interacting with it via the new line-profiler TOML configuration.

First, we need to setup a small project, for which we will use ``uv``. We will
also use the ``tomlkit`` package to edit the config file programatically. If
you don't have these installed, fist run:

.. code:: bash

   pip install uv tomlkit


Next, we are going to setup a small package for this demonstration.

.. code:: bash

   TEMP_DIR=$(mktemp -d --suffix=demo_pkg)
   mkdir -p $TEMP_DIR
   cd $TEMP_DIR

   uv init --lib --name demo_pkg

   # helper to prevent indentation errors
   codeblock(){
       echo "$1" | python -c "import sys; from textwrap import dedent; print(dedent(sys.stdin.read()).strip('\n'))"
   }

   codeblock "
       import time
       from demo_pkg.utils import leq
       from demo_pkg import utils

       def fib(n):
           if leq(n, 1):
               return n
           part1 = fib(n - 1)
           part2 = fib(n - 2)
           result = utils.add(part1, part2)
           return result

       def sleep_loop(n):
           for _ in range(n):
               time.sleep(0.01)
   " > src/demo_pkg/core.py

   codeblock "
       def leq(a, b):
           return a <= b

       def add(a, b):
           return a + b
   " > src/demo_pkg/utils.py

   codeblock "
       from demo_pkg import core
       import uuid

       def main():
           run_uuid = uuid.uuid4()
           print('The UUID of this run is', run_uuid)
           print('compute fib 10')
           result = core.fib(10)
           print('result', result)
           print('sleeping 5')
           core.sleep_loop(5)
           print('done')

       if __name__ == '__main__':
           main()
   " > src/demo_pkg/__main__.py

   # Run `uv pip install -e .` to install the project locally:
   uv pip install -e .


Test that the main entrypoint works.

.. code:: bash

   python -m demo_pkg


Running kernprof with a main script that uses your package behaves as in 4.x in that no defaults are modified.

.. code:: bash

    kernprof -m demo_pkg


However, you can modify pyproject.toml to specify new defaults. After doing
this, running kernprof will use defaults specified in your pyproject.toml (You
may also pass ``--config`` to tell kernprof to use a different file to load the
default config).

.. code:: bash

   # Edit the `pyproject.toml` file to modify default behavior
   update_pyproject_toml(){
       python -c "if 1:
           import pathlib
           import tomllib
           import tomlkit
           import sys
           config_path = pathlib.Path('pyproject.toml')
           config = tomllib.loads(config_path.read_text())

           # Add in new values
           from textwrap import dedent
           new_text = dedent(sys.argv[1])

           new_parts = tomllib.loads(new_text)
           config.update(new_parts)

           new_text = tomlkit.dumps(config)
           config_path.write_text(new_text)
       " "$1"
   }

   update_pyproject_toml "
       # New Config
       [tool.line_profiler.kernprof]
       line-by-line = true
       rich = true
       verbose = true
       skip-zero = true
       prof-mod = ['demo_pkg']
       "

   # Now, running kernprof uses the new defaults
   kernprof -m demo_pkg


You will now see how long each function took, and what the line-by line breakdown is

.. code::

  # line-by-line breakdown omitted here

  0.05 seconds - /tmp/tmp.vKpODQr6wndemo_pkg/src/demo_pkg/__main__.py:4 - main
  0.00 seconds - /tmp/tmp.vKpODQr6wndemo_pkg/src/demo_pkg/core.py:5 - fib
  0.05 seconds - /tmp/tmp.vKpODQr6wndemo_pkg/src/demo_pkg/core.py:13 - sleep_loop
  0.00 seconds - /tmp/tmp.vKpODQr6wndemo_pkg/src/demo_pkg/utils.py:1 - leq
  0.00 seconds - /tmp/tmp.vKpODQr6wndemo_pkg/src/demo_pkg/utils.py:4 - add


Note that by specifying ``prof-mod``, every function within the package is
automatically profiled without any need for the ``@profile`` decorator.

It is worth noting, there is no requirement that the module you are profiling
is part of your package. You can specify any module name as part of
``prof-mod``. For example, lets profile the stdlib uuid module.


.. code:: bash

   update_pyproject_toml "
       # New Config
       [tool.line_profiler.kernprof]
       line-by-line = true
       rich = true
       verbose = 0
       skip-zero = true
       prof-mod = ['uuid']
       "

   # Now, running kernprof uses the new defaults
   kernprof -m demo_pkg
   python -m line_profiler -rmtz demo_pkg.lprof


This results in only showing calls in the uuid package:

.. code::

  # line-by-line breakdown omitted here

  0.00 seconds - .pyenv/versions/3.13.2/lib/python3.13/uuid.py:142 - UUID.__init__
  0.00 seconds - .pyenv/versions/3.13.2/lib/python3.13/uuid.py:283 - UUID.__str__
  0.00 seconds - .pyenv/versions/3.13.2/lib/python3.13/uuid.py:277 - UUID.__repr__
  0.00 seconds - .pyenv/versions/3.13.2/lib/python3.13/uuid.py:710 - uuid4


You can list exact functions to profile as long as they are addressable by
dotted names. The above only profiles the ``fib`` function in our package:

.. code:: bash

   update_pyproject_toml "
       # New Config
       [tool.line_profiler.kernprof]
       line-by-line = true
       rich = true
       verbose = 0
       skip-zero = true
       prof-mod = ['demo_pkg.core.fib']
       "

   # Now, running kernprof uses the new defaults
   kernprof -m demo_pkg
   python -m line_profiler -rmtz demo_pkg.lprof


The output is:

.. code::

   Line #      Hits         Time  Per Hit   % Time  Line Contents
   ==============================================================
        5                                           def fib(n):
        6       177        145.1      0.8     42.5      if leq(n, 1):
        7        89         29.7      0.3      8.7          return n
        8        88         29.1      0.3      8.5      part1 = fib(n - 1)
        9        88         27.7      0.3      8.1      part2 = fib(n - 2)
       10        88         78.0      0.9     22.8      result = utils.add(part1, part2)
       11        88         32.2      0.4      9.4      return result


     0.00 seconds - /tmp/tmp.vKpODQr6wndemo_pkg/src/demo_pkg/core.py:5 - fib

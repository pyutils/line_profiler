"""
New in ``line_profiler`` version 4.1.0, this module defines a top-level
``profile`` decorator which will be disabled by default **unless** a script is
being run with :mod:`kernprof`, if the environment variable
:envvar:`LINE_PROFILE` is set, or if ``--line-profile`` is given on the command
line.

In the latter two cases, the :mod:`atexit` module is used to display and dump
line profiling results to disk when Python exits.

If none of the enabling conditions are met, then
:py:obj:`line_profiler.profile` is a no-op. This means you no longer have to add
and remove the implicit ``profile`` decorators required by previous version of
this library.

Basic usage is to import line_profiler and decorate your function with
line_profiler.profile.  By default this does nothing, it's a no-op decorator.
However, if you run with the environment variable ``LINE_PROFILER=1`` or if
``'--profile' in sys.argv``, then it enables profiling and at the end of your
script it will output the profile text.

Here is a minimal example that will write a script to disk and then run it
with profiling enabled or disabled by various methods:

.. code:: bash

    # Write demo python script to disk
    python -c "if 1:
        import textwrap
        text = textwrap.dedent(
            '''
            from line_profiler import profile

            @profile
            def plus(a, b):
                return a + b

            @profile
            def fib(n):
                a, b = 0, 1
                while a < n:
                    a, b = b, plus(a, b)

            @profile
            def main():
                import math
                import time
                start = time.time()

                print('start calculating')
                while time.time() - start < 1:
                    fib(10)
                    math.factorial(1000)
                print('done calculating')

            main()
            '''
        ).strip()
        with open('demo.py', 'w') as file:
            file.write(text)
    "

    echo "---"
    echo "## Base Case: Run without any profiling"
    python demo.py

    echo "---"
    echo "## Option 0: Original Usage"
    python -m kernprof -l demo.py
    python -m line_profiler -rmt demo.py.lprof

    echo "---"
    echo "## Option 1: Enable profiler with the command line"
    python demo.py --line-profile

    echo "---"
    echo "## Option 1: Enable profiler with an environment variable"
    LINE_PROFILE=1 python demo.py


The explicit :py:attr:`line_profiler.profile` decorator can also be enabled and
configured in the Python code itself by calling
:func:`line_profiler.profile.enable`. The following example demonstrates this:

.. code:: bash

    # In-code enabling
    python -c "if 1:
        import textwrap
        text = textwrap.dedent(
            '''
            from line_profiler import profile
            profile.enable(output_prefix='customized')

            @profile
            def fib(n):
                a, b = 0, 1
                while a < n:
                    a, b = b, a + b

            fib(100)
            '''
        ).strip()
        with open('demo.py', 'w') as file:
            file.write(text)
    "
    echo "## Configuration handled inside the script"
    python demo.py


Likewise there is a :func:`line_profiler.profile.disable` function that will
prevent any subsequent functions decorated with ``@profile`` from being
profiled. In the following example, profiling information will only be recorded
for ``func2`` and ``func4``.

.. code:: bash

    # In-code enabling / disable
    python -c "if 1:
        import textwrap
        text = textwrap.dedent(
            '''
            from line_profiler import profile

            @profile
            def func1():
                return list(range(100))

            profile.enable(output_prefix='custom')

            @profile
            def func2():
                return tuple(range(100))

            profile.disable()

            @profile
            def func3():
                return set(range(100))

            profile.enable()

            @profile
            def func4():
                return dict(zip(range(100), range(100)))

            print(type(func1()))
            print(type(func2()))
            print(type(func3()))
            print(type(func4()))
            '''
        ).strip()
        with open('demo.py', 'w') as file:
            file.write(text)
    "

    echo "---"
    echo "## Configuration handled inside the script"
    python demo.py

    # Running with --line-profile will also profile ``func1``
    python demo.py --line-profile

The core functionality in this module was ported from :mod:`xdev`.
"""
from __future__ import annotations
import atexit
import multiprocessing
import os
import pathlib
import sys
import typing
from typing import Any, Callable

if typing.TYPE_CHECKING:
    ConfigArg = str | pathlib.PurePath | bool | None


# This is for compatibility
from .cli_utils import boolean, get_python_executable as _python_command
from .line_profiler import LineProfiler
from .toml_config import ConfigSource

# The first process that enables profiling records its PID here. Child processes
# created via multiprocessing (spawn/forkserver) inherit this environment value,
# which helps prevent helper processes from claiming ownership and clobbering
# output. Standalone subprocess runs should always be able to reset this value.
_OWNER_PID_ENVVAR: str = 'LINE_PROFILER_OWNER_PID'


class GlobalProfiler:
    """
    Manages a profiler that will output on interpreter exit.

    The :py:obj:`line_profile.profile` decorator is an instance of this object.

    Arguments:
        config (str | PurePath | bool | None):
            Optional TOML config file from which to load the
            configurations (see Attributes);
            if not explicitly given (= :py:data:`True` or
            :py:data:`None`), it is either resolved from the
            :envvar:`!LINE_PROFILER_RC` environment variable or looked
            up among the current directory or its ancestors.  Should all
            that fail, the default config file at
            ``importlib.resources.path('line_profiler.rc', 'line_profiler.toml')`` is used;
            passing :py:data:`False` disables all lookup and falls back
            to the default configuration

    Attributes:
        setup_config (Dict[str, List[str]]):
            Determines how the implicit setup behaves by defining which
            environment variables / command line flags to look for.
            Defaults to the ``[tool.line_profiler.setup]`` table of the
            loaded config file.

        output_prefix (str):
            The prefix of any output files written. Should include
            a part of a filename. Defaults to the ``output_prefix``
            value in the ``[tool.line_profiler.write]`` table of the
            loaded config file.

        write_config (Dict[str, bool]):
            Which outputs are enabled;
            options are lprof, text, timestamped_text, and stdout.
            Defaults to the rest of the ``[tool.line_profiler.write]``
            table of the loaded config file.

        show_config (Dict[str, bool]):
            Display configuration options;
            some outputs force certain options (e.g. text always has
            details and is never rich).
            Defaults to the rest of the ``[tool.line_profiler.show]``
            table of the loaded config file, excluding the
            ``[tool.line_profiler.show.column_widths]`` subtable.

        enabled (bool | None):
            True if the profiler is enabled (i.e. if it will wrap a function
            that it decorates with a real profiler). If None, then the value
            defaults based on the ``setup_config``, :py:obj:`os.environ`, and
            :py:obj:`sys.argv`.

    Example:
        >>> from line_profiler.explicit_profiler import *  # NOQA
        >>> self = GlobalProfiler()
        >>> # Setting the _profile attribute prevents atexit from running.
        >>> self._profile = LineProfiler()
        >>> # User can personalize the configuration
        >>> self.show_config['details'] = True
        >>> self.write_config['lprof'] = False
        >>> self.write_config['text'] = False
        >>> self.write_config['timestamped_text'] = False
        >>> # Demo data: a function to profile
        >>> def collatz(n):
        ...     while n != 1:
        ...         if n % 2 == 0:
        ...             n = n // 2
        ...         else:
        ...             n = 3 * n + 1
        ...     return n
        >>> # Disabled by default, implicitly checks to auto-enable on first wrap
        >>> assert self.enabled is None
        >>> wrapped = self(collatz)
        >>> assert self.enabled is False
        >>> assert wrapped is collatz
        >>> # Can explicitly enable
        >>> self.enable()
        >>> wrapped = self(collatz)
        >>> assert self.enabled is True
        >>> assert wrapped is not collatz
        >>> wrapped(100)
        >>> # Can explicitly request output
        >>> self.show()
    """

    _config: pathlib.PurePath | None
    _profile: LineProfiler | None
    _owner_pid: int | None
    enabled: bool | None

    setup_config: dict[str, list[str]]
    write_config: dict[str, Any]
    show_config: dict[str, Any]
    output_prefix: str

    def __init__(self, config: ConfigArg = None) -> None:
        # Remember which config file we loaded settings from
        config_source = ConfigSource.from_config(config)
        self._config = config_source.path

        self._profile = None
        self._owner_pid = None
        self.enabled = None
        # Configs:
        # - How to toggle the profiler
        self.setup_config = config_source.conf_dict['setup']
        # - Which outputs to write on exit
        self.write_config = config_source.conf_dict['write']
        # - Whither to write output files
        self.output_prefix = self.write_config.pop('output_prefix')
        # - How output will be displayed
        self.show_config = config_source.conf_dict['show']
        # (This is not stored here nor is accepted by any method, but is
        # re-parsed by `LineProfiler.print_stats()` etc. from the
        # supplied `config`)
        self.show_config.pop('column_widths')

    def _kernprof_overwrite(self, profile: LineProfiler) -> None:
        """
        Kernprof will call this when it runs, so we can use its profile object
        instead of our own. Note: when kernprof overwrites us we wont register
        an atexit hook. This is what we want because kernprof wants us to use
        another program to read its output file.
        """
        self._profile = profile
        self.enabled = True

    def _implicit_setup(self) -> None:
        """
        Called once the first time the user decorates a function with
        ``line_profiler.profile`` and they have not explicitly setup the global
        profiling options.
        """
        environ_flags = self.setup_config['environ_flags']
        cli_flags = self.setup_config['cli_flags']
        is_profiling = any(boolean(os.environ.get(f, ''), fallback=True)
                           for f in environ_flags)
        is_profiling |= any(f in sys.argv for f in cli_flags)
        if is_profiling:
            self.enable()
        else:
            self.disable()

    def enable(self, output_prefix: str | None = None) -> None:
        """
        Explicitly enables global profiler and controls its settings.

        Notes:
            Multiprocessing start methods like 'spawn'/'forkserver' can create
            helper/bootstrap interpreters that import this module. Those helpers
            must not claim ownership or register an atexit hook, otherwise they can
            clobber output from the real script process.
        """
        self._debug("enable:ENTER")

        if is_mp_bootstrap():
            self._debug("enable:skip-mp-bootstrap")
            self.enabled = False
            return

        if self._should_skip_due_to_owner():
            self._debug("enable:skip-due-to-owner")
            self.enabled = False
            return

        owner_pid = os.getpid()
        os.environ[_OWNER_PID_ENVVAR] = str(owner_pid)
        self._owner_pid = owner_pid
        self._debug("enable:owner-claimed", owner_pid=owner_pid)

        if self._profile is None:
            atexit.register(self.show)
            self._profile = LineProfiler()

        self.enabled = True
        if output_prefix is not None:
            self.output_prefix = output_prefix

    def _should_skip_due_to_owner(self) -> bool:
        """
        Return True if another process has already claimed ownership.

        The first process to enable profiling records its PID in an env var.
        Child interpreters can inherit that value; they must not steal ownership.
        """
        owner = os.environ.get(_OWNER_PID_ENVVAR)
        if not owner:
            self._debug("owner:no-owner-env")
            return False

        current = str(os.getpid())
        if owner == current:
            self._debug("owner:is-us", owner=owner)
            return False

        if is_mp_bootstrap():
            self._debug("owner:skip-mp-bootstrap", owner=owner, current=current)
            return True

        # Standalone run: allow this interpreter to become the owner.
        self._debug("owner:allow-standalone-reset", owner=owner, current=current)
        return False

    def _debug(self, message: str, **extra: Any) -> None:
        if not os.environ.get('LINE_PROFILER_DEBUG'):
            return
        try:
            parent = multiprocessing.parent_process()
            parent_pid = parent.pid if parent is not None else None
        except Exception:
            parent_pid = None

        pid = os.getpid()

        info: dict[str, Any] = {
            'ppid': os.getppid(),
            'process': getattr(multiprocessing.current_process(), 'name', None),
            'parent_pid': parent_pid,
            'owner_env': os.environ.get(_OWNER_PID_ENVVAR),
            'owner_pid': self._owner_pid,
            'enabled': self.enabled,
        }
        info.update(extra)
        payload = ' '.join(f'{k}={v!r}' for k, v in info.items())
        print(f'[line_profiler debug {pid=}] {message} {payload}')

    def disable(self) -> None:
        """
        Explicitly initialize and disable this global profiler.
        """
        self.enabled = False

    def __call__(self, func: Callable) -> Callable:
        """
        If the global profiler is enabled, decorate a function to start the
        profiler on function entry and stop it on function exit. Otherwise
        return the input.

        Args:
            func (Callable): the function to profile

        Returns:
            Callable: a potentially wrapped function
        """
        # from multiprocessing import current_process
        # if current_process().name != 'MainProcess':
        #     return func

        if self.enabled is None:
            # Force a setup if we haven't done it before.
            self._implicit_setup()
        if not self.enabled:
            return func
        assert self._profile is not None

        wrapped = self._profile(func)
        return wrapped

    def show(self) -> None:
        """
        Write the managed profiler stats to enabled outputs.

        If the implicit setup triggered, then this will be called by
        :py:mod:`atexit`.
        """
        self._debug('show:enter')
        owner_env = os.environ.get(_OWNER_PID_ENVVAR)
        if os.getppid() == 1 and owner_env == str(os.getpid()):
            self._debug('show:skip-orphan-owner', owner_env=owner_env)
            return
        if self._owner_pid is not None and os.getpid() != self._owner_pid:
            self._debug('show:skip-non-owner', current_pid=os.getpid())
            return
        import io

        write_stdout = self.write_config['stdout']
        write_text = self.write_config['text']
        write_timestamped_text = self.write_config['timestamped_text']
        write_lprof = self.write_config['lprof']

        assert self._profile is not None

        kwargs: dict[str, Any] = {'config': self._config, **self.show_config}
        if write_stdout:
            self._profile.print_stats(**kwargs)

        if write_text or write_timestamped_text:
            stream = io.StringIO()
            # Text output always contains details, and cannot be rich.
            text_kwargs: dict[str, Any] = {**kwargs, 'rich': False, 'details': True}
            self._profile.print_stats(stream=stream, **text_kwargs)
            raw_text = stream.getvalue()

            if write_text:
                txt_output_fpath1 = pathlib.Path(f'{self.output_prefix}.txt')
                txt_output_fpath1.write_text(raw_text, encoding='utf-8')
                print('Wrote profile results to %s' % txt_output_fpath1)

            if write_timestamped_text:
                from datetime import datetime as datetime_cls
                now = datetime_cls.now()
                timestamp = now.strftime('%Y-%m-%dT%H%M%S')
                txt_output_fpath2 = pathlib.Path(
                    f'{self.output_prefix}_{timestamp}.txt')
                txt_output_fpath2.write_text(raw_text, encoding='utf-8')
                print('Wrote profile results to %s' % txt_output_fpath2)

        if write_lprof:
            lprof_output_fpath = pathlib.Path(f'{self.output_prefix}.lprof')
            self._profile.dump_stats(lprof_output_fpath)
            print('Wrote profile results to %s' % lprof_output_fpath)
            print('To view details run:')
            py_exe = _python_command()
            print(py_exe + ' -m line_profiler -rtmz '
                  + str(lprof_output_fpath))


def is_mp_bootstrap() -> bool:
    """
    True when this interpreter invocation looks like multiprocessing
    bootstrapping/plumbing, where we must not claim ownership / write outputs.

    Example:
        >>> # xdoctest: +SKIP('can be flaky at test time')
        >>> import pytest
        >>> if is_mp_bootstrap():
        ...     pytest.skip('Cannot test mp bootstrap detection from within an mp bootstrap process')
        >>> import sys, subprocess, textwrap
        >>> code = textwrap.dedent(r'''
        ... import multiprocessing as mp
        ... from line_profiler.explicit_profiler import is_mp_bootstrap
        ...
        ... def child(q):
        ...     q.put(is_mp_bootstrap())
        ...
        ... if __name__ == "__main__":
        ...     ctx = mp.get_context("spawn")
        ...     q = ctx.Queue()
        ...     p = ctx.Process(target=child, args=(q,))
        ...     p.start()
        ...     val = q.get()
        ...     p.join()
        ...     print(val)
        ... ''')
        >>> out = subprocess.check_output([sys.executable, "-c", code], text=True).strip()
        >>> out in {"True", "False"}
        True
    """
    try:
        import multiprocessing.spawn as mp_spawn
        if getattr(mp_spawn, "_inheriting", False):
            return True
    except Exception:
        pass

    orig = getattr(sys, "orig_argv", None) or []
    if any(a.startswith("--multiprocessing") for a in orig):
        return True
    if any("multiprocessing.forkserver" in a for a in orig):
        return True
    if any("multiprocessing.spawn" in a for a in orig):
        return True

    try:
        if multiprocessing.current_process().name != "MainProcess":
            return True
    except Exception:
        pass

    return False


# Construct the global profiler.
# The first time it is called, it will be initialized. This is usually a
# NoOpProfiler unless the user requested the real one.
# NOTE: kernprof or the user may explicitly setup the global profiler.
profile = GlobalProfiler()

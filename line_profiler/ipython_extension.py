"""
This module defines the |lprun| and |lprun_all| IPython magic functions.

If you are using IPython, there is an implementation of an |lprun| magic
command which will let you specify functions to profile and a statement
to execute. It will also add its
:py:class:`~.LineProfiler` instance into the |builtins|, but typically,
you would not use it like that.

You can also use |lprun_all|, which profiles the whole cell you're
executing automagically, without needing to specify lines/functions
yourself. It's meant for easier use for beginners.

For IPython 0.11+, you can install it by editing the IPython configuration file
``~/.ipython/profile_default/ipython_config.py`` to add the ``'line_profiler'``
item to the extensions list::

    c.TerminalIPythonApp.extensions = [
        'line_profiler',
    ]

Or explicitly call::

    %load_ext line_profiler

To get usage help for |lprun| and |lprun_all|, use the standard IPython
help mechanism::

    In [1]: %lprun?

.. |lprun| replace:: :py:data:`%lprun <LineProfilerMagics.lprun>`
.. |lprun_all| replace:: :py:data:`%%lprun_all <LineProfilerMagics.lprun_all>`
.. |builtins| replace:: :py:mod:`__builtins__ <builtins>`
"""

import ast
import builtins
import functools
import os
import tempfile
import textwrap
import time
import types
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Union
if TYPE_CHECKING:  # pragma: no cover
    from typing import (Callable, ParamSpec,  # noqa: F401
                        Any, ClassVar, TypeVar)

    PS = ParamSpec('PS')
    PD = TypeVar('PD', bound='_PatchDict')
    DefNode = TypeVar('DefNode', ast.FunctionDef, ast.AsyncFunctionDef)

from io import StringIO

from IPython.core.getipython import get_ipython
from IPython.core.magic import Magics, magics_class, line_magic, cell_magic
from IPython.core.page import page
from IPython.utils.ipstruct import Struct
from IPython.core.error import UsageError

from line_profiler import line_profiler, LineProfiler, LineStats
from line_profiler.autoprofile.ast_tree_profiler import AstTreeProfiler


__all__ = ('LineProfilerMagics',)

_LPRUN_ALL_CODE_OBJ_NAME = '<lprof_cell>'


@dataclass
class _ParseParamResult:
    """ Class for holding parsed info relevant to the behaviors of both
    the ``%lprun`` and ``%%lprun_all`` magics.

    Attributes:
        ``.opts``
            :py:class:`IPython.utils.ipstruct.Struct` object.
        ``.arg_str``
            :py:class:`str` of unparsed argument(s).
        ``.dump_raw_dest``
            (Descriptor) :py:class:`pathlib.Path` to write the raw
            (pickled) profiling results to, or :py:data:`None` if not to
            be written.
        ``.dump_text_dest``
            (Descriptor) :py:class:`pathlib.Path` to write the
            plain-text profiling results to, or :py:data:`None` if not
            to be written.
        ``.output_unit``
            (Descriptor) Unit to normalize the output of
            :py:meth:`line_profiler.LineProfiler.print_stats` to, or
            :py:data:`None` if not specified.
        ``.strip_zero``
            (Descriptor) Whether to call
            :py:meth:`line_profiler.LineProfiler.print_stats` with
            ``stripzeros=True``.
        ``.return_profiler``
            (Descriptor) Whether the
            :py:class:`line_profiler.LineProfiler` instance is to be
            returned.
    """
    opts: Struct
    arg_str: str

    def __getattr__(self, attr):  # type: (str) -> Any
        """ Defers to :py:attr:`_ParseParamResult.opts`."""
        return getattr(self.opts, attr)

    @functools.cached_property
    def dump_raw_dest(self):  # type: () -> Path | None
        path = self.opts.D[0]
        if path:
            return Path(path)
        return None

    @functools.cached_property
    def dump_text_dest(self):  # type: () -> Path | None
        path = self.opts.T[0]
        if path:
            return Path(path)
        return None

    @functools.cached_property
    def output_unit(self):  # type: () -> float | None
        if self.opts.u is None:
            return None
        try:
            return float(self.opts.u[0])
        except Exception:
            raise TypeError("Timer unit setting must be a float.")

    @functools.cached_property
    def strip_zero(self):  # type: () -> bool
        return "z" in self.opts

    @functools.cached_property
    def return_profiler(self):  # type: () -> bool
        return "r" in self.opts


@dataclass
class _RunAndProfileResult:
    """ Class for holding the results of both the ``%lprun`` and
    ``%%lprun_all`` magics.
    """
    stats: LineStats
    parse_result: _ParseParamResult
    message: Union[str, None] = None
    time_elapsed: Union[float, None] = None
    tempfile: Union[str, 'os.PathLike[str]', None] = None

    def __post_init__(self):
        if self.tempfile is not None:
            self.tempfile = Path(self.tempfile)
        self.output  # Fetch value

    def _make_show_func_wrapper(self, show_func):
        """
        Create a replacement for
        :py:func:`line_profiler.line_profiler.show_func` to be
        monkey-patched in, so that when showing the results of the
        entire cell the lines are not truncated at the end of the first
        code block.
        """
        tmp = self.tempfile
        if tmp is None:
            return show_func
        assert isinstance(tmp, Path)

        @functools.wraps(show_func)
        def show_func_wrapper(
                filename, start_lineno, func_name, *args, **kwargs):
            call = functools.partial(show_func,
                                     filename, start_lineno, func_name,
                                     *args, **kwargs)
            show_entire_module = (start_lineno == 1
                                  and func_name == _LPRUN_ALL_CODE_OBJ_NAME
                                  and tmp is not None
                                  and tmp.samefile(filename))
            if not show_entire_module:
                return call()
            with _PatchDict.from_module(
                    line_profiler, get_code_block=get_code_block_wrapper):
                return call()

        def get_code_block_wrapper(filename, lineno):
            """ Return the entire content of :py:attr:`~.tempfile`."""
            with tmp.open(mode='r') as fobj:
                return fobj.read().splitlines(keepends=True)

        return show_func_wrapper

    @functools.cached_property
    def output(self):  # type: () -> str
        with ExitStack() as stack:
            cap = stack.enter_context(StringIO())  # Trap text output
            patch_show_func = _PatchDict.from_module(
                line_profiler,
                show_func=self._make_show_func_wrapper(line_profiler.show_func))
            stack.enter_context(patch_show_func)
            self.stats.print(cap,
                             output_unit=self.parse_result.output_unit,
                             stripzeros=self.parse_result.strip_zero)
            return cap.getvalue().rstrip()


class _PatchProfilerIntoBuiltins:
    """
    Example:
        >>> # xdoctest: +REQUIRES(module:IPython)
        >>> import builtins
        >>> from line_profiler import LineProfiler
        >>>
        >>>
        >>> prof = LineProfiler()
        >>> with _PatchProfilerIntoBuiltins(prof):
        ...     assert builtins.profile is prof
        ...
        >>> print(builtins.profile)
        Traceback (most recent call last):
          ...
        AttributeError: ...

    Note:
        This class doesn't itself need :py:mod:`IPython`, but it
        resides in a module that does. To reduce complications, we just
        skip this doctest if :py:mod:`IPython` (and hence this module)
        can't be imported.
    """
    def __init__(self, prof=None):
        # type: (LineProfiler | None) -> None
        if prof is None:
            prof = LineProfiler()
        self.prof = prof
        self._ctx = _PatchDict.from_module(builtins, profile=self.prof)

    def __enter__(self):  # type: () -> LineProfiler
        self._ctx.__enter__()
        return self.prof

    def __exit__(self, *a, **k):
        return self._ctx.__exit__(*a, **k)


class _PatchDict:
    def __init__(self, namespace, /, **kwargs):
        # type: (dict[str, Any], Any) -> None
        self.namespace = namespace
        self.replacements = kwargs
        self._stack = []  # type: list[dict[str, Any]]
        self._absent = object()

    def __enter__(self):  # type: (PD) -> PD
        self._push()
        return self

    def __exit__(self, *_, **__):
        self._pop()

    def _push(self):
        entry = {}
        namespace = self.namespace
        absent = self._absent
        for key, value in self.replacements.items():
            entry[key] = namespace.pop(key, absent)
            namespace[key] = value
        self._stack.append(entry)

    def _pop(self):
        namespace = self.namespace
        absent = self._absent
        for key, value in self._stack.pop().items():
            if value is absent:
                namespace.pop(key, None)
            else:
                namespace[key] = value

    @classmethod
    def from_module(cls, module, /, **kwargs):
        # type: (type[PD], types.ModuleType, Any) -> PD
        return cls(vars(module), **kwargs)


@magics_class
class LineProfilerMagics(Magics):
    def _parse_parameters(self, parameter_s, getopt_spec, opts_def):
        # type: (str, str, Struct) -> _ParseParamResult
        # FIXME: There is a chance that this handling will need to be
        # updated to handle single-quoted characters better (#382)
        parameter_s = parameter_s.replace('"', r"\"").replace("'", r"\"")

        opts, arg_str = self.parse_options(
            parameter_s, getopt_spec, list_all=True)
        opts.merge(opts_def)
        return _ParseParamResult(opts, arg_str)

    @staticmethod
    def _run_and_profile(prof,  # type: LineProfiler
                         parse_result,  # type: _ParseParamResult
                         tempfile,  # type: str | None
                         method,  # type: Callable[PS, Any]
                         *args,  # type: PS.args
                         **kwargs,  # type: PS.kwargs
                         ):  # type: (...) -> _RunAndProfileResult
        # Use the time module because it's easier than parsing the
        # output from `show_text()`.
        # `perf_counter()` is a monotonically increasing alternative to
        # `time()` that's intended for simple benchmarking.
        start_time = time.perf_counter()
        try:
            method(*args, **kwargs)
            message = None
        except (SystemExit, KeyboardInterrupt) as e:
            message = (f"{type(e).__name__} exception caught in "
                       "code being profiled.")

        # Capture and save total runtime
        total_time = time.perf_counter() - start_time
        return _RunAndProfileResult(
            prof.get_stats(), parse_result,
            message=message, time_elapsed=total_time, tempfile=tempfile)

    @classmethod
    def _lprun_all_get_rewritten_profiled_code(cls, tmpfile):
        # type: (str) -> types.CodeType
        """ Transform and compile the AST of the profiled code. This is
        similar to :py:meth:`.LineProfiler.runctx`,
        """
        at = AstTreeProfiler(tmpfile, [tmpfile], profile_imports=False)
        tree = at.profile()

        return compile(tree, tmpfile, "exec")

    @classmethod
    def _lprun_get_top_level_profiled_code(cls, tmpfile):
        # type: (str) -> types.CodeType
        """ Compile the profiled code."""
        with open(tmpfile, mode='r') as fobj:
            return compile(fobj.read(), tmpfile, "exec")

    @staticmethod
    def _handle_end(prof, run_result):
        # type: (LineProfiler, _RunAndProfileResult) -> LineProfiler | None
        page(run_result.output)

        dump_file = run_result.parse_result.dump_raw_dest
        if dump_file is not None:
            prof.dump_stats(dump_file)
            print(f"\n*** Profile stats pickled to file {str(dump_file)!r}.")

        text_file = run_result.parse_result.dump_text_dest
        if text_file is not None:
            with text_file.open("w", encoding="utf-8") as pfile:
                print(run_result.output, file=pfile)
            print("\n*** Profile printout saved to text file "
                  f"{str(text_file)!r}.")

        if run_result.message:
            print("\n*** " + run_result.message)

        return prof if run_result.parse_result.return_profiler else None

    @line_magic
    def lprun(self, parameter_s=""):
        """Execute a statement under the line-by-line profiler from the
        :py:mod:`line_profiler` module.

        Usage::

            %lprun [<options>] <statement>

        The given statement (which doesn't require quote marks) is run
        via the :py:class:`~.LineProfiler`. Profiling is enabled for
        the functions specified by the ``-f`` options. The statistics
        will be shown side-by-side with the code through the pager once
        the statement has completed.

        Options:

        ``-f <function>``: :py:class:`~.LineProfiler` only profiles
        functions and methods it is told to profile. This option tells
        the profiler about these functions. Multiple ``-f`` options may
        be used. The argument may be any expression that gives
        a Python function or method object. However, one must be
        careful to avoid spaces that may confuse the option parser.

        ``-m <module>``: Get all the functions/methods in a module

        One or more ``-f`` or ``-m`` options are required to get any
        useful results.

        ``-D <filename>``: dump the raw statistics out to a pickle file
        on disk. The usual extension for this is ``.lprof``. These
        statistics may be viewed later by running
        ``python -m line_profiler``.

        ``-T <filename>``: dump the text-formatted statistics with the
        code side-by-side out to a text file.

        ``-r``: return the :py:class:`~.LineProfiler` object after it
        has completed profiling.

        ``-s``: strip out all entries from the print-out that have
        zeros. This is an old, soon-to-be-deprecated alias for ``-z``.

        ``-z``: strip out all entries from the print-out that have
        zeros.

        ``-u``: specify time unit for the print-out in seconds.
        """
        opts_def = Struct(D=[""], T=[""], f=[], m=[], u=None)
        parsed = self._parse_parameters(parameter_s, "rszf:m:D:T:u:", opts_def)
        if "s" in parsed.opts:  # Handle alias
            parsed.opts["z"] = True

        assert self.shell is not None
        global_ns = self.shell.user_global_ns
        local_ns = self.shell.user_ns

        # Get the requested functions.
        funcs = []
        for name in parsed.f:
            try:
                funcs.append(eval(name, global_ns, local_ns))
            except Exception as e:
                raise UsageError(
                    f"Could not find function {name}.\n{e.__class__.__name__}: {e}"
                )

        profile = LineProfiler(*funcs)

        # Get the modules, too
        for modname in parsed.m:
            try:
                mod = __import__(modname, fromlist=[""])
                profile.add_module(mod)
            except Exception as e:
                raise UsageError(
                    f"Could not find module {modname}.\n{e.__class__.__name__}: {e}"
                )

        with _PatchProfilerIntoBuiltins(profile):
            run = self._run_and_profile(
                profile, parsed, None, profile.runctx, parsed.arg_str,
                globals=global_ns, locals=local_ns)

        return self._handle_end(profile, run)

    @cell_magic
    def lprun_all(self, parameter_s="", cell=""):
        """Execute the whole notebook cell under the line-by-line
        profiler from the :py:mod:`line_profiler` module.

        Usage::

            %%lprun_all [<options>]

        By default, without the ``-p`` option, it includes nested
        functions in the profiler. The statistics will be shown
        side-by-side with the code through the pager once the statement
        has completed.

        Options:

        ``-D <filename>``: dump the raw statistics out to a pickle file
        on disk. The usual extension for this is ``.lprof``. These
        statistics may be viewed later by running
        ``python -m line_profiler``.

        ``-T <filename>``: dump the text-formatted statistics with the
        code side-by-side out to a text file.

        ``-r``: return the :py:class:`~.LineProfiler` object after it
        has completed profiling.

        ``-z``: strip out all entries from the print-out that have
        zeros. This is included for consistency with the CLI.

        ``-u``: specify time unit for the print-out in seconds.

        ``-t``: store the total time taken (in seconds) to a variable
        called ``_total_time_taken`` in your notebook. This can be
        useful if you want to plot the total time taken for different
        versions of a code cell without needing to manually look at and
        type down the time taken. This can be accomplished with ``-r``,
        but that would require a decent bit of boilerplate code and some
        knowledge of the timings data structure, so this is added to be
        beginner-friendly.

        ``-p``: Profile only top-level code (ignore nested functions).
        Using this can bypass any issues with :py:mod:`ast`
        transformations.
        """
        opts_def = Struct(D=[""], T=[""], u=None)
        parsed = self._parse_parameters(parameter_s, "rzptD:T:u:", opts_def)

        ip = get_ipython()
        if not cell.strip():  # Edge case
            cell = "..."

        # Write the cell to a temporary file so `show_text()` inside
        # `print_stats()` can open it.
        with tempfile.NamedTemporaryFile(
            suffix=".py", delete=False, mode="w", encoding="utf-8"
        ) as tf:
            tf.write(textwrap.dedent(cell).strip('\n'))

        try:
            if "p" not in parsed.opts:  # This is the default case.
                get_code = self._lprun_all_get_rewritten_profiled_code
            else:
                get_code = self._lprun_get_top_level_profiled_code
            # Inject a fresh LineProfiler into @profile.
            with _PatchProfilerIntoBuiltins() as prof:
                code = get_code(tf.name).replace(
                    co_name=_LPRUN_ALL_CODE_OBJ_NAME)
                try:
                    code = code.replace(
                        co_qualname=_LPRUN_ALL_CODE_OBJ_NAME)
                except TypeError:  # Python < 3.11
                    pass
                # "Register" the profiled code object with the profiler
                # Notes:
                # - This uses a dummy "function" object in a hacky way,
                #   but it's OK since `add_function()` ultimately only
                #   looks at the object's `.__code__` or
                #   `.__func__.__code__`.
                # - `prof.add_function()` might have replaced the code
                #   object, so retrieve it back from the dummy function
                mock_func = types.SimpleNamespace(__code__=code)
                prof.add_function(mock_func)  # type: ignore[arg-type]
                code = mock_func.__code__
                # Notes:
                # - We don't define `ip.user_global_ns` and `ip.user_ns`
                #   at the beginning like in lprun because the ns
                #   changes after the previous compile call.
                # - The method `._run_and_profile()` fetches the
                #   `LineProfiler.print_stats()` output before the
                #   `os.unlink()` below happens, allowing for transient
                #   items to be profiled.
                with prof:
                    run = self._run_and_profile(
                        prof, parsed, tf.name, exec, code,
                        # `globals` and `locals`
                        ip.user_global_ns, ip.user_ns)
        finally:
            os.unlink(tf.name)
        if "t" in parsed.opts:
            # I know it seems redundant to include this because users
            # could just use -r to get the info, but see the docstring
            # for why -t is included anyway.
            ip.user_ns["_total_time_taken"] = run.time_elapsed

        return self._handle_end(prof, run)

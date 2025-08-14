"""
This module defines the ``%lprun`` and ``%%lprun_all`` IPython magic functions.

If you are using IPython, there is an implementation of an %lprun magic command
which will let you specify functions to profile and a statement to execute. It
will also add its LineProfiler instance into the __builtins__, but typically,
you would not use it like that.

You can also use %%lprun_all, which profiles the whole cell you're executing
automagically, without needing to specify lines/functions yourself. It's meant
for easier use for beginners.

For IPython 0.11+, you can install it by editing the IPython configuration file
``~/.ipython/profile_default/ipython_config.py`` to add the ``'line_profiler'``
item to the extensions list::

    c.TerminalIPythonApp.extensions = [
        'line_profiler',
    ]

Or explicitly call::

    %load_ext line_profiler

To get usage help for %lprun and %%lprun_all, use the standard IPython help mechanism::

    In [1]: %lprun?
"""

import ast
import os
import tempfile
import textwrap
import time

from io import StringIO

from IPython.core.magic import Magics, magics_class, line_magic, cell_magic
from IPython.core.page import page
from IPython.utils.ipstruct import Struct
from IPython.core.error import UsageError

from .line_profiler import LineProfiler
from line_profiler.autoprofile.ast_tree_profiler import AstTreeProfiler
from line_profiler.autoprofile.ast_profile_transformer import AstProfileTransformer


# This is used for profiling all the code within a cell with lprun_all
class SkipWrapper(AstProfileTransformer):
    """
    AST Transformer that lets the base transformer add @profile everywhere, then
    removes it from the wrapper function only. Helps resolve issues where only top-level
    code would show timings.
    """

    def __init__(self, *args, wrapper_name, **kwargs):
        # Yes, I know these look like ChatGPT-generated docstrings, but I wrote them
        # in order to follow the format from ./autoprofile/ast_profile_transformer.py
        """Initialize the transformer.

        The base AstProfileTransformer is expected to add `@profile` to functions.
        This subclass remembers the name of the generated wrapper function so we can
        strip @profile off the wrapper function later because we only want to profile
        the code inside the wrapper, not the wrapper itself.

        Args:
            wrapper_name (str): The exact name of the wrapper function whose
                decorators should be cleaned.
            *args: Positional args forwarded to the parent transformer.
            **kwargs: Keyword args forwarded to the parent transformer.
        """
        super().__init__(*args, **kwargs)
        self._wrapper_name = wrapper_name

    def _strip_profile_from_decorators(self, node):
        """Remove any @profile decorator from a function node.

        Handles both the bare decorator form (@profile) and the callable form
        (@profile(...)). The node is modified in-place by filtering its
        decorator_list.

        Args:
            node (ast.FunctionDef | ast.AsyncFunctionDef): The function node to clean.

        Returns:
            ast.AST: The same node instance, with @profile-related decorators removed.
        """

        def keep(d):
            # Drop the decorator if it is exactly profile
            if isinstance(d, ast.Name) and d.id == "profile":
                return False
            # Drop calls like @profile(...) too
            if (
                isinstance(d, ast.Call)
                and isinstance(d.func, ast.Name)
                and d.func.id == "profile"
            ):
                return False
            # Keep the rest
            return True

        # Filter decorators in-place because NodeTransformer expects us to return the node
        node.decorator_list = [d for d in node.decorator_list if keep(d)]
        return node

    def visit_FunctionDef(self, node):
        """Visit a synchronous "def" function.

        We first delegate to the base transformer so it can apply its logic
        (e.g., adding @profile to functions). If the function happens to be the
        special wrapper (self._wrapper_name), we remove the `@profile` decorator
        from it so profiling reflects the code executed within the wrapper.

        Args:
            node (ast.FunctionDef): The function definition node.

        Returns:
            ast.FunctionDef: The possibly modified node.
        """
        node = super().visit_FunctionDef(node)
        if isinstance(node, ast.FunctionDef) and node.name == self._wrapper_name:
            node = self._strip_profile_from_decorators(node)
        return node

    # This isn't needed by our code because our _lprof_cell will never be async,
    # but it's included in case a user needs to make it async to work with their code
    def visit_AsyncFunctionDef(self, node):
        """Visit an asynchronous "async def" function.

        Mirrors visit_FunctionDef but for async functions. After the base
        transformer adds @profile, we remove it from the wrapper function if
        the names match.

        Args:
            node (ast.AsyncFunctionDef): The async function definition node.

        Returns:
            ast.AsyncFunctionDef: The possibly modified node.
        """
        node = super().visit_AsyncFunctionDef(node)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == self._wrapper_name:
            node = self._strip_profile_from_decorators(node)
        return node


@magics_class
class LineProfilerMagics(Magics):
    @line_magic
    def lprun(self, parameter_s=""):
        """Execute a statement under the line-by-line profiler from the
        line_profiler module.

        Usage:

            %lprun -f func1 -f func2 <statement>

        The given statement (which doesn't require quote marks) is run via the
        LineProfiler. Profiling is enabled for the functions specified by the -f
        options. The statistics will be shown side-by-side with the code through the
        pager once the statement has completed.

        Options:

        -f <function>: LineProfiler only profiles functions and methods it is told
        to profile.  This option tells the profiler about these functions. Multiple
        -f options may be used. The argument may be any expression that gives
        a Python function or method object. However, one must be careful to avoid
        spaces that may confuse the option parser.

        -m <module>: Get all the functions/methods in a module

        One or more -f or -m options are required to get any useful results.

        -D <filename>: dump the raw statistics out to a pickle file on disk. The
        usual extension for this is ".lprof". These statistics may be viewed later
        by running line_profiler.py as a script.

        -T <filename>: dump the text-formatted statistics with the code side-by-side
        out to a text file.

        -r: return the LineProfiler object after it has completed profiling.

        -s: strip out all entries from the print-out that have zeros.
        This is an old alias for -z.
        
        -z: strip out all entries from the print-out that have zeros.

        -u: specify time unit for the print-out in seconds.
        """

        # Escape quote markers.
        opts_def = Struct(D=[""], T=[""], f=[], m=[], u=None)
        parameter_s = parameter_s.replace('"', r"\"").replace("'", r"\'")
        opts, arg_str = self.parse_options(parameter_s, "rszf:m:D:T:u:", list_all=True)
        opts.merge(opts_def)

        global_ns = self.shell.user_global_ns
        local_ns = self.shell.user_ns

        # Get the requested functions.
        funcs = []
        for name in opts.f:
            try:
                funcs.append(eval(name, global_ns, local_ns))
            except Exception as e:
                raise UsageError(
                    f"Could not find module {name}.\n{e.__class__.__name__}: {e}"
                )

        profile = LineProfiler(*funcs)

        # Get the modules, too
        for modname in opts.m:
            try:
                mod = __import__(modname, fromlist=[""])
                profile.add_module(mod)
            except Exception as e:
                raise UsageError(
                    f"Could not find module {modname}.\n{e.__class__.__name__}: {e}"
                )

        if opts.u is not None:
            try:
                output_unit = float(opts.u[0])
            except Exception:
                raise TypeError("Timer unit setting must be a float.")
        else:
            output_unit = None

        # Add the profiler to the builtins for @profile.
        import builtins

        if "profile" in builtins.__dict__:
            had_profile = True
            old_profile = builtins.__dict__["profile"]
        else:
            had_profile = False
            old_profile = None
        builtins.__dict__["profile"] = profile

        try:
            try:
                profile.runctx(arg_str, global_ns, local_ns)
                message = ""
            except SystemExit:
                message = """*** SystemExit exception caught in code being profiled."""
            except KeyboardInterrupt:
                message = (
                    "*** KeyboardInterrupt exception caught in code being " "profiled."
                )
        finally:
            if had_profile:
                builtins.__dict__["profile"] = old_profile

        # Trap text output.
        stdout_trap = StringIO()
        profile.print_stats(
            stdout_trap, output_unit=output_unit, stripzeros=("s" in opts or "z" in opts)
        )
        output = stdout_trap.getvalue()
        output = output.rstrip()

        page(output)
        print(message, end="")

        dump_file = opts.D[0]
        if dump_file:
            profile.dump_stats(dump_file)
            print(f"\n*** Profile stats pickled to file {dump_file!r}. {message}")

        text_file = opts.T[0]
        if text_file:
            pfile = open(text_file, "w", encoding="utf-8")
            pfile.write(output)
            pfile.close()
            print(f"\n*** Profile printout saved to text file {text_file!r}. {message}")

        return_value = None
        if "r" in opts:
            return_value = profile

        return return_value

    @cell_magic
    def lprun_all(self, parameter_s="", cell=None):
        """Execute the whole notebook cell under the line-by-line profiler from the
        line_profiler module.

        Usage:

            %lprun_all <options>

        By default, without the -p option, it includes nested functions in the profiler.
        The statistics will be shown side-by-side with the code through the pager
        once the statement has completed.

        Options:

        -D <filename>: dump the raw statistics out to a pickle file on disk. The
        usual extension for this is ".lprof". These statistics may be viewed later
        by running line_profiler.py as a script.

        -T <filename>: dump the text-formatted statistics with the code side-by-side
        out to a text file.

        -r: return the LineProfiler object after it has completed profiling.

        -z: strip out all entries from the print-out that have zeros. Note: this is -s in
        lprun, however we use -z here for consistency with the CLI.

        -u: specify time unit for the print-out in seconds.

        -t: store the total time taken (in seconds) to a variable called
        `_total_time_taken` in your notebook. This can be useful if you want
        to plot the total time taken for different versions of a code cell without
        needing to manually look at and type down the time taken. This can be accomplished
        with -r, but that would require a decent bit of boilerplate code and some knowledge
        of the timings data structure, so this is added to be beginner-friendly.

        -p: Profile only top-level code (ignore nested functions). Using this can bypass
        any issues with ast transformation.
        """
        parameter_s = parameter_s.replace('"', r"\"").replace("'", r"\'")
        opts_def = Struct(D=[""], T=[""], u=None)
        opts, arg_str = self.parse_options(parameter_s, "rzptD:T:u:", list_all=True)
        opts.merge(opts_def)

        ip = get_ipython()
        fname = "_lprof_cell"

        # We have to encase the cell being profiled in an outer function if we want this to work.
        indented = textwrap.indent(cell, "    ")
        fsrc = f"def {fname}():\n{indented}"

        # Write the cell to a temporary file so show_text inside print_stats can open it.
        tf = tempfile.NamedTemporaryFile(
            suffix=".py", delete=False, mode="w", encoding="utf-8"
        )
        tf.write(fsrc)
        tf.flush()
        tf.close()

        if opts.u is not None:
            try:
                output_unit = float(opts.u[0])
            except Exception:
                raise TypeError("Timer unit setting must be a float.")
        else:
            output_unit = None

        # Add the profiler to the builtins for @profile.
        import builtins

        # This is the default case.
        if "p" not in opts:
            # Inject a fresh LineProfiler into @profile.
            profile = LineProfiler()
            had_profile = "profile" in builtins.__dict__
            oldp = builtins.__dict__.get("profile", None)
            builtins.__dict__["profile"] = profile

            # Run the AST transformer on the temp file, while skipping the wrapper function.
            at = AstTreeProfiler(
                tf.name,
                [tf.name],
                profile_imports=False,
                ast_transformer_class_handler=lambda *a, **k: SkipWrapper(
                    *a, wrapper_name=fname, **k
                ),
            )
            tree = at.profile()

            # Compile and exec that AST. This is similar to profiler.runctx,
            # but that doesn't support executing AST.
            code = compile(tree, tf.name, "exec")
            # We don't define ip.user_global_ns and ip.user_ns at the beginning like in lprun
            # because the ns changes after the previous compile call.
            exec(code, ip.user_global_ns, ip.user_ns)
            # Grab and call the wrapper so it actually runs under @profile.
            f = ip.user_ns.get(fname)
            if f is None:
                raise RuntimeError(f"No function {fname!r} defined after AST transform")
            profile.add_function(f)
            profile.enable_by_count()
            # Use the time module because it's easier than parsing the output from show_text.
            # perf_counter() is a monotonically increasing alternative to time() that's intended
            # for simple benchmarking.
            start_time = time.perf_counter()
            # If this goes in a try/finally block, the output goes blank :(
            try:
                f()
                message = ""
            except SystemExit:
                message = "*** SystemExit exception caught in code being profiled."
            except KeyboardInterrupt:
                message = (
                    "*** KeyboardInterrupt exception caught in code being profiled."
                )

            total_time = time.perf_counter() - start_time
            profile.disable_by_count()

            # Restore existing profiles in builtins.
            if had_profile:
                builtins.__dict__["profile"] = oldp
            else:
                del builtins.__dict__["profile"]

            # Trap text output.
            # I tried deduplicating this code by moving it outside of the if/else
            # but that ended up causing the line content output to go blank.
            trap = StringIO()
            profile.print_stats(trap, output_unit=output_unit, stripzeros="z" in opts)
            page(trap.getvalue())

            if "t" in opts:
                # I know it seems redundant to include this because users could just use -r
                # to get the info, but see the docstring for why -t is included anyway.
                ip.user_ns["_total_time_taken"] = total_time

            # Clean up temp file.
            os.unlink(tf.name)
        else:
            # Compile and define the function from that file.
            code = compile(fsrc, tf.name, "exec")
            # We don't define ip.user_global_ns and ip.user_ns at the beginning like in lprun
            # because the ns changes after the previous compile call.
            exec(code, ip.user_global_ns, ip.user_ns)

            f = ip.user_ns[fname]
            profile = LineProfiler(f)

            if "profile" in builtins.__dict__:
                had_profile = True
                old_profile = builtins.__dict__["profile"]
            else:
                had_profile = False
                old_profile = None
            builtins.__dict__["profile"] = profile

            # Use the time module because it's easier than parsing the output from show_text.
            start_time = time.perf_counter()
            try:
                try:
                    profile.runcall(f)
                    message = ""
                except SystemExit:
                    message = "*** SystemExit exception caught in code being profiled."
                except KeyboardInterrupt:
                    message = (
                        "*** KeyboardInterrupt exception caught in code being profiled."
                    )
            finally:
                # Restore any previous @profile.
                if had_profile:
                    builtins.__dict__["profile"] = old_profile
                else:
                    del builtins.__dict__["profile"]
            # Capture and save total runtime.
            total_time = time.perf_counter() - start_time
            if "t" in opts:
                ip.user_ns["_total_time_taken"] = total_time

            # Trap text output.
            stdout_trap = StringIO()
            profile.print_stats(
                stdout_trap, output_unit=output_unit, stripzeros="z" in opts
            )
            output = stdout_trap.getvalue()
            output = output.rstrip()

            page(output)
            print(message, end="")

        dump_file = opts.D[0]
        if dump_file:
            profile.dump_stats(dump_file)
            print(f"\n*** Profile stats pickled to file {dump_file!r}. {message}")

        text_file = opts.T[0]
        if text_file:
            pfile = open(text_file, "w", encoding="utf-8")
            pfile.write(output)
            pfile.close()
            print(f"\n*** Profile printout saved to text file {text_file!r}. {message}")

        return_value = None
        if "r" in opts:
            return_value = profile

        return return_value

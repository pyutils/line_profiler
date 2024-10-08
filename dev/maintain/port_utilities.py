"""
Statically port utilities from ubelt and xdocest need for the autoprofile
features.

Similar Scripts:
    ~/code/xdoctest/dev/maintain/port_ubelt_utils.py
    ~/code/mkinit/dev/maintain/port_ubelt_code.py
    ~/code/line_profiler/dev/maintain/port_utilities.py
"""
import ubelt as ub
import liberator
import re


def generate_util_static():
    lib = liberator.Liberator(verbose=0)

    import ubelt
    import xdoctest

    if 1:
        lib.add_dynamic(ubelt.util_import.modpath_to_modname)
        lib.add_dynamic(ubelt.util_import.modname_to_modpath)
        lib.expand(['ubelt'])

    if 1:
        lib.add_dynamic(xdoctest.static_analysis.package_modpaths)
        lib.expand(['xdoctest'])

        # # Hack because ubelt and xdoctest define this
        del lib.body_defs['xdoctest.utils.util_import._platform_pylib_exts']

    # lib.expand(['ubelt', 'xdoctest'])
    text = lib.current_sourcecode()

    """
    pip install rope
    pip install parso
    """

    prefix = ub.codeblock(
        '''
        """
        This file was autogenerated based on code in :py:mod:`ubelt` and
        :py:mod:`xdoctest` via dev/maintain/port_utilities.py in the
        line_profiler repo.
        """
        ''')

    # Remove doctest references to ubelt
    new_lines = []
    for line in text.split('\n'):
        if line.strip().startswith('>>> from ubelt'):
            continue
        if line.strip().startswith('>>> import ubelt as ub'):
            line = re.sub('>>> .*', '>>> # xdoctest: +SKIP("ubelt dependency")', line)
        new_lines.append(line)

    text = '\n'.join(new_lines)
    text = prefix + '\n' + text + '\n'
    return text


def main():
    text = generate_util_static()
    print(ub.highlight_code(text, backend='rich'))

    import parso
    import line_profiler
    target_fpath = ub.Path(line_profiler.__file__).parent / 'autoprofile' / 'util_static.py'

    new_module = parso.parse(text)
    if target_fpath.exists():
        old_module = parso.parse(target_fpath.read_text())
        new_names = [child.name.value for child in new_module.children if child.type in {'funcdef', 'classdef'}]
        old_names = [child.name.value for child in old_module.children if child.type in {'funcdef', 'classdef'}]
        print(set(old_names) - set(new_names))
        print(set(new_names) - set(old_names))

    target_fpath.write_text(text)

    # Fixup formatting
    if 1:
        ub.cmd(['black', target_fpath])


if __name__ == '__main__':
    """
    CommandLine:
        python ~/code/line_profiler/dev/maintain/port_utilities.py
    """
    main()

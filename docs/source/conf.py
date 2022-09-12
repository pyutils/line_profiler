"""
Notes:
    http://docs.readthedocs.io/en/latest/getting_started.html

    pip install sphinx sphinx-autobuild sphinx_rtd_theme sphinxcontrib-napoleon

    cd ~/code/line_profiler
    mkdir docs
    cd docs

    sphinx-quickstart

    # need to edit the conf.py

    cd ~/code/line_profiler/docs
    sphinx-apidoc -f -o ~/code/line_profiler/docs/source ~/code/line_profiler/line_profiler --separate
    make html

    Also:
        To turn on PR checks

        https://docs.readthedocs.io/en/stable/guides/autobuild-docs-for-pull-requests.html

        https://readthedocs.org/dashboard/line_profiler/advanced/

        ensure your github account is connected to readthedocs
        https://readthedocs.org/accounts/social/connections/

        ### For gitlab

        The user will need to enable the repo on their readthedocs account:
        https://readthedocs.org/dashboard/import/manual/?

        To enable the read-the-docs go to https://readthedocs.org/dashboard/ and login

        Make sure you have a .readthedocs.yml file

        Click import project: (for github you can select, but gitlab you need to import manually)
            Set the Repository NAME: $REPO_NAME
            Set the Repository URL: $REPO_URL

        For gitlab you also need to setup an integrations and add gitlab
        incoming webhook Then go to $REPO_URL/hooks and add the URL

        Will also need to activate the main branch:
            https://readthedocs.org/projects/line_profiler/versions/
"""
#
# Configuration file for the Sphinx documentation builder.
#
# This file does only contain a selection of the most common options. For a
# full list see the documentation:
# http://www.sphinx-doc.org/en/stable/config

# -- Path setup --------------------------------------------------------------

# If extensions (or modules to document with autodoc) are in another directory,
# add these directories to sys.path here. If the directory is relative to the
# documentation root, use os.path.abspath to make it absolute, like shown here.
#
# import os
# import sys
# sys.path.insert(0, os.path.abspath('.'))


# -- Project information -----------------------------------------------------
import sphinx_rtd_theme
from os.path import exists
from os.path import dirname
from os.path import join


def parse_version(fpath):
    """
    Statically parse the version number from a python file
    """
    import ast
    if not exists(fpath):
        raise ValueError('fpath={!r} does not exist'.format(fpath))
    with open(fpath, 'r') as file_:
        sourcecode = file_.read()
    pt = ast.parse(sourcecode)
    class VersionVisitor(ast.NodeVisitor):
        def visit_Assign(self, node):
            for target in node.targets:
                if getattr(target, 'id', None) == '__version__':
                    self.version = node.value.s
    visitor = VersionVisitor()
    visitor.visit(pt)
    return visitor.version

project = 'line_profiler'
copyright = '2022, Jon Crall'
author = 'Jon Crall'
modname = 'line_profiler'

modpath = join(dirname(dirname(dirname(__file__))), modname, '__init__.py')
release = parse_version(modpath)
version = '.'.join(release.split('.')[0:2])


# -- General configuration ---------------------------------------------------

# If your documentation needs a minimal Sphinx version, state it here.
#
# needs_sphinx = '1.0'

# Add any Sphinx extension module names here, as strings. They can be
# extensions coming with Sphinx (named 'sphinx.ext.*') or your custom
# ones.
extensions = [
    'sphinx.ext.autodoc',
    'sphinx.ext.viewcode',
    'sphinx.ext.napoleon',
    'sphinx.ext.intersphinx',
    'sphinx.ext.todo',
    'sphinx.ext.autosummary',
    # 'myst_parser',  # TODO
]

todo_include_todos = True
napoleon_google_docstring = True
napoleon_use_param = False
napoleon_use_ivar = True

autodoc_inherit_docstrings = False

autodoc_member_order = 'bysource'
# autodoc_mock_imports = ['torch', 'torchvision', 'visdom']

intersphinx_mapping = {
    # 'pytorch': ('http://pytorch.org/docs/master/', None),
    'python': ('https://docs.python.org/3', None),
    'click': ('https://click.palletsprojects.com/', None),
    # 'xxhash': ('https://pypi.org/project/xxhash/', None),
    # 'pygments': ('https://pygments.org/docs/', None),
    # 'tqdm': ('https://tqdm.github.io/', None),
    # Requries that the repo have objects.inv
    'kwarray': ('https://kwarray.readthedocs.io/en/latest/', None),
    'kwimage': ('https://kwimage.readthedocs.io/en/latest/', None),
    # 'kwplot': ('https://kwplot.readthedocs.io/en/latest/', None),
    'ndsampler': ('https://ndsampler.readthedocs.io/en/latest/', None),
    'ubelt': ('https://ubelt.readthedocs.io/en/latest/', None),
    'xdoctest': ('https://xdoctest.readthedocs.io/en/latest/', None),
    'networkx': ('https://networkx.org/documentation/stable/', None),
    'scriptconfig': ('https://scriptconfig.readthedocs.io/en/latest/', None),

}
__dev_note__ = """
python -m sphinx.ext.intersphinx https://docs.python.org/3/objects.inv
python -m sphinx.ext.intersphinx https://kwcoco.readthedocs.io/en/latest/objects.inv
python -m sphinx.ext.intersphinx https://networkx.org/documentation/stable/objects.inv
python -m sphinx.ext.intersphinx https://kwarray.readthedocs.io/en/latest/objects.inv
python -m sphinx.ext.intersphinx https://kwimage.readthedocs.io/en/latest/objects.inv
python -m sphinx.ext.intersphinx https://ubelt.readthedocs.io/en/latest/objects.inv
python -m sphinx.ext.intersphinx https://networkx.org/documentation/stable/objects.inv
"""


# Add any paths that contain templates here, relative to this directory.
templates_path = ['_templates']

# The suffix(es) of source filenames.
# You can specify multiple suffix as a list of string:
#
source_suffix = ['.rst', '.md']

# The master toctree document.
master_doc = 'index'

# The language for content autogenerated by Sphinx. Refer to documentation
# for a list of supported languages.
#
# This is also used if you do content translation via gettext catalogs.
# Usually you set "language" from the command line for these cases.
language = 'en'

# List of patterns, relative to source directory, that match files and
# directories to ignore when looking for source files.
# This pattern also affects html_static_path and html_extra_path .
exclude_patterns = []

# The name of the Pygments (syntax highlighting) style to use.
pygments_style = 'sphinx'


# -- Options for HTML output -------------------------------------------------

# The theme to use for HTML and HTML Help pages.  See the documentation for
# a list of builtin themes.
#
html_theme = 'sphinx_rtd_theme'
html_theme_path = [sphinx_rtd_theme.get_html_theme_path()]

# Theme options are theme-specific and customize the look and feel of a theme
# further.  For a list of options available for each theme, see the
# documentation.
#
html_theme_options = {
    'collapse_navigation': False,
    'display_version': True,
    # 'logo_only': True,
}
# html_logo = '.static/line_profiler.svg'
# html_favicon = '.static/line_profiler.ico'

# Add any paths that contain custom static files (such as style sheets) here,
# relative to this directory. They are copied after the builtin static files,
# so a file named "default.css" will overwrite the builtin "default.css".
html_static_path = ['_static']

# Custom sidebar templates, must be a dictionary that maps document names
# to template names.
#
# The default sidebars (for documents that don't match any pattern) are
# defined by theme itself.  Builtin themes are using these templates by
# default: ``['localtoc.html', 'relations.html', 'sourcelink.html',
# 'searchbox.html']``.
#
# html_sidebars = {}


# -- Options for HTMLHelp output ---------------------------------------------

# Output file base name for HTML help builder.
htmlhelp_basename = 'line_profilerdoc'


# -- Options for LaTeX output ------------------------------------------------

latex_elements = {
    # The paper size ('letterpaper' or 'a4paper').
    #
    # 'papersize': 'letterpaper',

    # The font size ('10pt', '11pt' or '12pt').
    #
    # 'pointsize': '10pt',

    # Additional stuff for the LaTeX preamble.
    #
    # 'preamble': '',

    # Latex figure (float) alignment
    #
    # 'figure_align': 'htbp',
}

# Grouping the document tree into LaTeX files. List of tuples
# (source start file, target name, title,
#  author, documentclass [howto, manual, or own class]).
latex_documents = [
    (master_doc, 'line_profiler.tex', 'line_profiler Documentation',
     'Jon Crall', 'manual'),
]


# -- Options for manual page output ------------------------------------------

# One entry per manual page. List of tuples
# (source start file, name, description, authors, manual section).
man_pages = [
    (master_doc, 'line_profiler', 'line_profiler Documentation',
     [author], 1)
]


# -- Options for Texinfo output ----------------------------------------------

# Grouping the document tree into Texinfo files. List of tuples
# (source start file, target name, title, author,
#  dir menu entry, description, category)
texinfo_documents = [
    (master_doc, 'line_profiler', 'line_profiler Documentation',
     author, 'line_profiler', 'One line description of project.',
     'Miscellaneous'),
]


# -- Extension configuration -------------------------------------------------


from sphinx.domains.python import PythonDomain  # NOQA
# from sphinx.application import Sphinx  # NOQA
from typing import Any, List  # NOQA


class PatchedPythonDomain(PythonDomain):
    """
    References:
        https://github.com/sphinx-doc/sphinx/issues/3866
    """
    def resolve_xref(self, env, fromdocname, builder, typ, target, node, contnode):
        # TODO: can use this to resolve references nicely
        # if target.startswith('ub.'):
        #     target = 'ubelt.' + target[3]
        return_value = super(PatchedPythonDomain, self).resolve_xref(
            env, fromdocname, builder, typ, target, node, contnode)
        return return_value


def process(app, what_: str, name: str, obj: Any, options: Any, lines:
            List[str]) -> None:
    """
    Custom process to transform docstring lines Remove "Ignore" blocks

    Args:
        app (sphinx.application.Sphinx): the Sphinx application object

        what (str):
            the type of the object which the docstring belongs to (one of
            "module", "class", "exception", "function", "method", "attribute")

        name (str): the fully qualified name of the object

        obj: the object itself

        options: the options given to the directive: an object with
            attributes inherited_members, undoc_members, show_inheritance
            and noindex that are true if the flag option of same name was
            given to the auto directive

        lines (List[str]): the lines of the docstring, see above

    References:
        https://www.sphinx-doc.org/en/1.5.1/_modules/sphinx/ext/autodoc.html
        https://www.sphinx-doc.org/en/master/usage/extensions/autodoc.html
    """
    # if what and what_ not in what:
    #     return
    orig_lines = lines[:]

    # text = '\n'.join(lines)
    # if 'Example' in text and 'CommandLine' in text:
    #     import xdev
    #     xdev.embed()

    ignore_tags = tuple(['Ignore'])

    mode = None
    # buffer = None
    new_lines = []
    for i, line in enumerate(orig_lines):

        # See if the line triggers a mode change
        if line.startswith(ignore_tags):
            mode = 'ignore'
        elif line.startswith('CommandLine'):
            mode = 'cmdline'
        elif line and not line.startswith(' '):
            # if the line startswith anything but a space, we are no
            # longer in the previous nested scope
            mode = None

        if mode is None:
            new_lines.append(line)
        elif mode == 'ignore':
            # print('IGNORE line = {!r}'.format(line))
            pass
        elif mode == 'cmdline':
            if line.startswith('CommandLine'):
                new_lines.append('.. rubric:: CommandLine')
                new_lines.append('')
                new_lines.append('.. code-block:: bash')
                new_lines.append('')
                # new_lines.append('    # CommandLine')
            else:
                # new_lines.append(line.strip())
                new_lines.append(line)
        else:
            raise KeyError(mode)

    lines[:] = new_lines
    # make sure there is a blank line at the end
    if lines and lines[-1]:
        lines.append('')


def setup(app):
    app.add_domain(PatchedPythonDomain, override=True)
    if 1:
        # New Way
        # what = None
        app.connect('autodoc-process-docstring', process)
    else:
        # OLD WAY
        # https://stackoverflow.com/questions/26534184/can-sphinx-ignore-certain-tags-in-python-docstrings
        # Register a sphinx.ext.autodoc.between listener to ignore everything
        # between lines that contain the word IGNORE
        # from sphinx.ext.autodoc import between
        # app.connect('autodoc-process-docstring', between('^ *Ignore:$', exclude=True))
        pass
    return app

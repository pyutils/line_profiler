Releasing a New Version
=======================

The github action to push to PYPI will trigger on the push of a new tag.  By
convention we tag each release as ``v{__version__}``, where ``{__version__}``
is the current ``__version__`` number in ``line_profiler/__init__.py``.

The ``./publish.sh`` script handles the creation of the version tag and pushing
it to github, at which point the github action will build wheels for all
supported operating systems and machine architectures.


The steps are as follows:

1. Run ``./publish.sh``. 

2. When given the prompt, ``Do you want to git tag and push version='{}'?``,
   confirm the new version looks correct and respond with "yes".

3. When asked: ``do you need to build wheels?`` Respond "no". (The CI will take
   care of this). 

4. When asked: ``Are you ready to directly publish version xxx?`` Answer no.
   Again, the CI will do this.

5. The script will summarize its actions. Double check them, then press enter
   to create and push the new release tag.


These options can be programatically given for a non-interactive interface. See
the ``publish.sh`` script for details (and make a PR that adds that information
here).


Notes on Signed Releases
========================

The "dev" folder contains encrypted GPG keys used to sign the wheels on the CI.
The CI is given a secret variable which is the key to decrypt them.

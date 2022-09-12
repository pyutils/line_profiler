#!/bin/bash
__doc__='''
Script to publish a new version of this library on PyPI. 

If your script has binary dependencies then we assume that you have built a
proper binary wheel with auditwheel and it exists in the wheelhouse directory.
Otherwise, for source tarballs and wheels this script runs the
setup.py script to create the wheels as well.

Running this script with the default arguments will perform any builds and gpg
signing, but nothing will be uploaded to pypi unless the user explicitly sets
DO_UPLOAD=True or answers yes to the prompts.

Args:
    TWINE_USERNAME (str) : 
        username for pypi. This must be set if uploading to pypi.
        Defaults to "".

    TWINE_PASSWORD (str) : 
        password for pypi. This must be set if uploading to pypi.
        Defaults to "".

    DO_GPG (bool) : 
        If True, sign the packages with a GPG key specified by `GPG_KEYID`.
        defaults to auto.

    DO_UPLOAD (bool) : 
        If True, upload the packages to the pypi server specified by
        `TWINE_REPOSITORY_URL`.

    DO_BUILD (bool) : 
        If True, will execute the setup.py build script, which is
        expected to use setuptools. In the future we may add support for other
        build systems. If False, this script will expect the pre-built packages
        to exist in "wheelhouse/{NAME}-{VERSION}-{SUFFIX}.{EXT}".

        Defaults to "auto". 

    DO_TAG (bool) : 
        if True, will "git tag" the current HEAD with 

    TWINE_REPOSITORY_URL (url) : 
         The URL of the pypi server to upload to. 
         Defaults to "auto", which if on the release branch, this will default
         to the live pypi server `https://upload.pypi.org/legacy` otherwise
         this will default to the test.pypi server:
         `https://test.pypi.org/legacy`

     GPG_KEYID (str) :
        The keyid of the gpg key to sign with. (if DO_GPG=True). Defaults to
        the local git config user.signingkey

    DEPLOY_REMOTE (str) : 
        The git remote to push any tags to. Defaults to "origin"

    GPG_EXECUTABLE (path) : 
        Path to the GPG executable. 
        Defaults to "auto", which chooses "gpg2" if it exists, otherwise "gpg".

    MODE (str):
        Can be pure, binary, or all. Defaults to pure unless a CMakeLists.txt
        exists in which case it defaults to binary.

Requirements:
     twine >= 1.13.0
     gpg2 >= 2.2.4
     OpenSSL >= 1.1.1c

Notes:
    # NEW API TO UPLOAD TO PYPI
    # https://docs.travis-ci.com/user/deployment/pypi/
    # https://packaging.python.org/tutorials/distributing-packages/
    # https://stackoverflow.com/questions/45188811/how-to-gpg-sign-a-file-that-is-built-by-travis-ci

    Based on template in

    # github.com/Erotemic/xcookie/
    ~/code/xcookie/publish.sh

Usage:
    load_secrets
    # TODO: set a trap to unload secrets?
    cd <YOUR REPO>
    # Set your variables or load your secrets
    export TWINE_USERNAME=<pypi-username>
    export TWINE_PASSWORD=<pypi-password>
    TWINE_REPOSITORY_URL="https://test.pypi.org/legacy/" 
'''

DEBUG=${DEBUG:=''}
if [[ "${DEBUG}" != "" ]]; then
    set -x
fi

check_variable(){
    KEY=$1
    HIDE=$2
    VAL=${!KEY}
    if [[ "$HIDE" == "" ]]; then
        echo "[DEBUG] CHECK VARIABLE: $KEY=\"$VAL\""
    else
        echo "[DEBUG] CHECK VARIABLE: $KEY=<hidden>"
    fi
    if [[ "$VAL" == "" ]]; then
        echo "[ERROR] UNSET VARIABLE: $KEY=\"$VAL\""
        exit 1;
    fi
}


normalize_boolean(){
    ARG=$1
    ARG=$(echo "$ARG" | awk '{print tolower($0)}')
    if [ "$ARG" = "true" ] || [ "$ARG" = "1" ] || [ "$ARG" = "yes" ] || [ "$ARG" = "on" ]; then
        echo "True"
    elif [ "$ARG" = "false" ] || [ "$ARG" = "0" ] || [ "$ARG" = "no" ] || [ "$ARG" = "off" ]; then
        echo "False"
    else
        echo "$ARG"
    fi
}


####
# Parameters
###

# Options
DEPLOY_REMOTE=${DEPLOY_REMOTE:=origin}
NAME=${NAME:=$(python -c "import setup; print(setup.NAME)")}
VERSION=$(python -c "import setup; print(setup.VERSION)")

check_variable DEPLOY_REMOTE

ARG_1=$1

DO_UPLOAD=${DO_UPLOAD:=$ARG_1}
DO_TAG=${DO_TAG:=$ARG_1}

DO_GPG=${DO_GPG:="auto"}
# Verify that we want to build
if [ "$DO_GPG" == "auto" ]; then
    DO_GPG="True"
fi

DO_BUILD=${DO_BUILD:="auto"}
# Verify that we want to build
if [ "$DO_BUILD" == "auto" ]; then
    DO_BUILD="True"
fi

DO_GPG=$(normalize_boolean "$DO_GPG")
DO_BUILD=$(normalize_boolean "$DO_BUILD")
DO_UPLOAD=$(normalize_boolean "$DO_UPLOAD")
DO_TAG=$(normalize_boolean "$DO_TAG")

TWINE_USERNAME=${TWINE_USERNAME:=""}
TWINE_PASSWORD=${TWINE_PASSWORD:=""}

DEFAULT_TEST_TWINE_REPO_URL="https://test.pypi.org/legacy/"
DEFAULT_LIVE_TWINE_REPO_URL="https://upload.pypi.org/legacy/"

TWINE_REPOSITORY_URL=${TWINE_REPOSITORY_URL:="auto"}
if [[ "${TWINE_REPOSITORY_URL}" == "auto" ]]; then
    #if [[ "$(cat .git/HEAD)" != "ref: refs/heads/release" ]]; then 
    #    # If we are not on release, then default to the test pypi upload repo
    #    TWINE_REPOSITORY_URL=${TWINE_REPOSITORY_URL:="https://test.pypi.org/legacy/"}
    #else
    if [[ "$DEBUG" == "" ]]; then
        TWINE_REPOSITORY_URL="live"
    else
        TWINE_REPOSITORY_URL="test"
    fi
fi

if [[ "${TWINE_REPOSITORY_URL}" == "live" ]]; then
    TWINE_REPOSITORY_URL=$DEFAULT_LIVE_TWINE_REPO_URL
elif [[ "${TWINE_REPOSITORY_URL}" == "test" ]]; then
    TWINE_REPOSITORY_URL=$DEFAULT_TEST_TWINE_REPO_URL
fi

GPG_EXECUTABLE=${GPG_EXECUTABLE:="auto"}
if [[ "$GPG_EXECUTABLE" == "auto" ]]; then
    if [[ "$(which gpg2)" != "" ]]; then
        GPG_EXECUTABLE="gpg2"
    else
        GPG_EXECUTABLE="gpg"
    fi
fi

GPG_KEYID=${GPG_KEYID:="auto"}
if [[ "$GPG_KEYID" == "auto" ]]; then
    GPG_KEYID=$(git config --local user.signingkey)
    if [[ "$GPG_KEYID" == "" ]]; then
        GPG_KEYID=$(git config --global user.signingkey)
    fi
fi


if [ -f CMakeLists.txt ] ; then
    DEFAULT_MODE="binary"
else
    DEFAULT_MODE="pure"
fi


# TODO: parameterize
# The default should change depending on the application
MODE=${MODE:=$DEFAULT_MODE}
if [[ "$MODE" == "all" ]]; then
    MODE_LIST=("sdist" "native" "bdist")
elif [[ "$MODE" == "pure" ]]; then
    MODE_LIST=("sdist" "native")
elif [[ "$MODE" == "binary" ]]; then
    MODE_LIST=("sdist" "bdist")
else
    MODE_LIST=("$MODE")
fi
MODE_LIST_STR=$(printf '"%s" ' "${MODE_LIST[@]}")
#echo "MODE_LIST_STR = $MODE_LIST_STR"


####
# Logic
###

WAS_INTERACTION="False"

echo "
=== PYPI BUILDING SCRIPT ==
NAME='$NAME'
VERSION='$VERSION'
TWINE_USERNAME='$TWINE_USERNAME'
TWINE_REPOSITORY_URL = $TWINE_REPOSITORY_URL
GPG_KEYID = '$GPG_KEYID'

DO_UPLOAD=${DO_UPLOAD}
DO_TAG=${DO_TAG}
DO_GPG=${DO_GPG}
DO_BUILD=${DO_BUILD}
MODE_LIST_STR=${MODE_LIST_STR}
"


# Verify that we want to tag
if [[ "$DO_TAG" == "True" ]]; then
    echo "About to tag VERSION='$VERSION'" 
else
    if [[ "$DO_TAG" == "False" ]]; then
        echo "We are NOT about to tag VERSION='$VERSION'" 
    else
        # shellcheck disable=SC2162
        read -p "Do you want to git tag and push version='$VERSION'? (input 'yes' to confirm)" ANS
        echo "ANS = $ANS"
        WAS_INTERACTION="True"
        DO_TAG="$ANS"
        DO_TAG=$(normalize_boolean "$DO_TAG")
        if [ "$DO_BUILD" == "auto" ]; then
            DO_BUILD=""
            DO_GPG=""
        fi
    fi
fi



if [[ "$DO_BUILD" == "True" ]]; then
    echo "About to build wheels"
else
    if [[ "$DO_BUILD" == "False" ]]; then
        echo "We are NOT about to build wheels"
    else
        # shellcheck disable=SC2162
        read -p "Do you need to build wheels? (input 'yes' to confirm)" ANS
        echo "ANS = $ANS"
        WAS_INTERACTION="True"
        DO_BUILD="$ANS"
        DO_BUILD=$(normalize_boolean "$DO_BUILD")
    fi
fi


# Verify that we want to publish
if [[ "$DO_UPLOAD" == "True" ]]; then
    echo "About to directly publish VERSION='$VERSION'" 
else
    if [[ "$DO_UPLOAD" == "False" ]]; then
        echo "We are NOT about to directly publish VERSION='$VERSION'" 
    else
        # shellcheck disable=SC2162
        read -p "Are you ready to directly publish version='$VERSION'? ('yes' will twine upload)" ANS
        echo "ANS = $ANS"
        WAS_INTERACTION="True"
        DO_UPLOAD="$ANS"
        DO_UPLOAD=$(normalize_boolean "$DO_UPLOAD")
    fi
fi


if [[ "$WAS_INTERACTION" == "True" ]]; then
    echo "
    === PYPI BUILDING SCRIPT ==
    VERSION='$VERSION'
    TWINE_USERNAME='$TWINE_USERNAME'
    TWINE_REPOSITORY_URL = $TWINE_REPOSITORY_URL
    GPG_KEYID = '$GPG_KEYID'

    DO_UPLOAD=${DO_UPLOAD}
    DO_TAG=${DO_TAG}
    DO_GPG=${DO_GPG}
    DO_BUILD=${DO_BUILD}
    MODE_LIST_STR='${MODE_LIST_STR}'
    "
    # shellcheck disable=SC2162
    read -p "Look good? Ready to build? Enter any text to continue" ANS
fi



if [ "$DO_BUILD" == "True" ]; then

    echo "
    === <BUILD WHEEL> ===
    "

    echo "LIVE BUILDING"
    # Build wheel and source distribution
    for _MODE in "${MODE_LIST[@]}"
    do
        echo "_MODE = $_MODE"
        if [[ "$_MODE" == "sdist" ]]; then
            python setup.py sdist || { echo 'failed to build sdist wheel' ; exit 1; }
        elif [[ "$_MODE" == "native" ]]; then
            python setup.py bdist_wheel || { echo 'failed to build native wheel' ; exit 1; }
        elif [[ "$_MODE" == "bdist" ]]; then
            echo "Assume wheel has already been built"
        else
            echo "ERROR: bad mode"
            exit 1
        fi
    done

    echo "
    === <END BUILD WHEEL> ===
    "

else
    echo "DO_BUILD=False, Skipping build"
fi


ls_array(){
    __doc__='
    Read the results of a glob pattern into an array

    Args:
        arr_name
        glob_pattern

    Example:
        arr_name="myarray"
        glob_pattern="*"
        pass
    '
    local arr_name="$1"
    local glob_pattern="$2"
    shopt -s nullglob
    # shellcheck disable=SC2206
    array=($glob_pattern)
    shopt -u nullglob # Turn off nullglob to make sure it doesn't interfere with anything later
    # FIXME; for some reason this doesnt always work properly
    # Copy the array into the dynamically named variable
    # shellcheck disable=SC2086
    readarray -t $arr_name < <(printf '%s\n' "${array[@]}")
}


WHEEL_PATHS=()
for _MODE in "${MODE_LIST[@]}"
do
    if [[ "$_MODE" == "sdist" ]]; then
        ls_array "_NEW_WHEEL_PATHS" "dist/${NAME}-${VERSION}*.tar.gz"
    elif [[ "$_MODE" == "native" ]]; then
        ls_array "_NEW_WHEEL_PATHS" "dist/${NAME}-${VERSION}*.whl"
    elif [[ "$_MODE" == "bdist" ]]; then
        ls_array "_NEW_WHEEL_PATHS" "wheelhouse/${NAME}-${VERSION}-*.whl"
    else
        echo "ERROR: bad mode"
        exit 1
    fi
    # hacky CONCAT because for some reason ls_array will return 
    # something that looks empty but has one empty element
    for new_item in "${_NEW_WHEEL_PATHS[@]}"
    do
        if [[ "$new_item" != "" ]]; then
            WHEEL_PATHS+=("$new_item")
        fi
    done
done

# Dedup the paths
readarray -t WHEEL_PATHS < <(printf '%s\n' "${WHEEL_PATHS[@]}" | sort -u)

WHEEL_PATHS_STR=$(printf '"%s" ' "${WHEEL_PATHS[@]}")
echo "WHEEL_PATHS_STR = $WHEEL_PATHS_STR"

echo "
MODE=$MODE
VERSION='$VERSION'
WHEEL_PATHS='$WHEEL_PATHS_STR'
"



if [ "$DO_GPG" == "True" ]; then

    echo "
    === <GPG SIGN> ===
    "

    for WHEEL_PATH in "${WHEEL_PATHS[@]}"
    do
        echo "WHEEL_PATH = $WHEEL_PATH"
        check_variable WHEEL_PATH
            # https://stackoverflow.com/questions/45188811/how-to-gpg-sign-a-file-that-is-built-by-travis-ci
            # secure gpg --export-secret-keys > all.gpg

            # REQUIRES GPG >= 2.2
            check_variable GPG_EXECUTABLE || { echo 'failed no gpg exe' ; exit 1; }
            check_variable GPG_KEYID || { echo 'failed no gpg key' ; exit 1; }

            echo "Signing wheels"
            GPG_SIGN_CMD="$GPG_EXECUTABLE --batch --yes --detach-sign --armor --local-user $GPG_KEYID"
            echo "GPG_SIGN_CMD = $GPG_SIGN_CMD"
            $GPG_SIGN_CMD --output "$WHEEL_PATH".asc "$WHEEL_PATH"

            echo "Checking wheels"
            twine check "$WHEEL_PATH".asc "$WHEEL_PATH" || { echo 'could not check wheels' ; exit 1; }

            echo "Verifying wheels"
            $GPG_EXECUTABLE --verify "$WHEEL_PATH".asc "$WHEEL_PATH" || { echo 'could not verify wheels' ; exit 1; }
    done
    echo "
    === <END GPG SIGN> ===
    "
else
    echo "DO_GPG=False, Skipping GPG sign"
fi


if [[ "$DO_TAG" == "True" ]]; then
    TAG_NAME="v${VERSION}"
    # if we messed up we can delete the tag
    # git push origin :refs/tags/$TAG_NAME
    # and then tag with -f
    # 
    git tag "$TAG_NAME" -m "tarball tag $VERSION"
    git push --tags $DEPLOY_REMOTE
    echo "Should also do a: git push $DEPLOY_REMOTE main:release"
    echo "For github should draft a new release: https://github.com/PyUtils/line_profiler/releases/new"
else
    echo "Not tagging"
fi


if [[ "$DO_UPLOAD" == "True" ]]; then
    check_variable TWINE_USERNAME
    check_variable TWINE_PASSWORD "hide"

    for WHEEL_PATH in "${WHEEL_PATHS[@]}"
    do
        if [ "$DO_GPG" == "True" ]; then
            twine upload --username "$TWINE_USERNAME" --password=$TWINE_PASSWORD  \
                --repository-url "$TWINE_REPOSITORY_URL" \
                --sign "$WHEEL_PATH".asc "$WHEEL_PATH" --skip-existing --verbose || { echo 'failed to twine upload' ; exit 1; }
        else
            twine upload --username "$TWINE_USERNAME" --password=$TWINE_PASSWORD \
                --repository-url "$TWINE_REPOSITORY_URL" \
                "$WHEEL_PATH" --skip-existing --verbose || { echo 'failed to twine upload' ; exit 1; }
        fi
    done
    echo """
        !!! FINISH: LIVE RUN !!!
    """
else
    echo """
        DRY RUN ... Skipping upload

        DEPLOY_REMOTE = '$DEPLOY_REMOTE'
        DO_UPLOAD = '$DO_UPLOAD'
        WHEEL_PATH = '$WHEEL_PATH'
        WHEEL_PATHS_STR = '$WHEEL_PATHS_STR'
        MODE_LIST_STR = '$MODE_LIST_STR'

        VERSION='$VERSION'
        NAME='$NAME'
        TWINE_USERNAME='$TWINE_USERNAME'
        GPG_KEYID = '$GPG_KEYID'

        To do live run set DO_UPLOAD=1 and ensure deploy and current branch are the same

        !!! FINISH: DRY RUN !!!
    """
fi

#!/usr/bin/env bash
__doc__='
Script to build, sign, tag, and optionally upload Python distributions.

This script is intentionally local-first: it can perform the same release
steps that CI performs, but without requiring a CI provider.  It supports both
PEP 517 / pyproject.toml projects and older setup.py projects.

Common dry-run usage:
    DO_UPLOAD=0 DO_TAG=0 ./publish.sh

Common live usage:
    load_secrets
    TWINE_REPOSITORY_URL=live DO_UPLOAD=1 DO_TAG=1 ./publish.sh

Useful variables:
    MODE=pure|binary|all|sdist|native|bdist
        pure   -> build sdist and pure wheel with python -m build
        binary -> build sdist and use prebuilt wheelhouse wheels
        all    -> build sdist and pure wheel, and include wheelhouse wheels
        sdist  -> only build/use sdist
        native -> only build/use native wheel from python -m build
        bdist  -> only use prebuilt wheelhouse wheels

    DO_BUILD=True|False|auto
    DO_GPG=True|False|auto
    DO_OTS=True|False|auto
    DO_UPLOAD=True|False
    DO_TAG=True|False
    ENSURE_BUILD_DEPS=True|False
'

set -e

DEBUG=${DEBUG:=''}
if [[ "${DEBUG}" != "" ]]; then
    set -x
fi

check_variable(){
    KEY=$1
    HIDE=${2:-}
    VAL=${!KEY:-}
    if [[ "$HIDE" == "" ]]; then
        echo "[DEBUG] CHECK VARIABLE: $KEY=\"$VAL\""
    else
        echo "[DEBUG] CHECK VARIABLE: $KEY=<hidden>"
    fi
    if [[ "$VAL" == "" ]]; then
        echo "[ERROR] UNSET VARIABLE: $KEY=\"$VAL\""
        exit 1
    fi
}

normalize_boolean(){
    ARG=${1:-}
    ARG=$(echo "$ARG" | awk '{print tolower($0)}')
    if [ "$ARG" = "true" ] || [ "$ARG" = "1" ] || [ "$ARG" = "yes" ] || [ "$ARG" = "y" ] || [ "$ARG" = "on" ]; then
        echo "True"
    elif [ "$ARG" = "false" ] || [ "$ARG" = "0" ] || [ "$ARG" = "no" ] || [ "$ARG" = "n" ] || [ "$ARG" = "off" ]; then
        echo "False"
    else
        echo "$ARG"
    fi
}

project_name(){
    python - <<'PY'
from __future__ import annotations
import pathlib
try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

root = pathlib.Path.cwd()
pyproject = root / 'pyproject.toml'
if pyproject.exists():
    data = tomllib.loads(pyproject.read_text())
    name = data.get('project', {}).get('name')
    if name:
        print(name)
        raise SystemExit
setup_py = root / 'setup.py'
if setup_py.exists():
    ns = {}
    exec(setup_py.read_text(), ns)
    name = ns.get('NAME')
    if name:
        print(name)
        raise SystemExit
print(root.name)
PY
}

project_dist_prefix(){
    python - "$1" <<'PY'
import re
import sys
name = sys.argv[1]
print(re.sub(r'[-_.]+', '_', name).lower())
PY
}

project_version(){
    python - <<'PY'
from __future__ import annotations
import ast
import pathlib
try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

root = pathlib.Path.cwd()
pyproject = root / 'pyproject.toml'

def static_parse_version(fpath: pathlib.Path, varname: str = '__version__'):
    try:
        tree = ast.parse(fpath.read_text())
    except Exception:
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if getattr(target, 'id', None) == varname:
                    try:
                        return ast.literal_eval(node.value)
                    except Exception:
                        return None
    return None

if pyproject.exists():
    data = tomllib.loads(pyproject.read_text())
    project = data.get('project', {})
    if project.get('version'):
        print(project['version'])
        raise SystemExit

    tool = data.get('tool', {})
    setuptools_dynamic = tool.get('setuptools', {}).get('dynamic', {})
    version_spec = setuptools_dynamic.get('version', {})
    attr = version_spec.get('attr') if isinstance(version_spec, dict) else None
    if attr:
        module_name, _, attr_name = attr.rpartition('.')
        xcookie_config = tool.get('xcookie', {})
        rel_parent = pathlib.Path(xcookie_config.get('rel_mod_parent_dpath', '.'))
        candidate_roots = [rel_parent]
        find_cfg = tool.get('setuptools', {}).get('packages', {}).get('find', {})
        if isinstance(find_cfg, dict):
            for item in find_cfg.get('where', []) or []:
                candidate_roots.append(pathlib.Path(item))
        candidate_roots.append(pathlib.Path('.'))
        rel_module = pathlib.Path(*module_name.split('.'))
        for base in dict.fromkeys(candidate_roots):
            for rel in [base / rel_module / '__init__.py', base / (str(rel_module) + '.py')]:
                version = static_parse_version(root / rel, attr_name)
                if version:
                    print(version)
                    raise SystemExit

setup_py = root / 'setup.py'
if setup_py.exists():
    ns = {}
    exec(setup_py.read_text(), ns)
    version = ns.get('VERSION')
    if version:
        print(version)
        raise SystemExit
raise SystemExit('Could not determine project version')
PY
}

ls_array(){
    local arr_name="$1"
    local glob_pattern="$2"
    shopt -s nullglob
    # shellcheck disable=SC2206
    array=($glob_pattern)
    shopt -u nullglob
    # shellcheck disable=SC2086
    readarray -t $arr_name < <(printf '%s\n' "${array[@]}")
}

DEPLOY_REMOTE=${DEPLOY_REMOTE:=origin}
NAME=${NAME:=$(project_name)}
VERSION=${VERSION:=$(project_version)}
DIST_PREFIX=${DIST_PREFIX:=$(project_dist_prefix "$NAME")}
DIST_DPATH=${DIST_DPATH:=dist}
WHEELHOUSE_DPATH=${WHEELHOUSE_DPATH:=wheelhouse}

check_variable DEPLOY_REMOTE

ARG_1=${1:-}
DO_UPLOAD=${DO_UPLOAD:=$ARG_1}
DO_TAG=${DO_TAG:=$ARG_1}
DO_UPLOAD=$(normalize_boolean "$DO_UPLOAD")
DO_TAG=$(normalize_boolean "$DO_TAG")

DO_GPG=${DO_GPG:="auto"}
if [ "$DO_GPG" == "auto" ]; then
    DO_GPG="True"
fi
DO_OTS=${DO_OTS:="auto"}
if [ "$DO_OTS" == "auto" ]; then
    if type ots >/dev/null 2>&1 ; then
        DO_OTS="True"
    else
        DO_OTS="False"
    fi
fi
DO_BUILD=${DO_BUILD:="auto"}
if [ "$DO_BUILD" == "auto" ]; then
    DO_BUILD="True"
fi
ENSURE_BUILD_DEPS=${ENSURE_BUILD_DEPS:="auto"}
if [ "$ENSURE_BUILD_DEPS" == "auto" ]; then
    ENSURE_BUILD_DEPS="$DO_BUILD"
fi

DO_GPG=$(normalize_boolean "$DO_GPG")
DO_OTS=$(normalize_boolean "$DO_OTS")
DO_BUILD=$(normalize_boolean "$DO_BUILD")
ENSURE_BUILD_DEPS=$(normalize_boolean "$ENSURE_BUILD_DEPS")

TWINE_USERNAME=${TWINE_USERNAME:=""}
TWINE_PASSWORD=${TWINE_PASSWORD:=""}
DEFAULT_TEST_TWINE_REPO_URL="https://test.pypi.org/legacy/"
DEFAULT_LIVE_TWINE_REPO_URL="https://upload.pypi.org/legacy/"

TWINE_REPOSITORY_URL=${TWINE_REPOSITORY_URL:="auto"}
if [[ "${TWINE_REPOSITORY_URL}" == "auto" ]]; then
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
    if [[ "$(command -v gpg2 || true)" != "" ]]; then
        GPG_EXECUTABLE="gpg2"
    else
        GPG_EXECUTABLE="gpg"
    fi
fi

GPG_KEYID=${GPG_KEYID:="auto"}
if [[ "$GPG_KEYID" == "auto" ]]; then
    GPG_KEYID=$(git config --local user.signingkey || true)
    if [[ "$GPG_KEYID" == "" ]]; then
        GPG_KEYID=$(git config --global user.signingkey || true)
    fi
fi

if [ -f CMakeLists.txt ] ; then
    DEFAULT_MODE="binary"
else
    DEFAULT_MODE="pure"
fi

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

WAS_INTERACTION="False"

echo "
=== PYPI BUILDING SCRIPT ==
NAME='$NAME'
VERSION='$VERSION'
DIST_PREFIX='$DIST_PREFIX'
TWINE_USERNAME='$TWINE_USERNAME'
TWINE_REPOSITORY_URL='$TWINE_REPOSITORY_URL'
GPG_KEYID='$GPG_KEYID'
DO_UPLOAD=${DO_UPLOAD}
DO_TAG=${DO_TAG}
DO_GPG=${DO_GPG}
DO_OTS=${DO_OTS}
DO_BUILD=${DO_BUILD}
MODE_LIST_STR=${MODE_LIST_STR}
"

if [[ "$DO_TAG" == "" ]]; then
    read -r -p "Do you want to git tag and push version='$VERSION'? (input 'yes' to confirm) " ANS
    WAS_INTERACTION="True"
    DO_TAG=$(normalize_boolean "$ANS")
fi

if [[ "$DO_BUILD" == "" ]]; then
    read -r -p "Do you need to build distributions? (input 'yes' to confirm) " ANS
    WAS_INTERACTION="True"
    DO_BUILD=$(normalize_boolean "$ANS")
fi

if [[ "$DO_UPLOAD" == "" ]]; then
    read -r -p "Are you ready to directly publish version='$VERSION'? ('yes' will twine upload) " ANS
    WAS_INTERACTION="True"
    DO_UPLOAD=$(normalize_boolean "$ANS")
fi

if [[ "$WAS_INTERACTION" == "True" ]]; then
    echo "
    VERSION='$VERSION'
    DO_UPLOAD=${DO_UPLOAD}
    DO_TAG=${DO_TAG}
    DO_GPG=${DO_GPG}
    DO_BUILD=${DO_BUILD}
    MODE_LIST_STR='${MODE_LIST_STR}'
    "
    read -r -p "Look good? Enter any text to continue " ANS
fi

if [ "$ENSURE_BUILD_DEPS" == "True" ]; then
    python -m pip install pip build twine -U
fi

if [ "$DO_BUILD" == "True" ]; then
    echo "=== <BUILD DISTRIBUTIONS> ==="
    for _MODE in "${MODE_LIST[@]}"; do
        echo "_MODE=$_MODE"
        if [[ "$_MODE" == "sdist" ]]; then
            python -m build --sdist --outdir "$DIST_DPATH" || { echo 'failed to build sdist' ; exit 1; }
        elif [[ "$_MODE" == "native" ]]; then
            python -m build --wheel --outdir "$DIST_DPATH" || { echo 'failed to build wheel' ; exit 1; }
        elif [[ "$_MODE" == "bdist" ]]; then
            echo "Assume binary wheels have already been built in $WHEELHOUSE_DPATH"
        else
            echo "ERROR: bad mode: $_MODE"
            exit 1
        fi
    done
    echo "=== <END BUILD DISTRIBUTIONS> ==="
else
    echo "DO_BUILD=False, skipping build"
fi

WHEEL_FPATHS=()
for _MODE in "${MODE_LIST[@]}"; do
    if [[ "$_MODE" == "sdist" ]]; then
        ls_array "_NEW_WHEEL_PATHS" "$DIST_DPATH/${DIST_PREFIX}-${VERSION}*.tar.gz"
    elif [[ "$_MODE" == "native" ]]; then
        ls_array "_NEW_WHEEL_PATHS" "$DIST_DPATH/${DIST_PREFIX}-${VERSION}*.whl"
    elif [[ "$_MODE" == "bdist" ]]; then
        ls_array "_NEW_WHEEL_PATHS" "$WHEELHOUSE_DPATH/${DIST_PREFIX}-${VERSION}-*.whl"
    else
        echo "ERROR: bad mode: $_MODE"
        exit 1
    fi
    for new_item in "${_NEW_WHEEL_PATHS[@]}"; do
        if [[ "$new_item" != "" ]]; then
            WHEEL_FPATHS+=("$new_item")
        fi
    done
done

readarray -t WHEEL_FPATHS < <(printf '%s\n' "${WHEEL_FPATHS[@]}" | sort -u)
if [[ "${#WHEEL_FPATHS[@]}" -eq 0 ]]; then
    echo "ERROR: no distributions found for NAME=$NAME VERSION=$VERSION"
    exit 1
fi

WHEEL_PATHS_STR=$(printf '"%s" ' "${WHEEL_FPATHS[@]}")
echo "WHEEL_PATHS_STR=$WHEEL_PATHS_STR"

python -m twine check "${WHEEL_FPATHS[@]}"

WHEEL_SIGNATURE_FPATHS=()
if [ "$DO_GPG" == "True" ]; then
    echo "=== <GPG SIGN> ==="
    for WHEEL_FPATH in "${WHEEL_FPATHS[@]}"; do
        check_variable WHEEL_FPATH
        check_variable GPG_EXECUTABLE
        check_variable GPG_KEYID
        GPG_SIGN_CMD="$GPG_EXECUTABLE --batch --yes --detach-sign --armor --local-user $GPG_KEYID"
        echo "GPG_SIGN_CMD=$GPG_SIGN_CMD"
        $GPG_SIGN_CMD --output "$WHEEL_FPATH".asc "$WHEEL_FPATH"
        python -m twine check "$WHEEL_FPATH"
        $GPG_EXECUTABLE --verify "$WHEEL_FPATH".asc "$WHEEL_FPATH"
        WHEEL_SIGNATURE_FPATHS+=("$WHEEL_FPATH".asc)
    done
    echo "=== <END GPG SIGN> ==="
else
    echo "DO_GPG=False, skipping GPG sign"
fi

if [ "$DO_OTS" == "True" ]; then
    echo "=== <OTS SIGN> ==="
    if [ "$DO_GPG" == "True" ]; then
        ots stamp "${WHEEL_FPATHS[@]}" "${WHEEL_SIGNATURE_FPATHS[@]}"
    else
        ots stamp "${WHEEL_FPATHS[@]}"
    fi
    echo "=== <END OTS SIGN> ==="
else
    echo "DO_OTS=False, skipping OTS sign"
fi

if [[ "$DO_TAG" == "True" ]]; then
    TAG_NAME="v${VERSION}"
    git tag "$TAG_NAME" -m "tarball tag $VERSION"
    git push --tags "$DEPLOY_REMOTE"
    echo "Tagged and pushed $TAG_NAME to $DEPLOY_REMOTE"
else
    echo "Not tagging"
fi

if [[ "$DO_UPLOAD" == "True" ]]; then
    check_variable TWINE_USERNAME
    check_variable TWINE_PASSWORD "hide"
    python -m twine upload --username "$TWINE_USERNAME" "--password=$TWINE_PASSWORD" \
        --repository-url "$TWINE_REPOSITORY_URL" \
        "${WHEEL_FPATHS[@]}" --skip-existing --verbose || { echo 'failed to twine upload' ; exit 1; }
    echo "!!! FINISH: LIVE RUN !!!"
else
    echo "
DRY RUN ... Skipping upload
DEPLOY_REMOTE='$DEPLOY_REMOTE'
DO_UPLOAD='$DO_UPLOAD'
WHEEL_PATHS_STR='$WHEEL_PATHS_STR'
MODE_LIST_STR='$MODE_LIST_STR'
VERSION='$VERSION'
NAME='$NAME'
TWINE_USERNAME='$TWINE_USERNAME'
GPG_KEYID='$GPG_KEYID'
To do live run set DO_UPLOAD=1.
!!! FINISH: DRY RUN !!!
"
fi

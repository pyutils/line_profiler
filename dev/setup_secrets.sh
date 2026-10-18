#!/usr/bin/env bash
__doc__='
============================
SETUP CI SECRET INSTRUCTIONS
============================

TODO: These instructions are currently pieced together from old disparate
instances, and are not yet fully organized.

The original template file should be:
~/code/xcookie/dev/setup_secrets.sh

Development script for updating secrets when they rotate


The intent of this script is to help setup secrets for whichever of the
following CI platforms is used:

../.github/workflows/tests.yml
../.gitlab-ci.yml
../.circleci/config.yml


=========================
GITHUB ACTION INSTRUCTIONS
=========================

* `PERSONAL_GITHUB_PUSH_TOKEN` -
    This is only needed if you want to automatically git-tag release branches.

    To make a API token go to:
        https://docs.github.com/en/free-pro-team@latest/github/authenticating-to-github/creating-a-personal-access-token


=========================
GITLAB ACTION INSTRUCTIONS
=========================

    ```bash
    cat .setup_secrets.sh | \
        sed "s|utils|<YOUR-GROUP>|g" | \
        sed "s|xcookie|<YOUR-REPO>|g" | \
        sed "s|travis-ci-Erotemic|<YOUR-GPG-ID>|g" | \
        sed "s|CI_SECRET|<YOUR_CI_SECRET>|g" | \
        sed "s|GITLAB_ORG_PUSH_TOKEN|<YOUR_GIT_ORG_PUSH_TOKEN>|g" | \
        sed "s|gitlab.org.com|gitlab.your-instance.com|g" | \
    tee /tmp/repl && colordiff .setup_secrets.sh /tmp/repl
    ```

    * Make sure you add Runners to your project
    https://gitlab.org.com/utils/xcookie/-/settings/ci_cd
    in Runners-> Shared Runners
    and Runners-> Available specific runners

    * Ensure that you are auto-cancel redundant pipelines.
    Navigate to https://gitlab.kitware.com/utils/xcookie/-/settings/ci_cd and ensure "Auto-cancel redundant pipelines" is checked.

    More details are here https://docs.gitlab.com/ee/ci/pipelines/settings.html#auto-cancel-redundant-pipelines

    * TWINE_USERNAME - this is your pypi username
        twine info is only needed if you want to automatically publish to pypi

    * TWINE_PASSWORD - this is your pypi password

    * CI_SECRET - We will use this as a secret key to encrypt/decrypt gpg secrets
        This is only needed if you want to automatically sign published
        wheels with a gpg key.

    * GITLAB_ORG_PUSH_TOKEN -
        This is only needed if you want to automatically git-tag release branches.

        Create a new personal access token in User->Settings->Tokens,
        You can name the token GITLAB_ORG_PUSH_TOKEN_VALUE
        Give it api and write repository permissions

        SeeAlso: https://gitlab.org.com/profile/personal_access_tokens

        Take this variable and record its value somewhere safe. I put it in my secrets file as such:

            export GITLAB_ORG_PUSH_TOKEN_VALUE=<paste-the-value-here>

        I also create another variable with the prefix "git-push-token", which is necessary

            export GITLAB_ORG_PUSH_TOKEN=git-push-token:$GITLAB_ORG_PUSH_TOKEN_VALUE

        Then add this as a secret variable here: https://gitlab.org.com/groups/utils/-/settings/ci_cd
        Note the value of GITLAB_ORG_PUSH_TOKEN will look something like: "{token-name}:{token-password}"
        For instance it may look like this: "git-push-token:62zutpzqga6tvrhklkdjqm"

        References:
            https://stackoverflow.com/questions/51465858/how-do-you-push-to-a-gitlab-repo-using-a-gitlab-ci-job

     # ADD RELEVANT VARIABLES TO GITLAB SECRET VARIABLES
     # https://gitlab.kitware.com/computer-vision/kwcoco/-/settings/ci_cd
     # Note that it is important to make sure that these variables are
     # only decrpyted on protected branches by selecting the protected
     # and masked option. Also make sure you have master and release
     # branches protected.
     # https://gitlab.kitware.com/computer-vision/kwcoco/-/settings/repository#js-protected-branches-settings


============================
Relevant CI Secret Locations
============================

https://github.com/pyutils/line_profiler/settings/secrets/actions

https://app.circleci.com/settings/project/github/pyutils/line_profiler/environment-variables?return-to=https%3A%2F%2Fapp.circleci.com%2Fpipelines%2Fgithub%2Fpyutils%2Fline_profiler
'


_log(){
    # Print a tagged progress line. Goes to stderr so it can be filtered
    # from data output, and so it survives subshell capture of stdout.
    printf '::  %s\n' "$*" >&2
}


setup_package_environs(){
    __doc__="
    Setup environment variables specific for this project.
    The remainder of this script should ideally be general to any repo.  These
    non-secret variables are written to disk and loaded by the script, such
    that the specific repo only needs to modify that configuration file.
    "
    echo "Choose an organization specific setting or make your own. This needs to be generalized more"
}

### FIXME: Should be configurable for general use

setup_package_environs_gitlab_kitware(){
    echo '
    export VARNAME_CI_SECRET="CI_KITWARE_SECRET"
    export VARNAME_TWINE_PASSWORD="EROTEMIC_PYPI_MASTER_TOKEN"
    export VARNAME_TEST_TWINE_PASSWORD="EROTEMIC_TEST_PYPI_MASTER_TOKEN"
    export VARNAME_PUSH_TOKEN="GITLAB_KITWARE_TOKEN"
    export VARNAME_TWINE_USERNAME="EROTEMIC_PYPI_MASTER_TOKEN_USERNAME"
    export VARNAME_TEST_TWINE_USERNAME="EROTEMIC_TEST_PYPI_MASTER_TOKEN_USERNAME"
    export GPG_IDENTIFIER="=Erotemic-CI <erotemic@gmail.com>"
    ' | python -c "import sys; from textwrap import dedent; print(dedent(sys.stdin.read()).strip(chr(10)))" > dev/secrets_configuration.sh
    git add dev/secrets_configuration.sh
}

setup_package_environs_github_erotemic(){
    echo '
    export VARNAME_CI_SECRET="EROTEMIC_CI_SECRET"
    export VARNAME_TWINE_PASSWORD="EROTEMIC_PYPI_MASTER_TOKEN"
    export VARNAME_TEST_TWINE_PASSWORD="EROTEMIC_TEST_PYPI_MASTER_TOKEN"
    export VARNAME_TWINE_USERNAME="EROTEMIC_PYPI_MASTER_TOKEN_USERNAME"
    export GITHUB_ENVIRONMENT_PYPI="pypi"
    export GITHUB_ENVIRONMENT_TESTPYPI="testpypi"
    export VARNAME_TEST_TWINE_USERNAME="EROTEMIC_TEST_PYPI_MASTER_TOKEN_USERNAME"
    export GPG_IDENTIFIER="=Erotemic-CI <erotemic@gmail.com>"
    ' | python -c "import sys; from textwrap import dedent; print(dedent(sys.stdin.read()).strip(chr(10)))" > dev/secrets_configuration.sh
    git add dev/secrets_configuration.sh
}

setup_package_environs_github_pyutils(){
    echo '
    export VARNAME_CI_SECRET="PYUTILS_CI_SECRET"
    export VARNAME_TWINE_PASSWORD="PYUTILS_PYPI_MASTER_TOKEN"
    export VARNAME_TEST_TWINE_PASSWORD="PYUTILS_TEST_PYPI_MASTER_TOKEN"
    export VARNAME_TWINE_USERNAME="PYUTILS_PYPI_MASTER_TOKEN_USERNAME"
    export GITHUB_ENVIRONMENT_PYPI="pypi"
    export GITHUB_ENVIRONMENT_TESTPYPI="testpypi"
    export VARNAME_TEST_TWINE_USERNAME="PYUTILS_TEST_PYPI_MASTER_TOKEN_USERNAME"
    export GPG_IDENTIFIER="=PyUtils-CI <openpyutils@gmail.com>"
    ' | python -c "import sys; from textwrap import dedent; print(dedent(sys.stdin.read()).strip(chr(10)))" > dev/secrets_configuration.sh
    git add dev/secrets_configuration.sh

    #echo '
    #export VARNAME_CI_SECRET="PYUTILS_CI_SECRET"
    #export GPG_IDENTIFIER="=PyUtils-CI <openpyutils@gmail.com>"
    #' | python -c "import sys; from textwrap import dedent; print(dedent(sys.stdin.read()).strip(chr(10)))" > dev/secrets_configuration.sh
}

resolve_secret_value_from_varname_ptr(){
    local secret_varname_ptr="$1"
    local secret_name="$2"
    local secret_varname="${!secret_varname_ptr}"
    if [[ "$secret_varname" == "" ]]; then
        echo "Skipping $secret_name because $secret_varname_ptr is unset" >&2
        return 1
    fi
    local secret_value="${!secret_varname}"
    if [[ "$secret_value" == "" ]]; then
        echo "Skipping $secret_name because $secret_varname is unset or empty" >&2
        return 1
    fi
    printf '%s' "$secret_value"
}

_github_repo_from_remote_url(){
    local remote_url="$1"
    if [[ "$remote_url" == git@github.com:* ]]; then
        printf '%s' "${remote_url#git@github.com:}" | sed 's/\.git$//'
    elif [[ "$remote_url" == https://github.com/* ]]; then
        printf '%s' "${remote_url#https://github.com/}" | sed 's/\.git$//'
    elif [[ "$remote_url" == ssh://git@github.com/* ]]; then
        printf '%s' "${remote_url#ssh://git@github.com/}" | sed 's/\.git$//'
    else
        return 1
    fi
}

github_repo_full_name(){
    # GH_REPO is the standard gh CLI override and is also injected by
    # `xcookie secrets` when github_url identifies a GitHub mirror.
    if [[ "${GH_REPO:-}" != "" ]]; then
        printf '%s' "${GH_REPO#github.com/}"
        return 0
    fi

    local remote_name
    local remote_url
    local candidate
    local resolved=""
    while IFS= read -r remote_name; do
        remote_url="$(git remote get-url "$remote_name")"
        candidate="$(_github_repo_from_remote_url "$remote_url")" || continue
        if [[ "$resolved" == "" ]]; then
            resolved="$candidate"
        elif [[ "$resolved" != "$candidate" ]]; then
            echo "Multiple GitHub repositories are configured; set GH_REPO=owner/repo explicitly." >&2
            return 1
        fi
    done < <(git remote)

    if [[ "$resolved" != "" ]]; then
        printf '%s' "$resolved"
        return 0
    fi

    echo "Unable to determine GitHub repo. Set GH_REPO=owner/repo or configure a GitHub remote." >&2
    return 1
}

upload_one_github_secret(){
    # Upload a secret to GitHub. `gh secret set` reads the value from stdin
    # when no --body flag is given, which keeps the secret off argv (out of
    # `ps` and `/proc/<pid>/cmdline`) and works across gh CLI versions —
    # `--body-file` only exists on newer releases.
    local secret_name="$1"
    local secret_value="$2"
    local environment_name="${3:-}"
    local repo_full_name
    repo_full_name="$(github_repo_full_name)" || return 1
    if [[ "$environment_name" == "" ]]; then
        printf '%s' "$secret_value" | gh secret set "$secret_name" --repo "$repo_full_name"
    else
        printf '%s' "$secret_value" | gh secret set "$secret_name" --repo "$repo_full_name" --env "$environment_name"
    fi
}

ensure_github_environment(){
    local environment_name="$1"
    local repo_full_name
    repo_full_name="$(github_repo_full_name)" || return 1
    gh api --method PUT \
        -H "Accept: application/vnd.github+json" \
        "/repos/${repo_full_name}/environments/${environment_name}" >/dev/null
}

setup_github_release_environments(){
    source dev/secrets_configuration.sh
    local repo_full_name
    local pypi_env
    local testpypi_env
    repo_full_name="$(github_repo_full_name)" || return 1
    pypi_env="${GITHUB_ENVIRONMENT_PYPI:-pypi}"
    testpypi_env="${GITHUB_ENVIRONMENT_TESTPYPI:-testpypi}"

    ensure_github_environment "$testpypi_env"
    ensure_github_environment "$pypi_env"

    echo "Ensured GitHub environments exist:"
    echo "  - $testpypi_env"
    echo "  - $pypi_env"
    echo "Review environment protection rules manually as needed:"
    echo "  https://github.com/${repo_full_name}/settings/environments"
    echo "Suggested policy:"
    echo "  - ${testpypi_env}: usually no approval required"
    echo "  - ${pypi_env}: require approval / reviewers and restrict to release refs"
}

upload_github_secrets(){
    local mode="${1:-legacy}"
    unset GITHUB_TOKEN
    #printf "%s" "$GITHUB_TOKEN" | gh auth login --hostname Github.com --with-token
    if ! gh auth status ; then
        gh auth login
    fi
    local secret_value
    local pypi_env
    local testpypi_env
    source dev/secrets_configuration.sh

    if [[ "$mode" == "trusted_publishing" ]]; then
        pypi_env="${GITHUB_ENVIRONMENT_PYPI:-pypi}"
        testpypi_env="${GITHUB_ENVIRONMENT_TESTPYPI:-testpypi}"
        setup_github_release_environments
        secret_value=$(resolve_secret_value_from_varname_ptr VARNAME_CI_SECRET CI_SECRET) || true
        if [[ "$secret_value" != "" ]]; then
            upload_one_github_secret "CI_SECRET" "$secret_value" "$pypi_env"
            upload_one_github_secret "CI_SECRET" "$secret_value" "$testpypi_env"
        fi
    elif [[ "$mode" == "direct_gpg" ]]; then
        # direct_ci GPG transport + non-trusted publishing.
        # GPG material is already uploaded by upload_github_gpg_secrets.
        # Upload Twine credentials environment-scoped (live password to pypi
        # env, test password to testpypi env). CI_SECRET is not uploaded.
        pypi_env="${GITHUB_ENVIRONMENT_PYPI:-pypi}"
        testpypi_env="${GITHUB_ENVIRONMENT_TESTPYPI:-testpypi}"
        setup_github_release_environments
        secret_value=$(resolve_secret_value_from_varname_ptr VARNAME_TWINE_USERNAME TWINE_USERNAME) || true
        if [[ "$secret_value" != "" ]]; then
            upload_one_github_secret "TWINE_USERNAME" "$secret_value" "$pypi_env"
            upload_one_github_secret "TWINE_USERNAME" "$secret_value" "$testpypi_env"
        fi
        secret_value=$(resolve_secret_value_from_varname_ptr VARNAME_TEST_TWINE_USERNAME TEST_TWINE_USERNAME) || true
        if [[ "$secret_value" != "" ]]; then
            upload_one_github_secret "TEST_TWINE_USERNAME" "$secret_value" "$testpypi_env"
        fi
        secret_value=$(resolve_secret_value_from_varname_ptr VARNAME_TWINE_PASSWORD TWINE_PASSWORD) || true
        if [[ "$secret_value" != "" ]]; then
            upload_one_github_secret "TWINE_PASSWORD" "$secret_value" "$pypi_env"
        fi
        secret_value=$(resolve_secret_value_from_varname_ptr VARNAME_TEST_TWINE_PASSWORD TEST_TWINE_PASSWORD) || true
        if [[ "$secret_value" != "" ]]; then
            upload_one_github_secret "TEST_TWINE_PASSWORD" "$secret_value" "$testpypi_env"
        fi
    else
        # Legacy mode: all secrets repo-level, CI_SECRET included.
        secret_value=$(resolve_secret_value_from_varname_ptr VARNAME_TWINE_USERNAME TWINE_USERNAME) && upload_one_github_secret "TWINE_USERNAME" "$secret_value"
        secret_value=$(resolve_secret_value_from_varname_ptr VARNAME_TEST_TWINE_USERNAME TEST_TWINE_USERNAME) && upload_one_github_secret "TEST_TWINE_USERNAME" "$secret_value"
        secret_value=$(resolve_secret_value_from_varname_ptr VARNAME_CI_SECRET CI_SECRET) && upload_one_github_secret "CI_SECRET" "$secret_value"
        secret_value=$(resolve_secret_value_from_varname_ptr VARNAME_TWINE_PASSWORD TWINE_PASSWORD) && upload_one_github_secret "TWINE_PASSWORD" "$secret_value"
        secret_value=$(resolve_secret_value_from_varname_ptr VARNAME_TEST_TWINE_PASSWORD TEST_TWINE_PASSWORD) && upload_one_github_secret "TEST_TWINE_PASSWORD" "$secret_value"
    fi
}


_gitlab_check_auth(){
    # Smoke-test that PRIVATE_GITLAB_TOKEN authenticates against HOST before
    # any upload work runs. Surfaces a clear, actionable message when the
    # token is missing, revoked, expired, or pointed at the wrong instance.
    # Assumes PRIVATE_GITLAB_TOKEN and HOST are in scope.
    local TMP_AUTH http_code username
    TMP_AUTH=$(mktemp -t gitlab-auth-XXXXXXXXXX)
    http_code=$(curl --silent --output "$TMP_AUTH" --write-out '%{http_code}' \
        --header "PRIVATE-TOKEN: $PRIVATE_GITLAB_TOKEN" \
        "$HOST/api/v4/user")
    if [[ "$http_code" != "200" ]]; then
        echo "ERROR: GitLab authentication failed against $HOST (HTTP $http_code)" >&2
        echo "       The PRIVATE_GITLAB_TOKEN env var is invalid, expired, revoked," >&2
        echo "       lacks 'api' scope, or belongs to a different GitLab instance." >&2
        echo "       Create a new personal access token with 'api' scope at:" >&2
        echo "         $HOST/-/user_settings/personal_access_tokens" >&2
        rm -f "$TMP_AUTH"
        return 1
    fi
    username=$(jq -r '.username // "?"' < "$TMP_AUTH")
    rm -f "$TMP_AUTH"
    _log "GitLab auth OK: authenticated as '$username' on $HOST"
}


_secret_fingerprint(){
    # Compute a stable 12-hex-char SHA-256 prefix of a secret value so we can
    # compare local vs. remote values in logs without revealing them.
    # Reads the secret from stdin to avoid putting it on argv (where `ps`
    # could observe it). Empty input prints "(empty)".
    local digest
    digest=$(sha256sum | cut -c1-12)
    # SHA-256 of the empty string starts with e3b0c44298fc — treat as empty.
    if [[ "$digest" == "e3b0c44298fc" ]]; then
        echo "(empty)"
    else
        echo "$digest"
    fi
}


_gitlab_pick_remote(){
    # Echo the name of the remote that points at a GitLab instance.
    # A project may have multiple backends (e.g. `origin` -> github.com and
    # `gitlab` -> gitlab.kitware.com), so we cannot assume `origin` is the
    # GitLab remote. Preference order:
    #   1. a remote literally named `gitlab`
    #   2. any remote whose URL host contains `gitlab`
    #   3. `origin` (legacy fallback for single-backend repos)
    local name url
    if git remote get-url gitlab >/dev/null 2>&1; then
        printf '%s\n' gitlab
        return 0
    fi
    while read -r name; do
        [[ -z "$name" ]] && continue
        url=$(git remote get-url "$name" 2>/dev/null) || continue
        if [[ "$url" == *gitlab* ]]; then
            printf '%s\n' "$name"
            return 0
        fi
    done < <(git remote 2>/dev/null)
    printf '%s\n' origin
}


_gitlab_remote_info(){
    # Parse the GitLab remote URL and emit three lines: HOST PROJECT_PATH GROUP_PATH.
    # Supports SSH (user@host:ns/repo.git) and HTTPS (https://host/ns/repo.git)
    # and arbitrarily nested namespaces. The GitLab remote is auto-detected
    # (see _gitlab_pick_remote) rather than assumed to be `origin`.
    local remote_name remote_url host path
    remote_name=$(_gitlab_pick_remote)
    remote_url=$(git remote get-url "$remote_name" 2>/dev/null) || {
        echo "ERROR: cannot read '$remote_name' remote URL" >&2
        return 1
    }
    if [[ "$remote_url" =~ ^[^@/:]+@([^:]+):(.+)$ ]]; then
        # SCP-style: user@host:namespace/repo
        host="${BASH_REMATCH[1]}"
        path="${BASH_REMATCH[2]}"
    elif [[ "$remote_url" =~ ^ssh://([^/@]+@)?([^/:]+)(:[0-9]+)?/(.+)$ ]]; then
        # ssh://[user@]host[:port]/path
        host="${BASH_REMATCH[2]}"
        path="${BASH_REMATCH[4]}"
    elif [[ "$remote_url" =~ ^https?://([^/@]+@)?([^/]+)/(.+)$ ]]; then
        # http(s)://[user@]host/path
        host="${BASH_REMATCH[2]}"
        path="${BASH_REMATCH[3]}"
    else
        echo "ERROR: unrecognized GitLab remote URL: $remote_url" >&2
        return 1
    fi
    path="${path%.git}"
    printf '%s\n%s\n%s\n' "https://$host" "$path" "${path%%/*}"
}


_gitlab_upsert_var(){
    # Upsert a protected, masked GitLab CI/CD variable.
    # Usage: _gitlab_upsert_var <api_base_url> <key> <<<"$value"
    # Reads the secret value from stdin (here-string body) so it never
    # appears on argv — `ps` cannot see it. <api_base_url> is e.g.
    # "$HOST/api/v4/projects/$PID/variables". Reads PRIVATE_GITLAB_TOKEN
    # from the caller's scope.
    local base_url="$1" key="$2"
    local value http_code rc
    value=$(cat)
    _log "GitLab upsert $key -> $base_url"
    http_code=$(curl --silent --output /dev/null --write-out '%{http_code}' \
        --header "PRIVATE-TOKEN: $PRIVATE_GITLAB_TOKEN" \
        "$base_url/$key")
    if [[ "$http_code" == "200" ]]; then
        curl --fail --silent --show-error --output /dev/null --request PUT \
            --header "PRIVATE-TOKEN: $PRIVATE_GITLAB_TOKEN" \
            "$base_url/$key" \
            --form "value=$value" \
            --form "protected=true" \
            --form "masked=true" \
            --form "environment_scope=*" \
            --form "variable_type=env_var"
    else
        curl --fail --silent --show-error --output /dev/null --request POST \
            --header "PRIVATE-TOKEN: $PRIVATE_GITLAB_TOKEN" \
            "$base_url" \
            --form "key=$key" \
            --form "value=$value" \
            --form "protected=true" \
            --form "masked=true" \
            --form "environment_scope=*" \
            --form "variable_type=env_var"
    fi
    rc=$?
    if [[ "$rc" != "0" ]]; then
        echo "ERROR: failed to upsert GitLab variable '$key'" >&2
        return 1
    fi
}


upload_gitlab_group_secrets(){
    __doc__="
    Upsert each configured secret as a protected, masked group-level CI/CD
    variable. Secret values are never echoed: we only print 12-hex-char
    SHA-256 fingerprints so local/remote drift is observable without
    disclosing the values themselves.
    "
    # NOTE: any `source` MUST happen before the RETURN trap is set,
    # because bash fires RETURN on source completion as well as on
    # function return — see `man bash` under 'trap'.
    source dev/secrets_configuration.sh

    local HOST PROJECT_PATH GROUP_NAME
    { read -r HOST; read -r PROJECT_PATH; read -r GROUP_NAME; } < <(_gitlab_remote_info) || return 1
    echo "
    * GROUP_NAME = $GROUP_NAME
    * HOST = $HOST
    "

    local PRIVATE_GITLAB_TOKEN="${PRIVATE_GITLAB_TOKEN:-}"
    if [[ -z "$PRIVATE_GITLAB_TOKEN" ]]; then
        echo "ERROR: PRIVATE_GITLAB_TOKEN is not set in the environment." >&2
        echo "       Export a GitLab personal access token with 'api' scope" >&2
        echo "       before running rotate-secrets, e.g.:" >&2
        echo "           export PRIVATE_GITLAB_TOKEN=<your-token>" >&2
        return 1
    fi
    _gitlab_check_auth || return 1

    local TMP_DIR
    TMP_DIR=$(mktemp -d -t gitlab-vars-XXXXXXXXXX)
    chmod 700 "$TMP_DIR"
    # shellcheck disable=SC2064
    trap "rm -rf '$TMP_DIR'" RETURN

    local GROUP_ID
    curl --fail --silent --show-error --header "PRIVATE-TOKEN: $PRIVATE_GITLAB_TOKEN" \
        "$HOST/api/v4/groups" > "$TMP_DIR/all_group_info"
    GROUP_ID=$(jq ". | map(select(.path==\"$GROUP_NAME\")) | .[0].id" < "$TMP_DIR/all_group_info")
    echo "GROUP_ID = $GROUP_ID"

    # Fetch group-level variables. This response includes plaintext values
    # of masked variables (GitLab masking only affects job logs), so the
    # response file lives in a 0700 tmpdir cleaned up by the RETURN trap.
    curl --fail --silent --show-error --header "PRIVATE-TOKEN: $PRIVATE_GITLAB_TOKEN" \
        "$HOST/api/v4/groups/$GROUP_ID/variables" > "$TMP_DIR/group_vars"
    local rc=$?
    if [[ "$rc" != "0" ]]; then
        echo "ERROR: failed to fetch group-level variables (permission issue?)" >&2
        return 1
    fi

    local SECRET_VARNAME_ARR=(VARNAME_CI_SECRET VARNAME_TWINE_PASSWORD VARNAME_TEST_TWINE_PASSWORD VARNAME_TWINE_USERNAME VARNAME_TEST_TWINE_USERNAME VARNAME_PUSH_TOKEN)
    local SECRET_VARNAME_PTR SECRET_VARNAME LOCAL_VALUE REMOTE_VALUE LOCAL_FP REMOTE_FP state
    for SECRET_VARNAME_PTR in "${SECRET_VARNAME_ARR[@]}"; do
        SECRET_VARNAME=${!SECRET_VARNAME_PTR}
        if [[ -z "$SECRET_VARNAME" ]]; then
            continue
        fi
        echo ""
        echo " ---- "
        echo "SECRET_VARNAME_PTR = $SECRET_VARNAME_PTR"
        echo "SECRET_VARNAME = $SECRET_VARNAME"

        # Read the local & remote values, fingerprint each, and decide what
        # action to take. Values themselves are never printed — only the
        # fingerprints — so logs are safe to share.
        LOCAL_VALUE=${!SECRET_VARNAME}
        REMOTE_VALUE=$(jq -r ".[] | select(.key==\"$SECRET_VARNAME\") | .value" < "$TMP_DIR/group_vars")
        LOCAL_FP=$(printf '%s' "$LOCAL_VALUE" | _secret_fingerprint)
        REMOTE_FP=$(printf '%s' "$REMOTE_VALUE" | _secret_fingerprint)
        if [[ -z "$REMOTE_VALUE" ]]; then
            state=new
        elif [[ "$REMOTE_VALUE" == "$LOCAL_VALUE" ]]; then
            state=match
        else
            state=update
        fi

        echo "(local)  $SECRET_VARNAME [${#LOCAL_VALUE} chars, fp=$LOCAL_FP]"
        echo "(remote) $SECRET_VARNAME [${#REMOTE_VALUE} chars, fp=$REMOTE_FP]"

        case "$state" in
            new)
                echo "Remote variable does not exist, posting"
                _gitlab_upsert_var "$HOST/api/v4/groups/$GROUP_ID/variables" "$SECRET_VARNAME" <<<"$LOCAL_VALUE"
                ;;
            update)
                echo "Remote variable disagrees with local, putting"
                _gitlab_upsert_var "$HOST/api/v4/groups/$GROUP_ID/variables" "$SECRET_VARNAME" <<<"$LOCAL_VALUE"
                ;;
            match)
                echo "Remote value agrees with local"
                ;;
        esac
    done
}

upload_gitlab_repo_secrets(){
    __doc__="
    Upsert each configured secret as a protected, masked project-level CI/CD
    variable. Secret values are never echoed: we only print 12-hex-char
    SHA-256 fingerprints so local/remote drift is observable without
    disclosing the values themselves.
    "
    # NOTE: any `source` MUST happen before the RETURN trap is set,
    # because bash fires RETURN on source completion as well as on
    # function return — see `man bash` under 'trap'.
    source dev/secrets_configuration.sh

    local HOST PROJECT_PATH GROUP_NAME
    { read -r HOST; read -r PROJECT_PATH; read -r GROUP_NAME; } < <(_gitlab_remote_info) || return 1
    echo "
    * GROUP_NAME = $GROUP_NAME
    * PROJECT_PATH = $PROJECT_PATH
    * HOST = $HOST
    "

    local PRIVATE_GITLAB_TOKEN="${PRIVATE_GITLAB_TOKEN:-}"
    if [[ -z "$PRIVATE_GITLAB_TOKEN" ]]; then
        echo "ERROR: PRIVATE_GITLAB_TOKEN is not set in the environment." >&2
        echo "       Export a GitLab personal access token with 'api' scope" >&2
        echo "       before running rotate-secrets, e.g.:" >&2
        echo "           export PRIVATE_GITLAB_TOKEN=<your-token>" >&2
        return 1
    fi
    _gitlab_check_auth || return 1

    local TMP_DIR
    TMP_DIR=$(mktemp -d -t gitlab-vars-XXXXXXXXXX)
    chmod 700 "$TMP_DIR"
    # shellcheck disable=SC2064
    trap "rm -rf '$TMP_DIR'" RETURN

    # URL-encoded path lookup is more robust than legacy group→project walk;
    # works for nested namespaces and requires narrower scope.
    local PROJECT_PATH_ENC=${PROJECT_PATH//\//%2F}
    local PROJECT_ID
    PROJECT_ID=$(curl --fail --silent --show-error --header "PRIVATE-TOKEN: $PRIVATE_GITLAB_TOKEN" \
        "$HOST/api/v4/projects/$PROJECT_PATH_ENC" \
        | jq -r '.id // empty')
    if [[ -z "$PROJECT_ID" ]]; then
        echo "ERROR: could not determine GitLab project ID for $PROJECT_PATH" >&2
        return 1
    fi
    echo "PROJECT_ID = $PROJECT_ID"

    # Fetch project-level variables (response contains plaintext values).
    curl --fail --silent --show-error --header "PRIVATE-TOKEN: $PRIVATE_GITLAB_TOKEN" \
        "$HOST/api/v4/projects/$PROJECT_ID/variables" > "$TMP_DIR/project_vars"
    local rc=$?
    if [[ "$rc" != "0" ]]; then
        echo "ERROR: failed to fetch project-level variables (permission issue?)" >&2
        return 1
    fi

    local mode="${1:-legacy}"
    local SECRET_VARNAME_ARR
    if [[ "$mode" == "trusted_publishing_direct_gpg" ]]; then
        # PyPI credentials are replaced by OIDC. GPG material is uploaded
        # separately, so GitLab only needs the repository push credential used
        # by the release/tag step.
        SECRET_VARNAME_ARR=(VARNAME_PUSH_TOKEN)
    elif [[ "$mode" == "trusted_publishing_encrypted_gpg" ]]; then
        # Trusted Publishing removes Twine credentials, but the legacy
        # encrypted-repo signing transport still needs CI_SECRET and the
        # release/tag step still needs the push credential.
        SECRET_VARNAME_ARR=(VARNAME_CI_SECRET VARNAME_PUSH_TOKEN)
    elif [[ "$mode" == "direct_gpg" ]]; then
        # GPG material is uploaded separately by upload_gitlab_gpg_secrets;
        # CI_SECRET isn't needed in this mode.
        SECRET_VARNAME_ARR=(VARNAME_TWINE_PASSWORD VARNAME_TEST_TWINE_PASSWORD VARNAME_TWINE_USERNAME VARNAME_TEST_TWINE_USERNAME VARNAME_PUSH_TOKEN)
    else
        SECRET_VARNAME_ARR=(VARNAME_CI_SECRET VARNAME_TWINE_PASSWORD VARNAME_TEST_TWINE_PASSWORD VARNAME_TWINE_USERNAME VARNAME_TEST_TWINE_USERNAME VARNAME_PUSH_TOKEN)
    fi

    local SECRET_VARNAME_PTR SECRET_VARNAME LOCAL_VALUE REMOTE_VALUE LOCAL_FP REMOTE_FP state
    for SECRET_VARNAME_PTR in "${SECRET_VARNAME_ARR[@]}"; do
        SECRET_VARNAME=${!SECRET_VARNAME_PTR}
        if [[ -z "$SECRET_VARNAME" ]]; then
            continue
        fi
        echo ""
        echo " ---- "
        echo "SECRET_VARNAME_PTR = $SECRET_VARNAME_PTR"
        echo "SECRET_VARNAME = $SECRET_VARNAME"

        LOCAL_VALUE=${!SECRET_VARNAME}
        REMOTE_VALUE=$(jq -r ".[] | select(.key==\"$SECRET_VARNAME\") | .value" < "$TMP_DIR/project_vars")
        LOCAL_FP=$(printf '%s' "$LOCAL_VALUE" | _secret_fingerprint)
        REMOTE_FP=$(printf '%s' "$REMOTE_VALUE" | _secret_fingerprint)
        if [[ -z "$REMOTE_VALUE" ]]; then
            state=new
        elif [[ "$REMOTE_VALUE" == "$LOCAL_VALUE" ]]; then
            state=match
        else
            state=update
        fi

        echo "(local)  $SECRET_VARNAME [${#LOCAL_VALUE} chars, fp=$LOCAL_FP]"
        echo "(remote) $SECRET_VARNAME [${#REMOTE_VALUE} chars, fp=$REMOTE_FP]"

        case "$state" in
            new)
                echo "Remote variable does not exist, posting"
                _gitlab_upsert_var "$HOST/api/v4/projects/$PROJECT_ID/variables" "$SECRET_VARNAME" <<<"$LOCAL_VALUE"
                ;;
            update)
                echo "Remote variable disagrees with local, putting"
                _gitlab_upsert_var "$HOST/api/v4/projects/$PROJECT_ID/variables" "$SECRET_VARNAME" <<<"$LOCAL_VALUE"
                ;;
            match)
                echo "Remote value agrees with local"
                ;;
        esac
    done
}


export_encrypted_code_signing_keys(){
    __doc__="
    Export the GPG signing subkey, public key, and ownertrust, encrypt each
    with CI_SECRET via openssl, and stage the resulting .enc files for
    commit. Rerun whenever the signing subkey is rotated.

    Security notes:
    - Plaintext key material is written to a private 0700 mktemp dir and
      cleaned up via a RETURN trap. The repo working tree (dev/) only ever
      sees the encrypted .enc files and the public fingerprint anchor.
    - CI_SECRET is never echoed. Passed to openssl via the GLKWS env var
      (GLKWS=$CI_SECRET openssl ... -pass env:GLKWS) so the secret is
      not on openssl's command line.
    "
    source dev/secrets_configuration.sh

    local CI_SECRET
    CI_SECRET="${!VARNAME_CI_SECRET}"
    if [[ -z "$CI_SECRET" ]]; then
        echo "ERROR: CI_SECRET (from $VARNAME_CI_SECRET) is empty" >&2
        return 1
    fi
    echo "VARNAME_CI_SECRET = $VARNAME_CI_SECRET"
    echo "GPG_IDENTIFIER = $GPG_IDENTIFIER"

    _gpg_locate_signing_subkey || return 1

    local TMP_DIR
    TMP_DIR=$(mktemp -d -t gpg-enc-XXXXXXXXXX)
    chmod 700 "$TMP_DIR"
    # shellcheck disable=SC2064
    trap "rm -rf '$TMP_DIR'" RETURN

    # Export plaintext key material into the protected temp dir — never the
    # working tree — so an interruption between export and encrypt cannot
    # leave a private subkey lying around in dev/.
    gpg --armor --export-options export-backup \
        --export-secret-subkeys "${GPG_SIGN_SUBKEY}!" > "$TMP_DIR/ci_secret_gpg_subkeys.pgp"
    gpg --armor --export "${GPG_SIGN_SUBKEY}" > "$TMP_DIR/ci_public_gpg_key.pgp"
    gpg --export-ownertrust > "$TMP_DIR/gpg_owner_trust"

    mkdir -p dev
    GLKWS=$CI_SECRET openssl enc -aes-256-cbc -pbkdf2 -md SHA512 -pass env:GLKWS -e -a \
        -in "$TMP_DIR/ci_public_gpg_key.pgp"      -out dev/ci_public_gpg_key.pgp.enc
    GLKWS=$CI_SECRET openssl enc -aes-256-cbc -pbkdf2 -md SHA512 -pass env:GLKWS -e -a \
        -in "$TMP_DIR/ci_secret_gpg_subkeys.pgp"  -out dev/ci_secret_gpg_subkeys.pgp.enc
    GLKWS=$CI_SECRET openssl enc -aes-256-cbc -pbkdf2 -md SHA512 -pass env:GLKWS -e -a \
        -in "$TMP_DIR/gpg_owner_trust"            -out dev/gpg_owner_trust.enc

    printf '%s\n' "$MAIN_GPG_FPR" > dev/public_gpg_key

    # Round-trip verification: decrypt each artifact back and feed gpg
    # --list-packets so we know the .enc files are well-formed. Output goes
    # to /dev/null because we only care about the exit status.
    GLKWS=$CI_SECRET openssl enc -aes-256-cbc -pbkdf2 -md SHA512 -pass env:GLKWS -d -a \
        -in dev/ci_public_gpg_key.pgp.enc      | gpg --list-packets >/dev/null
    GLKWS=$CI_SECRET openssl enc -aes-256-cbc -pbkdf2 -md SHA512 -pass env:GLKWS -d -a \
        -in dev/ci_secret_gpg_subkeys.pgp.enc  | gpg --list-packets >/dev/null
    GLKWS=$CI_SECRET openssl enc -aes-256-cbc -pbkdf2 -md SHA512 -pass env:GLKWS -d -a \
        -in dev/gpg_owner_trust.enc           >/dev/null


    echo "Wrote encrypted artifacts:"
    ls dev/*.enc
    echo "Public fingerprint anchor:"
    cat dev/public_gpg_key

    git status
    git add dev/ci_public_gpg_key.pgp.enc dev/ci_secret_gpg_subkeys.pgp.enc \
        dev/gpg_owner_trust.enc dev/public_gpg_key
}


# See the xcookie module gitlab python API
#gitlab_set_protected_branches(){
#}


_gpg_locate_signing_subkey(){
    __doc__="
    Internal helper. Sets MAIN_GPG_FPR and GPG_SIGN_SUBKEY in the caller's
    scope. Exits non-zero and prints a diagnostic if either cannot be found.
    Requires GPG_IDENTIFIER to already be set.
    "
    MAIN_GPG_FPR=$(gpg --list-keys --with-colons "$GPG_IDENTIFIER" \
        | awk -F: '/^fpr/ { print $10; exit }')
    GPG_SIGN_SUBKEY=$(gpg --list-keys --with-subkey-fingerprints "$GPG_IDENTIFIER" \
        | grep "\[S\]" -A 1 | tail -n 1 | awk '{print $1}')
    if [[ "$GPG_SIGN_SUBKEY" == "" ]]; then
        echo "WARNING: no [S] subkey found for $GPG_IDENTIFIER, falling back to [C] key" >&2
        GPG_SIGN_SUBKEY=$(gpg --list-keys --with-subkey-fingerprints "$GPG_IDENTIFIER" \
            | grep "\[C\]" -A 1 | tail -n 1 | awk '{print $1}')
    fi
    if [[ -z "$MAIN_GPG_FPR" ]]; then
        echo "ERROR: could not determine primary key fingerprint for $GPG_IDENTIFIER" >&2
        return 1
    fi
    if [[ -z "$GPG_SIGN_SUBKEY" ]]; then
        echo "ERROR: could not find a signing subkey for $GPG_IDENTIFIER" >&2
        return 1
    fi
    echo "MAIN_GPG_FPR    = $MAIN_GPG_FPR"
    echo "GPG_SIGN_SUBKEY = $GPG_SIGN_SUBKEY"
}


upload_github_gpg_secrets(){
    __doc__="
    Export GPG signing subkey material and upload it directly to GitHub
    Actions as environment-scoped secrets (pypi + testpypi environments).
    Also writes dev/public_gpg_key with the full primary key fingerprint
    and stages it for commit.

    No .enc files are written to disk or committed to git.
    This implements ci_gpg_secret_transport = 'direct_ci' for GitHub.
    Call this instead of export_encrypted_code_signing_keys.
    "
    source dev/secrets_configuration.sh

    local pypi_env="${GITHUB_ENVIRONMENT_PYPI:-pypi}"
    local testpypi_env="${GITHUB_ENVIRONMENT_TESTPYPI:-testpypi}"

    _gpg_locate_signing_subkey || return 1

    local TMP_DIR
    TMP_DIR=$(mktemp -d -t gpg-ci-XXXXXXXXXX)
    # shellcheck disable=SC2064
    trap "rm -rf '$TMP_DIR'" RETURN

    # Export signing subkey secret material and associated public key
    gpg --armor --export-options export-backup \
        --export-secret-subkeys "${GPG_SIGN_SUBKEY}!" > "$TMP_DIR/signing_subkey.pgp"
    gpg --armor --export "${GPG_SIGN_SUBKEY}" > "$TMP_DIR/public_key.pgp"
    gpg --export-ownertrust > "$TMP_DIR/owner_trust"

    # Single-line base64 for robust secret transport (tr -d '\n' is
    # portable across GNU and macOS; avoids -w 0 / -b 0 divergence).
    local GPG_SECRET_SIGNING_SUBKEY_B64 GPG_PUBLIC_KEY_B64 GPG_OWNER_TRUST_B64
    GPG_SECRET_SIGNING_SUBKEY_B64=$(base64 < "$TMP_DIR/signing_subkey.pgp" | tr -d '\n')
    GPG_PUBLIC_KEY_B64=$(base64 < "$TMP_DIR/public_key.pgp" | tr -d '\n')
    GPG_OWNER_TRUST_B64=$(base64 < "$TMP_DIR/owner_trust" | tr -d '\n')

    if [[ -z "$GPG_SECRET_SIGNING_SUBKEY_B64" ]]; then
        echo "ERROR: signing subkey export is empty — aborting" >&2
        return 1
    fi

    # Write the public fingerprint anchor to the repo.
    # This file is the only GPG artifact committed in direct_ci mode.
    mkdir -p dev
    printf '%s\n' "$MAIN_GPG_FPR" > dev/public_gpg_key
    git add dev/public_gpg_key
    git status


    # Ensure deployment environments exist before scoping secrets to them
    setup_github_release_environments

    if ! gh auth status; then gh auth login; fi

    for env_name in "$pypi_env" "$testpypi_env"; do
        upload_one_github_secret "GPG_SECRET_SIGNING_SUBKEY_B64" \
            "$GPG_SECRET_SIGNING_SUBKEY_B64" "$env_name"
        upload_one_github_secret "GPG_PUBLIC_KEY_B64" \
            "$GPG_PUBLIC_KEY_B64" "$env_name"
        upload_one_github_secret "GPG_OWNER_TRUST_B64" \
            "$GPG_OWNER_TRUST_B64" "$env_name"
    done
}


upload_gitlab_gpg_secrets(){
    __doc__="
    Export GPG signing subkey material and upload it directly to GitLab
    CI/CD project variables (protected=true, masked=true).
    Also writes dev/public_gpg_key with the full primary key fingerprint
    and stages it for commit.

    No .enc files are written to disk or committed to git.
    This implements ci_gpg_secret_transport = 'direct_ci' for GitLab.
    Call this instead of export_encrypted_code_signing_keys.
    "
    source dev/secrets_configuration.sh

    _gpg_locate_signing_subkey || return 1

    local TMP_DIR
    TMP_DIR=$(mktemp -d -t gpg-ci-XXXXXXXXXX)
    chmod 700 "$TMP_DIR"
    # shellcheck disable=SC2064
    trap "rm -rf '$TMP_DIR'" RETURN

    gpg --armor --export-options export-backup \
        --export-secret-subkeys "${GPG_SIGN_SUBKEY}!" > "$TMP_DIR/signing_subkey.pgp"
    gpg --armor --export "${GPG_SIGN_SUBKEY}" > "$TMP_DIR/public_key.pgp"
    gpg --export-ownertrust > "$TMP_DIR/owner_trust"

    local GPG_SECRET_SIGNING_SUBKEY_B64 GPG_PUBLIC_KEY_B64 GPG_OWNER_TRUST_B64
    GPG_SECRET_SIGNING_SUBKEY_B64=$(base64 < "$TMP_DIR/signing_subkey.pgp" | tr -d '\n')
    GPG_PUBLIC_KEY_B64=$(base64 < "$TMP_DIR/public_key.pgp" | tr -d '\n')
    GPG_OWNER_TRUST_B64=$(base64 < "$TMP_DIR/owner_trust" | tr -d '\n')

    if [[ -z "$GPG_SECRET_SIGNING_SUBKEY_B64" ]]; then
        echo "ERROR: signing subkey export is empty — aborting" >&2
        return 1
    fi

    # Write the public fingerprint anchor to the repo.
    mkdir -p dev
    printf '%s\n' "$MAIN_GPG_FPR" > dev/public_gpg_key
    git add dev/public_gpg_key
    git status

    local HOST PROJECT_PATH _GROUP
    { read -r HOST; read -r PROJECT_PATH; read -r _GROUP; } < <(_gitlab_remote_info) || return 1
    local PRIVATE_GITLAB_TOKEN="${PRIVATE_GITLAB_TOKEN:-}"
    if [[ -z "$PRIVATE_GITLAB_TOKEN" ]]; then
        echo "ERROR: PRIVATE_GITLAB_TOKEN is not set in the environment." >&2
        echo "       Export a GitLab personal access token with 'api' scope" >&2
        echo "       before running rotate-secrets, e.g.:" >&2
        echo "           export PRIVATE_GITLAB_TOKEN=<your-token>" >&2
        return 1
    fi
    _gitlab_check_auth || return 1

    local PROJECT_PATH_ENC=${PROJECT_PATH//\//%2F}
    local PROJECT_ID
    PROJECT_ID=$(curl --fail --silent --show-error --header "PRIVATE-TOKEN: $PRIVATE_GITLAB_TOKEN" \
        "$HOST/api/v4/projects/$PROJECT_PATH_ENC" \
        | jq -r '.id // empty')
    if [[ -z "$PROJECT_ID" ]]; then
        echo "ERROR: could not determine GitLab project ID for $PROJECT_PATH" >&2
        return 1
    fi
    echo "PROJECT_ID = $PROJECT_ID"


    local vars_url="$HOST/api/v4/projects/$PROJECT_ID/variables"
    _gitlab_upsert_var "$vars_url" "GPG_SECRET_SIGNING_SUBKEY_B64" <<<"$GPG_SECRET_SIGNING_SUBKEY_B64" || return 1
    _gitlab_upsert_var "$vars_url" "GPG_PUBLIC_KEY_B64"            <<<"$GPG_PUBLIC_KEY_B64"            || return 1
    _gitlab_upsert_var "$vars_url" "GPG_OWNER_TRUST_B64"           <<<"$GPG_OWNER_TRUST_B64"           || return 1
}


_test_gnu(){
    # Decrypt the encrypted-repo artifacts back into a throwaway GNUPGHOME
    # to verify the encrypt/decrypt round trip works end-to-end.
    source dev/secrets_configuration.sh

    local GNUPGHOME
    GNUPGHOME=$(mktemp -d -t gnupg-test-XXXXXXXXXX)
    chmod 700 "$GNUPGHOME"
    export GNUPGHOME
    # shellcheck disable=SC2064
    trap "rm -rf '$GNUPGHOME'" RETURN
    gpg -k

    local CI_SECRET
    CI_SECRET="${!VARNAME_CI_SECRET}"
    if [[ -z "$CI_SECRET" ]]; then
        echo "ERROR: CI_SECRET (from $VARNAME_CI_SECRET) is empty" >&2
        return 1
    fi

    cat dev/public_gpg_key
    GLKWS=$CI_SECRET openssl enc -aes-256-cbc -pbkdf2 -md SHA512 -pass env:GLKWS -d -a \
        -in dev/ci_public_gpg_key.pgp.enc      | gpg --import
    GLKWS=$CI_SECRET openssl enc -aes-256-cbc -pbkdf2 -md SHA512 -pass env:GLKWS -d -a \
        -in dev/gpg_owner_trust.enc            | gpg --import-ownertrust
    GLKWS=$CI_SECRET openssl enc -aes-256-cbc -pbkdf2 -md SHA512 -pass env:GLKWS -d -a \
        -in dev/ci_secret_gpg_subkeys.pgp.enc  | gpg --import

    gpg -k
}

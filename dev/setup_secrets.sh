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
    export VARNAME_TEST_TWINE_USERNAME="PYUTILS_TEST_PYPI_MASTER_TOKEN_USERNAME"
    export GPG_IDENTIFIER="=PyUtils-CI <openpyutils@gmail.com>"
    ' | python -c "import sys; from textwrap import dedent; print(dedent(sys.stdin.read()).strip(chr(10)))" > dev/secrets_configuration.sh
    git add dev/secrets_configuration.sh

    #echo '
    #export VARNAME_CI_SECRET="PYUTILS_CI_SECRET"
    #export GPG_IDENTIFIER="=PyUtils-CI <openpyutils@gmail.com>"
    #' | python -c "import sys; from textwrap import dedent; print(dedent(sys.stdin.read()).strip(chr(10)))" > dev/secrets_configuration.sh
}

upload_github_secrets(){
    load_secrets
    unset GITHUB_TOKEN
    #printf "%s" "$GITHUB_TOKEN" | gh auth login --hostname Github.com --with-token
    if ! gh auth status ; then
        gh auth login
    fi
    source dev/secrets_configuration.sh
    gh secret set "TWINE_USERNAME" -b"${!VARNAME_TWINE_USERNAME}"
    gh secret set "TEST_TWINE_USERNAME" -b"${!VARNAME_TEST_TWINE_USERNAME}"
    toggle_setx_enter
    gh secret set "CI_SECRET" -b"${!VARNAME_CI_SECRET}"
    gh secret set "TWINE_PASSWORD" -b"${!VARNAME_TWINE_PASSWORD}"
    gh secret set "TEST_TWINE_PASSWORD" -b"${!VARNAME_TEST_TWINE_PASSWORD}"
    toggle_setx_exit
}


toggle_setx_enter(){
    # Can we do something like a try/finally?
    # https://stackoverflow.com/questions/15656492/writing-try-catch-finally-in-shell
    echo "Enter sensitive area"
    if [[ -n "${-//[^x]/}" ]]; then
        __context_1_toggle_setx=1
    else
        __context_1_toggle_setx=0
    fi
    if [[ "$__context_1_toggle_setx" == "1" ]]; then
        echo "Setx was on, disable temporarily"
        set +x
    fi
}

toggle_setx_exit(){
    echo "Exit sensitive area"
    # Can we guarantee this will happen?
    if [[ "$__context_1_toggle_setx" == "1" ]]; then
        set -x
    fi
}


upload_gitlab_group_secrets(){
    __doc__="
    Use the gitlab API to modify group-level secrets
    "
    # In Repo Directory
    load_secrets
    REMOTE=origin
    GROUP_NAME=$(git remote get-url $REMOTE | cut -d ":" -f 2 | cut -d "/" -f 1)
    HOST=https://$(git remote get-url $REMOTE | cut -d "/" -f 1 | cut -d "@" -f 2 | cut -d ":" -f 1)
    echo "
    * GROUP_NAME = $GROUP_NAME
    * HOST = $HOST
    "
    PRIVATE_GITLAB_TOKEN=$(git_token_for "$HOST")
    if [[ "$PRIVATE_GITLAB_TOKEN" == "ERROR" ]]; then
        echo "Failed to load authentication key"
        return 1
    fi

    TMP_DIR=$(mktemp -d -t ci-XXXXXXXXXX)
    curl --header "PRIVATE-TOKEN: $PRIVATE_GITLAB_TOKEN" "$HOST/api/v4/groups" > "$TMP_DIR/all_group_info"
    GROUP_ID=$(< "$TMP_DIR/all_group_info" jq ". | map(select(.path==\"$GROUP_NAME\")) | .[0].id")
    echo "GROUP_ID = $GROUP_ID"

    curl --header "PRIVATE-TOKEN: $PRIVATE_GITLAB_TOKEN" "$HOST/api/v4/groups/$GROUP_ID" > "$TMP_DIR/group_info"
    < "$TMP_DIR/group_info" jq

    # Get group-level secret variables
    curl --header "PRIVATE-TOKEN: $PRIVATE_GITLAB_TOKEN" "$HOST/api/v4/groups/$GROUP_ID/variables" > "$TMP_DIR/group_vars"
    < "$TMP_DIR/group_vars" jq '.[] | .key'

    if [[ "$?" != "0" ]]; then
        echo "Failed to access group level variables. Probably a permission issue"
    fi

    source dev/secrets_configuration.sh
    SECRET_VARNAME_ARR=(VARNAME_CI_SECRET VARNAME_TWINE_PASSWORD VARNAME_TEST_TWINE_PASSWORD VARNAME_TWINE_USERNAME VARNAME_TEST_TWINE_USERNAME VARNAME_PUSH_TOKEN)
    for SECRET_VARNAME_PTR in "${SECRET_VARNAME_ARR[@]}"; do
        SECRET_VARNAME=${!SECRET_VARNAME_PTR}
        echo ""
        echo " ---- "
        LOCAL_VALUE=${!SECRET_VARNAME}
        REMOTE_VALUE=$(< "$TMP_DIR/group_vars" jq -r ".[] | select(.key==\"$SECRET_VARNAME\") | .value")

        # Print current local and remote value of a variable
        echo "SECRET_VARNAME_PTR = $SECRET_VARNAME_PTR"
        echo "SECRET_VARNAME = $SECRET_VARNAME"
        echo "(local)  $SECRET_VARNAME = $LOCAL_VALUE"
        echo "(remote) $SECRET_VARNAME = $REMOTE_VALUE"

        #curl --request GET --header "PRIVATE-TOKEN: $PRIVATE_GITLAB_TOKEN" "$HOST/api/v4/groups/$GROUP_ID/variables/SECRET_VARNAME" | jq -r .message
        if [[ "$REMOTE_VALUE" == "" ]]; then
            # New variable
            echo "Remove variable does not exist, posting"

            toggle_setx_enter
            curl --request POST --header "PRIVATE-TOKEN: $PRIVATE_GITLAB_TOKEN" "$HOST/api/v4/groups/$GROUP_ID/variables" \
                    --form "key=${SECRET_VARNAME}" \
                    --form "value=${LOCAL_VALUE}" \
                    --form "protected=true" \
                    --form "masked=true" \
                    --form "environment_scope=*" \
                    --form "variable_type=env_var"
            toggle_setx_exit
        elif [[ "$REMOTE_VALUE" != "$LOCAL_VALUE" ]]; then
            echo "Remove variable does not agree, putting"
            # Update variable value
            toggle_setx_enter
            curl --request PUT --header "PRIVATE-TOKEN: $PRIVATE_GITLAB_TOKEN" "$HOST/api/v4/groups/$GROUP_ID/variables/$SECRET_VARNAME" \
                    --form "value=${LOCAL_VALUE}"
            toggle_setx_exit
        else
            echo "Remote value agrees with local"
        fi
    done
    rm "$TMP_DIR/group_vars"
}

upload_gitlab_repo_secrets(){
    __doc__="
    Use the gitlab API to modify group-level secrets
    "
    # In Repo Directory
    load_secrets
    REMOTE=origin
    GROUP_NAME=$(git remote get-url $REMOTE | cut -d ":" -f 2 | cut -d "/" -f 1)
    PROJECT_NAME=$(git remote get-url $REMOTE | cut -d ":" -f 2 | cut -d "/" -f 2 | cut -d "." -f 1)
    HOST=https://$(git remote get-url $REMOTE | cut -d "/" -f 1 | cut -d "@" -f 2 | cut -d ":" -f 1)
    echo "
    * GROUP_NAME = $GROUP_NAME
    * PROJECT_NAME = $PROJECT_NAME
    * HOST = $HOST
    "
    PRIVATE_GITLAB_TOKEN=$(git_token_for "$HOST")
    if [[ "$PRIVATE_GITLAB_TOKEN" == "ERROR" ]]; then
        echo "Failed to load authentication key"
        return 1
    fi

    TMP_DIR=$(mktemp -d -t ci-XXXXXXXXXX)
    toggle_setx_enter
    curl --header "PRIVATE-TOKEN: $PRIVATE_GITLAB_TOKEN" "$HOST/api/v4/groups" > "$TMP_DIR/all_group_info"
    toggle_setx_exit
    GROUP_ID=$(< "$TMP_DIR/all_group_info" jq ". | map(select(.path==\"$GROUP_NAME\")) | .[0].id")
    echo "GROUP_ID = $GROUP_ID"

    toggle_setx_enter
    curl --header "PRIVATE-TOKEN: $PRIVATE_GITLAB_TOKEN" "$HOST/api/v4/groups/$GROUP_ID" > "$TMP_DIR/group_info"
    toggle_setx_exit
    GROUP_ID=$(< "$TMP_DIR/all_group_info" jq ". | map(select(.path==\"$GROUP_NAME\")) | .[0].id")
    < "$TMP_DIR/group_info" jq

    PROJECT_ID=$(< "$TMP_DIR/group_info" jq ".projects | map(select(.path==\"$PROJECT_NAME\")) | .[0].id")
    echo "PROJECT_ID = $PROJECT_ID"

    # Get group-level secret variables
    toggle_setx_enter
    curl --header "PRIVATE-TOKEN: $PRIVATE_GITLAB_TOKEN" "$HOST/api/v4/projects/$PROJECT_ID/variables" > "$TMP_DIR/project_vars"
    toggle_setx_exit
    < "$TMP_DIR/project_vars" jq '.[] | .key'
    if [[ "$?" != "0" ]]; then
        echo "Failed to access project level variables. Probably a permission issue"
    fi

    LIVE_MODE=1
    source dev/secrets_configuration.sh
    SECRET_VARNAME_ARR=(VARNAME_CI_SECRET VARNAME_TWINE_PASSWORD VARNAME_TEST_TWINE_PASSWORD VARNAME_TWINE_USERNAME VARNAME_TEST_TWINE_USERNAME VARNAME_PUSH_TOKEN)
    for SECRET_VARNAME_PTR in "${SECRET_VARNAME_ARR[@]}"; do
        SECRET_VARNAME=${!SECRET_VARNAME_PTR}
        echo ""
        echo " ---- "
        LOCAL_VALUE=${!SECRET_VARNAME}
        REMOTE_VALUE=$(< "$TMP_DIR/project_vars" jq -r ".[] | select(.key==\"$SECRET_VARNAME\") | .value")

        # Print current local and remote value of a variable
        echo "SECRET_VARNAME_PTR = $SECRET_VARNAME_PTR"
        echo "SECRET_VARNAME = $SECRET_VARNAME"
        echo "(local)  $SECRET_VARNAME = $LOCAL_VALUE"
        echo "(remote) $SECRET_VARNAME = $REMOTE_VALUE"

        #curl --request GET --header "PRIVATE-TOKEN: $PRIVATE_GITLAB_TOKEN" "$HOST/api/v4/projects/$PROJECT_ID/variables/SECRET_VARNAME" | jq -r .message
        if [[ "$REMOTE_VALUE" == "" ]]; then
            # New variable
            echo "Remove variable does not exist, posting"
            if [[ "$LIVE_MODE" == "1" ]]; then
                curl --request POST --header "PRIVATE-TOKEN: $PRIVATE_GITLAB_TOKEN" "$HOST/api/v4/projects/$PROJECT_ID/variables" \
                        --form "key=${SECRET_VARNAME}" \
                        --form "value=${LOCAL_VALUE}" \
                        --form "protected=true" \
                        --form "masked=true" \
                        --form "environment_scope=*" \
                        --form "variable_type=env_var"
            else
                echo "dry run, not posting"
            fi
        elif [[ "$REMOTE_VALUE" != "$LOCAL_VALUE" ]]; then
            echo "Remove variable does not agree, putting"
            # Update variable value
            if [[ "$LIVE_MODE" == "1" ]]; then
                curl --request PUT --header "PRIVATE-TOKEN: $PRIVATE_GITLAB_TOKEN" "$HOST/api/v4/projects/$PROJECT_ID/variables/$SECRET_VARNAME" \
                        --form "value=${LOCAL_VALUE}"
            else
                echo "dry run, not putting"
            fi
        else
            echo "Remote value agrees with local"
        fi
    done
    rm "$TMP_DIR/project_vars"
}


export_encrypted_code_signing_keys(){
    # You will need to rerun this whenever the signkeys expire and are renewed

    # Load or generate secrets
    load_secrets

    source dev/secrets_configuration.sh

    CI_SECRET="${!VARNAME_CI_SECRET}"
    echo "VARNAME_CI_SECRET = $VARNAME_CI_SECRET"
    echo "CI_SECRET=$CI_SECRET"
    echo "GPG_IDENTIFIER=$GPG_IDENTIFIER"

    # ADD RELEVANT VARIABLES TO THE CI SECRET VARIABLES
    # HOW TO ENCRYPT YOUR SECRET GPG KEY
    # You need to have a known public gpg key for this to make any sense

    MAIN_GPG_KEYID=$(gpg --list-keys --keyid-format LONG "$GPG_IDENTIFIER" | head -n 2 | tail -n 1 | awk '{print $1}')
    GPG_SIGN_SUBKEY=$(gpg --list-keys --with-subkey-fingerprints "$GPG_IDENTIFIER" | grep "\[S\]" -A 1 | tail -n 1 | awk '{print $1}')
    # Careful, if you don't have a subkey, requesting it will export more than you want.
    # Export the main key instead (its better to have subkeys, but this is a lesser evil)
    if [[ "$GPG_SIGN_SUBKEY" == "" ]]; then
        # NOTE: if you get here this probably means your subkeys expired (and
        # wont even be visible), so we probably should check for that here and
        # thrown an error instead of using this hack, which likely wont work
        # anyway.
        GPG_SIGN_SUBKEY=$(gpg --list-keys --with-subkey-fingerprints "$GPG_IDENTIFIER" | grep "\[C\]" -A 1 | tail -n 1 | awk '{print $1}')
    fi
    echo "MAIN_GPG_KEYID  = $MAIN_GPG_KEYID"
    echo "GPG_SIGN_SUBKEY = $GPG_SIGN_SUBKEY"

    # Only export the signing secret subkey
    # Export plaintext gpg public keys, private sign key, and trust info
    mkdir -p dev
    gpg --armor --export-options export-backup --export-secret-subkeys "${GPG_SIGN_SUBKEY}!" > dev/ci_secret_gpg_subkeys.pgp
    gpg --armor --export "${GPG_SIGN_SUBKEY}" > dev/ci_public_gpg_key.pgp
    gpg --export-ownertrust > dev/gpg_owner_trust

    # Encrypt gpg keys and trust with CI secret
    GLKWS=$CI_SECRET openssl enc -aes-256-cbc -pbkdf2 -md SHA512 -pass env:GLKWS -e -a -in dev/ci_public_gpg_key.pgp > dev/ci_public_gpg_key.pgp.enc
    GLKWS=$CI_SECRET openssl enc -aes-256-cbc -pbkdf2 -md SHA512 -pass env:GLKWS -e -a -in dev/ci_secret_gpg_subkeys.pgp > dev/ci_secret_gpg_subkeys.pgp.enc
    GLKWS=$CI_SECRET openssl enc -aes-256-cbc -pbkdf2 -md SHA512 -pass env:GLKWS -e -a -in dev/gpg_owner_trust > dev/gpg_owner_trust.enc
    echo "$MAIN_GPG_KEYID" > dev/public_gpg_key

    # Test decrpyt
    GLKWS=$CI_SECRET openssl enc -aes-256-cbc -pbkdf2 -md SHA512 -pass env:GLKWS -d -a -in dev/ci_public_gpg_key.pgp.enc | gpg --list-packets --verbose
    GLKWS=$CI_SECRET openssl enc -aes-256-cbc -pbkdf2 -md SHA512 -pass env:GLKWS -d -a -in dev/ci_secret_gpg_subkeys.pgp.enc  | gpg --list-packets --verbose
    GLKWS=$CI_SECRET openssl enc -aes-256-cbc -pbkdf2 -md SHA512 -pass env:GLKWS -d -a -in dev/gpg_owner_trust.enc
    cat dev/public_gpg_key

    unload_secrets

    # Look at what we did, clean up, and add it to git
    ls dev/*.enc
    rm dev/*.pgp
    rm dev/gpg_owner_trust
    git status
    git add dev/*.enc
    git add dev/gpg_owner_trust
    git add dev/public_gpg_key
}


# See the xcookie module gitlab python API
#gitlab_set_protected_branches(){
#}


_test_gnu(){
    # shellcheck disable=SC2155
    export GNUPGHOME=$(mktemp -d -t)
    ls -al "$GNUPGHOME"
    chmod 700 -R "$GNUPGHOME"

    source dev/secrets_configuration.sh

    gpg -k

    load_secrets
    CI_SECRET="${!VARNAME_CI_SECRET}"
    echo "CI_SECRET = $CI_SECRET"

    cat dev/public_gpg_key
    GLKWS=$CI_SECRET openssl enc -aes-256-cbc -pbkdf2 -md SHA512 -pass env:GLKWS -d -a -in dev/ci_public_gpg_key.pgp.enc
    GLKWS=$CI_SECRET openssl enc -aes-256-cbc -pbkdf2 -md SHA512 -pass env:GLKWS -d -a -in dev/gpg_owner_trust.enc
    GLKWS=$CI_SECRET openssl enc -aes-256-cbc -pbkdf2 -md SHA512 -pass env:GLKWS -d -a -in dev/ci_secret_gpg_subkeys.pgp.enc

    GLKWS=$CI_SECRET openssl enc -aes-256-cbc -pbkdf2 -md SHA512 -pass env:GLKWS -d -a -in dev/ci_public_gpg_key.pgp.enc | gpg --import
    GLKWS=$CI_SECRET openssl enc -aes-256-cbc -pbkdf2 -md SHA512 -pass env:GLKWS -d -a -in dev/gpg_owner_trust.enc | gpg --import-ownertrust
    GLKWS=$CI_SECRET openssl enc -aes-256-cbc -pbkdf2 -md SHA512 -pass env:GLKWS -d -a -in dev/ci_secret_gpg_subkeys.pgp.enc | gpg --import

    gpg -k
    # | gpg --import
    # | gpg --list-packets --verbose
}

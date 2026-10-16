#!/bin/sh
set -eu

directory=$(CDPATH= cd -P "$(dirname "$0")" && pwd)
cd "$directory"
python=/app/.venv/bin/python

fail() { printf '%s\n' "$1" >&2; exit 1; }
usage() {
    printf '%s\n' \
        'Usage: sh moyai.sh configure [--from-env] [--secrets-from-env] [--topology standalone|split] [--temporal external|bundled]' \
        '       sh moyai.sh up [--timeout SECONDS]' \
        '       sh moyai.sh status|validate|check|password|recover-setup' \
        '       sh moyai.sh pause|resume|maintenance-status  (split installations)' \
        '       sh moyai.sh upgrade registry/image@sha256:digest'
}

action=${1:-help}
if [ "$#" -gt 0 ]; then shift; fi
case "$action" in help|--help|-h) usage; exit 0 ;; esac
command -v docker >/dev/null 2>&1 || fail 'Install Docker with Compose 2.30 or newer.'
version=$(docker compose version --short) || fail 'Docker Compose is unavailable.'
major=${version#v}; major=${major%%.*}
minor=${version#*.}; minor=${minor%%.*}
case "$major:$minor" in :*|*:|*[!0-9:]*) fail 'Could not determine the Docker Compose version.' ;; esac
if [ "$major" -lt 2 ] || { [ "$major" -eq 2 ] && [ "$minor" -lt 30 ]; }; then
    fail 'Docker Compose 2.30 or newer is required for raw configuration values.'
fi

valid_image() {
    case "$1" in ''|-*|*[!a-zA-Z0-9._/@:-]*) fail 'Select a valid release image reference.' ;; esac
}

if [ "$action" = configure ]; then
    for argument in "$@"; do
        case "$argument" in
            --image|--image=*|--directory|--directory=*)
                fail 'The bundle directory and image are selected by this helper.'
                ;;
        esac
    done
    [ ! -e .env ] && [ ! -L .env ] || fail 'Configuration already exists. It has not been changed.'
    image=${MOYAI_IMAGE:-}
    if [ -z "$image" ] && [ -f image.txt ]; then
        IFS= read -r image < image.txt || [ -n "$image" ]
    fi
    valid_image "$image"
    identity="$(id -u):$(id -g)"
    case "$(docker info --format '{{json .SecurityOptions}}')" in
        *'"name=rootless"'*) identity=0:0 ;;
    esac
    terminal=-i
    if [ -t 0 ] && [ -t 1 ]; then terminal=-it; fi
    exec docker run --rm "$terminal" --network none --user "$identity" \
        --volume "$directory:/install" --workdir /install --env HOME=/tmp \
        --env MOYAI_INSTALLATION_ID --env COMPOSE_PROJECT_NAME \
        --env MOYAI_INSTALL_TOPOLOGY --env MOYAI_TEMPORAL_MODE \
        --env MOYAI_BIND_ADDRESS --env MOYAI_PORT --env MOYAI_PUBLIC_URL \
        --env MOYAI_DATABASE_URL --env OBJECT_STORAGE_BUCKET --env OBJECT_STORAGE_REGION \
        --env OBJECT_STORAGE_ENDPOINT --env OBJECT_STORAGE_PREFIX --env MOYAI_NATIVE_OBJECT_PREFIX \
        --env OBJECT_STORAGE_ACCESS_KEY_ID --env OBJECT_STORAGE_SECRET_ACCESS_KEY --env OBJECT_STORAGE_SESSION_TOKEN \
        --env LITELLM_API_BASE --env LITELLM_API_KEY --env AGENT_MODEL \
        --env MODAL_TOKEN_ID --env MODAL_TOKEN_SECRET --env MODAL_APP_NAME \
        --env TEMPORAL_ADDRESS --env TEMPORAL_NAMESPACE --env TEMPORAL_TLS --env TEMPORAL_API_KEY \
        --env MOYAI_TEMPORAL_DATABASE_PASSWORD \
        --env CLOUDFLARE_ACCESS_CLIENT_ID --env CLOUDFLARE_ACCESS_CLIENT_SECRET \
        --env WORKSPACE_PASSWORD --env SESSION_SECRET --env ENCRYPTION_KEY --env MOYAI_NATIVE_ENCRYPTION_KEY \
        --entrypoint "$python" "$image" -m moyai.install_config --directory /install --image "$image" "$@"
fi

if [ "$action" = up ] && [ ! -e .env ] && [ ! -L .env ]; then
    printf '%s\n' 'Preparing a new installation from the exported environment.'
    sh "$directory/moyai.sh" configure --from-env --secrets-from-env
fi
[ -f .env ] || fail 'Export the required settings and run: sh moyai.sh up'
value() { awk -v name="$1" 'index($0,name "=")==1 {value=substr($0,length(name)+2)} END {sub(/\r$/,"",value); print value}' .env; }
COMPOSE_FILE=$(value COMPOSE_FILE)
COMPOSE_PROJECT_NAME=$(value COMPOSE_PROJECT_NAME)
MOYAI_IMAGE=$(value MOYAI_IMAGE)
MOYAI_BIND_ADDRESS=$(value MOYAI_BIND_ADDRESS)
MOYAI_PORT=$(value MOYAI_PORT)
stored_password=$(value MOYAI_TEMPORAL_DATABASE_PASSWORD)
if [ -n "$stored_password" ]; then MOYAI_TEMPORAL_DATABASE_PASSWORD=$stored_password; fi
valid_image "$MOYAI_IMAGE"
[ -n "$COMPOSE_FILE" ] && [ -n "$COMPOSE_PROJECT_NAME" ] || fail 'The configuration lacks its Compose files or project name.'
COMPOSE_PATH_SEPARATOR=:
# Only the interpolation settings above are read by the shell. Service settings
# remain raw env_file values, including literal dollars and quotes in credentials.
COMPOSE_ENV_FILES=/dev/null
export COMPOSE_FILE COMPOSE_PROJECT_NAME COMPOSE_PATH_SEPARATOR COMPOSE_ENV_FILES MOYAI_IMAGE
export MOYAI_BIND_ADDRESS MOYAI_PORT
if [ "${MOYAI_TEMPORAL_DATABASE_PASSWORD+x}" ]; then export MOYAI_TEMPORAL_DATABASE_PASSWORD; fi
compose() { docker compose --project-directory "$directory" --env-file /dev/null --project-name "$COMPOSE_PROJECT_NAME" "$@"; }
command_in_image() { compose run --rm --no-deps setup "$python" -m moyai.deployment "$@"; }

case "$action" in
    up)
        timeout=600
        if [ "$#" -eq 2 ] && [ "$1" = --timeout ]; then timeout=$2; shift 2; fi
        [ "$#" -eq 0 ] || { usage; exit 1; }
        case "$timeout" in ''|*[!0-9]*) fail 'The startup timeout must be a positive number of seconds.' ;; esac
        timeout=$(awk -v value="$timeout" 'BEGIN {value+=0; if(value<1 || value>86400) exit 1; printf "%.0f",value}') ||
            fail 'The startup timeout must be between 1 and 86400 seconds.'
        # This cannot migrate or rotate keys, and runs before replacing services.
        command_in_image validate
        compose up -d --wait --wait-timeout "$timeout"
        if compose config --services | grep -qx activate; then
            container=$(compose ps --all --quiet activate)
            [ -n "$container" ] || fail 'The activation service was not created.'
            remaining=$timeout
            while [ "$(docker inspect --format '{{.State.Running}}' "$container")" = true ]; do
                [ "$remaining" -gt 0 ] || fail 'Activation is still pending. Inspect: docker compose logs activate'
                sleep 1
                remaining=$((remaining - 1))
            done
            [ "$(docker inspect --format '{{.State.ExitCode}}' "$container")" = 0 ] ||
                fail 'Activation failed. Inspect: docker compose logs activate'
        fi
        command_in_image ready
        printf '%s\n' 'Sign in and run a real file task to verify model and computer access.' \
            'To retrieve the generated administrator password privately: sh moyai.sh password'
        ;;
    status|validate|check|password|recover-setup)
        [ "$#" -eq 0 ] || { usage; exit 1; }
        if [ "$action" = status ]; then action=ready; fi
        command_in_image "$action"
        ;;
    pause|resume|maintenance-status)
        [ "$#" -eq 0 ] || { usage; exit 1; }
        if compose config --services | grep -qx app; then
            fail 'Coordinated maintenance commands require the split topology.'
        fi
        if [ "$action" = maintenance-status ]; then action=status; fi
        command_in_image "$action"
        ;;
    upgrade)
        [ "$#" -eq 1 ] || { usage; exit 1; }
        exec sh "$directory/upgrade.sh" "$1"
        ;;
    *) usage; exit 1 ;;
esac

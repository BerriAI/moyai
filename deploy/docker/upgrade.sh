#!/bin/sh
set -eu

candidate=${1:?Usage: sh upgrade.sh registry/image@sha256:digest}
case "$candidate" in
    *@sha256:*) ;;
    *) echo "Select an immutable image digest." >&2; exit 1 ;;
esac

docker pull "$candidate"
revision=$(docker run --rm --entrypoint /bin/cat "$candidate" /app/build-revision)
if docker compose config --services | grep -qx app; then
    topology=standalone
    source_revision=$(docker compose exec -T app /bin/cat /app/build-revision)
    docker compose stop proxy app
    MOYAI_IMAGE="$candidate" docker compose run --rm --no-deps setup \
        /app/native/.venv/bin/python -m moyai.deployment standalone-upgrade --source-revision "$source_revision"
else
    topology=split
    docker compose run --rm --no-deps setup /app/native/.venv/bin/python -m moyai.deployment \
        begin-upgrade --revision "$revision"
    docker compose stop proxy api worker broker coordinator
    MOYAI_IMAGE="$candidate" docker compose run --rm --no-deps setup \
        /app/native/.venv/bin/python -m moyai.deployment apply-upgrade
fi

umask 077
awk -v image="$candidate" '
    /^MOYAI_IMAGE=/ { print "MOYAI_IMAGE=" image; found=1; next }
    { print }
    END { if (!found) print "MOYAI_IMAGE=" image }
' .env > .env.next
mv .env.next .env

if [ "$topology" = split ]; then
    MOYAI_IMAGE="$candidate" docker compose up -d --wait coordinator broker worker api
    MOYAI_IMAGE="$candidate" docker compose run --rm --no-deps setup \
        /app/native/.venv/bin/python -m moyai.deployment resume
else
    MOYAI_IMAGE="$candidate" docker compose up -d --wait app
fi
MOYAI_IMAGE="$candidate" docker compose up -d proxy

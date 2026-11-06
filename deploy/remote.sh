#!/usr/bin/env bash
# Runs on the VPS over SSH. No secrets are transferred by CI.
set -euo pipefail
release=${1:?Commit SHA required}
[[ "$release" =~ ^[0-9a-f]{40}$ ]] || { echo 'Invalid commit SHA' >&2; exit 1; }
base="$HOME/radar-promocoes"
export RADAR_SHARED_DIR="$base/shared"
export RADAR_UID RADAR_GID
RADAR_UID=$(id -u)
RADAR_GID=$(id -g)
mkdir -p "$base/releases" "$RADAR_SHARED_DIR/data"
chmod 700 "$RADAR_SHARED_DIR" "$RADAR_SHARED_DIR/data"
exec 9>"$base/deploy.lock"
flock -w 300 9 || { echo 'Another deploy holds the lock' >&2; exit 1; }
archive="$base/$release.tar.gz"
trap 'rm -f "$archive"' EXIT
[[ -s "$RADAR_SHARED_DIR/.env" ]] || {
  echo 'Create ~/radar-promocoes/shared/.env before deploying (see deploy/README.md).' >&2
  exit 1
}
chmod 600 "$RADAR_SHARED_DIR/.env"
# Releases contain code only. Persistent data lives outside them.
mkdir -p "$base/releases/$release"
tar -xzf "$archive" -C "$base/releases/$release"
cd "$base/releases/$release"
export RADAR_RELEASE="$release"
docker compose build --pull radar
# Do not stop the current service if the session has not been provisioned.
[[ -s "$RADAR_SHARED_DIR/data/telegram.session" ]] || {
  echo "Authenticate first: cd $base/releases/$release; export RADAR_SHARED_DIR=$base/shared RADAR_UID=$RADAR_UID RADAR_GID=$RADAR_GID RADAR_RELEASE=$release; docker compose run --rm --no-deps radar python -m radar.login" >&2
  exit 1
}
previous=$(readlink "$base/current" || true)
if docker compose up -d --no-build --wait --wait-timeout 120 radar; then
  ln -sfn "$base/releases/$release" "$base/current"
  echo "Deployed $release; radar is healthy."
else
  echo 'New release failed its health check.' >&2
  docker compose ps
  # Do not print application logs: runtime logs can include external message content.
  if [[ -n "$previous" && -d "$previous" && "$previous" != "$base/releases/$release" ]]; then
    export RADAR_RELEASE
    RADAR_RELEASE=$(basename "$previous")
    cd "$previous"
    docker compose up -d --no-build --wait --wait-timeout 120 radar
    echo "Restored previous release $RADAR_RELEASE. Database and session preserved."
  else
    docker compose stop radar
    echo 'No previous successful release; unhealthy service stopped.' >&2
  fi
  exit 1
fi

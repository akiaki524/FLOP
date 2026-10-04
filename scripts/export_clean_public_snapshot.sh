#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/export_clean_public_snapshot.sh DESTINATION [COMMIT]

Create a new local Git repository from one sanitized FLOP tree.
The destination receives exactly one root commit and no source repository
history, branches, pull-request refs, Issues, Actions history, or remotes.

This command never pushes and never changes the source repository.
EOF
}

if [[ ${1:-} == "-h" || ${1:-} == "--help" || $# -lt 1 || $# -gt 2 ]]; then
  usage
  [[ $# -ge 1 ]] && exit 0 || exit 2
fi

dest=$1
commit=${2:-HEAD}
root=$(git rev-parse --show-toplevel)

if [[ -e "$dest" || -L "$dest" ]]; then
  echo "destination already exists: $dest" >&2
  exit 2
fi
dest=$(realpath -m -- "$dest")

cd "$root"
python3 -B scripts/check_public_hygiene.py

source_commit=$(git rev-parse "$commit^{commit}")
source_tree=$(git rev-parse "$source_commit^{tree}")
mkdir -p -- "$(dirname -- "$dest")"
# Only a destination created by this invocation may be removed on failure.
mkdir -- "$dest"
complete=false
cleanup() {
  if [[ "$complete" != true ]]; then
    rm -rf -- "$dest"
  fi
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
git -c core.autocrlf=false archive --format=tar "$source_commit" | tar -xf - -C "$dest"

git -C "$dest" init -b main
git -C "$dest" config user.name "akiaki524"
git -C "$dest" config user.email "260222483+akiaki524@users.noreply.github.com"
git -C "$dest" config core.autocrlf false
git -C "$dest" config core.hooksPath /dev/null
git -C "$dest" add -A
GIT_AUTHOR_NAME=akiaki524 \
GIT_AUTHOR_EMAIL=260222483+akiaki524@users.noreply.github.com \
GIT_COMMITTER_NAME=akiaki524 \
GIT_COMMITTER_EMAIL=260222483+akiaki524@users.noreply.github.com \
  git -C "$dest" commit --no-gpg-sign -m "Initial public release"

expected_identity=$(printf '%s\n' \
  'akiaki524' '260222483+akiaki524@users.noreply.github.com' \
  'akiaki524' '260222483+akiaki524@users.noreply.github.com')
if [[ $(git -C "$dest" log -1 --format='%an%n%ae%n%cn%n%ce') != "$expected_identity" ]]; then
  echo "exported commit identity differs from expected Project-owner identity" >&2
  exit 1
fi

count=$(git -C "$dest" rev-list --count HEAD)
if [[ "$count" != "1" ]]; then
  echo "expected one public commit, found $count" >&2
  exit 1
fi

public_tree=$(git -C "$dest" rev-parse "HEAD^{tree}")
if [[ "$public_tree" != "$source_tree" ]]; then
  echo "exported tree differs from reviewed source tree" >&2
  exit 1
fi

(cd "$dest" && python3 -B scripts/check_public_hygiene.py)
(cd "$dest" && python3 -B scripts/check_public_commit_metadata.py HEAD)

if git -C "$dest" remote | grep -q .; then
  echo "export unexpectedly contains a remote" >&2
  exit 1
fi

complete=true
printf 'Clean public snapshot created.\n'
printf 'Source commit (keep private unless intentionally disclosed): %s\n' "$source_commit"
printf 'Destination: %s\n' "$dest"
printf 'Public commit: %s\n' "$(git -C "$dest" rev-parse HEAD)"
printf 'Tree identity: %s\n' "$public_tree"
printf 'No remote was configured and nothing was pushed.\n'

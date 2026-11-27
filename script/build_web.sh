#!/bin/bash
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
source="$root/device-webapp"
staged="$root/.build/web-source"
mkdir -p "$staged"
rsync -a --delete --exclude node_modules --exclude .microworld-npm-lock "$source/" "$staged/"
cd "$staged"
if [[ -L node_modules ]]; then
  unlink node_modules
fi
if ! cmp -s .microworld-npm-lock package-lock.json || [[ ! -x node_modules/.bin/vue-cli-service ]]; then
  npm ci --no-audit --no-fund --legacy-peer-deps --cache "$root/.build/cache/npm"
  cp package-lock.json .microworld-npm-lock
fi
python3 "$root/script/adapt_web.py" "$staged" "$root/Resources"
NODE_OPTIONS=--openssl-legacy-provider VUE_APP_ENABLE_IMJOY=false node_modules/.bin/vue-cli-service build --mode production --dest "$root/.build/Web" --skip-plugins @vue/cli-plugin-eslint
python3 "$root/script/app_icon.py" --web "$root/.build/Web"

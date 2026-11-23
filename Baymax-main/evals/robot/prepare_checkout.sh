#!/bin/bash
# Give a PR checkout a Python environment on the test robot.
#
#   prepare_checkout.sh /path/to/pr-repo-root
#
# Composes with the devops/opi4-flash image: its ember-update installs a validated
# release at ~/ember-deployment/current with a ready .venv. When the PR's
# requirements and bootstrap are identical to that release's, the checkout reuses
# that environment (as ember-update itself does for code-only updates). Otherwise it
# installs one the way ember-update does: devops/clickables/bootstrap.py, plus the v10
# native packages when the PR runs realtime_gemini_10.py. The image's files are only read.
set -Eeuo pipefail
REPO=$(cd "${1:?usage: prepare_checkout.sh PR_REPO_ROOT}" && pwd)
APP="$REPO/Baymax-main"
INSTALLED=${EMBER_RIG_INSTALLED:-$HOME/ember-deployment/current}
PROVISION=/opt/ember-provision

if [ -x "$APP/.venv/bin/python" ]; then
  echo "Environment already present: $APP/.venv"
  exit 0
fi

same_requirements() {
  local file
  for file in "$REPO"/Baymax-main/requirements*.txt "$INSTALLED"/Baymax-main/requirements*.txt; do
    local relative=${file#"$REPO"/}
    relative=${relative#"$INSTALLED"/}
    cmp -s "$REPO/$relative" "$INSTALLED/$relative" || return 1
  done
  cmp -s "$REPO/devops/clickables/bootstrap.py" "$INSTALLED/devops/clickables/bootstrap.py" 2>/dev/null ||
    { [ ! -e "$REPO/devops/clickables/bootstrap.py" ] && [ ! -e "$INSTALLED/devops/clickables/bootstrap.py" ]; }
}

if [ -f "$INSTALLED/Baymax-main/.venv/.ember-ready.json" ] && same_requirements; then
  ln -s "$(readlink -f "$INSTALLED/Baymax-main/.venv")" "$APP/.venv"
  echo "Requirements unchanged from the installed release; reusing its environment."
  exit 0
fi

PYTHON=/opt/ember-python/python/bin/python3.12
[ -x "$PYTHON" ] || PYTHON=$(command -v python3.12 || command -v python3)
if [ -f "$REPO/devops/clickables/bootstrap.py" ]; then
  UV=${UV:-$HOME/.local/bin/uv}
  [ -d "$PROVISION/wheels" ] && export UV_FIND_LINKS="$PROVISION/wheels"
  "$PYTHON" "$REPO/devops/clickables/bootstrap.py" setup --uv "$UV" --install-only
else
  "$PYTHON" -m venv "$APP/.venv"
  "$APP/.venv/bin/python" -m pip install --quiet -r "$APP/requirements-orange-pi.txt"
fi
if grep -q 'realtime_gemini_10.py' "$APP/run_pc.py" && [ -f "$PROVISION/ember-v10-requirements.txt" ]; then
  "$UV" pip install --python "$APP/.venv/bin/python" -r "$PROVISION/ember-v10-requirements.txt"
fi
echo "Installed a fresh environment for the PR."

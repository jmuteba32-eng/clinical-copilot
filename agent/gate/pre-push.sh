#!/usr/bin/env bash
# Week 2 eval gate -- runs the fast offline rubric suite before every push.
# If any gate test fails, the push is ABORTED. This is the PR-blocking gate:
# a regression cannot leave this machine. Live suite (agent/evals/) is NOT
# run here -- it needs live OpenEMR/Anthropic and is a pre-deploy step.
#
# Install as a git pre-push hook (from repo root):
#   cp agent/gate/pre-push.sh .git/hooks/pre-push && chmod +x .git/hooks/pre-push
set -euo pipefail

echo "[pre-push] Running Week 2 offline eval gate (agent/gate/)..."

python -m pytest agent/gate -q -p no:cacheprovider
status=$?

if [ $status -ne 0 ]; then
  echo ""
  echo "[pre-push] GATE FAILED -- push aborted. Fix the failing rubric test(s) above."
  exit 1
fi

echo "[pre-push] Gate passed. Proceeding with push."
exit 0

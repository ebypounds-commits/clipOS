#!/usr/bin/env bash
set -euo pipefail
printf 'ClipOS M-1 preflight\n'
printf 'date: '; date -Is || true
printf 'user: '; id
printf 'pwd: '; pwd
printf 'git: '; git --version
printf 'branch: '; git branch --show-current
printf 'remote: '; git remote -v || true
printf 'status:\n'; git status --short
printf 'docker: '; docker --version
printf 'compose: '; docker compose version
printf 'python: '; python3 --version || true
printf 'node: '; node --version || true
printf 'npm: '; npm --version || true
printf 'disk:\n'; df -h .
printf 'memory:\n'; free -h || true
printf 'worktree list:\n'; git worktree list
printf 'tracked secret-like files check:\n'
git ls-files | grep -E '(^|/)(\.env($|\.)|.*\.(pem|key|p12|pfx)$)' && { echo 'Review tracked secret-like files above'; exit 2; } || true
printf 'preflight shell checks complete\n'

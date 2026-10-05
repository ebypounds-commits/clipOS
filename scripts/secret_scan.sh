#!/usr/bin/env bash
set -euo pipefail

pattern='(^|/)(\.env($|\.)|.*\.(pem|key|p12|pfx)$)'
secret_patterns='sk-[A-Za-z0-9]{20,}|sk-or-v1-[A-Za-z0-9]{8,}|ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----'

if git ls-files | grep -E "$pattern"; then
    echo 'secret_scan: blocked tracked secret-like path(s) found' >&2
    exit 1
fi

while IFS= read -r file; do
    if [ -f "$file" ] && grep -IqE "$secret_patterns" "$file"; then
        echo "secret_scan: likely secret found in $file" >&2
        exit 1
    fi
done < <(git ls-files -z | tr '\0' '\n')

echo 'secret_scan: clean'

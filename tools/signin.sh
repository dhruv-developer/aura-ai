#!/usr/bin/env bash
# Generate one-time AURA sign-in links for local testing (dev/test only).
#
#   tools/signin.sh <email>            link for the web app (http://localhost:3000)
#   tools/signin.sh <email> --open     ...and open it in your default browser
#   tools/signin.sh --admin <email>    link for the admin console (http://localhost:3001)
#   tools/signin.sh --all              a link for every demo account
#   tools/signin.sh --seed             (re)create the demo accounts first, then --all
#
# Links work once and expire after 15 minutes. Unknown emails get a new personal
# account on first sign-in. Open links in a private window to be two users at once
# (all localhost apps share one sign-in cookie).
set -euo pipefail
cd "$(dirname "$0")/.."

DEMO_WEB=(free@demo.aura plus@demo.aura pro@demo.aura owner@acme.demo admin@acme.demo dev@acme.demo viewer@acme.demo)
DEMO_ADMIN=admin@aura.demo

link() { uv run --quiet aura-api login-link --email "$1" --app "$2" 2>/dev/null | tail -n 1; }

usage() { sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

[[ $# -eq 0 ]] && usage 1
case "$1" in
  -h|--help) usage ;;
  --seed)
    uv run --quiet aura-api seed-demo
    exec "$0" --all
    ;;
  --all)
    printf '\n%-18s %s\n' "ACCOUNT" "SIGN-IN LINK (one-time, 15 min)"
    for e in "${DEMO_WEB[@]}"; do printf '%-18s %s\n' "$e" "$(link "$e" web)"; done
    printf '%-18s %s\n' "$DEMO_ADMIN" "$(link "$DEMO_ADMIN" admin)   (admin console)"
    ;;
  --admin)
    [[ $# -ge 2 ]] || usage 1
    url="$(link "$2" admin)"; echo "$url"
    if [[ "${3:-}" == "--open" ]]; then open "$url"; fi
    ;;
  *)
    url="$(link "$1" web)"; echo "$url"
    if [[ "${2:-}" == "--open" ]]; then open "$url"; fi
    ;;
esac

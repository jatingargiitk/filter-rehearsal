#!/usr/bin/env bash
# Re-runs every command shown in README.md and records its output (and exit
# code) under examples/out/. CI runs this and fails if anything changed;
# tests/test_readme.py checks each recorded output appears in README.md.
set -u
cd "$(dirname "$0")/.."
mkdir -p examples/out

record() {  # record NAME -- command...
  local name="$1"; shift; shift
  local out="examples/out/$name.txt"
  "$@" > "$out" 2>&1
  echo "exit $?" >> "$out"
}

record run -- filter-rehearsal run examples/events.jsonl -f examples/proposed.json -k examples/must-keep.json
record baseline -- filter-rehearsal run examples/events.jsonl -f examples/proposed.json --baseline examples/current.json --fail-on-new --format markdown
record try -- filter-rehearsal run examples/events.jsonl --try 'error_type:TypeError release:<2.0.0' -k examples/must-keep.json
record explain -- filter-rehearsal explain examples/events.jsonl --event-id ae87 --try 'error_message:*CartStateException*' --try 'error_type:a.b.c'
echo "recorded: $(ls examples/out | tr '\n' ' ')"

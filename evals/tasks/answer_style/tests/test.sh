#!/bin/sh
set -eu
mkdir -p /logs/verifier
python /tests/judge.py \
  --case /tests/case.json \
  --result /eval/moyai_result.json \
  --requests /logs/agent/stub_requests.jsonl \
  --output /logs/verifier/judgment.json \
  --reward /logs/verifier/reward.txt

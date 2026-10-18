#!/bin/sh
set -eu
mkdir -p /logs/verifier
python3 /tests/verify.py --task preferences --result /eval/moyai_result.json --output /logs/verifier/judgment.json --reward /logs/verifier/reward.txt

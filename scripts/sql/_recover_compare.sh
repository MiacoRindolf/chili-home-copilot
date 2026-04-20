#!/bin/sh
set -eu
psql -U chili -d chili_recover -Atc 'SELECT name FROM scan_patterns' | sort > /tmp/recover_names.txt
psql -U chili -d chili -Atc 'SELECT name FROM scan_patterns' | sort > /tmp/live_names.txt
echo "== collisions =="
comm -12 /tmp/recover_names.txt /tmp/live_names.txt
echo "== recover total =="
wc -l /tmp/recover_names.txt
echo "== live total =="
wc -l /tmp/live_names.txt

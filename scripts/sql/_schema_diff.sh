#!/bin/bash
TABLE="$1"
psql -U chili -d chili_recover -Atc "SELECT column_name FROM information_schema.columns WHERE table_name='$TABLE' ORDER BY column_name" > /tmp/recover_cols.txt
psql -U chili -d chili -Atc "SELECT column_name FROM information_schema.columns WHERE table_name='$TABLE' ORDER BY column_name" > /tmp/live_cols.txt
echo "== ONLY IN RECOVER =="
comm -23 /tmp/recover_cols.txt /tmp/live_cols.txt
echo "== ONLY IN LIVE =="
comm -13 /tmp/recover_cols.txt /tmp/live_cols.txt
echo "== COMMON COUNT =="
comm -12 /tmp/recover_cols.txt /tmp/live_cols.txt | wc -l

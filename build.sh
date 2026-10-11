#!/usr/bin/env bash
# Render build step. Runs once per deploy, before the service starts.
#
# The sample Parquet dataset is NOT in git (data/ is gitignored, and committing
# binary fixtures to a repo people will read is worse than regenerating them),
# so it is generated here. Everything downstream -- the catalog row, the demo
# history, the numbers on every page -- is derived from this data at build time.
set -o errexit

pip install --upgrade pip
pip install -e .

# Keep this modest. Render's free instance has 512 MB of RAM, and the engine
# fans row-groups across a process pool: a dataset big enough to be slow here is
# a dataset big enough to OOM the web service later. 300k rows over 15 files
# still yields ~120 row-groups, which is what makes pruning visible.
python scripts/seed_data.py --out ./data/trips --rows "${SEED_ROWS:-300000}" --days 5

python manage.py migrate --noinput
python manage.py register_dataset trips ./data/trips \
  --description "NYC-style taxi trips, Hive-partitioned by region and date."

# Real executions against the real dataset, so the deployed site opens with
# measured numbers instead of empty states.
python manage.py seed_demo_runs --passes 3

python manage.py collectstatic --noinput

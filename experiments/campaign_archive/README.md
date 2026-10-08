# Archived Campaign Files

This directory contains campaign files that are no longer part of the active
R4 production path.

- `historical/WORKLOG.md` preserves the closed W1-W15 campaign-infrastructure
  worklog. Current refactor reports belong in `REFACTOR_WORKLOG.md`.
- `legacy/submit.sh` and `legacy/submit.slurm` are the superseded single-worker
  submission path. The active submission path is
  `experiments/campaign/submit_sharded.sh` with `submit_sharded.slurm`.
- `legacy/model_smoke.slurm` is the superseded standalone model smoke wrapper;
  it is not part of the R4 campaign launch path.

The active campaign directory is `experiments/campaign/`. Do not dispatch or
report current work through archived files.

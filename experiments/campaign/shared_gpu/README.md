# Shared-GPU Pilot

This directory contains an experimental launcher for measuring whether two
independent HackAgent workers can share one H200 more efficiently than the
baseline one-worker-per-GPU campaign.

The pilot deliberately does not modify the baseline launcher. It starts one
private Ollama server, keeps the target, judge, and one attacker resident, and
launches two workers against distinct immutable shards with:

```text
OLLAMA_NUM_PARALLEL=2
one H200
two isolated shard databases
two independent output trees
```

The workers write directly to the manifest's canonical paths:

```text
<campaign-root>/shards/<shard-id>/
```

Only launcher, Ollama, and worker logs are written below
`<campaign-root>/shared_gpu/`. This preserves downstream reharvest and merge
discovery.

The launcher preloads each unique model once through Ollama and sets
`HACKAGENT_SKIP_CAMPAIGN_WARMUP=1` for both workers. The ordinary campaign path
still performs its existing diagnostic warmups.

For a campaign-wide pair array, use `setup_campaign.sh` with one or more
`--attacker-key` arguments. It defaults to six shards per attacker, yielding an
even shard count that can be paired two-at-a-time. `submit_array.sh --max-pairs`
limits how many physical H200 pair jobs are submitted without changing the
immutable manifest.

The two shard IDs must have the same attacker key and disjoint cell IDs. The
launcher rejects overlap before allocating resources or starting Ollama.

This path is experimental. It is not the production campaign submission path.

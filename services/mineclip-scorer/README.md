# MineCLIP scorer service

This service isolates the official MineCLIP model and Torch dependencies from the harness backend.
It accepts exactly 16 RGB frames plus one target prompt and one or more negative prompts.

The setup script pins the official repository commit used during Week 11 and downloads the selected
checkpoint directly from the upstream MineCLIP Google Drive release. Model weights remain ignored by
Git and are not redistributed by this project. The script verifies the published MD5 and prefetches
the CLIP tokenizer into a local ignored cache.

```bash
make mineclip-scorer-setup
make mineclip-scorer-start
make mineclip-scorer-status
make mineclip-scorer-smoke
make mineclip-scorer-stop
```

`services/mineclip-scorer/.env.local` contains the generated absolute runtime paths. Override
`MINECLIP_DEVICE`, `MINECLIP_SCORER_HOST`, or `MINECLIP_SCORER_PORT` in the environment when needed.
The default `auto` device selects CUDA, Apple MPS, then CPU.

To install the smaller-pooling upstream variant instead:

```bash
scripts/setup_mineclip_scorer.sh --variant avg
```

The scorer process is not required during Minecraft execution. The Week 11 managed workflow starts
it after recording and stops it after offline evaluation, keeping unified-memory pressure bounded.

Manual readiness check:

```bash
curl http://127.0.0.1:8091/health
```

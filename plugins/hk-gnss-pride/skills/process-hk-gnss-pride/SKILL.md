---
name: process-hk-gnss-pride
description: Download Hong Kong SatRef GNSS RINEX 3 observation files, batch-process them with PRIDE PPP-AR using static positioning, VMF3, and STO models for ZTD and horizontal gradients, validate outputs, inspect temporal coverage, and resume prior runs. Use when Codex is asked to download Hong Kong CORS data, run or rerun PRIDE PPP-AR over one or more dates or stations, automate GNSS atmospheric preprocessing for water-vapor tomography, replace selected PRIDE results, or quality-check ZTD, HTG, and residual products.
---

# Hong Kong GNSS PRIDE pipeline

Use the bundled `scripts/run_pipeline.py` from this skill directory. Keep all paths workspace-relative or supply them explicitly.

## Fixed scientific strategy

- Download daily RINEX 3 observation files at 30 s sampling from Hong Kong SatRef.
- Run PRIDE PPP-AR with static positioning, VMF3, STO ZTD, and STO horizontal gradients: `pdp3 -m S -p V3 -z S -h S`.
- Preserve original compressed RINEX files.
- Process different days in parallel and stations within a day sequentially.
- Treat dates as UTC and include both endpoints.

If the user requests a different sampling rate, positioning mode, mapping function, or tropospheric parameterization, explain that the bundled pipeline encodes the fixed strategy above and adapt it deliberately rather than silently changing the science.

## Resolve the environment

1. Treat the current workspace as the default project root. Pass `--project-root` when data belongs elsewhere.
2. Locate `pdp3` from `--pdp3`, `$PDP3`, `$PRIDE_PPPAR_HOME/pdp3`, `PATH`, or the conventional user installation at `~/.PRIDE_PPPAR_BIN/pdp3`.
3. Locate Python from `--python`, `$HK_GNSS_PYTHON`, `<project-root>/.venv/bin/python`, or the active Python.
4. Locate Hatanaka decompression from `--rinex-decompress`, `$RINEX_DECOMPRESS`, the selected Python's executable directory, the project virtual environment, or `PATH`.
5. Run `--dry-run` before a real solve to confirm the resolved environment and paths.

The PRIDE installation must contain its runtime tables and a working configuration template. Do not bundle or redistribute PRIDE binaries or proprietary/external data products in this plugin.

## Run the workflow

Set `SKILL_DIR` to the directory containing this `SKILL.md`, then invoke:

```bash
python3 "$SKILL_DIR/scripts/run_pipeline.py" \
  --project-root "$PWD" \
  --start 2026-05-10 --end 2026-05-12 --stations all
```

Reuse downloaded RINEX without a network download:

```bash
python3 "$SKILL_DIR/scripts/run_pipeline.py" \
  --project-root "$PWD" \
  --start 2026-05-10 --end 2026-05-12 --stations hkcl,kyc1 \
  --skip-download
```

By default, store downloads under `<project-root>/data/rinex3` and solutions under `<project-root>/data/pride_results/solution`. Override them with `--data-root` or `--result-root`.

By default, skip an existing station only when all required products pass validation. Add `--overwrite-results` only when the user explicitly asks to replace the selected days. This option removes only the selected three-digit day directories under the configured result, log, and staging roots before solving.

## Validate and report

1. Read `pipeline_summary.json` and relevant station logs after execution; do not judge success only by the process exit code.
2. Verify that `ztd`, `htg`, `res`, `pos`, `amb`, and `log` products exist and are nonempty.
3. Verify that ZTD and HTG headers say `STO` and that the saved PRIDE configuration says `VM3`.
4. Report partial coverage, missing days, failed stations, and the report path.
5. Treat any HTG count other than 2880 for a 30 s UTC day as a coverage warning, not an automatic solver failure.

Read [references/output-files.md](references/output-files.md) when interpreting atmospheric products or explaining quality checks.

## Handle failures safely

- If `rinex-decompress` is missing, create or use a Python environment and install `hatanaka` only with authorization.
- If `pdp3` is missing, ask for its path or installation; do not download an unofficial binary.
- Product-download retries, QZSS product warnings, or partial receiver coverage do not by themselves prove that ZTD or HTG is invalid.
- Do not delete raw RINEX. Do not use `--overwrite-results` without an explicit replacement request.
- Retain failed station logs and staging inputs, report the exact failed station-day, and resume remaining work safely.

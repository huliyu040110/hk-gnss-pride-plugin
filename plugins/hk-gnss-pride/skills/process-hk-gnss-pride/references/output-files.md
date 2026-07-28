# PRIDE atmospheric output notes

## Fixed processing model

The bundled pipeline uses:

- static PPP-AR: `-m S`
- VMF3 mapping function: `-p V3`, recorded as `VM3` in the generated configuration
- stochastic ZTD: `-z S`, recorded as `ZTD model = STO`
- stochastic horizontal gradients: `-h S`, recorded as `HTG model = STO`

## Main station files

For station `ssss`, year `YYYY`, and day of year `DDD`:

- `ztd_YYYYDDD_ssss`: zenith tropospheric delay estimates
- `htg_YYYYDDD_ssss`: horizontal tropospheric gradient estimates
- `res_YYYYDDD_ssss`: observation residuals used in later slant-delay and quality-control work
- `pos_YYYYDDD_ssss`: estimated station positions
- `amb_YYYYDDD_ssss`: ambiguity results
- `log_YYYYDDD_ssss`: PRIDE processing log produced as part of the solution

The pipeline also writes its own execution log at `logs/YYYY/DDD/ssss.log` and a cross-run JSON report at `pipeline_summary.json`.

## HTG interpretation

The horizontal gradient series is reconstructed from initial and correction columns:

```text
G_north = HTGCini + HTGCcor
G_east  = HTGSini + HTGScor
```

The native values are metres. Multiply by 1000 for millimetres.

For a 30 s full UTC day, expect 2880 records. Fewer records mean incomplete temporal coverage, not necessarily a defective estimator. Do not fill such gaps silently when preparing 15 min tomography windows.

## Acceptance checks

1. Confirm all six station files exist and are nonempty.
2. Confirm the ZTD header contains `STO TROP ZENITH`.
3. Confirm the HTG header contains `STO TROP GRADIENT`.
4. Confirm the generated PRIDE configuration has an active station row using `S  VM3`.
5. Review station execution logs for product download errors, missing observations, ambiguity failures, and early termination.
6. Report coverage warnings separately from solver failures.

# Hong Kong GNSS PRIDE plugin

A portable Codex plugin for downloading Hong Kong SatRef RINEX 3 observations and processing them with PRIDE PPP-AR for atmospheric applications.

## Scientific defaults

- Daily RINEX 3 observations at 30 s
- Static PPP-AR
- VMF3 mapping function
- Stochastic ZTD and horizontal-gradient parameters
- Output completeness, model, and temporal-coverage checks

## Requirements

- Python 3.10 or later
- [`hatanaka`](https://pypi.org/project/hatanaka/) providing `rinex-decompress`
- A working [PRIDE PPP-AR](https://github.com/PrideLab/PRIDE-PPPAR) installation with runtime tables
- Network access to Hong Kong SatRef and precise-product providers when downloading

Place `pdp3` and `rinex-decompress` on `PATH`, or set:

```bash
export PDP3=/path/to/pdp3
export RINEX_DECOMPRESS=/path/to/rinex-decompress
```

`PRIDE_PPPAR_HOME` and `HK_GNSS_PYTHON` are also supported.

## Install from this marketplace

```bash
codex plugin marketplace add huliyu040110/hk-gnss-pride-plugin
codex plugin add hk-gnss-pride@hk-gnss-research
```

Restart Codex or start a new task if the newly installed skill does not appear immediately.

## Use

Invoke the bundled skill:

```text
$process-hk-gnss-pride download and process all Hong Kong stations from 2026-05-10 through 2026-05-12 UTC
```

Data defaults to the active workspace's `data/` directory. The workflow never removes raw RINEX files and replaces existing solution days only after an explicit request.

PRIDE PPP-AR and downloaded GNSS/product data are not included in this repository.

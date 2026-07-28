#!/usr/bin/env python3
"""Download Hong Kong SatRef RINEX and run PRIDE PPP-AR by day.

The fixed atmospheric strategy is:

    static position + VMF3 + STO ZTD + STO horizontal gradients
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parent.parent
DOWNLOADER = SKILL_ROOT / "scripts" / "download_satref.py"
OUTPUT_PREFIXES = ("ztd", "htg", "res", "pos", "amb", "log")


@dataclass(frozen=True)
class JobResult:
    year: int
    doy: int
    station: str
    status: str
    ztd_file: str = ""
    message: str = ""


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def iter_dates(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def day_key(day: date) -> tuple[int, int]:
    return day.year, int(day.strftime("%j"))


def selected_stations(value: str) -> set[str] | None:
    if value.strip().lower() == "all":
        return None
    stations = {item.strip().lower() for item in value.split(",") if item.strip()}
    if not stations:
        raise ValueError("station list is empty")
    return stations


def locate_executable(explicit: Path | None, names: tuple[str, ...]) -> Path:
    if explicit is not None:
        path = explicit.expanduser().absolute()
        if path.is_file() and os.access(path, os.X_OK):
            return path
        raise FileNotFoundError(f"executable not found: {path}")
    for name in names:
        found = shutil.which(name)
        if found:
            return Path(found).absolute()
    raise FileNotFoundError(f"none of these executables were found: {', '.join(names)}")


def environment_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None


def safe_remove_day(path: Path, result_root: Path) -> None:
    target = path.resolve()
    root = result_root.resolve()
    if root not in target.parents or not re.fullmatch(r"\d{3}", target.name):
        raise RuntimeError(f"refusing unsafe removal target: {target}")
    if target.exists():
        shutil.rmtree(target)


def run_download(
    *,
    python: Path,
    start: date,
    end: date,
    stations: str,
    data_root: Path,
    workers: int,
) -> None:
    command = [
        str(python),
        str(DOWNLOADER),
        "--start",
        start.isoformat(),
        "--end",
        end.isoformat(),
        "--stations",
        stations,
        "--rinex",
        "3",
        "--rate",
        "30s",
        "--products",
        "observation",
        "--period",
        "daily",
        "--output",
        str(data_root),
        "--workers",
        str(workers),
    ]
    print("Downloading SatRef RINEX:", " ".join(command))
    subprocess.run(command, check=True)


def discover_jobs(
    *,
    raw_root: Path,
    start: date,
    end: date,
    station_filter: set[str] | None,
) -> dict[tuple[int, int], list[Path]]:
    jobs: dict[tuple[int, int], list[Path]] = {}
    for day in iter_dates(start, end):
        year, doy = day_key(day)
        day_root = raw_root / str(year) / f"{doy:03d}"
        files = sorted(day_root.glob("*/30s/*.crx.gz"))
        if station_filter is not None:
            files = [path for path in files if path.name[:4].lower() in station_filter]
        jobs[(year, doy)] = files
    return jobs


def count_data_records(path: Path) -> int:
    count = 0
    after_header = False
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if "END OF HEADER" in line:
                after_header = True
                continue
            if after_header and line.lstrip().startswith("20"):
                count += 1
    return count


def validate_station_outputs(day_dir: Path, year: int, doy: int, station: str) -> list[str]:
    problems: list[str] = []
    for prefix in OUTPUT_PREFIXES:
        path = day_dir / f"{prefix}_{year}{doy:03d}_{station}"
        if not path.is_file() or path.stat().st_size == 0:
            problems.append(f"missing {path.name}")

    ztd = day_dir / f"ztd_{year}{doy:03d}_{station}"
    htg = day_dir / f"htg_{year}{doy:03d}_{station}"
    for path, label in ((ztd, "TROP ZENITH"), (htg, "TROP GRADIENT")):
        if path.is_file():
            header = path.read_text(encoding="utf-8", errors="replace")[:6000]
            if not re.search(rf"^STO\s+{label}$", header, re.MULTILINE):
                problems.append(f"{path.name} is not STO")
    return problems


def solve_day(
    *,
    year: int,
    doy: int,
    rinex_files: list[Path],
    result_root: Path,
    pdp3: Path,
    decompressor: Path,
    keep_staging: bool,
) -> list[JobResult]:
    day_dir = result_root / str(year) / f"{doy:03d}"
    log_dir = result_root / "logs" / str(year) / f"{doy:03d}"
    stage_dir = result_root / "staging" / str(year) / f"{doy:03d}"
    day_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    stage_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["PATH"] = f"{pdp3.parent}:{env.get('PATH', '')}"
    results: list[JobResult] = []

    for source_crx in rinex_files:
        station = source_crx.name[:4].lower()
        expected_ztd = day_dir / f"ztd_{year}{doy:03d}_{station}"
        station_log = log_dir / f"{station}.log"
        if not validate_station_outputs(day_dir, year, doy, station):
            results.append(
                JobResult(year, doy, station, "skipped-existing", str(expected_ztd))
            )
            continue

        staged_crx = stage_dir / source_crx.name
        staged_rnx = Path(str(staged_crx)[: -len(".crx.gz")] + ".rnx")
        shutil.copy2(source_crx, staged_crx)

        with station_log.open("w", encoding="utf-8") as log:
            decompression = subprocess.run(
                [str(decompressor), str(staged_crx)],
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
            )
            if decompression.returncode not in (0, 2) or not staged_rnx.is_file():
                results.append(
                    JobResult(year, doy, station, "decompress-failed", message=str(station_log))
                )
                continue

            command = [
                pdp3.name,
                "-m",
                "S",
                "-p",
                "V3",
                "-z",
                "S",
                "-h",
                "S",
                str(staged_rnx),
            ]
            solve = subprocess.run(
                command,
                cwd=result_root,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
            )

        problems = validate_station_outputs(day_dir, year, doy, station)
        if solve.returncode == 0 and not problems:
            results.append(JobResult(year, doy, station, "success", str(expected_ztd)))
            staged_crx.unlink(missing_ok=True)
            staged_rnx.unlink(missing_ok=True)
        else:
            detail = "; ".join(problems) or f"pdp3 exit={solve.returncode}"
            results.append(JobResult(year, doy, station, "solve-failed", message=detail))

    for nav in stage_dir.glob("brdm*.??p"):
        shutil.move(str(nav), day_dir / nav.name)

    summary = log_dir / "summary.csv"
    with summary.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=JobResult.__dataclass_fields__.keys())
        writer.writeheader()
        writer.writerows(asdict(item) for item in results)

    failed = [item for item in results if item.status.endswith("failed")]
    if not keep_staging and not failed and stage_dir.exists():
        shutil.rmtree(stage_dir)
    return results


def validate_mapping(day_dir: Path) -> bool:
    configs = sorted(day_dir.glob("config.*"))
    return bool(configs) and any(
        re.search(
            r"^\s*[A-Za-z0-9]{4}\s+S\s+VM3\s+",
            path.read_text(encoding="utf-8", errors="replace"),
            re.MULTILINE,
        )
        for path in configs
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download Hong Kong SatRef RINEX and solve with PRIDE PPP-AR."
    )
    parser.add_argument("--start", required=True, type=parse_date)
    parser.add_argument("--end", required=True, type=parse_date)
    parser.add_argument("--stations", default="all")
    parser.add_argument(
        "--project-root",
        type=Path,
        help="Workspace root; defaults to the current working directory.",
    )
    parser.add_argument("--data-root", type=Path)
    parser.add_argument("--result-root", type=Path)
    parser.add_argument("--python", type=Path)
    parser.add_argument("--pdp3", type=Path)
    parser.add_argument("--rinex-decompress", type=Path)
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--overwrite-results", action="store_true")
    parser.add_argument("--download-workers", type=int, default=4)
    parser.add_argument("--day-workers", type=int, default=3)
    parser.add_argument("--keep-staging", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.end < args.start:
        raise SystemExit("--end cannot be earlier than --start")

    project_root = (args.project_root or Path.cwd()).expanduser().resolve()
    data_root = (args.data_root or project_root / "data").expanduser().resolve()
    result_root = (
        args.result_root or data_root / "pride_results" / "solution"
    ).expanduser().resolve()
    raw_root = data_root / "rinex3"
    python = locate_executable(
        args.python or environment_path("HK_GNSS_PYTHON"),
        (
            str(project_root / ".venv" / "bin" / "python"),
            sys.executable,
            "python3",
        ),
    )
    pride_home = environment_path("PRIDE_PPPAR_HOME")
    pride_candidates = tuple(
        candidate
        for candidate in (
            str(pride_home / "pdp3") if pride_home else "",
            "pdp3",
            str(Path.home() / ".PRIDE_PPPAR_BIN" / "pdp3"),
        )
        if candidate
    )
    pdp3 = locate_executable(
        args.pdp3 or environment_path("PDP3"),
        pride_candidates,
    )
    decompressor = locate_executable(
        args.rinex_decompress or environment_path("RINEX_DECOMPRESS"),
        (
            str(python.parent / "rinex-decompress"),
            str(project_root / ".venv" / "bin" / "rinex-decompress"),
            "rinex-decompress",
        ),
    )

    print(f"Date range: {args.start} through {args.end}")
    print(f"Stations: {args.stations}")
    print(f"Raw root: {raw_root}")
    print(f"Result root: {result_root}")
    print("PRIDE strategy: -m S -p V3 -z S -h S")
    if args.dry_run:
        print("Dry run: no downloads, deletions, or solves were performed.")
        return 0

    if not args.skip_download:
        run_download(
            python=python,
            start=args.start,
            end=args.end,
            stations=args.stations,
            data_root=data_root,
            workers=max(1, args.download_workers),
        )

    station_filter = selected_stations(args.stations)
    jobs = discover_jobs(
        raw_root=raw_root,
        start=args.start,
        end=args.end,
        station_filter=station_filter,
    )
    missing_days = [key for key, files in jobs.items() if not files]
    if missing_days:
        print(f"No RINEX files for: {missing_days}", file=sys.stderr)

    if args.overwrite_results:
        for year, doy in jobs:
            safe_remove_day(result_root / str(year) / f"{doy:03d}", result_root)
            safe_remove_day(result_root / "logs" / str(year) / f"{doy:03d}", result_root)
            safe_remove_day(result_root / "staging" / str(year) / f"{doy:03d}", result_root)

    result_root.mkdir(parents=True, exist_ok=True)
    all_results: list[JobResult] = []
    active_jobs = [(key, files) for key, files in jobs.items() if files]
    with ThreadPoolExecutor(max_workers=max(1, args.day_workers)) as executor:
        futures = {
            executor.submit(
                solve_day,
                year=year,
                doy=doy,
                rinex_files=files,
                result_root=result_root,
                pdp3=pdp3,
                decompressor=decompressor,
                keep_staging=args.keep_staging,
            ): (year, doy)
            for (year, doy), files in active_jobs
        }
        for future in as_completed(futures):
            key = futures[future]
            day_results = future.result()
            all_results.extend(day_results)
            success = sum(item.status in {"success", "skipped-existing"} for item in day_results)
            print(f"Completed {key}: {success}/{len(day_results)} successful")

    mapping_failures = []
    coverage_warnings = []
    for (year, doy), files in active_jobs:
        day_dir = result_root / str(year) / f"{doy:03d}"
        if not validate_mapping(day_dir):
            mapping_failures.append((year, doy))
        for raw in files:
            station = raw.name[:4].lower()
            htg = day_dir / f"htg_{year}{doy:03d}_{station}"
            if htg.is_file():
                records = count_data_records(htg)
                if records != 2880:
                    coverage_warnings.append(
                        {"year": year, "doy": doy, "station": station, "htg_records": records}
                    )

    report = {
        "start": args.start.isoformat(),
        "end": args.end.isoformat(),
        "stations": args.stations,
        "strategy": "-m S -p V3 -z S -h S",
        "results": [asdict(item) for item in sorted(all_results, key=lambda x: (x.year, x.doy, x.station))],
        "mapping_failures": mapping_failures,
        "coverage_warnings": coverage_warnings,
    }
    report_path = result_root / "pipeline_summary.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    failures = [item for item in all_results if item.status.endswith("failed")]
    print(f"Summary: jobs={len(all_results)}, failures={len(failures)}")
    print(f"Report: {report_path}")
    if coverage_warnings:
        print(f"Coverage warnings: {coverage_warnings}")
    if mapping_failures:
        print(f"VMF3 validation failures: {mapping_failures}", file=sys.stderr)
    return 1 if failures or missing_days or mapping_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

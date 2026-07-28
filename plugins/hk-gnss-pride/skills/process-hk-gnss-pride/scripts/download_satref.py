#!/usr/bin/env python3
"""下载香港地政总署 SatRef 的公开 GNSS/RINEX 数据。

数据目录（UTC）::

    https://rinex.geodetic.gov.hk/rinex3/YYYY/DDD/site/

本脚本只依赖 Python 标准库。它先读取官方目录，再下载目录中实际存在的
文件，因此不会假设每个站点、每天都拥有完整数据。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlsplit
from urllib.request import Request, urlopen


BASE_URL = "https://rinex.geodetic.gov.hk/"
USER_AGENT = "hk-satref-downloader/1.0 (+https://www.geodetic.gov.hk/)"

# 香港地政总署数据字典列出的 18 个 SatRef CORS，加上海事处的 KYC1。
STATIONS = (
    "HKCL",
    "HKFN",
    "HKKS",
    "HKKT",
    "HKLM",
    "HKLT",
    "HKMW",
    "HKNP",
    "HKOH",
    "HKPC",
    "HKQT",
    "HKSC",
    "HKSL",
    "HKSS",
    "HKST",
    "HKTK",
    "HKWS",
    "KYC1",
    "T430",
)

PRODUCTS = ("observation", "navigation", "meteorological", "tilt")
RINEX3_NAV_TYPES = {"CN", "EN", "GN", "JN", "RN"}


class LinkParser(HTMLParser):
    """提取 IIS 目录页中的链接。"""

    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self.links.append(href)


@dataclass(frozen=True)
class RemoteFile:
    url: str
    relative_path: Path


@dataclass(frozen=True)
class DownloadResult:
    remote: RemoteFile
    status: str
    size: int


def parse_iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"日期 {value!r} 无效，应使用 YYYY-MM-DD"
        ) from exc


def date_range(start: date, end: date) -> Iterable[date]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def parse_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_hours(value: str) -> set[int]:
    """解析 ``0,3,8-12`` 形式的 UTC 小时列表。"""
    hours: set[int] = set()
    for token in parse_csv(value):
        if "-" in token:
            try:
                first_text, last_text = token.split("-", 1)
                first, last = int(first_text), int(last_text)
            except ValueError as exc:
                raise argparse.ArgumentTypeError(f"小时范围 {token!r} 无效") from exc
            if first > last:
                raise argparse.ArgumentTypeError(f"小时范围 {token!r} 起止颠倒")
            hours.update(range(first, last + 1))
        else:
            try:
                hours.add(int(token))
            except ValueError as exc:
                raise argparse.ArgumentTypeError(f"小时 {token!r} 无效") from exc
    if not hours or min(hours) < 0 or max(hours) > 23:
        raise argparse.ArgumentTypeError("小时必须在 0 到 23 之间")
    return hours


def resolved_period(version: int, rate: str, period: str) -> str:
    if period != "auto":
        return period
    if version == 3:
        return "daily" if rate == "30s" else "hourly"
    # RINEX 2 的 5 秒目录同时含日文件和小时文件，默认选择体积更小的日文件集合。
    return "hourly" if rate == "1s" else "daily"


def is_selected_file(
    filename: str,
    *,
    version: int,
    rate: str,
    products: set[str],
    period: str,
    hours: set[int] | None,
) -> bool:
    """判断官方目录中的一个文件是否符合筛选条件。"""
    if version == 3:
        obs_match = re.search(
            r"_R_\d{7}(\d{2})\d{2}_(01[DH])_(01S|05S|30S)_MO\.crx\.gz$",
            filename,
            re.IGNORECASE,
        )
        if "observation" in products and obs_match:
            hour = int(obs_match.group(1))
            file_period = "daily" if obs_match.group(2).upper() == "01D" else "hourly"
            file_rate = {
                "01S": "1s",
                "05S": "5s",
                "30S": "30s",
            }[obs_match.group(3).upper()]
            return (
                file_rate == rate
                and period in {file_period, "all"}
                and (file_period == "daily" or hours is None or hour in hours)
            )

        daily_match = re.search(r"_01D_([A-Z]{2})\.rnx\.gz$", filename, re.I)
        if not daily_match:
            return False
        data_type = daily_match.group(1).upper()
        return (
            ("navigation" in products and data_type in RINEX3_NAV_TYPES)
            or ("meteorological" in products and data_type == "MM")
        )

    obs_match = re.search(r"\d{3}([0a-x])\.\d{2}d\.gz$", filename, re.I)
    if "observation" in products and obs_match:
        marker = obs_match.group(1).lower()
        file_period = "daily" if marker == "0" else "hourly"
        hour = None if marker == "0" else ord(marker) - ord("a")
        return period in {file_period, "all"} and (
            hour is None or hours is None or hour in hours
        )

    type_match = re.search(r"\.\d{2}([agnm])\.gz$", filename, re.I)
    if not type_match:
        return False
    data_type = type_match.group(1).lower()
    return (
        ("navigation" in products and data_type in {"n", "g"})
        or ("meteorological" in products and data_type == "m")
        or ("tilt" in products and data_type == "a")
    )


def request_bytes(url: str, *, timeout: float, retries: int) -> bytes:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            request = Request(url, headers={"User-Agent": USER_AGENT})
            with urlopen(request, timeout=timeout) as response:
                return response.read()
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if isinstance(exc, HTTPError) and 400 <= exc.code < 500 and exc.code != 429:
                break
            if attempt < retries:
                time.sleep(min(2**attempt, 4))
    assert last_error is not None
    raise last_error


def list_directory(url: str, *, timeout: float, retries: int) -> list[str]:
    html = request_bytes(url, timeout=timeout, retries=retries).decode(
        "utf-8", errors="replace"
    )
    parser = LinkParser()
    parser.feed(html)
    files: list[str] = []
    directory_path = urlsplit(url).path.rstrip("/") + "/"
    for href in parser.links:
        absolute = urljoin(url, href)
        path = urlsplit(absolute).path
        # 排除父目录、子目录和跳到其他位置的链接。
        if path.startswith(directory_path) and not path.endswith("/"):
            files.append(absolute)
    return files


def discovery_urls(
    *, base_url: str, version: int, day: date, station: str, products: set[str], rate: str
) -> list[str]:
    doy = day.timetuple().tm_yday
    root = urljoin(
        base_url.rstrip("/") + "/",
        f"rinex{version}/{day.year}/{doy:03d}/{station.lower()}/",
    )
    urls: list[str] = []
    if products - {"observation"}:
        urls.append(root)
    if "observation" in products:
        urls.append(urljoin(root, f"{rate}/"))
    return list(dict.fromkeys(urls))


def discover_files(args: argparse.Namespace) -> tuple[list[RemoteFile], list[str]]:
    selected: dict[str, RemoteFile] = {}
    warnings: list[str] = []
    products = set(args.products)
    period = resolved_period(args.rinex, args.rate, args.period)
    base_path = urlsplit(args.base_url).path.rstrip("/") + "/"

    for day in date_range(args.start_date, args.end_date):
        for station in args.stations:
            for directory_url in discovery_urls(
                base_url=args.base_url,
                version=args.rinex,
                day=day,
                station=station,
                products=products,
                rate=args.rate,
            ):
                try:
                    urls = list_directory(
                        directory_url, timeout=args.timeout, retries=args.retries
                    )
                except HTTPError as exc:
                    if exc.code == 404:
                        warnings.append(f"目录不存在（可能缺测）：{directory_url}")
                        continue
                    raise
                for url in urls:
                    filename = unquote(PurePosixPath(urlsplit(url).path).name)
                    if not is_selected_file(
                        filename,
                        version=args.rinex,
                        rate=args.rate,
                        products=products,
                        period=period,
                        hours=args.hours,
                    ):
                        continue
                    remote_path = unquote(urlsplit(url).path)
                    if base_path != "/" and remote_path.startswith(base_path):
                        remote_path = remote_path[len(base_path) :]
                    else:
                        remote_path = remote_path.lstrip("/")
                    selected[url] = RemoteFile(url, Path(remote_path))
    return sorted(selected.values(), key=lambda item: item.url), warnings


def download_file(
    remote: RemoteFile,
    *,
    output_dir: Path,
    timeout: float,
    retries: int,
    overwrite: bool,
) -> DownloadResult:
    target = output_dir / remote.relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and target.stat().st_size > 0 and not overwrite:
        return DownloadResult(remote, "skipped", target.stat().st_size)

    partial = target.with_name(target.name + ".part")
    if overwrite and partial.exists():
        partial.unlink()

    last_error: Exception | None = None
    for attempt in range(retries + 1):
        existing = partial.stat().st_size if partial.exists() else 0
        headers = {"User-Agent": USER_AGENT}
        if existing:
            headers["Range"] = f"bytes={existing}-"
        request = Request(remote.url, headers=headers)
        try:
            with urlopen(request, timeout=timeout) as response:
                status = getattr(response, "status", response.getcode())
                append = existing > 0 and status == 206
                mode = "ab" if append else "wb"
                with partial.open(mode) as file_handle:
                    shutil.copyfileobj(response, file_handle, length=1024 * 1024)
            os.replace(partial, target)
            return DownloadResult(remote, "downloaded", target.stat().st_size)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if isinstance(exc, HTTPError) and 400 <= exc.code < 500 and exc.code != 429:
                break
            if attempt < retries:
                time.sleep(min(2**attempt, 4))
    assert last_error is not None
    raise last_error


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


def station_argument(value: str) -> list[str]:
    raw = [item.upper() for item in parse_csv(value)]
    if not raw:
        raise argparse.ArgumentTypeError("至少提供一个站点")
    if raw == ["ALL"]:
        return list(STATIONS)
    unknown = sorted(set(raw) - set(STATIONS))
    if unknown:
        raise argparse.ArgumentTypeError(
            f"未知站点：{', '.join(unknown)}；可用 --list-stations 查看站点"
        )
    return list(dict.fromkeys(raw))


def product_argument(value: str) -> list[str]:
    raw = [item.lower() for item in parse_csv(value)]
    if raw == ["all"]:
        return list(PRODUCTS)
    unknown = sorted(set(raw) - set(PRODUCTS))
    if not raw or unknown:
        raise argparse.ArgumentTypeError(
            "产品类型应为 observation、navigation、meteorological、tilt 或 all"
        )
    return list(dict.fromkeys(raw))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="下载香港 SatRef 参考站的 GNSS 原始数据（RINEX）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--start", dest="start_date", type=parse_iso_date, help="开始日期（UTC）")
    parser.add_argument("--end", dest="end_date", type=parse_iso_date, help="结束日期（UTC，含当天）")
    parser.add_argument(
        "--stations",
        type=station_argument,
        help="站点代码，逗号分隔；使用 all 选择全部站点",
    )
    parser.add_argument("--rinex", type=int, choices=(2, 3), default=3, help="RINEX 版本")
    parser.add_argument(
        "--rate", choices=("1s", "5s", "30s"), default="30s", help="观测数据采样间隔"
    )
    parser.add_argument(
        "--products",
        type=product_argument,
        default=["observation"],
        help="产品类型，逗号分隔，或 all",
    )
    parser.add_argument(
        "--period",
        choices=("auto", "daily", "hourly", "all"),
        default="auto",
        help="观测文件时段；auto 根据版本和采样率选择",
    )
    parser.add_argument(
        "--hours",
        type=parse_hours,
        help="只取指定 UTC 小时，例如 0,6,12-18（仅小时观测文件）",
    )
    parser.add_argument("--output", type=Path, default=Path("data"), help="下载目录")
    parser.add_argument("--workers", type=int, default=4, help="并行下载数")
    parser.add_argument("--timeout", type=float, default=30.0, help="单次网络请求超时秒数")
    parser.add_argument("--retries", type=int, default=3, help="失败重试次数")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已经下载的文件")
    parser.add_argument("--list-only", action="store_true", help="只列出匹配文件，不下载")
    parser.add_argument("--list-stations", action="store_true", help="列出支持的站点并退出")
    parser.add_argument(
        "--base-url", default=BASE_URL, help=argparse.SUPPRESS
    )
    return parser


def validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.list_stations:
        return
    if args.start_date is None or args.stations is None:
        parser.error("--start 和 --stations 是必填参数")
    if args.end_date is None:
        args.end_date = args.start_date
    if args.end_date < args.start_date:
        parser.error("--end 不能早于 --start")
    if args.workers < 1 or args.workers > 16:
        parser.error("--workers 必须在 1 到 16 之间")
    if args.timeout <= 0 or args.retries < 0:
        parser.error("--timeout 必须大于 0，--retries 不能小于 0")
    period = resolved_period(args.rinex, args.rate, args.period)
    if "observation" in args.products:
        unsupported = (
            (args.rinex == 3 and args.rate in {"1s", "5s"} and period == "daily")
            or (args.rate in {"1s", "5s"} and period == "all" and args.rinex == 3)
            or (args.rate == "30s" and period in {"hourly", "all"})
            or (args.rinex == 2 and args.rate == "1s" and period in {"daily", "all"})
        )
        if unsupported:
            parser.error("所选 RINEX 版本、采样率和文件时段组合不受官方数据支持")
    if args.hours is not None and period == "daily":
        parser.error("--hours 不能用于日观测文件；请选择小时文件")
    if "tilt" in args.products and args.rinex == 3:
        parser.error("倾斜仪数据仅提供 RINEX 2 格式")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    validate_args(parser, args)

    if args.list_stations:
        print(" ".join(STATIONS))
        return 0

    print(
        f"正在查询官方目录：{args.start_date} 至 {args.end_date}（UTC），"
        f"站点 {', '.join(args.stations)}"
    )
    try:
        files, warnings = discover_files(args)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        print(f"查询目录失败：{exc}", file=sys.stderr)
        return 2

    for warning in warnings:
        print(f"警告：{warning}", file=sys.stderr)
    if not files:
        print("没有找到符合条件的文件。请检查日期、站点和筛选条件。")
        return 1

    print(f"找到 {len(files)} 个文件：")
    if args.list_only:
        for remote in files:
            print(remote.url)
        return 0

    downloaded = skipped = failed = total_bytes = 0
    with ThreadPoolExecutor(max_workers=min(args.workers, len(files))) as executor:
        futures = {
            executor.submit(
                download_file,
                remote,
                output_dir=args.output,
                timeout=args.timeout,
                retries=args.retries,
                overwrite=args.overwrite,
            ): remote
            for remote in files
        }
        for future in as_completed(futures):
            remote = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # 每个文件独立失败，继续下载其余文件。
                failed += 1
                print(f"[失败] {remote.url}：{exc}", file=sys.stderr)
                continue
            total_bytes += result.size
            if result.status == "downloaded":
                downloaded += 1
                label = "完成"
            else:
                skipped += 1
                label = "跳过"
            print(f"[{label}] {remote.relative_path} ({human_size(result.size)})")

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(
        f"结束于 {now_utc}：下载 {downloaded}，跳过 {skipped}，失败 {failed}，"
        f"文件合计 {human_size(total_bytes)}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

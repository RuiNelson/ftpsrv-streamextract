#!/usr/bin/env python3
"""Compare sequential small-file passive FTP transfers using MODE S and B."""

import argparse
import ftplib
import hashlib
import json
from pathlib import Path
import re
import socket
import statistics
import subprocess
import tempfile
import time


def wait_for_server(server, port, log):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if server.poll() is not None:
            log.seek(0)
            raise RuntimeError(log.read().decode(errors="replace"))
        ftp = ftplib.FTP(timeout=1)
        try:
            ftp.connect("127.0.0.1", port)
            ftp.login()
            return
        except OSError:
            time.sleep(0.05)
        finally:
            ftp.close()
    raise TimeoutError("Local FTP server did not start")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def transfer(args, root, source, expected, port, mode, direction, repeat):
    label = f"passive-{mode}-{direction}-{repeat}"
    target = root / label
    target.mkdir()
    # Downloads read files prepared locally, so their timing is independent
    # of the preceding upload run. Source creation and verification are untimed.
    input_dir = source
    if direction == "download":
        input_dir = root / (label + "-source")
        input_dir.mkdir()
        for name in expected:
            (input_dir / name).write_bytes((source / name).read_bytes())

    config = root / (label + ".curl")
    lines = []
    for index, name in enumerate(expected):
        if index:
            lines.append("next")
        lines += ["silent", "show-error", "fail", "verbose", "max-time = 30",
                  'noproxy = "*"',
                  'write-out = "%{response_code} %{size_upload} %{size_download}\\n"']
        if mode == "B":
            lines.append("ftp-block-mode")
        remote = target / name if direction == "upload" else input_dir / name
        lines.append("url = " + json.dumps(f"ftp://127.0.0.1:{port}{remote}"))
        option = "upload-file" if direction == "upload" else "output"
        local = source / name if direction == "upload" else target / name
        lines.append(option + " = " + json.dumps(str(local)))
    config.write_text("\n".join(lines) + "\n")

    trace = root / (label + ".trace")
    stats = root / (label + ".stats")
    with trace.open("w") as stderr, stats.open("w") as stdout:
        started = time.perf_counter()
        process = subprocess.run([args.curl, "-q", "--fail-early", "--config", str(config)],
                                 stdout=stdout, stderr=stderr, timeout=args.timeout)
        elapsed = time.perf_counter() - started
    log = trace.read_text()
    # Keep evidence available even if a count or integrity check fails.
    output_dir = Path(args.output).resolve().parent
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / ("small-files-" + label + ".trace")).write_text(log)
    if process.returncode:
        raise RuntimeError(f"{label}: curl exited {process.returncode}\n{log[-8000:]}")

    control_connections = len(re.findall(r"^> USER ", log, re.MULTILINE))
    if re.search(r"^> (?:EPRT|PORT)\b", log, re.MULTILINE):
        raise AssertionError(f"{label}: active FTP was used")
    data_connections = len(re.findall(r"^> (?:EPSV|PASV)\b", log, re.MULTILINE))
    reused = len(re.findall(r"^< 125 ", log, re.MULTILINE))
    negotiated = len(re.findall(r"^> MODE B\r?$", log, re.MULTILINE))
    transfers = len(re.findall(r"^> (?:STOR|RETR) ", log, re.MULTILINE))
    expected_connections = 1 if mode == "B" else args.files
    if (control_connections, data_connections, reused, negotiated, transfers) != (
            1, expected_connections, args.files - 1 if mode == "B" else 0,
            1 if mode == "B" else 0, args.files):
        raise AssertionError(f"{label}: unexpected connection/transfer counts: "
                             f"{control_connections}, {data_connections}, {reused}, "
                             f"{negotiated}, {transfers}")

    rows = [line.split() for line in stats.read_text().splitlines()]
    expected_row = ["250" if mode == "B" else "226",
                    str(args.size if direction == "upload" else 0),
                    str(args.size if direction == "download" else 0)]
    if len(rows) != args.files or any(row != expected_row for row in rows):
        raise AssertionError(f"{label}: invalid reply codes or payload byte counts")
    actual = {path.name: digest(path) for path in target.iterdir()}
    if actual != expected:
        raise AssertionError(f"{label}: file names or SHA-256 hashes differ")

    result = dict(connection="passive", mode=mode,
                  direction=direction, repeat=repeat, seconds=elapsed,
                  files_per_second=args.files / elapsed,
                  control_connections=control_connections,
                  data_connections=data_connections, reused_transfers=reused,
                  verified_files=len(actual), verified_bytes=args.files * args.size)
    print(f"{label}: {elapsed:.3f}s, {result['files_per_second']:.0f} files/s, "
          f"{data_connections} data connection(s), SHA-256 OK", flush=True)
    return result


def report(args, runs):
    lines = ["# Small-file FTP benchmark", "",
             f"{args.files} files × {args.size} bytes; {args.repeats} repetitions "
             "per case. Sequential passive FTP transfers on localhost using the same curl "
             "executable for MODE S and B. Times include one curl invocation for "
             "the complete batch, configuration parsing, and verbose logging. "
             "File preparation and SHA-256 verification are outside the timer.", "",
             "| Connection | Transfer | MODE S median | MODE B median | Speedup | "
             "Data connections S / B |", "|---|---|---:|---:|---:|---:|"]
    summaries = []
    for direction in ("upload", "download"):
        cases = {mode: [run for run in runs if run["direction"] == direction
                        and run["mode"] == mode] for mode in ("S", "B")}
        medians = {mode: statistics.median(run["seconds"] for run in cases[mode])
                   for mode in ("S", "B")}
        ratio = medians["S"] / medians["B"]
        summaries.append(dict(connection="passive", direction=direction,
                              stream_seconds=medians["S"], block_seconds=medians["B"],
                              speedup=ratio))
        lines.append(f"| passive | {direction} | {medians['S']:.3f} s | "
                     f"{medians['B']:.3f} s | {ratio:.2f}× | {args.files} / 1 |")
    lines += ["", "All files matched their source SHA-256 hashes. Each batch used "
              "one control connection. MODE B negotiated once and reused its data "
              f"connection for {args.files - 1} transfers. MODE S opened a new data "
              "connection for every file.", "", "These are localhost results; "
              "latency, storage, and TCP behavior on a console or network may "
              "change the timings.", ""]
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(dict(files=args.files, bytes_per_file=args.size,
                                     repetitions=args.repeats, runs=runs,
                                     summaries=summaries), indent=2) + "\n")
    output.with_suffix(".md").write_text("\n".join(lines))
    print("\n" + "\n".join(lines[4:]), flush=True)
    print(f"\nResults: {output}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True, help="Native ftpsrv executable")
    parser.add_argument("--curl", required=True, help="curl with --ftp-block-mode")
    parser.add_argument("--files", type=int, default=1000)
    parser.add_argument("--size", type=int, default=4096, help="Bytes per file")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=300, help="Seconds per batch")
    parser.add_argument("--output", default=".build/small-files-results.json")
    args = parser.parse_args()
    if args.files < 1 or args.size < 0 or args.repeats < 1 or args.timeout < 1:
        parser.error("files, repeats, and timeout must be positive; size must be nonnegative")
    args.server = str(Path(args.server).resolve())
    args.curl = str(Path(args.curl).resolve())
    with tempfile.TemporaryDirectory(prefix="ftpsrv-small-files-") as temporary:
        root = Path(temporary)
        source = root / "source"
        source.mkdir()
        expected = {}
        for index in range(args.files):
            seed = hashlib.sha256(str(index).encode()).digest()
            payload = (seed * ((args.size + len(seed) - 1) // len(seed)))[:args.size]
            name = f"file-{index:06d}.bin"
            (source / name).write_bytes(payload)
            expected[name] = hashlib.sha256(payload).hexdigest()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with tempfile.TemporaryFile() as log:
            server = subprocess.Popen([args.server, "-p", str(port)], stdout=log, stderr=log)
            try:
                wait_for_server(server, port, log)
                runs = []
                for repeat in range(1, args.repeats + 1):
                    # Alternate the order to reduce cache/order bias.
                    modes = ("S", "B") if repeat % 2 else ("B", "S")
                    for direction in ("upload", "download"):
                        for mode in modes:
                            runs.append(transfer(args, root, source, expected, port,
                                                 mode, direction, repeat))
                report(args, runs)
            finally:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)


if __name__ == "__main__":
    main()

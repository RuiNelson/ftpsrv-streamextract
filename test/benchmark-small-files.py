#!/usr/bin/env python3
"""Compare sequential small-file passive FTP transfers using MODE S and B."""

import argparse
import ftplib
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import socket
import statistics
import subprocess
import tempfile
import time
from urllib.parse import quote
import uuid


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


def transfer(args, root, source, expected, port, mode, direction, repeat, remote_root=None):
    label = f"passive-{mode}-{direction}-{repeat}"
    target = root / label
    target.mkdir()
    # Downloads read files prepared locally, so their timing is independent
    # of the preceding upload run. Source creation and verification are untimed.
    input_dir = source
    if direction == "download" and remote_root is None:
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
        if remote_root is not None:
            remote = remote_root / f"passive-{mode}-upload-{repeat}" / name
        else:
            remote = target / name if direction == "upload" else input_dir / name
        host = args.host or "127.0.0.1"
        # The extra slash makes the FTP path absolute regardless of login PWD.
        url = f"ftp://{host}:{port}/{quote(str(remote), safe='/')}"
        lines.append("url = " + json.dumps(url))
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
    verified_files = 0
    if remote_root is None or direction == "download":
        actual = {path.name: digest(path) for path in target.iterdir()}
        if actual != expected:
            raise AssertionError(f"{label}: file names or SHA-256 hashes differ")
        verified_files = len(actual)

    result = dict(connection="passive", mode=mode,
                  direction=direction, repeat=repeat, seconds=elapsed,
                  files_per_second=args.files / elapsed,
                  control_connections=control_connections,
                  data_connections=data_connections, reused_transfers=reused,
                  verified_files=verified_files, verified_bytes=verified_files * args.size)
    verification = "SHA-256 OK" if verified_files else "readback verification pending"
    print(f"{label}: {elapsed:.3f}s, {result['files_per_second']:.0f} files/s, "
          f"{data_connections} data connection(s), {verification}", flush=True)
    return result


def report(args, runs):
    lines = ["# Small-file FTP benchmark", "",
             f"{args.files} files × {args.size} bytes; {args.repeats} repetitions "
             f"per case. Sequential passive FTP transfers to {args.host or 'localhost'} "
             f"port {args.port} using the same curl "
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
              "connection for every file.", ""]
    if args.host:
        lines += [f"Remote test directory: `{args.remote_dir}`. Downloaded data was "
                  "read back from the corresponding measured uploads. The isolated "
                  "test subdirectory and its files were removed after verification.", ""]
    else:
        lines += ["These are localhost results; latency, storage, and TCP behavior "
                  "on a console or network may change the timings.", ""]
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(dict(host=args.host or "127.0.0.1", port=args.port,
                                     remote_directory=args.remote_dir,
                                     files=args.files, bytes_per_file=args.size,
                                     repetitions=args.repeats, runs=runs,
                                     summaries=summaries), indent=2) + "\n")
    output.with_suffix(".md").write_text("\n".join(lines))
    output.with_suffix(".partial.json").unlink(missing_ok=True)
    print("\n" + "\n".join(lines[4:]), flush=True)
    print(f"\nResults: {output}", flush=True)


def run_batches(args, root, source, expected, port, remote_root=None):
    runs = []
    for repeat in range(1, args.repeats + 1):
        # Alternate the order to reduce cache/order bias.
        modes = ("S", "B") if repeat % 2 else ("B", "S")
        for direction in ("upload", "download"):
            for mode in modes:
                run = transfer(args, root, source, expected, port, mode,
                               direction, repeat, remote_root)
                runs.append(run)
                if remote_root is not None and direction == "download":
                    uploaded = next(item for item in runs if item["mode"] == mode
                                    and item["repeat"] == repeat
                                    and item["direction"] == "upload")
                    uploaded["verified_files"] = run["verified_files"]
                    uploaded["verified_bytes"] = run["verified_bytes"]
                    print(f"passive-{mode}-upload-{repeat}: readback SHA-256 OK", flush=True)
        # Preserve completed rounds if a subsequent batch fails.
        output = Path(args.output).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.with_suffix(".partial.json").write_text(json.dumps(runs, indent=2) + "\n")
    return runs


def connect_remote(args):
    ftp = ftplib.FTP(timeout=15)
    try:
        ftp.connect(args.host, args.port)
        ftp.login()
        ftp.set_pasv(True)
        return ftp
    except BaseException:
        ftp.close()
        raise


def remote_benchmark(args, root, source, expected):
    remote_root = None
    directories = []
    with connect_remote(args) as ftp:
        print(ftp.getwelcome(), flush=True)
        print(ftp.voidcmd("MODE B"), flush=True)
        ftp.voidcmd("MODE S")
        ftp.cwd(args.remote_dir)
        session = "benchmark-" + uuid.uuid4().hex
        # Never reuse an existing directory or overwrite pre-existing data.
        ftp.mkd(session)
        remote_root = PurePosixPath(ftp.pwd()) / session
        print(f"Remote workspace: {remote_root}", flush=True)
    try:
        with connect_remote(args) as ftp:
            for repeat in range(1, args.repeats + 1):
                for mode in ("S", "B"):
                    directory = remote_root / f"passive-{mode}-upload-{repeat}"
                    ftp.mkd(str(directory))
                    directories.append(directory)
        return run_batches(args, root, source, expected, args.port, remote_root)
    finally:
        print(f"Cleaning benchmark workspace: {remote_root}", flush=True)
        with connect_remote(args) as ftp:
            for directory in directories:
                for name in expected:
                    try:
                        ftp.delete(str(directory / name))
                    except ftplib.error_perm as error:
                        if not str(error).startswith("550"):
                            raise
                ftp.rmd(str(directory))
            ftp.rmd(str(remote_root))
        print(f"Removed benchmark workspace: {remote_root}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    endpoint = parser.add_mutually_exclusive_group(required=True)
    endpoint.add_argument("--server", help="Launch a native ftpsrv executable")
    endpoint.add_argument("--host", help="Benchmark an existing anonymous FTP server")
    parser.add_argument("--port", type=int, default=2121)
    parser.add_argument("--remote-dir", help="Existing absolute FTP test directory")
    parser.add_argument("--curl", required=True, help="curl with --ftp-block-mode")
    parser.add_argument("--files", type=int, default=1000)
    parser.add_argument("--size", type=int, default=4096, help="Bytes per file")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=300, help="Seconds per batch")
    parser.add_argument("--output", default=".build/small-files-results.json")
    args = parser.parse_args()
    if args.files < 1 or args.size < 0 or args.repeats < 1 or args.timeout < 1:
        parser.error("files, repeats, and timeout must be positive; size must be nonnegative")
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    if args.host and (not args.remote_dir or not args.remote_dir.startswith("/")):
        parser.error("--host requires an absolute --remote-dir")
    if args.server:
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
        if args.host:
            runs = remote_benchmark(args, root, source, expected)
            report(args, runs)
            return
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        args.port = port
        with tempfile.TemporaryFile() as log:
            server = subprocess.Popen([args.server, "-p", str(port)], stdout=log, stderr=log)
            try:
                wait_for_server(server, port, log)
                runs = run_batches(args, root, source, expected, port)
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

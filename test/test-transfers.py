#!/usr/bin/env python3
"""Local file-transfer regression checks; uses only the Python standard library."""

import ftplib
import io
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest


class Transfers(unittest.TestCase):
    binary = None

    @classmethod
    def setUpClass(cls):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            cls.port = probe.getsockname()[1]
        cls.log = tempfile.TemporaryFile()
        cls.addClassCleanup(cls.log.close)
        cls.server = subprocess.Popen(
            [cls.binary, "-p", str(cls.port)], stdout=cls.log, stderr=cls.log
        )
        cls.addClassCleanup(cls.stop_server)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if cls.server.poll() is not None:
                cls.log.seek(0)
                raise RuntimeError(cls.log.read().decode(errors="replace"))
            try:
                with cls.connect():
                    return
            except OSError:
                time.sleep(0.05)
        raise TimeoutError("Local FTP server did not start")

    @classmethod
    def stop_server(cls):
        if cls.server.poll() is None:
            cls.server.terminate()
            try:
                cls.server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cls.server.kill()
                cls.server.wait(timeout=5)

    @classmethod
    def connect(cls):
        ftp = ftplib.FTP(timeout=5)
        try:
            ftp.connect("127.0.0.1", cls.port)
            ftp.login()
            return ftp
        except BaseException:
            ftp.close()
            raise

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="ftpsrv-check-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.ftp = self.connect()
        self.addCleanup(self.ftp.close)
        self.ftp.cwd(str(self.root))

    def download(self, name, **kwargs):
        out = io.BytesIO()
        self.ftp.retrbinary("RETR " + name, out.write, **kwargs)
        return out.getvalue()

    def test_binary_roundtrip_and_buffer_reuse(self):
        # Listing allocates the small buffer, file transfer grows it, and
        # subsequent commands must safely reuse the resulting storage.
        self.assertEqual(set(self.ftp.nlst()) - {".", ".."}, set())
        payload = bytes(range(256)) * 9000
        for passive in (True, False):
            with self.subTest(passive=passive):
                self.ftp.set_pasv(passive)
                self.ftp.storbinary("STOR data.bin", io.BytesIO(payload))
                self.assertEqual(self.download("data.bin"), payload)
                self.assertIn("data.bin", self.ftp.nlst())
                rows = []
                self.ftp.retrlines("LIST", rows.append)
                self.assertTrue(any("data.bin" in row for row in rows))
                self.assertIn("data.bin", dict(self.ftp.mlsd()))

    def test_store_replaces_and_truncates(self):
        target = self.root / "data.bin"
        target.write_bytes(b"old contents" * 100)
        self.ftp.storbinary("STOR data.bin", io.BytesIO(b"new"))
        self.assertEqual(target.read_bytes(), b"new")
        self.ftp.storbinary("STOR data.bin", io.BytesIO())
        self.assertEqual(target.read_bytes(), b"")

    def test_resume_upload_and_download(self):
        target = self.root / "data.bin"
        target.write_bytes(b"0123456789")
        self.ftp.storbinary("STOR data.bin", io.BytesIO(b"abc"), rest=5)
        self.assertEqual(target.read_bytes(), b"01234abc")
        self.assertEqual(self.download("data.bin", rest=5), b"abc")

    def test_append_creates_and_preserves_contents(self):
        target = self.root / "append.bin"
        self.ftp.storbinary("APPE append.bin", io.BytesIO(b"first"))
        self.ftp.storbinary("APPE append.bin", io.BytesIO())
        self.ftp.storbinary("APPE append.bin", io.BytesIO(b"second"))
        self.assertEqual(target.read_bytes(), b"firstsecond")

    def test_append_ignores_restart_marker(self):
        target = self.root / "append.bin"
        target.write_bytes(b"original")
        self.ftp.storbinary("APPE append.bin", io.BytesIO(b"tail"), rest=2)
        self.assertEqual(target.read_bytes(), b"originaltail")
        self.assertEqual(self.download("append.bin"), b"originaltail")

    def test_overlapping_append_sessions(self):
        for initial in (b"", b"prefix"):
            with self.subTest(initial=initial):
                target = self.root / "shared.bin"
                target.write_bytes(initial)
                with self.connect() as other:
                    other.cwd(str(self.root))
                    self.ftp.voidcmd("TYPE I")
                    other.voidcmd("TYPE I")
                    # Both commands open the file before either sends data.
                    with self.ftp.transfercmd("APPE shared.bin") as first:
                        with other.transfercmd("APPE shared.bin") as second:
                            first.sendall(b"A" * 4096)
                            first.shutdown(socket.SHUT_WR)
                            self.ftp.voidresp()
                            second.sendall(b"B" * 4096)
                            second.shutdown(socket.SHUT_WR)
                            other.voidresp()
                self.assertEqual(target.read_bytes(), initial + b"A" * 4096 + b"B" * 4096)

    def test_failed_data_connection_preserves_file(self):
        target = self.root / "keep.bin"
        target.write_bytes(b"keep these contents")
        # Choose a closed local port. A bound but unlistening socket can
        # silently drop SYNs on macOS instead of immediately refusing them.
        with socket.socket() as unused:
            unused.bind(("127.0.0.1", 0))
            port = unused.getsockname()[1]
        self.ftp.sendport("127.0.0.1", port)
        self.ftp.putcmd("STOR keep.bin")
        self.assertTrue(self.ftp.getresp().startswith("150"))
        with self.assertRaises(ftplib.error_perm):
            self.ftp.getresp()
        self.assertEqual(target.read_bytes(), b"keep these contents")
        self.assertTrue(self.ftp.voidcmd("NOOP").startswith("200"))

    def test_symlink_upload_is_rejected(self):
        target = self.root / "keep.bin"
        target.write_bytes(b"keep")
        (self.root / "link.bin").symlink_to(target)
        for command in ("STOR", "APPE"):
            with self.subTest(command=command):
                with self.assertRaises(ftplib.error_perm):
                    self.ftp.storbinary(command + " link.bin", io.BytesIO(b"new"))
                self.assertEqual(target.read_bytes(), b"keep")

    def block_mode(self):
        self.assertTrue(self.ftp.voidcmd("MODE B").startswith("200"))
        self.ftp.voidcmd("TYPE I")

    @staticmethod
    def block(payload=b"", descriptor=0):
        return bytes([descriptor]) + len(payload).to_bytes(2, "big") + payload

    def block_start(self, command, data=None, method="EPSV", rest=None):
        self.assertIn(method, ("PASV", "EPSV"))
        reused = data is not None
        if not reused:
            if method == "PASV":
                host, port = self.ftp.makepasv()
            else:
                response = self.ftp.sendcmd("EPSV")
                port = int(re.search(r"\(\|\|\|(\d+)\|\)", response)[1])
                host = "127.0.0.1"
            data = socket.create_connection((host, port), timeout=5)
            self.addCleanup(data.close)
        if rest is not None:
            self.ftp.sendcmd(f"REST {rest}")
        response = self.ftp.sendcmd(command)
        self.assertTrue(response.startswith("125" if reused else "150"), response)
        data.settimeout(5)
        return data

    def block_upload(self, command, payload, data=None, **kwargs):
        data = self.block_start(command, data, **kwargs)
        for offset in range(0, len(payload), 65535):
            data.sendall(self.block(payload[offset:offset + 65535]))
        data.sendall(self.block(descriptor=0x40))
        self.assertTrue(self.ftp.voidresp().startswith("250"))
        return data

    def read_exact(self, data, size):
        result = bytearray()
        while len(result) < size:
            chunk = data.recv(size - len(result))
            self.assertTrue(chunk, "Data socket closed before the EOF block")
            result.extend(chunk)
        return bytes(result)

    def block_download(self, command, data=None, **kwargs):
        data = self.block_start(command, data, **kwargs)
        result = bytearray()
        while True:
            header = self.read_exact(data, 3)
            self.assertEqual(header[0] & 0x0f, 0)
            payload = self.read_exact(data, int.from_bytes(header[1:], "big"))
            if not header[0] & 0x10:
                result.extend(payload)
            if header[0] & 0x40:
                break
        self.assertTrue(self.ftp.voidresp().startswith("250"))
        return bytes(result), data

    def test_block_roundtrip_and_connection_reuse(self):
        self.block_mode()
        payload = bytes(range(256)) * 9000
        for method in ("EPSV", "PASV"):
            with self.subTest(method=method):
                data = self.block_upload("STOR block.bin", payload, method=method)
                self.assertEqual((self.root / "block.bin").read_bytes(), payload)
                downloaded, _ = self.block_download("RETR block.bin", data)
                self.assertEqual(downloaded, payload)
                self.block_upload("APPE block.bin", b"tail", data)
                self.block_upload("APPE block.bin", b"", data)
                downloaded, _ = self.block_download("RETR block.bin", data)
                self.assertEqual(downloaded, payload + b"tail")
                self.block_upload("STOR block.bin", b"new", data)
                self.assertEqual((self.root / "block.bin").read_bytes(), b"new")
                self.block_upload("STOR block.bin", b"", data)
                downloaded, _ = self.block_download("RETR block.bin", data)
                self.assertEqual(downloaded, b"")
                data.close()

    def test_block_listings_and_resume(self):
        self.block_mode()
        # Start with a listing's smaller buffer, then grow it for file I/O.
        listing, data = self.block_download("NLST")
        self.assertEqual(set(listing.splitlines()) - {b".", b".."}, set())
        self.block_upload("STOR block.bin", b"0123456789", data)
        self.block_upload("STOR block.bin", b"abc", data, rest=5)
        self.assertEqual((self.root / "block.bin").read_bytes(), b"01234abc")
        downloaded, _ = self.block_download("RETR block.bin", data, rest=5)
        self.assertEqual(downloaded, b"abc")
        self.block_upload("APPE block.bin", b"tail", data, rest=2)
        downloaded, _ = self.block_download("RETR block.bin", data)
        self.assertEqual(downloaded, b"01234abctail")
        for command in ("LIST", "NLST", "MLSD"):
            listing, _ = self.block_download(command, data)
            self.assertIn(b"block.bin\r\n", listing)
        with self.assertRaises(ftplib.error_perm):
            self.ftp.sendcmd("RETR missing.bin")
        downloaded, _ = self.block_download("RETR block.bin", data, rest=12)
        self.assertEqual(downloaded, b"")

    def test_block_large_listing(self):
        self.block_mode()
        names = {f"{i:04d}-" + "x" * 180 for i in range(400)}
        for name in names:
            (self.root / name).touch()
        listing, _ = self.block_download("NLST")
        self.assertGreater(len(listing), 65535)
        self.assertEqual(set(listing.decode().splitlines()) - {".", ".."}, names)

    def test_block_fragmented_headers_and_restart_markers(self):
        self.block_mode()
        data = self.block_start("STOR flags.bin")
        frames = [
            self.block(), self.block(b"checkpoint", 0x10),
            self.block(b"record", 0x80), self.block(b"suspect", 0x20),
            self.block(b"ignored" * 9000, 0x10), self.block(b"final", 0xc0),
        ]
        for frame in frames:
            for byte in frame[:3]:
                data.sendall(bytes([byte]))
                time.sleep(0.005)
            data.sendall(frame[3:])
        self.assertTrue(self.ftp.voidresp().startswith("250"))
        self.assertEqual((self.root / "flags.bin").read_bytes(), b"recordsuspectfinal")
        self.block_start("STOR marker-eof.bin", data)
        data.sendall(self.block(b"metadata", 0x50))
        self.assertTrue(self.ftp.voidresp().startswith("250"))
        self.assertEqual((self.root / "marker-eof.bin").read_bytes(), b"")
        self.block_upload("STOR after.bin", b"healthy", data)

    def test_block_many_blocks_per_read(self):
        # Several blocks arrive in one socket read and are written in large chunks.
        self.block_mode()
        payload = os.urandom(3 * 1024 * 1024 + 123)
        sizes = [1, 3, 65535, 2, 40000, 65535, 7]
        wire, offset, i = bytearray(), 0, 0
        while offset < len(payload):
            size = sizes[i % len(sizes)]
            wire += self.block(payload[offset:offset + size])
            if i == 5:
                wire += self.block(b"marker", 0x10)
            offset += size
            i += 1
        data = self.block_start("STOR many.bin")
        data.sendall(bytes(wire) + self.block(descriptor=0x40))
        self.assertTrue(self.ftp.voidresp().startswith("250"))
        self.assertEqual((self.root / "many.bin").read_bytes(), payload)

        # A connection lost mid-block keeps every payload byte that arrived.
        data = self.block_start("STOR cut.bin", data)
        data.sendall(self.block(payload[:65535]) + self.block(payload[65535:131070])[:1000])
        data.shutdown(socket.SHUT_WR)
        with self.assertRaises(ftplib.error_temp):
            self.ftp.voidresp()
        self.assertEqual((self.root / "cut.bin").read_bytes(), payload[:65535 + 997])

    def test_block_eof_does_not_consume_next_transfer(self):
        self.block_mode()
        data = self.block_start("STOR first.bin")
        data.sendall(self.block(b"first", 0x40) + self.block(b"second", 0x40))
        self.assertTrue(self.ftp.voidresp().startswith("250"))
        self.block_start("STOR second.bin", data)
        self.assertTrue(self.ftp.voidresp().startswith("250"))
        self.assertEqual((self.root / "first.bin").read_bytes(), b"first")
        self.assertEqual((self.root / "second.bin").read_bytes(), b"second")

    def test_block_truncated_uploads_are_rejected(self):
        self.block_mode()
        for wire in (b"", b"\0", b"\0\0", b"\0\0\5abc", b"\0\0\3abc"):
            with self.subTest(wire=wire):
                data = self.block_start("STOR truncated.bin")
                data.sendall(wire)
                data.shutdown(socket.SHUT_WR)
                with self.assertRaises(ftplib.error_temp) as error:
                    self.ftp.voidresp()
                self.assertTrue(str(error.exception).startswith("426"))
                self.assertEqual(data.recv(1), b"")
                self.ftp.voidcmd("NOOP")
        self.block_upload("STOR recovered.bin", b"healthy")
        self.assertEqual((self.root / "recovered.bin").read_bytes(), b"healthy")

    def test_block_invalid_descriptors_are_rejected(self):
        self.block_mode()
        for descriptor in (0x01, 0x08, 0xff):
            with self.subTest(descriptor=descriptor):
                data = self.block_start("STOR invalid.bin")
                data.sendall(self.block(descriptor=descriptor))
                with self.assertRaises(ftplib.error_temp) as error:
                    self.ftp.voidresp()
                self.assertTrue(str(error.exception).startswith("426"))
                self.assertEqual(data.recv(1), b"")
                self.ftp.voidcmd("NOOP")
        self.block_upload("STOR recovered.bin", b"healthy")

    def test_block_closed_idle_connection(self):
        self.block_mode()
        data = self.block_upload("STOR idle.bin", b"keep")
        data.shutdown(socket.SHUT_WR)
        with self.assertRaises(ftplib.error_temp) as error:
            self.ftp.sendcmd("STOR idle.bin")
        self.assertTrue(str(error.exception).startswith("425"))
        self.assertEqual((self.root / "idle.bin").read_bytes(), b"keep")
        self.block_upload("STOR idle.bin", b"new")
        self.assertEqual((self.root / "idle.bin").read_bytes(), b"new")

    def test_block_mode_changes_and_abort(self):
        self.block_mode()
        data = self.block_upload("STOR mode.bin", b"block")
        self.ftp.voidcmd("MODE b")
        for command in ("MODE", "MODE BS", "MODE S extra", "MODE C"):
            with self.assertRaises(ftplib.error_perm):
                self.ftp.sendcmd(command)
        downloaded, _ = self.block_download("RETR mode.bin", data)
        self.assertEqual(downloaded, b"block")
        self.ftp.voidcmd("MODE S")
        self.assertEqual(data.recv(1), b"")
        self.ftp.storbinary("STOR mode.bin", io.BytesIO(b"stream"))
        self.assertEqual(self.download("mode.bin"), b"stream")
        self.block_mode()
        data = self.block_upload("STOR mode.bin", b"block again")
        self.assertTrue(self.ftp.sendcmd("ABOR").startswith("225"))
        self.assertEqual(data.recv(1), b"")
        with self.assertRaises(ftplib.error_temp):
            self.ftp.sendcmd("RETR mode.bin")
        self.block_upload("APPE mode.bin", b"tail")
        self.assertEqual((self.root / "mode.bin").read_bytes(), b"block againtail")

    @unittest.skipUnless(os.environ.get("FTP_MODE_B_CURL"), "Set FTP_MODE_B_CURL to the curl fork")
    def test_block_curl_interoperability(self):
        payload = bytes(range(256)) * 9000
        (self.root / "source.bin").write_bytes(payload)
        base = f"ftp://127.0.0.1:{self.port}/{str(self.root).lstrip('/')}/"
        for epsv in (True, False):
            with self.subTest(epsv=epsv):
                commands = [
                    ["-T", str(self.root / "source.bin"), base + "curl.bin"],
                    [base + "curl.bin", "-o", str(self.root / "download.bin")],
                    [base, "-l", "-o", str(self.root / "listing.txt")],
                    ["-T", str(self.root / "source.bin"), "--append", base + "curl.bin"],
                    [base + "curl.bin", "-o", str(self.root / "appended.bin")],
                ]
                args = [os.environ["FTP_MODE_B_CURL"], "-q", "--fail-early"]
                for index, command in enumerate(commands):
                    if index:
                        args += ["--next"]
                    args += ["--ftp-block-mode", "--verbose", "--fail", "--max-time", "15"]
                    args += ["--noproxy", "*"]
                    if not epsv:
                        args += ["--disable-epsv"]
                    args += command
                result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        timeout=90, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((self.root / "download.bin").read_bytes(), payload)
                self.assertEqual((self.root / "appended.bin").read_bytes(), payload * 2)
                self.assertIn("curl.bin", (self.root / "listing.txt").read_text().splitlines())
                log = result.stderr.splitlines()
                self.assertFalse(any(re.match(r"> (EPRT|PORT)\b", line) for line in log))
                setup = [line for line in log if re.match(r"> (EPSV|PASV)\b", line)]
                self.assertEqual(len(setup), 1, result.stderr)
                self.assertTrue(setup[0].startswith("> EPSV" if epsv else "> PASV"))
                self.assertEqual(sum(line.startswith("> MODE B") for line in log), 1)
                self.assertEqual(sum(line.startswith("< 125 ") for line in log), 4)
                self.assertEqual(sum(line.startswith("< 250 Transfer complete") for line in log), 5)


    # FTPMC (draft-ftpmc-00): one transfer over several data connections.

    def mc_enable(self, n, stripe=None):
        response = self.ftp.sendcmd(f"OPTS MC {n}" + (f" {stripe}" if stripe else ""))
        match = re.fullmatch(r"200 MC (\d+) (\d+)", response)
        self.assertTrue(match, response)
        return int(match[1]), int(match[2])

    def mc_passive(self, connect=True):
        response = self.ftp.sendcmd("MPSV")
        self.assertTrue(response.startswith("224 "), response)
        ports = [int(p) for p in re.search(r"\(\|\|\|([\d,]+)\|\)", response)[1].split(",")]
        if not connect:
            return ports
        return [self.mc_connect(port) for port in ports]

    def mc_connect(self, port):
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.addCleanup(sock.close)
        return sock

    @staticmethod
    def mc_split(data, n, stripe):
        parts = [bytearray() for _ in range(n)]
        for i in range(0, len(data), stripe):
            parts[(i // stripe) % n] += data[i:i + stripe]
        return parts

    @staticmethod
    def mc_join(parts, stripe):
        # The receiver algorithm of draft Section 6.2, executed literally.
        out, pos, i = bytearray(), [0] * len(parts), 0
        while True:
            k = i % len(parts)
            chunk = parts[k][pos[k]:pos[k] + stripe]
            pos[k] += len(chunk)
            out += chunk
            if len(chunk) < stripe:
                assert all(p == len(part) for p, part in zip(pos, parts)), "inconsistent"
                return bytes(out)
            i += 1

    def mc_parallel(self, func, socks, args):
        errors = []

        def run(*a):
            try:
                func(*a)
            except BaseException as error:  # reported in the main thread
                errors.append(error)

        threads = [threading.Thread(target=run, args=(sock, arg)) for sock, arg in zip(socks, args)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        if errors:
            raise errors[0]

    def mc_send(self, socks, data, stripe, block=False):
        def send(sock, part):
            if block:
                for offset in range(0, len(part), 65535):
                    sock.sendall(self.block(part[offset:offset + 65535]))
                sock.sendall(self.block(descriptor=0x40))
            else:
                sock.sendall(part)
                sock.shutdown(socket.SHUT_WR)
        self.mc_parallel(send, socks, self.mc_split(data, len(socks), stripe))

    def mc_recv(self, socks, stripe, block=False):
        parts = [bytearray() for _ in socks]

        def recv(sock, part):
            if not block:
                while chunk := sock.recv(1 << 20):
                    part += chunk
                return
            while True:
                header = self.read_exact(sock, 3)
                self.assertEqual(header[0] & ~0x40, 0)
                part += self.read_exact(sock, int.from_bytes(header[1:], "big"))
                if header[0] & 0x40:
                    return
        self.mc_parallel(recv, socks, parts)
        return self.mc_join(parts, stripe)

    def mc_upload(self, command, data, stripe, socks=None, block=False, rest=None):
        reused = socks is not None
        socks = socks or self.mc_passive()
        if rest is not None:
            self.ftp.sendcmd(f"REST {rest}")
        response = self.ftp.sendcmd(command)
        # All connections were open before the command: 125 (Section 5.3).
        self.assertTrue(response.startswith("125"), response)
        self.mc_send(socks, data, stripe, block)
        response = self.ftp.voidresp()
        self.assertTrue(response.startswith("250" if block else "226"), response)
        return socks

    def mc_download(self, command, stripe, socks=None, block=False, rest=None):
        socks = socks or self.mc_passive()
        if rest is not None:
            self.ftp.sendcmd(f"REST {rest}")
        response = self.ftp.sendcmd(command)
        self.assertTrue(response.startswith("125"), response)
        data = self.mc_recv(socks, stripe, block)
        response = self.ftp.voidresp()
        self.assertTrue(response.startswith("250" if block else "226"), response)
        return data, socks

    def test_mc_negotiation(self):
        self.assertIn(" MC 16", self.ftp.sendcmd("FEAT").splitlines())
        for command in ("MPSV", "MPRT |1|127.0.0.1|5000|"):
            with self.assertRaises(ftplib.error_perm) as error:
                self.ftp.sendcmd(command)
            self.assertTrue(str(error.exception).startswith("503"))
        self.assertEqual(self.mc_enable(4), (4, 4096))
        self.assertEqual(self.mc_enable(9, 1048576), (9, 65536))
        self.assertEqual(self.mc_enable(20), (16, 65536))
        self.assertEqual(self.mc_enable(2, 100), (2, 512))
        self.assertEqual(self.mc_enable(3), (3, 512))
        self.assertEqual(self.mc_enable(1, 4294967295), (1, 65536))
        for args in ("", "0", "x", "4 0", "4 4294967296", "4 4096 1", "123456", "-1"):
            with self.subTest(args=args):
                with self.assertRaises(ftplib.error_perm) as error:
                    self.ftp.sendcmd("OPTS MC " + args)
                self.assertTrue(str(error.exception).startswith("501"))
        self.assertEqual(self.ftp.sendcmd("OPTS MC OFF"), "200 MC OFF")
        with self.assertRaises(ftplib.error_perm):
            self.ftp.sendcmd("MPSV")

    def test_mc_stream_roundtrip(self):
        self.ftp.voidcmd("TYPE I")
        for n, stripe in ((4, 4096), (3, 512), (2, 65536), (1, 4096)):
            self.assertEqual(self.mc_enable(n, stripe), (n, stripe))
            for size in (0, 1, stripe - 1, stripe, n * stripe, n * stripe + 1,
                         3 * n * stripe + 17, 300001):
                with self.subTest(n=n, stripe=stripe, size=size):
                    data = os.urandom(size)
                    self.mc_upload("STOR mc.bin", data, stripe)
                    self.assertEqual((self.root / "mc.bin").read_bytes(), data)
                    downloaded, _ = self.mc_download("RETR mc.bin", stripe)
                    self.assertEqual(downloaded, data)

    def test_mc_large_transfer(self):
        # Larger than the ring and the socket buffers, so it wraps many times.
        self.ftp.voidcmd("TYPE I")
        data = os.urandom(24 * 1024 * 1024 + 4321)
        for n, stripe in ((4, 4096), (4, 65536), (3, 5000), (16, 65536)):
            with self.subTest(n=n, stripe=stripe):
                self.mc_enable(n, stripe)
                self.mc_upload("STOR big.bin", data, stripe)
                self.assertEqual((self.root / "big.bin").read_bytes(), data)
                downloaded, _ = self.mc_download("RETR big.bin", stripe)
                self.assertEqual(downloaded, data)

    def test_mc_buffer_budget(self):
        # The ring (2 * N * S) and the 2 * N socket buffers of at least
        # 128 KiB must fit in 16 MiB, so large stripes are lowered.
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        for args in (["-c", "61"], ["-s", "4095"]):
            result = subprocess.run([self.binary, "-p", str(port)] + args,
                                    stdout=subprocess.DEVNULL, timeout=5)
            self.assertNotEqual(result.returncode, 0, args)
        server = subprocess.Popen([self.binary, "-p", str(port), "-s", "4194304"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(server.wait)
        self.addCleanup(server.terminate)
        deadline = time.monotonic() + 10
        while True:
            try:
                ftp = ftplib.FTP()
                ftp.connect("127.0.0.1", port, timeout=5)
                break
            except OSError:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.05)
        with ftp:
            ftp.login()
            mib = 1024 * 1024
            for n, expected in ((16, (16 * mib - 32 * 128 * 1024) // 32),
                                (2, (16 * mib - 4 * 128 * 1024) // 4), (1, 4194304)):
                response = ftp.sendcmd(f"OPTS MC {n} 4194304")
                self.assertEqual(response, f"200 MC {n} {expected}")

    def test_mc_preliminary_reply_waits_for_all_connections(self):
        self.ftp.voidcmd("TYPE I")
        (self.root / "mc.bin").write_bytes(b"x" * 10000)
        self.mc_enable(3)
        ports = self.mc_passive(connect=False)
        self.ftp.putcmd("RETR mc.bin")
        time.sleep(0.3)  # the server is now waiting for the connections
        socks = [self.mc_connect(port) for port in ports]
        self.assertTrue(self.ftp.getresp().startswith("150"))
        self.assertEqual(self.mc_recv(socks, 4096), b"x" * 10000)
        self.assertTrue(self.ftp.voidresp().startswith("226"))
        # In MODE S a set serves one transfer only (Section 7.1).
        with self.assertRaises(ftplib.error_temp) as error:
            self.ftp.sendcmd("RETR mc.bin")
        self.assertTrue(str(error.exception).startswith("425"))

    def test_mc_resume_and_append(self):
        self.ftp.voidcmd("TYPE I")
        self.mc_enable(3, 1000)
        data = os.urandom(10000)
        target = self.root / "mc.bin"
        target.write_bytes(data[:4000] + b"junk" * 5000)
        self.mc_upload("STOR mc.bin", data[4000:], 1000, rest=4000)
        self.assertEqual(target.read_bytes(), data)
        downloaded, _ = self.mc_download("RETR mc.bin", 1000, rest=2500)
        self.assertEqual(downloaded, data[2500:])
        downloaded, _ = self.mc_download("RETR mc.bin", 1000, rest=len(data))
        self.assertEqual(downloaded, b"")
        self.mc_upload("APPE mc.bin", b"tail" * 1000, 1000)
        self.assertEqual(target.read_bytes(), data + b"tail" * 1000)

    def test_mc_listings(self):
        names = {f"{i:04d}-" + "x" * 100 for i in range(300)}
        for name in names:
            (self.root / name).touch()
        self.mc_enable(4)
        listing, _ = self.mc_download("NLST", 4096)
        self.assertEqual(set(listing.decode().splitlines()) - {".", ".."}, names)
        self.block_mode()
        socks = None
        for command in ("NLST", "LIST", "MLSD"):
            listing, socks = self.mc_download(command, 4096, socks, block=True)
            self.assertIn(b"0000-", listing)

    def test_mc_block_persistent_set(self):
        self.block_mode()
        self.mc_enable(4, 4096)
        data = os.urandom(200000)
        socks = self.mc_upload("STOR mc.bin", data, 4096, block=True)
        self.assertEqual((self.root / "mc.bin").read_bytes(), data)
        for _ in range(3):
            downloaded, _ = self.mc_download("RETR mc.bin", 4096, socks, block=True)
            self.assertEqual(downloaded, data)
        self.mc_upload("STOR mc.bin", b"", 4096, socks, block=True)
        self.assertEqual((self.root / "mc.bin").read_bytes(), b"")
        self.mc_upload("STOR mc.bin", b"abc", 4096, socks, block=True)
        downloaded, _ = self.mc_download("RETR mc.bin", 4096, socks, block=True, rest=1)
        self.assertEqual(downloaded, b"bc")
        # MODE discards the set (Section 7.2).
        self.ftp.voidcmd("MODE S")
        for sock in socks:
            self.assertEqual(sock.recv(1), b"")

    def test_mc_block_closed_connection_discards_set(self):
        self.block_mode()
        self.mc_enable(2)
        socks = self.mc_upload("STOR mc.bin", b"keep", 4096, block=True)
        socks[1].close()
        time.sleep(0.1)
        with self.assertRaises(ftplib.error_temp) as error:
            self.ftp.sendcmd("RETR mc.bin")
        self.assertTrue(str(error.exception).startswith("425"))
        self.assertEqual(socks[0].recv(1), b"")

    def test_mc_block_invalid_descriptor(self):
        self.block_mode()
        self.mc_enable(2)
        for descriptor in (0x10, 0x80, 0x20, 0x01):
            with self.subTest(descriptor=descriptor):
                socks = self.mc_passive()
                self.assertTrue(self.ftp.sendcmd("STOR bad.bin").startswith("125"))
                socks[0].sendall(self.block(b"data", descriptor | 0x40))
                socks[1].sendall(self.block(descriptor=0x40))
                with self.assertRaises(ftplib.error_temp) as error:
                    self.ftp.voidresp()
                self.assertTrue(str(error.exception).startswith("426"))

    def test_mc_active_mode(self):
        self.ftp.voidcmd("TYPE I")
        self.mc_enable(3, 2048)
        listeners = []
        for _ in range(3):
            listener = socket.create_server(("127.0.0.1", 0))
            listener.settimeout(5)
            self.addCleanup(listener.close)
            listeners.append(listener)
        ports = [listener.getsockname()[1] for listener in listeners]
        for bad, code in ((ports[:2], "501"), ([ports[0]] * 3, "501"),
                          ([ports[0], ports[1], 80], "504")):
            with self.assertRaises(ftplib.error_perm) as error:
                self.ftp.sendcmd("MPRT |1|127.0.0.1|" + ",".join(map(str, bad)) + "|")
            self.assertTrue(str(error.exception).startswith(code))
        with self.assertRaises(ftplib.error_perm) as error:
            self.ftp.sendcmd("MPRT |2|::1|1,2,3|")
        self.assertTrue(str(error.exception).startswith("522"))
        with self.assertRaises(ftplib.error_perm) as error:
            self.ftp.sendcmd("MPRT |1|10.1.2.3|" + ",".join(map(str, ports)) + "|")
        self.assertTrue(str(error.exception).startswith("504"))

        data = os.urandom(123457)
        (self.root / "mc.bin").write_bytes(data)
        self.ftp.voidcmd("MPRT |1|127.0.0.1|" + ",".join(map(str, ports)) + "|")
        self.assertTrue(self.ftp.sendcmd("RETR mc.bin").startswith("150"))
        socks = []
        for listener in listeners:
            sock, _ = listener.accept()
            self.addCleanup(sock.close)
            socks.append(sock)
        self.assertEqual(self.mc_recv(socks, 2048), data)
        self.assertTrue(self.ftp.voidresp().startswith("226"))

    def test_mc_single_connection_commands_discard_set(self):
        self.mc_enable(2)
        ports = self.mc_passive(connect=False)
        self.ftp.storbinary("STOR single.bin", io.BytesIO(b"single"))
        self.assertEqual((self.root / "single.bin").read_bytes(), b"single")
        for port in ports:
            with self.assertRaises(OSError):
                socket.create_connection(("127.0.0.1", port), timeout=2).close()

    def test_mc_upload_failure_keeps_valid_prefix(self):
        self.ftp.voidcmd("TYPE I")
        self.mc_enable(3, 4096)
        data = os.urandom(10 * 4096)
        target = self.root / "mc.bin"
        target.write_bytes(b"A" * 100000)
        socks = self.mc_passive()
        self.ftp.sendcmd("REST 1000")
        self.assertTrue(self.ftp.sendcmd("STOR mc.bin").startswith("125"))
        parts = self.mc_split(data, 3, 4096)
        parts[1] = parts[1][:4096]  # connection 1 ends early, after stripe 1
        for sock, part in zip(socks, parts):
            sock.sendall(part)
            sock.shutdown(socket.SHUT_WR)
        with self.assertRaises(ftplib.error_temp) as error:
            self.ftp.voidresp()
        self.assertTrue(str(error.exception).startswith("426"))
        stored = target.read_bytes()
        self.assertEqual(stored[:1000], b"A" * 1000)
        self.assertLessEqual(len(stored), 1000 + 4 * 4096)
        self.assertTrue(data.startswith(stored[1000:]))
        self.assertEqual(self.ftp.sendcmd(f"SIZE mc.bin"), f"213 {len(stored)}")

    def mc_big_file(self):
        path = self.root / "big.bin"
        with open(path, "wb") as out:
            out.truncate(64 * 1024 * 1024)
        return path

    def mc_drain(self, sock, timeout):
        # Read until the server closes the connection.
        sock.settimeout(timeout)
        try:
            while sock.recv(1 << 20):
                pass
        except ConnectionResetError:
            pass

    def test_mc_abort(self):
        self.ftp.voidcmd("TYPE I")
        self.mc_big_file()
        self.mc_enable(4)
        socks = self.mc_passive()
        self.assertTrue(self.ftp.sendcmd("RETR big.bin").startswith("125"))
        time.sleep(0.2)
        self.ftp.putcmd("ABOR")
        with self.assertRaises(ftplib.error_temp) as error:
            self.ftp.getresp()
        self.assertTrue(str(error.exception).startswith("426"))
        self.assertTrue(self.ftp.voidresp().startswith("226"))
        for sock in socks:
            self.mc_drain(sock, 5)
        self.assertTrue(self.ftp.voidcmd("NOOP").startswith("200"))

    def test_mc_client_disconnect_frees_connections(self):
        self.mc_big_file()
        for command in ("RETR big.bin", "STOR up.bin"):
            with self.subTest(command=command):
                ftp = self.connect()
                ftp.cwd(str(self.root))
                ftp.voidcmd("TYPE I")
                ftp.sendcmd("OPTS MC 4")
                response = ftp.sendcmd("MPSV")
                ports = [int(p) for p in re.search(r"\|\|\|([\d,]+)\|", response)[1].split(",")]
                socks = [self.mc_connect(port) for port in ports]
                self.assertTrue(ftp.sendcmd(command).startswith("125"))
                if command.startswith("STOR"):
                    socks[0].sendall(b"x" * 100000)
                time.sleep(0.2)
                ftp.sock.close()
                ftp.file.close()
                # The server notices at once, not after its stall timeout.
                started = time.monotonic()
                for sock in socks:
                    self.mc_drain(sock, 5)
                self.assertLess(time.monotonic() - started, 3)
        self.assertTrue(self.ftp.voidcmd("NOOP").startswith("200"))


if __name__ == "__main__":
    Transfers.binary = str(Path(sys.argv.pop(1)).resolve())
    unittest.main(verbosity=2)

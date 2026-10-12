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
        listener = None
        reused = data is not None
        if not reused:
            if method in ("PASV", "EPSV"):
                if method == "PASV":
                    host, port = self.ftp.makepasv()
                else:
                    response = self.ftp.sendcmd("EPSV")
                    port = int(re.search(r"\(\|\|\|(\d+)\|\)", response)[1])
                    host = "127.0.0.1"
                data = socket.create_connection((host, port), timeout=5)
                self.addCleanup(data.close)
            else:
                listener = socket.socket()
                self.addCleanup(listener.close)
                listener.bind(("127.0.0.1", 0))
                listener.listen(1)
                listener.settimeout(5)
                port = listener.getsockname()[1]
                if method == "PORT":
                    self.ftp.sendport("127.0.0.1", port)
                else:
                    self.ftp.voidcmd(f"EPRT |1|127.0.0.1|{port}|")
        if rest is not None:
            self.ftp.sendcmd(f"REST {rest}")
        response = self.ftp.sendcmd(command)
        self.assertTrue(response.startswith("125" if reused else "150"), response)
        if listener:
            data, _ = listener.accept()
            listener.close()
            self.addCleanup(data.close)
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
        for method in ("EPSV", "PASV", "EPRT", "PORT"):
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
        for active in (False, True):
            with self.subTest(active=active):
                commands = [
                    ["-T", str(self.root / "source.bin"), base + "curl.bin"],
                    [base + "curl.bin", "-o", str(self.root / "download.bin")],
                    [base, "-l", "-o", str(self.root / "listing.txt")],
                    ["-T", str(self.root / "source.bin"), "--append", base + "curl.bin"],
                    [base + "curl.bin", "-o", str(self.root / "appended.bin")],
                ]
                args = [os.environ["FTP_MODE_B_CURL"]]
                for index, command in enumerate(commands):
                    if index:
                        args += ["--next"]
                    args += ["--ftp-block-mode", "--verbose", "--fail", "--max-time", "15"]
                    if active:
                        args += ["--ftp-port", "127.0.0.1"]
                    args += command
                result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        timeout=90, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((self.root / "download.bin").read_bytes(), payload)
                self.assertEqual((self.root / "appended.bin").read_bytes(), payload * 2)
                self.assertIn("curl.bin", (self.root / "listing.txt").read_text().splitlines())
                log = result.stderr.splitlines()
                setup = [line for line in log if re.match(r"> (EPSV|PASV|EPRT|PORT)\b", line)]
                self.assertEqual(len(setup), 1, result.stderr)
                self.assertEqual(sum(line.startswith("> MODE B") for line in log), 1)
                self.assertEqual(sum(line.startswith("< 125 ") for line in log), 4)
                self.assertEqual(sum(line.startswith("< 250 Transfer complete") for line in log), 5)


if __name__ == "__main__":
    Transfers.binary = str(Path(sys.argv.pop(1)).resolve())
    unittest.main(verbosity=2)

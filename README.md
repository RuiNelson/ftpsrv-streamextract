# ftpsrv
This is a simple FTP server that can be executed on a Playstation 4 or
Playstation 5 that has been jailbroken and is running an ELF loader.
The FTP server accepts connection on port 2121.

## Features
Client software that has been tested include gFTP, Filezilla, curl, and Thunar.
FTP block mode (`MODE B`, [RFC 959 section 3.4.2][ftp-rfc]) is supported for
downloads, uploads, appends, directory listings, and the PS5 virtual `/proc`
transfers. Blocks use a three-byte header and at most 65535 payload bytes;
an explicit EOF block ends each transfer without closing the data connection.
Successful transfers return `250`, and the next transfer reuses the connection
with `125`, using passive data connections. `MODE S` selects the default
stream mode again. Changing mode, configuring a new data endpoint, or sending
`ABOR` closes the previous data connection.

Use the curl fork's `--ftp-block-mode` option to select block mode, for example:

```console
curl --ftp-block-mode ftp://ps5:2121/path/file -o file
```

Uploads reject truncated block headers/payloads, invalid descriptor bits, and
connection closure without EOF with `426`. Restart-marker blocks are skipped
as metadata; the existing numeric `REST` offsets remain supported for binary
file transfers. Only file structure (`STRU F`) is supported.

The payload supports a couple of custom SITE commands specifically for the
PS4 and PS5 (executed without prepending SITE). In particular:
 - KILL - kill the FTP server.
 - MTRW - remount /system and /system_ex with write permissions.
 - AUTHID <hex_authid> - set process AuthID (PS5 only).
 - SELF - toggle on-the-fly SELF to ELF decryption (enabled by default).
 - SCHK - toggle SELF digest verification.

Note: the SELF command operates on induvidual connections so use e.g.,
```console
john@localhost:ftpsrv$ curl -o libkernel.sprx -Q SELF ftp://ps5:2121/system/common/lib/libkernel.sprx
```

## Multiple data connections (FTPMC)
The server implements [FTPMC][ftpmc] (draft-ftpmc-00), which carries one
transfer over N data connections in parallel: the data stream is cut into
stripes of S octets, and stripe i goes on connection i mod N. It works in
`MODE S` and `MODE B`, for `RETR`, `STOR`, `APPE` and listings.

```
C: OPTS MC 4 65536
S: 200 MC 4 65536
C: MPSV
S: 224 Entering Multi-Passive Mode (|||50000,50001,50002,50003|)
C: RETR file.bin
S: 125 Data connections already open; transfer starting (MC 4 65536)
S: 226 Transfer complete
```

`FEAT` lists ` MC 16`. By default the server accepts up to 16 connections and
stripes up to 64 KiB; a larger request gets these values in the `200 MC`
reply. `MPRT` is the active-mode equivalent, and only connects to the client's
own address. In `MODE B` the connection set stays open between transfers.

Each transfer runs in the session thread with non-blocking sockets and
`poll()`. Data goes through one ring buffer (1 MiB, or 2 × N × S when larger)
that exists only during the transfer, so idle sessions hold no buffer, even
with an open `MODE B` set. Each connection reads or writes its stripes with
scatter/gather I/O, and the file is read and written with large sequential
calls, so an upload that fails leaves a valid prefix on disk.

All buffers of a transfer, the ring plus the send and receive socket buffers
of every connection, are limited to 16 MiB (`FTP_MC_BUFFER_BUDGET`). The
socket buffers share what the ring leaves, up to the 4 MiB of a single data
connection: 448 KiB each with N = 16 and S = 64 KiB, 1.875 MiB with N = 4.
A stripe that would leave less than 128 KiB per buffer is lowered.

A transfer ends at once, freeing its connections and buffer, when the client
closes the control connection or sends `ABOR`, and fails with `426` when no
connection makes progress for 60 seconds. Data connections use TCP keepalive,
so a client that vanishes also frees an idle set.

The limits can be changed with `-c CONNS` (up to 60) and `-s STRIPE` on Linux
and macOS, or at compile time with `-DFTP_MC_MAX_CONN=8
-DFTP_MC_MAX_STRIPE=1048576`.

## Additional commands
These commands are implemented but were not previously documented here:
 - DSIZ <path> - report recursive directory size (errors if path is a file).
 - RMDA <dir> - delete a directory tree (alias: SITE RMDIR).
 - AVBL <path> - available space in bytes for the given path.
 - XQUOTA - report file count/limits and disk usage/limits.
 - SITE CHMOD <mode> <path> - change permissions.
 - SITE UMASK [mode] - get/set file creation mask.
 - SITE SYMLINK <target> <link> - create a symlink.
 - SITE CPFR <from> / SITE CPTO <to> - server-side copy (asynchronous).
 - SITE COPY <from> <to> - server-side copy (asynchronous).
 - SITE MOVE <from> <to> - server-side move (asynchronous).
 - SITE LOWER <path> - recursively rename a file or directory tree to lowercase.
 - SITE UPPER <path> - recursively rename a file or directory tree to uppercase.
 - STOP / SITE STOP - stop the current copy/move/delete/case operation.
 - SITE AUTHID <hex_authid> - set process AuthID (PS5 only).

On PS5, the server also exposes a virtual `/proc` directory in the root listing.
`LIST /proc` shows process files with process start times and names formatted
as `<pid>_<title-id>_<command>`, plus `_all_` for the full process table.
`RETR /proc/_all_` shows the current process table, and `RETR /proc/<pid>` or
`RETR /proc/<pid>_<title-id>_<command>` returns detailed process information.
Deleting a process file, for example `DELE /proc/105_0000_ps`, sends `SIGKILL`
to that process.

Note: the copy and move commands return immediately with `250 Copy started in
background` or `250 Move started in background`; errors during the background
operation are not reported to the client. Only one copy, move, recursive
delete, LOWER, or UPPER operation can run on the server at a time.

The LOWER and UPPER commands also work without the SITE prefix. They rename the
target path and every nested file, directory, and symlink name. Before changing
anything, the server checks each directory for case-conversion name collisions
such as `File.bin` and `file.bin`.

Some clients (e.g. WinSCP) can be configured to use these extra commands
for file management operations.

## Building for the PS4
Assuming you have the [ps4-payload-sdk][sdk-ps4] installed on a POSIX machine,
the FTP server can be compiled using the following two commands:
```console
john@localhost:ftpsrv$ export PS4_PAYLOAD_SDK=/opt/ps4-payload-sdk
john@localhost:ftpsrv$ make -f Makefile.ps4
```

## Building for the PS5
Assuming you have the [ps5-payload-sdk][sdk-ps5] installed on a POSIX machine,
the FTP server can be compiled using the following two commands:
```console
john@localhost:ftpsrv$ export PS5_PAYLOAD_SDK=/opt/ps5-payload-sdk
john@localhost:ftpsrv$ make -f Makefile.ps5
```

## Building for GNU/Linux systems
Assuming you have a compiler toolchain installed on your GNU/Linux system,
the FTP server can be compiled using the following command:
```console
john@localhost:ftpsrv$ make -f Makefile.linux
```

Run local regression checks (requires Python 3) with
`make -f Makefile.linux check`. The checks launch their own server on a temporary
port and use temporary directories for all file transfers.

## Building for macOS
Assuming you have the Xcode Command Line Tools installed, the FTP server can be
compiled using the following command:
```console
john@localhost:ftpsrv$ make -f Makefile.macos
```

Run local regression checks (requires Python 3) with
`make -f Makefile.macos check`, optionally with `FTP_MODE_B_CURL=/path/to/curl`
to test block-mode interoperability against the curl fork.

## Known issues
Some PS5 firmwares below vesion 4 contains a kernel bug where reading from some SELF
files causes the read syscall to stall.

## Reporting Bugs
If you encounter problems with ftpsrv, please [file a github issue][issues].
If you plan on sending pull requests which affect more than a few lines of code,
please file an issue before you start to work on you changes. This will allow us
to discuss the solution properly before you commit time and effort.

## License
ftpsrv is licensed under the GPLv3+.

[sdk-ps4]: https://github.com/ps4-payload-dev/sdk
[sdk-ps5]: https://github.com/ps5-payload-dev/sdk
[issues]: https://github.com/ps5-payload-dev/ftpsrv/issues/new
[ftp-rfc]: https://www.rfc-editor.org/rfc/rfc959.html#section-3.4.2
[ftpmc]: https://github.com/RuiNelson/FTPMC

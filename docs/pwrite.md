# Positional writes over the FTP control connection

`SITE PWRITE <path> <offset> <hex-data>` overwrites an existing regular file
without truncating it. No data connection is opened. This is a custom extension
for curl-streamextract and OnTheFly5; it is not SFTP.

Paths without spaces may be bare. Double-quoted paths support `\\` for a
literal backslash and `\"` for a literal double quote. Relative paths resolve
against the FTP working directory. Control characters are not allowed.

Offsets are unsigned decimal byte positions, representable by the server's
signed file-offset type. Payloads contain 1 through 1024 bytes, encoded as
contiguous hexadecimal pairs (either letter case). The entire range must
already exist: PWRITE never creates or extends a file and rejects symlink
and non-regular targets. Reserve metadata space in the initial upload.
The server's normal control-line limit (8192 bytes including framing/storage)
also applies; very long escaped paths can reduce the usable payload size.

Responses: `250 PWRITE OK` after writing and closing; `501` for malformed
arguments or excessive payload; `551` for a range beyond EOF; `550` for
filesystem failures. A successful reply does not imply fsync durability.
An I/O failure can leave a partial patch; retry the full patch after resolving
the failure. Writes are not transactional or coordinated with other sessions.
Finish uploading before patching; do not write or truncate the same file
concurrently. TYPE, MODE, FTPMC settings and pending REST offsets are unchanged.

Example (replace the first two bytes, preserving all subsequent bytes):

```sh
curl --quote 'SITE PWRITE "/data/game folder/game.pkg" 0 7f43' \
  --head ftp://ps5:2121/
```

Discover support through the `SITE PWRITE` line in `FEAT`. Larger patches must
be split into bounded commands. With libcurl, use `CURLOPT_QUOTE` and
`CURLOPT_NOBODY`; use an absolute remote path because QUOTE runs before CWD.

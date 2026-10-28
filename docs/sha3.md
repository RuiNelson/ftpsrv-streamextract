# Server-side SHA3-256

`SITE SHA3 <path> <offset> <length>` hashes exactly the selected existing file
range, using SHA3-256 (FIPS 202). Offset and length are unsigned decimal text,
limited to the server's signed 64-bit file-offset range. Length zero is valid,
including at EOF. Requests extending beyond EOF fail without returning a digest.

Quote paths containing spaces. Within quotes, escape quotes and backslashes
with a backslash, as for [PWRITE](pwrite.md). Discover support via `SITE SHA3`
in FEAT. A successful reply is:

```text
213 SHA3-256 <offset> <length> <64 lowercase hexadecimal characters>
```

Errors: `501` malformed arguments, `551` range beyond EOF, `550` inaccessible
or non-regular file, `450` detected file change during hashing. Symlink targets,
directories and FIFOs are rejected. The command does not alter file contents,
REST offsets, TYPE, MODE or the data connection. It reads through a fixed 64 KiB
buffer plus a small incremental hash state; no complete-file allocation occurs.

## Package finalization order

1. Finish the sequential mount upload to `game.pkg.part`.
2. Apply all FIH/PFS/CNT patches using PWRITE.
3. Send `SITE SHA3 "game.pkg.part" 0 <mount-length>` and validate the reply's
   algorithm, offset, length and digest encoding.
4. Build supplementary SI metadata using the returned digest and append it
   using APPE. SI is excluded from the hashed mount interval.
5. Publish only after all required finalization checks succeed.

No second source read, decompression, compression or upload is needed for this
hash. The server must read the destination interval once. FTP client response
timeouts must accommodate that read/hash operation. The command runs on its
session worker; disconnect the control connection to cancel. Other commands on
that session, including ABOR, wait until hashing ends. Other sessions continue.

The caller must prevent concurrent writes/renames. Size, timestamps and path
identity are checked before/after reading, but this is not an atomic snapshot;
equal-size writes with unchanged timestamp granularity may escape detection.
Filesystem parent-path resolution follows the server's existing FTP model.

The portable implementation uses the SHA3-256 rate and padding in
[NIST FIPS 202](https://csrc.nist.gov/pubs/fips/202/final). Host regressions compare
boundary vectors and ranges against Python hashlib, and exercise PWRITE/hash/APPE,
large sparse offsets, malformed arguments and special-file rejection. Console
runtime verification remains separate from host tests and SDK compilation.

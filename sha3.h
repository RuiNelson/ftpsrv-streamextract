/* SHA3-256 (FIPS 202), incremental byte input. GPL-3.0-or-later. */
#ifndef FTP_SHA3_H
#define FTP_SHA3_H
#include <stddef.h>
#include <stdint.h>
typedef struct { uint64_t lanes[25]; size_t position; } ftp_sha3_ctx;
void ftp_sha3_init(ftp_sha3_ctx *ctx);
void ftp_sha3_update(ftp_sha3_ctx *ctx, const void *bytes, size_t length);
void ftp_sha3_final(ftp_sha3_ctx *ctx, unsigned char digest[32]);
#endif

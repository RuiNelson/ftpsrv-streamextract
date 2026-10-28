/* SHA3-256 (FIPS 202), GPL-3.0-or-later.
 * Byte-to-lane conversion is explicit and independent of host byte order.
 * SHA-3 domain suffix is 0x06 (not legacy Keccak's 0x01).
 */
#include "sha3.h"
#include <string.h>

static uint64_t
rotate(uint64_t value, unsigned bits) {
  return bits ? (value << bits) | (value >> (64 - bits)) : value;
}

static void
permute(uint64_t *a) {
  static const uint64_t rc[24] = {
    0x0000000000000001ULL, 0x0000000000008082ULL, 0x800000000000808aULL,
    0x8000000080008000ULL, 0x000000000000808bULL, 0x0000000080000001ULL,
    0x8000000080008081ULL, 0x8000000000008009ULL, 0x000000000000008aULL,
    0x0000000000000088ULL, 0x0000000080008009ULL, 0x000000008000000aULL,
    0x000000008000808bULL, 0x800000000000008bULL, 0x8000000000008089ULL,
    0x8000000000008003ULL, 0x8000000000008002ULL, 0x8000000000000080ULL,
    0x000000000000800aULL, 0x800000008000000aULL, 0x8000000080008081ULL,
    0x8000000000008080ULL, 0x0000000080000001ULL, 0x8000000080008008ULL
  };
  static const unsigned rotations[25] = {
    0, 1, 62, 28, 27, 36, 44, 6, 55, 20, 3, 10, 43,
    25, 39, 41, 45, 15, 21, 8, 18, 2, 61, 56, 14
  };
  uint64_t b[25], c[5], d[5];
  for(unsigned round = 0; round < 24; round++) {
    for(unsigned x = 0; x < 5; x++)
      c[x] = a[x] ^ a[x + 5] ^ a[x + 10] ^ a[x + 15] ^ a[x + 20];
    for(unsigned x = 0; x < 5; x++)
      d[x] = c[(x + 4) % 5] ^ rotate(c[(x + 1) % 5], 1);
    for(unsigned y = 0; y < 5; y++)
      for(unsigned x = 0; x < 5; x++) {
        unsigned i = x + 5 * y;
        b[y + 5 * ((2 * x + 3 * y) % 5)] = rotate(a[i] ^ d[x], rotations[i]);
      }
    for(unsigned y = 0; y < 5; y++)
      for(unsigned x = 0; x < 5; x++)
        a[x + 5 * y] = b[x + 5 * y] ^ ((~b[(x + 1) % 5 + 5 * y]) & b[(x + 2) % 5 + 5 * y]);
    a[0] ^= rc[round];
  }
}

void
ftp_sha3_init(ftp_sha3_ctx *ctx) { memset(ctx, 0, sizeof(*ctx)); }

void
ftp_sha3_update(ftp_sha3_ctx *ctx, const void *data, size_t length) {
  const unsigned char *bytes = data;
  for(size_t i = 0; i < length; i++) {
    ctx->lanes[ctx->position / 8] ^= (uint64_t)bytes[i] << (8 * (ctx->position % 8));
    if(++ctx->position == 136) { permute(ctx->lanes); ctx->position = 0; }
  }
}

void
ftp_sha3_final(ftp_sha3_ctx *ctx, unsigned char digest[32]) {
  ctx->lanes[ctx->position / 8] ^= (uint64_t)0x06 << (8 * (ctx->position % 8));
  ctx->lanes[16] ^= (uint64_t)0x80 << 56;
  permute(ctx->lanes);
  for(unsigned i = 0; i < 32; i++) digest[i] = (unsigned char)(ctx->lanes[i / 8] >> (8 * (i % 8)));
  memset(ctx, 0, sizeof(*ctx));
}

/* XXH3_64bits (seed 0) with a caller-supplied 192-byte secret: the arena checksum D3DMetal writes at
 * record+0x10 of every cache record.  Port of tools/d3dmetal_pipeline_key.py (all length paths),
 * validated against the checksums of real D3DMetal records.
 *
 *   clang -O3 -shared -o libxxh3.dylib xxh3.c
 */
#include <stdint.h>
#include <stddef.h>
#include <string.h>

#define P32_1 0x9E3779B1ULL
#define P32_2 0x85EBCA77ULL
#define P32_3 0xC2B2AE3DULL
#define P64_1 0x9E3779B185EBCA87ULL
#define P64_2 0xC2B2AE3D27D4EB4FULL
#define P64_3 0x165667B19E3779F9ULL
#define P64_4 0x85EBCA77C2B2AE63ULL
#define P64_5 0x27D4EB2F165667C5ULL
#define SECRET_LEN 192
#define STRIPE 64

static inline uint64_t r64(const uint8_t *p) { uint64_t v; memcpy(&v, p, 8); return v; }
static inline uint32_t r32(const uint8_t *p) { uint32_t v; memcpy(&v, p, 4); return v; }
static inline uint64_t fold(uint64_t a, uint64_t b) {
    __uint128_t p = (__uint128_t)a * b;
    return (uint64_t)p ^ (uint64_t)(p >> 64);
}
static inline uint64_t avalanche64(uint64_t h) {
    h ^= h >> 33; h *= P64_2; h ^= h >> 29; h *= P64_3; h ^= h >> 32; return h;
}
static inline uint64_t avalanche3(uint64_t h) {
    h ^= h >> 37; h *= 0x165667919E3779F9ULL; h ^= h >> 32; return h;
}
static inline uint64_t rotl(uint64_t x, int r) { return (x << r) | (x >> (64 - r)); }
static inline uint64_t rrmxmx(uint64_t h, uint64_t len) {
    h ^= rotl(h, 49) ^ rotl(h, 24);
    h *= 0x9FB21C651E98DF25ULL;
    h ^= (h >> 35) + len;
    h *= 0x9FB21C651E98DF25ULL;
    h ^= h >> 28;
    return h;
}
static inline uint64_t mix16(const uint8_t *d, const uint8_t *s) {
    return fold(r64(d) ^ r64(s), r64(d + 8) ^ r64(s + 8));
}
static inline void accumulate512(uint64_t *acc, const uint8_t *in, const uint8_t *sec) {
    for (int i = 0; i < 8; i++) {
        uint64_t dv = r64(in + 8 * i), dk = dv ^ r64(sec + 8 * i);
        acc[i ^ 1] += dv;
        acc[i] += (dk & 0xFFFFFFFFULL) * (dk >> 32);
    }
}
static inline void scramble(uint64_t *acc, const uint8_t *sec) {
    for (int i = 0; i < 8; i++) {
        uint64_t x = acc[i];
        x ^= x >> 47;
        x ^= r64(sec + 8 * i);
        acc[i] = x * P32_1;
    }
}

uint64_t xxh3_64(const uint8_t *d, size_t n, const uint8_t *s) {
    if (n == 0) return avalanche64(r64(s + 56) ^ r64(s + 64));
    if (n <= 3) {
        uint32_t c1 = d[0], c2 = d[n >> 1], c3 = d[n - 1];
        uint32_t comb = (c1 << 16) | (c2 << 24) | c3 | ((uint32_t)n << 8);
        return avalanche64((uint64_t)comb ^ (uint64_t)(r32(s) ^ r32(s + 4)));
    }
    if (n <= 8) {
        uint64_t inp = (uint64_t)r32(d + n - 4) | ((uint64_t)r32(d) << 32);
        return rrmxmx(inp ^ (r64(s + 8) ^ r64(s + 16)), n);
    }
    if (n <= 16) {
        uint64_t il = r64(d) ^ (r64(s + 24) ^ r64(s + 32));
        uint64_t ih = r64(d + n - 8) ^ (r64(s + 40) ^ r64(s + 48));
        return avalanche3(n + __builtin_bswap64(il) + ih + fold(il, ih));
    }
    if (n <= 128) {
        uint64_t acc = n * P64_1;
        if (n > 32) {
            if (n > 64) {
                if (n > 96) { acc += mix16(d + 48, s + 96); acc += mix16(d + n - 64, s + 112); }
                acc += mix16(d + 32, s + 64); acc += mix16(d + n - 48, s + 80);
            }
            acc += mix16(d + 16, s + 32); acc += mix16(d + n - 32, s + 48);
        }
        acc += mix16(d, s); acc += mix16(d + n - 16, s + 16);
        return avalanche3(acc);
    }
    if (n <= 240) {
        uint64_t acc = n * P64_1;
        int rounds = (int)(n / 16);
        for (int i = 0; i < 8; i++) acc += mix16(d + 16 * i, s + 16 * i);
        acc = avalanche3(acc);
        for (int i = 8; i < rounds; i++) acc += mix16(d + 16 * i, s + 16 * (i - 8) + 3);
        acc += mix16(d + n - 16, s + 136 - 17);
        return avalanche3(acc);
    }
    uint64_t acc[8] = {P32_3, P64_1, P64_2, P64_3, P64_4, P32_2, P64_5, P32_1};
    const size_t stripes_per_block = (SECRET_LEN - STRIPE) / 8, block_len = stripes_per_block * STRIPE;
    const size_t blocks = (n - 1) / block_len;
    for (size_t b = 0; b < blocks; b++) {
        for (size_t st = 0; st < stripes_per_block; st++)
            accumulate512(acc, d + b * block_len + st * STRIPE, s + st * 8);
        scramble(acc, s + SECRET_LEN - STRIPE);
    }
    const size_t stripes = ((n - 1) - block_len * blocks) / STRIPE;
    for (size_t st = 0; st < stripes; st++)
        accumulate512(acc, d + blocks * block_len + st * STRIPE, s + st * 8);
    accumulate512(acc, d + n - STRIPE, s + SECRET_LEN - STRIPE - 7);
    uint64_t r = n * P64_1;
    for (int i = 0; i < 4; i++)
        r += fold(acc[2 * i] ^ r64(s + 11 + 16 * i), acc[2 * i + 1] ^ r64(s + 11 + 16 * i + 8));
    return avalanche3(r);
}

static inline uint64_t merge_accs(const uint64_t *acc, const uint8_t *sec, uint64_t start) {
    uint64_t r = start;
    for (int i = 0; i < 4; i++)
        r += fold(acc[2 * i] ^ r64(sec + 16 * i), acc[2 * i + 1] ^ r64(sec + 16 * i + 8));
    return avalanche3(r);
}

/* XXH3-128 long path (n > 240), seed 0.  out = {low64, high64}.  Returns -1 for n <= 240. */
int xxh3_128_long(const uint8_t *d, size_t n, const uint8_t *s, uint64_t *out) {
    if (n <= 240) return -1;
    uint64_t acc[8] = {P32_3, P64_1, P64_2, P64_3, P64_4, P32_2, P64_5, P32_1};
    const size_t stripes_per_block = (SECRET_LEN - STRIPE) / 8, block_len = stripes_per_block * STRIPE;
    const size_t blocks = (n - 1) / block_len;
    for (size_t b = 0; b < blocks; b++) {
        for (size_t st = 0; st < stripes_per_block; st++)
            accumulate512(acc, d + b * block_len + st * STRIPE, s + st * 8);
        scramble(acc, s + SECRET_LEN - STRIPE);
    }
    const size_t stripes = ((n - 1) - block_len * blocks) / STRIPE;
    for (size_t st = 0; st < stripes; st++)
        accumulate512(acc, d + blocks * block_len + st * STRIPE, s + st * 8);
    accumulate512(acc, d + n - STRIPE, s + SECRET_LEN - STRIPE - 7);
    out[0] = merge_accs(acc, s + 11, n * P64_1);
    out[1] = merge_accs(acc, s + SECRET_LEN - STRIPE - 11, ~(n * P64_2));
    return 0;
}

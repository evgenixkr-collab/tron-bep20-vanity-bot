/*
 * ethvanitygen.c - BEP-20/Ethereum vanity address generator
 *
 * Uses OpenSSL for secp256k1 key generation and
 * Keccak-256 (sha3.c from vanitygen++) for address derivation.
 *
 * Output format (same as vanitygen++ so the Telegram bot can reuse parsers):
 *   Progress (stderr, \r-terminated): [speed][total N][Prob X%][50% in Y]
 *   Result   (stdout, \n-terminated): ETH Address: 0x...\nETH Privkey: ...\n
 *
 * Usage: ./ethvanitygen <hex_prefix>
 *   e.g.  ./ethvanitygen DEAD    -> finds 0xDEAD...
 *         ./ethvanitygen 0xCAFE  -> finds 0xCAFE...
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <pthread.h>
#include <stdatomic.h>
#include <time.h>
#include <ctype.h>
#include <signal.h>
#include <math.h>
#include <unistd.h>

#include <openssl/ec.h>
#include <openssl/bn.h>
#include <openssl/rand.h>
#include <openssl/obj_mac.h>

/* sha3.h / sha3.c from vanitygen-plusplus (Keccak-256 with 0x01 padding) */
#include "sha3.h"

/* ------------------------------------------------------------------ */
/* Global state                                                         */
/* ------------------------------------------------------------------ */
static volatile int  g_found  = 0;
static volatile int  g_stop   = 0;
static _Atomic uint64_t g_total = 0;
static pthread_mutex_t  g_result_mutex = PTHREAD_MUTEX_INITIALIZER;

/* Stored result */
static char g_result_address[43]; /* "0x" + 40 hex + '\0' */
static char g_result_privkey[65]; /* 64 hex + '\0' */

/* Search target (lowercase hex) */
static char g_prefix[41];
static int  g_prefix_len = 0;

/* Number of worker threads */
static int g_num_threads = 4;

/* ------------------------------------------------------------------ */
/* Signal handler                                                       */
/* ------------------------------------------------------------------ */
static void signal_handler(int sig) {
    (void)sig;
    g_stop = 1;
}

/* ------------------------------------------------------------------ */
/* Worker thread: generates keys, computes addresses, checks prefix    */
/* ------------------------------------------------------------------ */
static void *worker_thread(void *arg) {
    (void)arg;

    /* Each thread owns its own EC objects for thread safety */
    EC_GROUP *group = EC_GROUP_new_by_curve_name(NID_secp256k1);
    EC_KEY   *key   = EC_KEY_new();
    EC_KEY_set_group(key, group);
    BN_CTX   *ctx   = BN_CTX_new();

    uint8_t pub_bytes[65];  /* uncompressed public key */
    uint8_t keccak[32];     /* Keccak-256 hash */
    char    addr_hex[41];   /* 40 lowercase hex chars + '\0' */
    static const char hex_chars[] = "0123456789abcdef";

    while (!g_found && !g_stop) {
        /* 1. Generate random private key */
        if (!EC_KEY_generate_key(key))
            continue;

        /* 2. Derive uncompressed public key */
        const EC_POINT *pub_point = EC_KEY_get0_public_key(key);
        size_t pub_len = EC_POINT_point2oct(group, pub_point,
                            POINT_CONVERSION_UNCOMPRESSED,
                            pub_bytes, sizeof(pub_bytes), ctx);
        if (pub_len != 65)
            continue;

        /* 3. Keccak-256 of the 64-byte public key (skip 0x04 prefix) */
        sha3_256(keccak, 32, pub_bytes + 1, 64);

        /* 4. Ethereum address = last 20 bytes → lowercase hex */
        const uint8_t *addr_bytes = keccak + 12;
        for (int i = 0; i < 20; i++) {
            addr_hex[i * 2]     = hex_chars[(addr_bytes[i] >> 4) & 0x0f];
            addr_hex[i * 2 + 1] = hex_chars[ addr_bytes[i]       & 0x0f];
        }
        addr_hex[40] = '\0';

        /* 5. Compare prefix (case-insensitive) */
        int match = 1;
        for (int i = 0; i < g_prefix_len && match; i++) {
            if (addr_hex[i] != g_prefix[i])
                match = 0;
        }

        if (match) {
            pthread_mutex_lock(&g_result_mutex);
            if (!g_found) {
                g_found = 1;

                /* Format full address */
                g_result_address[0] = '0';
                g_result_address[1] = 'x';
                memcpy(g_result_address + 2, addr_hex, 40);
                g_result_address[42] = '\0';

                /* Format private key (64 hex chars, zero-padded) */
                const BIGNUM *priv = EC_KEY_get0_private_key(key);
                char *hex = BN_bn2hex(priv);
                int hexlen = (int)strlen(hex);
                memset(g_result_privkey, '0', 64);
                if (hexlen <= 64)
                    memcpy(g_result_privkey + 64 - hexlen, hex, hexlen);
                g_result_privkey[64] = '\0';
                OPENSSL_free(hex);
            }
            pthread_mutex_unlock(&g_result_mutex);
        }

        atomic_fetch_add(&g_total, 1);
    }

    BN_CTX_free(ctx);
    EC_KEY_free(key);
    EC_GROUP_free(group);
    return NULL;
}

/* ------------------------------------------------------------------ */
/* Stats thread: prints progress every second to stderr                 */
/* ------------------------------------------------------------------ */
static void *stats_thread(void *arg) {
    (void)arg;

    struct timespec prev, curr;
    clock_gettime(CLOCK_MONOTONIC, &prev);
    uint64_t prev_total = 0;

    /* Difficulty: 16^prefix_len possibilities */
    double difficulty = pow(16.0, (double)g_prefix_len);

    while (!g_found && !g_stop) {
        usleep(1000000); /* 1 second */

        clock_gettime(CLOCK_MONOTONIC, &curr);
        uint64_t cur_total = atomic_load(&g_total);

        double elapsed = (double)(curr.tv_sec - prev.tv_sec)
                       + (double)(curr.tv_nsec - prev.tv_nsec) / 1e9;
        if (elapsed <= 0) elapsed = 1.0;

        double speed = (double)(cur_total - prev_total) / elapsed;

        /* Format speed */
        char speed_str[32];
        if (speed >= 1e6)
            snprintf(speed_str, sizeof(speed_str), "%.2f Mkey/s", speed / 1e6);
        else if (speed >= 1e3)
            snprintf(speed_str, sizeof(speed_str), "%.2f Kkey/s", speed / 1e3);
        else
            snprintf(speed_str, sizeof(speed_str), "%.0f key/s", speed);

        /* 50% probability ETA */
        double p50_sec = (speed > 0) ? (difficulty * log(2.0) / speed) : 1e9;
        char eta_str[32];
        if (p50_sec < 60)
            snprintf(eta_str, sizeof(eta_str), "%.1fsec", p50_sec);
        else if (p50_sec < 3600)
            snprintf(eta_str, sizeof(eta_str), "%.1fmin", p50_sec / 60.0);
        else
            snprintf(eta_str, sizeof(eta_str), "%.1fhrs", p50_sec / 3600.0);

        /* Cumulative probability */
        double prob = (1.0 - pow(1.0 - 1.0 / difficulty, (double)cur_total)) * 100.0;
        if (prob > 99.99) prob = 99.99;

        /* Output progress in same format as vanitygen++ */
        fprintf(stderr, "\r[%s][total %lu][Prob %.1f%%][50%% in %s]",
                speed_str, (unsigned long)cur_total, prob, eta_str);
        fflush(stderr);

        prev = curr;
        prev_total = cur_total;
    }

    return NULL;
}

/* ------------------------------------------------------------------ */
/* main                                                                 */
/* ------------------------------------------------------------------ */
int main(int argc, char *argv[]) {
    if (argc < 2) {
        fprintf(stderr,
            "Usage: %s <hex_prefix>\n"
            "Example: %s DEAD   (finds 0xDEAD...)\n",
            argv[0], argv[0]);
        return 1;
    }

    const char *raw = argv[1];
    /* Strip optional 0x / 0X prefix */
    if ((raw[0] == '0') && (raw[1] == 'x' || raw[1] == 'X'))
        raw += 2;

    g_prefix_len = (int)strlen(raw);
    if (g_prefix_len == 0 || g_prefix_len > 40) {
        fprintf(stderr, "Prefix must be 1..40 hex characters\n");
        return 1;
    }

    /* Validate and normalise to lowercase */
    for (int i = 0; i < g_prefix_len; i++) {
        char c = tolower((unsigned char)raw[i]);
        if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) {
            fprintf(stderr, "Invalid hex character: '%c'\n", raw[i]);
            return 1;
        }
        g_prefix[i] = c;
    }
    g_prefix[g_prefix_len] = '\0';

    /* Thread count: from env NPROC, else nproc, else 4 */
    {
        char *env_nproc = getenv("NPROC");
        if (env_nproc) {
            g_num_threads = atoi(env_nproc);
        } else {
            long nproc = sysconf(_SC_NPROCESSORS_ONLN);
            g_num_threads = (nproc > 0) ? (int)nproc : 4;
        }
        if (g_num_threads < 1) g_num_threads = 1;
        if (g_num_threads > 32) g_num_threads = 32;
    }

    signal(SIGINT,  signal_handler);
    signal(SIGTERM, signal_handler);

    fprintf(stderr, "Searching for BEP-20/ETH address with prefix 0x%s (%d threads)\n",
            g_prefix, g_num_threads);
    fflush(stderr);

    /* Start stats thread */
    pthread_t stats_tid;
    pthread_create(&stats_tid, NULL, stats_thread, NULL);

    /* Start worker threads */
    pthread_t *workers = malloc(g_num_threads * sizeof(pthread_t));
    for (int i = 0; i < g_num_threads; i++)
        pthread_create(&workers[i], NULL, worker_thread, NULL);

    /* Wait for workers */
    for (int i = 0; i < g_num_threads; i++)
        pthread_join(workers[i], NULL);
    free(workers);

    /* Stop stats thread */
    g_stop = 1;
    pthread_join(stats_tid, NULL);

    fprintf(stderr, "\n");
    fflush(stderr);

    if (g_found) {
        printf("ETH Address: %s\n", g_result_address);
        printf("ETH Privkey: %s\n", g_result_privkey);
        fflush(stdout);
        return 0;
    }

    return 1;
}

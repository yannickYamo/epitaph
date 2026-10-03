/* The ESP32 port on a laptop.
 *
 *   test_host logits N    greedy N tokens from <s>, printing each token and its logits
 *   test_host life        one whole life on a simulated ESP32: a virtual clock that runs
 *                         at the chip's speed, and a 300 KB heap the machine can take */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "../epitaph_esp32/epitaph_tiny.h"

/* ---- a simulated ESP32 ---- */

#define CHIP_SLOWDOWN 25.0  /* an ESP32 at 240 MHz is about this much slower than a laptop core */
#define HEAP_BYTES (300 * 1024)  /* a plain ESP32's free heap with Wi-Fi off */

static double virtual_ms, last_real_ms;
static int mhz = 240, light;
static size_t heap_used;
static uint32_t rng = 0x2545F491u;

static double real_ms(void)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec * 1000.0 + t.tv_nsec / 1e6;
}

static uint32_t sim_millis(void)
{
    double now = real_ms();
    virtual_ms += (now - last_real_ms) * CHIP_SLOWDOWN * 240.0 / mhz;
    last_real_ms = now;
    return (uint32_t)virtual_ms;
}

static void sim_sleep(uint32_t ms) { sim_millis(); virtual_ms += ms; }
static void sim_write(const char *text) { fputs(text, stdout); fflush(stdout); }

static int sim_clock(int to)
{
    if (to != mhz) printf("\n        [clock %d -> %d MHz at %.0f s]\n", mhz, to, virtual_ms / 1000);
    sim_millis();
    mhz = to;
    return 1;
}

static int sim_light(int on)
{
    if (on != light) printf("\n        [light %s at %.0f s]\n", on ? "on" : "off", virtual_ms / 1000);
    light = on;
    return 1;
}

static uint32_t sim_random(void)
{
    rng ^= rng << 13;
    rng ^= rng >> 17;
    rng ^= rng << 5;
    return rng;
}

typedef struct { size_t n; } block;

static void *sim_alloc(size_t n)
{
    if (heap_used + n + sizeof(block) > HEAP_BYTES) return NULL;
    block *b = malloc(sizeof(block) + n);
    if (!b) return NULL;
    b->n = n + sizeof(block);
    heap_used += b->n;
    return b + 1;
}

static void sim_release(void *p)
{
    if (!p) return;
    block *b = (block *)p - 1;
    heap_used -= b->n;
    free(b);
}

int main(int argc, char **argv)
{
    if (argc >= 2 && strcmp(argv[1], "logits") == 0) {
        int n = argc >= 3 ? atoi(argv[2]) : 20, tok = 1;
        ep_test_reset();
        for (int i = 0; i < n; i++) {
            const float *l = ep_test_forward(tok, 0);
            int best = 0;
            printf("%d", tok);
            for (int t = 0; t < 512; t++) {
                printf(" %.5f", l[t]);
                if (l[t] > l[best]) best = t;
            }
            printf("\n");
            tok = best;
        }
        return 0;
    }
    ep_platform p = {sim_millis, sim_sleep, sim_write, sim_clock, sim_light,
                     sim_random, sim_alloc, sim_release};
    ep_config cfg;
    ep_default_config(&cfg);
    cfg.silence_s = 0;
    last_real_ms = real_ms();
    if (!ep_init(&p)) {
        printf("the memory window does not fit in %d bytes\n", HEAP_BYTES);
        return 1;
    }
    printf("heap: %d bytes, %zu taken by the memory window (%d positions)\n\n", HEAP_BYTES,
           heap_used, EP_SEQ);
    float lived = ep_live(&p, &cfg, 1);
    printf("\n[died at %.0f s of a %d s life; heap in use at the end: %zu bytes]\n", lived,
           cfg.life_s, heap_used);
    return lived > cfg.life_s * 0.9 ? 0 : 1;
}

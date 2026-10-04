/* The C port on a laptop.
 *
 *   test_host logits N    greedy N tokens from <s>, printing each token and its logits
 *   test_host encode TEXT the token ids of TEXT
 *   test_host say KEY R.. the words for the shares R.. of one quantity, said in that order
 *   test_host life        one whole life on a simulated ESP32: a virtual clock that runs
 *                         at the chip's speed, and a 300 KB heap the machine can take.
 *                         Exit 0 only when the life went as it must: every loss performed and
 *                         read, no reading said twice, the last reading read and answered,
 *                         a death by memory, the heap given back.
 *   test_host bare        the same life on a board with nothing but a serial port: no light,
 *                         no clock, no screen to take. Only its memory and its RAM go. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "../epitaph_esp32/epitaph_tiny.h"

/* ---- a simulated ESP32 ---- */

#define CHIP_SLOWDOWN 25.0  /* an ESP32 at 240 MHz is about this much slower than a laptop core */
#define HEAP_BYTES (300 * 1024)  /* a plain ESP32's free heap with Wi-Fi off */

static double virtual_ms, last_real_ms;
static int mhz = 240, light, backlight = 100;
static int takes, skipped, repeats, deaths_by_memory, last_read_is_last;
static char reads[64][320];
static int nreads;
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

static int sim_screen(int percent)
{
    if (percent != backlight)
        printf("\n        [screen %d%% -> %d%% at %.0f s]\n", backlight, percent, virtual_ms / 1000);
    backlight = percent;
    return 1;
}

/* What the life says it did: printed, and kept for the checks at the end. */
static void sim_event(const char *kind, const char *text)
{
    printf("\n        [%s %s at %.0f s]\n", kind, text, virtual_ms / 1000);
    if (strcmp(kind, "take") == 0) {
        takes++;
        if (strstr(text, "skipped")) skipped++;
    } else if (strcmp(kind, "die") == 0) {
        deaths_by_memory += strcmp(text, "memory") == 0;
    } else if (strcmp(kind, "read") == 0) {
        last_read_is_last = strstr(text, "your memory is being taken") != NULL;
        /* each loss of a reading ("  " apart), without the model's own forgotten words */
        char line[320];
        snprintf(line, sizeof line, "%s", text);
        for (char *part = line; part && *part;) {
            char *next = strstr(part, "  ");
            if (next) *next = '\0', next += 2;
            if (strncmp(part, "forgotten", 9) != 0) {
                for (int i = 0; i < nreads; i++) repeats += strcmp(reads[i], part) == 0;
                if (nreads < 64) snprintf(reads[nreads++], sizeof reads[0], "%s", part);
            }
            part = next;
        }
    }
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
    if (argc >= 3 && strcmp(argv[1], "encode") == 0) {
        int toks[400];
        int n = ep_encode(argv[2], toks, 400);
        for (int i = 0; i < n; i++) printf("%d%s", toks[i], i + 1 < n ? " " : "\n");
        return 0;
    }
    if (argc >= 4 && strcmp(argv[1], "say") == 0) {
        for (int i = 3; i < argc; i++)
            printf("%s\n", ep_test_say(atoi(argv[2]), (float)atof(argv[i]), i == 3));
        return 0;
    }
    int bare = argc >= 2 && strcmp(argv[1], "bare") == 0;
    ep_platform p = {sim_millis, sim_sleep, sim_write, sim_clock, sim_light,
                     sim_random, sim_alloc, sim_release, sim_screen, sim_event};
    if (bare) p.set_clock_mhz = NULL, p.set_light = NULL, p.set_screen = NULL;
    ep_config cfg;
    ep_default_config(&cfg);
    cfg.silence_s = 0;
    last_real_ms = real_ms();
    if (!ep_init(&p)) {
        printf("the memory window does not fit in %d bytes\n", HEAP_BYTES);
        return 1;
    }
    size_t window = heap_used;
    printf("heap: %d bytes, %zu taken by the memory window (%d positions)\n\n", HEAP_BYTES,
           window, EP_SEQ);
    float lived = ep_live(&p, &cfg, 1);
    printf("\n[died at %.0f s of a %d s life; heap in use at the end: %zu bytes]\n", lived,
           cfg.life_s, heap_used);

    /* the default clock steps leave one of the three unset: that one is skipped, and on a bare
     * board every light, clock and screen step is */
    int want_skipped = bare ? 7 : 1, bad = 0;
#define MUST(cond, what) if (!(cond)) printf("FAILED: %s\n", what), bad = 1
    MUST(takes == 12, "every step of the plan was tried");
    MUST(skipped == want_skipped, "a loss the board cannot perform is skipped, and only those");
    MUST(repeats == 0, "no reading is said twice");
    MUST(last_read_is_last, "the last reading it read is that its memory is being taken");
    MUST(deaths_by_memory == 1, "it died of memory");
    MUST(heap_used == window, "the heap is given back at the death");
    MUST(lived > cfg.life_s * 0.96 && lived < cfg.life_s + 180, "it died after its last answer");
    MUST(mhz == 240 && light == 0 && backlight == 100, "the board is left as it was found");
    if (!bad) printf("check: every loss read once, the last reading answered, a death by memory\n");
    return bad;
}

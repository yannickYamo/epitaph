/* epitaph on a microcontroller: the 260K-parameter model and its life, in portable C.
 *
 * The weights are int8 in flash (model_data.h); RAM holds only the memory window (the KV
 * cache) and a few kilobytes of scratch, which is allocated per thought so the death is
 * real: when the machine takes the heap, the next thought cannot be allocated.
 *
 * The platform (the ESP32 sketch, or the laptop test) supplies the hooks below. */
#pragma once
#include <stddef.h>
#include <stdint.h>

#ifndef EP_SEQ
#define EP_SEQ 128 /* positions it can hold at birth; 128 fits a plain ESP32's heap */
#endif

typedef struct {
    uint32_t (*millis)(void);
    void (*sleep_ms)(uint32_t ms);
    void (*write)(const char *text);   /* the screen: a serial terminal, a display */
    int (*set_clock_mhz)(int mhz);     /* returns 0 when the chip cannot do it */
    int (*set_light)(int on);          /* returns 0 when there is no light to take */
    uint32_t (*random32)(void);
    void *(*alloc)(size_t n);          /* the heap the machine takes at the end */
    void (*release)(void *ptr);
} ep_platform;

typedef struct {
    int life_s;        /* how long a life lasts before its RAM is taken */
    int silence_s;     /* dark between lives */
    int full_mhz;      /* its clock at birth */
    int clocks[3];     /* the clock steps it is taken down through */
} ep_config;

#ifdef __cplusplus
extern "C" {
#endif

/* Allocate the memory window (once, at boot); 0 when the heap is too small. */
int ep_init(const ep_platform *p);
void ep_default_config(ep_config *cfg);

/* Live one life, from birth to the death; returns the seconds it lived. */
float ep_live(const ep_platform *p, const ep_config *cfg, int life_number);

/* For tests: the logits for `token` at the next position (window 0 = everything). */
const float *ep_test_forward(int token, int window);
void ep_test_reset(void);
int ep_encode(const char *text, int *out, int max);

#ifdef __cplusplus
}
#endif

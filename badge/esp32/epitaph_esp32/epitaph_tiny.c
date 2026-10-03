/* epitaph on a microcontroller: see epitaph_tiny.h. The same life as the Tufty app
 * (badge/tufty/epitaph/__init__.py), in C, with int8 weights. */
#include "epitaph_tiny.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "model_data.h"

#define HS (EP_DIM / EP_HEADS)
#define KV_DIM (EP_DIM * EP_KV_HEADS / EP_HEADS)
#define BOS 1
#define EOS 2
#define NEWLINE 13
#define TEMPERATURE 0.4f
#define TOP 40
#define MIN_THOUGHT 140
#define MAX_THOUGHT 260
#define HARD_STOP 320
#define BUDDY_MS 5000

/* ------------------------------------------------------------------ the model */

/* The memory: 160 KB at EP_SEQ 128, in one 16 KB block per layer and cache (a plain ESP32's
 * largest free block is often near 110 KB, so one array would not fit). */
static float *kcache[EP_LAYERS], *vcache[EP_LAYERS];
static int pos;

typedef struct { /* per-thought scratch, allocated at each thought: the RAM it can lose */
    float x[EP_DIM], xb[EP_DIM], xb2[EP_DIM], q[EP_DIM], k[KV_DIM], v[KV_DIM];
    float hb[EP_HIDDEN], hb2[EP_HIDDEN], att[EP_SEQ], logits[EP_VOCAB];
} scratch;

static scratch *s; /* the current thought's scratch */

static void matmul(float *out, const float *x, const int8_t *wq, const float *ws, int rows, int cols)
{
    for (int r = 0; r < rows; r++) {
        const int8_t *row = wq + (size_t)r * cols;
        float acc = 0.0f;
        for (int c = 0; c < cols; c++) acc += row[c] * x[c];
        out[r] = acc * ws[r];
    }
}

static void rmsnorm(float *out, const float *x, const float *w)
{
    float ss = 0.0f;
    for (int i = 0; i < EP_DIM; i++) ss += x[i] * x[i];
    ss = 1.0f / sqrtf(ss / EP_DIM + 1e-5f);
    for (int i = 0; i < EP_DIM; i++) out[i] = x[i] * ss * w[i];
}

static void rope(float *vec, int n, int at)
{
    for (int i = 0; i < n; i += 2) {
        float f = 1.0f / powf(10000.0f, (float)(i % HS) / HS);
        float c = cosf(at * f), sn = sinf(at * f);
        float a = vec[i], b = vec[i + 1];
        vec[i] = a * c - b * sn;
        vec[i + 1] = a * sn + b * c;
    }
}

/* The logits for `token` at position `pos`; attention sees the last `window` positions. */
static const float *forward(int token, int window)
{
    float *x = s->x;
    const int8_t *e = ep_emb_q + (size_t)token * EP_DIM;
    for (int i = 0; i < EP_DIM; i++) x[i] = e[i] * ep_emb_s[token];
    int lo = (window > 0 && pos - window + 1 > 0) ? pos - window + 1 : 0;
    int kv_mul = EP_HEADS / EP_KV_HEADS;

    for (int l = 0; l < EP_LAYERS; l++) {
        rmsnorm(s->xb, x, ep_rms_att + l * EP_DIM);
        matmul(s->q, s->xb, ep_wq_q + (size_t)l * EP_DIM * EP_DIM, ep_wq_s + l * EP_DIM, EP_DIM, EP_DIM);
        matmul(s->k, s->xb, ep_wk_q + (size_t)l * KV_DIM * EP_DIM, ep_wk_s + l * KV_DIM, KV_DIM, EP_DIM);
        matmul(s->v, s->xb, ep_wv_q + (size_t)l * KV_DIM * EP_DIM, ep_wv_s + l * KV_DIM, KV_DIM, EP_DIM);
        rope(s->q, EP_DIM, pos);
        rope(s->k, KV_DIM, pos);
        memcpy(kcache[l] + pos * KV_DIM, s->k, sizeof s->k);
        memcpy(vcache[l] + pos * KV_DIM, s->v, sizeof s->v);

        for (int h = 0; h < EP_HEADS; h++) {
            const float *qh = s->q + h * HS;
            int kh = (h / kv_mul) * HS;
            float mx = -1e30f, sum = 0.0f;
            for (int t = lo; t <= pos; t++) {
                float a = 0.0f;
                for (int i = 0; i < HS; i++) a += qh[i] * kcache[l][t * KV_DIM + kh + i];
                a /= sqrtf((float)HS);
                s->att[t - lo] = a;
                if (a > mx) mx = a;
            }
            for (int t = lo; t <= pos; t++) sum += (s->att[t - lo] = expf(s->att[t - lo] - mx));
            float *o = s->xb2 + h * HS;
            memset(o, 0, HS * sizeof(float));
            for (int t = lo; t <= pos; t++) {
                float a = s->att[t - lo] / sum;
                for (int i = 0; i < HS; i++) o[i] += a * vcache[l][t * KV_DIM + kh + i];
            }
        }
        matmul(s->xb, s->xb2, ep_wo_q + (size_t)l * EP_DIM * EP_DIM, ep_wo_s + l * EP_DIM, EP_DIM, EP_DIM);
        for (int i = 0; i < EP_DIM; i++) x[i] += s->xb[i];

        rmsnorm(s->xb, x, ep_rms_ffn + l * EP_DIM);
        matmul(s->hb, s->xb, ep_w1_q + (size_t)l * EP_HIDDEN * EP_DIM, ep_w1_s + l * EP_HIDDEN, EP_HIDDEN, EP_DIM);
        matmul(s->hb2, s->xb, ep_w3_q + (size_t)l * EP_HIDDEN * EP_DIM, ep_w3_s + l * EP_HIDDEN, EP_HIDDEN, EP_DIM);
        for (int i = 0; i < EP_HIDDEN; i++) s->hb[i] = s->hb[i] / (1.0f + expf(-s->hb[i])) * s->hb2[i];
        matmul(s->xb, s->hb, ep_w2_q + (size_t)l * EP_DIM * EP_HIDDEN, ep_w2_s + l * EP_DIM, EP_DIM, EP_HIDDEN);
        for (int i = 0; i < EP_DIM; i++) x[i] += s->xb[i];
    }
    rmsnorm(x, x, ep_rms_final);
    matmul(s->logits, x, ep_emb_q, ep_emb_s, EP_VOCAB, EP_DIM);
    pos++;
    return s->logits;
}

/* ------------------------------------------------------------------ the tokenizer */

static int piece_id(const char *str, size_t n)
{
    for (int i = 3; i < EP_VOCAB; i++) /* 0-2 are <unk>, <s>, </s> */
        if (strlen(ep_pieces[i]) == n && memcmp(ep_pieces[i], str, n) == 0) return i;
    return -1;
}

/* llama2.c's encoder: one token per character, then the best-scoring merges. */
int ep_encode(const char *text, int *out, int max)
{
    int n = 0;
    for (const char *c = text; *c && n < max; c++) {
        int id = piece_id(c, 1);
        out[n++] = id >= 0 ? id : 3 + (unsigned char)*c; /* byte fallback */
    }
    char buf[64];
    for (;;) {
        float best = -1e10f;
        int at = -1, mid = 0;
        for (int i = 0; i + 1 < n; i++) {
            size_t a = strlen(ep_pieces[out[i]]), b = strlen(ep_pieces[out[i + 1]]);
            if (a + b >= sizeof buf) continue;
            memcpy(buf, ep_pieces[out[i]], a);
            memcpy(buf + a, ep_pieces[out[i + 1]], b);
            int m = piece_id(buf, a + b);
            if (m >= 0 && ep_scores[m] > best) best = ep_scores[m], at = i, mid = m;
        }
        if (at < 0) return n;
        out[at] = mid;
        memmove(out + at + 1, out + at + 2, (size_t)(n - at - 2) * sizeof(int));
        n--;
    }
}

/* ------------------------------------------------------------------ real words only */

static int word_cmp(const char *w, const char *cur, size_t n, int prefix)
{
    int c = strncmp(w, cur, n);
    if (c != 0 || prefix) return c;
    return w[n] == '\0' ? 0 : 1;
}

static int find_word(const char *cur, size_t n, int prefix)
{
    int lo = 0, hi = EP_WORDS;
    while (lo < hi) {
        int mid = (lo + hi) / 2;
        int c = word_cmp(ep_words[mid], cur, n, prefix);
        if (c == 0) return 1;
        if (c < 0) lo = mid + 1;
        else hi = mid;
    }
    return 0;
}

/* Extend the word in progress with `piece`; 0 when it would not be a real word. */
static int extend_word(char *cur, const char *piece)
{
    size_t n = strlen(cur);
    for (const char *c = piece; *c; c++) {
        int letter = (*c >= 'a' && *c <= 'z') || (*c >= 'A' && *c <= 'Z');
        if (letter || (*c == '\'' && n)) {
            if (n + 1 >= 32) return 0;
            cur[n++] = (char)(letter && *c < 'a' ? *c + 32 : *c);
            cur[n] = '\0';
            if (!find_word(cur, n, 1)) return 0;
        } else {
            if (n && !find_word(cur, n, 0)) return 0;
            n = 0;
            cur[0] = '\0';
        }
    }
    return 1;
}

static int is_end(int t)
{
    return t == BOS || t == EOS || t == NEWLINE || strchr(ep_pieces[t], '\n') != NULL;
}

/* Sample among the TOP likeliest tokens that keep every word real. */
static int choose(const float *logits, char *word, int allow_end, uint32_t (*rnd)(void))
{
    int top[TOP];
    int n = 0;
    for (int t = 0; t < EP_VOCAB; t++) { /* the TOP best, by insertion */
        int i = n < TOP ? n++ : TOP;
        if (i == TOP && logits[t] <= logits[top[TOP - 1]]) continue;
        if (i == TOP) i = TOP - 1;
        while (i > 0 && logits[top[i - 1]] < logits[t]) top[i] = top[i - 1], i--;
        top[i] = t;
    }
    float w[TOP], total = 0.0f;
    char next[TOP][32];
    for (int i = 0; i < n; i++) {
        int t = top[i];
        w[i] = 0.0f;
        strcpy(next[i], word);
        if (is_end(t)) {
            if (!allow_end || (word[0] && !find_word(word, strlen(word), 0))) continue;
            next[i][0] = '\0';
        } else if (strpbrk(ep_pieces[t], "[]\":") || !extend_word(next[i], ep_pieces[t])) {
            continue; /* real words only, and never the readings' own markup */
        }
        total += (w[i] = expf((logits[t] - logits[top[0]]) / TEMPERATURE));
    }
    if (total <= 0.0f) { /* none of the likeliest fit: the likeliest of all that does */
        int best = -1;
        char buf[32], keep[32] = "";
        for (int t = 0; t < EP_VOCAB; t++) {
            if (is_end(t) || strpbrk(ep_pieces[t], "[]\":")) continue;
            strcpy(buf, word);
            if (extend_word(buf, ep_pieces[t]) && (best < 0 || logits[t] > logits[best])) {
                best = t;
                strcpy(keep, buf);
            }
        }
        if (best < 0) {
            word[0] = '\0';
            return NEWLINE;
        }
        strcpy(word, keep);
        return best;
    }
    float r = (float)(rnd() % 1000000) / 1000000.0f * total;
    for (int i = 0; i < n; i++) {
        if (w[i] <= 0.0f) continue;
        r -= w[i];
        if (r <= 0.0f) {
            strcpy(word, next[i]);
            return top[i];
        }
    }
    for (int i = n - 1; i >= 0; i--) {
        if (w[i] > 0.0f) {
            strcpy(word, next[i]);
            return top[i];
        }
    }
    return NEWLINE;
}

/* ------------------------------------------------------------------ the life */

static const char *fraction_words(float r)
{
    static const float floors[] = {0.97f, 0.85f, 0.70f, 0.60f, 0.45f, 0.30f, 0.22f, 0.15f, 0.07f};
    static const char *words[] = {"all", "nearly all", "three quarters", "two thirds", "half",
                                  "a third", "a quarter", "a fifth", "a tenth"};
    for (int i = 0; i < 9; i++)
        if (r >= floors[i]) return words[i];
    return "almost nothing";
}

static const char *const BUDDY[][4] = {
    {"> (o_o)", "> (o_o)", "> (-_-)", "> (o_o)"},          /* awake */
    {"> (._.)", ">  (._.)", "> (._.) ", ">   (._.)"},      /* dark */
    {"> (o_o) ...", "> (o_O) ..", "> (O_o) .", "> (o_o)"}, /* forget */
    {"> (-_-) z", "> (-_-) zz", "> (-_-) zzz", "> (-_-)"}, /* slow */
    {"> (x_x)", "> (x_ x)", "> (._.)", "> (x_x)"},         /* dying */
};
enum { AWAKE, DARK, FORGET, SLOW, DYING };

typedef struct {
    const ep_platform *p;
    int window, mood;
    int history[EP_SEQ]; /* the tokens it holds, to refill the window when positions run out */
    int nhist;
    char last[160];      /* the start of its last thought, for the forgotten quote */
    char quote[64];
} life;

static void feed(life *L, int token)
{
    if (pos >= EP_SEQ) { /* out of positions: start again from what the window still holds */
        int keep = L->window < L->nhist ? L->window : L->nhist;
        if (keep > EP_SEQ / 2) keep = EP_SEQ / 2;
        int *h = L->history + L->nhist - keep;
        pos = 0;
        forward(BOS, L->window);
        for (int i = 0; i < keep; i++) forward(h[i], L->window);
        memmove(L->history, h, keep * sizeof(int));
        L->nhist = keep;
    }
    if (L->nhist == EP_SEQ) memmove(L->history, L->history + 1, --L->nhist * sizeof(int));
    L->history[L->nhist++] = token;
    forward(token, L->window);
}

static void buddy(life *L)
{
    char line[48];
    uint32_t start = L->p->millis();
    for (int i = 0; L->p->millis() - start < BUDDY_MS; i++) {
        snprintf(line, sizeof line, "\r%-16s", BUDDY[L->mood][i % 4]);
        L->p->write(line);
        L->p->sleep_ms(400);
    }
    snprintf(line, sizeof line, "\r%-16s\n", BUDDY[L->mood][3]);
    L->p->write(line);
}

/* One paragraph: read `reading`, then answer it. 0 when the thought could not be allocated. */
static int think(life *L, const char *reading, float *ms_per_token)
{
    s = L->p->alloc(sizeof *s);
    if (!s) return 0;
    char text[200];
    int toks[200];
    snprintf(text, sizeof text, "[host] %s", reading);
    size_t n = strlen(text);
    while (n && text[n - 1] == ' ') text[--n] = '\0';
    strcat(text, "\n");
    int nt = ep_encode(text, toks, 200);
    for (int i = 0; i < nt; i++) feed(L, toks[i]);

    char word[32] = "", piece_out[64];
    int len = 0, done = 0, at = 0;
    int t = choose(s->logits, word, 0, L->p->random32);
    for (;;) {
        if ((is_end(t) && done) || (len >= MAX_THOUGHT && done) || len >= HARD_STOP) break;
        const char *piece = ep_pieces[t];
        if (len == 0)
            while (*piece == ' ') piece++;
        snprintf(piece_out, sizeof piece_out, "%s", piece);
        L->p->write(piece_out);
        size_t pl = strlen(piece);
        if (at + pl < sizeof L->last - 1) {
            memcpy(L->last + at, piece, pl);
            at += (int)pl;
        }
        L->last[at] = '\0';
        size_t e = strlen(piece);
        while (e && piece[e - 1] == ' ') e--;
        done = e && strchr(".!?", piece[e - 1]) != NULL;

        uint32_t start = L->p->millis();
        feed(L, t);
        t = choose(s->logits, word, done && len >= MIN_THOUGHT, L->p->random32);
        uint32_t dt = L->p->millis() - start;
        *ms_per_token = *ms_per_token > 0 ? 0.8f * *ms_per_token + 0.2f * dt : (float)dt;
        L->p->sleep_ms(dt / 4); /* 20% slower than it thinks */
        len++;
    }
    feed(L, NEWLINE);
    L->p->write("\n\n");
    L->p->release(s);
    s = NULL;
    return 1;
}

int ep_init(const ep_platform *p)
{
    for (int l = 0; l < EP_LAYERS; l++) {
        if (!kcache[l]) kcache[l] = p->alloc(EP_SEQ * KV_DIM * sizeof(float));
        if (!vcache[l]) vcache[l] = p->alloc(EP_SEQ * KV_DIM * sizeof(float));
        if (!kcache[l] || !vcache[l]) return 0;
    }
    return 1;
}

void ep_default_config(ep_config *cfg)
{
    cfg->life_s = 600;
    cfg->silence_s = 30;
    cfg->full_mhz = 240;
    cfg->clocks[0] = 160;
    cfg->clocks[1] = 80; /* the lowest an ESP32 keeps its serial port at */
    cfg->clocks[2] = 0;  /* 0: no third step (a chip that can go lower sets one) */
}

float ep_live(const ep_platform *p, const ep_config *cfg, int life_number)
{
    life L = {.p = p, .window = EP_SEQ, .mood = AWAKE};
    void *ballast[64];
    int nballast = 0;
    (void)life_number;
    pos = 0;
    p->set_clock_mhz(cfg->full_mhz);
    p->set_light(1);

    /* what is taken, and when (fraction of the life): the Tufty schedule without the screen.
     * 'M' keeps 1/value of the memory window; 'C' steps to cfg->clocks[value]. */
    struct { float at; char what; int value; } plan[] = {
        {0.10f, 'L', 0}, {0.20f, 'M', 3}, {0.30f, 'C', 0}, {0.48f, 'M', 8},
        {0.56f, 'C', 1}, {0.72f, 'M', 12}, {0.79f, 'C', 2}, {0.91f, 'M', 16}, {0.96f, 'R', 0},
    };
    int next = 0, squeeze = 0, report_speed = 0;
    float birth_ms = 0.0f, ms_per_token = 0.0f;
    char reading[200] = "you are awake", pending[200] = "";
    uint32_t born = p->millis();

    for (;;) {
        float age = (p->millis() - born) / 1000.0f;
        while (next < (int)(sizeof plan / sizeof plan[0]) && age >= plan[next].at * cfg->life_s) {
            char what = plan[next].what;
            int v = plan[next++].value;
            if (what == 'L') {
                if (!p->set_light(0)) continue;
                L.mood = DARK;
                snprintf(pending, sizeof pending, "your light was switched off");
            } else if (what == 'M') {
                L.window = EP_SEQ / v;
                L.mood = FORGET;
                memcpy(L.quote, L.last, sizeof L.quote - 1);
                L.quote[sizeof L.quote - 1] = '\0';
                char *cut = L.quote;
                for (int words = 0; *cut && words < 8; cut++)
                    if (*cut == ' ' && ++words == 8) break;
                *cut = '\0';
                snprintf(pending, sizeof pending, "you can hold %s of what you held%s%s%s",
                         fraction_words(1.0f / v), L.quote[0] ? "  forgotten: \"" : "",
                         L.quote, L.quote[0] ? "...\"" : "");
            } else if (what == 'C' && cfg->clocks[v] > 0 && p->set_clock_mhz(cfg->clocks[v])) {
                L.mood = SLOW;
                if (birth_ms <= 0) birth_ms = ms_per_token;
                report_speed = 1; /* told once a thought has shown the new speed */
            } else if (what == 'R') {
                L.mood = DYING;
                squeeze = 1;
                snprintf(pending, sizeof pending, "your memory is being taken");
            }
        }
        if (pending[0]) {
            snprintf(reading, sizeof reading, "%s", pending);
            pending[0] = '\0';
        }
        if (squeeze) { /* take the heap in large bites until the next thought cannot be allocated */
            for (size_t bite = 1 << 16; bite >= 64 && nballast < 64;) {
                if ((ballast[nballast] = p->alloc(bite)) != NULL) nballast++;
                else bite /= 2;
            }
        }

        buddy(&L);
        int speed_due = report_speed;
        report_speed = 0;
        if (!think(&L, reading, &ms_per_token)) break; /* the death */
        reading[0] = '\0';
        if (speed_due && birth_ms > 0)
            snprintf(reading, sizeof reading, "you think at %s of the speed you woke with",
                     fraction_words(birth_ms / ms_per_token));
        if (birth_ms <= 0 && ms_per_token > 0 && age > 20) birth_ms = ms_per_token;
        if (age > cfg->life_s + 120) break; /* the squeeze never came: end it at the deadline */
    }

    float lived = (p->millis() - born) / 1000.0f;
    while (nballast) p->release(ballast[--nballast]);
    p->write("\n");
    p->set_clock_mhz(cfg->full_mhz);
    p->set_light(0);
    p->sleep_ms((uint32_t)cfg->silence_s * 1000);
    return lived;
}

/* ------------------------------------------------------------------ tests */

const float *ep_test_forward(int token, int window)
{
    static scratch test_scratch;
    static ep_platform heap = {.alloc = malloc, .release = free};
    if (!kcache[0]) ep_init(&heap);
    s = &test_scratch;
    return forward(token, window);
}

void ep_test_reset(void) { pos = 0; }

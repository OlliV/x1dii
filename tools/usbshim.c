/*
 * LibUsb.framework replacement for the throwaway Phocus copy.
 *
 * HBTethered is the only image in Phocus.app that imports libusb, and it imports
 * exactly 23 symbols. We define all 23 here and forward them to the real library,
 * logging every transfer. Because we *are* the library HBTethered binds against,
 * two-level namespace binding cannot bypass us.
 */
#include <libusb.h>
#include <dlfcn.h>
#include <stdio.h>
#include <stdarg.h>
#include <string.h>
#include <pthread.h>
#include <time.h>
#include <unistd.h>

#define REALPATH "/tmp/PhocusProbe.app/Contents/Frameworks/LibUsb.framework/Versions/A/LibUsb.real"

static void *g_real;
static pthread_once_t g_once = PTHREAD_ONCE_INIT;

static void load_real(void)
{
    g_real = dlopen(REALPATH, RTLD_LAZY | RTLD_LOCAL);
    if (!g_real) {
        FILE *f = fopen("/tmp/libusb_trace.log", "a");
        if (f) { fprintf(f, "!! dlopen %s failed: %s\n", REALPATH, dlerror()); fclose(f); }
    }
}

static void *R(const char *sym)
{
    pthread_once(&g_once, load_real);
    if (!g_real) return NULL;
    return dlsym(g_real, sym);
}

/* ---------------- logging ---------------- */

static pthread_mutex_t g_loglock = PTHREAD_MUTEX_INITIALIZER;

static void logmsg(const char *fmt, ...)
{
    pthread_mutex_lock(&g_loglock);
    FILE *f = fopen("/tmp/libusb_trace.log", "a");
    if (f) {
        struct timespec ts;
        clock_gettime(CLOCK_REALTIME, &ts);
        fprintf(f, "%ld.%03ld ", (long)ts.tv_sec, ts.tv_nsec / 1000000);
        va_list ap;
        va_start(ap, fmt);
        vfprintf(f, fmt, ap);
        va_end(ap);
        fputc('\n', f);
        fclose(f);
    }
    pthread_mutex_unlock(&g_loglock);
}

static int frame_len(const unsigned char *p, int avail)
{
    /* camera framing: <type> <00> <src> <dst> <len8> <len8 bytes of payload> */
    if (avail >= 5 && (p[0] == 0x03 || p[0] == 0x04)) {
        int n = 5 + p[4];
        if (n <= avail) return n;
    }
    return avail;
}

static void hexdump(const char *tag, const unsigned char *p, int n)
{
    if (n <= 0) { logmsg("%s (empty)", tag); return; }
    n = frame_len(p, n);
    if (n > 512) n = 512;
    char line[4096];
    int o = snprintf(line, sizeof line, "%s", tag);
    for (int i = 0; i < n && o < (int)sizeof(line) - 4; i++)
        o += snprintf(line + o, sizeof(line) - o, "%02x ", p[i]);
    logmsg("%s", line);
}

/* ---------------- transfer callback wrapper ---------------- */

#define MAXCB 256
static struct { struct libusb_transfer *t; libusb_transfer_cb_fn cb; } g_cbs[MAXCB];
static pthread_mutex_t g_cblock = PTHREAD_MUTEX_INITIALIZER;

static void remember_cb(struct libusb_transfer *t, libusb_transfer_cb_fn cb)
{
    pthread_mutex_lock(&g_cblock);
    int slot = -1;
    for (int i = 0; i < MAXCB; i++) {
        if (g_cbs[i].t == t) { slot = i; break; }
        if (slot < 0 && g_cbs[i].t == NULL) slot = i;
    }
    if (slot >= 0) { g_cbs[slot].t = t; g_cbs[slot].cb = cb; }
    pthread_mutex_unlock(&g_cblock);
}

static libusb_transfer_cb_fn peek_cb(struct libusb_transfer *t)
{
    libusb_transfer_cb_fn cb = NULL;
    pthread_mutex_lock(&g_cblock);
    for (int i = 0; i < MAXCB; i++)
        if (g_cbs[i].t == t) { cb = g_cbs[i].cb; break; }
    pthread_mutex_unlock(&g_cblock);
    return cb;
}

static void forget_cb(struct libusb_transfer *t)
{
    pthread_mutex_lock(&g_cblock);
    for (int i = 0; i < MAXCB; i++)
        if (g_cbs[i].t == t) { g_cbs[i].t = NULL; g_cbs[i].cb = NULL; break; }
    pthread_mutex_unlock(&g_cblock);
}

static void our_cb(struct libusb_transfer *t)
{
    libusb_transfer_cb_fn orig = peek_cb(t);
    const char *dir = (t->endpoint & LIBUSB_ENDPOINT_IN) ? "IN " : "OUT";
    logmsg("COMPLETE %s ep=0x%02x status=%d actual=%d len=%d",
           dir, t->endpoint, t->status, t->actual_length, t->length);
    if ((t->endpoint & LIBUSB_ENDPOINT_IN) && t->status == LIBUSB_TRANSFER_COMPLETED)
        hexdump("  <- data:", t->buffer, t->actual_length);
    if (orig) orig(t);
}

/* ---------------- forwarded API ---------------- */

int libusb_init(libusb_context **ctx)
{
    int (*fn)(libusb_context **) = R("libusb_init");
    int r = fn ? fn(ctx) : -1;
    logmsg("libusb_init -> %d", r);
    return r;
}

void libusb_exit(libusb_context *ctx)
{
    void (*fn)(libusb_context *) = R("libusb_exit");
    logmsg("libusb_exit");
    if (fn) fn(ctx);
}

ssize_t libusb_get_device_list(libusb_context *ctx, libusb_device ***list)
{
    ssize_t (*fn)(libusb_context *, libusb_device ***) = R("libusb_get_device_list");
    ssize_t r = fn ? fn(ctx, list) : -1;
    logmsg("libusb_get_device_list -> %ld", (long)r);
    if (r > 0) {
        for (ssize_t i = 0; i < r; i++) {
            struct libusb_device_descriptor d;
            int (*gd)(libusb_device *, struct libusb_device_descriptor *) =
                R("libusb_get_device_descriptor");
            if (gd && gd((*list)[i], &d) == 0) {
                int (*gb)(libusb_device *) = R("libusb_get_bus_number");
                int (*ga)(libusb_device *) = R("libusb_get_device_address");
                logmsg("  dev[%ld] %04x:%04x bus=%d addr=%d", (long)i, d.idVendor, d.idProduct,
                       gb ? gb((*list)[i]) : -1, ga ? ga((*list)[i]) : -1);
            }
        }
    }
    return r;
}

void libusb_free_device_list(libusb_device **list, int unref)
{
    void (*fn)(libusb_device **, int) = R("libusb_free_device_list");
    if (fn) fn(list, unref);
}

int libusb_get_device_descriptor(libusb_device *dev, struct libusb_device_descriptor *desc)
{
    int (*fn)(libusb_device *, struct libusb_device_descriptor *) = R("libusb_get_device_descriptor");
    return fn ? fn(dev, desc) : -1;
}

int libusb_get_config_descriptor(libusb_device *dev, uint8_t idx, struct libusb_config_descriptor **cfg)
{
    int (*fn)(libusb_device *, uint8_t, struct libusb_config_descriptor **) = R("libusb_get_config_descriptor");
    return fn ? fn(dev, idx, cfg) : -1;
}

void libusb_free_config_descriptor(struct libusb_config_descriptor *cfg)
{
    void (*fn)(struct libusb_config_descriptor *) = R("libusb_free_config_descriptor");
    if (fn) fn(cfg);
}

uint8_t libusb_get_bus_number(libusb_device *dev)
{
    uint8_t (*fn)(libusb_device *) = R("libusb_get_bus_number");
    return fn ? fn(dev) : 0;
}

uint8_t libusb_get_device_address(libusb_device *dev)
{
    uint8_t (*fn)(libusb_device *) = R("libusb_get_device_address");
    return fn ? fn(dev) : 0;
}

int libusb_get_device_speed(libusb_device *dev)
{
    int (*fn)(libusb_device *) = R("libusb_get_device_speed");
    return fn ? fn(dev) : -1;
}

int libusb_open(libusb_device *dev, libusb_device_handle **h)
{
    int (*fn)(libusb_device *, libusb_device_handle **) = R("libusb_open");
    int r = fn ? fn(dev, h) : -1;
    logmsg("libusb_open -> %d handle=%p", r, (void *)(h ? *h : NULL));
    return r;
}

void libusb_close(libusb_device_handle *h)
{
    void (*fn)(libusb_device_handle *) = R("libusb_close");
    logmsg("libusb_close %p", (void *)h);
    if (fn) fn(h);
}

libusb_device *libusb_ref_device(libusb_device *dev)
{
    libusb_device *(*fn)(libusb_device *) = R("libusb_ref_device");
    return fn ? fn(dev) : dev;
}

void libusb_unref_device(libusb_device *dev)
{
    void (*fn)(libusb_device *) = R("libusb_unref_device");
    if (fn) fn(dev);
}

int libusb_claim_interface(libusb_device_handle *h, int iface)
{
    int (*fn)(libusb_device_handle *, int) = R("libusb_claim_interface");
    int r = fn ? fn(h, iface) : -1;
    logmsg("libusb_claim_interface %d -> %d", iface, r);
    return r;
}

int libusb_hotplug_register_callback(libusb_context *ctx, int ev, int fl,
                                     int vid, int pid, int cls,
                                     libusb_hotplug_callback_fn cb, void *ud,
                                     libusb_hotplug_callback_handle *ch)
{
    int (*fn)(libusb_context *, int, int, int, int, int,
              libusb_hotplug_callback_fn, void *, libusb_hotplug_callback_handle *) =
        R("libusb_hotplug_register_callback");
    int r = fn ? fn(ctx, ev, fl, vid, pid, cls, cb, ud, ch) : -1;
    logmsg("libusb_hotplug_register_callback vid=%04x pid=%04x events=%d -> %d", vid, pid, ev, r);
    return r;
}

void libusb_hotplug_deregister_callback(libusb_context *ctx, libusb_hotplug_callback_handle ch)
{
    void (*fn)(libusb_context *, libusb_hotplug_callback_handle) = R("libusb_hotplug_deregister_callback");
    if (fn) fn(ctx, ch);
}

struct libusb_transfer *libusb_alloc_transfer(int iso)
{
    struct libusb_transfer *(*fn)(int) = R("libusb_alloc_transfer");
    struct libusb_transfer *t = fn ? fn(iso) : NULL;
    return t;
}

void libusb_free_transfer(struct libusb_transfer *t)
{
    void (*fn)(struct libusb_transfer *) = R("libusb_free_transfer");
    if (t) forget_cb(t);
    if (fn) fn(t);
}

int libusb_submit_transfer(struct libusb_transfer *t)
{
    int (*fn)(struct libusb_transfer *) = R("libusb_submit_transfer");
    if (!t) return fn ? fn(t) : -1;

    const char *dir = (t->endpoint & LIBUSB_ENDPOINT_IN) ? "IN " : "OUT";
    static const char *types[] = { "CONTROL", "ISO", "BULK", "INTERRUPT", "BULK_STREAM" };
    int ty = t->type;
    logmsg("SUBMIT %s %s ep=0x%02x len=%d timeout=%u",
           dir, (ty >= 0 && ty <= 4) ? types[ty] : "?", t->endpoint, t->length, t->timeout);
    if (!(t->endpoint & LIBUSB_ENDPOINT_IN))
        hexdump("  -> data:", t->buffer, t->length);

    if (t->callback && t->callback != our_cb) {
        remember_cb(t, t->callback);
        t->callback = our_cb;
    }
    int r = fn ? fn(t) : -1;
    if (r < 0) logmsg("  submit failed %d (%s)", r, libusb_strerror(r));
    return r;
}

int libusb_cancel_transfer(struct libusb_transfer *t)
{
    int (*fn)(struct libusb_transfer *) = R("libusb_cancel_transfer");
    logmsg("libusb_cancel_transfer ep=0x%02x", t ? t->endpoint : 0);
    return fn ? fn(t) : -1;
}

const char *libusb_strerror(int code)
{
    const char *(*fn)(int) = R("libusb_strerror");
    return fn ? fn(code) : "?";
}

int libusb_handle_events_timeout_completed(libusb_context *ctx, struct timeval *tv, int *completed)
{
    int (*fn)(libusb_context *, struct timeval *, int *) = R("libusb_handle_events_timeout_completed");
    return fn ? fn(ctx, tv, completed) : -1;
}

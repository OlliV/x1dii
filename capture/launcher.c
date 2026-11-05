/*
 * Launcher for X1D II Tether.app.
 *
 * A shell script cannot be the CFBundleExecutable: it has no architecture, so
 * LaunchServices runs the bundle as x86_64 under Rosetta while a terminal runs it as
 * arm64. That silently breaks the bundled libusb, which is arm64-only - dlopen fails
 * with "incompatible architecture" and pyusb reports NoBackendError.
 *
 * This is a real universal binary, so LaunchServices launches it natively. It locates
 * the bundle, points pyusb at the bundled libusb, and execs the interpreter that was
 * chosen at build time.
 */
#include <mach-o/dyld.h>
#include <sys/sysctl.h>
#include <errno.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#ifndef PYTHON
#define PYTHON "/usr/bin/python3"
#endif

/* Running under Rosetta? The bundled libusb is arm64-only, so it would not load and
 * pyusb would report NoBackendError. macOS can launch a bundle translated if it has
 * stale LaunchServices metadata, so re-exec natively rather than trust it. */
static int is_translated(void)
{
    int translated = 0;
    size_t size = sizeof(translated);
    if (sysctlbyname("sysctl.proc_translated", &translated, &size, NULL, 0) == -1) {
        return 0;
    }
    return translated;
}

static void dir_of(const char *path, char *out, size_t n)
{
    strncpy(out, path, n - 1);
    out[n - 1] = '\0';
    char *slash = strrchr(out, '/');
    if (slash) {
        *slash = '\0';
    }
}

int main(int argc, char **argv)
{
    char exe[PATH_MAX];
    uint32_t size = sizeof(exe);
    if (_NSGetExecutablePath(exe, &size) != 0) {
        return 1;
    }

    char macos[PATH_MAX], contents[PATH_MAX];
    dir_of(exe, macos, sizeof(macos));          /* .../Contents/MacOS */
    dir_of(macos, contents, sizeof(contents));  /* .../Contents */

    char res[PATH_MAX], vendor[PATH_MAX], script[PATH_MAX], lib[PATH_MAX];
    snprintf(res, sizeof(res), "%s/Resources", contents);
    snprintf(vendor, sizeof(vendor), "%s/vendor", res);
    snprintf(script, sizeof(script), "%s/app/capture/viewer.py", res);
    snprintf(lib, sizeof(lib), "%s/vendor/libusb-1.0.dylib", res);

    setenv("PYTHONPATH", vendor, 1);
    struct stat st;
    if (stat(lib, &st) == 0) {
        setenv("HBLIBUSB", lib, 1);
    }
    setenv("PATH",
           "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin", 1);

    /* a Finder launch has nowhere to print, so keep a log for diagnosis */
    (void)freopen("/tmp/x1d-tether.err", "a", stderr);

    int translated = is_translated();
    if (translated) {
        fprintf(stderr, "launcher: running translated, re-executing arm64\n");
    }

    char *args[argc + 5];
    int i = 0;
    if (translated) {
        args[i++] = (char *)"/usr/bin/arch";
        args[i++] = (char *)"-arm64";
    }
    args[i++] = (char *)PYTHON;
    args[i++] = script;
    for (int k = 1; k < argc; k++) {
        args[i++] = argv[k];
    }
    args[i] = NULL;

    if (translated) {
        execv("/usr/bin/arch", args);
    } else {
        execv(PYTHON, args);
    }
    fprintf(stderr, "exec %s failed: %s\n", PYTHON, strerror(errno));
    return 127;
}

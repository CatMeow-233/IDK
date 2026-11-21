#define _POSIX_C_SOURCE 200809L

#include <fcntl.h>
#include <pthread.h>
#include <sanitizer/lsan_interface.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <unistd.h>

#define NOT_INSTRUMENTED __attribute__((no_instrument_function))

enum { LINE_CAPACITY = 192, MAX_PROBED_EXITS = 64 };
enum trace_state { TRACE_UNINITIALISED, TRACE_ACTIVE, TRACE_DISABLED };

static const char LOG_PATH_ENV[] = "LEANC_OWNERSHIP_LOG";
static const char PROBED_EXITS_ENV[] = "LEANC_OWNERSHIP_CHECKS";
static const char CHECK_MARKER[] = "LEANC-OWNERSHIP-CHECK";
static const char HEX_DIGITS[] = "0123456789abcdef";

void *__real_malloc(size_t size);
void *__real_calloc(size_t count, size_t size);
void *__real_realloc(void *pointer, size_t size);
void __real_free(void *pointer);
extern const char __executable_start;
extern const char _end;

struct trace_line {
    char text[LINE_CAPACITY];
    size_t length;
};

static enum trace_state state = TRACE_UNINITIALISED;
static int log_fd = -1;
static int inside_runtime;
static unsigned long next_exit_ordinal = 1;
static unsigned long probed_exits[MAX_PROBED_EXITS];
static size_t probed_exit_count;
static unsigned long pending_after_probe;
static pthread_t tracing_thread;
static int tracing_thread_known;

static void NOT_INSTRUMENTED append_char(struct trace_line *line, char c)
{
    if (line->length + 1 < LINE_CAPACITY) {
        line->text[line->length++] = c;
    }
}

static void NOT_INSTRUMENTED append_text(struct trace_line *line, const char *text)
{
    while (*text) {
        append_char(line, *text++);
    }
}

static void NOT_INSTRUMENTED append_hex(struct trace_line *line, uintptr_t value)
{
    char digits[2 * sizeof value];
    size_t count = 0;
    do {
        digits[count++] = HEX_DIGITS[value & 15u];
        value >>= 4;
    } while (value);
    append_char(line, ' ');
    append_text(line, "0x");
    while (count) {
        append_char(line, digits[--count]);
    }
}

static void NOT_INSTRUMENTED append_decimal(struct trace_line *line, unsigned long value)
{
    char digits[24];
    size_t count = 0;
    do {
        digits[count++] = (char)('0' + value % 10u);
        value /= 10u;
    } while (value);
    append_char(line, ' ');
    while (count) {
        append_char(line, digits[--count]);
    }
}

static uintptr_t NOT_INSTRUMENTED image_offset(const void *address)
{
    uintptr_t value = (uintptr_t)address;
    if (value < (uintptr_t)&__executable_start || value >= (uintptr_t)&_end) {
        return 0;
    }
    return value - (uintptr_t)&__executable_start;
}

static void NOT_INSTRUMENTED emit(int fd, struct trace_line *line)
{
    size_t written = 0;
    append_char(line, '\n');
    while (fd >= 0 && written < line->length) {
        ssize_t step = write(fd, line->text + written, line->length - written);
        if (step <= 0) {
            return;
        }
        written += (size_t)step;
    }
}

static void NOT_INSTRUMENTED load_probed_exits(void)
{
    const char *cursor = getenv(PROBED_EXITS_ENV);
    while (cursor && *cursor && probed_exit_count < MAX_PROBED_EXITS) {
        unsigned long ordinal = 0;
        int digits = 0;
        while (*cursor >= '0' && *cursor <= '9' && digits < 12) {
            ordinal = ordinal * 10u + (unsigned long)(*cursor - '0');
            ++cursor;
            ++digits;
        }
        if (digits) {
            probed_exits[probed_exit_count++] = ordinal;
        }
        if (*cursor != ',') {
            return;
        }
        ++cursor;
    }
}

static int NOT_INSTRUMENTED tracing_active(void)
{
    if (state == TRACE_UNINITIALISED) {
        const char *path = getenv(LOG_PATH_ENV);
        log_fd = path ? open(path, O_WRONLY | O_CREAT | O_TRUNC | O_APPEND | O_CLOEXEC, 0600) : -1;
        load_probed_exits();
        state = log_fd >= 0 ? TRACE_ACTIVE : TRACE_DISABLED;
    }
    return state == TRACE_ACTIVE;
}

static int NOT_INSTRUMENTED exit_is_probed(unsigned long ordinal)
{
    for (size_t i = 0; i < probed_exit_count; ++i) {
        if (probed_exits[i] == ordinal) {
            return 1;
        }
    }
    return 0;
}

static void NOT_INSTRUMENTED probe_reachability(unsigned long ordinal, const char *phase)
{
    struct trace_line marker = {.length = 0};
    struct trace_line record = {.length = 0};
    append_text(&marker, CHECK_MARKER);
    append_decimal(&marker, ordinal);
    append_char(&marker, ' ');
    append_text(&marker, phase);
    emit(STDERR_FILENO, &marker);
    int leaked = __lsan_do_recoverable_leak_check();
    append_char(&record, 'C');
    append_decimal(&record, ordinal);
    append_char(&record, ' ');
    append_text(&record, phase);
    append_decimal(&record, leaked ? 1u : 0u);
    emit(log_fd, &record);
}

static int NOT_INSTRUMENTED single_threaded(void)
{
    if (!tracing_thread_known) {
        tracing_thread = pthread_self();
        tracing_thread_known = 1;
        return 1;
    }
    if (pthread_equal(tracing_thread, pthread_self())) {
        return 1;
    }
    struct trace_line line = {.length = 0};
    append_char(&line, 'T');
    emit(log_fd, &line);
    state = TRACE_DISABLED;
    return 0;
}

static int NOT_INSTRUMENTED begin_event(void)
{
    if (inside_runtime || !tracing_active() || !single_threaded()) {
        return 0;
    }
    inside_runtime = 1;
    if (pending_after_probe) {
        unsigned long ordinal = pending_after_probe;
        pending_after_probe = 0;
        probe_reachability(ordinal, "after");
    }
    return 1;
}

static void NOT_INSTRUMENTED end_event(void)
{
    inside_runtime = 0;
}

static void NOT_INSTRUMENTED log_allocation(void *pointer, size_t size, const void *site)
{
    struct trace_line line = {.length = 0};
    append_char(&line, 'A');
    append_hex(&line, (uintptr_t)pointer);
    append_decimal(&line, (unsigned long)size);
    append_hex(&line, image_offset(site));
    emit(log_fd, &line);
}

void *NOT_INSTRUMENTED __wrap_malloc(size_t size)
{
    const void *site = __builtin_return_address(0);
    int tracing = begin_event();
    void *pointer = __real_malloc(size);
    if (tracing) {
        if (pointer) {
            log_allocation(pointer, size, site);
        }
        end_event();
    }
    return pointer;
}

void *NOT_INSTRUMENTED __wrap_calloc(size_t count, size_t size)
{
    const void *site = __builtin_return_address(0);
    int tracing = begin_event();
    void *pointer = __real_calloc(count, size);
    if (tracing) {
        if (pointer) {
            log_allocation(pointer, count * size, site);
        }
        end_event();
    }
    return pointer;
}

void *NOT_INSTRUMENTED __wrap_realloc(void *old_pointer, size_t size)
{
    const void *site = __builtin_return_address(0);
    int tracing = begin_event();
    void *pointer = __real_realloc(old_pointer, size);
    if (tracing) {
        struct trace_line line = {.length = 0};
        append_char(&line, 'R');
        append_hex(&line, (uintptr_t)old_pointer);
        append_hex(&line, (uintptr_t)pointer);
        append_decimal(&line, (unsigned long)size);
        append_hex(&line, image_offset(site));
        emit(log_fd, &line);
        end_event();
    }
    return pointer;
}

void NOT_INSTRUMENTED __wrap_free(void *pointer)
{
    const void *site = __builtin_return_address(0);
    int tracing = begin_event();
    __real_free(pointer);
    if (tracing) {
        if (pointer) {
            struct trace_line line = {.length = 0};
            append_char(&line, 'F');
            append_hex(&line, (uintptr_t)pointer);
            append_hex(&line, image_offset(site));
            emit(log_fd, &line);
        }
        end_event();
    }
}

void NOT_INSTRUMENTED __cyg_profile_func_enter(void *function, void *call_site)
{
    if (!begin_event()) {
        return;
    }
    struct trace_line line = {.length = 0};
    append_char(&line, 'E');
    append_hex(&line, image_offset(function));
    append_hex(&line, image_offset(call_site));
    emit(log_fd, &line);
    end_event();
}

void NOT_INSTRUMENTED __cyg_profile_func_exit(void *function, void *call_site)
{
    if (!begin_event()) {
        return;
    }
    unsigned long ordinal = next_exit_ordinal++;
    struct trace_line line = {.length = 0};
    append_char(&line, 'X');
    append_decimal(&line, ordinal);
    append_hex(&line, image_offset(function));
    append_hex(&line, image_offset(call_site));
    emit(log_fd, &line);
    if (exit_is_probed(ordinal)) {
        probe_reachability(ordinal, "in");
        pending_after_probe = ordinal;
    }
    end_event();
}

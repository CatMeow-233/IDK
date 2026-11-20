/* Trusted runtime tests for sensor_read (run under ASan/UBSan/LSan). */
#include <stdio.h>
#include <string.h>

#include "sensor.h"

static int check(int cond, const char *what)
{
    if (!cond) {
        fprintf(stderr, "FAILED: %s\n", what);
        return 1;
    }
    return 0;
}

static int test_enabled(void)
{
    struct sensor dev = {7, 1, 42};
    int out = -1;
    int f = 0;
    f |= check(sensor_read(&dev, &out) == SENSOR_OK && out == 42, "enabled sensor");
    dev.value = -5;
    f |= check(sensor_read(&dev, &out) == SENSOR_OK && out == -5, "negative value");
    f |= check(dev.id == 7 && dev.enabled == 1 && dev.value == -5, "sensor unchanged");
    return f;
}

static int test_disabled(void)
{
    struct sensor dev = {3, 0, 99};
    int out = -1;
    int f = 0;
    f |= check(sensor_read(&dev, &out) == SENSOR_ERR_DISABLED, "disabled sensor");
    f |= check(out == 0, "out reset to 0");
    return f;
}

static int test_null_sensor(void)
{
    int out = -1;
    int f = 0;
    f |= check(sensor_read(NULL, &out) == SENSOR_ERR_NULL, "NULL sensor");
    f |= check(out == 0, "out reset to 0");
    return f;
}

static int test_null_out(void)
{
    struct sensor dev = {1, 1, 10};
    int f = 0;
    f |= check(sensor_read(&dev, NULL) == SENSOR_ERR_NULL, "NULL out");
    f |= check(sensor_read(NULL, NULL) == SENSOR_ERR_NULL, "NULL sensor and NULL out");
    return f;
}

int main(int argc, char **argv)
{
    if (argc != 2) {
        fprintf(stderr, "usage: %s <case>\n", argv[0]);
        return 2;
    }
    if (strcmp(argv[1], "enabled") == 0) return test_enabled();
    if (strcmp(argv[1], "disabled") == 0) return test_disabled();
    if (strcmp(argv[1], "null_sensor") == 0) return test_null_sensor();
    if (strcmp(argv[1], "null_out") == 0) return test_null_out();
    fprintf(stderr, "unknown case %s\n", argv[1]);
    return 2;
}

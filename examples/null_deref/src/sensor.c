#include "sensor.h"

int sensor_read(const struct sensor *s, int *out)
{
    int rc = SENSOR_OK;

    if (out == NULL) {
        return SENSOR_ERR_NULL;
    }
    *out = 0;
    if (s == NULL) {
        rc = SENSOR_ERR_NULL;
    }
    if (!s->enabled) {
        return SENSOR_ERR_DISABLED;
    }
    *out = s->value;
    return rc;
}

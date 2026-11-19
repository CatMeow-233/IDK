/* Trusted CBMC harness for sensor_read (human-reviewed). */
#include <assert.h>

#include "leanc.h"
#include "sensor.h"

int main(void)
{
    /* Every field of the sensor is arbitrary; s and out may each be NULL. */
    struct sensor dev;
    dev.id = nondet_int();
    dev.enabled = nondet_int();
    dev.value = nondet_int();
    struct sensor snapshot = dev;

    const struct sensor *s = nondet_bool() ? NULL : &dev;
    int out_cell = nondet_int();
    int *out = nondet_bool() ? NULL : &out_cell;
    int out_before = out_cell;

    int rc = sensor_read(s, out);
    LEANC_REACH("after_call");

    /* POST5: the return value is always a documented status code. */
    assert(rc == SENSOR_OK || rc == SENSOR_ERR_NULL || rc == SENSOR_ERR_DISABLED);
    if (out == NULL) {
        LEANC_REACH("null_out");
        assert(rc == SENSOR_ERR_NULL);                    /* POST3 */
        assert(out_cell == out_before);                   /* nothing written */
    } else if (s == NULL) {
        LEANC_REACH("null_sensor");
        assert(rc == SENSOR_ERR_NULL);                    /* POST2 */
        assert(out_cell == 0);
    } else if (dev.enabled == 0) {
        LEANC_REACH("disabled");
        assert(rc == SENSOR_ERR_DISABLED);                /* POST4 */
        assert(out_cell == 0);
    } else {
        LEANC_REACH("enabled");
        assert(rc == SENSOR_OK);                          /* POST1 */
        assert(out_cell == dev.value);
    }
    /* FRAME: the sensor record is never modified. */
    assert(dev.id == snapshot.id && dev.enabled == snapshot.enabled && dev.value == snapshot.value);
    return 0;
}

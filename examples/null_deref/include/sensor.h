#ifndef SENSOR_H
#define SENSOR_H

#include <stddef.h>

/* A sensor channel as kept by the driver. */
struct sensor {
    int id;
    int enabled; /* non-zero when the channel is switched on */
    int value;   /* last sampled value (meaningful only when enabled) */
};

/* Status codes returned by sensor_read. */
#define SENSOR_OK 0
#define SENSOR_ERR_NULL (-1)
#define SENSOR_ERR_DISABLED (-2)

/*
 * Read the current value of a sensor channel into *out.
 *   returns SENSOR_OK            on success (*out == s->value)
 *           SENSOR_ERR_NULL      if out == NULL (nothing is dereferenced or written), or
 *                                if s == NULL (s is not dereferenced; *out is set to 0)
 *           SENSOR_ERR_DISABLED  if s->enabled == 0 (*out is set to 0)
 * On every error with out != NULL, *out holds the defined value 0.  *s is never modified.
 */
int sensor_read(const struct sensor *s, int *out);

#endif

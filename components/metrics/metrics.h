#pragma once

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#if CONFIG_METRICS_ENABLE

void    metrics_init(void);
int64_t metrics_now_us(void);
void    metrics_emit(const char *event, const char *fmt, ...);

#else

static inline void    metrics_init(void)                               {}
static inline int64_t metrics_now_us(void)                            { return 0; }
static inline void    metrics_emit(const char *e, const char *f, ...) {}

#endif /* CONFIG_METRICS_ENABLE */

#ifdef __cplusplus
}
#endif

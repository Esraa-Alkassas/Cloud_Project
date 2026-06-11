#include "metrics.h"

#if CONFIG_METRICS_ENABLE

#include <stdio.h>
#include <stdarg.h>
#include <inttypes.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_timer.h"
#include "esp_app_desc.h"
#include "esp_ota_ops.h"
#include "esp_system.h"
#include "nvs_flash.h"
#include "nvs.h"

static uint32_t s_seq = 0;

int64_t metrics_now_us(void)
{
    return esp_timer_get_time();
}

void metrics_emit(const char *event, const char *fmt, ...)
{
    const esp_app_desc_t *desc = esp_app_get_description();
    int64_t t = esp_timer_get_time();
    uint32_t seq = s_seq++;

    char extra[384] = {0};
    if (fmt && fmt[0] != '\0') {
        va_list ap;
        va_start(ap, fmt);
        vsnprintf(extra, sizeof(extra), fmt, ap);
        va_end(ap);
    }

    if (extra[0] != '\0') {
        printf("##M## {\"v\":1,\"seq\":%" PRIu32 ",\"t_us\":%" PRId64
               ",\"fw\":\"%s\",\"dev\":\"%s\",\"ev\":\"%s\",%s}\n",
               seq, t, desc->version, CONFIG_METRICS_DEVICE_ID, event, extra);
    } else {
        printf("##M## {\"v\":1,\"seq\":%" PRIu32 ",\"t_us\":%" PRId64
               ",\"fw\":\"%s\",\"dev\":\"%s\",\"ev\":\"%s\"}\n",
               seq, t, desc->version, CONFIG_METRICS_DEVICE_ID, event);
    }
}

static void heartbeat_task(void *pv)
{
    while (1) {
        vTaskDelay(pdMS_TO_TICKS(10000));
        uint32_t heap_free = esp_get_free_heap_size();
        uint32_t heap_min  = esp_get_minimum_free_heap_size();
        metrics_emit("heartbeat", "\"heap_free\":%" PRIu32 ",\"heap_min\":%" PRIu32,
                     heap_free, heap_min);
    }
}

void metrics_init(void)
{
    /* Read and clear the reboot-pending marker written before OTA restart */
    uint8_t boot_pend = 0;
    nvs_handle_t h;
    if (nvs_open("provision", NVS_READWRITE, &h) == ESP_OK) {
        nvs_get_u8(h, "ota_boot_pend", &boot_pend);
        nvs_set_u8(h, "ota_boot_pend", 0);
        nvs_commit(h);
        nvs_close(h);
    }

    int reset_reason = (int)esp_reset_reason();
    const esp_partition_t *part = esp_ota_get_running_partition();
    const char *part_label = part ? part->label : "unknown";

    metrics_emit("boot",
                 "\"reset_reason\":%d,\"part\":\"%s\",\"prev_boot_marker\":%d",
                 reset_reason, part_label, (int)boot_pend);

    xTaskCreate(heartbeat_task, "metrics_hb", 2048, NULL, 3, NULL);
}

#endif /* CONFIG_METRICS_ENABLE */

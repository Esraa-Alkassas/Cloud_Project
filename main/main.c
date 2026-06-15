#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "nvs_flash.h"
#include "metrics.h"
#include "wifi.h"
#include "ota.h"
#include "telemetry.h"

static const char *TAG = "MAIN";

void app_main(void)
{
    esp_log_level_set("wifi", ESP_LOG_ERROR);

    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    /* Mark app valid if running from an OTA slot (not factory) */
    if (esp_ota_get_running_partition()->subtype != ESP_PARTITION_SUBTYPE_APP_FACTORY)
        esp_ota_mark_app_valid_cancel_rollback();

    /* Metrics init first — emits boot event, starts heartbeat task */
    metrics_init();

    wifi_init_sta();

#if CONFIG_TELEMETRY_ENABLE
    xTaskCreate(&led_blink_task,    "led_task", 3072, NULL, 5, NULL);
    xTaskCreate(&version_print_task, "ver_task", 3072, NULL, 5, NULL);
    xEventGroupWaitBits(wifi_event_group, WIFI_CONNECTED_BIT, pdFALSE, pdTRUE, portMAX_DELAY);
    mqtt_app_start();
#else
    xTaskCreate(&led_blink_task, "led_task", 3072, NULL, 5, NULL);
#endif

    xTaskCreate(&ota_task, "ota_task", CONFIG_OTA_TASK_STACK_SIZE, NULL, 5, NULL);

    ESP_LOGI(TAG, "app_main complete, tasks running.");
}

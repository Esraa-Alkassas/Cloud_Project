#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "esp_system.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_partition.h"
#include "esp_http_client.h"
#include "nvs_flash.h"
#include "esp_wifi.h"
#include "detools.h"

static const char *TAG = "OTA_DELTA";

#define FIRMWARE_VERSION "3.0.0"

static EventGroupHandle_t wifi_event_group;
#define WIFI_CONNECTED_BIT BIT0

struct patch_state_t
{
    const esp_partition_t *old_partition;
    const esp_partition_t *new_partition;
    esp_ota_handle_t ota_handle;
    esp_http_client_handle_t http_client;
    int old_read_offset;
};

static int read_old_cb(void *arg_p, uint8_t *buf_p, size_t size)
{
    struct patch_state_t *state = (struct patch_state_t *)arg_p;

    esp_err_t err = esp_partition_read(state->old_partition, state->old_read_offset, buf_p, size);
    if (err != ESP_OK)
    {
        ESP_LOGE(TAG, "Failed to read old partition at offset %d", state->old_read_offset);
        return -1;
    }

    state->old_read_offset += size;
    return 0;
}

static int seek_old_cb(void *arg_p, int offset)
{
    struct patch_state_t *state = (struct patch_state_t *)arg_p;
    state->old_read_offset += offset;
    return 0;
}

static int read_patch_cb(void *arg_p, uint8_t *buf_p, size_t size)
{
    struct patch_state_t *state = (struct patch_state_t *)arg_p;
    size_t total_read = 0;

    while (total_read < size)
    {
        int read_len = esp_http_client_read(state->http_client,
                                            (char *)(buf_p + total_read),
                                            size - total_read);

        if (read_len < 0)
        {
            ESP_LOGE(TAG, "HTTP read error");
            return -1;
        }

        if (read_len == 0)
        {
            if (esp_http_client_is_complete_data_received(state->http_client))
            {
                ESP_LOGE(TAG, "HTTP stream ended before expected. File truncated?");
                return -1;
            }
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }

        total_read += read_len;
    }

    return 0;
}

static int write_new_cb(void *arg_p, const uint8_t *buf_p, size_t size)
{
    struct patch_state_t *state = (struct patch_state_t *)arg_p;

    esp_err_t err = esp_ota_write(state->ota_handle, buf_p, size);
    if (err != ESP_OK)
    {
        ESP_LOGE(TAG, "Failed to write to OTA partition");
        return -1;
    }

    return 0;
}

static void event_handler(void *arg, esp_event_base_t event_base, int32_t event_id, void *event_data)
{
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START)
    {
        esp_wifi_connect();
    }
    else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED)
    {
        ESP_LOGW(TAG, "Wi-Fi disconnected. Retrying...");
        esp_wifi_connect();
    }
    else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP)
    {
        ip_event_got_ip_t *event = (ip_event_got_ip_t *)event_data;
        ESP_LOGI(TAG, "Got IP: " IPSTR, IP2STR(&event->ip_info.ip));
        xEventGroupSetBits(wifi_event_group, WIFI_CONNECTED_BIT);
    }
}

static void wifi_init_sta(void)
{
    wifi_event_group = xEventGroupCreate();
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    esp_event_handler_instance_t instance_any_id;
    esp_event_handler_instance_t instance_got_ip;
    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &event_handler, NULL, &instance_any_id));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &event_handler, NULL, &instance_got_ip));

    wifi_config_t wifi_config = {
        .sta = {
            .ssid = CONFIG_ESP_WIFI_SSID,
            .password = CONFIG_ESP_WIFI_PASSWORD,
            .threshold.authmode = WIFI_AUTH_WPA2_PSK,
        },
    };

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_start());

    ESP_LOGI(TAG, "Wi-Fi initialized. Waiting for connection...");
    xEventGroupWaitBits(wifi_event_group, WIFI_CONNECTED_BIT, pdFALSE, pdFALSE, portMAX_DELAY);
}

void trigger_delta_ota_update(void)
{
    ESP_LOGI(TAG, "Starting Delta OTA update sequence...");
    struct patch_state_t state = {0};
    esp_err_t err;

    state.old_partition = esp_ota_get_running_partition();
    state.new_partition = esp_ota_get_next_update_partition(NULL);

    if (state.old_partition == NULL || state.new_partition == NULL)
    {
        ESP_LOGE(TAG, "Could not find valid partitions. Check partitions.csv.");
        return;
    }

    ESP_LOGI(TAG, "Reading from: %s, Rebuilding into: %s",
             state.old_partition->label, state.new_partition->label);

    esp_http_client_config_t config = {
        .url = "http://10.89.227.59:8000/patch.bin", // REPLACE WITH YOUR SERVER IP
        .keep_alive_enable = true,
    };
    state.http_client = esp_http_client_init(&config);

    err = esp_http_client_open(state.http_client, 0);
    if (err != ESP_OK)
    {
        ESP_LOGE(TAG, "Failed to open HTTP connection: %s", esp_err_to_name(err));
        esp_http_client_cleanup(state.http_client);
        return;
    }

    esp_http_client_fetch_headers(state.http_client);
    int status_code = esp_http_client_get_status_code(state.http_client);
    if (status_code != 200)
    {
        ESP_LOGE(TAG, "Invalid HTTP status code: %d. File not found?", status_code);
        esp_http_client_cleanup(state.http_client);
        return;
    }

    int patch_size = esp_http_client_get_content_length(state.http_client);
    if (patch_size <= 0)
    {
        ESP_LOGE(TAG, "Server did not provide Content-Length. Cannot proceed.");
        esp_http_client_cleanup(state.http_client);
        return;
    }
    ESP_LOGI(TAG, "Incoming patch size: %d bytes", patch_size);

    err = esp_ota_begin(state.new_partition, OTA_WITH_SEQUENTIAL_WRITES, &state.ota_handle);
    if (err != ESP_OK)
    {
        ESP_LOGE(TAG, "esp_ota_begin failed: %s", esp_err_to_name(err));
        esp_http_client_cleanup(state.http_client);
        return;
    }

    ESP_LOGI(TAG, "Applying Binary Patch...");
    int patch_res = detools_apply_patch_callbacks(read_old_cb,
                                                  seek_old_cb,
                                                  read_patch_cb,
                                                  patch_size,
                                                  write_new_cb,
                                                  &state);

    esp_http_client_cleanup(state.http_client);

    if (patch_res >= 0)
    {
        ESP_LOGI(TAG, "Patch Applied Successfully! Finalizing...");

        err = esp_ota_end(state.ota_handle);
        if (err == ESP_OK)
        {
            err = esp_ota_set_boot_partition(state.new_partition);
            if (err == ESP_OK)
            {
                ESP_LOGI(TAG, "Boot partition set. Rebooting in 2 seconds...");
                vTaskDelay(pdMS_TO_TICKS(2000));
                esp_restart();
            }
            else
            {
                ESP_LOGE(TAG, "Failed to set boot partition: %s", esp_err_to_name(err));
            }
        }
        else
        {
            ESP_LOGE(TAG, "esp_ota_end failed! Firmware may be corrupted: %s", esp_err_to_name(err));
        }
    }
    else
    {
        ESP_LOGE(TAG, "Detools patching failed with error code: %d", patch_res);
        esp_ota_abort(state.ota_handle);
    }
}

void print_version_task(void *pvParameter)
{
    while (1)
    {
        ESP_LOGW(TAG, "=== Running Firmware Version: %s ===", FIRMWARE_VERSION);
        vTaskDelay(pdMS_TO_TICKS(5000)); // Delay for 5000 milliseconds (5 seconds)
    }
}

void app_main(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND)
    {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    esp_ota_mark_app_valid_cancel_rollback();
    ESP_LOGI(TAG, "Application state marked as valid.");

    xTaskCreate(&print_version_task, "print_version_task", 2048, NULL, 5, NULL);

    wifi_init_sta();

    ESP_LOGI(TAG, "Network stable. Preparing to pull firmware in 5 seconds...");
    vTaskDelay(pdMS_TO_TICKS(5000));

    trigger_delta_ota_update();
}
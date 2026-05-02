#include <string.h>
#include <stdlib.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "esp_system.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_app_format.h"
#include "esp_partition.h"
#include "esp_http_client.h"
#include "esp_crt_bundle.h"
#include "nvs_flash.h"
#include "esp_wifi.h"
#include "driver/gpio.h"
#include "detools.h"
#include "cJSON.h"

static const char *TAG = "OTA_DELTA";

#define API_GATEWAY_URL "https://kavm965brg.execute-api.us-east-1.amazonaws.com/check"

#define LED_PIN_RED GPIO_NUM_21
#define LED_PIN_GREEN GPIO_NUM_23
#define LED_PIN_BLUE GPIO_NUM_22

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

// ==========================================
// LED TASK LOGIC
// ==========================================
void led_blink_task(void *pvParameter)
{
    gpio_reset_pin(LED_PIN_RED);
    gpio_reset_pin(LED_PIN_GREEN);
    gpio_reset_pin(LED_PIN_BLUE);
    gpio_set_direction(LED_PIN_RED, GPIO_MODE_OUTPUT);
    gpio_set_direction(LED_PIN_GREEN, GPIO_MODE_OUTPUT);
    gpio_set_direction(LED_PIN_BLUE, GPIO_MODE_OUTPUT);

    while (1)
    {
        gpio_set_level(LED_PIN_RED, 0);
        gpio_set_level(LED_PIN_GREEN, 1);
        gpio_set_level(LED_PIN_BLUE, 1);
        vTaskDelay(pdMS_TO_TICKS(500));

        gpio_set_level(LED_PIN_RED, 1);
        gpio_set_level(LED_PIN_GREEN, 0);
        gpio_set_level(LED_PIN_BLUE, 1);
        vTaskDelay(pdMS_TO_TICKS(500));

        gpio_set_level(LED_PIN_RED, 1);
        gpio_set_level(LED_PIN_GREEN, 1);
        gpio_set_level(LED_PIN_BLUE, 0);
        vTaskDelay(pdMS_TO_TICKS(500));
    }
}

// ==========================================
// DETOOLS CALLBACKS
// ==========================================
static int read_old_cb(void *arg_p, uint8_t *buf_p, size_t size)
{
    struct patch_state_t *state = (struct patch_state_t *)arg_p;
    esp_err_t err = esp_partition_read(state->old_partition, state->old_read_offset, buf_p, size);
    if (err != ESP_OK)
        return -1;
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
        int read_len = esp_http_client_read(state->http_client, (char *)(buf_p + total_read), size - total_read);
        if (read_len < 0)
            return -1;
        if (read_len == 0)
        {
            if (esp_http_client_is_complete_data_received(state->http_client))
                return -1;
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
        return -1;
    return 0;
}

// ==========================================
// WIFI & NETWORKING
// ==========================================
static void event_handler(void *arg, esp_event_base_t event_base, int32_t event_id, void *event_data)
{
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START)
    {
        esp_wifi_connect();
    }
    else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED)
    {
        xEventGroupClearBits(wifi_event_group, WIFI_CONNECTED_BIT);
        ESP_LOGE(TAG, "Wi-Fi disconnected! Pausing OTA checks until reconnected...");
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

    esp_wifi_set_ps(WIFI_PS_NONE);

    ESP_LOGI(TAG, "Wi-Fi initialized. Waiting for connection...");
}

// ==========================================
// OTA ORCHESTRATION
// ==========================================
void trigger_delta_ota_update(void)
{
    ESP_LOGI(TAG, "Starting Delta OTA update sequence...");

    const esp_app_desc_t *app_desc = esp_app_get_description();

    ESP_LOGI(TAG, "Checking for updates... Sending Hash: [%s]", app_desc->version);

    char api_url[512];
    snprintf(api_url, sizeof(api_url), "%s?hash=%s", API_GATEWAY_URL, app_desc->version);

    esp_http_client_config_t api_config = {
        .url = api_url,
        .crt_bundle_attach = esp_crt_bundle_attach,
    };

    esp_http_client_handle_t api_client = esp_http_client_init(&api_config);
    esp_err_t err = esp_http_client_open(api_client, 0);
    if (err != ESP_OK)
    {
        ESP_LOGE(TAG, "Failed to open API Gateway: %s", esp_err_to_name(err));
        esp_http_client_cleanup(api_client);
        return;
    }

    esp_http_client_fetch_headers(api_client);
    int status_code = esp_http_client_get_status_code(api_client);
    int content_length = esp_http_client_get_content_length(api_client);

    if (status_code != 200 || content_length <= 0)
    {
        ESP_LOGE(TAG, "Invalid API response. Status: %d", status_code);

        ESP_LOGE(TAG, "Attempted URL: %s", api_url);

        if (content_length > 0)
        {
            char *error_buffer = malloc(content_length + 1);
            int read_len = esp_http_client_read(api_client, error_buffer, content_length);
            if (read_len >= 0)
            {
                error_buffer[read_len] = '\0';
                ESP_LOGE(TAG, "AWS Error Message: %s", error_buffer);
            }
            free(error_buffer);
        }
        else
        {
            ESP_LOGE(TAG, "AWS did not send an error body.");
        }

        esp_http_client_cleanup(api_client);
        return;
    }

    char *response_buffer = malloc(content_length + 1);
    int read_len = esp_http_client_read(api_client, response_buffer, content_length);
    response_buffer[read_len] = '\0';
    esp_http_client_cleanup(api_client);

    cJSON *json = cJSON_Parse(response_buffer);
    free(response_buffer);

    if (json == NULL)
        return;

    cJSON *update_available = cJSON_GetObjectItem(json, "update_available");
    if (!update_available || !cJSON_IsTrue(update_available))
    {
        ESP_LOGI(TAG, "Device is up to date. No patch required.");
        cJSON_Delete(json);
        return;
    }

    cJSON *download_url = cJSON_GetObjectItem(json, "download_url");
    if (!download_url || !download_url->valuestring)
    {
        cJSON_Delete(json);
        return;
    }

    ESP_LOGI(TAG, "Update found! Proceeding with patch download...");

    struct patch_state_t state = {0};
    state.old_partition = esp_ota_get_running_partition();
    state.new_partition = esp_ota_get_next_update_partition(NULL);

    esp_http_client_config_t s3_config = {
        .url = download_url->valuestring,
        .crt_bundle_attach = esp_crt_bundle_attach,
        .keep_alive_enable = true,
    };
    state.http_client = esp_http_client_init(&s3_config);
    cJSON_Delete(json);

    err = esp_http_client_open(state.http_client, 0);
    if (err != ESP_OK)
    {
        esp_http_client_cleanup(state.http_client);
        return;
    }

    esp_http_client_fetch_headers(state.http_client);
    int patch_size = esp_http_client_get_content_length(state.http_client);

    err = esp_ota_begin(state.new_partition, OTA_WITH_SEQUENTIAL_WRITES, &state.ota_handle);
    if (err != ESP_OK)
    {
        esp_http_client_cleanup(state.http_client);
        return;
    }

    ESP_LOGI(TAG, "Applying Binary Patch...");
    int patch_res = detools_apply_patch_callbacks(read_old_cb, seek_old_cb, read_patch_cb, patch_size, write_new_cb, &state);
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
                ESP_LOGI(TAG, "Rebooting in 2 seconds...");
                vTaskDelay(pdMS_TO_TICKS(2000));
                esp_restart();
            }
        }
    }
    else
    {
        ESP_LOGE(TAG, "Detools patching failed: %d", patch_res);
        esp_ota_abort(state.ota_handle);
    }
}

void print_version_task(void *pvParameter)
{
    const esp_app_desc_t *app_desc = esp_app_get_description();
    while (1)
    {
        ESP_LOGW(TAG, "=== Running Firmware Version: %s ===", app_desc->version);
        vTaskDelay(pdMS_TO_TICKS(5000));
    }
}

void app_main(void)
{
    // Mute the noisy Wi-Fi state logs
    esp_log_level_set("wifi", ESP_LOG_ERROR);

    volatile uint8_t dummy_counter = 0;
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND)
    {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    const esp_partition_t *running_partition = esp_ota_get_running_partition();
    if (running_partition->subtype != ESP_PARTITION_SUBTYPE_APP_FACTORY)
    {
        esp_ota_mark_app_valid_cancel_rollback();
        ESP_LOGI(TAG, "Application state marked as valid.");
    }
    else
    {
        ESP_LOGI(TAG, "Running from factory partition. Skipping OTA validation.");
    }

    xTaskCreate(&print_version_task, "print_version_task", 2048, NULL, 5, NULL);
    xTaskCreate(&led_blink_task, "led_blink_task", 2048, NULL, 5, NULL);

    wifi_init_sta();

    while (1)
    {
        xEventGroupWaitBits(wifi_event_group, WIFI_CONNECTED_BIT, pdFALSE, pdTRUE, portMAX_DELAY);

        dummy_counter++;
        trigger_delta_ota_update();

        // Wait 32 seconds before checking again
        vTaskDelay(pdMS_TO_TICKS(32000));
    }
}
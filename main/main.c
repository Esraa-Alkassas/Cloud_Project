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
#include "mqtt_client.h"

static const char *TAG = "OTA_DELTA";

// ==========================================
// CONFIGURATION
// ==========================================
#define API_GATEWAY_URL "https://kavm965brg.execute-api.us-east-1.amazonaws.com/check"
#define TB_MQTT_URL "mqtt://mqtt.eu.thingsboard.cloud"

#define LED_PIN_RED GPIO_NUM_21
#define LED_PIN_GREEN GPIO_NUM_23
#define LED_PIN_BLUE GPIO_NUM_22

static EventGroupHandle_t wifi_event_group;
#define WIFI_CONNECTED_BIT BIT0

esp_mqtt_client_handle_t mqtt_client = NULL;

struct patch_state_t
{
    const esp_partition_t *old_partition;
    const esp_partition_t *new_partition;
    esp_ota_handle_t ota_handle;
    esp_http_client_handle_t http_client;
    int old_read_offset;
};

// ==========================================
// TELEMETRY LOGIC
// ==========================================
void send_led_telemetry(int r, int g, int b)
{
    if (mqtt_client == NULL)
        return;

    char payload[128];
    // ThingsBoard expects a JSON object for telemetry
    snprintf(payload, sizeof(payload), "{\"red_led\":%d, \"green_led\":%d, \"blue_led\":%d}", r, g, b);

    int msg_id = esp_mqtt_client_publish(mqtt_client, "v1/devices/me/telemetry", payload, 0, 1, 0);
    ESP_LOGD(TAG, "Sent telemetry, msg_id=%d", msg_id);
}

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
        // State 1: Red/Green ON
        gpio_set_level(LED_PIN_RED, 1);
        gpio_set_level(LED_PIN_GREEN, 0);
        gpio_set_level(LED_PIN_BLUE, 0);
        send_led_telemetry(1, 0, 0);
        vTaskDelay(pdMS_TO_TICKS(500));

        // State 2: Green ON
        gpio_set_level(LED_PIN_RED, 0);
        gpio_set_level(LED_PIN_GREEN, 1);
        gpio_set_level(LED_PIN_BLUE, 0);
        send_led_telemetry(0, 1, 0);
        vTaskDelay(pdMS_TO_TICKS(1000));

        // State 3: Blue ON
        gpio_set_level(LED_PIN_RED, 0);
        gpio_set_level(LED_PIN_GREEN, 0);
        gpio_set_level(LED_PIN_BLUE, 1);
        send_led_telemetry(0, 0, 1);
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

// ==========================================
// MQTT EVENT HANDLER
// ==========================================
static void mqtt_event_handler(void *handler_args, esp_event_base_t base, int32_t event_id, void *event_data)
{
    esp_mqtt_event_handle_t event = event_data;
    switch ((esp_mqtt_event_id_t)event_id)
    {
    case MQTT_EVENT_CONNECTED:
        ESP_LOGI(TAG, "MQTT Connected to ThingsBoard");
        break;
    case MQTT_EVENT_DISCONNECTED:
        ESP_LOGW(TAG, "MQTT Disconnected");
        break;
    case MQTT_EVENT_ERROR:
        ESP_LOGE(TAG, "MQTT Error");
        break;
    default:
        break;
    }
}

static void mqtt_app_start(void)
{
    esp_mqtt_client_config_t mqtt_cfg = {
        .broker.address.uri = TB_MQTT_URL,
        .credentials.username = CONFIG_THINGSBOARD_MQTT_ACCESS_TOKEN,
    };

    mqtt_client = esp_mqtt_client_init(&mqtt_cfg);
    esp_mqtt_client_register_event(mqtt_client, ESP_EVENT_ANY_ID, mqtt_event_handler, NULL);
    esp_mqtt_client_start(mqtt_client);
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
    state->old_read_offset = offset;
    return 0;
}

static int read_patch_cb(void *arg_p, uint8_t *buf_p, size_t size)
{
    struct patch_state_t *state = (struct patch_state_t *)arg_p;
    size_t total_read = 0;
    int retry_count = 0; // FIX: Prevent infinite blocking on silent network drops

    while (total_read < size)
    {
        int read_len = esp_http_client_read(state->http_client, (char *)(buf_p + total_read), size - total_read);
        if (read_len < 0)
            return -1;

        if (read_len == 0)
        {
            if (esp_http_client_is_complete_data_received(state->http_client))
                return -1;

            retry_count++;
            if (retry_count > 500) // ~5 seconds timeout (500 * 10ms)
            {
                ESP_LOGE(TAG, "Timeout waiting for S3 chunk");
                return -1;
            }
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }

        retry_count = 0; // Reset counter on successful read
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

    vTaskDelay(pdMS_TO_TICKS(1));
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
        ESP_LOGE(TAG, "Wi-Fi disconnected!");
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

    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &event_handler, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &event_handler, NULL, NULL));

    wifi_config_t wifi_config = {
        .sta = {
            .ssid = CONFIG_ESP_WIFI_SSID,
            .password = CONFIG_ESP_WIFI_PASSWORD,
        },
    };

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_start());
}

// ==========================================
// OTA ORCHESTRATION
// ==========================================
void trigger_delta_ota_update(void)
{
    const esp_app_desc_t *app_desc = esp_app_get_description();
    char api_url[512];
    snprintf(api_url, sizeof(api_url), "%s?hash=%s", API_GATEWAY_URL, app_desc->version);

    esp_http_client_config_t api_config = {
        .url = api_url,
        .crt_bundle_attach = esp_crt_bundle_attach,
        .buffer_size = 2048,
    };

    esp_http_client_handle_t api_client = esp_http_client_init(&api_config);
    esp_err_t err = esp_http_client_open(api_client, 0);
    if (err != ESP_OK)
    {
        ESP_LOGE(TAG, "Failed to open API HTTP client: %s", esp_err_to_name(err));
        esp_http_client_cleanup(api_client);
        return;
    }

    esp_http_client_fetch_headers(api_client);
    int status_code = esp_http_client_get_status_code(api_client);

    if (status_code != 200)
    {
        ESP_LOGE(TAG, "API check failed. HTTP %d", status_code);
        esp_http_client_cleanup(api_client);
        return;
    }

    char response_buffer[2048] = {0};
    int total_read = 0;
    int api_retry_count = 0; // FIX: Timeout for API connection drops

    while (1)
    {
        int read_len = esp_http_client_read(api_client, response_buffer + total_read, sizeof(response_buffer) - 1 - total_read);
        if (read_len < 0)
        {
            ESP_LOGE(TAG, "Error reading from API");
            break;
        }
        if (read_len == 0)
        {
            if (esp_http_client_is_complete_data_received(api_client))
                break;

            api_retry_count++;
            if (api_retry_count > 500)
            {
                ESP_LOGE(TAG, "Timeout waiting for API response");
                break;
            }
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }

        api_retry_count = 0;
        total_read += read_len;
        if (total_read >= sizeof(response_buffer) - 1)
            break; // Buffer is full
    }

    esp_http_client_cleanup(api_client);

    if (total_read <= 0)
    {
        ESP_LOGE(TAG, "Empty response from API Gateway.");
        return;
    }

    // FIX: Ensure strict null-termination before parsing to avoid cJSON segfaults
    response_buffer[total_read] = '\0';

    cJSON *json = cJSON_Parse(response_buffer);
    if (json == NULL)
    {
        ESP_LOGE(TAG, "Failed to parse API JSON. Raw: %s", response_buffer);
        return;
    }

    cJSON *update_available = cJSON_GetObjectItem(json, "update_available");
    if (!update_available || !cJSON_IsTrue(update_available))
    {
        ESP_LOGI(TAG, "Device is up to date. No update available.");
        cJSON_Delete(json);
        return;
    }

    cJSON *download_url = cJSON_GetObjectItem(json, "download_url");
    if (!download_url || !download_url->valuestring)
    {
        ESP_LOGE(TAG, "Update available, but API omitted the download_url.");
        cJSON_Delete(json);
        return;
    }

    ESP_LOGI(TAG, "Extracted S3 Download URL: %s", download_url->valuestring);

    struct patch_state_t state = {0};
    state.old_partition = esp_ota_get_running_partition();
    state.new_partition = esp_ota_get_next_update_partition(NULL);

    esp_http_client_config_t s3_config = {
        .url = download_url->valuestring,
        .crt_bundle_attach = esp_crt_bundle_attach,
        .keep_alive_enable = true,
        .buffer_size = 2048,
        .buffer_size_tx = 2048,
    };

    state.http_client = esp_http_client_init(&s3_config);
    cJSON_Delete(json);

    err = esp_http_client_open(state.http_client, 0);
    if (err != ESP_OK)
    {
        ESP_LOGE(TAG, "Failed to open S3 HTTP client: %s", esp_err_to_name(err));
        esp_http_client_cleanup(state.http_client);
        return;
    }

    esp_http_client_fetch_headers(state.http_client);

    int s3_status = esp_http_client_get_status_code(state.http_client);
    if (s3_status != 200)
    {
        ESP_LOGE(TAG, "S3 returned HTTP %d. Aborting.", s3_status);

        char err_buf[512] = {0};
        int error_read_len = esp_http_client_read(state.http_client, err_buf, sizeof(err_buf) - 1);
        if (error_read_len > 0)
        {
            ESP_LOGE(TAG, "S3 Error Body: %s", err_buf);
        }

        esp_http_client_cleanup(state.http_client);
        return;
    }

    int patch_size = esp_http_client_get_content_length(state.http_client);

    err = esp_ota_begin(state.new_partition, OTA_WITH_SEQUENTIAL_WRITES, &state.ota_handle);
    if (err != ESP_OK)
    {
        ESP_LOGE(TAG, "esp_ota_begin failed: %s", esp_err_to_name(err));
        esp_http_client_cleanup(state.http_client);
        return;
    }

    ESP_LOGI(TAG, "Starting detools patch application...");
    int patch_res = detools_apply_patch_callbacks(read_old_cb, seek_old_cb, read_patch_cb, patch_size, write_new_cb, &state);
    esp_http_client_cleanup(state.http_client);

    if (patch_res >= 0)
    {
        ESP_LOGI(TAG, "Patch applied successfully. Validating image...");
        err = esp_ota_end(state.ota_handle);
        if (err == ESP_OK)
        {
            err = esp_ota_set_boot_partition(state.new_partition);
            if (err == ESP_OK)
            {
                ESP_LOGI(TAG, "OTA Success! Rebooting...");
                esp_restart();
            }
            else
            {
                ESP_LOGE(TAG, "esp_ota_set_boot_partition failed! %s", esp_err_to_name(err));
            }
        }
        else
        {
            ESP_LOGE(TAG, "esp_ota_end failed (likely corrupt image)! %s", esp_err_to_name(err));
        }
    }
    else
    {
        ESP_LOGE(TAG, "Detools patch application failed! Error code: %d", patch_res);
        esp_ota_abort(state.ota_handle);
    }
}

void print_version_task(void *pvParameter)
{
    const esp_app_desc_t *app_desc = esp_app_get_description();
    while (1)
    {
        ESP_LOGI(TAG, "Firmware: %s", app_desc->version);

        if (mqtt_client != NULL)
        {
            char payload[64];
            snprintf(payload, sizeof(payload), "{\"fw_version\":\"%s\"}", app_desc->version);
            esp_mqtt_client_publish(mqtt_client, "v1/devices/me/telemetry", payload, 0, 1, 0);
        }

        vTaskDelay(pdMS_TO_TICKS(10000));
    }
}

void ota_task(void *pvParameter)
{
    while (1)
    {
        trigger_delta_ota_update();
        vTaskDelay(pdMS_TO_TICKS(60000));
    }
}

void app_main(void)
{
    esp_log_level_set("wifi", ESP_LOG_ERROR);

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
    }

    xTaskCreate(&print_version_task, "version_task", 2048, NULL, 5, NULL);
    xTaskCreate(&led_blink_task, "led_task", 3072, NULL, 5, NULL);

    wifi_init_sta();

    xEventGroupWaitBits(wifi_event_group, WIFI_CONNECTED_BIT, pdFALSE, pdTRUE, portMAX_DELAY);
    mqtt_app_start();

    // FIX: Increased task stack from 8192 to 12288 to safely accommodate HTTPS mbedTLS overhead and stack variables
    xTaskCreate(&ota_task, "ota_task", 12288, NULL, 5, NULL);
}
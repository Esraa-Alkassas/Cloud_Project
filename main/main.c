#include <string.h>
#include <stdlib.h>
#include <stdbool.h>
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
    int total_bytes_written;
    int content_length;
    int last_pct;
};

// ==========================================
// TELEMETRY LOGIC
// ==========================================
void send_led_telemetry(int r, int g, int b)
{
    if (mqtt_client == NULL)
        return;

    char payload[128];
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
        gpio_set_level(LED_PIN_RED, 1);
        gpio_set_level(LED_PIN_GREEN, 0);
        gpio_set_level(LED_PIN_BLUE, 0);
        send_led_telemetry(1, 0, 0);
        vTaskDelay(pdMS_TO_TICKS(1000));

        gpio_set_level(LED_PIN_RED, 0);
        gpio_set_level(LED_PIN_GREEN, 1);
        gpio_set_level(LED_PIN_BLUE, 0);
        send_led_telemetry(0, 1, 0);
        vTaskDelay(pdMS_TO_TICKS(1000));

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
    int retry_count = 0;

    while (total_read < size)
    {
        int read_len = esp_http_client_read(state->http_client, (char *)(buf_p + total_read), size - total_read);
        if (read_len < 0)
        {
            ESP_LOGE(TAG, "S3 Read error: %d", read_len);
            return -1;
        }

        if (read_len == 0)
        {
            if (esp_http_client_is_complete_data_received(state->http_client))
                return -1;

            retry_count++;
            if (retry_count > 500)
            {
                ESP_LOGE(TAG, "Timeout waiting for S3 chunk (5s)");
                return -1;
            }
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }

        retry_count = 0;
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
        ESP_LOGE(TAG, "esp_ota_write failed: %s", esp_err_to_name(err));
        return -1;
    }

    state->total_bytes_written += size;
    if (state->content_length > 0)
    {
        int pct = (state->total_bytes_written * 100) / state->content_length;
        if (pct != state->last_pct && pct % 5 == 0)
        {
            ESP_LOGI(TAG, "Update Progress: %d%% (%d/%d bytes)", pct, state->total_bytes_written, state->content_length);
            state->last_pct = pct;
        }
    }

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
    ESP_LOGI(TAG, "Checking for updates. Current version: %s", app_desc->version);

    char api_url[512];
    snprintf(api_url, sizeof(api_url), "%s?hash=%s", API_GATEWAY_URL, app_desc->version);

    esp_http_client_config_t api_config = {
        .url = api_url,
        .crt_bundle_attach = esp_crt_bundle_attach,
        .timeout_ms = 10000,
    };

    esp_http_client_handle_t api_client = esp_http_client_init(&api_config);
    esp_err_t err = esp_http_client_open(api_client, 0);
    if (err != ESP_OK)
    {
        ESP_LOGE(TAG, "Failed to connect to Update Broker: %s", esp_err_to_name(err));
        esp_http_client_cleanup(api_client);
        return;
    }

    esp_http_client_fetch_headers(api_client);
    int status_code = esp_http_client_get_status_code(api_client);

    if (status_code != 200)
    {
        ESP_LOGE(TAG, "Update Broker returned error HTTP %d", status_code);
        esp_http_client_cleanup(api_client);
        return;
    }

    char response_buffer[2048] = {0};
    int total_read = 0;
    while (1)
    {
        int read_len = esp_http_client_read(api_client, response_buffer + total_read, sizeof(response_buffer) - 1 - total_read);
        if (read_len <= 0)
        {
            if (read_len == 0 && esp_http_client_is_complete_data_received(api_client)) break;
            if (read_len < 0) ESP_LOGE(TAG, "Error reading API response");
            break;
        }
        total_read += read_len;
    }
    esp_http_client_cleanup(api_client);

    if (total_read <= 0) return;
    response_buffer[total_read] = '\0';

    cJSON *json = cJSON_Parse(response_buffer);
    if (json == NULL)
    {
        ESP_LOGE(TAG, "Invalid JSON from Broker: %s", response_buffer);
        return;
    }

    cJSON *update_available = cJSON_GetObjectItem(json, "update_available");
    if (!update_available || !cJSON_IsTrue(update_available))
    {
        ESP_LOGI(TAG, "Device is up-to-date.");
        cJSON_Delete(json);
        return;
    }

    cJSON *download_url = cJSON_GetObjectItem(json, "download_url");
    cJSON *is_delta_json = cJSON_GetObjectItem(json, "is_delta");
    cJSON *latest_ver_json = cJSON_GetObjectItem(json, "latest_version");

    if (!download_url || !download_url->valuestring)
    {
        ESP_LOGE(TAG, "Broker response missing download_url");
        cJSON_Delete(json);
        return;
    }

    bool is_delta = cJSON_IsTrue(is_delta_json);
    ESP_LOGI(TAG, "New version found: %s", latest_ver_json ? latest_ver_json->valuestring : "Unknown");
    ESP_LOGI(TAG, "Update strategy: %s", is_delta ? "Optimized Delta Patch" : "Full Binary Fallback");

    struct patch_state_t state = {0};
    state.old_partition = esp_ota_get_running_partition();
    state.new_partition = esp_ota_get_next_update_partition(NULL);

    esp_http_client_config_t s3_config = {
        .url = download_url->valuestring,
        .crt_bundle_attach = esp_crt_bundle_attach,
        .keep_alive_enable = true,
        .timeout_ms = 15000,
    };

    state.http_client = esp_http_client_init(&s3_config);
    cJSON_Delete(json);

    err = esp_http_client_open(state.http_client, 0);
    if (err != ESP_OK)
    {
        ESP_LOGE(TAG, "Failed to connect to S3 storage: %s", esp_err_to_name(err));
        esp_http_client_cleanup(state.http_client);
        return;
    }

    esp_http_client_fetch_headers(state.http_client);
    int s3_status = esp_http_client_get_status_code(state.http_client);
    if (s3_status != 200)
    {
        ESP_LOGE(TAG, "S3 access denied. HTTP %d", s3_status);
        esp_http_client_cleanup(state.http_client);
        return;
    }

    state.content_length = esp_http_client_get_content_length(state.http_client);
    ESP_LOGI(TAG, "Download started. Size: %d bytes", state.content_length);

    err = esp_ota_begin(state.new_partition, OTA_WITH_SEQUENTIAL_WRITES, &state.ota_handle);
    if (err != ESP_OK)
    {
        ESP_LOGE(TAG, "OTA initialization failed: %s", esp_err_to_name(err));
        esp_http_client_cleanup(state.http_client);
        return;
    }

    int update_res = -1;
    if (is_delta)
    {
        update_res = detools_apply_patch_callbacks(read_old_cb, seek_old_cb, read_patch_cb, state.content_length, write_new_cb, &state);
    }
    else
    {
        char *buf = malloc(2048);
        if (buf)
        {
            update_res = 0;
            while (1)
            {
                int r = esp_http_client_read(state.http_client, buf, 2048);
                if (r <= 0)
                {
                    if (r == 0 && esp_http_client_is_complete_data_received(state.http_client)) break;
                    update_res = -1;
                    break;
                }
                if (write_new_cb(&state, (uint8_t *)buf, r) != 0)
                {
                    update_res = -1;
                    break;
                }
            }
            free(buf);
        }
    }

    esp_http_client_cleanup(state.http_client);

    if (update_res >= 0)
    {
        ESP_LOGI(TAG, "Download complete. Verifying image integrity...");
        err = esp_ota_end(state.ota_handle);
        if (err == ESP_OK)
        {
            ESP_ERROR_CHECK(esp_ota_set_boot_partition(state.new_partition));
            ESP_LOGI(TAG, "OTA Successful! Current slot: %s. Rebooting...", state.new_partition->label);
            vTaskDelay(pdMS_TO_TICKS(500));
            esp_restart();
        }
        else
        {
            ESP_LOGE(TAG, "Image verification failed! (Error: %s)", esp_err_to_name(err));
        }
    }
    else
    {
        ESP_LOGE(TAG, "Update failed during transmission. Detools/HTTP Error: %d", update_res);
        esp_ota_abort(state.ota_handle);
    }
}

void print_version_task(void *pvParameter)
{
    const esp_app_desc_t *app_desc = esp_app_get_description();
    while (1)
    {
        ESP_LOGI(TAG, "Active Firmware Version: %s", app_desc->version);
        if (mqtt_client)
        {
            char p[64];
            snprintf(p, sizeof(p), "{\"fw_version\":\"%s\"}", app_desc->version);
            esp_mqtt_client_publish(mqtt_client, "v1/devices/me/telemetry", p, 0, 1, 0);
        }
        vTaskDelay(pdMS_TO_TICKS(30000));
    }
}

void ota_task(void *pvParameter)
{
    // Wait for network to stabilize
    vTaskDelay(pdMS_TO_TICKS(5000));
    while (1)
    {
        trigger_delta_ota_update();
        vTaskDelay(pdMS_TO_TICKS(300000)); // Check every 5 minutes
    }
}

void app_main(void)
{
    esp_log_level_set("wifi", ESP_LOG_ERROR);
    ESP_ERROR_CHECK(nvs_flash_init());

    const esp_partition_t *run = esp_ota_get_running_partition();
    ESP_LOGI(TAG, "Booting from partition: %s", run->label);
    if (run->subtype != ESP_PARTITION_SUBTYPE_APP_FACTORY)
    {
        esp_ota_mark_app_valid_cancel_rollback();
    }

    xTaskCreate(&print_version_task, "ver_task", 3072, NULL, 5, NULL);
    xTaskCreate(&led_blink_task, "led_task", 3072, NULL, 5, NULL);

    wifi_init_sta();
    xEventGroupWaitBits(wifi_event_group, WIFI_CONNECTED_BIT, pdFALSE, pdTRUE, portMAX_DELAY);
    
    mqtt_app_start();
    xTaskCreate(&ota_task, "ota_task", 12288, NULL, 5, NULL);
}

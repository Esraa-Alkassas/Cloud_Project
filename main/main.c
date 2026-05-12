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
#include "nvs.h"
#include "esp_wifi.h"
#include "driver/gpio.h"
#include "detools.h"
#include "cJSON.h"
#include "mqtt_client.h"

static const char *TAG = "OTA_DELTA";

#define API_GATEWAY_URL "https://kavm965brg.execute-api.us-east-1.amazonaws.com/check"
#define TB_MQTT_URL "mqtt://mqtt.eu.thingsboard.cloud"
#define LED_PIN_RED GPIO_NUM_21
#define LED_PIN_GREEN GPIO_NUM_23
#define LED_PIN_BLUE GPIO_NUM_22

static EventGroupHandle_t wifi_event_group;
#define WIFI_CONNECTED_BIT BIT0
esp_mqtt_client_handle_t mqtt_client = NULL;
static int s_retry_num = 0;

struct patch_state_t {
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
// NVS & PERSISTENCE
// ==========================================
esp_err_t get_stored_value(const char *key, char *out_val, size_t max_len) {
    nvs_handle_t handle;
    esp_err_t err = nvs_open("provision", NVS_READONLY, &handle);
    if (err != ESP_OK) return err;
    err = nvs_get_str(handle, key, out_val, &max_len);
    nvs_close(handle);
    return err;
}

esp_err_t store_value(const char *key, const char *val) {
    nvs_handle_t handle;
    esp_err_t err = nvs_open("provision", NVS_READWRITE, &handle);
    if (err != ESP_OK) return err;
    err = nvs_set_str(handle, key, val);
    if (err == ESP_OK) nvs_commit(handle);
    nvs_close(handle);
    return err;
}

// ==========================================
// TELEMETRY & LED
// ==========================================
void send_led_telemetry(int r, int g, int b) {
    if (mqtt_client == NULL) return;
    char payload[128];
    snprintf(payload, sizeof(payload), "{\"red_led\":%d, \"green_led\":%d, \"blue_led\":%d}", r, g, b);
    esp_mqtt_client_publish(mqtt_client, "v1/devices/me/telemetry", payload, 0, 1, 0);
}

void led_blink_task(void *pvParameter) {
    gpio_reset_pin(LED_PIN_RED); gpio_reset_pin(LED_PIN_GREEN); gpio_reset_pin(LED_PIN_BLUE);
    gpio_set_direction(LED_PIN_RED, GPIO_MODE_OUTPUT); gpio_set_direction(LED_PIN_GREEN, GPIO_MODE_OUTPUT); gpio_set_direction(LED_PIN_BLUE, GPIO_MODE_OUTPUT);
    while (1) {
        gpio_set_level(LED_PIN_RED, 1); vTaskDelay(pdMS_TO_TICKS(1000));
        gpio_set_level(LED_PIN_RED, 0); gpio_set_level(LED_PIN_GREEN, 1); vTaskDelay(pdMS_TO_TICKS(1000));
        gpio_set_level(LED_PIN_GREEN, 0); gpio_set_level(LED_PIN_BLUE, 1); vTaskDelay(pdMS_TO_TICKS(1000));
        gpio_set_level(LED_PIN_BLUE, 0);
        send_led_telemetry(0,0,0);
    }
}

// ==========================================
// WIFI MANAGER
// ==========================================
static void wifi_event_handler(void* arg, esp_event_base_t event_base, int32_t event_id, void* event_data) {
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        wifi_event_sta_disconnected_t* event = (wifi_event_sta_disconnected_t*) event_data;
        ESP_LOGW(TAG, "WiFi: Disconnected (Reason: %d). Retry %d/10", event->reason, s_retry_num);
        if (s_retry_num < 10) {
            esp_wifi_connect();
            s_retry_num++;
        } else {
            ESP_LOGE(TAG, "WiFi: Max retries reached. Clearing NVS WiFi config...");
            esp_wifi_restore();
            esp_restart();
        }
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t* event = (ip_event_got_ip_t*) event_data;
        ESP_LOGI(TAG, "WiFi: Connected! IP: " IPSTR, IP2STR(&event->ip_info.ip));
        s_retry_num = 0;
        xEventGroupSetBits(wifi_event_group, WIFI_CONNECTED_BIT);
    }
}

static void wifi_init_sta(void) {
    wifi_event_group = xEventGroupCreate();
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &wifi_event_handler, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &wifi_event_handler, NULL, NULL));

    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_FLASH));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));

    wifi_config_t current_conf;
    esp_wifi_get_config(WIFI_IF_STA, &current_conf);

    bool use_defaults = (strlen((char *)current_conf.sta.ssid) == 0) || 
                        (strcmp((char *)current_conf.sta.ssid, "myssid") == 0);

    if (use_defaults) {
        ESP_LOGI(TAG, "WiFi: Provisioning with build defaults (SSID: %s)", CONFIG_ESP_WIFI_SSID);
        wifi_config_t wifi_config = {
            .sta = { .ssid = CONFIG_ESP_WIFI_SSID, .password = CONFIG_ESP_WIFI_PASSWORD },
        };
        ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    } else {
        ESP_LOGI(TAG, "WiFi: Using stored SSID: %s", (char *)current_conf.sta.ssid);
    }
    ESP_ERROR_CHECK(esp_wifi_start());
}

// ==========================================
// MQTT & OTA
// ==========================================
static void mqtt_app_start(void) {
    char token[64] = {0};
    if (get_stored_value("mqtt_token", token, sizeof(token)) != ESP_OK) {
        strncpy(token, CONFIG_THINGSBOARD_MQTT_ACCESS_TOKEN, sizeof(token)-1);
        store_value("mqtt_token", token);
    }
    esp_mqtt_client_config_t mqtt_cfg = { .broker.address.uri = TB_MQTT_URL, .credentials.username = token };
    mqtt_client = esp_mqtt_client_init(&mqtt_cfg);
    esp_mqtt_client_start(mqtt_client);
}

static int read_patch_cb(void *arg_p, uint8_t *buf_p, size_t size) {
    struct patch_state_t *state = (struct patch_state_t *)arg_p;
    size_t total = 0;
    while (total < size) {
        int r = esp_http_client_read(state->http_client, (char *)(buf_p + total), size - total);
        if (r <= 0) return -1;
        total += r;
    }
    return 0;
}

static int write_new_cb(void *arg_p, const uint8_t *buf_p, size_t size) {
    struct patch_state_t *state = (struct patch_state_t *)arg_p;
    if (esp_ota_write(state->ota_handle, buf_p, size) != ESP_OK) return -1;
    state->total_bytes_written += size;
    if (state->content_length > 0) {
        int pct = (state->total_bytes_written * 100) / state->content_length;
        if (pct != state->last_pct && pct % 10 == 0) { ESP_LOGI(TAG, "OTA: %d%%", pct); state->last_pct = pct; }
    }
    return 0;
}

void trigger_delta_ota_update(void) {
    const esp_app_desc_t *app_desc = esp_app_get_description();
    char url[512]; snprintf(url, sizeof(url), "%s?hash=%s", API_GATEWAY_URL, app_desc->version);
    esp_http_client_config_t cfg = { .url = url, .crt_bundle_attach = esp_crt_bundle_attach };
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    if (esp_http_client_open(client, 0) != ESP_OK) { esp_http_client_cleanup(client); return; }
    esp_http_client_fetch_headers(client);
    char buf[1024] = {0}; int r = esp_http_client_read(client, buf, sizeof(buf)-1);
    esp_http_client_cleanup(client);
    if (r <= 0) return;
    cJSON *json = cJSON_Parse(buf);
    if (cJSON_IsTrue(cJSON_GetObjectItem(json, "update_available"))) {
        char *dl_url = cJSON_GetObjectItem(json, "download_url")->valuestring;
        struct patch_state_t state = { .old_partition = esp_ota_get_running_partition(), .new_partition = esp_ota_get_next_update_partition(NULL) };
        esp_http_client_config_t s3_cfg = { .url = dl_url, .crt_bundle_attach = esp_crt_bundle_attach, .buffer_size = 8192 };
        state.http_client = esp_http_client_init(&s3_cfg);
        if (esp_http_client_open(state.http_client, 0) == ESP_OK) {
            esp_http_client_fetch_headers(state.http_client);
            state.content_length = esp_http_client_get_content_length(state.http_client);
            if (esp_ota_begin(state.new_partition, OTA_SIZE_UNKNOWN, &state.ota_handle) == ESP_OK) {
                int res = cJSON_IsTrue(cJSON_GetObjectItem(json, "is_delta")) ? 
                    detools_apply_patch_callbacks(NULL, NULL, read_patch_cb, state.content_length, write_new_cb, &state) : -1;
                if (res >= 0 && esp_ota_end(state.ota_handle) == ESP_OK) {
                    esp_ota_set_boot_partition(state.new_partition);
                    ESP_LOGI(TAG, "OTA Success! Rebooting...");
                    vTaskDelay(pdMS_TO_TICKS(2000));
                    esp_restart();
                }
            }
        }
        esp_http_client_cleanup(state.http_client);
    }
    cJSON_Delete(json);
}

void ota_task(void *pv) { vTaskDelay(pdMS_TO_TICKS(10000)); while(1) { trigger_delta_ota_update(); vTaskDelay(pdMS_TO_TICKS(31000)); } }

void app_main(void) {
    esp_log_level_set("wifi", ESP_LOG_ERROR);
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) { ESP_ERROR_CHECK(nvs_flash_erase()); ret = nvs_flash_init(); }
    ESP_ERROR_CHECK(ret);
    const esp_partition_t *run = esp_ota_get_running_partition();
    if (run->subtype != ESP_PARTITION_SUBTYPE_APP_FACTORY) esp_ota_mark_app_valid_cancel_rollback();
    
    wifi_init_sta();
    xTaskCreate(&led_blink_task, "led_task", 3072, NULL, 5, NULL);
    
    ESP_LOGI(TAG, "Waiting for WiFi IP...");
    if (xEventGroupWaitBits(wifi_event_group, WIFI_CONNECTED_BIT, pdFALSE, pdTRUE, pdMS_TO_TICKS(20000)) & WIFI_CONNECTED_BIT) {
        mqtt_app_start();
        xTaskCreate(&ota_task, "ota_task", 12288, NULL, 5, NULL);
    } else {
        ESP_LOGE(TAG, "Failed to get IP. Device will retry in background.");
    }
}

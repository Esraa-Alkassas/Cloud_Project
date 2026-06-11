#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "nvs_flash.h"
#include "nvs.h"
#include "wifi.h"

static const char *TAG = "WIFI";

EventGroupHandle_t wifi_event_group;
static int s_retry_num = 0;

esp_err_t get_stored_value(const char *key, char *out_val, size_t max_len)
{
    nvs_handle_t handle;
    esp_err_t err = nvs_open("provision", NVS_READONLY, &handle);
    if (err != ESP_OK)
        return err;
    err = nvs_get_str(handle, key, out_val, &max_len);
    nvs_close(handle);
    return err;
}

esp_err_t store_value(const char *key, const char *val)
{
    nvs_handle_t handle;
    esp_err_t err = nvs_open("provision", NVS_READWRITE, &handle);
    if (err != ESP_OK)
        return err;
    err = nvs_set_str(handle, key, val);
    if (err == ESP_OK)
        nvs_commit(handle);
    nvs_close(handle);
    return err;
}

static void event_handler(void *arg, esp_event_base_t event_base,
                          int32_t event_id, void *event_data)
{
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        wifi_event_sta_disconnected_t *event = (wifi_event_sta_disconnected_t *)event_data;
        ESP_LOGW(TAG, "WiFi Disconnected (Reason: %d). Retry %d/%d",
                 event->reason, s_retry_num, CONFIG_ESP_MAXIMUM_RETRY);
        if (s_retry_num < CONFIG_ESP_MAXIMUM_RETRY) {
            esp_wifi_connect();
            s_retry_num++;
        } else {
            ESP_LOGE(TAG, "WiFi connection failed after max retries.");
        }
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = (ip_event_got_ip_t *)event_data;
        ESP_LOGI(TAG, "Got IP:" IPSTR, IP2STR(&event->ip_info.ip));
        s_retry_num = 0;
        if (wifi_event_group)
            xEventGroupSetBits(wifi_event_group, WIFI_CONNECTED_BIT);

        wifi_config_t conf;
        if (esp_wifi_get_config(WIFI_IF_STA, &conf) == ESP_OK) {
            store_value("wifi_ssid", (char *)conf.sta.ssid);
            store_value("wifi_pass", (char *)conf.sta.password);
        }
    }
}

void wifi_init_sta(void)
{
    wifi_event_group = xEventGroupCreate();

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    esp_event_handler_instance_t instance_any_id;
    esp_event_handler_instance_t instance_got_ip;
    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                                        &event_handler, NULL, &instance_any_id));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                                        &event_handler, NULL, &instance_got_ip));

    wifi_config_t wifi_config = {
        .sta = {
#if CONFIG_ESP_WIFI_AUTH_OPEN
            .threshold.authmode = WIFI_AUTH_OPEN,
#elif CONFIG_ESP_WIFI_AUTH_WEP
            .threshold.authmode = WIFI_AUTH_WEP,
#elif CONFIG_ESP_WIFI_AUTH_WPA_PSK
            .threshold.authmode = WIFI_AUTH_WPA_PSK,
#elif CONFIG_ESP_WIFI_AUTH_WPA2_PSK
            .threshold.authmode = WIFI_AUTH_WPA2_PSK,
#elif CONFIG_ESP_WIFI_AUTH_WPA_WPA2_PSK
            .threshold.authmode = WIFI_AUTH_WPA_WPA2_PSK,
#elif CONFIG_ESP_WIFI_AUTH_WPA3_PSK
            .threshold.authmode = WIFI_AUTH_WPA3_PSK,
#elif CONFIG_ESP_WIFI_AUTH_WPA2_WPA3_PSK
            .threshold.authmode = WIFI_AUTH_WPA2_WPA3_PSK,
#elif CONFIG_ESP_WIFI_AUTH_WAPI_PSK
            .threshold.authmode = WIFI_AUTH_WAPI_PSK,
#endif
#if CONFIG_ESP_WPA3_SAE_PWE_HUNT_AND_PECK
            .sae_pwe_h2e = WPA3_SAE_PWE_HUNT_AND_PECK,
#elif CONFIG_ESP_WPA3_SAE_PWE_HASH_TO_ELEMENT
            .sae_pwe_h2e = WPA3_SAE_PWE_HASH_TO_ELEMENT,
#elif CONFIG_ESP_WPA3_SAE_PWE_BOTH
            .sae_pwe_h2e = WPA3_SAE_PWE_BOTH,
#endif
        },
    };

    char nvs_ssid[32] = {0};
    char nvs_pass[64] = {0};
    if (get_stored_value("wifi_ssid", nvs_ssid, sizeof(nvs_ssid)) == ESP_OK &&
        get_stored_value("wifi_pass", nvs_pass, sizeof(nvs_pass)) == ESP_OK) {
        ESP_LOGI(TAG, "Using WiFi credentials from NVS");
        strncpy((char *)wifi_config.sta.ssid, nvs_ssid, sizeof(wifi_config.sta.ssid));
        strncpy((char *)wifi_config.sta.password, nvs_pass, sizeof(wifi_config.sta.password));
    } else {
        ESP_LOGI(TAG, "Using WiFi credentials from Kconfig");
        strncpy((char *)wifi_config.sta.ssid, CONFIG_ESP_WIFI_SSID, sizeof(wifi_config.sta.ssid));
        strncpy((char *)wifi_config.sta.password, CONFIG_ESP_WIFI_PASSWORD,
                sizeof(wifi_config.sta.password));
    }

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_LOGI(TAG, "wifi_init_sta finished.");
}

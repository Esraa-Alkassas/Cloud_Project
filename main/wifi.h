#pragma once

#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"

#define WIFI_CONNECTED_BIT BIT0

extern EventGroupHandle_t wifi_event_group;

esp_err_t get_stored_value(const char *key, char *out_val, size_t max_len);
esp_err_t store_value(const char *key, const char *val);
void      wifi_init_sta(void);

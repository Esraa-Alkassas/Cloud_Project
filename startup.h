#pragma once
#include "esp_ota_ops.h"

class start_System {
  public:
    void begin() {
        // FIXED: ESP_PARTITION_SUBTYPE_APP_FACTORY
        const esp_partition_t* factory = esp_partition_find_first(
            ESP_PARTITION_TYPE_APP, 
            ESP_PARTITION_SUBTYPE_APP_FACTORY, 
            NULL
        );
        
        if (factory != NULL) {
            esp_ota_set_boot_partition(factory);
        }
    }
};

// Create the global instance so students can just call TEC.begin();
start_System TEC;
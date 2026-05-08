idf_component_register(
    SRCS "main.c"
    INCLUDE_DIRS "."
    REQUIRES 
        detools            
        esp_partition      
        app_update         
        esp_http_client    
        nvs_flash 
        esp_wifi  
        esp_event 
        esp_netif 
        mbedtls            
        json               
        bootloader_support 
        driver
)
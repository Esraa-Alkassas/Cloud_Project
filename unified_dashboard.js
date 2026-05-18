let lastLogProcessed = "";

self.onInit = function() {
    const btn = document.getElementById('deploy-btn');
    btn.addEventListener('click', function() {
        self.ctx.controlApi.sendTwoWayRpcCommand("start_ota", {});
    });
};

self.onDataUpdated = function() {
    const data = self.ctx.data;
    const elements = {
        version: document.getElementById('fw-version'),
        strategy: document.getElementById('ota-strategy'),
        size: document.getElementById('ota-size'),
        flashTime: document.getElementById('flash-time'),
        banner: document.getElementById('status-banner'),
        btn: document.getElementById('deploy-btn'),
        logs: document.getElementById('log-window'),
        ledRed: document.getElementById('led-red'),
        ledGreen: document.getElementById('led-green'),
        ledBlue: document.getElementById('led-blue')
    };

    let updateAvailable = false;

    data.forEach(item => {
        const key = item.dataKey.name;
        if (!item.data || item.data.length === 0) return;
        
        const latestVal = item.data[item.data.length - 1][1];

        switch(key) {
            case 'red_led':
                toggleLed(elements.ledRed, 'led-red-on', latestVal);
                break;
            case 'green_led':
                toggleLed(elements.ledGreen, 'led-green-on', latestVal);
                break;
            case 'blue_led':
                toggleLed(elements.ledBlue, 'led-blue-on', latestVal);
                break;
            case 'fw_version':
                elements.version.innerText = latestVal;
                break;
            case 'strategy':
                elements.strategy.innerText = latestVal;
                break;
            case 'size':
                const sizeKb = (parseInt(latestVal) / 1024).toFixed(1);
                elements.size.innerText = sizeKb + " KB";
                break;
            case 'flash_time_ms':
                elements.flashTime.innerText = latestVal + " ms";
                break;
            case 'update_available':
                updateAvailable = (latestVal === "true" || latestVal === true);
                break;
            case 'tb_log':
                if (latestVal !== lastLogProcessed) {
                    addLog(elements.logs, latestVal);
                    lastLogProcessed = latestVal;
                }
                break;
        }
    });

    // Update UI State
    if (updateAvailable) {
        elements.banner.innerText = "NEW FIRMWARE DETECTED";
        elements.banner.classList.add('ready-text');
        elements.btn.disabled = false;
        elements.btn.classList.add('ready-btn');
        elements.btn.innerText = "DEPLOY FIRMWARE UPDATE";
    } else {
        elements.banner.innerText = "SYSTEM UP TO DATE";
        elements.banner.classList.remove('ready-text');
        elements.btn.disabled = true;
        elements.btn.classList.remove('ready-btn');
        elements.btn.innerText = "NO UPDATE AVAILABLE";
    }
};

function addLog(container, msg) {
    const time = new Date().toLocaleTimeString();
    const div = document.createElement('div');
    div.className = 'log-entry';
    div.innerHTML = `<span class="log-ts">[${time}]</span>${msg}`;
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
    
    // Keep only last 50 logs to prevent lag
    while (container.childNodes.length > 50) {
        container.removeChild(container.firstChild);
    }
}

function toggleLed(el, className, val) {
    if (val === 1 || val === "1" || val === true || val === "true") {
        el.classList.add(className);
    } else {
        el.classList.remove(className);
    }
}

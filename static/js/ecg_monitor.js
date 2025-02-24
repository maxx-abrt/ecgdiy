const charts = {
    'raw-ch1': null,
    'raw-ch2': null,
    'filtered-ch1': null,
    'filtered-ch2': null
};

function initializeCharts() {
    const chartConfigs = {
        'raw-ch1': {
            title: 'Canal 1 (Raw)',
            yaxis: { range: [-2, 2] }
        },
        'raw-ch2': {
            title: 'Canal 2 (Raw)',
            yaxis: { range: [-2, 2] }
        },
        'filtered-ch1': {
            title: 'Canal 1 (Filtered)',
            yaxis: { range: [-2, 2] }
        },
        'filtered-ch2': {
            title: 'Canal 2 (Filtered)',
            yaxis: { range: [-2, 2] }
        }
    };

    Object.entries(chartConfigs).forEach(([id, config]) => {
        charts[id] = createChart(id, config);
    });
}

function updateCharts(data) {
    Object.entries(charts).forEach(([id, chart]) => {
        Plotly.extendTraces(id, {
            y: [[data[id]]]
        }, [0], 1000);
    });
}

function updateData() {
    if (!isRecording) return;

    fetch('/api/ecg-data')
        .then(response => response.json())
        .then(data => {
            updateCharts(data);
            
            document.getElementById('heart-rate').textContent = 
                `Fréquence cardiaque: ${data.heart_rate.toFixed(1)} BPM`;
        });
}

function updateSystemStats() {
    fetch('/api/system-stats')
        .then(response => response.json())
        .then(stats => {
            document.getElementById('cpu-temp').textContent = 
                `Température CPU: ${stats.cpu_temp.toFixed(1)}°C`;
            document.getElementById('cpu-usage').textContent = 
                `Utilisation CPU: ${stats.cpu_usage}%`;
            document.getElementById('memory-usage').textContent = 
                `Mémoire: ${stats.memory_usage}%`;
            document.getElementById('uptime').textContent = 
                `Temps de fonctionnement: ${stats.uptime}`;
        });
}

function updateDebugInfo() {
    fetch('/api/debug-info')
        .then(response => response.json())
        .then(debug => {
            const debugInfo = document.getElementById('debug-info');
            debugInfo.innerHTML = `
                <h5>Débogage</h5>
                <div>État DRDY: ${debug.debug_info.drdy_status ? 'Actif' : 'Inactif'}</div>
                <div>État SPI: ${debug.debug_info.spi_status ? 'OK' : 'Erreur'}</div>
                <div>Qualité Signal: ${debug.debug_info.signal_quality}</div>
                <div>Dernière erreur: ${debug.debug_info.last_error || 'Aucune'}</div>
                <h6>Registres:</h6>
                <pre>${JSON.stringify(debug.debug_info.register_values, null, 2)}</pre>
                <h6>Données brutes:</h6>
                <pre>${JSON.stringify(debug.debug_info.raw_data, null, 2)}</pre>
            `;
        });
}

function toggleRecording() {
    isRecording = !isRecording;
    if (isRecording) {
        updateData();
    }
}

function exportData() {
    fetch('/api/ecg-data')
        .then(response => response.json())
        .then(data => {
            const blob = new Blob([JSON.stringify(data)], 
                                {type: 'application/json'});
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `ecg_data_${new Date().toISOString()}.json`;
            a.click();
        });
}

function setGain(gain) {
    fetch(`/api/set-gain/${gain}`)
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                console.log(`Gain changed to ${data.current_gain}`);
            }
        });
}

// Initialize
document.addEventListener('DOMContentLoaded', () => {
    initializeCharts();
    setInterval(updateData, 20);  // 50Hz update rate
    setInterval(updateSystemStats, 1000);  // 1Hz system stats update
    setInterval(updateDebugInfo, 1000);
}); 
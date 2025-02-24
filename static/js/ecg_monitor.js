let isRecording = false;
let updateInterval;
const MAX_POINTS = 200;

const charts = {
    'raw-ch1-chart': null,
    'raw-ch2-chart': null,
    'filtered-ch1-chart': null,
    'filtered-ch2-chart': null
};

function initializeApp() {
    initializeCharts();
    startDataCollection();
    
    // Démarrer les mises à jour périodiques
    setInterval(updateSystemStats, 2000);
    setInterval(updateDebugInfo, 1000);
}

function startDataCollection() {
    updateInterval = setInterval(() => {
        fetch('/api/data')
            .then(response => {
                if (!response.ok) throw new Error('Network response was not ok');
                return response.json();
            })
            .then(data => {
                Object.entries(charts).forEach(([id, chart]) => {
                    if (data[id]) {
                        Plotly.update(id, {
                            y: [data[id]],
                            x: [...Array(data[id].length)].map((_, i) => i)
                        });
                    }
                });
            })
            .catch(error => console.error('Erreur de mise à jour:', error));
    }, 100);
}

function initializeCharts() {
    const chartConfigs = {
        'raw-ch1-chart': {
            title: 'Canal 1 (Raw)',
            color: '#2196F3'
        },
        'raw-ch2-chart': {
            title: 'Canal 2 (Raw)',
            color: '#4CAF50'
        },
        'filtered-ch1-chart': {
            title: 'Canal 1 (Filtré)',
            color: '#FF9800'
        },
        'filtered-ch2-chart': {
            title: 'Canal 2 (Filtré)',
            color: '#9C27B0'
        }
    };

    Object.entries(chartConfigs).forEach(([id, config]) => {
        Plotly.newPlot(id, [{
            y: [],
            type: 'scatter',
            mode: 'lines',
            line: { color: config.color, width: 2 }
        }], {
            title: config.title,
            height: 250,
            margin: { t: 30, b: 30, l: 50, r: 20 },
            yaxis: { 
                range: [-2, 2],
                title: 'Voltage (mV)',
                gridcolor: '#f0f0f0'
            },
            xaxis: {
                title: 'Échantillons',
                showgrid: true,
                gridcolor: '#f0f0f0'
            },
            plot_bgcolor: 'white',
            paper_bgcolor: 'white'
        }, {
            responsive: true,
            displayModeBar: false
        });
    });
}

function updateCharts(data) {
    for (const [id, chart] of Object.entries(charts)) {
        if (data[id]) {
            Plotly.extendTraces(id, {
                y: [[data[id]]]
            }, [0], 1000);
        }
    }
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
    const recordButton = document.querySelector('#record-button');
    
    if (isRecording) {
        recordButton.textContent = 'Arrêter';
        recordButton.classList.replace('btn-primary', 'btn-danger');
        updateInterval = setInterval(updateData, 100);
    } else {
        recordButton.textContent = 'Démarrer';
        recordButton.classList.replace('btn-danger', 'btn-primary');
        clearInterval(updateInterval);
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

// Initialisation au chargement de la page
document.addEventListener('DOMContentLoaded', initializeApp); 
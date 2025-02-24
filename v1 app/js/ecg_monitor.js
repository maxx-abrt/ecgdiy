let isRecording = false;
let updateInterval;
const MAX_POINTS = 500;
const UPDATE_INTERVAL_MS = 50;

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
            title: 'ECG Canal 1 (Raw)',
            color: '#2196F3'
        },
        'filtered-ch1-chart': {
            title: 'ECG Canal 1 (Filtered)',
            color: '#4CAF50'
        },
        'raw-ch2-chart': {
            title: 'ECG Canal 2 (Raw)',
            color: '#FFC107'
        },
        'filtered-ch2-chart': {
            title: 'ECG Canal 2 (Filtered)',
            color: '#9C27B0'
        }
    };

    Object.entries(chartConfigs).forEach(([id, config]) => {
        Plotly.newPlot(id, [{
            y: [],
            type: 'scatter',
            mode: 'lines',
            line: { color: config.color, width: 2 },
            name: config.title
        }], {
            title: config.title,
            height: 250,
            margin: { t: 30, b: 30, l: 50, r: 20 },
            yaxis: {
                range: [-2, 2],
                title: 'Voltage (mV)'
            },
            xaxis: {
                showgrid: true,
                range: [0, MAX_POINTS]
            },
            plot_bgcolor: 'white',
            paper_bgcolor: 'white'
        }, {
            responsive: true,
            displayModeBar: false
        });
    });
}

function fetchData() {
    return fetch('/api/data', {
        method: 'GET',
        headers: {
            'Accept': 'application/json',
            'Content-Type': 'application/json'
        },
        credentials: 'same-origin'
    })
    .then(response => {
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }
        return response.json();
    });
}

function updateCharts() {
    fetch('/api/data')
        .then(response => response.json())
        .then(data => {
            Object.entries(charts).forEach(([id, chart]) => {
                if (data[id]) {
                    const update = {
                        y: [data[id]],
                        x: [...Array(data[id].length)].map((_, i) => i)
                    };
                    Plotly.update(id, update, {
                        xaxis: {
                            range: [Math.max(0, data[id].length - MAX_POINTS), 
                                   data[id].length]
                        }
                    });
                }
            });
        })
        .catch(console.error);
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
        .then(data => {
            if (!data.debug_info) return;
            
            const debugInfo = document.getElementById('debug-info');
            debugInfo.innerHTML = `
                <h5>Débogage</h5>
                <div>État DRDY: ${data.debug_info.drdy_status ? 'Actif' : 'Inactif'}</div>
                <div>État SPI: ${data.debug_info.spi_status ? 'OK' : 'Erreur'}</div>
                <div>Qualité Signal: ${data.debug_info.signal_quality}</div>
                <div>Dernière erreur: ${data.debug_info.last_error || 'Aucune'}</div>
            `;
        })
        .catch(error => {
            console.error('Erreur debug:', error);
            document.getElementById('debug-info').innerHTML = '<div>Erreur de mise à jour debug</div>';
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

// Démarrage de la mise à jour continue
setInterval(updateCharts, UPDATE_INTERVAL_MS); 
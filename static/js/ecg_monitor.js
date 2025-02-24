let ecgChart = null;
let isRecording = true;

function initializeChart() {
    const trace = {
        y: [],
        mode: 'lines',
        line: {
            color: '#00ff00',
            width: 1
        },
        name: 'ECG'
    };

    const layout = {
        title: 'Monitoring ECG en Temps Réel',
        xaxis: {
            title: 'Échantillons',
            showgrid: true
        },
        yaxis: {
            title: 'Amplitude (mV)',
            range: [-2, 2],
            showgrid: true
        },
        plot_bgcolor: '#111',
        paper_bgcolor: '#111',
        font: {
            color: '#fff'
        },
        margin: {
            l: 50,
            r: 50,
            b: 50,
            t: 50,
            pad: 4
        }
    };

    Plotly.newPlot('ecg-chart', [trace], layout);
    ecgChart = document.getElementById('ecg-chart');
}

function updateData() {
    if (!isRecording) return;

    fetch('/api/ecg-data')
        .then(response => response.json())
        .then(data => {
            Plotly.update('ecg-chart', {
                y: [data.ecg_data]
            });
            
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

// Initialize
document.addEventListener('DOMContentLoaded', () => {
    initializeChart();
    setInterval(updateData, 20);  // 50Hz update rate
    setInterval(updateSystemStats, 1000);  // 1Hz system stats update
}); 
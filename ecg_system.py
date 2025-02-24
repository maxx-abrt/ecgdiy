import spidev
import RPi.GPIO as GPIO
import numpy as np
import time
from flask import Flask, render_template, Response, jsonify
import json
from threading import Thread, Lock
import psutil
import datetime
from collections import deque
from flask_cors import CORS

# Configuration des broches selon Data.txt aand ext
class Configuration:
    MOSI_PIN = 10  # GPIO10 (Pin 19)
    MISO_PIN = 9   # GPIO9 (Pin 21)
    SCK_PIN = 11   # GPIO11 (Pin 23)
    CS_PIN = 8     # GPIO8 (Pin 24)
    DRDY_PIN = 17  # GPIO17 (Pin 11)
    PWDN_PIN = 27  # GPIO27 (Pin 13)
    START_PIN = 22 # GPIO22 (Pin 15)


class ECGSystem:
    WREG = 0x40  # Define WREG as 0x40

    def __init__(self):
        self.spi = spidev.SpiDev()
        self.spi.open(0, 0)
        self.spi.max_speed_hz = 1000000
        self.spi.mode = 1
        
        self.signal_buffers = {
            'raw_ch1': deque(maxlen=5000),
            'raw_ch2': deque(maxlen=5000),
            'filtered_ch1': deque(maxlen=5000),
            'filtered_ch2': deque(maxlen=5000)
        }
        
        # Coefficients de filtrage (repris du code Arduino)
        self.filter_coeffs = np.array([
            -72, 122, -31, -99, 117, 0, -121, 105, 34,
            -137, 84, 70, -146, 55, 104, -147, 20, 135
            # ... (utiliser les mêmes coefficients que dans le code Arduino)
        ]) / 32768.0  # Normalisation
        
        self.filter_state_ch1 = np.zeros(len(self.filter_coeffs))
        self.filter_state_ch2 = np.zeros(len(self.filter_coeffs))
        
        # Configuration du logging
        self.log_file = 'ecg_data.json'
        self.last_log_time = time.time()
        self.log_interval = 1.0  # Intervalle en secondes
        
        # Ajout des paramètres de sensibilité
        self.gain_settings = {
            '1x': 0x00,
            '2x': 0x10,
            '3x': 0x20,
            '4x': 0x30,
            '6x': 0x40,
            '8x': 0x50,
            '12x': 0x60
        }
        self.current_gain = '6x'  # Gain par défaut
        
        self.data_buffer = deque(maxlen=5000)
        self.heart_rate_buffer = deque(maxlen=10)
        self.data_lock = Lock()
        
        self.last_peak_time = time.time()
        self.heart_rate = 0
        
        # System stats initialization
        self.system_stats = {
            'cpu_temp': 0,
            'cpu_usage': 0,
            'memory_usage': 0,
            'start_time': datetime.datetime.now(),
            'samples_collected': 0
        }
        
        # Initialisation de debug_info
        self.debug_info = {
            'raw_data': [],
            'spi_status': False,
            'drdy_status': False,
            'register_values': {},
            'last_error': None,
            'signal_quality': 'Unknown'
        }
        
        # Initialize hardware after all variables are set
        self.setup_gpio()
        self.initialize_ads1292r()

    def get_cpu_temperature(self):
        try:
            with open('/sys/class/thermal/thermal_zone0/temp', 'r') as temp_file:
                temp = float(temp_file.read()) / 1000.0
            return temp
        except:
            return 0

    def update_system_stats(self):
        self.system_stats.update({
            'cpu_temp': self.get_cpu_temperature(),
            'cpu_usage': psutil.cpu_percent(),
            'memory_usage': psutil.virtual_memory().percent,
            'samples_collected': len(self.data_buffer),
            'uptime': str(datetime.datetime.now() - self.system_stats['start_time'])
        })

    def calculate_heart_rate(self, new_value):
        threshold = 1.0
        if new_value > threshold:
            current_time = time.time()
            time_diff = current_time - self.last_peak_time
            if time_diff > 0.4:  # Éviter les faux positifs
                heart_rate = 60 / time_diff
                self.heart_rate_buffer.append(heart_rate)
                self.heart_rate = np.mean(self.heart_rate_buffer)
                self.last_peak_time = current_time

    def setup_gpio(self):
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(Configuration.DRDY_PIN, GPIO.IN)
        GPIO.setup(Configuration.START_PIN, GPIO.OUT)
        GPIO.setup(Configuration.PWDN_PIN, GPIO.OUT)
        GPIO.setup(Configuration.CS_PIN, GPIO.OUT)
        
        GPIO.output(Configuration.PWDN_PIN, GPIO.HIGH)
        GPIO.output(Configuration.START_PIN, GPIO.LOW)
        GPIO.output(Configuration.CS_PIN, GPIO.HIGH)
        
    def write_register(self, address, value):
        GPIO.output(Configuration.CS_PIN, GPIO.LOW)
        self.spi.xfer2([self.WREG | address, 0x00, value])
        GPIO.output(Configuration.CS_PIN, GPIO.HIGH)
        
    def initialize_ads1292r(self):
        try:
            # Reset hardware complet
            GPIO.output(Configuration.PWDN_PIN, GPIO.LOW)
            time.sleep(0.1)
            GPIO.output(Configuration.PWDN_PIN, GPIO.HIGH)
            time.sleep(0.1)
            
            # Stop data continuous
            GPIO.output(Configuration.CS_PIN, GPIO.LOW)
            self.spi.xfer2([0x11])  # SDATAC command
            GPIO.output(Configuration.CS_PIN, GPIO.HIGH)
            time.sleep(0.05)
            
            # Configuration registres avec vérification
            registers = [
                (0x01, 0x00),  # CONFIG1: 125 SPS
                (0x02, 0xA0),  # CONFIG2: Test signals disabled
                (0x03, 0xE0),  # LOFF: Lead-off detection off
                (0x04, 0x60),  # CH1SET: Gain 12, normal electrode input
                (0x05, 0x60),  # CH2SET: Gain 12, normal electrode input
                (0x06, 0x2C),  # RLD_SENS
                (0x07, 0x00),  # LOFF_SENS
                (0x08, 0x00),  # LOFF_STAT
                (0x09, 0xF2),  # RESP1: Resp modulation/demod enabled
                (0x0A, 0x03)   # RESP2: Resp modulation frequency
            ]
            
            for reg_addr, reg_value in registers:
                for _ in range(3):  # 3 tentatives par registre
                    if self._write_verify_register(reg_addr, reg_value):
                        break
                    time.sleep(0.01)
            
            # Démarrer l'acquisition continue
            GPIO.output(Configuration.CS_PIN, GPIO.LOW)
            self.spi.xfer2([0x10])  # RDATAC command
            GPIO.output(Configuration.CS_PIN, GPIO.HIGH)
            time.sleep(0.01)
            
            # Start conversion
            GPIO.output(Configuration.START_PIN, GPIO.HIGH)
            
            return True
            
        except Exception as e:
            self.debug_info['last_error'] = f"Init error: {str(e)}"
            return False

    def _write_verify_register(self, reg_addr, reg_value):
        try:
            # Write register
            GPIO.output(Configuration.CS_PIN, GPIO.LOW)
            self.spi.xfer2([0x40 | reg_addr, 0x00, reg_value])
            GPIO.output(Configuration.CS_PIN, GPIO.HIGH)
            time.sleep(0.001)
            
            # Read back for verification
            GPIO.output(Configuration.CS_PIN, GPIO.LOW)
            result = self.spi.xfer2([0x20 | reg_addr, 0x00, 0x00])
            GPIO.output(Configuration.CS_PIN, GPIO.HIGH)
            
            if result[2] != reg_value:
                self.debug_info['last_error'] = f"Register write failed - 0x{reg_addr:X}: expected 0x{reg_value:X}, got 0x{result[2]:X}"
                return False
            
            return True
            
        except Exception as e:
            self.debug_info['last_error'] = f"Register write error: {str(e)}"
            return False

    def debug_registers(self):
        try:
            # Lecture des registres importants
            registers_to_check = {
                'CONFIG1': 0x01,
                'CONFIG2': 0x02,
                'LOFF': 0x03,
                'CH1SET': 0x04,
                'CH2SET': 0x05,
                'RLD_SENS': 0x06
            }
            
            for name, addr in registers_to_check.items():
                GPIO.output(Configuration.CS_PIN, GPIO.LOW)
                data = self.spi.xfer2([0x20 | addr, 0x00])  # 0x20 pour lire
                GPIO.output(Configuration.CS_PIN, GPIO.HIGH)
                self.debug_info['register_values'][name] = hex(data[1])
                
            return True
        except Exception as e:
            self.debug_info['last_error'] = str(e)
            return False

    def check_signal_quality(self, data_sample):
        if data_sample is None:
            return "Pas de signal"
        
        # Vérification de la plage du signal
        if abs(data_sample) < 0.1:
            return "Signal faible"
        elif abs(data_sample) > 2.0:
            return "Signal saturé"
        
        return "OK"

    def read_data(self):
        try:
            if GPIO.input(Configuration.DRDY_PIN) == 0:
                GPIO.output(Configuration.CS_PIN, GPIO.LOW)
                time.sleep(0.0001)
                
                data = self.spi.xfer2([0x00] * 9)
                GPIO.output(Configuration.CS_PIN, GPIO.HIGH)
                
                status = data[0]
                ch1_data = self._convert_24bit_to_int(data[1:4])
                ch2_data = self._convert_24bit_to_int(data[4:7])
                
                # Ajustement de l'échelle et conversion en mV
                vref = 2.4  # Tension de référence
                gain_factor = int(self.current_gain.replace('x', ''))
                
                ch1_mv = (ch1_data * vref) / (gain_factor * 0x7FFFFF)
                ch2_mv = (ch2_data * vref) / (gain_factor * 0x7FFFFF)
                
                self._process_and_store_data((ch1_mv, ch2_mv))
                self.debug_info['signal_quality'] = self.check_signal_quality(ch1_mv)
                
                return ch1_mv, ch2_mv
                
        except Exception as e:
            self.debug_info['last_error'] = f"Read error: {str(e)}"
            return None

    def apply_filter(self, data, state):
        # Implémentation du filtre FIR
        filtered = np.convolve(data, self.filter_coeffs, mode='valid')
        return filtered[-1]

    def _process_and_store_data(self, data):
        if data is None:
            return
        
        ch1, ch2 = data
        with self.data_lock:
            # Stockage données brutes
            self.signal_buffers['raw_ch1'].append(ch1)
            self.signal_buffers['raw_ch2'].append(ch2)
            
            # Application du filtrage
            filtered_ch1 = self.apply_filter(
                np.array(list(self.signal_buffers['raw_ch1'])[-161:]), 
                self.filter_state_ch1
            )
            filtered_ch2 = self.apply_filter(
                np.array(list(self.signal_buffers['raw_ch2'])[-161:]), 
                self.filter_state_ch2
            )
            
            self.signal_buffers['filtered_ch1'].append(filtered_ch1)
            self.signal_buffers['filtered_ch2'].append(filtered_ch2)
            
            # Détection QRS et calcul du rythme cardiaque
            self.detect_qrs_and_calculate_hr(filtered_ch1)

    def _convert_24bit_to_int(self, data_bytes):
        # Conversion des données 24-bit en entier signé
        value = (data_bytes[0] << 16) | (data_bytes[1] << 8) | data_bytes[2]
        if value & 0x800000:
            value -= 0x1000000
        return value

    def set_gain(self, gain):
        if gain not in self.gain_settings:
            return False
        
        self.current_gain = gain
        # Mettre à jour les deux canaux
        success1 = self._write_verify_register(0x04, self.gain_settings[gain])
        success2 = self._write_verify_register(0x05, self.gain_settings[gain])
        return success1 and success2

    def detect_qrs_and_calculate_hr(self, filtered_data):
        # Implémentation simple de la détection QRS
        threshold = 0.5  # Ajustez ce seuil selon vos besoins
        peaks = []

        for i in range(1, len(filtered_data) - 1):
            if filtered_data[i] > threshold and filtered_data[i] > filtered_data[i - 1] and filtered_data[i] > filtered_data[i + 1]:
                peaks.append(i)

        # Calculer le rythme cardiaque basé sur les pics détectés
        if len(peaks) > 1:
            intervals = np.diff(peaks)  # Intervalles entre les pics
            if len(intervals) > 0:
                avg_interval = np.mean(intervals) * 0.002  # Convertir en secondes (500Hz)
                if avg_interval > 0:
                    self.heart_rate = 60 / avg_interval  # Calculer le rythme cardiaque
                    self.heart_rate_buffer.append(self.heart_rate)
                    if len(self.heart_rate_buffer) > 10:
                        self.heart_rate_buffer.popleft()  # Limiter la taille du buffer

# Application Flask
app = Flask(__name__)
CORS(app, resources={
    r"/api/*": {
        "origins": "*",
        "methods": ["GET", "POST"],
        "allow_headers": ["Content-Type"]
    }
})
ecg_system = ECGSystem()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/system-stats')
def system_stats():
    ecg_system.update_system_stats()
    return jsonify(ecg_system.system_stats)

@app.route('/api/ecg-data')
def ecg_data():
    with ecg_system.data_lock:
        data = {
            'ecg_data': list(ecg_system.data_buffer),
            'heart_rate': ecg_system.heart_rate
        }
    return jsonify(data)

@app.route('/api/debug-info')
def get_debug_info():
    try:
        return jsonify({
            'debug_info': {
                'drdy_status': GPIO.input(Configuration.DRDY_PIN) == 0,
                'spi_status': ecg_system.debug_info['spi_status'],
                'signal_quality': ecg_system.debug_info['signal_quality'],
                'last_error': ecg_system.debug_info['last_error'],
                'register_values': ecg_system.debug_info['register_values'],
                'raw_data': list(ecg_system.signal_buffers['raw_ch1'])[-10:]  # Derniers points
            }
        })
    except Exception as e:
        return jsonify({
            'debug_info': {
                'drdy_status': False,
                'spi_status': False,
                'signal_quality': 'Error',
                'last_error': str(e),
                'register_values': {},
                'raw_data': []
            }
        })

@app.route('/api/set-gain/<gain>')
def set_gain_route(gain):
    success = ecg_system.set_gain(gain)
    return jsonify({'success': success, 'current_gain': ecg_system.current_gain})

@app.route('/api/data')
def get_data():
    with ecg_system.data_lock:
        return jsonify({
            'raw-ch1-chart': list(ecg_system.signal_buffers['raw_ch1'])[-100:],
            'raw-ch2-chart': list(ecg_system.signal_buffers['raw_ch2'])[-100:],
            'filtered-ch1-chart': list(ecg_system.signal_buffers['filtered_ch1'])[-100:],
            'filtered-ch2-chart': list(ecg_system.signal_buffers['filtered_ch2'])[-100:]
        })

@app.route('/api/raw-signals')
def get_raw_signals():
    return jsonify({
        'drdy': GPIO.input(Configuration.DRDY_PIN),
        'mosi': GPIO.input(Configuration.MOSI_PIN),
        'miso': GPIO.input(Configuration.MISO_PIN),
        'sck': GPIO.input(Configuration.SCK_PIN),
        'cs': GPIO.input(Configuration.CS_PIN)
    })

def data_collection_thread():
    while True:
        ecg_system.read_data()
        time.sleep(0.002)  # 500Hz sampling rate

if __name__ == '__main__':
    Thread(target=data_collection_thread, daemon=True).start()
    app.run(host='0.0.0.0', port=5000, threaded=True)
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
        # Ajout des buffers pour différents signaux
        self.signal_buffers = {
            'raw_ch1': deque(maxlen=5000),
            'raw_ch2': deque(maxlen=5000),
            'filtered_ch1': deque(maxlen=5000),
            'filtered_ch2': deque(maxlen=5000),
            'test_signal': deque(maxlen=5000),
            'rld_signal': deque(maxlen=5000)
        }
        
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
        
        # SPI setup
        self.spi = spidev.SpiDev()
        self.spi.open(0, 0)
        self.spi.max_speed_hz = 500000  # Reduced to 500kHz
        self.spi.mode = 1                # CPOL=0, CPHA=1
        self.spi.bits_per_word = 8
        self.spi.lsbfirst = False
        self.spi.cshigh = False         # CS active low
        
        
        # Initialize debug_info first
        self.debug_info = {
            'raw_data': [],
            'spi_status': False,
            'drdy_status': False,
            'register_values': {},
            'last_error': None,
            'signal_quality': 'Unknown'
        }
        
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
        # Reset complet et plus long
        GPIO.output(Configuration.START_PIN, GPIO.LOW)
        GPIO.output(Configuration.CS_PIN, GPIO.HIGH)
        time.sleep(0.2)
        
        # Reset hardware
        GPIO.output(Configuration.PWDN_PIN, GPIO.LOW)
        time.sleep(1.0)  # Augmentation du temps de reset
        GPIO.output(Configuration.PWDN_PIN, GPIO.HIGH)
        time.sleep(0.5)  # Attente plus longue après reset
        
        # Stop Data Continuous
        GPIO.output(Configuration.CS_PIN, GPIO.LOW)
        time.sleep(0.01)
        self.spi.xfer2([0x11])  # SDATAC
        time.sleep(0.01)
        GPIO.output(Configuration.CS_PIN, GPIO.HIGH)
        time.sleep(0.05)
        
        # Configuration avec vérification
        configs = [
            (0x01, 0x03),  # CONFIG1: 1kSPS, continuous mode
            (0x02, 0xA0),  # CONFIG2: Test signal square wave
            (0x03, 0xE0),  # LOFF: Lead-off comp off
            (0x04, self.gain_settings[self.current_gain]),  # CH1SET: PGA gain
            (0x05, self.gain_settings[self.current_gain]),  # CH2SET: PGA gain
            (0x06, 0x2C),  # RLD_SENS
            (0x07, 0x00),  # LOFF_SENS
            (0x08, 0x00),  # LOFF_STAT
            (0x09, 0x02),  # RESP1: Internal clock
            (0x0A, 0x03),  # RESP2: Internal oscillator
        ]
        
        # Méthode de vérification améliorée
        for addr, value in configs:
            success = self._write_verify_register(addr, value)
            if not success:
                return False
        
        return self._start_continuous_mode()

    def _write_verify_register(self, addr, value):
        for _ in range(3):  # 3 tentatives
            # Reset CS avec délai
            GPIO.output(Configuration.CS_PIN, GPIO.HIGH)
            time.sleep(0.01)
            
            # Écriture
            GPIO.output(Configuration.CS_PIN, GPIO.LOW)
            time.sleep(0.01)
            self.spi.xfer2([self.WREG | addr, 0x00, value])
            time.sleep(0.01)
            GPIO.output(Configuration.CS_PIN, GPIO.HIGH)
            time.sleep(0.01)
            
            # Lecture de vérification
            GPIO.output(Configuration.CS_PIN, GPIO.LOW)
            time.sleep(0.01)
            read_data = self.spi.xfer2([0x20 | addr, 0x00, 0x00])
            time.sleep(0.01)
            GPIO.output(Configuration.CS_PIN, GPIO.HIGH)
            
            if read_data[2] == value:
                self.debug_info['register_values'][hex(addr)] = hex(value)
                return True
            
        self.debug_info['last_error'] = f"Register write failed - {hex(addr)}: expected {hex(value)}, got {hex(read_data[2])}"
        return False

    def _start_continuous_mode(self):
        # Send START command
        GPIO.output(Configuration.CS_PIN, GPIO.LOW)
        self.spi.xfer2([0x08])  # START command
        GPIO.output(Configuration.CS_PIN, GPIO.HIGH)
        time.sleep(0.01)
        
        # Send RDATAC command
        GPIO.output(Configuration.CS_PIN, GPIO.LOW)
        self.spi.xfer2([0x10])  # RDATAC command
        GPIO.output(Configuration.CS_PIN, GPIO.HIGH)
        time.sleep(0.01)
        
        # Start data conversion
        GPIO.output(Configuration.START_PIN, GPIO.HIGH)
        time.sleep(0.1)
        
        return True

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
                data = self._read_raw_data()
                if data:
                    self._process_and_store_data(data)
                return data
        except Exception as e:
            self.debug_info['last_error'] = f"Read error: {str(e)}"
            return None

    def _read_raw_data(self):
        if GPIO.input(Configuration.DRDY_PIN) == 0:  # DRDY is active LOW
            self.debug_info['drdy_status'] = True
            
            # Read data
            GPIO.output(Configuration.CS_PIN, GPIO.LOW)
            # Read 9 bytes (status + 24-bit for each channel)
            data = self.spi.xfer2([0x00] * 9)
            GPIO.output(Configuration.CS_PIN, GPIO.HIGH)
            
            self.debug_info['raw_data'] = [hex(x) for x in data]
            
            # Check status byte
            status = data[0]
            if status == 0xFF:
                self.debug_info['last_error'] = "Communication error - check SPI connections"
                return None
                
            if status & 0xF0:
                self.debug_info['last_error'] = f"Device error: {hex(status)}"
                return None
            
            # Convert data
            ch1 = self._convert_24bit_to_int(data[3:6]) * 2.42 / 0x7FFFFF
            ch2 = self._convert_24bit_to_int(data[6:9]) * 2.42 / 0x7FFFFF
            
            self.debug_info['signal_quality'] = self.check_signal_quality(ch1)
            self.debug_info['spi_status'] = True
            
            return ch1, ch2
        else:
            self.debug_info['drdy_status'] = False
            return None

    def _process_and_store_data(self, data):
        if data is None:
            return
        
        ch1, ch2 = data
        with self.data_lock:
            # Stockage des données brutes
            self.signal_buffers['raw_ch1'].append(ch1)
            self.signal_buffers['raw_ch2'].append(ch2)
            
            # Filtrage simple (moyenne mobile)
            filtered_ch1 = np.mean(list(self.signal_buffers['raw_ch1'])[-10:])
            filtered_ch2 = np.mean(list(self.signal_buffers['raw_ch2'])[-10:])
            
            self.signal_buffers['filtered_ch1'].append(filtered_ch1)
            self.signal_buffers['filtered_ch2'].append(filtered_ch2)
            
            # Calcul du rythme cardiaque
            self.calculate_heart_rate(filtered_ch1)

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

# Application Flask
app = Flask(__name__)
CORS(app)
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
def debug_info():
    ecg_system.debug_registers()
    debug_data = {
        'debug_info': ecg_system.debug_info,
        'system_stats': ecg_system.system_stats,
        'buffer_size': len(ecg_system.data_buffer),
        'heart_rate_buffer': list(ecg_system.heart_rate_buffer)
    }
    return jsonify(debug_data)

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

def data_collection_thread():
    while True:
        ecg_system.read_data()
        time.sleep(0.002)  # 500Hz sampling rate

if __name__ == '__main__':
    Thread(target=data_collection_thread, daemon=True).start()
    app.run(host='0.0.0.0', port=5000, threaded=True)
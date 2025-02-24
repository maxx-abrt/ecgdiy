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

# Configuration des broches selon Data.txt
class Configuration:
    MOSI_PIN = 10  # GPIO10 (Pin 19)
    MISO_PIN = 9   # GPIO9 (Pin 21)
    SCK_PIN = 11   # GPIO11 (Pin 23)
    CS_PIN = 8     # GPIO8 (Pin 24)
    DRDY_PIN = 17  # GPIO17 (Pin 11)
    PWDN_PIN = 27  # GPIO27 (Pin 13)
    START_PIN = 22 # GPIO22 (Pin 15)

class ECGSystem:
    def __init__(self):
        self.spi = spidev.SpiDev()
        self.spi.open(0, 0)
        self.spi.max_speed_hz = 1000000
        self.spi.mode = 1
        
        self.data_buffer = deque(maxlen=5000)
        self.heart_rate_buffer = deque(maxlen=10)
        self.data_lock = Lock()
        
        self.last_peak_time = time.time()
        self.heart_rate = 0
        self.setup_gpio()
        self.initialize_ads1292r()
        
        # Statistiques système
        self.system_stats = {
            'cpu_temp': 0,
            'cpu_usage': 0,
            'memory_usage': 0,
            'start_time': datetime.datetime.now(),
            'samples_collected': 0
        }

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
        self.spi.xfer2([WREG | address, 0x00, value])
        GPIO.output(Configuration.CS_PIN, GPIO.HIGH)
        
    def initialize_ads1292r(self):
        # Reset
        GPIO.output(Configuration.PWDN_PIN, GPIO.LOW)
        time.sleep(0.1)
        GPIO.output(Configuration.PWDN_PIN, GPIO.HIGH)
        time.sleep(0.1)
        
        # Configuration des registres
        self.write_register(0x01, 0x00)  # CONFIG1: HR mode
        self.write_register(0x02, 0xE0)  # CONFIG2: Test signals
        self.write_register(0x03, 0xF0)  # LOFF: Lead-off detection off
        self.write_register(0x04, 0x00)  # CH1SET: Gain 6, normal electrode input
        self.write_register(0x05, 0x00)  # CH2SET: Gain 6, normal electrode input
        self.write_register(0x06, 0x2C)  # RLD_SENS: RLD connected to positive side
        self.write_register(0x07, 0x00)  # LOFF_SENS: Lead-off detection disabled
        self.write_register(0x08, 0x00)  # LOFF_STAT: Lead-off status
        
        # Démarrage des conversions
        GPIO.output(Configuration.START_PIN, GPIO.HIGH)
        
    def read_data(self):
        if GPIO.input(Configuration.DRDY_PIN) == 0:
            GPIO.output(Configuration.CS_PIN, GPIO.LOW)
            data = self.spi.xfer2([0x00] * 9)
            GPIO.output(Configuration.CS_PIN, GPIO.HIGH)
            
            # Conversion des données
            status = data[0]
            ch1 = ((data[3] << 16) | (data[4] << 8) | data[5]) * 2.42 / 0x7FFFFF
            ch2 = ((data[6] << 16) | (data[7] << 8) | data[8]) * 2.42 / 0x7FFFFF
            
            with self.data_lock:
                self.data_buffer.append(ch1)
                if len(self.data_buffer) > 1000:
                    self.data_buffer.pop(0)
            
            return ch1, ch2
        return None, None

# Application Flask
app = Flask(__name__)
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

def data_collection_thread():
    while True:
        ecg_system.read_data()
        time.sleep(0.002)  # 500Hz sampling rate

if __name__ == '__main__':
    Thread(target=data_collection_thread, daemon=True).start()
    app.run(host='0.0.0.0', port=5000, threaded=True)
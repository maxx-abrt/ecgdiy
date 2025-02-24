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
        # Reset sequence plus strict
        GPIO.output(Configuration.START_PIN, GPIO.LOW)
        GPIO.output(Configuration.CS_PIN, GPIO.HIGH)
        time.sleep(0.1)
        
        # Reset pulse
        GPIO.output(Configuration.PWDN_PIN, GPIO.LOW)
        time.sleep(0.1)
        GPIO.output(Configuration.PWDN_PIN, GPIO.HIGH)
        time.sleep(0.1)  # Wait for device to stabilize
        
        # Send SDATAC command with verification
        GPIO.output(Configuration.CS_PIN, GPIO.LOW)
        self.spi.xfer2([0x11])  # SDATAC command
        GPIO.output(Configuration.CS_PIN, GPIO.HIGH)
        time.sleep(0.01)
        
        # Configuration des registres avec vérification stricte
        configs = [
            (0x01, 0x03),  # CONFIG1: 1kSPS, continuous mode
            (0x02, 0xA0),  # CONFIG2: Internal test signal
            (0x03, 0xE0),  # LOFF: Lead-off comp off, DC lead-off
            (0x04, 0x60),  # CH1SET: PGA gain 6, test signal
            (0x05, 0x60),  # CH2SET: PGA gain 6, test signal
            (0x06, 0x2C),  # RLD_SENS
            (0x07, 0x00),  # LOFF_SENS
            (0x08, 0x00),  # LOFF_STAT
        ]
        
        for addr, value in configs:
            retry_count = 0
            while retry_count < 3:  # Try up to 3 times for each register
                # Write register
                GPIO.output(Configuration.CS_PIN, GPIO.LOW)
                time.sleep(0.001)  # Add small delay
                self.spi.xfer2([self.WREG | addr, 0x00, value])
                time.sleep(0.001)  # Add small delay
                GPIO.output(Configuration.CS_PIN, GPIO.HIGH)
                time.sleep(0.001)
                
                # Verify register
                GPIO.output(Configuration.CS_PIN, GPIO.LOW)
                time.sleep(0.001)
                read_data = self.spi.xfer2([0x20 | addr, 0x00, 0x00])
                time.sleep(0.001)
                GPIO.output(Configuration.CS_PIN, GPIO.HIGH)
                
                if read_data[2] == value:
                    break
                retry_count += 1
                time.sleep(0.01)  # Wait before retry
            
            if retry_count == 3:
                self.debug_info['last_error'] = f"Register write failed after 3 attempts - {hex(addr)}: expected {hex(value)}, got {hex(read_data[2])}"
                return False
        
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
                    return None, None
                    
                if status & 0xF0:
                    self.debug_info['last_error'] = f"Device error: {hex(status)}"
                    return None, None
                
                # Convert data
                ch1 = self._convert_24bit_to_int(data[3:6]) * 2.42 / 0x7FFFFF
                ch2 = self._convert_24bit_to_int(data[6:9]) * 2.42 / 0x7FFFFF
                
                self.debug_info['signal_quality'] = self.check_signal_quality(ch1)
                self.debug_info['spi_status'] = True
                
                with self.data_lock:
                    self.data_buffer.append(ch1)
                    self.calculate_heart_rate(ch1)
                
                return ch1, ch2
            else:
                self.debug_info['drdy_status'] = False
                return None, None
            
        except Exception as e:
            self.debug_info['last_error'] = f"Read error: {str(e)}"
            self.debug_info['spi_status'] = False
            return None, None

    def _convert_24bit_to_int(self, data_bytes):
        # Conversion des données 24-bit en entier signé
        value = (data_bytes[0] << 16) | (data_bytes[1] << 8) | data_bytes[2]
        if value & 0x800000:
            value -= 0x1000000
        return value

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

def data_collection_thread():
    while True:
        ecg_system.read_data()
        time.sleep(0.002)  # 500Hz sampling rate

if __name__ == '__main__':
    Thread(target=data_collection_thread, daemon=True).start()
    app.run(host='0.0.0.0', port=5000, threaded=True)
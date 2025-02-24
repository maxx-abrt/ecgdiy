import logging
import time
import numpy as np
from functools import wraps
from flask import Flask, render_template, jsonify
from flask_socketio import SocketIO
import spidev
import RPi.GPIO as GPIO
from scipy.signal import butter, lfilter, iirnotch
import json
import psutil
from threading import Lock

# Configuration
CONFIG = {
    "hardware": {
        "spi_bus": 0,
        "spi_device": 0,
        "gpio": {"drdy": 24, "start": 25, "reset": 23, "cs": 8},
        "sample_rate": 500,
        "expected_device_id": 0x73
    },
    "filters": {
        "bandpass": [0.5, 40.0],
        "notch_freq": 50.0,
        "notch_q": 30.0
    },
    "buffer": {
        "size": 1000,
        "warning_threshold": 0.8
    },
    "system": {
        "max_retries": 5,
        "retry_delay": 0.1,
        "max_voltage": 4.5,
        "safe_gain_range": (1, 12)
    }
}

# Initialize Flask and SocketIO
app = Flask(__name__)
app.config['SECRET_KEY'] = 'ecg_secret!'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('ecg_monitor.log'),
        logging.StreamHandler()
    ]
)

def handle_errors(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as e:
            logging.error(f"Error in {f.__name__}: {str(e)}", exc_info=True)
            socketio.emit('system_error', {'message': str(e)})
            return None
    return wrapper

class ECGSensor:
    _instance = None
    _lock = Lock()

    def __new__(cls):
        with cls._lock:
            if not cls._instance:
                cls._instance = super().__new__(cls)
                cls._instance._initialize()
            return cls._instance

    def _initialize(self):
        self.running = False
        self.buffer = np.zeros(CONFIG['buffer']['size'])
        self.filter_state = None
        self.notch_state = None
        self._init_hardware()
        self._init_filters()
        self._last_heartbeat = time.time()
        self.data_lock = Lock()

    @handle_errors
    def _init_hardware(self):
        try:
            GPIO.setmode(GPIO.BCM)
            for pin in CONFIG['hardware']['gpio'].values():
                GPIO.setup(pin, GPIO.OUT if pin != CONFIG['hardware']['gpio']['drdy'] else GPIO.IN)

            self.spi = spidev.SpiDev()
            self._spi_connect()
            self._verify_device()
            self._configure_sensor()
            logging.info("Hardware initialized successfully")

        except Exception as e:
            self._emergency_shutdown()
            raise RuntimeError(f"Hardware init failed: {str(e)}")

    def _spi_connect(self, retries=CONFIG['system']['max_retries']):
        for attempt in range(retries):
            try:
                self.spi.open(CONFIG['hardware']['spi_bus'], CONFIG['hardware']['spi_device'])
                self.spi.max_speed_hz = 2000000
                self.spi.mode = 0b01
                return
            except Exception as e:
                if attempt == retries - 1:
                    raise
                time.sleep(CONFIG['system']['retry_delay'])
                logging.warning(f"SPI connection retry {attempt + 1}/{retries}")

    @handle_errors
    def _verify_device(self):
        device_id = self._read_reg(0x00)
        if device_id != CONFIG['hardware']['expected_device_id']:
            raise ValueError(f"Invalid device ID: 0x{device_id:02x} (expected 0x{CONFIG['hardware']['expected_device_id']:02x})")

    def _init_filters(self):
        # Bandpass filter
        nyq = 0.5 * CONFIG['hardware']['sample_rate']
        low = CONFIG['filters']['bandpass'][0] / nyq
        high = CONFIG['filters']['bandpass'][1] / nyq
        self.b, self.a = butter(2, [low, high], btype='band')

        # Notch filter
        notch_freq = CONFIG['filters']['notch_freq'] / nyq
        self.notch_b, self.notch_a = iirnotch(notch_freq, CONFIG['filters']['notch_q'])

    @handle_errors
    def _read_reg(self, reg):
        return self._spi_transaction([0x20 | reg, 0x00, 0x00])[2]

    @handle_errors
    def _write_reg(self, reg, value):
        self._spi_transaction([0x40 | reg, 0x00, value])

    def _spi_transaction(self, data, retries=3):
        for attempt in range(retries):
            try:
                return self.spi.xfer2(data)
            except Exception as e:
                if attempt == retries - 1:
                    raise
                self._spi_reconnect()
                time.sleep(0.1)

    def _spi_reconnect(self):
        self.spi.close()
        time.sleep(0.5)
        self._spi_connect()

    @handle_errors
    def _configure_sensor(self):
        self._write_reg(0x01, 0x02)  # CONFIG1
        self._write_reg(0x04, 0x40)  # CH1SET
        self._write_reg(0x05, 0x00)  # CH2SET
        self._write_reg(0x0D, 0x24)  # CONFIG3
        self._write_reg(0x0E, 0x04)  # RLD_SENS

    @handle_errors
    def _read_ecg(self):
        for _ in range(10):  # Timeout protection
            if not GPIO.input(CONFIG['hardware']['gpio']['drdy']):
                data = self._spi_transaction([0x12] + [0]*6)
                raw = (data[3] << 16) | (data[4] << 8) | data[5]
                return self._convert_raw_value(raw)
            time.sleep(0.001)
        raise TimeoutError("ECG data ready timeout")

    def _convert_raw_value(self, raw):
        # Validate raw value before conversion
        if not (0 <= raw <= 0xFFFFFF):
            raise ValueError(f"Invalid raw value: 0x{raw:06x}")

        value = raw - (1 << 24) if (raw & (1 << 23)) else raw
        voltage = (value * CONFIG['system']['max_voltage']) / (0x7FFFFF * 6)
        
        if abs(voltage) > 4.5:
            raise ValueError(f"Voltage out of safe range: {voltage:.2f}V")
            
        return voltage

    @handle_errors
    def _process_data(self, data):
        filtered, self.filter_state = lfilter(self.b, self.a, [data[-1]], zi=self.filter_state)
        notch_filtered, self.notch_state = lfilter(self.notch_b, self.notch_a, filtered, zi=self.notch_state)
        return notch_filtered[0]

    @handle_errors
    def start_acquisition(self):
        if not self.running:
            self.running = True
            GPIO.output(CONFIG['hardware']['gpio']['start'], GPIO.HIGH)
            logging.info("Data acquisition started")
            socketio.start_background_task(target=self._acquisition_loop)

    def _acquisition_loop(self):
        buffer_warned = False
        while self.running:
            try:
                # Read and process data
                raw_ecg = self._read_ecg()
                processed_ecg = self._process_data([raw_ecg])

                # Update buffer
                with self.data_lock:
                    self.buffer = np.roll(self.buffer, -1)
                    self.buffer[-1] = processed_ecg

                # Check buffer health
                buffer_usage = np.count_nonzero(self.buffer) / len(self.buffer)
                if buffer_usage > CONFIG['buffer']['warning_threshold'] and not buffer_warned:
                    socketio.emit('system_warning', {'message': 'Buffer approaching capacity'})
                    buffer_warned = True
                elif buffer_usage < CONFIG['buffer']['warning_threshold']:
                    buffer_warned = False

                # Emit data
                socketio.emit('ecg_update', {
                    'timestamp': time.time(),
                    'value': processed_ecg,
                    'buffer': self.buffer.tolist(),
                    'system_stats': self._get_system_stats()
                })

                # Thread management
                time.sleep(1/CONFIG['hardware']['sample_rate'])

            except Exception as e:
                logging.error(f"Acquisition error: {str(e)}")
                self.stop_acquisition()
                break

    def _get_system_stats(self):
        return {
            'cpu': psutil.cpu_percent(),
            'memory': psutil.virtual_memory().percent,
            'buffer': len(self.buffer),
            'uptime': time.time() - self._last_heartbeat
        }

    @handle_errors
    def stop_acquisition(self):
        if self.running:
            self.running = False
            GPIO.output(CONFIG['hardware']['gpio']['start'], GPIO.LOW)
            logging.info("Data acquisition stopped")

    def _emergency_shutdown(self):
        self.stop_acquisition()
        self.spi.close()
        GPIO.cleanup()

# Web Interface Routes
@app.route('/')
def index():
    return render_template('dashboard.html')

@app.route('/config', methods=['GET'])
def get_config():
    return jsonify(CONFIG)

@socketio.on('control')
def handle_control(command):
    sensor = ECGSensor()
    if command == 'start':
        sensor.start_acquisition()
    elif command == 'stop':
        sensor.stop_acquisition()

if __name__ == '__main__':
    try:
        logging.info("Starting ECG Monitoring System")
        socketio.run(app, host='0.0.0.0', port=5000, use_reloader=False)
    except KeyboardInterrupt:
        ECGSensor()._emergency_shutdown()
    finally:
        logging.info("System shutdown complete")
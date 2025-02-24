# ecg_server.py
import logging
import time
import numpy as np
from flask import Flask, render_template, jsonify
from flask_socketio import SocketIO
import spidev
import RPi.GPIO as GPIO
from scipy.signal import butter, lfilter, find_peaks
from dataclasses import dataclass
from threading import Lock
import signal
import sys

# Configuration
@dataclass
class Config:
    SPI_BUS: int = 0
    SPI_DEVICE: int = 0
    SAMPLE_RATE: int = 500
    BUFFER_SIZE: int = 2000
    GPIO_CONFIG: dict = None
    FILTER_RANGE: tuple = (0.5, 40.0)
    MAX_RETRIES: int = 5
    RETRY_DELAY: float = 0.1
    HEART_RATE_WINDOW: int = 10  # seconds

config = Config(
    GPIO_CONFIG={
        'DRDY': 24,
        'START': 25,
        'RESET': 23,
        'CS': 8
    }
)

# Initialize Flask and SocketIO
app = Flask(__name__)
app.config['SECRET_KEY'] = 'ecg_secret!'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

class ECGSensorError(Exception):
    """Base class for ECG sensor errors"""
    pass

class ECGSensorCommunicationError(ECGSensorError):
    """SPI communication failure"""
    pass

class ECGSensorConfigurationError(ECGSensorError):
    """Sensor configuration error"""
    pass

class ECGMonitor:
    _instance = None
    _lock = Lock()
    
    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
            
        self.running = False
        self.buffer = np.zeros(config.BUFFER_SIZE)
        self.filter_coeffs = self._create_bandpass_filter()
        self.heart_rate_history = []
        self.spi = None
        self._filter_state = None
        self._last_update = time.time()
        self._initialized = True
        self._setup_signal_handlers()
        
        try:
            self._initialize_hardware()
            logging.info("ECG Monitor initialized successfully")
        except ECGSensorError as e:
            logging.critical(f"Failed to initialize ECG Monitor: {str(e)}")
            self.cleanup()
            raise

    def _setup_signal_handlers(self):
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame):
        logging.info(f"Received signal {signum}, shutting down...")
        self.cleanup()
        sys.exit(0)

    def _create_bandpass_filter(self):
        nyq = 0.5 * config.SAMPLE_RATE
        low = config.FILTER_RANGE[0] / nyq
        high = config.FILTER_RANGE[1] / nyq
        b, a = butter(2, [low, high], btype='band')
        return (b, a)

    def _initialize_hardware(self):
        self._setup_gpio()
        self._setup_spi()
        self._verify_sensor()
        self._configure_sensor()

    def _setup_gpio(self):
        try:
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(config.GPIO_CONFIG['DRDY'], GPIO.IN)
            GPIO.setup(config.GPIO_CONFIG['START'], GPIO.OUT)
            GPIO.setup(config.GPIO_CONFIG['RESET'], GPIO.OUT)
            GPIO.output(config.GPIO_CONFIG['START'], GPIO.LOW)
            GPIO.output(config.GPIO_CONFIG['RESET'], GPIO.HIGH)
        except Exception as e:
            raise ECGSensorConfigurationError(f"GPIO setup failed: {str(e)}")

    def _setup_spi(self):
        try:
            self.spi = spidev.SpiDev()
            self.spi.open(config.SPI_BUS, config.SPI_DEVICE)
            self.spi.max_speed_hz = 2000000
            self.spi.mode = 0b01
            self.spi.no_cs = True  # Manual CS control
        except Exception as e:
            raise ECGSensorCommunicationError(f"SPI initialization failed: {str(e)}")

    def _verify_sensor(self):
        for attempt in range(config.MAX_RETRIES):
            try:
                device_id = self._read_reg(0x00)
                if device_id != 0x73:
                    raise ECGSensorConfigurationError(
                        f"Unexpected device ID: 0x{device_id:02x} (expected 0x73)"
                    )
                return
            except ECGSensorCommunicationError:
                if attempt == config.MAX_RETRIES - 1:
                    raise
                time.sleep(config.RETRY_DELAY)
                self._hard_reset()

    def _hard_reset(self):
        GPIO.output(config.GPIO_CONFIG['RESET'], GPIO.LOW)
        time.sleep(0.1)
        GPIO.output(config.GPIO_CONFIG['RESET'], GPIO.HIGH)
        time.sleep(0.5)

    def _configure_sensor(self):
        register_settings = {
            0x01: 0x02,  # CONFIG1: 500 SPS
            0x04: 0x40,  # CH1SET: Gain=6, enabled
            0x05: 0x00,  # CH2SET: Disabled
            0x0E: 0x04   # RLD_SENS: RLD enabled
        }
        
        for reg, value in register_settings.items():
            self._write_reg(reg, value)
            read_back = self._read_reg(reg)
            if read_back != value:
                raise ECGSensorConfigurationError(
                    f"Register 0x{reg:02x} configuration failed "
                    f"(wrote 0x{value:02x}, read 0x{read_back:02x})"
                )

    def _read_reg(self, reg):
        try:
            GPIO.output(config.GPIO_CONFIG['CS'], GPIO.LOW)
            response = self.spi.xfer2([0x20 | reg, 0x00, 0x00])
            GPIO.output(config.GPIO_CONFIG['CS'], GPIO.HIGH)
            return response[2]
        except Exception as e:
            raise ECGSensorCommunicationError(f"Register read failed: {str(e)}")

    def _write_reg(self, reg, value):
        try:
            GPIO.output(config.GPIO_CONFIG['CS'], GPIO.LOW)
            self.spi.xfer2([0x40 | reg, 0x00, value])
            GPIO.output(config.GPIO_CONFIG['CS'], GPIO.HIGH)
        except Exception as e:
            raise ECGSensorCommunicationError(f"Register write failed: {str(e)}")

    def _read_ecg_data(self):
        try:
            GPIO.output(config.GPIO_CONFIG['CS'], GPIO.LOW)
            data = self.spi.xfer2([0x12] + [0]*6)
            GPIO.output(config.GPIO_CONFIG['CS'], GPIO.HIGH)
            raw = (data[3] << 16) | (data[4] << 8) | data[5]
            return self._convert_raw_value(raw)
        except Exception as e:
            raise ECGSensorCommunicationError(f"ECG read failed: {str(e)}")

    def _convert_raw_value(self, raw):
        # Convert 24-bit two's complement to voltage (VREF = 4.5V)
        value = raw - (1 << 24) if (raw & (1 << 23)) else raw
        voltage = (value * 4.5) / (0x7FFFFF * 6)  # Gain=6
        return voltage * 1000  # Convert to mV

    def _process_ecg_data(self, data):
        filtered, self._filter_state = lfilter(
            self.filter_coeffs[0],
            self.filter_coeffs[1],
            [data],
            zi=self._filter_state
        )
        return filtered[0]

    def _calculate_heart_rate(self, signal_window):
        try:
            peaks, _ = find_peaks(signal_window, height=0.5, distance=int(config.SAMPLE_RATE*0.3))
            if len(peaks) < 2:
                return None
            intervals = np.diff(peaks) / config.SAMPLE_RATE
            return 60 / np.mean(intervals)
        except Exception as e:
            logging.warning(f"Heart rate calculation failed: {str(e)}")
            return None

    def start_acquisition(self):
        if self.running:
            return
            
        self.running = True
        GPIO.output(config.GPIO_CONFIG['START'], GPIO.HIGH)
        logging.info("Data acquisition started")
        
        while self.running:
            try:
                raw_value = self._read_ecg_data()
                filtered_value = self._process_ecg_data(raw_value)
                
                # Update buffer
                self.buffer = np.roll(self.buffer, -1)
                self.buffer[-1] = filtered_value
                
                # Calculate metrics
                current_time = time.time()
                if current_time - self._last_update >= 1:
                    window = self.buffer[-config.SAMPLE_RATE*config.HEART_RATE_WINDOW:]
                    heart_rate = self._calculate_heart_rate(window)
                    if heart_rate:
                        self.heart_rate_history.append(heart_rate)
                        self.heart_rate_history = self.heart_rate_history[-10:]  # Keep last 10 readings
                    
                    socketio.emit('system_status', {
                        'timestamp': current_time,
                        'buffer_level': len(self.buffer),
                        'heart_rate': np.mean(self.heart_rate_history) if self.heart_rate_history else None,
                        'processing_latency': time.time() - current_time
                    })
                    self._last_update = current_time
                
                socketio.emit('ecg_data', {
                    'timestamp': time.time(),
                    'raw': raw_value,
                    'filtered': filtered_value
                })
                
            except ECGSensorCommunicationError as e:
                logging.error(f"Data acquisition error: {str(e)}")
                self.stop_acquisition()
                socketio.emit('system_error', {'message': str(e)})
                break
                
            time.sleep(1/config.SAMPLE_RATE)

    def stop_acquisition(self):
        if self.running:
            self.running = False
            GPIO.output(config.GPIO_CONFIG['START'], GPIO.LOW)
            logging.info("Data acquisition stopped")

    def cleanup(self):
        self.stop_acquisition()
        if self.spi:
            self.spi.close()
        GPIO.cleanup()
        logging.info("ECG Monitor resources cleaned up")

# Web Interface Routes
@app.route('/')
def index():
    return render_template('dashboard.html')

@app.route('/status')
def system_status():
    monitor = ECGMonitor()
    return jsonify({
        'running': monitor.running,
        'buffer_size': len(monitor.buffer),
        'heart_rate': np.mean(monitor.heart_rate_history) if monitor.heart_rate_history else None,
        'sample_rate': config.SAMPLE_RATE
    })

@socketio.on('control')
def handle_control(command):
    monitor = ECGMonitor()
    try:
        if command == 'start':
            if not monitor.running:
                socketio.start_background_task(target=monitor.start_acquisition)
        elif command == 'stop':
            monitor.stop_acquisition()
    except Exception as e:
        logging.error(f"Control command failed: {str(e)}")
        socketio.emit('system_error', {'message': str(e)})

if __name__ == '__main__':
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler('ecg_monitor.log'),
            logging.StreamHandler()
        ]
    )
    
    try:
        ECGMonitor()  # Initialize early to catch hardware issues
        socketio.run(app, host='0.0.0.0', port=5000, debug=False, use_reloader=False)
    except Exception as e:
        logging.critical(f"Fatal initialization error: {str(e)}")
        sys.exit(1)
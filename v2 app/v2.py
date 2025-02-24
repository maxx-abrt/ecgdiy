import logging
import time
import numpy as np
from flask import Flask, render_template
from flask_socketio import SocketIO
import spidev
import RPi.GPIO as GPIO
from scipy.signal import butter, lfilter

# Configuration
SPI_BUS = 0
SPI_DEVICE = 0
SAMPLE_RATE = 500  # Hz
BUFFER_SIZE = 1000
ECG_CHANNEL = 1  # Use channel 1 for ECG

# GPIO Pins (BCM numbering)
GPIO_CONFIG = {
    'DRDY': 24,
    'START': 25,
    'RESET': 23,
    'CS': 8  # Hardware CS (CE0)
}

# Initialize Flask and SocketIO
app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

class ECGMonitor:
    def __init__(self):
        self.running = False
        self.buffer = np.zeros(BUFFER_SIZE)
        self.filter_coeffs = self._create_bandpass_filter()
        self.initialize_hardware()

    def _create_bandpass_filter(self):
        nyq = 0.5 * SAMPLE_RATE
        low = 0.5 / nyq
        high = 40.0 / nyq
        b, a = butter(2, [low, high], btype='band')
        return (b, a)

    def initialize_hardware(self):
        try:
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(GPIO_CONFIG['DRDY'], GPIO.IN)
            GPIO.setup(GPIO_CONFIG['START'], GPIO.OUT)
            GPIO.setup(GPIO_CONFIG['RESET'], GPIO.OUT)

            self.spi = spidev.SpiDev()
            self.spi.open(SPI_BUS, SPI_DEVICE)
            self.spi.max_speed_hz = 2000000
            self.spi.mode = 0b01

            self._reset_ads()
            self._configure_ads()
            logging.info("Hardware initialized successfully")

        except Exception as e:
            logging.error(f"Hardware init failed: {str(e)}")
            self.cleanup()
            raise

    def _reset_ads(self):
        GPIO.output(GPIO_CONFIG['RESET'], GPIO.LOW)
        time.sleep(0.1)
        GPIO.output(GPIO_CONFIG['RESET'], GPIO.HIGH)
        time.sleep(0.1)

    def _configure_ads(self):
        # Verify device ID
        device_id = self._read_reg(0x00)
        if device_id != 0x73:
            raise RuntimeError(f"Invalid device ID: 0x{device_id:02x}")

        # Configure registers
        self._write_reg(0x01, 0x02)  # CONFIG1: 500 SPS
        self._write_reg(0x04, 0x40)  # CH1SET: Gain=6, enabled
        self._write_reg(0x05, 0x00)  # CH2SET: Disabled
        self._write_reg(0x0E, 0x04)  # RLD_SENS: RLD enabled

    def _read_reg(self, reg):
        return self.spi.xfer2([0x20 | reg, 0x00, 0x00])[2]

    def _write_reg(self, reg, value):
        self.spi.xfer2([0x40 | reg, 0x00, value])

    def _read_ecg(self):
        while GPIO.input(GPIO_CONFIG['DRDY']):
            pass
        data = self.spi.xfer2([0x12] + [0]*6)
        raw = (data[3] << 16) | (data[4] << 8) | data[5]
        return self._convert_raw_value(raw)

    def _convert_raw_value(self, raw):
        # Convert 24-bit two's complement to voltage (VREF = 4.5V)
        value = raw - (1 << 24) if (raw & (1 << 23)) else raw
        return (value * 4.5) / (0x7FFFFF * 6)  # Gain=6

    def _process_data(self, data):
        return lfilter(*self.filter_coeffs, data)

    def start(self):
        self.running = True
        GPIO.output(GPIO_CONFIG['START'], GPIO.HIGH)
        logging.info("Data acquisition started")

        while self.running:
            try:
                ecg = self._read_ecg()
                self.buffer = np.roll(self.buffer, -1)
                self.buffer[-1] = ecg

                # Send filtered data every 50ms
                if time.time() % 0.05 < 0.001:
                    filtered = self._process_data(self.buffer)
                    socketio.emit('ecg_update', {
                        'raw': ecg,
                        'filtered': filtered[-1],
                        'buffer': filtered.tolist()
                    })

            except Exception as e:
                logging.error(f"Data error: {str(e)}")
                self.stop()
                break

    def stop(self):
        self.running = False
        GPIO.output(GPIO_CONFIG['START'], GPIO.LOW)
        logging.info("Data acquisition stopped")

    def cleanup(self):
        self.stop()
        self.spi.close()
        GPIO.cleanup()

ecg_monitor = ECGMonitor()

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('control')
def handle_control(command):
    if command == 'start':
        socketio.start_background_task(target=ecg_monitor.start)
    elif command == 'stop':
        ecg_monitor.stop()

if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG)
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)
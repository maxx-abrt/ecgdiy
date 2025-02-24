import unittest
from unittest.mock import MagicMock, patch
from ecg_server import ECGSensor, CONFIG

class TestECGSystem(unittest.TestCase):
    
    @patch('RPi.GPIO.setup')
    @patch('spidev.SpiDev')
    def test_sensor_initialization(self, mock_spi, mock_gpio):
        sensor = ECGSensor()
        self.assertIsNotNone(sensor)
        
    # Add more tests for data processing, error handling, etc.

if __name__ == '__main__':
    unittest.main()
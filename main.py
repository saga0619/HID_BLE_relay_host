import sys
import threading
import asyncio
import time
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout
)
from PyQt5.QtCore import Qt, QCoreApplication, QObject, pyqtSignal
from PyQt5.QtMultimedia import QCamera, QCameraInfo, QCameraViewfinderSettings
from PyQt5.QtMultimediaWidgets import QCameraViewfinder
from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from itertools import count, takewhile
from typing import Iterator

# ---- TARGET CAPTURE BOARD NAME ----
TARGET_CAMERA_NAME = "UGREEN-25854"

# ---- TARGET CAPTURE RESOLUTION / FPS ----
TARGET_CAPTURE_WIDTH = 1920
TARGET_CAPTURE_HEIGHT = 1080
TARGET_CAPTURE_FPS = 60

# ---- TARGET BLE DEVICE ----
TARGET_BLE_NAME = "HID BLE Relay"

# ---- HID Relay custom service UUIDs (must match firmware) ----
HID_SERVICE_UUID = "597f1290-5b99-477d-9261-f0ed801fc566"
HID_RX_CHAR_UUID = "597f1291-5b99-477d-9261-f0ed801fc566"  # Write
HID_TX_CHAR_UUID = "597f1292-5b99-477d-9261-f0ed801fc566"  # Notify

BLE_RECONNECT_DELAY_S = 2.0
ABS_COORD_MAX = 32767


def sliced(data: bytes, n: int) -> Iterator[bytes]:
    return takewhile(len, (data[i : i + n] for i in count(0, n)))


# ===================================================
# 1. BLE Manager (HID Relay custom GATT service)
# ===================================================
class BleManager(QObject):
    connected_changed = pyqtSignal(bool)

    def __init__(self):
        super().__init__()
        self.client: BleakClient | None = None
        self.connected = False
        self.rx_char: BleakGATTCharacteristic | None = None
        self._stop = False

        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()

    def _set_connected(self, value: bool):
        if self.connected != value:
            self.connected = value
            self.connected_changed.emit(value)

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        while not self._stop:
            try:
                self.loop.run_until_complete(self.connect_and_run())
            except Exception as e:
                print(f"[BLE] Exception: {e}")
            if self._stop:
                break
            time.sleep(BLE_RECONNECT_DELAY_S)

    async def connect_and_run(self):
        def match_hid_device(device: BLEDevice, adv: AdvertisementData):
            if not device.name or TARGET_BLE_NAME not in device.name:
                return False
            if HID_SERVICE_UUID.lower() in [s.lower() for s in adv.service_uuids]:
                print(f"[BLE] Found HID Device: {device.name}")
                return True
            return False

        print("[BLE] Scanning HID Relay device...")
        device = await BleakScanner.find_device_by_filter(match_hid_device, timeout=10.0)

        if device is None:
            print("[BLE] HID Relay not found, retrying...")
            return

        def handle_disconnect(_: BleakClient):
            print("[BLE] Device disconnected.")
            for task in asyncio.all_tasks():
                task.cancel()

        print(f"[BLE] Connecting to {device.address}...")
        async with BleakClient(device, disconnected_callback=handle_disconnect) as client:
            self.client = client
            print("[BLE] Connected!")

            await client.start_notify(HID_TX_CHAR_UUID, self.handle_rx)

            nus_service = client.services.get_service(HID_SERVICE_UUID)
            self.rx_char = nus_service.get_characteristic(HID_RX_CHAR_UUID)

            self._set_connected(True)
            try:
                while True:
                    await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                pass
            finally:
                print("[BLE] Connection closed.")
                self._set_connected(False)
                self.client = None
                self.rx_char = None

    def handle_rx(self, _: BleakGATTCharacteristic, data: bytearray):
        print("[BLE] Received:", data)

    def send_data_sync(self, msg: str):
        if not self.connected or not self.rx_char:
            return
        asyncio.run_coroutine_threadsafe(self._send_data(msg), self.loop)

    async def _send_data(self, msg: str):
        if not self.rx_char or not self.client:
            return
        data = msg.encode()
        max_size = self.rx_char.max_write_without_response_size
        for chunk in sliced(data, max_size):
            try:
                await self.client.write_gatt_char(self.rx_char, chunk, response=False)
            except Exception as e:
                print(f"[BLE] write failed: {e}")
                return


# ===================================================
# 2. PyQt5 GUI App
# ===================================================
class VideoApp(QWidget):
    def __init__(self, camera_index=0, ble_manager=None):
        super().__init__()
        self.ble_manager = ble_manager
        self._mouse_buttons = 0  # bit0=left, bit1=right
        self._update_title(False)
        self.setGeometry(100, 200, 960, 540)

        self.camera_viewfinder = QCameraViewfinder(self)
        layout = QVBoxLayout()
        layout.addWidget(self.camera_viewfinder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.setLayout(layout)
        # Required to receive mouseMoveEvent without a button held.
        self.setMouseTracking(True)
        self.camera_viewfinder.setMouseTracking(True)

        cameras = QCameraInfo.availableCameras()
        viewfinder_settings = QCameraViewfinderSettings()
        viewfinder_settings.setResolution(TARGET_CAPTURE_WIDTH, TARGET_CAPTURE_HEIGHT)
        viewfinder_settings.setMinimumFrameRate(TARGET_CAPTURE_FPS)
        viewfinder_settings.setMaximumFrameRate(TARGET_CAPTURE_FPS)

        self.camera = QCamera(cameras[camera_index])
        self.camera.setViewfinder(self.camera_viewfinder)
        self.camera.setViewfinderSettings(viewfinder_settings)
        self.camera.start()
        print("Available Resolution:", self.camera.supportedViewfinderResolutions())
        print("Current resolution:", self.camera.viewfinderSettings().resolution())

        if self.ble_manager:
            self.ble_manager.connected_changed.connect(self._update_title)

    def _update_title(self, connected: bool):
        status = "Connected" if connected else "Scanning..."
        self.setWindowTitle(f"HID BLE Relay — {status}")

    def keyPressEvent(self, event):
        # The remote target's OS handles key auto-repeat itself; only relay
        # the initial press so we don't spam the link.
        if event.isAutoRepeat():
            return
        if self.ble_manager:
            self.ble_manager.send_data_sync(f"KP:{hex(event.key())}")

    def keyReleaseEvent(self, event):
        if event.isAutoRepeat():
            return
        if self.ble_manager:
            self.ble_manager.send_data_sync(f"KR:{hex(event.key())}")

    def closeEvent(self, event):
        print("Program Termination")
        self.camera.stop()
        event.accept()

    def get_video_display_rect(self):
        resolution = self.camera.viewfinderSettings().resolution()
        video_width, video_height = resolution.width(), resolution.height()

        viewfinder_rect = self.camera_viewfinder.geometry()
        viewfinder_width, viewfinder_height = viewfinder_rect.width(), viewfinder_rect.height()

        aspect_video = video_width / video_height
        aspect_viewfinder = viewfinder_width / viewfinder_height

        if aspect_video > aspect_viewfinder:
            display_width = viewfinder_width
            display_height = int(viewfinder_width / aspect_video)
            offset_x = 0
            offset_y = (viewfinder_height - display_height) // 2
        else:
            display_width = int(viewfinder_height * aspect_video)
            display_height = viewfinder_height
            offset_x = (viewfinder_width - display_width) // 2
            offset_y = 0

        return offset_x, offset_y, display_width, display_height

    def _normalized_pos(self, event):
        x_off, y_off, width, height = self.get_video_display_rect()
        if width <= 0 or height <= 0:
            return 0, 0
        nx = (event.pos().x() - x_off) / width
        ny = (event.pos().y() - y_off) / height
        nx = max(0.0, min(1.0, nx))
        ny = max(0.0, min(1.0, ny))
        return int(nx * ABS_COORD_MAX), int(ny * ABS_COORD_MAX)

    def mousePressEvent(self, event):
        if not self.ble_manager:
            return
        pos_x, pos_y = self._normalized_pos(event)
        if event.button() == Qt.LeftButton:
            self._mouse_buttons |= 0x1
            self.ble_manager.send_data_sync(f"ML:{pos_x},{pos_y}")
        elif event.button() == Qt.RightButton:
            self._mouse_buttons |= 0x2
            self.ble_manager.send_data_sync(f"MR:{pos_x},{pos_y}")

    def mouseReleaseEvent(self, event):
        if not self.ble_manager:
            return
        pos_x, pos_y = self._normalized_pos(event)
        if event.button() == Qt.LeftButton:
            self._mouse_buttons &= ~0x1
            self.ble_manager.send_data_sync(f"MS:{pos_x},{pos_y}")
        elif event.button() == Qt.RightButton:
            self._mouse_buttons &= ~0x2
            self.ble_manager.send_data_sync(f"ME:{pos_x},{pos_y}")

    def mouseMoveEvent(self, event):
        if not self.ble_manager:
            return
        pos_x, pos_y = self._normalized_pos(event)
        # Pick the verb based on which button is currently held so the
        # firmware reports the right HID button bit during a drag.
        if self._mouse_buttons & 0x1:
            verb = "ML"
        elif self._mouse_buttons & 0x2:
            verb = "MR"
        else:
            verb = "MM"
        self.ble_manager.send_data_sync(f"{verb}:{pos_x},{pos_y}")

    def wheelEvent(self, event):
        if not self.ble_manager:
            return
        # Qt's angleDelta is in 1/8 of a degree; one mouse-wheel detent ≈ 120.
        delta = event.angleDelta().y()
        if delta == 0:
            return
        wheel = delta // 120
        if wheel == 0:
            wheel = 1 if delta > 0 else -1
        wheel = max(-127, min(127, wheel))
        self.ble_manager.send_data_sync(f"WW:{wheel}")


# ===================================================
# 3. Main
# ===================================================
def main():
    # On macOS, PyQt swaps Cmd<->Ctrl by default. That makes the host's
    # physical Ctrl key arrive as Qt::Key_Meta, which the dongle then maps
    # to the target's Cmd key — so Ctrl+C never reaches the target as Ctrl.
    # Disable the swap.
    QCoreApplication.setAttribute(Qt.AA_MacDontSwapCtrlAndMeta)

    app = QApplication(sys.argv)

    ble_manager = BleManager()

    cameras = QCameraInfo.availableCameras()
    if not cameras:
        print("No cameras detected.")
        sys.exit(1)

    selected_camera_index = 0
    target_found = False
    print("Available Cameras:")
    for idx, camera_info in enumerate(cameras):
        print(f"{idx}: {camera_info.description()}")
        if TARGET_CAMERA_NAME in camera_info.description():
            selected_camera_index = idx
            target_found = True

    if not target_found:
        print(f"Target Camera {TARGET_CAMERA_NAME} not found. Selecting first available.")
    print(f"Selected Camera: {selected_camera_index}: "
          f"{cameras[selected_camera_index].description()}")

    window = VideoApp(selected_camera_index, ble_manager=ble_manager)
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()

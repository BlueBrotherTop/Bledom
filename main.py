import asyncio
import colorsys
import json
import math
import os
import threading
import time

from kivy.config import Config
from kivy.utils import platform

# На ПК открываем окно в пропорциях мобилки 430x880
if platform not in ("android", "ios"):
    Config.set("graphics", "width", "430")
    Config.set("graphics", "height", "880")
    Config.set("graphics", "resizable", "1")

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.graphics import Color, Line, Rectangle, RoundedRectangle
from kivy.graphics.texture import Texture
from kivy.lang import Builder
from kivy.metrics import dp
from kivy.properties import (
    BooleanProperty,
    ListProperty,
    NumericProperty,
    StringProperty,
)
from kivy.uix.behaviors import ButtonBehavior
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label

from bleak import BleakClient, BleakScanner

# =====================================================================
# КОНСТАНТЫ ПРОТОКОЛА LOTUS LANTERN / BLEDOM
# =====================================================================
TARGET_MAC = "BE:37:EA:01:09:28"
TARGET_NAMES = ("BLEDOM", "ELK-BLEDOM")

UUID_PRIMARY = "0000fff3-0000-1000-8000-00805f9b34fb"
UUID_FALLBACK = "0000ffe1-0000-1000-8000-00805f9b34fb"

CMD_POWER_ON = bytes([0x7E, 0x00, 0x04, 0xF0, 0x00, 0x01, 0xFF, 0x00, 0xEF])
CMD_POWER_OFF = bytes([0x7E, 0x00, 0x04, 0x00, 0x00, 0x00, 0xFF, 0x00, 0xEF])


def make_brightness_packet(val: int) -> bytes:
    v = max(1, min(100, int(val)))
    return bytes([0x7E, 0x00, 0x01, v, 0x00, 0x00, 0x00, 0x00, 0xEF])


def make_rgb_packet(r: int, g: int, b: int) -> bytes:
    r_b = max(0, min(255, int(r)))
    g_b = max(0, min(255, int(g)))
    b_b = max(0, min(255, int(b)))
    return bytes([0x7E, 0x00, 0x05, 0x03, r_b, g_b, b_b, 0x00, 0xEF])


def rgb_to_hex(r: int, g: int, b: int) -> str:
    return f"#{int(r):02X}{int(g):02X}{int(b):02X}"


def hex_to_rgb(hex_str: str):
    h = hex_str.strip().lstrip("#")
    if len(h) != 6:
        return (0, 240, 255)
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


# =====================================================================
# АСИНХРОННЫЙ BLE-ДВИЖОК С DROP-QUEUE И ФИЛЬТРОМ ДУБЛИКАТОВ
# =====================================================================
class BLEDOMWorker:
    """
    Фоновый поток asyncio + bleak.
    Отбрасывает устаревшие кадры (LIFO Drop-Queue) и не шлёт повторно
    одинаковые RGB-пакеты, чтобы аппаратный буфер контроллера не захлёбывался.
    """

    def __init__(self, status_callback):
        self.status_callback = status_callback
        self._lock = threading.Lock()
        self._latest_power = None
        self._latest_brightness = None
        self._latest_rgb = None
        self._last_sent_rgb_pkt = None

        self._loop = None
        self._wake_event = None
        self._reconnect_flag = False
        self._stop_flag = False
        self._connected = False

        self._thread = threading.Thread(target=self._run_thread, daemon=True)
        self._thread.start()

    def _notify_status(self, state: str, text: str):
        Clock.schedule_once(lambda dt: self.status_callback(state, text), 0)

    def _wake(self):
        if self._loop and self._wake_event:
            self._loop.call_soon_threadsafe(self._wake_event.set)

    def send_power(self, is_on: bool):
        with self._lock:
            self._latest_power = CMD_POWER_ON if is_on else CMD_POWER_OFF
            self._last_sent_rgb_pkt = None
        self._wake()

    def send_brightness(self, val: int):
        with self._lock:
            self._latest_brightness = make_brightness_packet(val)
        self._wake()

    def send_rgb(self, r: int, g: int, b: int, force: bool = False):
        pkt = make_rgb_packet(r, g, b)
        with self._lock:
            if not force and pkt == self._last_sent_rgb_pkt:
                return
            self._latest_rgb = pkt
        self._wake()

    def request_reconnect(self):
        with self._lock:
            self._reconnect_flag = True
        self._wake()

    def stop(self):
        self._stop_flag = True
        self._wake()

    def _pop_next_packet(self):
        with self._lock:
            if self._latest_power is not None:
                pkt = self._latest_power
                self._latest_power = None
                return ("power", pkt)
            if self._latest_brightness is not None:
                pkt = self._latest_brightness
                self._latest_brightness = None
                return ("brightness", pkt)
            if self._latest_rgb is not None:
                pkt = self._latest_rgb
                self._latest_rgb = None
                if pkt == self._last_sent_rgb_pkt:
                    return None
                self._last_sent_rgb_pkt = pkt
                return ("rgb", pkt)
            return None

    def _has_pending(self) -> bool:
        with self._lock:
            return any(
                x is not None
                for x in (
                    self._latest_power,
                    self._latest_brightness,
                    self._latest_rgb,
                )
            )

    def _run_thread(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._wake_event = asyncio.Event()
        self._loop.run_until_complete(self._main_loop())

    async def _find_target_device(self):
        try:
            dev = await BleakScanner.find_device_by_address(TARGET_MAC, timeout=3.5)
            if dev is not None:
                return dev
        except Exception:
            pass

        try:
            devices = await BleakScanner.discover(timeout=4.0)
            for d in devices:
                addr = (d.address or "").upper()
                name = (d.name or "").upper()
                if addr == TARGET_MAC.upper():
                    return d
                if any(t in name for t in TARGET_NAMES):
                    return d
        except Exception:
            pass
        return TARGET_MAC

    def _resolve_write_char(self, client: BleakClient) -> str:
        try:
            for service in client.services:
                for char in service.characteristics:
                    cu = str(char.uuid).lower()
                    if cu == UUID_PRIMARY.lower():
                        return char.uuid
            for service in client.services:
                for char in service.characteristics:
                    cu = str(char.uuid).lower()
                    if cu == UUID_FALLBACK.lower():
                        return char.uuid
        except Exception:
            pass
        return UUID_PRIMARY

    async def _main_loop(self):
        while not self._stop_flag:
            self._connected = False
            self._notify_status("connecting", "Поиск BLEDOM...")

            target = await self._find_target_device()
            if self._stop_flag:
                break

            try:
                async with BleakClient(target, timeout=8.0) as client:
                    if not client.is_connected:
                        raise RuntimeError("Не удалось открыть GATT-сессию")

                    write_uuid = self._resolve_write_char(client)
                    self._connected = True
                    dev_label = (
                        target.address
                        if hasattr(target, "address")
                        else str(target)
                    )
                    self._notify_status("connected", f"Подключено ({dev_label})")

                    with self._lock:
                        self._reconnect_flag = False
                        self._last_sent_rgb_pkt = None

                    while client.is_connected and not self._stop_flag:
                        with self._lock:
                            if self._reconnect_flag:
                                self._reconnect_flag = False
                                break

                        item = self._pop_next_packet()
                        if item is not None:
                            _, pkt = item
                            await client.write_gatt_char(
                                write_uuid, pkt, response=False
                            )
                            # Оптимальный тайминг для чипа BLEDOM без переполнения буфера
                            await asyncio.sleep(0.022)
                        else:
                            self._wake_event.clear()
                            if not self._has_pending():
                                try:
                                    await asyncio.wait_for(
                                        self._wake_event.wait(), timeout=0.5
                                    )
                                except asyncio.TimeoutError:
                                    pass

            except Exception:
                self._connected = False
                self._notify_status("disconnected", "Нет связи • Нажми ↻")

                self._wake_event.clear()
                for _ in range(40):
                    if self._stop_flag:
                        break
                    with self._lock:
                        if self._reconnect_flag:
                            self._reconnect_flag = False
                            break
                    await asyncio.sleep(0.1)


# =====================================================================
# КАСТОМНЫЕ ВИДЖЕТЫ И KV-РАЗМЕТКА (LIQUID GLASS MOBILE)
# =====================================================================
KV_MARKUP = """
#:import dp kivy.metrics.dp

<GlassCard@BoxLayout>:
    orientation: 'vertical'
    padding: dp(14)
    spacing: dp(10)
    size_hint_y: None
    height: self.minimum_height
    canvas.before:
        Color:
            rgba: 1, 1, 1, 0.06
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(16)]
        Color:
            rgba: 0.667, 0.882, 1.0, 0.18
        Line:
            rounded_rectangle: (self.x, self.y, self.width, self.height, dp(16))
            width: 1.1

<GlassButton>:
    bg_color: 1, 1, 1, 0.09
    border_color: 0.667, 0.882, 1.0, 0.25
    radius_val: dp(12)
    bold: True
    font_size: '13sp'
    color: 0.95, 0.98, 1, 1
    halign: 'center'
    valign: 'middle'
    text_size: self.size
    canvas.before:
        Color:
            rgba: (self.bg_color[0]*1.2, self.bg_color[1]*1.2, self.bg_color[2]*1.2, min(1.0, self.bg_color[3] + 0.15)) if self.state == 'down' else self.bg_color
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [self.radius_val]
        Color:
            rgba: self.border_color
        Line:
            rounded_rectangle: (self.x, self.y, self.width, self.height, dp(12))
            width: 1.15

<ReloadButton>:
    size_hint: None, None
    size: dp(38), dp(38)
    bg_color: 1, 1, 1, 0.08
    border_color: 0.667, 0.882, 1.0, 0.28
    radius_val: dp(11)
    text: ""
    canvas.after:
        Color:
            rgba: 0.85, 0.95, 1, 0.95
        Line:
            circle: (self.center_x, self.center_y, dp(8), 35, 315)
            width: dp(1.6)
        Line:
            points: [self.center_x + dp(4), self.center_y + dp(9), self.center_x + dp(9), self.center_y + dp(8), self.center_x + dp(7), self.center_y + dp(3)]
            width: dp(1.5)

<QuickColorButton>:
    size_hint_y: None
    height: dp(34)
    radius_val: dp(10)
    border_color: 1, 1, 1, 0.35
    text: ""

<RootUI>:
    orientation: 'vertical'
    padding: [dp(14), dp(12), dp(14), dp(12)]
    spacing: dp(10)

    # ---------------- ВЕРХНЯЯ ПАНЕЛЬ ----------------
    GlassCard:
        padding: [dp(12), dp(10), dp(12), dp(10)]
        spacing: dp(8)

        BoxLayout:
            size_hint_y: None
            height: dp(38)
            spacing: dp(10)

            Widget:
                size_hint: None, None
                size: dp(14), dp(38)
                canvas:
                    Color:
                        rgba: root.ble_led_color[0], root.ble_led_color[1], root.ble_led_color[2], 0.28
                    Ellipse:
                        pos: self.center_x - dp(9), self.center_y - dp(9)
                        size: dp(18), dp(18)
                    Color:
                        rgba: root.ble_led_color
                    Ellipse:
                        pos: self.center_x - dp(5), self.center_y - dp(5)
                        size: dp(10), dp(10)

            BoxLayout:
                orientation: 'vertical'
                Label:
                    text: "LOTUS LANTERN • BLEDOM"
                    font_size: '11sp'
                    bold: True
                    color: 0.62, 0.82, 0.96, 0.85
                    halign: 'left'
                    valign: 'bottom'
                    text_size: self.size
                Label:
                    text: root.ble_status_text
                    font_size: '13sp'
                    bold: True
                    color: 0.95, 0.98, 1, 1
                    halign: 'left'
                    valign: 'top'
                    text_size: self.size

            ReloadButton:
                on_release: root.on_reconnect_pressed()

        GlassButton:
            size_hint_y: None
            height: dp(46)
            radius_val: dp(13)
            text: "ПИТАНИЕ: ВКЛЮЧЕНО" if root.power_on else "ПИТАНИЕ: ВЫКЛЮЧЕНО"
            font_size: '14sp'
            bg_color: (0.05, 0.68, 0.52, 0.42) if root.power_on else (0.75, 0.16, 0.24, 0.32)
            border_color: (0.25, 0.98, 0.78, 0.65) if root.power_on else (1.0, 0.35, 0.42, 0.45)
            on_release: root.toggle_power()

    # ---------------- СКРОЛЛ-КОНТЕЙНЕР ----------------
    ScrollView:
        do_scroll_x: False
        bar_width: dp(3)
        bar_color: 0.5, 0.8, 1, 0.35
        bar_inactive_color: 0.5, 0.8, 1, 0.1
        scroll_type: ['bars', 'content']

        BoxLayout:
            orientation: 'vertical'
            size_hint_y: None
            height: self.minimum_height
            spacing: dp(12)
            padding: [0, dp(2), 0, dp(10)]

            # 1. КАРТОЧКА ЯРКОСТИ
            GlassCard:
                BoxLayout:
                    size_hint_y: None
                    height: dp(26)
                    Label:
                        text: "ЯРКОСТЬ ЛЕНТЫ"
                        font_size: '12sp'
                        bold: True
                        color: 0.75, 0.90, 1, 0.95
                        halign: 'left'
                        valign: 'middle'
                        text_size: self.size

                    Label:
                        size_hint_x: None
                        width: dp(58)
                        text: f"{int(root.brightness)}%"
                        font_size: '13sp'
                        bold: True
                        color: 0.95, 0.99, 1, 1
                        halign: 'center'
                        valign: 'middle'
                        text_size: self.size
                        canvas.before:
                            Color:
                                rgba: 0.15, 0.55, 0.85, 0.28
                            RoundedRectangle:
                                pos: self.pos
                                size: self.size
                                radius: [dp(8)]

                BoxLayout:
                    size_hint_y: None
                    height: dp(38)
                    spacing: dp(8)

                    GlassButton:
                        text: "−"
                        font_size: '18sp'
                        size_hint_x: None
                        width: dp(40)
                        on_release: root.step_brightness(-5)

                    Slider:
                        id: slider_brightness
                        min: 1
                        max: 100
                        step: 1
                        value: root.brightness
                        value_track: True
                        value_track_color: 0.30, 0.82, 1.0, 0.95
                        value_track_width: dp(4)
                        cursor_size: dp(20), dp(20)
                        on_value: root.on_brightness_slider(self.value)

                    GlassButton:
                        text: "+"
                        font_size: '18sp'
                        size_hint_x: None
                        width: dp(40)
                        on_release: root.step_brightness(5)

            # 2. КАРТОЧКА ТОЧЕЧНОЙ НАСТРОЙКИ RGB
            GlassCard:
                BoxLayout:
                    size_hint_y: None
                    height: dp(32)
                    spacing: dp(10)

                    Label:
                        text: "ТОЧЕЧНЫЙ ЦВЕТ (RGB)"
                        font_size: '12sp'
                        bold: True
                        color: 0.75, 0.90, 1, 0.95
                        halign: 'left'
                        valign: 'middle'
                        text_size: self.size

                    # Плашка-индикатор текущего цвета с HEX-кодом
                    Label:
                        size_hint_x: None
                        width: dp(104)
                        text: root.current_hex
                        font_size: '13sp'
                        bold: True
                        color: (0.08, 0.10, 0.14, 1) if (root.preview_rgb[0]*0.299 + root.preview_rgb[1]*0.587 + root.preview_rgb[2]*0.114) > 150 else (0.98, 0.99, 1, 1)
                        canvas.before:
                            Color:
                                rgba: root.preview_rgb[0]/255.0, root.preview_rgb[1]/255.0, root.preview_rgb[2]/255.0, 1.0
                            RoundedRectangle:
                                pos: self.pos
                                size: self.size
                                radius: [dp(9)]
                            Color:
                                rgba: 1, 1, 1, 0.45
                            Line:
                                rounded_rectangle: (self.x, self.y, self.width, self.height, dp(9))
                                width: 1.1

                # Ползунок R
                BoxLayout:
                    size_hint_y: None
                    height: dp(32)
                    spacing: dp(8)
                    Label:
                        text: "R"
                        bold: True
                        font_size: '13sp'
                        color: 1.0, 0.35, 0.35, 1
                        size_hint_x: None
                        width: dp(20)
                    Slider:
                        id: slider_r
                        min: 0
                        max: 255
                        step: 1
                        value: root.r_val
                        value_track: True
                        value_track_color: 1.0, 0.23, 0.19, 0.95
                        value_track_width: dp(4)
                        cursor_size: dp(20), dp(20)
                        on_value: root.on_rgb_slider('r', self.value)
                    Label:
                        text: f"{int(root.r_val)}"
                        bold: True
                        font_size: '13sp'
                        color: 0.92, 0.96, 1, 1
                        size_hint_x: None
                        width: dp(34)
                        halign: 'right'
                        valign: 'middle'
                        text_size: self.size

                # Ползунок G
                BoxLayout:
                    size_hint_y: None
                    height: dp(32)
                    spacing: dp(8)
                    Label:
                        text: "G"
                        bold: True
                        font_size: '13sp'
                        color: 0.25, 0.92, 0.48, 1
                        size_hint_x: None
                        width: dp(20)
                    Slider:
                        id: slider_g
                        min: 0
                        max: 255
                        step: 1
                        value: root.g_val
                        value_track: True
                        value_track_color: 0.20, 0.78, 0.35, 0.95
                        value_track_width: dp(4)
                        cursor_size: dp(20), dp(20)
                        on_value: root.on_rgb_slider('g', self.value)
                    Label:
                        text: f"{int(root.g_val)}"
                        bold: True
                        font_size: '13sp'
                        color: 0.92, 0.96, 1, 1
                        size_hint_x: None
                        width: dp(34)
                        halign: 'right'
                        valign: 'middle'
                        text_size: self.size

                # Ползунок B
                BoxLayout:
                    size_hint_y: None
                    height: dp(32)
                    spacing: dp(8)
                    Label:
                        text: "B"
                        bold: True
                        font_size: '13sp'
                        color: 0.35, 0.65, 1.0, 1
                        size_hint_x: None
                        width: dp(20)
                    Slider:
                        id: slider_b
                        min: 0
                        max: 255
                        step: 1
                        value: root.b_val
                        value_track: True
                        value_track_color: 0.0, 0.48, 1.0, 0.95
                        value_track_width: dp(4)
                        cursor_size: dp(20), dp(20)
                        on_value: root.on_rgb_slider('b', self.value)
                    Label:
                        text: f"{int(root.b_val)}"
                        bold: True
                        font_size: '13sp'
                        color: 0.92, 0.96, 1, 1
                        size_hint_x: None
                        width: dp(34)
                        halign: 'right'
                        valign: 'middle'
                        text_size: self.size

                # Быстрые базовые цвета
                BoxLayout:
                    size_hint_y: None
                    height: dp(34)
                    spacing: dp(6)

                    QuickColorButton:
                        bg_color: 1.0, 0.0, 0.0, 0.9
                        on_release: root.select_quick_color(255, 0, 0)
                    QuickColorButton:
                        bg_color: 0.0, 0.9, 0.2, 0.9
                        on_release: root.select_quick_color(0, 255, 0)
                    QuickColorButton:
                        bg_color: 0.0, 0.35, 1.0, 0.95
                        on_release: root.select_quick_color(0, 0, 255)
                    QuickColorButton:
                        bg_color: 0.0, 0.95, 1.0, 0.9
                        on_release: root.select_quick_color(0, 255, 255)
                    QuickColorButton:
                        bg_color: 0.62, 0.0, 1.0, 0.9
                        on_release: root.select_quick_color(157, 0, 255)
                    QuickColorButton:
                        bg_color: 1.0, 0.42, 0.0, 0.95
                        on_release: root.select_quick_color(255, 106, 0)
                    QuickColorButton:
                        bg_color: 1.0, 1.0, 1.0, 0.95
                        on_release: root.select_quick_color(255, 255, 255)

            # 3. КАРТОЧКА КАСТОМНЫХ ПРОГРАММНЫХ РЕЖИМОВ
            GlassCard:
                BoxLayout:
                    size_hint_y: None
                    height: dp(24)
                    Label:
                        text: "ПЛАВНЫЕ ЭФФЕКТЫ (SMOOTH ENGINE)"
                        font_size: '12sp'
                        bold: True
                        color: 0.75, 0.90, 1, 0.95
                        halign: 'left'
                        valign: 'middle'
                        text_size: self.size
                    Label:
                        size_hint_x: None
                        width: dp(110)
                        text: f"Скорость: {int(root.effect_speed)}%"
                        font_size: '12sp'
                        bold: True
                        color: 0.55, 0.92, 1.0, 1
                        halign: 'right'
                        valign: 'middle'
                        text_size: self.size

                Slider:
                    size_hint_y: None
                    height: dp(30)
                    min: 1
                    max: 100
                    step: 1
                    value: root.effect_speed
                    value_track: True
                    value_track_color: 0.62, 0.35, 1.0, 0.95
                    value_track_width: dp(4)
                    cursor_size: dp(20), dp(20)
                    on_value: root.effect_speed = self.value

                GridLayout:
                    cols: 2
                    spacing: dp(8)
                    size_hint_y: None
                    height: dp(104)

                    GlassButton:
                        text: "Неоновое дыхание\\n(Breath)"
                        font_size: '12sp'
                        bg_color: (0.0, 0.68, 0.95, 0.42) if root.active_effect == 'breath' else (1, 1, 1, 0.07)
                        border_color: (0.25, 0.92, 1.0, 0.85) if root.active_effect == 'breath' else (0.667, 0.882, 1.0, 0.22)
                        on_release: root.toggle_effect('breath')

                    GlassButton:
                        text: "Киберпанк-перелив\\n(Cyberpunk Wave)"
                        font_size: '12sp'
                        bg_color: (0.62, 0.0, 1.0, 0.45) if root.active_effect == 'cyberpunk' else (1, 1, 1, 0.07)
                        border_color: (0.95, 0.25, 1.0, 0.85) if root.active_effect == 'cyberpunk' else (0.667, 0.882, 1.0, 0.22)
                        on_release: root.toggle_effect('cyberpunk')

                    GlassButton:
                        text: "Радужный микс\\n(Smooth Rainbow)"
                        font_size: '12sp'
                        bg_color: (0.10, 0.75, 0.55, 0.42) if root.active_effect == 'rainbow' else (1, 1, 1, 0.07)
                        border_color: (0.30, 1.0, 0.75, 0.85) if root.active_effect == 'rainbow' else (0.667, 0.882, 1.0, 0.22)
                        on_release: root.toggle_effect('rainbow')

                    GlassButton:
                        text: "Глубокий пульс\\n(Deep Pulse)"
                        font_size: '12sp'
                        bg_color: (0.90, 0.12, 0.45, 0.42) if root.active_effect == 'pulse' else (1, 1, 1, 0.07)
                        border_color: (1.0, 0.35, 0.68, 0.85) if root.active_effect == 'pulse' else (0.667, 0.882, 1.0, 0.22)
                        on_release: root.toggle_effect('pulse')

            # 4. КАРТОЧКА 6 DIY-СЛОТОВ
            GlassCard:
                Label:
                    size_hint_y: None
                    height: dp(22)
                    text: "DIY-СЛОТЫ ПАМЯТИ (6 ПРЕСЕТОВ)"
                    font_size: '12sp'
                    bold: True
                    color: 0.75, 0.90, 1, 0.95
                    halign: 'left'
                    valign: 'middle'
                    text_size: self.size

                GlassButton:
                    size_hint_y: None
                    height: dp(40)
                    text: "ВЫБЕРИ СЛОТ ДЛЯ ПЕРЕЗАПИСИ (ОТМЕНА)" if root.diy_record_mode else "Записать текущий цвет в слот"
                    font_size: '12.5sp'
                    bg_color: (0.95, 0.52, 0.05, 0.48) if root.diy_record_mode else (1, 1, 1, 0.09)
                    border_color: (1.0, 0.78, 0.20, 0.90) if root.diy_record_mode else (0.667, 0.882, 1.0, 0.28)
                    on_release: root.diy_record_mode = not root.diy_record_mode

                GridLayout:
                    id: diy_grid
                    cols: 3
                    spacing: dp(8)
                    size_hint_y: None
                    height: dp(96)
"""


class GlassButton(ButtonBehavior, Label):
    bg_color = ListProperty([1, 1, 1, 0.09])
    border_color = ListProperty([0.667, 0.882, 1.0, 0.25])
    radius_val = NumericProperty(dp(12))


class ReloadButton(GlassButton):
    pass


class QuickColorButton(GlassButton):
    pass


class DIYSlotButton(GlassButton):
    slot_index = NumericProperty(0)
    hex_code = StringProperty("#FF0055")


# =====================================================================
# ГЛАВНЫЙ КОРНЕВОЙ ВИДЖЕТ И ВЫСОКОТОЧНЫЙ ДВИЖОК ЭФФЕКТОВ
# =====================================================================
class RootUI(BoxLayout):
    ble_status_text = StringProperty("Инициализация BLE...")
    ble_led_color = ListProperty([1.0, 0.85, 0.1, 1.0])

    power_on = BooleanProperty(True)
    brightness = NumericProperty(100)

    r_val = NumericProperty(0)
    g_val = NumericProperty(240)
    b_val = NumericProperty(255)
    preview_rgb = ListProperty([0, 240, 255])
    current_hex = StringProperty("#00F0FF")

    effect_speed = NumericProperty(50)
    active_effect = StringProperty("")  # '', 'breath', 'cyberpunk', 'rainbow', 'pulse'

    diy_record_mode = BooleanProperty(False)
    diy_presets = ListProperty(
        ["#00F0FF", "#9D00FF", "#FF007F", "#00FF66", "#FF6A00", "#FFFFFF"]
    )

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._updating_sliders = False
        self._base_rgb = (0, 240, 255)
        self._diy_buttons = []

        # Переменные плавного генератора эффектов (отдельный поток 35 FPS)
        self._effect_phase = 0.0
        self._smooth_rgb = [0.0, 240.0, 255.0]
        self._effect_stop_flag = False

        # Отрисовка процедурного вертикального градиента (#225270 -> #08101C)
        self._grad_texture = self._create_vertical_gradient(
            top_rgb=(0x22, 0x52, 0x70), bottom_rgb=(0x08, 0x10, 0x1C)
        )
        with self.canvas.before:
            Color(1, 1, 1, 1)
            self._bg_rect = Rectangle(
                texture=self._grad_texture, pos=self.pos, size=self.size
            )
        self.bind(pos=self._update_bg_rect, size=self._update_bg_rect)

        self._load_diy_presets()
        Clock.schedule_once(self._build_diy_buttons, 0)

        # Запускаем фоновый BLE-движок
        self.ble = BLEDOMWorker(status_callback=self._on_ble_status)

        # Запускаем независимый поток плавных эффектов (не зависит от лагов UI)
        self._fx_thread = threading.Thread(
            target=self._effect_worker_loop, daemon=True
        )
        self._fx_thread.start()

    def _create_vertical_gradient(self, top_rgb, bottom_rgb) -> Texture:
        tex = Texture.create(size=(1, 256), colorfmt="rgba")
        buf = bytearray(256 * 4)
        for i in range(256):
            t = i / 255.0
            r = int(bottom_rgb[0] + (top_rgb[0] - bottom_rgb[0]) * t)
            g = int(bottom_rgb[1] + (top_rgb[1] - bottom_rgb[1]) * t)
            b = int(bottom_rgb[2] + (top_rgb[2] - bottom_rgb[2]) * t)
            idx = i * 4
            buf[idx : idx + 4] = bytes([r, g, b, 255])
        tex.blit_buffer(bytes(buf), colorfmt="rgba", bufferfmt="ubyte")
        tex.mag_filter = "linear"
        tex.min_filter = "linear"
        return tex

    def _update_bg_rect(self, *args):
        self._bg_rect.pos = self.pos
        self._bg_rect.size = self.size

    # ---------------- СТАТУС BLE И ПИТАНИЕ ----------------
    def _on_ble_status(self, state: str, text: str):
        self.ble_status_text = text
        if state == "connected":
            self.ble_led_color = [0.0, 0.92, 0.46, 1.0]
        elif state == "connecting":
            self.ble_led_color = [1.0, 0.85, 0.10, 1.0]
        else:
            self.ble_led_color = [1.0, 0.22, 0.30, 1.0]

    def on_reconnect_pressed(self):
        self.ble_status_text = "Переподключение..."
        self.ble_led_color = [1.0, 0.85, 0.10, 1.0]
        self.ble.request_reconnect()

    def toggle_power(self):
        self.power_on = not self.power_on
        self.ble.send_power(self.power_on)

    # ---------------- ЯРКОСТЬ ----------------
    def on_brightness_slider(self, val):
        v = max(1, min(100, int(round(val))))
        if v != int(self.brightness):
            self.brightness = v
        self.ble.send_brightness(v)

    def step_brightness(self, delta: int):
        new_val = max(1, min(100, int(self.brightness) + delta))
        self.brightness = new_val
        self.ids.slider_brightness.value = new_val
        self.ble.send_brightness(new_val)

    # ---------------- УПРАВЛЕНИЕ ЦВЕТОМ RGB ----------------
    def on_rgb_slider(self, channel: str, val: float):
        if self._updating_sliders:
            return

        if self.active_effect:
            self.active_effect = ""

        iv = max(0, min(255, int(round(val))))
        if channel == "r":
            self.r_val = iv
        elif channel == "g":
            self.g_val = iv
        elif channel == "b":
            self.b_val = iv

        r, g, b = int(self.r_val), int(self.g_val), int(self.b_val)
        self._base_rgb = (r, g, b)
        self._smooth_rgb = [float(r), float(g), float(b)]
        self.preview_rgb = [r, g, b]
        self.current_hex = rgb_to_hex(r, g, b)
        self.ble.send_rgb(r, g, b)

    def _set_static_rgb(self, r: int, g: int, b: int):
        if self.active_effect:
            self.active_effect = ""

        r_i, g_i, b_i = int(r), int(g), int(b)
        self._base_rgb = (r_i, g_i, b_i)
        self._smooth_rgb = [float(r_i), float(g_i), float(b_i)]

        self._updating_sliders = True
        try:
            self.r_val = r_i
            self.g_val = g_i
            self.b_val = b_i
            self.ids.slider_r.value = r_i
            self.ids.slider_g.value = g_i
            self.ids.slider_b.value = b_i
            self.preview_rgb = [r_i, g_i, b_i]
            self.current_hex = rgb_to_hex(r_i, g_i, b_i)
        finally:
            self._updating_sliders = False

        self.ble.send_rgb(r_i, g_i, b_i, force=True)

    def select_quick_color(self, r: int, g: int, b: int):
        self._set_static_rgb(r, g, b)

    # ---------------- ВЫСОКОПЛАВНЫЙ ДВИЖОК 4 РЕЖИМОВ (35 FPS) ----------------
    def toggle_effect(self, mode_name: str):
        if self.active_effect == mode_name:
            self.active_effect = ""
            self._set_static_rgb(*self._base_rgb)
        else:
            if sum(self._base_rgb) < 15:
                self._base_rgb = (0, 240, 255)
            self._smooth_rgb = [
                float(self._base_rgb[0]),
                float(self._base_rgb[1]),
                float(self._base_rgb[2]),
            ]
            self._effect_phase = 0.0
            self.active_effect = mode_name

    def _update_preview_badge_ui(self, r: int, g: int, b: int):
        # Обновляем ТОЛЬКО плашку предпросмотра и HEX, не дёргая ползунки!
        self.preview_rgb = [r, g, b]
        self.current_hex = rgb_to_hex(r, g, b)

    def _effect_worker_loop(self):
        """
        Отдельный высокоточный поток 35 FPS (каждые 28 мс).
        Рассчитывает цвет по гладким косинусным кривым + применяет экспоненциальный
        фильтр сглаживания (EMA), чтобы полностью убрать ступеньки и рывки.
        """
        last_t = time.perf_counter()
        ui_sync_counter = 0

        # Опорные точки Киберпанка в HSV для идеально ровной яркости при переливе:
        # #00F0FF (Cyan) -> #9D00FF (Purple) -> #FF007F (Neon Rose)
        cyber_anchors = [
            (0.0, 240.0, 255.0),
            (157.0, 0.0, 255.0),
            (255.0, 0.0, 127.0),
        ]

        while not self._effect_stop_flag:
            now = time.perf_counter()
            dt = min(0.08, max(0.001, now - last_t))
            last_t = now

            mode = self.active_effect
            if not mode or not self.power_on:
                time.sleep(0.04)
                last_t = time.perf_counter()
                continue

            # Плавная нелинейная шкала скорости (1..100%):
            # На 50% полный цикл занимает ~4 секунды (~140 плавных микрошагов!)
            s_norm = max(0.01, min(1.0, self.effect_speed / 100.0))
            cycle_freq = 0.07 + (s_norm ** 1.35) * 0.55

            if mode == "rainbow":
                # Радужный круг длиннее по спектру, поэтому вращаем его чуть спокойнее
                cycle_freq *= 0.65

            self._effect_phase = (self._effect_phase + dt * cycle_freq) % 1.0
            p = self._effect_phase

            br, bg, bb = self._base_rgb
            if br + bg + bb < 15:
                br, bg, bb = (0, 240, 255)

            if mode == "breath":
                # 1. Неоновое дыхание: чистая косинусная волна без резких провалов в 0
                wave = (1.0 - math.cos(p * 2.0 * math.pi)) * 0.5
                # Нижний порог 10%, чтобы ШИМ дешёвого контроллера не стробил на низах
                factor = 0.10 + 0.90 * wave
                tr, tg, tb = br * factor, bg * factor, bb * factor

            elif mode == "cyberpunk":
                # 2. Киберпанк-перелив с косинусной интерполяцией между 3 неонами
                pos = p * 3.0
                idx = int(pos) % 3
                frac = pos - int(pos)
                t = (1.0 - math.cos(frac * math.pi)) * 0.5
                c1 = cyber_anchors[idx]
                c2 = cyber_anchors[(idx + 1) % 3]
                tr = c1[0] + (c2[0] - c1[0]) * t
                tg = c1[1] + (c2[1] - c1[1]) * t
                tb = c1[2] + (c2[2] - c1[2]) * t

            elif mode == "rainbow":
                # 3. Плавный радужный микс по кругу HSV
                rf, gf, bf = colorsys.hsv_to_rgb(p, 1.0, 1.0)
                tr, tg, tb = rf * 255.0, gf * 255.0, bf * 255.0

            elif mode == "pulse":
                # 4. Глубокий пульс: широкие мягкие двойные волны (сердцебиение)
                b1 = math.exp(-((p - 0.24) ** 2) / 0.014)
                b2 = 0.78 * math.exp(-((p - 0.54) ** 2) / 0.020)
                env = min(1.0, 0.10 + 0.90 * max(b1, b2))
                tr, tg, tb = br * env, bg * env, bb * env
            else:
                time.sleep(0.03)
                continue

            # Экспоненциальное сглаживание (Low-Pass фильтр) убирает любые рывки
            alpha = min(1.0, dt * 14.0)
            self._smooth_rgb[0] += (tr - self._smooth_rgb[0]) * alpha
            self._smooth_rgb[1] += (tg - self._smooth_rgb[1]) * alpha
            self._smooth_rgb[2] += (tb - self._smooth_rgb[2]) * alpha

            out_r = max(0, min(255, int(round(self._smooth_rgb[0]))))
            out_g = max(0, min(255, int(round(self._smooth_rgb[1]))))
            out_b = max(0, min(255, int(round(self._smooth_rgb[2]))))

            # Отправляем напрямую в BLE Drop-Queue
            self.ble.send_rgb(out_r, out_g, out_b)

            # Плашку цвета в UI обновляем ~10 раз в секунду, не нагружая Kivy
            ui_sync_counter += 1
            if ui_sync_counter >= 3:
                ui_sync_counter = 0
                Clock.schedule_once(
                    lambda _dt, r=out_r, g=out_g, b=out_b: self._update_preview_badge_ui(
                        r, g, b
                    ),
                    0,
                )

            time.sleep(0.028)

    def stop_workers(self):
        self._effect_stop_flag = True
        self.ble.stop()

    # ---------------- 6 DIY-СЛОТОВ И СОХРАНЕНИЕ В JSON ----------------
    def _get_presets_filepath(self) -> str:
        local_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "diy_presets.json"
        )
        if platform in ("android", "ios"):
            app = App.get_running_app()
            if app and app.user_data_dir:
                return os.path.join(app.user_data_dir, "diy_presets.json")
        return local_path

    def _load_diy_presets(self):
        path = self._get_presets_filepath()
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list) and len(data) == 6:
                    self.diy_presets = [str(c).upper() for c in data]
            except Exception:
                pass

    def _save_diy_presets(self):
        path = self._get_presets_filepath()
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(list(self.diy_presets), f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _build_diy_buttons(self, *args):
        grid = self.ids.diy_grid
        grid.clear_widgets()
        self._diy_buttons.clear()

        for idx, hex_col in enumerate(self.diy_presets):
            btn = DIYSlotButton(slot_index=idx)
            self._style_diy_button(btn, hex_col)
            btn.bind(on_release=self._on_diy_slot_clicked)
            grid.add_widget(btn)
            self._diy_buttons.append(btn)

    def _style_diy_button(self, btn: DIYSlotButton, hex_col: str):
        r, g, b = hex_to_rgb(hex_col)
        btn.hex_code = rgb_to_hex(r, g, b)
        btn.text = btn.hex_code
        btn.bg_color = [r / 255.0, g / 255.0, b / 255.0, 0.88]
        btn.border_color = [1, 1, 1, 0.42]
        luminance = r * 0.299 + g * 0.587 + b * 0.114
        btn.color = (
            (0.07, 0.09, 0.13, 1.0)
            if luminance > 155
            else (0.98, 0.99, 1.0, 1.0)
        )

    def _on_diy_slot_clicked(self, btn: DIYSlotButton):
        idx = btn.slot_index
        if self.diy_record_mode:
            new_hex = rgb_to_hex(
                int(self.preview_rgb[0]),
                int(self.preview_rgb[1]),
                int(self.preview_rgb[2]),
            )
            presets = list(self.diy_presets)
            presets[idx] = new_hex
            self.diy_presets = presets
            self._style_diy_button(btn, new_hex)
            self._save_diy_presets()
            self.diy_record_mode = False
        else:
            r, g, b = hex_to_rgb(self.diy_presets[idx])
            self._set_static_rgb(r, g, b)


# =====================================================================
# ТОЧКА ВХОДА ПРИЛОЖЕНИЯ
# =====================================================================
class BLEDOMGlassApp(App):
    title = "BLEDOM Liquid Glass"

    def build(self):
        if platform not in ("android", "ios"):
            Window.size = (430, 880)
        self._request_android_ble_permissions()
        Builder.load_string(KV_MARKUP)
        self.root_ui = RootUI()
        return self.root_ui

    def _request_android_ble_permissions(self):
        if platform == "android":
            try:
                from android.permissions import Permission, request_permissions

                perms = [
                    Permission.BLUETOOTH,
                    Permission.BLUETOOTH_ADMIN,
                    Permission.ACCESS_FINE_LOCATION,
                    Permission.ACCESS_COARSE_LOCATION,
                ]
                for extra in ("BLUETOOTH_SCAN", "BLUETOOTH_CONNECT"):
                    if hasattr(Permission, extra):
                        perms.append(getattr(Permission, extra))
                request_permissions(perms)
            except Exception:
                pass

    def on_stop(self):
        if hasattr(self, "root_ui") and self.root_ui:
            self.root_ui.stop_workers()


if __name__ == "__main__":
    BLEDOMGlassApp().run()

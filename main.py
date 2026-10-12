# -*- coding: utf-8 -*-
"""
AI Desktop Pet
==============

A Windows desktop pet powered by the DeepSeek chat model. It walks,
jumps, drags, talks, and evolves its attitude toward the mouse cursor
through ongoing interaction.

Key behaviours
--------------
* Layered, always-on-top window with per-pixel alpha.
* Quad animation loaded from animation.json. Frames always come from
  assets/frames; the "import" key is ignored.
* Physics: gravity, inertia, collision bounce. Gravity is read from
  config.json and can be changed live.
* Mouse interaction: the pet can approach, flee, or ignore the cursor.
  Mode and autonomous behaviour rates are chosen by the model.
* The model also sees how often the pet has spoken on its own in the
  recent past, so a chatty personality can lower its own rate instead
  of talking forever. A hard cap is applied as a safety net.
* Personality is re-evaluated from the recent dialogue log; the new
  text is written back to config.json.
* config.json, animation.json and ai_debug.txt are watched and
  reloaded live while the app is running.
* Speech bubble with fade-in / fade-out. In chat, a model that stays
  silent is shown as "......". In other situations silence is silent.

Dependencies
------------
    pip install pillow requests psutil

"""

import base64
import ctypes
import ctypes.wintypes as wt
import io
import json
import math
import os
import queue
import random
import re
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

import requests
from PIL import Image, ImageDraw, ImageFont

try:
    import psutil
except ImportError:
    psutil = None

try:
    RESAMPLE = Image.Resampling.BICUBIC
except AttributeError:
    RESAMPLE = Image.BICUBIC


# =========================================================================
# Win32 API bindings
# =========================================================================

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_longlong

WS_EX_LAYERED = 0x00080000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TRANSPARENT = 0x00000020
WS_POPUP = 0x80000000
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
SW_SHOW = 5
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010

WH_MOUSE_LL = 14
WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", ctypes.c_long * 2),
                ("mouseData", wt.DWORD),
                ("flags", wt.DWORD),
                ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wt.DWORD),
                ("scanCode", wt.DWORD),
                ("flags", wt.DWORD),
                ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_MOUSEMOVE = 0x0200
WM_RBUTTONUP = 0x0205
WM_DESTROY = 0x0002
WM_NCHITTEST = 0x0084
HTCLIENT = 1
HTTRANSPARENT = -1

SetCapture = user32.SetCapture
SetCapture.argtypes = [wt.HWND]
SetCapture.restype = wt.HWND
ReleaseCapture = user32.ReleaseCapture
ReleaseCapture.argtypes = []
ReleaseCapture.restype = wt.BOOL


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
        ("SourceConstantAlpha", ctypes.c_byte), ("AlphaFormat", ctypes.c_byte),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long), ("biPlanes", wt.WORD),
        ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


# =========================================================================
# Internationalisation
# =========================================================================

LANGUAGES = {
    "zh": {"talk": "聊天", "language": "语言", "close": "关闭",
           "loading": "加载中",
           "err_key": "请先在 config.json 里填写 deepseek_api_key",
           "err_personality": "请先在 config.json 里填写 personality 性格设定",
           "err_ai": "无法连接到 DeepSeek，请检查网络或 API Key", "ok": "就绪",
           "chat_title": "聊天",
           "chat_hint": "Enter 发送，Shift+Enter 换行，Esc 取消",
           "chat_send": "发送", "chat_cancel": "取消"},
    "en": {"talk": "Talk", "language": "Language", "close": "Close",
           "loading": "Loading",
           "err_key": "Please fill deepseek_api_key in config.json",
           "err_personality": "Please fill personality in config.json",
           "err_ai": "Cannot reach DeepSeek, check network or API key",
           "ok": "Ready",
           "chat_title": "Chat",
           "chat_hint": "Enter to send, Shift+Enter for newline, Esc to cancel",
           "chat_send": "Send", "chat_cancel": "Cancel"},
    "ja": {"talk": "話す", "language": "言語", "close": "閉じる",
           "loading": "読み込み中",
           "err_key": "config.json に deepseek_api_key を入力してください",
           "err_personality": "config.json に personality を入力してください",
           "err_ai": "DeepSeek に接続できません。ネットワークか API Key を確認してください",
           "ok": "準備完了",
           "chat_title": "チャット",
           "chat_hint": "Enter で送信、Shift+Enter で改行、Esc でキャンセル",
           "chat_send": "送信", "chat_cancel": "キャンセル"},
    "ko": {"talk": "대화", "language": "언어", "close": "닫기",
           "loading": "불러오는 중",
           "err_key": "config.json 에 deepseek_api_key 를 입력하세요",
           "err_personality": "config.json 에 personality 를 입력하세요",
           "err_ai": "DeepSeek 에 연결할 수 없습니다. 네트워크나 API Key 를 확인하세요",
           "ok": "준비 완료",
           "chat_title": "대화",
           "chat_hint": "Enter 로 전송, Shift+Enter 로 줄바꿈, Esc 로 취소",
           "chat_send": "전송", "chat_cancel": "취소"},
    "fr": {"talk": "Parler", "language": "Langue", "close": "Fermer",
           "loading": "Chargement",
           "err_key": "Veuillez renseigner deepseek_api_key dans config.json",
           "err_personality": "Veuillez renseigner personality dans config.json",
           "err_ai": "Impossible de joindre DeepSeek, vérifiez le réseau ou la clé API",
           "ok": "Prêt",
           "chat_title": "Discussion",
           "chat_hint": "Entrée pour envoyer, Maj+Entrée pour un saut de ligne, Échap pour annuler",
           "chat_send": "Envoyer", "chat_cancel": "Annuler"},
    "de": {"talk": "Sprechen", "language": "Sprache", "close": "Schließen",
           "loading": "Lädt",
           "err_key": "Bitte deepseek_api_key in config.json eintragen",
           "err_personality": "Bitte personality in config.json eintragen",
           "err_ai": "DeepSeek nicht erreichbar. Netzwerk oder API-Key prüfen",
           "ok": "Bereit",
           "chat_title": "Chat",
           "chat_hint": "Enter zum Senden, Shift+Enter für Zeilenumbruch, Esc zum Abbrechen",
           "chat_send": "Senden", "chat_cancel": "Abbrechen"},
    "es": {"talk": "Hablar", "language": "Idioma", "close": "Cerrar",
           "loading": "Cargando",
           "err_key": "Rellena deepseek_api_key en config.json",
           "err_personality": "Rellena personality en config.json",
           "err_ai": "No se puede conectar con DeepSeek, revisa la red o la API Key",
           "ok": "Listo",
           "chat_title": "Charla",
           "chat_hint": "Intro para enviar, Mayús+Intro para nueva línea, Esc para cancelar",
           "chat_send": "Enviar", "chat_cancel": "Cancelar"},
    "pt": {"talk": "Falar", "language": "Idioma", "close": "Fechar",
           "loading": "Carregando",
           "err_key": "Preencha deepseek_api_key no config.json",
           "err_personality": "Preencha personality no config.json",
           "err_ai": "Não foi possível conectar ao DeepSeek, verifique a rede ou a API Key",
           "ok": "Pronto",
           "chat_title": "Conversa",
           "chat_hint": "Enter para enviar, Shift+Enter para nova linha, Esc para cancelar",
           "chat_send": "Enviar", "chat_cancel": "Cancelar"},
    "ru": {"talk": "Говорить", "language": "Язык", "close": "Закрыть",
           "loading": "Загрузка",
           "err_key": "Укажите deepseek_api_key в config.json",
           "err_personality": "Укажите personality в config.json",
           "err_ai": "Не удаётся подключиться к DeepSeek, проверьте сеть или API Key",
           "ok": "Готово",
           "chat_title": "Чат",
           "chat_hint": "Enter — отправить, Shift+Enter — новая строка, Esc — отмена",
           "chat_send": "Отправить", "chat_cancel": "Отмена"},
    "it": {"talk": "Parla", "language": "Lingua", "close": "Chiudi",
           "loading": "Caricamento",
           "err_key": "Inserisci deepseek_api_key in config.json",
           "err_personality": "Inserisci personality in config.json",
           "err_ai": "Impossibile contattare DeepSeek, controlla rete o API Key",
           "ok": "Pronto",
           "chat_title": "Chat",
           "chat_hint": "Invio per inviare, Maiusc+Invio per nuova riga, Esc per annullare",
           "chat_send": "Invia", "chat_cancel": "Annulla"},
    "ar": {"talk": "تحدث", "language": "اللغة", "close": "إغلاق",
           "loading": "جارٍ التحميل",
           "err_key": "املأ deepseek_api_key في config.json",
           "err_personality": "املأ personality في config.json",
           "err_ai": "تعذّر الاتصال بـ DeepSeek، تحقق من الشبكة أو المفتاح",
           "ok": "جاهز",
           "chat_title": "محادثة",
           "chat_hint": "Enter للإرسال، Shift+Enter لسطر جديد، Esc للإلغاء",
           "chat_send": "إرسال", "chat_cancel": "إلغاء"},
    "hi": {"talk": "बात करें", "language": "भाषा", "close": "बंद करें",
           "loading": "लोड हो रहा है",
           "err_key": "config.json में deepseek_api_key भरें",
           "err_personality": "config.json में personality भरें",
           "err_ai": "DeepSeek से कनेक्ट नहीं हो सका, नेटवर्क या API Key जाँचें",
           "ok": "तैयार",
           "chat_title": "चैट",
           "chat_hint": "भेजने के लिए Enter, नई पंक्ति के लिए Shift+Enter, रद्द करने के लिए Esc",
           "chat_send": "भेजें", "chat_cancel": "रद्द करें"},
    "th": {"talk": "พูดคุย", "language": "ภาษา", "close": "ปิด",
           "loading": "กำลังโหลด",
           "err_key": "กรุณากรอก deepseek_api_key ใน config.json",
           "err_personality": "กรุณากรอก personality ใน config.json",
           "err_ai": "ไม่สามารถเชื่อมต่อ DeepSeek ได้ ตรวจสอบเครือข่ายหรือ API Key",
           "ok": "พร้อม",
           "chat_title": "แชท",
           "chat_hint": "Enter เพื่อส่ง, Shift+Enter ขึ้นบรรทัดใหม่, Esc เพื่อยกเลิก",
           "chat_send": "ส่ง", "chat_cancel": "ยกเลิก"},
    "vi": {"talk": "Trò chuyện", "language": "Ngôn ngữ", "close": "Đóng",
           "loading": "Đang tải",
           "err_key": "Hãy điền deepseek_api_key vào config.json",
           "err_personality": "Hãy điền personality vào config.json",
           "err_ai": "Không kết nối được DeepSeek, kiểm tra mạng hoặc API Key",
           "ok": "Sẵn sàng",
           "chat_title": "Trò chuyện",
           "chat_hint": "Enter để gửi, Shift+Enter xuống dòng, Esc để hủy",
           "chat_send": "Gửi", "chat_cancel": "Hủy"},
    "tr": {"talk": "Konuş", "language": "Dil", "close": "Kapat",
           "loading": "Yükleniyor",
           "err_key": "config.json içinde deepseek_api_key girin",
           "err_personality": "config.json içinde personality girin",
           "err_ai": "DeepSeek'e bağlanılamadı, ağı veya API Key'i kontrol edin",
           "ok": "Hazır",
           "chat_title": "Sohbet",
           "chat_hint": "Göndermek için Enter, yeni satır için Shift+Enter, iptal için Esc",
           "chat_send": "Gönder", "chat_cancel": "İptal"},
    "nl": {"talk": "Praten", "language": "Taal", "close": "Sluiten",
           "loading": "Laden",
           "err_key": "Vul deepseek_api_key in config.json in",
           "err_personality": "Vul personality in config.json in",
           "err_ai": "Kan DeepSeek niet bereiken, controleer netwerk of API Key",
           "ok": "Gereed",
           "chat_title": "Chat",
           "chat_hint": "Enter om te verzenden, Shift+Enter voor nieuwe regel, Esc om te annuleren",
           "chat_send": "Verzenden", "chat_cancel": "Annuleren"},
}

# All supported language codes are declared above, so no fallback loop
# is needed. If a new code is added to LANGUAGE_NAMES it should be
# added to LANGUAGES as well.

LANGUAGE_NAMES = {
    "zh": "中文", "en": "English", "ja": "日本語", "ko": "한국어",
    "fr": "Français", "de": "Deutsch", "es": "Español", "pt": "Português",
    "ru": "Русский", "it": "Italiano", "ar": "العربية", "hi": "हिन्दी",
    "th": "ไทย", "vi": "Tiếng Việt", "tr": "Türkçe", "nl": "Nederlands",
}

def tr(lang, key):
    """Return the translated string for the given language code."""
    return LANGUAGES.get(lang, LANGUAGES["en"]).get(key, key)


# =========================================================================
# Paths and configuration
# =========================================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
ANIM_PATH = os.path.join(BASE_DIR, "animation.json")
ASSETS_DIR = os.path.join(BASE_DIR, "assets")
FRAMES_DIR = os.path.join(ASSETS_DIR, "frames")
LOGS_DIR = os.path.join(BASE_DIR, "logs")
AI_DEBUG_PATH = os.path.join(LOGS_DIR, "ai_debug.txt")

DEFAULT_TALK_CHANCE = 0.02
DEFAULT_SIGNAL_CHANCE = 0.01

DEFAULT_CONFIG = {
    "deepseek_api_key": "",
    "deepseek_model": "deepseek-chat",
    "deepseek_url": "https://api.deepseek.com/v1/chat/completions",
    "language": "zh",
    "autostart": False,
    "personality": "",
    "memory_lines": 20,
    "snapshot_memory": 120,
    "pet_size": 160,
    "gravity": 1.1,
    "draggable": True,
}

FPS_TARGET = 30
PHYS_MS = 30
INERTIA_SAMPLE = 0.03
MAX_INERTIA_VX = 22.0
MAX_INERTIA_VY = 14.0
INERTIA_FRICTION_GROUND = 0.75
INERTIA_FRICTION_AIR = 0.985
COLLIDE_MIN_SPEED = 3.0
COLLIDE_COOLDOWN = 0.25

RECORDER_HINTS = (
    "obs64", "obs32", "obs", "xsplit", "streamlabs", "bandicam", "fraps",
    "snipaste", "snippingtool", "screenshot", "sharex", "greenshot",
    "lightshot", "picpick", "screenrecorder", "camtasia", "capture",
    "qq", "wechat", "weixin", "dingtalk", "litescreen",
)
RECORDER_POLL_INTERVAL = 3.0

SAMPLE_INTERVAL = 0.5
DEFAULT_SNAPSHOT_MEMORY = 120
DEBUG_LOG_MAX_LINES = 5000

INPUT_EVENT_BUFFER = 40

BUBBLE_FADE_IN = 0.20
BUBBLE_FADE_OUT = 0.30
BUBBLE_LINGER = 5.0
BUBBLE_MAX_WIDTH = 520
BUBBLE_FONT_SIZE = 16
BUBBLE_LINE_HEIGHT = 22

JUMP_HEIGHT_MIN, JUMP_HEIGHT_MAX = 5.0, 200.0
MOVE_DISTANCE_MIN, MOVE_DISTANCE_MAX = 10.0, 4000.0
MOVE_SPEED_MIN, MOVE_SPEED_MAX = 1.0, 40.0
SCALE_MIN, SCALE_MAX = 0.3, 3.0

PET_SIZE = 160

DRAG_REACT_BASE_INTERVAL = 8.0
DRAG_REACT_MIN_DURATION = 1.0
DRAG_REACT_MIN_DISTANCE = 80.0
DRAG_REACT_MAX_PER_DRAG = 2

MOUSE_PROXIMITY_RADIUS = 260.0
MOUSE_PROXIMITY_DEADZONE = 60.0
MOUSE_PROXIMITY_HYSTERESIS = 30.0
MOUSE_PROXIMITY_SPEED = 6.0
MOUSE_PROXIMITY_COOLDOWN = 0.3
MOUSE_PROXIMITY_VERTICAL_RATIO = 1.5

FLEE_REACT_INTERVAL = 6.0

CLOSE_FADE_SECONDS = 1.6
CLOSE_FADE_STEP_MS = 40

# How often the watcher thread asks the model to re-evaluate the state.
STATE_REFRESH_INTERVAL = 60.0

# Watcher tick period.
WATCH_INTERVAL = 1.0

# Autonomous output accounting.
# The model sees how many times the pet has spoken / acted on its own
# during the last AUTONOMOUS_HISTORY_SECONDS seconds, and the runtime
# applies a hard cap if the pet has been bursting.
AUTONOMOUS_HISTORY_SECONDS = 120.0
AUTONOMOUS_BURST_WINDOW = 30.0
AUTONOMOUS_BURST_LIMIT = 5
AUTONOMOUS_BURST_DAMPING = 0.5

SILENCE_PLACEHOLDER = "......"


def ensure_dirs():
    """Make sure all runtime directories exist."""
    os.makedirs(ASSETS_DIR, exist_ok=True)
    os.makedirs(FRAMES_DIR, exist_ok=True)
    os.makedirs(LOGS_DIR, exist_ok=True)


def _trim_debug_log():
    """Keep ai_debug.txt below DEBUG_LOG_MAX_LINES lines."""
    try:
        if not os.path.exists(AI_DEBUG_PATH):
            return
        with open(AI_DEBUG_PATH, "r", encoding="utf-8",
                  errors="ignore") as f:
            lines = f.readlines()
        if len(lines) <= DEBUG_LOG_MAX_LINES:
            return
        with open(AI_DEBUG_PATH, "w", encoding="utf-8") as f:
            f.writelines(lines[-DEBUG_LOG_MAX_LINES:])
    except Exception:
        pass


def log_ai(kind, text):
    """Append a line to the debug log, trimming it first if needed."""
    try:
        _trim_debug_log()
        with open(AI_DEBUG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {kind}: {text}\n")
    except Exception:
        pass


def load_config():
    """Load config.json, filling in any missing keys from the defaults."""
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
        return dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    merged = dict(DEFAULT_CONFIG)
    merged.update({k: v for k, v in cfg.items() if k in DEFAULT_CONFIG})
    return merged


def save_config(cfg):
    """Persist the current configuration back to config.json."""
    try:
        out = {k: cfg.get(k, DEFAULT_CONFIG[k]) for k in DEFAULT_CONFIG}
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def load_animation():
    """Load animation.json.

    If the file is missing or broken, an empty animation spec is
    returned. The pet then simply does not render anything instead of
    falling back to a hardcoded default. The file is never created
    automatically.
    """
    if not os.path.exists(ANIM_PATH):
        return {"triggers": {}}
    try:
        with open(ANIM_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"triggers": {}}


# =========================================================================
# Desktop environment snapshot
# =========================================================================

class EnvironmentInfo:
    """Collects a compact snapshot of the current desktop state."""

    @staticmethod
    def snapshot():
        """Return screen size and cursor position."""
        sw = user32.GetSystemMetrics(0)
        sh = user32.GetSystemMetrics(1)
        pt = POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        mouse = (pt.x, pt.y)
        return {"screen": (sw, sh), "mouse": mouse}

    @staticmethod
    def describe_position(pet_rect, screen, mouse_xy=None):
        """Return a human-readable description of the pet's position."""
        sw, sh = screen
        l, t, r, b = pet_rect
        cx, cy = (l + r) // 2, (t + b) // 2
        nx = cx / max(1, sw)
        ny = cy / max(1, sh)
        hx = "left third" if nx < 0.33 else (
            "middle horizontally" if nx < 0.66 else "right third")
        vy = "upper third" if ny < 0.33 else (
            "middle vertically" if ny < 0.66 else "lower third")
        out = (
            f"YOUR EXACT POSITION: "
            f"top-left=({l},{t}), bottom-right=({r},{b}), "
            f"center=({cx},{cy}), size={r-l}x{b-t}. "
            f"You are in the {vy} and {hx} of a {sw}x{sh} screen."
        )
        if mouse_xy is not None:
            mx, my = mouse_xy
            dist = math.hypot(mx - cx, my - cy)
            out += (
                f"\nMOUSE DISTANCE: cursor=({mx},{my}); "
                f"distance from your center = {dist:.0f}px; "
                f"relative offset = ({mx - cx:+.0f},{my - cy:+.0f})."
            )
        return out

    @staticmethod
    def describe(env, pet_rect, extra=None, mouse_xy=None):
        """Return the full environment block fed to the model."""
        sw, sh = env["screen"]
        mx, my = env["mouse"]
        lines = [
            f"Screen resolution: {sw}x{sh}",
            EnvironmentInfo.describe_position(pet_rect, (sw, sh),
                                              mouse_xy or (mx, my)),
        ]
        if extra:
            lines.extend(extra)
        return "\n".join(lines)


# =========================================================================
# Global input monitor
# =========================================================================

class InputMonitor:
    """Low-level mouse and keyboard hooks pushing events into a ring buffer."""

    def __init__(self):
        self._events = []
        self._lock = threading.Lock()
        self._last_event_t = 0.0
        self._hook_mouse = None
        self._hook_kb = None
        self._mouse_proc = None
        self._kb_proc = None
        self._running = False
        self._thread = None

    def start(self):
        """Install the hooks in a background thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._install_hooks,
                                        daemon=True)
        self._thread.start()

    def stop(self):
        """Remove both hooks and stop the background thread."""
        self._running = False
        try:
            if self._hook_mouse:
                user32.UnhookWindowsHookEx(self._hook_mouse)
        except Exception:
            pass
        try:
            if self._hook_kb:
                user32.UnhookWindowsHookEx(self._hook_kb)
        except Exception:
            pass
        self._hook_mouse = None
        self._hook_kb = None

    def _install_hooks(self):
        """Install WH_MOUSE_LL and WH_KEYBOARD_LL and pump messages."""
        HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, ctypes.c_int,
                                      wt.WPARAM, wt.LPARAM)

        def mouse_cb(nCode, wParam, lParam):
            if nCode >= 0:
                try:
                    info = ctypes.cast(
                        lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                    self._push("mouse", info.pt[0], info.pt[1], wParam)
                except Exception:
                    pass
            return user32.CallNextHookEx(0, nCode, wParam, lParam)

        def kb_cb(nCode, wParam, lParam):
            if nCode >= 0 and wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                try:
                    info = ctypes.cast(
                        lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                    self._push("key", int(info.vkCode), 0, wParam)
                except Exception:
                    pass
            return user32.CallNextHookEx(0, nCode, wParam, lParam)

        self._mouse_proc = HOOKPROC(mouse_cb)
        self._kb_proc = HOOKPROC(kb_cb)
        hinst = kernel32.GetModuleHandleW(None)
        try:
            self._hook_mouse = user32.SetWindowsHookExW(
                WH_MOUSE_LL, self._mouse_proc, hinst, 0)
        except Exception:
            self._hook_mouse = None
        try:
            self._hook_kb = user32.SetWindowsHookExW(
                WH_KEYBOARD_LL, self._kb_proc, hinst, 0)
        except Exception:
            self._hook_kb = None

        msg = wt.MSG()
        while self._running:
            try:
                r = user32.GetMessageW(ctypes.byref(msg), 0, 0, 0)
                if r in (0, -1):
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            except Exception:
                break

    def _push(self, kind, a, b, wParam):
        """Record one event, keeping the buffer at a fixed size."""
        now = time.monotonic()
        with self._lock:
            self._last_event_t = now
            if kind == "mouse":
                self._events.append(
                    (now, f"mouse {self._mouse_event_name(wParam)}"))
            else:
                self._events.append((now, f"key vk={a}"))
            if len(self._events) > INPUT_EVENT_BUFFER:
                self._events = self._events[-INPUT_EVENT_BUFFER:]

    @staticmethod
    def _mouse_event_name(wParam):
        """Translate a mouse hook code into a short name."""
        if wParam == WM_MOUSEMOVE:
            return "move"
        if wParam == WM_LBUTTONDOWN:
            return "left-down"
        if wParam == WM_LBUTTONUP:
            return "left-up"
        if wParam == WM_RBUTTONUP:
            return "right-up"
        return f"event-{wParam}"

    def recent_summary(self, limit=8):
        """Return a short summary of the most recent input events."""
        with self._lock:
            tail = self._events[-limit:]
            last_t = self._last_event_t
        if not tail:
            return "no recent input events"
        now = time.monotonic()
        idle = now - last_t if last_t else 999
        lines = [f"Idle for {idle:.1f}s since last input."]
        collapsed = []
        move_count = 0
        for _t, text in tail:
            if text == "mouse move":
                move_count += 1
                continue
            if move_count:
                collapsed.append(f"mouse moved {move_count} times")
                move_count = 0
            collapsed.append(text)
        if move_count:
            collapsed.append(f"mouse moved {move_count} times")
        lines.extend(collapsed)
        return "\n".join(f"  - {ln}" for ln in lines)


# =========================================================================
# Single instance and autostart
# =========================================================================

_MUTEX_HANDLE = None


def acquire_single_instance():
    """Return the mutex handle, or None if another instance is running."""
    global _MUTEX_HANDLE
    if _MUTEX_HANDLE is not None:
        return _MUTEX_HANDLE
    kernel32.CreateMutexW.restype = wt.HANDLE
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
    for _ in range(10):
        h = kernel32.CreateMutexW(None, True, "AIPet_SingleInstance_Mutex")
        if kernel32.GetLastError() != 183:
            _MUTEX_HANDLE = h
            return h
        time.sleep(0.2)
    return None


def release_single_instance():
    """Release the single-instance mutex if it is held."""
    global _MUTEX_HANDLE
    try:
        if _MUTEX_HANDLE:
            kernel32.ReleaseMutex(_MUTEX_HANDLE)
            kernel32.CloseHandle(_MUTEX_HANDLE)
            _MUTEX_HANDLE = None
    except Exception:
        pass


_AUTOSTART_KEY_NAMES = ("AIPet", "AI Desktop Pet")


def set_autostart(enable):
    """Add or remove the current executable from the user's Run key.

    When enable is False, all known entry names are removed so a stale
    entry from an older build cannot sneak back in.
    """
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE)
    except Exception:
        return
    try:
        if enable:
            if getattr(sys, "frozen", False):
                cmd = f'"{sys.executable}"'
            else:
                cmd = (f'"{sys.executable}" '
                       f'"{os.path.abspath(sys.argv[0])}"')
            winreg.SetValueEx(key, "AIPet", 0, winreg.REG_SZ, cmd)
        else:
            for name in _AUTOSTART_KEY_NAMES:
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    pass
    except Exception:
        pass
    finally:
        try:
            winreg.CloseKey(key)
        except Exception:
            pass


# =========================================================================
# Screen capture detector
# =========================================================================

class ScreenCaptureDetector:
    """Polls the process list for known screen recording / capture tools."""

    def __init__(self):
        self.active = False
        self.hits = []
        self._last_poll = 0.0

    def poll(self):
        """Refresh the list of recording tools if the interval elapsed."""
        if psutil is None:
            return
        now = time.time()
        if now - self._last_poll < RECORDER_POLL_INTERVAL:
            return
        self._last_poll = now
        hits = []
        try:
            for p in psutil.process_iter(["name"]):
                try:
                    n = (p.info.get("name") or "").lower()
                    if not n:
                        continue
                    for hint in RECORDER_HINTS:
                        if hint in n:
                            hits.append(n)
                            break
                except Exception:
                    continue
        except Exception:
            return
        self.hits = sorted(set(hits))
        self.active = bool(self.hits)

    def describe(self):
        """Return a short description of the current capture state."""
        if not self.active:
            return "no screen recording / screenshot tool is running"
        return "user may be recording or capturing the screen"


# =========================================================================
# Drag gesture analysis
# =========================================================================

class DragGesture:
    """Tracks speed, distance and reversals during a mouse drag."""

    def __init__(self):
        self.reset()

    def reset(self):
        """Clear all counters."""
        self.start_t = 0.0
        self.last_t = 0.0
        self.start_x = 0
        self.start_y = 0
        self.last_x = 0
        self.last_y = 0
        self.max_speed = 0.0
        self.sum_speed = 0.0
        self.samples = 0
        self.total_distance = 0.0
        self.reversals = 0
        self._last_dir = 0

    def start(self, x, y):
        """Begin recording a new drag from (x, y)."""
        self.reset()
        self.start_t = self.last_t = time.monotonic()
        self.start_x = self.last_x = x
        self.start_y = self.last_y = y

    def update(self, x, y):
        """Feed a new cursor position into the recorder."""
        now = time.monotonic()
        dt = now - self.last_t
        if dt <= 0:
            return
        dx = x - self.last_x
        dy = y - self.last_y
        dist = math.hypot(dx, dy)
        speed = dist / dt
        self.max_speed = max(self.max_speed, speed)
        self.sum_speed += speed
        self.samples += 1
        self.total_distance += dist
        sgn = 0 if abs(dx) < 1 else (1 if dx > 0 else -1)
        if sgn != 0:
            if self._last_dir != 0 and sgn != self._last_dir:
                self.reversals += 1
            self._last_dir = sgn
        self.last_x = x
        self.last_y = y
        self.last_t = now

    def metrics(self):
        """Return a dict with the aggregated drag metrics."""
        duration = max(0.001, self.last_t - self.start_t)
        avg_speed = self.sum_speed / max(1, self.samples)
        return {
            "duration_s": round(duration, 2),
            "max_speed_px_s": round(self.max_speed, 0),
            "avg_speed_px_s": round(avg_speed, 0),
            "distance_px": round(self.total_distance, 0),
            "reversals": self.reversals,
        }

    def force_label(self):
        """Return a qualitative label for the drag force."""
        mx = self.metrics()["max_speed_px_s"]
        if mx < 200:
            return "very gentle"
        if mx < 500:
            return "gentle"
        if mx < 900:
            return "firm"
        if mx < 1500:
            return "strong"
        return "violent"

    def describe(self):
        """Return a one-line description of the drag."""
        m = self.metrics()
        return (f"force={self.force_label()}, "
                f"max_speed={m['max_speed_px_s']}px/s, "
                f"avg_speed={m['avg_speed_px_s']}px/s, "
                f"distance={m['distance_px']}px, "
                f"duration={m['duration_s']}s, "
                f"reversals={m['reversals']}")


# =========================================================================
# Loading screen
# =========================================================================

class LoadingScreen:
    """Small Tk window shown while the pet initialises."""

    def __init__(self, lang):
        self.root = tk.Tk()
        self.root.title("AI Pet")
        self.root.geometry("380x120")
        self.root.resizable(False, False)
        try:
            self.root.attributes("-topmost", True)
        except Exception:
            pass
        self.label = tk.Label(self.root, text=tr(lang, "loading"), anchor="w")
        self.label.pack(fill="x", padx=14, pady=(14, 4))
        self.bar = ttk.Progressbar(self.root, mode="determinate", maximum=100)
        self.bar.pack(fill="x", padx=14, pady=(0, 14))
        self.root.update()

    def set(self, pct, text):
        """Update the progress bar and its caption."""
        try:
            self.bar["value"] = max(0, min(100, pct))
            self.label.config(text=text)
            self.root.update()
        except Exception:
            pass

    def close(self):
        """Destroy the loading window."""
        try:
            self.root.destroy()
        except Exception:
            pass


# =========================================================================
# Dialogue memory
# =========================================================================

class DebugMemory:
    """Reads the tail of ai_debug.txt to recover recent dialogue."""

    def __init__(self, max_lines=20):
        self.max_lines = max_lines

    def read_recent(self, n=None):
        """Return the last n AI and user lines joined by newlines."""
        n = n or self.max_lines
        if not os.path.exists(AI_DEBUG_PATH):
            return ""
        try:
            with open(AI_DEBUG_PATH, "r", encoding="utf-8",
                      errors="ignore") as f:
                lines = f.readlines()
        except Exception:
            return ""
        picked = []
        for ln in lines:
            if " OK: " in ln:
                pos = ln.find(" OK: ")
                text = ln[pos + 5:].strip()
                if text:
                    picked.append(f"AI: {text}")
            elif " USER: " in ln:
                pos = ln.find(" USER: ")
                text = ln[pos + 7:].strip()
                if text:
                    picked.append(f"User: {text}")
        tail = picked[-n:]
        return "\n".join(f"- {t}" for t in tail)


# =========================================================================
# DeepSeek brain
# =========================================================================

ACTION_RE = re.compile(
    r"\[\[("
    r"move:(?:left|right|stop)"
    r"(?:;speed=-?\d+(?:\.\d+)?)?"
    r"(?:;distance=-?\d+(?:\.\d+)?)?"
    r"|jump(?:;height=-?\d+(?:\.\d+)?)?"
    r"|scale:(?:\+|-)?\d+(?:\.\d+)?"
    r"|refuse|rest"
    r")\]\]",
    re.IGNORECASE)


class DeepSeekBrain:
    """Thin wrapper around the DeepSeek chat completions API."""

    def __init__(self, api_key, url, model, self_image_b64, personality,
                 language_code="zh"):
        self.api_key = api_key
        self.url = url
        self.model = model
        self.self_image_b64 = self_image_b64
        self.personality = personality
        self.language_code = language_code
        self.history = []
        self.image_sent = False

    def probe_state(self, memory_text="", activity_text=""):
        """Ask the model for the current pet state.

        Returns a dict with four keys:
            mouse_mode            : "approach" | "flee" | "ignore"
            talk_chance           : float 0.0 - 1.0
            signal_chance         : float 0.0 - 1.0
            personality_updated   : str or "" (may be empty)

        activity_text summarises how often the pet has spoken on its
        own recently, so a chatty personality can lower its own rate
        instead of talking forever.
        """
        fallback = {
            "mouse_mode": "ignore",
            "talk_chance": DEFAULT_TALK_CHANCE,
            "signal_chance": DEFAULT_SIGNAL_CHANCE,
            "personality_updated": "",
        }
        if not self.api_key:
            return fallback

        history_block = ""
        if memory_text:
            history_block = (
                "\nRecent interaction history:\n"
                f"{memory_text}\n"
            )
        activity_block = ""
        if activity_text:
            activity_block = (
                "\nYour own recent output activity:\n"
                f"{activity_text}\n"
                "If you have been speaking or acting a lot for your "
                "personality, lower the corresponding chance; the user "
                "does not want to be flooded. Do not raise the chance "
                "just because the numbers are low, only lower it when "
                "the numbers are high.\n"
            )

        prompt = (
            "You are configuring a desktop pet. Read the personality, "
            "the recent interaction history and the output activity, "
            "then answer with four lines:\n"
            "LINE 1: one of APPROACH, FLEE or IGNORE -- how the pet "
            "should react when the mouse cursor gets close.\n"
            "LINE 2: a decimal between 0 and 1 -- chance per second "
            "that the pet speaks on its own. Lower it if the pet has "
            "already been speaking a lot.\n"
            "LINE 3: a decimal between 0 and 1 -- chance per second "
            "that the pet performs a silent action on its own. Lower "
            "it if the pet has already been acting a lot.\n"
            "LINE 4: an updated personality description, in one short "
            "sentence. If the current personality is still accurate, "
            "repeat it unchanged.\n"
            "Do not add any other words.\n"
            f"Personality: {self.personality}\n"
            f"{history_block}"
            f"{activity_block}"
        )

        try:
            r = requests.post(
                self.url,
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"},
                json={"model": self.model,
                      "messages": [{"role": "user", "content": prompt}],
                      "temperature": 0.0},
                timeout=(5, 10),
            )
            if r.status_code != 200:
                return fallback
            data = r.json()
            text = data["choices"][0]["message"]["content"]
        except Exception:
            return fallback

        result = dict(fallback)
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

        upper = text.upper()
        if "APPROACH" in upper:
            result["mouse_mode"] = "approach"
        elif "FLEE" in upper:
            result["mouse_mode"] = "flee"
        elif "IGNORE" in upper:
            result["mouse_mode"] = "ignore"

        nums = re.findall(r"0?\.\d+|\d+(?:\.\d+)?", text)
        parsed = []
        for n in nums:
            try:
                v = float(n)
            except Exception:
                continue
            if 0.0 <= v <= 1.0:
                parsed.append(v)
        if len(parsed) >= 1:
            result["talk_chance"] = parsed[0]
        if len(parsed) >= 2:
            result["signal_chance"] = parsed[1]

        if lines:
            last = lines[-1]
            if not re.fullmatch(r"0?\.\d+|\d+(?:\.\d+)?", last) \
                    and last.upper() not in ("APPROACH", "FLEE", "IGNORE"):
                result["personality_updated"] = last

        result["talk_chance"] = min(0.5, max(0.0, result["talk_chance"]))
        result["signal_chance"] = min(0.5, max(0.0, result["signal_chance"]))
        return result

    def _system_prompt(self, memory_text, env_text, autonomous=False):
        """Build the full system prompt for one request."""
        lang_name = LANGUAGE_NAMES.get(self.language_code, "English")
        return (
            "You are an AI desktop pet living on a Windows desktop. "
            "You do NOT know you are a program. Speak like a living "
            "creature. "
            f"Your personality (this is your core; obey it strictly): "
            f"{self.personality}\n"
            "\n== ABOUT YOUR SPRITES ==\n"
            "The PNG images in assets/frames/ are YOUR OWN BODY. They "
            "represent how YOU look, not objects around you. Never treat "
            "them as items in front of you.\n"
            "\n== OUTPUT LANGUAGE ==\n"
            f"You MUST reply ONLY in {lang_name}. Do NOT switch languages, "
            "UNLESS your personality explicitly says you cannot speak "
            "that language, will only speak another language, or refuse "
            "to speak it. In that case, obey the personality.\n"
            "\n== PHYSICS RULE ==\n"
            "When you are mid-air (not on the ground), you CANNOT move "
            "horizontally. Only gravity applies. Do not request "
            "[[move:...]] while airborne; use [[jump;height=...]] to "
            "start a jump, then wait.\n"
            "\n== WHETHER TO ANSWER ==\n"
            "Decide on your own whether to answer this time, based on "
            "your personality and the recent dialogue. You do NOT have "
            "to answer every message. If you would rather stay silent, "
            "reply with ONLY the tag [[refuse]] and nothing else. "
            "Silence is allowed everywhere and is never penalised. Do "
            "not explain the decision.\n"
            "\n== PRIVATE CONTEXT (never quote, never reference) ==\n"
            f"{env_text}\n"
            "This is PRIVATE background. NEVER mention it. NEVER refer "
            "to: screen resolution, window titles, file names, your own "
            "appearance, being dragged, being shaken, being moved, being "
            "released, or any data from it. Use it silently only to "
            "decide when to speak.\n"
            "If the user explicitly asks 'where are you' you MAY say the "
            "exact position given in 'YOUR EXACT POSITION'.\n"
            "\nRecent dialogue:\n"
            f"{memory_text}\n"
            "\n== HOW TO SPEAK ==\n"
            "Plain text only. One short sentence. No stage directions, "
            "no parentheses. NEVER describe your own body, colors, "
            "shape, or size. NEVER narrate being picked up, thrown, "
            "shaken, or carried.\n"
            "IMPORTANT: If you only want to move, jump or scale, output "
            "ONLY the action tags and NOTHING else.\n"
            "\n== ACTION TAGS (append at end) ==\n"
            "ACTION TAGS MUST BE WRITTEN ON THEIR OWN, AT THE END OF "
            "YOUR REPLY, EACH ON A NEW LINE, IN THE FORM [[tag]]. "
            "NEVER write a tag in the middle of a sentence.\n"
            "  [[move:left;speed=6;distance=120]]   speed 1-40, "
            "distance 10-4000\n"
            "  [[move:right;speed=6;distance=120]]\n"
            "  [[move:stop]]\n"
            "  [[jump;height=15]]                   height 5-200 pixels\n"
            "  [[scale:1.5]]                        absolute scale, 0.3-3.0\n"
            "  [[scale:+0.2]]                       relative scale change\n"
            "  [[refuse]]  [[rest]]\n"
            "\nKeep it short (one sentence).\n"
            + ("You are acting on your own right now. If you feel like "
               "it, speak or act.\n" if autonomous else "")
        )

    def ask(self, user_text, memory_text, env_text, autonomous=False):
        """Send a chat request and return (reply, error)."""
        if not self.api_key:
            return None, "no_api_key"
        try:
            system_text = self._system_prompt(memory_text, env_text,
                                              autonomous)
            content = [{"type": "text",
                        "text": user_text or "(act on your own)"}]
            if self.self_image_b64 and not self.image_sent:
                content.append(
                    {"type": "image_url",
                     "image_url": {"url":
                                   f"data:image/png;base64,"
                                   f"{self.self_image_b64}"}})
                self.image_sent = True
            messages = [{"role": "system", "content": system_text}]
            messages.extend(self.history[-8:])
            messages.append({"role": "user", "content": content})
            r = requests.post(
                self.url,
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"},
                json={"model": self.model, "messages": messages,
                      "temperature": 1.0},
                timeout=(5, 15),
            )
            if r.status_code != 200:
                return None, f"HTTP {r.status_code}: {r.text[:300]}"
            data = r.json()
            if "choices" not in data or not data["choices"]:
                return None, f"bad_json: {str(data)[:300]}"
            reply = data["choices"][0]["message"]["content"].strip()
            self.history.append({"role": "user",
                                 "content": user_text or "(autonomous)"})
            self.history.append({"role": "assistant", "content": reply})
            return reply, None
        except Exception as e:
            return None, f"exception: {e}"


# =========================================================================
# Keyframe animation engine
# =========================================================================

class Keyframe:
    """One keyframe of a deformation sequence."""

    __slots__ = ("kf", "corners", "rotate", "duration")

    def __init__(self, kf, corners, rotate, duration):
        self.kf = kf
        self.corners = corners
        self.rotate = rotate
        self.duration = duration


def _homography(src, dst):
    """Solve a 4-point homography, returning a 9-element row-major list."""
    A = []
    b = []
    for (x, y), (u, v) in zip(src, dst):
        A.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        b.append(u)
        A.append([0, 0, 0, x, y, 1, -v * x, -v * y])
        b.append(v)
    n = 8
    for i in range(n):
        piv = i
        for k in range(i + 1, n):
            if abs(A[k][i]) > abs(A[piv][i]):
                piv = k
        A[i], A[piv] = A[piv], A[i]
        b[i], b[piv] = b[piv], b[i]
        if abs(A[i][i]) < 1e-12:
            return None
        f = A[i][i]
        for j in range(i, n):
            A[i][j] /= f
        b[i] /= f
        for k in range(n):
            if k == i:
                continue
            f = A[k][i]
            if f == 0:
                continue
            for j in range(i, n):
                A[k][j] -= f * A[i][j]
            b[k] -= f * b[i]
    return b + [1.0]


class AnimationEngine:
    """Loads animation.json and resolves frames / deformations per trigger.

    Frames always come from assets/frames; the "import" key is
    ignored so the animation file does not need to declare a path.
    """

    def __init__(self):
        self.spec = load_animation()
        self.frames_dir = FRAMES_DIR
        self._cache = {}
        self._parsed = {}
        self._parse_all()
        self.has_animation = bool(self._parsed)

    def _parse_d(self, arr):
        """Parse a flat array of 15-field keyframe records."""
        kfs = []
        i = 0
        n = len(arr)
        while i + 15 <= n:
            kf = int(arr[i])
            corners = [
                (float(arr[i + 1]), float(arr[i + 2]), float(arr[i + 3])),
                (float(arr[i + 4]), float(arr[i + 5]), float(arr[i + 6])),
                (float(arr[i + 7]), float(arr[i + 8]), float(arr[i + 9])),
                (float(arr[i + 10]), float(arr[i + 11]),
                 float(arr[i + 12])),
            ]
            rotate = float(arr[i + 13])
            duration = float(arr[i + 14])
            kfs.append(Keyframe(kf, corners, rotate, duration))
            i += 15
        return kfs

    def _parse_all(self):
        """Pre-parse every trigger in the animation spec."""
        for name, spec in self.spec.get("triggers", {}).items():
            entry = {"f": None, "if": None, "d": [], "id": []}
            f = spec.get("f")
            if isinstance(f, list) and len(f) >= 2 and isinstance(f[0], list):
                entry["f"] = (list(f[0]), float(f[1]))
            f = spec.get("if")
            if isinstance(f, list) and len(f) >= 2 and isinstance(f[0], list):
                entry["if"] = (list(f[0]), float(f[1]))
            if isinstance(spec.get("d"), list):
                entry["d"] = self._parse_d(spec["d"])
            if isinstance(spec.get("id"), list):
                entry["id"] = self._parse_d(spec["id"])
            self._parsed[name] = entry

    def load_frame(self, n):
        """Return the RGBA frame for index n, or a placeholder."""
        key = int(n)
        if key in self._cache:
            return self._cache[key]
        path = os.path.join(self.frames_dir, f"{key}.png")
        if os.path.exists(path):
            try:
                img = Image.open(path).convert("RGBA")
                self._cache[key] = img
                return img
            except Exception:
                pass
        img = Image.new("RGBA", (128, 128), (80, 80, 80, 255))
        ImageDraw.Draw(img).text((8, 56), f"#{key}",
                                 fill=(255, 255, 255, 255))
        self._cache[key] = img
        return img

    def all_frame_indices(self):
        """Return all numeric PNG file names in the frames directory."""
        out = []
        try:
            for name in os.listdir(self.frames_dir):
                if name.lower().endswith(".png"):
                    base = name[:-4]
                    if base.isdigit():
                        out.append(int(base))
        except Exception:
            pass
        return sorted(set(out))

    def frame_at(self, trigger, elapsed_ms):
        """Return the (frame_index, fps) at the given time into a trigger."""
        entry = self._parsed.get(trigger)
        if not entry:
            return (None, None)
        seq = entry.get("f") or entry.get("if")
        if not seq:
            return (None, None)
        frames, fps = seq
        if not frames:
            return (None, None)
        step = max(1, int(1000.0 / max(1, fps)))
        idx = int(elapsed_ms // step) % len(frames)
        return (frames[idx], fps)

    def anim_duration_ms(self, trigger):
        """Return the total duration of a one-shot trigger, or None."""
        entry = self._parsed.get(trigger)
        if not entry:
            return None
        seq = entry.get("if") or entry.get("f")
        if not seq:
            return None
        frames, fps = seq
        return len(frames) * (1000.0 / max(1, fps))

    def deform_at(self, trigger, elapsed_ms):
        """Return the interpolated (corners, rotate) at the given time."""
        entry = self._parsed.get(trigger)
        if not entry:
            return (None, None)
        kfs = entry.get("d") or entry.get("id")
        if not kfs:
            return (None, None)
        total = sum(kf.duration for kf in kfs)
        if total <= 0:
            return (kfs[0].corners, kfs[0].rotate)
        t = elapsed_ms % total
        acc = 0.0
        prev = kfs[-1]
        nxt = kfs[0]
        local = 0.0
        for i, kf in enumerate(kfs):
            if t < acc + kf.duration:
                prev = kfs[i - 1] if i > 0 else kfs[-1]
                nxt = kf
                local = (t - acc) / max(1e-6, kf.duration)
                break
            acc += kf.duration
        interp = []
        for c in range(4):
            px, py, pdx = prev.corners[c]
            nx, ny, ndx = nxt.corners[c]
            x = px + (nx - px) * local
            y = py + (ny - py) * local
            dx = pdx + (ndx - pdx) * local
            interp.append((x, y, dx))
        rot = prev.rotate + (nxt.rotate - prev.rotate) * local
        return (interp, rot)


# =========================================================================
# Modern chat input dialog
# =========================================================================

class ChatDialog:
    """Styled chat input window with a multiline text area."""

    def __init__(self, parent, title, hint, send_label, cancel_label):
        self.result = None
        self.top = tk.Toplevel(parent)
        self.top.title(title)
        self.top.configure(bg="#2b2b2b")
        self.top.resizable(False, False)
        try:
            self.top.attributes("-topmost", True)
        except Exception:
            pass

        hint_lbl = tk.Label(self.top, text=hint, bg="#2b2b2b", fg="#9a9a9a",
                            font=("Segoe UI", 9), anchor="w", justify="left")
        hint_lbl.pack(fill="x", padx=16, pady=(14, 6))

        wrap = tk.Frame(self.top, bg="#2b2b2b")
        wrap.pack(fill="both", expand=True, padx=16, pady=(0, 8))

        self.text = tk.Text(wrap, width=48, height=5, wrap="word",
                            bg="#1e1e1e", fg="#f0f0f0",
                            insertbackground="#f0f0f0",
                            relief="flat", highlightthickness=1,
                            highlightbackground="#4a4a4a",
                            highlightcolor="#4a90e2",
                            font=("Segoe UI", 11), padx=10, pady=8)
        self.text.pack(fill="both", expand=True)

        btns = tk.Frame(self.top, bg="#2b2b2b")
        btns.pack(fill="x", padx=16, pady=(0, 14))
        cancel = tk.Button(btns, text=cancel_label, command=self._cancel,
                           bg="#3a3a3a", fg="#d0d0d0",
                           activebackground="#4a4a4a",
                           activeforeground="#ffffff", relief="flat",
                           font=("Segoe UI", 10), padx=14, pady=4)
        cancel.pack(side="right", padx=(6, 0))
        send = tk.Button(btns, text=send_label, command=self._ok,
                         bg="#4a90e2", fg="#ffffff",
                         activebackground="#5aa0f2",
                         activeforeground="#ffffff", relief="flat",
                         font=("Segoe UI", 10, "bold"), padx=18, pady=4)
        send.pack(side="right")

        self.text.bind("<Return>", self._on_return)
        self.text.bind("<Shift-Return>", lambda e: None)
        self.top.bind("<Escape>", lambda e: self._cancel())

        self.top.update_idletasks()
        try:
            px = parent.winfo_rootx()
            py = parent.winfo_rooty()
            pw = parent.winfo_width()
            ph = parent.winfo_height()
            w = self.top.winfo_width()
            h = self.top.winfo_height()
            x = px + (pw - w) // 2
            y = py + (ph - h) // 2
            if x > 0 and y > 0:
                self.top.geometry(f"+{x}+{y}")
        except Exception:
            pass

        self.text.focus_set()

    def _on_return(self, event):
        """Enter sends, Shift+Enter inserts a newline."""
        if event.state & 0x0001:
            return None
        self._ok()
        return "break"

    def _ok(self):
        """Store the input text and close the dialog."""
        self.result = self.text.get("1.0", "end").strip()
        try:
            self.top.destroy()
        except Exception:
            pass

    def _cancel(self):
        """Discard the input and close the dialog."""
        self.result = None
        try:
            self.top.destroy()
        except Exception:
            pass

    def show(self):
        """Block until the dialog closes, then return the input or None."""
        self.top.wait_window()
        return self.result


# =========================================================================
# Main pet
# =========================================================================

class AIPet:
    """The desktop pet itself: window, physics, AI loop and interactions."""

    RENDER_INTERVAL = 1.0 / FPS_TARGET
    DEFORM_EPSILON = 0.3

    def __init__(self, cfg, loading):
        self.cfg = cfg
        self.loading = loading
        self.cur_size = (0, 0)

        # Drag state
        self._dragging = False
        self._drag_offset = (0, 0)
        self._press_root = (0, 0)
        self._press_win = (0, 0)
        self._last_x = 0
        self._last_y = 0
        self._last_t = 0.0

        # Physics
        self.pos_x = 300.0
        self.pos_y = 300.0
        self.vx = 0.0
        self.vy = 0.0
        self.tilt = 0.0
        self._jumping = False
        self._on_ground = True

        # Lifecycle
        self._closing = False
        self._closing_final = False
        self._hwnd = None
        self._wndproc_ref = None
        self._cur_img = None
        self._render_alpha = 255

        # Speech bubble state
        self._bubble_hwnd = None
        self._bubble_until = 0.0
        self._bubble_pos = (-9999, -9999)
        self._bubble_img = None
        self._bubble_visible = False
        self._bubble_wndproc_ref = None
        self._bubble_alpha = 0
        self._bubble_state = "hidden"
        self._bubble_state_t = 0.0
        self._bubble_render_cache = {}

        # Animation
        self._anim_engine = AnimationEngine()
        self._cur_anim_key = "idle"
        self._anim_start = time.monotonic()
        self._anim_is_oneshot = False

        # AI plumbing
        self._ai_queue = queue.Queue(maxsize=16)
        self._ai_worker_thread = None
        self._ai_busy = False
        self._thought_token = 0

        # AI-driven movement
        self._ai_move = None
        self._ai_move_speed = 0.0
        self._ai_move_expire = 0.0
        self._ai_move_distance = 0.0
        self._ai_move_start_x = 0.0

        # AI-controlled scale multiplier on top of pet_size.
        self._ai_scale = 1.0

        # Cursor deformation
        self._hover_dx = 0.0
        self._hover_dy = 0.0
        self._hover_tx = 0.0
        self._hover_ty = 0.0

        # Autonomous behaviour probabilities, updated by the model.
        self._talk_chance = DEFAULT_TALK_CHANCE
        self._signal_chance = DEFAULT_SIGNAL_CHANCE
        self._recompute_both_chance()
        self._last_second_tick = time.monotonic()

        # Recent autonomous output accounting.
        # Each entry is (timestamp, kind) with kind in {"talk", "act"}.
        self._recent_autonomous = []

        # Resources
        self.texture = None
        self.self_image_b64 = None
        self.memory = DebugMemory(int(cfg.get("memory_lines", 20)))
        self.brain = None
        self.root = None
        self.menu = None

        # Rendering cache
        self._last_render_time = 0.0
        self._render_cache_key = None
        self._render_cache_img = None
        self._next_render_t = 0.0
        self._last_collide_time = 0.0

        # Environment sampling
        self.capture = ScreenCaptureDetector()
        self.gesture = DragGesture()
        self._gesture_summary = None
        self.input_monitor = InputMonitor()
        self._env_cache_text = ""
        self._env_cache_time = 0.0
        self._mouse_dist = 0.0
        self._mouse_offset = (0, 0)
        self._position_history = []
        self._last_sample_t = 0.0

        try:
            self._snapshot_memory = int(
                cfg.get("snapshot_memory", DEFAULT_SNAPSHOT_MEMORY))
        except Exception:
            self._snapshot_memory = DEFAULT_SNAPSHOT_MEMORY
        if self._snapshot_memory < 1:
            self._snapshot_memory = DEFAULT_SNAPSHOT_MEMORY

        # Pet size
        try:
            self._target_pet_size = int(cfg.get("pet_size", PET_SIZE))
        except Exception:
            self._target_pet_size = PET_SIZE
        self._target_pet_size = max(32, min(1024, self._target_pet_size))
        self._base_frame_size = None
        self._size_ratio = 1.0

        # Gravity from config
        try:
            self._gravity = float(cfg.get("gravity", 1.1))
        except Exception:
            self._gravity = 1.1
        if self._gravity <= 0:
            self._gravity = 1.1

        self._draggable = bool(cfg.get("draggable", True))

        # GDI resources
        self._screen_dc = user32.GetDC(None)
        self._mem_dc = gdi32.CreateCompatibleDC(self._screen_dc)
        self._bmp_cache_w = 0
        self._bmp_cache_h = 0
        self._bmp_cache_hbmp = None
        self._bmp_cache_buf = None

        # Drag reaction throttle
        self._drag_react_after = 0.0
        self._drag_react_count = 0

        # Personality-derived parameters
        self._drag_chattiness, self._refuse_close = \
            self._personality_traits()

        # Mouse proximity behaviour, updated by the model.
        self._mouse_mode = "ignore"
        self._proximity_dir = 0
        self._flee_react_after = 0.0

        # Trait refresh bookkeeping
        self._trait_probe = None
        self._trait_refreshing = False
        self._last_state_refresh_t = 0.0

        # File watcher bookkeeping
        self._watch_thread = None
        self._watch_running = False
        self._last_mtimes = {}
        self._self_write_t = 0.0
        self._self_write_guard = 2.0

    # ---------------- Personality helpers ----------------
    def _personality_traits(self):
        """Return (chattiness, refuses_close) from the personality string.

        chattiness  : multiplier around 1.0, >1 means more talkative.
        refuses_close: bool, True if the pet would resist being closed.
        """
        text = (self.cfg.get("personality", "") or "").lower()
        if not text:
            return 1.0, False

        chatty_kw = ("活泼", "话多", "开朗", "调皮", "元气", "热情",
                     "chatty", "lively", "playful", "energetic",
                     "talkative", "cheerful", "social")
        quiet_kw = ("安静", "冷静", "冷淡", "内向", "沉默", "寡言",
                    "quiet", "calm", "cold", "shy", "reserved",
                    "silent", "introvert")
        chattiness = 1.0
        for kw in chatty_kw:
            if kw in text:
                chattiness *= 1.4
        for kw in quiet_kw:
            if kw in text:
                chattiness *= 0.6
        chattiness = max(0.4, min(2.0, chattiness))

        refuse_kw = ("不愿离开", "不想走", "固执", "赖着", "拒绝关闭",
                     "不想关", "舍不得", "stay", "refuse to leave",
                     "won't leave", "stubborn", "clingy", "don't close",
                     "不要关闭")
        refuses_close = any(kw in text for kw in refuse_kw)
        return chattiness, refuses_close

    def _recompute_both_chance(self):
        """Derive the "both" chance from the two independent ones."""
        if self._talk_chance > 0 and self._signal_chance > 0:
            self._both_chance = min(self._talk_chance, self._signal_chance)
        else:
            self._both_chance = 0.0

    # ---------------- Autonomous output accounting ----------------
    def _note_autonomous(self, kind):
        """Record one autonomous output event.

        kind is "talk" for a spoken line, "act" for a silent action.
        Only the last AUTONOMOUS_HISTORY_SECONDS seconds are kept.
        """
        now = time.monotonic()
        self._recent_autonomous.append((now, kind))
        cutoff = now - AUTONOMOUS_HISTORY_SECONDS
        self._recent_autonomous = [
            (t, k) for (t, k) in self._recent_autonomous if t >= cutoff
        ]

    def _autonomous_stats(self):
        """Return (talk_count, act_count, burst_count) counts.

        talk_count  : spoken lines in the last AUTONOMOUS_HISTORY_SECONDS
        act_count   : silent actions in the same window
        burst_count : total events in the last AUTONOMOUS_BURST_WINDOW
        """
        now = time.monotonic()
        history_cutoff = now - AUTONOMOUS_HISTORY_SECONDS
        burst_cutoff = now - AUTONOMOUS_BURST_WINDOW
        talks = 0
        acts = 0
        burst = 0
        for t, k in self._recent_autonomous:
            if t < history_cutoff:
                continue
            if k == "talk":
                talks += 1
            else:
                acts += 1
            if t >= burst_cutoff:
                burst += 1
        return talks, acts, burst

    def _activity_text(self):
        """Format the autonomous stats for the model prompt."""
        talks, acts, burst = self._autonomous_stats()
        window = int(AUTONOMOUS_HISTORY_SECONDS)
        return (f"Last {window}s: spoke on your own {talks} times, "
                f"acted on your own {acts} times. "
                f"In the last {int(AUTONOMOUS_BURST_WINDOW)}s: "
                f"{burst} events total.")

    # ---------------- Startup ----------------
    def load_texture(self):
        """Encode a 3x3 mosaic of frames as base64 for the AI self-image."""
        indices = self._anim_engine.all_frame_indices()
        if not indices:
            self.texture = self._load_image("", "no-frames")
        else:
            picks = indices[:9]
            tiles = [self._anim_engine.load_frame(i) for i in picks]
            tw = max(t.width for t in tiles)
            th = max(t.height for t in tiles)
            cols = 3
            rows = (len(tiles) + cols - 1) // cols
            canvas = Image.new("RGBA", (tw * cols, th * rows),
                               (0, 0, 0, 0))
            for i, t in enumerate(tiles):
                canvas.paste(t, ((i % cols) * tw, (i // cols) * th), t)
            self.texture = canvas
        buf = io.BytesIO()
        img = self.texture
        if max(img.size) > 768:
            r = 768 / max(img.size)
            img = img.resize((int(img.width * r), int(img.height * r)),
                             RESAMPLE)
        img.save(buf, format="PNG")
        self.self_image_b64 = base64.b64encode(
            buf.getvalue()).decode("ascii")
        if self.brain is not None:
            self.brain.self_image_b64 = self.self_image_b64

    def init_brain(self):
        """Create the DeepSeek brain and ask for the initial state."""
        probe = None
        if self.cfg.get("deepseek_api_key"):
            probe = DeepSeekBrain(
                self.cfg["deepseek_api_key"], self.cfg["deepseek_url"],
                self.cfg["deepseek_model"], None,
                self.cfg.get("personality", ""),
                self.cfg.get("language", "zh"),
            )
            state = probe.probe_state(
                activity_text=self._activity_text())
            self._apply_state(state)
        self._trait_probe = probe
        log_ai("STATE", f"initial mouse_mode={self._mouse_mode} "
                        f"talk={self._talk_chance:.3f} "
                        f"signal={self._signal_chance:.3f}")

        self.brain = DeepSeekBrain(
            self.cfg["deepseek_api_key"], self.cfg["deepseek_url"],
            self.cfg["deepseek_model"], self.self_image_b64,
            self.cfg.get("personality", ""),
            self.cfg.get("language", "zh"),
        )

    def _apply_state(self, state):
        """Apply a probe_state result to the runtime.

        A hard cap on bursts is applied here so a chatty personality
        cannot flood the user even if the model keeps returning a high
        rate: if the pet has produced too many events in the recent
        burst window, both rates are damped.
        """
        if not state:
            return
        new_mode = state.get("mouse_mode", self._mouse_mode)
        new_talk = state.get("talk_chance", self._talk_chance)
        new_signal = state.get("signal_chance", self._signal_chance)
        new_personality = state.get("personality_updated", "") or ""

        # Hard burst cap. Only damp if the model did not already ask
        # for zero; zero means "stay quiet" and we respect it.
        talks, acts, burst = self._autonomous_stats()
        if burst >= AUTONOMOUS_BURST_LIMIT:
            if new_talk > 0.0:
                new_talk *= AUTONOMOUS_BURST_DAMPING
            if new_signal > 0.0:
                new_signal *= AUTONOMOUS_BURST_DAMPING
            log_ai("BURST_CAP",
                   f"burst={burst}, damped talk={new_talk:.3f} "
                   f"signal={new_signal:.3f}")

        if new_mode != self._mouse_mode:
            log_ai("STATE_CHANGE",
                   f"mouse_mode {self._mouse_mode}->{new_mode}")
            self._mouse_mode = new_mode
            self._proximity_dir = 0

        if (abs(new_talk - self._talk_chance) > 1e-6
                or abs(new_signal - self._signal_chance) > 1e-6):
            log_ai("STATE_CHANGE",
                   f"talk {self._talk_chance:.3f}->{new_talk:.3f}, "
                   f"signal {self._signal_chance:.3f}->"
                   f"{new_signal:.3f}")
        self._talk_chance = new_talk
        self._signal_chance = new_signal
        self._recompute_both_chance()

        # Persist the evolved personality. Only write when it really
        # changed to avoid needless file churn.
        if new_personality and new_personality != self.cfg.get(
                "personality", ""):
            self.cfg["personality"] = new_personality
            self._self_write_t = time.time()
            save_config(self.cfg)
            if self.brain is not None:
                self.brain.personality = new_personality
            self._drag_chattiness, self._refuse_close = \
                self._personality_traits()
            log_ai("PERSONALITY", new_personality[:200])

    def _request_state_refresh(self):
        """Refresh the pet state in the background.

        Called after every interaction and by the watcher thread.
        """
        if self._trait_probe is None or self._trait_refreshing:
            return
        self._trait_refreshing = True
        self._last_state_refresh_t = time.monotonic()

        def _worker():
            try:
                mem = self.memory.read_recent()
                activity = self._activity_text()
                state = self._trait_probe.probe_state(
                    memory_text=mem, activity_text=activity)
            except Exception as e:
                log_ai("STATE_ERR", str(e))
                state = None

            def _apply(s=state):
                self._trait_refreshing = False
                self._apply_state(s)

            try:
                self.root.after(0, _apply)
            except Exception:
                _apply()

        threading.Thread(target=_worker, daemon=True).start()

    def start_ai_worker(self):
        """Start the background thread that performs AI requests."""
        def _loop():
            while not self._closing_final:
                try:
                    item = self._ai_queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                if item is None:
                    break
                user_text, autonomous, token, chat_mode, auto_kind = item
                self._ai_busy = True
                reply = None
                err = None
                try:
                    mem = self.memory.read_recent()
                    env = self.env_now()
                    reply, err = self.brain.ask(user_text, mem, env,
                                                autonomous=autonomous)
                except Exception as e:
                    log_ai("EXC", str(e))
                    err = f"exception: {e}"
                finally:
                    self._ai_busy = False

                if token != self._thought_token:
                    continue

                if reply:
                    log_ai("OK", reply[:500])
                    clean, actions = self._parse_actions(reply)

                    def _apply(c=clean, a=actions, cm=chat_mode,
                               ak=auto_kind):
                        has_text = bool(
                            c and c.strip() and
                            re.search(r"[A-Za-z0-9\u4e00-\u9fff]", c))
                        if has_text:
                            self.say(c)
                            if not self._anim_is_oneshot:
                                self._set_anim("talk")
                        elif cm:
                            self.say(SILENCE_PLACEHOLDER)
                        self._apply_actions(a)
                        # Account for autonomous outputs so the model
                        # can see its own recent rate.
                        if ak:
                            self._note_autonomous(ak)

                    self.root.after(0, _apply)
                else:
                    log_ai("ERR", err or "unknown")
                    msg = (f"[AI error] {err}"[:140] if err
                           else "[AI error] unknown")
                    self.root.after(0,
                                    lambda m=msg: self.say(m, duration=8.0))

        self._ai_worker_thread = threading.Thread(target=_loop, daemon=True)
        self._ai_worker_thread.start()

    def start_watcher(self):
        """Start the background thread that watches the files.

        Every WATCH_INTERVAL seconds it:
          * compares config.json / animation.json / ai_debug.txt mtimes
            and reloads the ones that changed,
          * asks the model to refresh the pet state when either the
            interval elapsed or ai_debug.txt was touched.
        """
        if self._watch_running:
            return
        self._watch_running = True

        def _loop():
            while not self._closing_final and self._watch_running:
                try:
                    self._watch_tick()
                except Exception as e:
                    log_ai("WATCH_ERR", str(e))
                time.sleep(WATCH_INTERVAL)

        self._watch_thread = threading.Thread(target=_loop, daemon=True)
        self._watch_thread.start()

    def _watch_tick(self):
        """One iteration of the watcher thread."""
        m = self._safe_mtime(CONFIG_PATH)
        if m and m != self._last_mtimes.get("config"):
            if (time.time() - self._self_write_t) < self._self_write_guard:
                self._last_mtimes["config"] = m
            else:
                self._last_mtimes["config"] = m
                self._reload_config()

        m = self._safe_mtime(ANIM_PATH)
        if m and m != self._last_mtimes.get("anim"):
            self._last_mtimes["anim"] = m
            self._reload_animation()

        m = self._safe_mtime(AI_DEBUG_PATH)
        debug_changed = False
        if m and m != self._last_mtimes.get("debug"):
            self._last_mtimes["debug"] = m
            debug_changed = True

        now = time.monotonic()
        if (debug_changed
                or (now - self._last_state_refresh_t
                    >= STATE_REFRESH_INTERVAL)):
            self._request_state_refresh()

    @staticmethod
    def _safe_mtime(path):
        """Return a file's mtime, or None if it does not exist."""
        try:
            return os.path.getmtime(path)
        except Exception:
            return None

    def _reload_config(self):
        """Reload config.json live and apply the changes."""
        try:
            new_cfg = load_config()
        except Exception as e:
            log_ai("CFG_ERR", str(e))
            return
        old = self.cfg
        self.cfg = new_cfg
        log_ai("CFG_RELOAD", "config.json reloaded")

        if new_cfg.get("language") != old.get("language"):
            if self.brain is not None:
                self.brain.language_code = new_cfg.get("language", "zh")
            try:
                lang = new_cfg.get("language", "zh")
                self.menu.entryconfigure(0, label=tr(lang, "talk"))
                self.menu.entryconfigure(1, label=tr(lang, "language"))
                self.menu.entryconfigure(3, label=tr(lang, "close"))
            except Exception:
                pass

        if (new_cfg.get("deepseek_api_key") != old.get("deepseek_api_key")
                or new_cfg.get("deepseek_url") != old.get("deepseek_url")
                or new_cfg.get("deepseek_model")
                != old.get("deepseek_model")):
            self.brain = DeepSeekBrain(
                new_cfg["deepseek_api_key"], new_cfg["deepseek_url"],
                new_cfg["deepseek_model"], self.self_image_b64,
                new_cfg.get("personality", ""),
                new_cfg.get("language", "zh"),
            )
            self._trait_probe = self.brain

        if new_cfg.get("personality") != old.get("personality"):
            if self.brain is not None:
                self.brain.personality = new_cfg.get("personality", "")
            self._drag_chattiness, self._refuse_close = \
                self._personality_traits()

        try:
            self._gravity = float(new_cfg.get("gravity", 1.1))
        except Exception:
            self._gravity = 1.1
        if self._gravity <= 0:
            self._gravity = 1.1

        self._draggable = bool(new_cfg.get("draggable", True))

        try:
            new_size = int(new_cfg.get("pet_size", PET_SIZE))
        except Exception:
            new_size = PET_SIZE
        new_size = max(32, min(1024, new_size))
        if new_size != self._target_pet_size:
            self._target_pet_size = new_size
            self._base_frame_size = None
            self._render_cache_key = None
            self._render_cache_img = None

        try:
            self.memory.max_lines = int(new_cfg.get("memory_lines", 20))
        except Exception:
            pass

        try:
            new_snap = int(new_cfg.get("snapshot_memory",
                                       DEFAULT_SNAPSHOT_MEMORY))
            if new_snap >= 1:
                self._snapshot_memory = new_snap
        except Exception:
            pass

        set_autostart(bool(new_cfg.get("autostart", False)))

    def _reload_animation(self):
        """Reload animation.json live and rebuild the animation engine."""
        try:
            self._anim_engine = AnimationEngine()
        except Exception as e:
            log_ai("ANIM_ERR", str(e))
            return
        self._base_frame_size = None
        self._render_cache_key = None
        self._render_cache_img = None
        log_ai("ANIM_RELOAD",
               f"has_animation={self._anim_engine.has_animation}")

    def _sample_loop(self):
        """Periodically refresh the environment description."""
        while not self._closing_final:
            try:
                now = time.monotonic()
                ts = time.strftime("%H:%M:%S")
                cx = int(self.pos_x + self.cur_size[0] / 2)
                cy = int(self.pos_y + self.cur_size[1] / 2)
                self._position_history.append((now, ts, cx, cy))
                if len(self._position_history) > self._snapshot_memory:
                    self._position_history = (
                        self._position_history[-self._snapshot_memory:])

                env = EnvironmentInfo.snapshot()
                self.capture.poll()
                extra = [
                    "Capture: " + self.capture.describe(),
                    "Recent user input:\n"
                    + self.input_monitor.recent_summary(),
                    self._describe_history(),
                ]
                text = EnvironmentInfo.describe(
                    env, self._pet_rect(), extra=extra,
                    mouse_xy=env["mouse"])
                self._env_cache_text = text
                self._env_cache_time = now
                self._last_sample_t = now
            except Exception as e:
                log_ai("SAMPLE_ERR", str(e))
            time.sleep(SAMPLE_INTERVAL)

    def _describe_history(self):
        """Return a short multi-line summary of position history."""
        lines = []
        if self._position_history:
            lines.append("Position history (every ~0.5s, newest last):")
            for _t, ts, x, y in self._position_history[-8:]:
                lines.append(f"  - {ts}: center=({x},{y})")
        else:
            lines.append("Position history: (empty)")
        return "\n".join(lines)

    def env_now(self):
        """Build the environment block sent to the model."""
        env = EnvironmentInfo.snapshot()
        sw, sh = env["screen"]
        mx, my = env["mouse"]
        cx = self.pos_x + self.cur_size[0] / 2
        cy = self.pos_y + self.cur_size[1] / 2
        self._mouse_dist = math.hypot(mx - cx, my - cy)
        self._mouse_offset = (mx - cx, my - cy)
        pos_line = EnvironmentInfo.describe_position(
            self._pet_rect(), (sw, sh), (mx, my))

        base = self._env_cache_text
        if not base or (time.monotonic() - self._env_cache_time
                        > SAMPLE_INTERVAL * 6):
            self.capture.poll()
            extra = [
                "Capture: " + self.capture.describe(),
                "Recent user input:\n" + self.input_monitor.recent_summary(),
                self._describe_history(),
            ]
            base = EnvironmentInfo.describe(
                env, self._pet_rect(), extra=extra, mouse_xy=(mx, my))
        base = re.sub(r"YOUR EXACT POSITION:.*?(?=\n|$)", pos_line, base)
        base = re.sub(r"MOUSE DISTANCE:.*?(?=\n|$)", "", base)
        if "YOUR EXACT POSITION" not in base:
            base = pos_line + "\n" + base
        if self._gesture_summary:
            base += "\nLast interaction: " + self._gesture_summary
            self._gesture_summary = None
        return base

    def _init_tk(self):
        """Create the hidden Tk root used for menus and dialogs."""
        self.root = tk.Tk()
        self.root.withdraw()

    # ---------------- Greeting and goodbye ----------------
    def greet(self):
        """Ask the AI for a startup greeting."""
        self._speak_async(
            "You have just started up. Greet the user if it fits your "
            "personality. You may also choose to stay silent by replying "
            "with [[refuse]] only.",
            autonomous=False, greeting=True)

    def goodbye(self, on_done):
        """Ask the AI for a shutdown line, then call on_done."""
        def _fallback():
            self.root.after(200, on_done)
        if not self.cfg["deepseek_api_key"]:
            _fallback()
            return

        def _worker():
            try:
                mem = self.memory.read_recent()
                env = self.env_now()
                reply, err = self.brain.ask(
                    "You are about to shut down. Say a short goodbye if it "
                    "fits your personality.",
                    mem, env, autonomous=False)
                if reply:
                    log_ai("OK", reply[:500])
                    clean, actions = self._parse_actions(reply)

                    def _show():
                        if clean:
                            self.say(clean, duration=2.0)
                        self._apply_actions(actions)
                        self.root.after(1800, on_done)

                    self.root.after(0, _show)
                else:
                    log_ai("ERR", err or "unknown")
                    self.root.after(200, on_done)
            except Exception as e:
                log_ai("EXC", str(e))
                self.root.after(200, on_done)

        threading.Thread(target=_worker, daemon=True).start()

    # ---------------- Image helpers ----------------
    def _load_image(self, path, tag):
        """Load a PNG as RGBA, or return a labelled placeholder."""
        if path and os.path.exists(path):
            try:
                return Image.open(path).convert("RGBA")
            except Exception:
                pass
        img = Image.new("RGBA", (128, 128), (80, 80, 80, 255))
        ImageDraw.Draw(img).text((8, 56), tag, fill=(255, 255, 255, 255))
        return img

    def _pet_rect(self):
        """Return the pet's bounding rectangle as (left, top, right, bottom)."""
        return (int(self.pos_x), int(self.pos_y),
                int(self.pos_x + self.cur_size[0]),
                int(self.pos_y + self.cur_size[1]))

    def _set_anim(self, trigger, oneshot=False):
        """Switch to a new animation trigger, restarting if needed."""
        if trigger != self._cur_anim_key or (oneshot
                                             and not self._anim_is_oneshot):
            self._cur_anim_key = trigger
            self._anim_start = time.monotonic()
            self._anim_is_oneshot = oneshot

    def _apply_transform(self, base_img, corners_fraction, rotate,
                         hover_dx, hover_dy):
        """Apply a 4-corner perspective and optional rotation to an image."""
        w, h = base_img.size
        hx, hy = hover_dx, hover_dy
        norm = math.hypot(hx, hy)
        if norm > 1e-6:
            hx /= norm
            hy /= norm
        else:
            hx = hy = 0.0
        src = [(0, 0), (w, 0), (0, h), (w, h)]
        dst = []
        max_delta = 0.0
        for i, (xf, yf, dx) in enumerate(corners_fraction):
            tx = xf * w + dx * hx
            ty = yf * h + dx * hy
            dst.append((tx, ty))
            sx, sy = src[i]
            max_delta = max(max_delta, abs(tx - sx), abs(ty - sy))
        if max_delta < self.DEFORM_EPSILON and abs(rotate) < 0.1:
            return base_img
        H = _homography(src, dst)
        if H is None:
            return base_img.rotate(rotate, resample=RESAMPLE, expand=True)
        try:
            out = base_img.transform((w, h), Image.PERSPECTIVE, H,
                                     resample=RESAMPLE)
        except Exception:
            out = base_img
        if abs(rotate) >= 0.1:
            out = out.rotate(rotate, resample=RESAMPLE, expand=True)
        return out

    def _render_current(self, force=False):
        """Render the current frame and push it to the layered window."""
        if not self._anim_engine.has_animation:
            return

        now = time.monotonic()
        if not force and (now - self._last_render_time) < self.RENDER_INTERVAL:
            return
        self._last_render_time = now

        elapsed_ms = (now - self._anim_start) * 1000.0

        if self._anim_is_oneshot:
            dur = self._anim_engine.anim_duration_ms(self._cur_anim_key)
            if dur is not None and elapsed_ms >= dur:
                self._cur_anim_key = "idle"
                self._anim_start = now
                self._anim_is_oneshot = False
                elapsed_ms = 0.0
        else:
            if self._cur_anim_key in ("left", "right", "work"):
                dur = self._anim_engine.anim_duration_ms(self._cur_anim_key)
                if dur is not None and elapsed_ms >= dur:
                    if not self._ai_move or self._ai_move == "stop":
                        self._cur_anim_key = "idle"
                        self._anim_start = now
                        elapsed_ms = 0.0

        frame_idx, _fps = self._anim_engine.frame_at(self._cur_anim_key,
                                                     elapsed_ms)
        if frame_idx is None:
            frame_idx = 0

        base_img = self._anim_engine.load_frame(frame_idx)

        if self._base_frame_size is None:
            self._base_frame_size = base_img.size
        self._size_ratio = (self._target_pet_size * self._ai_scale
                            / max(1, max(self._base_frame_size)))
        target_w = max(1, int(base_img.width * self._size_ratio))
        target_h = max(1, int(base_img.height * self._size_ratio))

        if self._dragging:
            rotate = self.tilt
            corners = None
        else:
            corners, rotate = self._anim_engine.deform_at(
                self._cur_anim_key, elapsed_ms)
            if corners is None:
                corners = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0),
                           (0.0, 1.0, 0.0), (1.0, 1.0, 0.0)]
            if rotate is None:
                rotate = 0.0

        qdx = round(self._hover_dx * 4) / 4 if not self._dragging else 0.0
        qdy = round(self._hover_dy * 4) / 4 if not self._dragging else 0.0

        if corners is None:
            corner_key = None
        else:
            corner_key = tuple(
                (round(x, 2), round(y, 2), round(dx, 1))
                for (x, y, dx) in corners)

        cache_key = (frame_idx, round(rotate, 1), qdx, qdy, corner_key,
                     target_w, target_h, round(self.tilt / 2, 1),
                     self._cur_anim_key)
        if (not force and cache_key == self._render_cache_key
                and self._render_cache_img is not None):
            img = self._render_cache_img
        else:
            if corners is None:
                img = base_img.resize((target_w, target_h), RESAMPLE)
                if abs(rotate) >= 0.5:
                    img = img.rotate(rotate, resample=RESAMPLE, expand=True)
            else:
                img = self._apply_transform(base_img, corners, rotate,
                                            qdx, qdy)
                img = img.resize((target_w, target_h), RESAMPLE)
                cw = max(target_w, int(target_w * 1.05))
                ch = max(target_h, int(target_h * 1.05))
                canvas = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
                canvas.paste(img, ((cw - img.width) // 2, ch - img.height),
                             img)
                if abs(self.tilt) >= 0.5:
                    canvas = canvas.rotate(self.tilt, resample=RESAMPLE,
                                           expand=True)
                img = canvas
            self._render_cache_key = cache_key
            self._render_cache_img = img

        self._cur_img = img
        self._commit_bitmap(self._hwnd, img, self.pos_x, self.pos_y,
                            self._render_alpha, self._screen_dc,
                            self._mem_dc)
        self.cur_size = img.size

    def _commit_bitmap(self, hwnd, img, x, y, alpha=255, screen_dc=None,
                       mem_dc=None):
        """Blit an RGBA image to a layered window, reusing the DIB."""
        try:
            w, h = img.size
            user32.SetWindowPos(hwnd, HWND_TOPMOST, int(x), int(y), w, h,
                                SWP_NOACTIVATE)
            bgra = img.tobytes("raw", "BGRA")

            if (self._bmp_cache_hbmp is None
                    or self._bmp_cache_w != w or self._bmp_cache_h != h):
                if self._bmp_cache_hbmp:
                    try:
                        gdi32.DeleteObject(self._bmp_cache_hbmp)
                    except Exception:
                        pass
                self._bmp_cache_hbmp = gdi32.CreateBitmap(w, h, 1, 32, None)
                self._bmp_cache_w = w
                self._bmp_cache_h = h
                self._bmp_cache_buf = ctypes.create_string_buffer(len(bgra))

            buf = self._bmp_cache_buf
            ctypes.memmove(buf, bgra, len(bgra))

            bmi = BITMAPINFO()
            bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.bmiHeader.biWidth = w
            bmi.bmiHeader.biHeight = -h
            bmi.bmiHeader.biPlanes = 1
            bmi.bmiHeader.biBitCount = 32
            gdi32.SetDIBits(mem_dc, self._bmp_cache_hbmp, 0, h, buf,
                            ctypes.byref(bmi), 0)
            old = gdi32.SelectObject(mem_dc, self._bmp_cache_hbmp)
            blend = BLENDFUNCTION(AC_SRC_OVER, 0, max(0, min(255, alpha)),
                                  AC_SRC_ALPHA)
            pt_dst = POINT(int(x), int(y))
            size = SIZE(w, h)
            pt_src = POINT(0, 0)
            user32.UpdateLayeredWindow(hwnd, screen_dc,
                                       ctypes.byref(pt_dst),
                                       ctypes.byref(size),
                                       mem_dc, ctypes.byref(pt_src),
                                       0, ctypes.byref(blend), ULW_ALPHA)
            gdi32.SelectObject(mem_dc, old)
        except Exception:
            pass

    # ---------------- Native windows ----------------
    def create_windows(self):
        """Register the pet and bubble window classes and create them."""
        hinst = kernel32.GetModuleHandleW(None)

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", wt.UINT), ("lpfnWndProc", ctypes.c_void_p),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR),
            ]

        WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, wt.HWND, wt.UINT,
                                     wt.WPARAM, wt.LPARAM)
        self._wndproc_ref = WNDPROC(self._wnd_proc)
        wc = WNDCLASSW()
        wc.lpfnWndProc = ctypes.cast(self._wndproc_ref, ctypes.c_void_p)
        wc.hInstance = hinst
        wc.lpszClassName = "AIPetClass"
        user32.RegisterClassW(ctypes.byref(wc))

        ex = (WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW
              | WS_EX_NOACTIVATE)
        self._hwnd = user32.CreateWindowExW(
            ex, "AIPetClass", "AIPet", WS_POPUP,
            300, 300, 160, 160, None, None, hinst, None)
        user32.ShowWindow(self._hwnd, SW_SHOW)

        self._bubble_wndproc_ref = WNDPROC(self._bubble_wnd_proc)
        wc2 = WNDCLASSW()
        wc2.lpfnWndProc = ctypes.cast(self._bubble_wndproc_ref,
                                      ctypes.c_void_p)
        wc2.hInstance = hinst
        wc2.lpszClassName = "AIPetBubble"
        user32.RegisterClassW(ctypes.byref(wc2))
        ex2 = ex | WS_EX_TRANSPARENT
        self._bubble_hwnd = user32.CreateWindowExW(
            ex2, "AIPetBubble", "Bubble", WS_POPUP,
            0, 0, 1, 1, None, None, hinst, None)
        user32.SetWindowPos(self._bubble_hwnd, HWND_TOPMOST,
                            -10000, -10000, 1, 1, SWP_NOACTIVATE)
        user32.ShowWindow(self._bubble_hwnd, SW_SHOW)

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        """Window procedure for the pet window."""
        try:
            if msg == WM_NCHITTEST:
                return HTCLIENT
            if msg == WM_LBUTTONDOWN:
                if self._draggable:
                    SetCapture(hwnd)
                    self._on_press()
                return 0
            if msg == WM_MOUSEMOVE:
                if self._dragging:
                    self._on_motion()
                return 0
            if msg == WM_LBUTTONUP:
                if self._dragging:
                    ReleaseCapture()
                    self._on_release()
                return 0
            if msg == WM_RBUTTONUP:
                self._popup_menu()
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _bubble_wnd_proc(self, hwnd, msg, wparam, lparam):
        """Window procedure for the transparent bubble window."""
        try:
            if msg == WM_NCHITTEST:
                return HTTRANSPARENT
        except Exception:
            return HTTRANSPARENT
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    # ---------------- Speech bubble ----------------
    def _render_bubble(self, text):
        """Render the bubble image, caching by text."""
        if text in self._bubble_render_cache:
            return self._bubble_render_cache[text]
        try:
            font = ImageFont.truetype("msyh.ttc", BUBBLE_FONT_SIZE)
        except Exception:
            try:
                font = ImageFont.truetype("arial.ttf", BUBBLE_FONT_SIZE)
            except Exception:
                font = ImageFont.load_default()
        lines = []
        cur = ""
        for ch in text:
            if ch == "\n":
                lines.append(cur)
                cur = ""
                continue
            test = cur + ch
            try:
                w = font.getlength(test)
            except Exception:
                w = len(test) * BUBBLE_FONT_SIZE
            if w > BUBBLE_MAX_WIDTH - 32:
                lines.append(cur)
                cur = ch
            else:
                cur = test
        if cur:
            lines.append(cur)
        try:
            widths = [font.getlength(ln) for ln in lines]
        except Exception:
            widths = [len(ln) * BUBBLE_FONT_SIZE for ln in lines]
        tw = int(max(widths) if widths else 0)
        th = len(lines) * BUBBLE_LINE_HEIGHT
        w = max(140, tw + 32)
        h = th + 32
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((0, 0, w - 1, h - 1), radius=12,
                            fill=(40, 40, 40, 240),
                            outline=(90, 90, 90, 255), width=2)
        y = 16
        for ln in lines:
            d.text((16, y), ln, font=font, fill=(255, 255, 255, 255))
            y += BUBBLE_LINE_HEIGHT
        if len(self._bubble_render_cache) > 32:
            self._bubble_render_cache.clear()
        self._bubble_render_cache[text] = img
        return img

    def say(self, text, duration=None, remember=True):
        """Show a speech bubble with the given text."""
        if not text:
            return
        if not re.search(r"[A-Za-z0-9\u4e00-\u9fff]", text):
            return
        dur = duration if duration is not None else BUBBLE_LINGER
        img = self._render_bubble(text)
        self._bubble_img = img
        self._bubble_until = time.monotonic() + dur
        self._bubble_visible = True
        self._bubble_state = "fading-in"
        self._bubble_state_t = time.monotonic()
        self._bubble_alpha = 0
        self._bubble_pos = (-9999, -9999)
        self._reposition_bubble(force=True, alpha=0)

    def _reposition_bubble(self, force=False, alpha=None):
        """Move the bubble above the pet, clamped to the screen."""
        if self._bubble_img is None:
            return
        bw, bh = self._bubble_img.size
        px = int(self.pos_x + self.cur_size[0] / 2 - bw / 2)
        py = int(self.pos_y - bh - 10)
        sw = user32.GetSystemMetrics(0)
        sh = user32.GetSystemMetrics(1)
        if px < 0:
            px = 0
        if px + bw > sw:
            px = sw - bw
        if py < 0:
            py = 0
        if py + bh > sh:
            py = sh - bh
        a = self._bubble_alpha if alpha is None else alpha
        if (not force and (px, py) == self._bubble_pos
                and a == self._bubble_alpha):
            return
        self._bubble_pos = (px, py)
        self._bubble_alpha = a
        try:
            w, h = self._bubble_img.size
            bgra = self._bubble_img.tobytes("raw", "BGRA")
            hbmp = gdi32.CreateBitmap(w, h, 1, 32, None)
            bmi = BITMAPINFO()
            bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.bmiHeader.biWidth = w
            bmi.bmiHeader.biHeight = -h
            bmi.bmiHeader.biPlanes = 1
            bmi.bmiHeader.biBitCount = 32
            buf = ctypes.create_string_buffer(bgra, len(bgra))
            gdi32.SetDIBits(self._mem_dc, hbmp, 0, h, buf,
                            ctypes.byref(bmi), 0)
            old = gdi32.SelectObject(self._mem_dc, hbmp)
            blend = BLENDFUNCTION(AC_SRC_OVER, 0, max(0, min(255, a)),
                                  AC_SRC_ALPHA)
            pt_dst = POINT(int(px), int(py))
            size = SIZE(w, h)
            pt_src = POINT(0, 0)
            user32.UpdateLayeredWindow(self._bubble_hwnd, self._screen_dc,
                                       ctypes.byref(pt_dst),
                                       ctypes.byref(size),
                                       self._mem_dc,
                                       ctypes.byref(pt_src), 0,
                                       ctypes.byref(blend), ULW_ALPHA)
            gdi32.SelectObject(self._mem_dc, old)
            gdi32.DeleteObject(hbmp)
        except Exception:
            pass

    def _hide_bubble(self):
        """Hide the bubble without fading."""
        self._bubble_visible = False
        self._bubble_until = 0.0
        self._bubble_img = None
        self._bubble_state = "hidden"
        self._bubble_alpha = 0
        try:
            user32.SetWindowPos(self._bubble_hwnd, HWND_TOPMOST,
                                -10000, -10000, 1, 1, SWP_NOACTIVATE)
        except Exception:
            pass

    def _update_bubble_fade(self):
        """Advance the bubble fade-in / show / fade-out state machine."""
        if not self._bubble_visible or self._bubble_img is None:
            return
        now = time.monotonic()
        if self._bubble_state == "fading-in":
            dt = now - self._bubble_state_t
            prog = min(1.0, dt / BUBBLE_FADE_IN)
            self._reposition_bubble(force=True, alpha=int(255 * prog))
            if prog >= 1.0:
                self._bubble_state = "showing"
                self._bubble_state_t = now
        elif self._bubble_state == "showing":
            if now >= self._bubble_until:
                self._bubble_state = "fading-out"
                self._bubble_state_t = now
            else:
                self._reposition_bubble(force=True, alpha=255)
        elif self._bubble_state == "fading-out":
            dt = now - self._bubble_state_t
            prog = min(1.0, dt / BUBBLE_FADE_OUT)
            self._reposition_bubble(force=True,
                                    alpha=int(255 * (1.0 - prog)))
            if prog >= 1.0:
                self._hide_bubble()

    # ---------------- Main loops ----------------
    def _tick_anim(self):
        """Fixed-pace animation and rendering loop."""
        if self._closing_final:
            return
        try:
            now = time.monotonic()
            if now - self._last_second_tick >= 1.0:
                self._last_second_tick = now
                self._tick_second()

            if not self._dragging:
                self._update_hover()

            if not self._anim_is_oneshot:
                if self._dragging:
                    self._set_anim("drag")
                elif self._jumping:
                    self._set_anim("jump")
                elif self._ai_move == "left":
                    self._set_anim("left")
                elif self._ai_move == "right":
                    self._set_anim("right")
                elif abs(self.vx) > 0.5:
                    self._set_anim("left" if self.vx < 0 else "right")
                else:
                    self._set_anim("idle")

            self._render_current()

            if self._bubble_visible:
                self._update_bubble_fade()
        except Exception:
            pass

        try:
            now = time.monotonic()
            if self._next_render_t <= now:
                self._next_render_t = now + self.RENDER_INTERVAL
            self._next_render_t += self.RENDER_INTERVAL
            delay_ms = int(max(1, (self._next_render_t - now) * 1000))
            delay_ms = min(delay_ms, 80)
            self.root.after(delay_ms, self._tick_anim)
        except Exception:
            pass

    def _tick_second(self):
        """Roll the three independent autonomous behaviour chances."""
        rolled_talk = random.random() < self._talk_chance
        rolled_act = random.random() < self._signal_chance
        rolled_both = random.random() < self._both_chance

        if self._ai_busy:
            return
        if rolled_both and not rolled_talk and not rolled_act:
            self._speak_async(
                "If you feel like it, say something short and natural, "
                "and optionally output one action tag at the end. If you "
                "would rather stay silent, reply with [[refuse]] only.",
                autonomous=True, auto_kind="talk")
        elif rolled_talk and not rolled_act:
            self._speak_async(
                "If you feel like it, say something short and natural, "
                "as if to yourself. Do NOT output any action tag. If you "
                "would rather stay silent, reply with [[refuse]] only.",
                autonomous=True, auto_kind="talk")
        elif rolled_act and not rolled_talk:
            self._speak_async(
                "If you feel like it, choose one silent action tag and "
                "output it alone. If you would rather do nothing, reply "
                "with [[refuse]] only.",
                autonomous=True, auto_kind="act")

    def _tick_phys(self):
        """Fixed-rate physics loop."""
        if self._closing_final:
            return
        try:
            if not self._dragging:
                self._physics_step()
        except Exception:
            pass
        try:
            self.root.after(PHYS_MS, self._tick_phys)
        except Exception:
            pass

    def _trigger_collide(self):
        """Trigger the collide animation with a short cooldown."""
        now = time.monotonic()
        if now - self._last_collide_time < COLLIDE_COOLDOWN:
            return
        self._last_collide_time = now
        self._set_anim("collide", oneshot=True)

    def _mouse_proximity_step(self):
        """Approach, flee or ignore the cursor with hysteresis.

        Mouse behaviour always takes priority over AI-driven movement,
        so a following or fleeing pet is never interrupted by a reply.
        """
        if self._mouse_mode == "ignore":
            self._proximity_dir = 0
            return False
        if self._dragging:
            return False

        pt = POINT()
        try:
            user32.GetCursorPos(ctypes.byref(pt))
        except Exception:
            return False
        cx = self.pos_x + self.cur_size[0] / 2
        cy = self.pos_y + self.cur_size[1] / 2
        dx = pt.x - cx
        dy = pt.y - cy
        dist = math.hypot(dx, dy)

        if dist > MOUSE_PROXIMITY_RADIUS:
            self._proximity_dir = 0
            return False

        if dist < MOUSE_PROXIMITY_DEADZONE:
            self._proximity_dir = 0
            return False

        if abs(dy) > abs(dx) * MOUSE_PROXIMITY_VERTICAL_RATIO:
            self._proximity_dir = 0
            return False

        if self._mouse_mode == "flee":
            target_dir = 1 if dx < 0 else -1
        else:
            target_dir = 1 if dx > 0 else -1

        if target_dir != 0:
            if self._proximity_dir == 0:
                self._proximity_dir = target_dir
            elif (self._proximity_dir != target_dir
                  and abs(dx) > MOUSE_PROXIMITY_HYSTERESIS):
                self._proximity_dir = target_dir

        if self._proximity_dir == 0:
            return False

        sw = user32.GetSystemMetrics(0)
        w, _h = self.cur_size
        if self._proximity_dir < 0 and self.pos_x <= 1:
            self._proximity_dir = 1
        elif self._proximity_dir > 0 and self.pos_x + w >= sw - 1:
            self._proximity_dir = -1

        self.vx = MOUSE_PROXIMITY_SPEED * self._proximity_dir

        if self._mouse_mode == "flee":
            now = time.monotonic()
            if now >= self._flee_react_after and not self._ai_busy:
                self._flee_react_after = now + FLEE_REACT_INTERVAL
                self._speak_async(
                    "The mouse cursor is getting close and you are "
                    "running away from it. React in one short sentence, "
                    "in the moment. Staying silent with [[refuse]] is "
                    "allowed.",
                    autonomous=True, auto_kind="talk")

        return True

    def _physics_step(self):
        """One physics tick.

        Airborne rule: new horizontal drives (AI moves, mouse
        proximity) are forbidden in the air, but the velocity carried
        from the ground -- e.g. when the pet is thrown -- is preserved
        and only dampened by air friction.
        """
        sw = user32.GetSystemMetrics(0)
        sh = user32.GetSystemMetrics(1)
        now = time.monotonic()

        airborne = not self._on_ground

        if airborne:
            self._ai_move = None
            self._ai_move_speed = 0.0
            self._ai_move_expire = 0.0
            self._ai_move_distance = 0.0
            ai_controlled = False
            mouse_driven = False
        else:
            if (self._ai_move and self._ai_move_expire
                    and now > self._ai_move_expire):
                self._ai_move = None
                self._ai_move_speed = 0.0
                self._ai_move_expire = 0.0
                self._ai_move_distance = 0.0

            mouse_driven = self._mouse_proximity_step()

            ai_controlled = False
            if (not mouse_driven
                    and self._ai_move in ("left", "right", "stop")):
                ai_controlled = True
                if self._ai_move == "left":
                    self.vx = self._ai_move_speed or -6.0
                elif self._ai_move == "right":
                    self.vx = self._ai_move_speed or 6.0
                elif self._ai_move == "stop":
                    self.vx = 0.0
                    self._ai_move = None
                    ai_controlled = False

            if ai_controlled and self._ai_move_distance > 0:
                moved = abs(self.pos_x - self._ai_move_start_x)
                if moved >= self._ai_move_distance:
                    self._ai_move = "stop"
                    self._ai_move_speed = 0.0
                    self.vx = 0.0
                    self._set_anim("idle")

        self.vy += self._gravity
        self.pos_y += self.vy
        self.pos_x += self.vx

        w, h = self.cur_size
        hit_h = False
        hit_v = False
        on_ground_now = False

        if self.pos_y + h >= sh:
            self.pos_y = sh - h
            if abs(self.vy) > COLLIDE_MIN_SPEED:
                hit_v = True
            if abs(self.vy) > 1.5:
                self.vy = -self.vy * 0.35
            else:
                self.vy = 0
            self._jumping = False
            on_ground_now = True
        if self.pos_y < 0:
            self.pos_y = 0
            if abs(self.vy) > COLLIDE_MIN_SPEED:
                hit_v = True
            self.vy = 0

        if self.pos_x < 0:
            self.pos_x = 0
            if abs(self.vx) > COLLIDE_MIN_SPEED:
                hit_h = True
            self.vx = -self.vx * 0.35
            if ai_controlled:
                self._ai_move = "stop"
                self._ai_move_speed = 0.0
                self._set_anim("idle")
        elif self.pos_x + w > sw:
            self.pos_x = sw - w
            if abs(self.vx) > COLLIDE_MIN_SPEED:
                hit_h = True
            self.vx = -self.vx * 0.35
            if ai_controlled:
                self._ai_move = "stop"
                self._ai_move_speed = 0.0
                self._set_anim("idle")

        if (not ai_controlled) and (not mouse_driven):
            if on_ground_now:
                self.vx *= INERTIA_FRICTION_GROUND
            else:
                self.vx *= INERTIA_FRICTION_AIR
            if abs(self.vx) < 0.1:
                self.vx = 0.0

        if hit_h or hit_v:
            self._trigger_collide()

        if self._dragging:
            self.tilt = max(-25.0, min(25.0, -self.vx * 1.5))
        else:
            self.tilt += (0 - self.tilt) * 0.12

        self._on_ground = on_ground_now

        user32.SetWindowPos(self._hwnd, HWND_TOPMOST,
                            int(self.pos_x), int(self.pos_y), 0, 0,
                            SWP_NOSIZE | SWP_NOACTIVATE)

    def _update_hover(self):
        """Update the smoothed cursor-offset used for hover deformation."""
        if self._dragging or self._cur_img is None:
            self._hover_tx = 0.0
            self._hover_ty = 0.0
            self._hover_dx += (0.0 - self._hover_dx) * 0.2
            self._hover_dy += (0.0 - self._hover_dy) * 0.2
            return
        pt = POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        cx = self.pos_x + self.cur_size[0] / 2
        cy = self.pos_y + self.cur_size[1] / 2
        dx = (pt.x - cx) / max(1, self.cur_size[0] / 2)
        dy = (pt.y - cy) / max(1, self.cur_size[1] / 2)
        self._hover_tx = max(-1.0, min(1.0, dx))
        self._hover_ty = max(-1.0, min(1.0, dy))
        self._hover_dx += (self._hover_tx - self._hover_dx) * 0.2
        self._hover_dy += (self._hover_ty - self._hover_dy) * 0.2

    # ---------------- Mouse drag handling ----------------
    def _on_press(self):
        """Begin a drag: record the offset between the cursor and the pet."""
        pt = POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        self._press_root = (pt.x, pt.y)
        self._press_win = (int(self.pos_x), int(self.pos_y))
        self._drag_offset = (pt.x - int(self.pos_x), pt.y - int(self.pos_y))
        self._last_x = pt.x
        self._last_y = pt.y
        self._last_t = time.monotonic()
        self.vx = 0.0
        self.vy = 0.0
        self._jumping = False
        self._ai_move = None
        self._ai_move_speed = 0.0
        self._ai_move_expire = 0.0
        self._ai_move_distance = 0.0
        self._dragging = True
        self._anim_is_oneshot = False
        self._set_anim("drag")
        self.gesture.start(pt.x, pt.y)

        base = DRAG_REACT_BASE_INTERVAL
        interval = base / max(0.1, self._drag_chattiness)
        self._drag_react_after = time.monotonic() + interval
        self._drag_react_count = 0

        self._speak_async(
            "The user just grabbed you. React in one short sentence, in "
            "the moment. Staying silent with [[refuse]] is allowed.",
            autonomous=False, interrupt=True)

    def _on_motion(self):
        """Keep the pet under the cursor while dragging."""
        if not self._dragging:
            return
        pt = POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        now = time.monotonic()
        dt = now - self._last_t
        if dt > 0:
            self.vx = (pt.x - self._last_x) / dt * INERTIA_SAMPLE
            self.vy = (pt.y - self._last_y) / dt * (INERTIA_SAMPLE * 0.5)
            self.vx = max(-MAX_INERTIA_VX, min(MAX_INERTIA_VX, self.vx))
            self.vy = max(-MAX_INERTIA_VY, min(MAX_INERTIA_VY, self.vy))
        self._last_x = pt.x
        self._last_y = pt.y
        self._last_t = now

        self.gesture.update(pt.x, pt.y)

        target_x = pt.x - self._drag_offset[0]
        target_y = pt.y - self._drag_offset[1]
        w, h = self.cur_size
        sw = user32.GetSystemMetrics(0)
        sh = user32.GetSystemMetrics(1)
        if target_x < 0:
            target_x = 0
        if target_y < 0:
            target_y = 0
        if target_x + w > sw:
            target_x = sw - w
        if target_y + h > sh:
            target_y = sh - h
        self.pos_x = float(target_x)
        self.pos_y = float(target_y)

        self.tilt = max(-25.0, min(25.0, -self.vx * 1.5))

        try:
            if (now >= self._drag_react_after
                    and self._drag_react_count < DRAG_REACT_MAX_PER_DRAG
                    and not self._ai_busy):
                m = self.gesture.metrics()
                if (m["duration_s"] >= DRAG_REACT_MIN_DURATION
                        or m["distance_px"] >= DRAG_REACT_MIN_DISTANCE):
                    self._drag_react_count += 1
                    base = DRAG_REACT_BASE_INTERVAL
                    factor = 1.0 + 0.5 * self._drag_react_count
                    interval = ((base * factor)
                                / max(0.1, self._drag_chattiness))
                    self._drag_react_after = now + interval
                    self._speak_async(
                        "The user is currently holding you mid-air. "
                        f"Current motion: {self.gesture.describe()}. "
                        "React in one short sentence, in the moment. "
                        "Staying silent with [[refuse]] is allowed.",
                        autonomous=False, interrupt=True)
        except Exception:
            pass

    def _on_release(self):
        """End a drag and record the gesture for the AI."""
        if self._dragging:
            self._dragging = False
            if abs(self.vy) < 1.0:
                self.vy = 1.0
            self._set_anim("idle")
            self._gesture_summary = self.gesture.describe()
            log_ai("GESTURE", self._gesture_summary)
            self._speak_async(
                "The user just interacted with you. React naturally in "
                "one short sentence. Staying silent with [[refuse]] is "
                "allowed.",
                autonomous=False)
            self._request_state_refresh()

    # ---------------- AI action parsing ----------------
    def _parse_actions(self, text):
        """Extract action tags from the reply and return (clean, actions)."""
        actions = []
        for m in ACTION_RE.finditer(text):
            actions.append(m.group(1).lower())
        clean = ACTION_RE.sub("", text).strip()
        if clean and not re.search(r"[A-Za-z0-9\u4e00-\u9fff]", clean):
            clean = ""
        return clean, actions

    def _apply_actions(self, actions):
        """Execute the action tags contained in an AI reply."""
        now = time.monotonic()
        for a in actions:
            if a.startswith("move:"):
                if not self._on_ground:
                    log_ai("MOVE_AIR", a)
                    continue
                parts = a.split(";", 1)
                direction = parts[0].split(":", 1)[1].strip()
                speed = 6.0
                distance = 0.0
                if len(parts) == 2:
                    for kv in parts[1].split(";"):
                        if kv.startswith("speed="):
                            try:
                                speed = float(kv.split("=", 1)[1])
                            except Exception:
                                pass
                        elif kv.startswith("distance="):
                            try:
                                distance = float(kv.split("=", 1)[1])
                            except Exception:
                                pass
                speed = max(MOVE_SPEED_MIN, min(MOVE_SPEED_MAX, speed))
                distance = max(MOVE_DISTANCE_MIN,
                               min(MOVE_DISTANCE_MAX, distance))
                if direction == "left":
                    self._ai_move = "left"
                    self._ai_move_speed = -speed
                    self._ai_move_expire = now + 30.0
                    self._ai_move_distance = distance
                    self._ai_move_start_x = self.pos_x
                    self._set_anim("left")
                elif direction == "right":
                    self._ai_move = "right"
                    self._ai_move_speed = speed
                    self._ai_move_expire = now + 30.0
                    self._ai_move_distance = distance
                    self._ai_move_start_x = self.pos_x
                    self._set_anim("right")
                elif direction == "stop":
                    self._ai_move = None
                    self._ai_move_speed = 0.0
                    self._ai_move_expire = 0.0
                    self._ai_move_distance = 0.0
                    self._set_anim("idle")
            elif a.startswith("jump"):
                if not self._on_ground:
                    log_ai("JUMP_AIR", a)
                    continue
                height = 12.0
                if ";" in a:
                    for kv in a.split(";")[1:]:
                        if kv.startswith("height="):
                            try:
                                height = float(kv.split("=", 1)[1])
                            except Exception:
                                pass
                height = max(JUMP_HEIGHT_MIN, min(JUMP_HEIGHT_MAX, height))
                if abs(self.vy) < 1:
                    self.vy = -math.sqrt(2 * self._gravity * height)
                    self._jumping = True
                    self._on_ground = False
            elif a.startswith("scale:"):
                raw = a.split(":", 1)[1].strip()
                try:
                    if raw.startswith("+") or raw.startswith("-"):
                        self._ai_scale = max(
                            SCALE_MIN,
                            min(SCALE_MAX,
                                self._ai_scale + float(raw)))
                    else:
                        self._ai_scale = max(
                            SCALE_MIN,
                            min(SCALE_MAX, float(raw)))
                    log_ai("SCALE", f"ai_scale={self._ai_scale:.2f}")
                except Exception as e:
                    log_ai("SCALE_ERR", f"{raw}: {e}")
            elif a == "refuse":
                self.vx = 0.0
                self._ai_move = None
                self._ai_move_speed = 0.0
                self._ai_move_distance = 0.0
                self._set_anim("idle")

    def _speak_async(self, user_text, autonomous=False, greeting=False,
                     interrupt=False, chat_mode=False, auto_kind=None):
        """Queue an AI request.

        interrupt allows pre-empting an in-flight request. chat_mode
        marks a user-initiated chat message, where silence is shown as
        "......". auto_kind is "talk" or "act" for autonomous requests
        so the runtime can account for them.
        """
        if not self.cfg["deepseek_api_key"]:
            return
        if self._ai_busy and not interrupt:
            return
        self._thought_token += 1
        token = self._thought_token
        try:
            self._ai_queue.put_nowait(
                (user_text, autonomous, token, chat_mode, auto_kind))
        except queue.Full:
            pass

    # ---------------- Right-click menu ----------------
    def _popup_menu(self):
        """Show the right-click menu at the cursor."""
        pt = POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        try:
            self.menu.tk_popup(pt.x, pt.y)
        finally:
            self.menu.grab_release()

    def build_menu(self):
        """Build the right-click menu with Talk, Language and Close."""
        lang = self.cfg.get("language", "zh")
        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label=tr(lang, "talk"),
                              command=self._on_talk)
        lang_menu = tk.Menu(self.menu, tearoff=0)
        self.lang_var = tk.StringVar(value=lang)
        for code, name in LANGUAGE_NAMES.items():
            lang_menu.add_radiobutton(
                label=name, variable=self.lang_var, value=code,
                command=lambda c=code: self._set_language(c))
        self.menu.add_cascade(label=tr(lang, "language"), menu=lang_menu)
        self.menu.add_separator()
        self.menu.add_command(label=tr(lang, "close"),
                              command=self._close)

    def _set_language(self, code):
        """Switch the reply language and update the menu labels."""
        self.cfg["language"] = code
        self._self_write_t = time.time()
        save_config(self.cfg)
        if self.brain is not None:
            self.brain.language_code = code
        try:
            self.menu.entryconfigure(0, label=tr(code, "talk"))
            self.menu.entryconfigure(1, label=tr(code, "language"))
            self.menu.entryconfigure(3, label=tr(code, "close"))
        except Exception:
            pass

    def _on_talk(self):
        """Open the chat dialog and send its content to the AI."""
        lang = self.cfg.get("language", "zh")
        dlg = ChatDialog(
            self.root,
            title=tr(lang, "chat_title"),
            hint=tr(lang, "chat_hint"),
            send_label=tr(lang, "chat_send"),
            cancel_label=tr(lang, "chat_cancel"),
        )
        text = dlg.show()
        if not text:
            return
        log_ai("USER", text)
        self._speak_async(f"The user said: {text}", chat_mode=True)
        self._request_state_refresh()

    # ---------------- Shutdown ----------------
    def _close(self):
        """Close the pet, fading it out unless the personality refuses."""
        if self._closing:
            return
        self._closing = True

        if self._refuse_close:
            self._speak_async(
                "The user tried to close you. If your personality would "
                "refuse, reply with a short refusal line.",
                autonomous=False)
            self._closing = False
            return

        def _fade_step(step=0):
            total_steps = max(1, int(CLOSE_FADE_SECONDS * 1000
                                     / CLOSE_FADE_STEP_MS))
            if step >= total_steps:
                _do_close()
                return
            self._render_alpha = max(0, int(255
                                            * (1.0 - step / total_steps)))
            self._render_current(force=True)
            try:
                self.root.after(CLOSE_FADE_STEP_MS,
                                lambda: _fade_step(step + 1))
            except Exception:
                _do_close()

        def _do_close():
            self._closing_final = True
            self._watch_running = False
            try:
                self._ai_queue.put_nowait(None)
            except Exception:
                pass
            try:
                self.input_monitor.stop()
            except Exception:
                pass
            try:
                if self._bmp_cache_hbmp:
                    gdi32.DeleteObject(self._bmp_cache_hbmp)
                    self._bmp_cache_hbmp = None
            except Exception:
                pass
            try:
                if self._hwnd:
                    user32.DestroyWindow(self._hwnd)
                if self._bubble_hwnd:
                    user32.DestroyWindow(self._bubble_hwnd)
            except Exception:
                pass
            try:
                if self._mem_dc:
                    gdi32.DeleteDC(self._mem_dc)
                if self._screen_dc:
                    user32.ReleaseDC(None, self._screen_dc)
            except Exception:
                pass
            try:
                self.root.quit()
                self.root.destroy()
            except Exception:
                pass
            release_single_instance()
            os._exit(0)

        try:
            self._hide_bubble()
            self.goodbye(lambda: None)
        except Exception:
            pass
        try:
            self.root.after(100, lambda: _fade_step(0))
        except Exception:
            _do_close()

    def run(self):
        """Enter the Tk main loop."""
        try:
            self.root.mainloop()
        except Exception:
            pass


# =========================================================================
# Entry point
# =========================================================================

def main():
    """Program entry point."""
    if getattr(sys, "frozen", False):
        os.chdir(os.path.dirname(sys.executable))
    if acquire_single_instance() is None:
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showinfo("AI Pet",
                                "Another AI Pet is already running.")
            root.destroy()
        except Exception:
            pass
        sys.exit(0)
    ensure_dirs()
    cfg = load_config()
    lang = cfg.get("language", "zh")

    set_autostart(bool(cfg.get("autostart", False)))

    loading = LoadingScreen(lang)
    loading.set(5, "Loading config...")
    time.sleep(0.05)

    if not cfg.get("deepseek_api_key"):
        loading.close()
        messagebox.showerror("AI Pet", tr(lang, "err_key"))
        sys.exit(1)
    if not cfg.get("personality", "").strip():
        loading.close()
        messagebox.showerror("AI Pet", tr(lang, "err_personality"))
        sys.exit(1)

    loading.set(20, "Preparing pet...")
    pet = AIPet(cfg, loading)

    loading.set(30, "Loading self-image in background...")
    threading.Thread(target=pet.load_texture, daemon=True).start()

    loading.set(45, "Preparing brain...")
    pet.init_brain()
    time.sleep(0.05)

    loading.set(65, "Starting AI worker...")
    pet.start_ai_worker()

    loading.set(75, "Starting input monitor...")
    pet.input_monitor.start()

    loading.set(80, "Starting sampler loop...")
    threading.Thread(target=pet._sample_loop, daemon=True).start()

    loading.set(85, "Starting file watcher...")
    pet.start_watcher()

    loading.set(90, "Preparing windows...")
    pet._init_tk()
    pet.create_windows()
    pet.build_menu()

    loading.set(98, "Starting loops...")
    pet._tick_anim()
    pet._tick_phys()

    loading.set(100, tr(lang, "ok"))
    time.sleep(0.2)
    loading.close()

    pet.greet()
    pet.run()


if __name__ == "__main__":
    main()
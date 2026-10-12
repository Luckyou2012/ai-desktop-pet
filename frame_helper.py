# -*- coding: utf-8 -*-
"""
frame_helper.py — 独立补帧进程
作用：通过 Windows 多媒体定时器 + 独立线程，稳定地每隔 N 毫秒
      向 stdout 输出一行 "TICK"，主进程读它来触发额外一帧渲染。
      此进程本身不绘制任何东西，只做“节拍器”。
用法：python frame_helper.py [interval_ms]
"""
import ctypes
import ctypes.wintypes as wt
import sys
import threading
import time

winmm = ctypes.windll.winmm
kernel32 = ctypes.windll.kernel32

TIMERR_NOERROR = 0
TIME_PERIODIC = 1
CALLBACK_FUNCTION = 0x00030000


class TIMECAPS(ctypes.Structure):
    _fields_ = [("wPeriodMin", ctypes.c_uint), ("wPeriodMax", ctypes.c_uint)]


_timer_proc_ref = None
_running = True


def _emit():
    try:
        sys.stdout.write("TICK\n")
        sys.stdout.flush()
    except Exception:
        pass


def _timer_callback(uTimerID, uMsg, dwUser, dw1, dw2):
    if _running:
        _emit()


def _run_with_timer(interval_ms):
    global _timer_proc_ref
    TIMECALLBACK = ctypes.WINFUNCTYPE(
        None, ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_ulong),
        ctypes.c_ulong, ctypes.c_ulong)
    _timer_proc_ref = TIMECALLBACK(_timer_callback)

    caps = TIMECAPS()
    winmm.timeGetDevCaps(ctypes.byref(caps), ctypes.sizeof(caps))
    period = max(caps.wPeriodMin, min(caps.wPeriodMax, interval_ms))
    winmm.timeBeginPeriod(period)

    timer_id = ctypes.c_uint(0)
    winmm.timeSetEvent(
        ctypes.c_uint(period),
        ctypes.c_uint(0),
        _timer_proc_ref,
        ctypes.c_uint(0),
        ctypes.c_uint(TIME_PERIODIC),
    )
    while _running:
        time.sleep(1.0)
    try:
        winmm.timeKillTimer(timer_id)
        winmm.timeEndPeriod(period)
    except Exception:
        pass


def _run_fallback(interval_ms):
    """没有 winmm 时退化为纯 sleep 循环。"""
    while _running:
        _emit()
        time.sleep(interval_ms / 1000.0)


def main():
    interval_ms = 33
    if len(sys.argv) >= 2:
        try:
            interval_ms = max(5, int(sys.argv[1]))
        except Exception:
            pass
    try:
        _run_with_timer(interval_ms)
    except Exception:
        _run_fallback(interval_ms)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        _running = False
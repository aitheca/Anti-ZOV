"""
antizetnik_migalka.py
Windows: мигающий антизетник с исчезающими блоками, проигрыванием migalka.mp3,
toast-уведомлением "Обнаружен зетник!" и скрытием консоли.
Нажми ESC чтобы завершить.
"""

import threading
import time
import re
import queue
import sys
from dataclasses import dataclass
import os

# ---- сторонние библиотеки ----
try:
    import mss
    from PIL import Image
    import pytesseract
    from playsound import playsound
    from win10toast import ToastNotifier
    import tkinter as tk
except Exception as e:
    print("Ошибка импорта библиотек:", e)
    print("Установи зависимости: pip install mss pillow pytesseract playsound==1.2.2 win10toast")
    sys.exit(1)

# --------------------- НАСТРОЙКИ ---------------------
# путь к tesseract (подтвердил ранее)
pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# mp3 сирены (файл должен лежать рядом со скриптом или укажи абсолютный путь)
SIREN_PATH = os.path.join(os.path.dirname(__file__), "migalka.mp3")

# Запрещённые паттерны — включаем варианты, которые просил.
# Используем \b чтобы по возможности ловить отдельные слова, но учти, что OCR может отделять/сливать символы.
BANNED_PATTERNS = [
    r"\bZV\b",
    r"\bZVO\b",
    r"\bZOV\b",
    r"\bZZZ\b",
    r"\bZZ\b",
    r"\bZ\b",   # опционально — одно "Z" (удалить если слишком много ложных)
]
# Собираем в одно регулярное выражение
BANNED_RE = re.compile("|".join(BANNED_PATTERNS), re.IGNORECASE)

# Частота скриншотов (сек)
CAPTURE_INTERVAL = 0.45

# Время, в течение которого найденный блок остаётся активным (сек)
BOX_TTL = 1.2

# Интервал мигания (сек)
BLINK_INTERVAL = 0.25

# Рефрешь оверлея (мс)
REFRESH_MS = 30

# OCR конфиг
OCR_CONFIG = r"--oem 3 --psm 6"

# padding вокруг слова
BOX_PADDING = 6

# Минимальная задержка между alert-ами (чтобы не спамить звуком/уведомлениями)
ALERT_COOLDOWN = 1.2

# Скрывать консоль? True — попытаемся скрыть (Windows)
HIDE_CONSOLE = True
# -----------------------------------------------------

# Уведомления (win10toast)
toaster = ToastNotifier()

@dataclass
class Box:
    x: int
    y: int
    w: int
    h: int
    last_seen: float  # время последнего обнаружения

def try_hide_console():
    """Попытка скрыть консольное окно (Windows)."""
    if not HIDE_CONSOLE:
        return
    try:
        import ctypes
        wh = ctypes.windll.kernel32.GetConsoleWindow()
        if wh:
            # 0 = SW_HIDE
            ctypes.windll.user32.ShowWindow(wh, 0)
    except Exception:
        pass

def preprocess_pil_for_ocr(pil_img: Image.Image) -> Image.Image:
    # простая предобработка: grayscale + небольшой ресайз для маленьких экранов
    img = pil_img.convert("L")
    w, h = img.size
    if min(w, h) < 1000:
        img = img.resize((int(w * 1.25), int(h * 1.25)), Image.BILINEAR)
    return img

def ocr_worker(rect_queue: queue.Queue, stop_event: threading.Event, alert_event: threading.Event):
    """
    Фоновый поток: делает скрин, распознаёт текст, при совпадении добавляет прямоугольники в очередь.
    Также сигналит alert_event, если обнаружен запрещённый текст.
    """
    sct = mss.mss()
    monitor = sct.monitors[0]
    while not stop_event.is_set():
        t0 = time.time()
        try:
            sct_img = sct.grab(monitor)
            pil = Image.frombytes("RGB", sct_img.size, sct_img.rgb)
            pre = preprocess_pil_for_ocr(pil)
            data = pytesseract.image_to_data(pre, output_type=pytesseract.Output.DICT, config=OCR_CONFIG)
            boxes = []
            n = len(data.get("text", []))
            found_any = False
            for i in range(n):
                word = data["text"][i].strip()
                if not word:
                    continue
                if BANNED_RE.search(word):
                    found_any = True
                    # пересчёт координат к оригиналу (если был ресайз)
                    pre_w, pre_h = pre.size
                    orig_w, orig_h = pil.size
                    scale_x = orig_w / pre_w
                    scale_y = orig_h / pre_h
                    left = int(data["left"][i] * scale_x)
                    top = int(data["top"][i] * scale_y)
                    width = int(data["width"][i] * scale_x)
                    height = int(data["height"][i] * scale_y)
                    bx = max(0, left - BOX_PADDING)
                    by = max(0, top - BOX_PADDING)
                    bw = max(4, min(orig_w - bx, width + 2 * BOX_PADDING))
                    bh = max(4, min(orig_h - by, height + 2 * BOX_PADDING))
                    boxes.append(Box(bx, by, bw, bh, time.time()))
            if boxes:
                rect_queue.put(boxes)
            if found_any:
                # сигнал — в основном для проигрывания звука/уведомления
                alert_event.set()
        except pytesseract.pytesseract.TesseractNotFoundError:
            print("Tesseract не найден — проверь pytesseract.pytesseract.tesseract_cmd", file=sys.stderr)
            stop_event.set()
            return
        except Exception as e:
            # просто логируем и продолжаем
            print("OCR worker error:", e, file=sys.stderr)
        # ожидание
        dt = time.time() - t0
        stop_event.wait(max(0.01, CAPTURE_INTERVAL - dt))

class TkOverlay:
    def __init__(self, rect_queue: queue.Queue, stop_event: threading.Event, alert_event: threading.Event):
        self.rect_queue = rect_queue
        self.stop_event = stop_event
        self.alert_event = alert_event

        # Tk init
        self.root = tk.Tk()
        self.root.title("Антизетник")
        self.root.overrideredirect(True)
        try:
            self.root.attributes("-topmost", True)
        except Exception:
            pass

        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.root.geometry(f"{sw}x{sh}+0+0")

        # transparentcolor работает в Windows
        try:
            self.root.attributes("-transparentcolor", "white")
        except Exception:
            pass

        self.canvas = tk.Canvas(self.root, width=sw, height=sh, highlightthickness=0, bg="white")
        self.canvas.pack(fill="both", expand=True)

        self.root.bind("<Escape>", self.on_escape)

        # map: key=(x,y,w,h) -> Box ; используем список для простоты
        self.boxes = []  # список Box, обновляем last_seen
        self.rect_ids = []  # активные canvas ids

        # для контроля alert (звук/уведомление)
        self.last_alert_time = 0.0

        # мигание toggle
        self.blink_state = True
        self.last_blink = time.time()

        # старт опроса очереди и рендер
        self.root.after(REFRESH_MS, self._update)

    def on_escape(self, _event=None):
        self.stop_event.set()
        try:
            self.root.destroy()
        except Exception:
            pass

    def _update(self):
        # читаем новые коробки из очереди и добавляем/обновляем
        updated = False
        try:
            while True:
                boxes = self.rect_queue.get_nowait()
                for b in boxes:
                    # если очень похожая коробка уже есть (пересечение), просто обновим last_seen
                    merged = False
                    for existing in self.boxes:
                        # простая проверка пересечения
                        if (abs(existing.x - b.x) < 30 and abs(existing.y - b.y) < 30) or \
                           (existing.x < b.x + b.w and b.x < existing.x + existing.w and existing.y < b.y + b.h and b.y < existing.y + existing.h):
                            existing.last_seen = time.time()
                            merged = True
                            break
                    if not merged:
                        self.boxes.append(b)
                    updated = True
        except queue.Empty:
            pass

        # проверим alert_event — если поднят и прошло ALERT_COOLDOWN — проиграть звук и показать уведомление
        if self.alert_event.is_set():
            now = time.time()
            if now - self.last_alert_time >= ALERT_COOLDOWN:
                self.last_alert_time = now
                # звук и уведомление в отдельных потоках
                threading.Thread(target=self._play_siren, daemon=True).start()
                threading.Thread(target=self._show_toast, daemon=True).start()
            # очистим флаг
            self.alert_event.clear()

        # очистим устаревшие коробки (если не видели BOX_TTL)
        now = time.time()
        new_boxes = []
        for b in self.boxes:
            if now - b.last_seen <= BOX_TTL:
                new_boxes.append(b)
            else:
                # пропал — не добавляем (значит исчезнет)
                updated = True
        self.boxes = new_boxes

        # мигание toggle
        if time.time() - self.last_blink >= BLINK_INTERVAL:
            self.blink_state = not self.blink_state
            self.last_blink = time.time()
            updated = True

        if updated:
            self._redraw()

        if not self.stop_event.is_set():
            self.root.after(REFRESH_MS, self._update)
        else:
            try:
                self.root.destroy()
            except Exception:
                pass

    def _redraw(self):
        # удаляем предыдущие отрисовки
        for cid in self.rect_ids:
            try:
                self.canvas.delete(cid)
            except Exception:
                pass
        self.rect_ids.clear()

        if not self.blink_state:
            # в "off" состоянии мигания ничего не рисуем
            return

        # рисуем текущие коробки
        for b in self.boxes:
            x, y, w, h = b.x, b.y, b.w, b.h
            # верх - жёлтая полоса
            id1 = self.canvas.create_rectangle(x, y, x + w, y + h // 2, fill="#FFD700", outline="")
            # низ - синяя полоса
            id2 = self.canvas.create_rectangle(x, y + h // 2, x + w, y + h, fill="#0057B7", outline="")
            self.rect_ids.extend([id1, id2])

    def _play_siren(self):
        try:
            if os.path.isfile(SIREN_PATH):
                # playsound блокирует поток, поэтому запускаем в отдельном потоке
                playsound(SIREN_PATH)
            else:
                # fallback — короткий beep (winsound)
                try:
                    import winsound
                    winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
                except Exception:
                    pass
        except Exception:
            pass

    def _show_toast(self):
        try:
            toaster.show_toast("Антизетник", "Обнаружен зетник!", duration=3, threaded=True)
        except Exception:
            # fallback: простое messagebox (не идеально)
            try:
                from tkinter import messagebox
                messagebox.showinfo("Антизетник", "Обнаружен зетник!")
            except Exception:
                pass

    def start(self):
        # главный цикл (главный поток)
        self.root.mainloop()

def main():
    try_hide_console()

    rect_queue = queue.Queue()
    stop_event = threading.Event()
    alert_event = threading.Event()

    worker = threading.Thread(target=ocr_worker, args=(rect_queue, stop_event, alert_event), daemon=True)
    worker.start()

    overlay = TkOverlay(rect_queue, stop_event, alert_event)
    try:
        overlay.start()
    except KeyboardInterrupt:
        stop_event.set()
    stop_event.set()
    worker.join(timeout=1.0)
    print("Антизетник остановлен.")

if __name__ == "__main__":
    print("Запуск антизетника (миг) — ESC чтобы выйти.")
    main()

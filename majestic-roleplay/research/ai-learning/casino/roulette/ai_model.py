import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import cv2
import mss
import os
import time
import keyboard
import win32api
import win32con
import pytesseract
import pickle
import csv
import logging
import threading
from datetime import datetime
from tkinter import Tk, filedialog


# ANSI цвета для консоли (работают в современных терминалах)
class Colors:
    GREEN = '\033[92m'  # Игры, номера
    ORANGE = '\033[38;5;208m'  # ИИ, прогнозы
    RED = '\033[91m'  # Реальные деньги, ставки
    BLUE = '\033[94m'  # Статистика
    YELLOW = '\033[93m'  # Предупреждения
    RESET = '\033[0m'
    BOLD = '\033[1m'


pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'

# ==========================================
# НАСТРОЙКИ
# ==========================================
CELLS = [
    (411, 113, 42, 14), (482, 116, 33, 10), (545, 116, 38, 9), (611, 117, 37, 7),
    (676, 116, 32, 11), (738, 117, 31, 6), (800, 118, 32, 5), (859, 116, 27, 9)
]
ZONE_GAME_TEXT = (989, 108, 58, 26)
ZONE_GAME_ID = (1060, 109, 80, 25)

COLOR_TARGETS = {'2X': (107, 107, 107), '3X': (46, 20, 106), '5X': (76, 106, 29), '10X': (34, 93, 125)}
REV_LABEL_MAP = {0: '2X', 1: '3X', 2: '5X', 3: '10X'}
LABEL_MAP = {v: k for k, v in REV_LABEL_MAP.items()}
MULTIPLIERS = {'2X': 2, '3X': 3, '5X': 5, '10X': 10}

INPUT_FIELD_CENTER = (560, 889)
BET_BUTTON_CENTER = (801, 983)

# 🔧 НАСТРОЙКИ СТРАТЕГИИ
TRIGGER_THRESHOLD = 20  # 🔥 Порог активации: 25 игр без 10X (было 20)
MAX_ATTEMPTS_PER_LEVEL = 10
BET_LEVELS = [10, 20, 40]  # Прогрессия ставок

# 🔧 НАСТРОЙКИ ИИ
AI_MIN_EV_TO_BET = 1.2  # 🔽 Снижен порог: ИИ ставит при EV > 1.2 (было 1.5)
AI_MAX_VIRTUAL_BET = 1000
AI_BASE_BET = 50  # Базовая ставка ИИ


# ==========================================
# 🧠 ИИ-МОЗГ С ВИРТУАЛЬНЫМ БАЛАНСОМ
# ==========================================
class AIBrain(nn.Module):
    def __init__(self):
        super().__init__()
        self.embedding = nn.Embedding(4, 16)
        self.lstm = nn.LSTM(16, 32, batch_first=True)
        self.fc = nn.Linear(32, 4)
        self.opt = torch.optim.Adam(self.parameters(), lr=0.001)

        self.history = []  # История для обучения

        # 💰 Виртуальный баланс (для обучения ИИ)
        self.virtual_balance = 1000.0
        self.virtual_bet_amount = 0
        self.virtual_target = None
        self.virtual_total_bets = 0
        self.virtual_wins = 0

        # 🎯 Реальный баланс (ваша стратегия)
        self.real_balance = 200.0
        self.real_bet_amount = 0
        self.real_target = '10X'

        # 📊 Статистика
        self.total_preds = 0
        self.correct_preds = 0

    def add_result(self, label):
        """Добавляет результат в историю для обучения"""
        idx = LABEL_MAP.get(label)
        if idx is None: return
        self.history.append(idx)
        if len(self.history) >= 9:
            self._train()

    def _train(self):
        """Обучение LSTM на последних данных"""
        if len(self.history) < 9: return
        seqs, tgts = [], []
        for i in range(len(self.history) - 8):
            seqs.append(self.history[i:i + 8])
            tgts.append(self.history[i + 8])
        if len(seqs) < 5: return

        X = torch.LongTensor(seqs[-50:])
        Y = torch.LongTensor(tgts[-50:])
        self.opt.zero_grad()
        emb = self.embedding(X)
        out, _ = self.lstm(emb)
        logits = self.fc(out[:, -1, :])
        loss = nn.CrossEntropyLoss()(logits, Y)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(self.parameters(), 1.0)
        self.opt.step()

    def predict(self):
        """Возвращает прогноз ИИ: лучший выбор, уверенность, вероятности, EV"""
        if len(self.history) < 8:
            return None, 0.0, {}, {}

        X = torch.LongTensor([self.history[-8:]])
        with torch.no_grad():
            emb = self.embedding(X)
            out, _ = self.lstm(emb)
            logits = self.fc(out[:, -1, :])
            probs = F.softmax(logits, dim=1)[0]

        # 🎯 Расчёт ожидаемой ценности (EV = вероятность × множитель)
        ev = {}
        for i, label in REV_LABEL_MAP.items():
            prob = float(probs[i])
            mult = MULTIPLIERS[label]
            ev[label] = prob * mult

        best = max(ev, key=ev.get)
        confidence = ev[best]
        probs_dict = {REV_LABEL_MAP[i]: float(probs[i]) * 100 for i in range(4)}

        return best, confidence, probs_dict, ev

    def place_virtual_bet(self, amount, target):
        """ИИ размещает виртуальную ставку для обучения"""
        if target not in LABEL_MAP or amount <= 0:
            return False
        self.virtual_bet_amount = amount
        self.virtual_target = target
        self.virtual_balance -= amount
        self.virtual_total_bets += 1
        return True

    def process_virtual_result(self, actual_label):
        """Обработка результата виртуальной ставки ИИ"""
        if self.virtual_target is None:
            return 0, None

        reward = 0
        win_info = None

        if actual_label == self.virtual_target:
            mult = MULTIPLIERS[actual_label]
            win_amount = self.virtual_bet_amount * mult
            self.virtual_balance += win_amount
            reward = win_amount - self.virtual_bet_amount
            self.virtual_wins += 1
            win_info = f"+${reward:.0f} (×{mult})"
        else:
            reward = -self.virtual_bet_amount
            win_info = f"-${self.virtual_bet_amount:.0f}"

        self.virtual_target = None
        return reward, win_info

    def place_real_bet(self, amount):
        """Размещение реальной ставки (ваша стратегия)"""
        self.real_bet_amount = amount
        self.real_balance -= amount
        return True

    def process_real_result(self, actual_label):
        """Обработка результата реальной ставки"""
        if actual_label == self.real_target:
            mult = MULTIPLIERS[actual_label]
            win_amount = self.real_bet_amount * mult
            self.real_balance += win_amount
            return win_amount - self.real_bet_amount, f"+${(win_amount - self.real_bet_amount):.0f} (×{mult})"
        return -self.real_bet_amount, f"-${self.real_bet_amount:.0f}"

    def save(self, path):
        torch.save({
            'state': self.state_dict(),
            'opt': self.opt.state_dict(),
            'hist': self.history,
            'virtual_balance': self.virtual_balance,
            'virtual_bets': self.virtual_total_bets,
            'virtual_wins': self.virtual_wins,
            'real_balance': self.real_balance,
            'stats': {'total': self.total_preds, 'correct': self.correct_preds}
        }, path)

    def load(self, path):
        if os.path.exists(path):
            try:
                ck = torch.load(path, map_location='cpu', weights_only=False)
                self.load_state_dict(ck['state'])
                self.opt.load_state_dict(ck['opt'])
                self.history = ck.get('hist', [])
                self.virtual_balance = ck.get('virtual_balance', 1000.0)
                self.virtual_total_bets = ck.get('virtual_bets', 0)
                self.virtual_wins = ck.get('virtual_wins', 0)
                self.real_balance = ck.get('real_balance', 200.0)
                stats = ck.get('stats', {})
                self.total_preds = stats.get('total', 0)
                self.correct_preds = stats.get('correct', 0)
                return True
            except:
                pass
        return False


# ==========================================
# 💰 СИСТЕМА СТАВОК (Ваша стратегия 10X)
# ==========================================
class BettingSystem:
    def __init__(self):
        self.active = False
        self.level = 0  # 0=$10, 1=$20, 2=$40
        self.attempts = 0
        self.amounts = BET_LEVELS
        self.max_attempts = MAX_ATTEMPTS_PER_LEVEL
        self.last_bet_round = None
        self.total_spent = 0
        self.total_won = 0

    def activate(self):
        self.active = True
        self.level = 0
        self.attempts = 0
        self.last_bet_round = None

    def deactivate(self):
        self.active = False
        self.level = 0
        self.attempts = 0
        self.last_bet_round = None

    def place(self, click_func):
        """Размещает ставку в игре"""
        if self.last_bet_round is not None:
            return False
        amount = self.amounts[self.level]
        click_func(*INPUT_FIELD_CENTER)
        time.sleep(0.5)
        keyboard.press_and_release('ctrl+a')
        time.sleep(0.1)
        keyboard.write(str(amount))
        time.sleep(0.5)
        click_func(*BET_BUTTON_CENTER)
        time.sleep(0.5)
        self.last_bet_round = True
        self.total_spent += amount
        return True

    def process_result(self, was_10x):
        """Обрабатывает результат раунда"""
        self.last_bet_round = None
        if was_10x:
            win_amount = self.amounts[self.level] * 10
            self.total_won += win_amount
            self.deactivate()
            return True, win_amount - self.amounts[self.level]

        self.attempts += 1
        if self.attempts >= self.max_attempts and self.level < 2:
            self.level += 1
            self.attempts = 0
        return False, -self.amounts[self.level]


# ==========================================
# 🛡️ МЕНЕДЖЕР ВЫЖИВАНИЯ
# ==========================================
class SurvivalManager:
    def __init__(self, bot):
        self.bot = bot
        self.next_run = time.time() + 1800
        self.active = False
        self.thread = None

    def start(self):
        self.active = True
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self.active = False

    def _press(self, vk, sc):
        win32api.keybd_event(vk, sc, 0, 0)
        time.sleep(0.05)
        win32api.keybd_event(vk, sc, win32con.KEYEVENTF_KEYUP, 0)

    def _click(self, x, y):
        win32api.SetCursorPos((x, y))
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, x, y, 0, 0)
        time.sleep(0.05)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, x, y, 0, 0)

    def _loop(self):
        while self.active:
            time.sleep(1)
            if self.bot.betting.active:
                continue  # 🔒 Приоритет ставок над едой
            if time.time() >= self.next_run:
                self._execute()
                self.next_run = time.time() + 1800

    def _execute(self):
        self.bot.logger.info(f"\n{Colors.YELLOW}🍔 ПЕРСОНАЖ ГОЛОДЕН! Пауза...{Colors.RESET}")
        self.bot.is_busy = True
        try:
            self._press(0x71, 0x3C);
            time.sleep(2)
            self._press(0xC0, 0x29);
            time.sleep(1)
            self._click(1798, 951);
            time.sleep(2)
            self._click(1801, 999);
            time.sleep(2)
            self._press(0xC0, 0x29);
            time.sleep(1)
            self._press(0x71, 0x3C);
            time.sleep(2)
            self._click(147, 359);
            time.sleep(2)
            self._click(158, 499);
            time.sleep(7)
            self.bot.logger.info(f"{Colors.GREEN}✅ Выживание завершено.{Colors.RESET}\n")
        except Exception as e:
            self.bot.logger.error(f"{Colors.RED}❌ Ошибка выживания: {e}{Colors.RESET}")
        finally:
            self.bot.is_busy = False


# ==========================================
# 🎨 Кастомный логгер с цветами
# ==========================================
class ColoredFormatter(logging.Formatter):
    """Добавляет цвета в консольный вывод, но не в файл"""

    LEVEL_COLORS = {
        'INFO': Colors.GREEN,
        'WARNING': Colors.YELLOW,
        'ERROR': Colors.RED,
    }

    def format(self, record):
        # Сохраняем оригинальный формат
        original = super().format(record)

        # Если вывод в консоль - добавляем цвета
        if hasattr(record, 'color') and record.color:
            color = self.LEVEL_COLORS.get(record.levelname, Colors.RESET)
            return f"{color}{original}{Colors.RESET}"

        return original


# ==========================================
# 🤖 ГЛАВНЫЙ БОТ
# ==========================================
class RouletteBot:
    def __init__(self, folder):
        self.folder = folder
        self.log_file = os.path.join(folder, f"bot_{datetime.now().strftime('%Y-%m-%d')}.txt")
        self.csv_file = os.path.join(folder, f"data_{datetime.now().strftime('%Y-%m-%d')}.csv")
        self.name_check = os.path.join(folder, "name_check.png")
        self.state_file = os.path.join(folder, "state.pkl")
        self.ai_file = os.path.join(folder, "ai_model.pt")

        # 🎨 Настройка логгера с цветами
        self.logger = logging.getLogger("Bot")
        self.logger.setLevel(logging.INFO)
        self.logger.handlers = []  # Очищаем хендлеры

        # Файловый хендлер (без цветов)
        file_handler = logging.FileHandler(self.log_file, encoding='utf-8')
        file_handler.setFormatter(logging.Formatter('%(asctime)s | %(message)s'))
        self.logger.addHandler(file_handler)

        # Консольный хендлер (с цветами)
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(ColoredFormatter('%(asctime)s | %(message)s'))
        self.logger.addHandler(console_handler)

        if not os.path.exists(self.csv_file):
            with open(self.csv_file, 'w', newline='', encoding='utf-8') as f:
                csv.writer(f).writerow([
                    "Time", "CurrentID", "PreviousID", "Result",
                    "AI_Pred", "AI_EV", "VirtualBal", "RealBal",
                    "BetActive", "Win", "StreakNo10X"
                ])

        self.ai = AIBrain()
        self.betting = BettingSystem()
        self.survival = SurvivalManager(self)
        self.survival.start()

        self.last_game_id = 0
        self.is_busy = False
        self.running = False

        # 🔑 ОТДЕЛЬНЫЙ СЧЁТЧИК для активации (не зависит от ai.history!)
        self.streak_no_10x = 0

        self.load_state()
        self._log_header()

    def _log_header(self):
        """Красивый заголовок при запуске"""
        self.logger.info(f"\n{Colors.BOLD}{'=' * 70}{Colors.RESET}")
        self.logger.info(f"{Colors.BOLD}🤖 ROULETTE BOT v4.0 - AI + Strategy{Colors.RESET}")
        self.logger.info(f"{Colors.BOLD}{'=' * 70}{Colors.RESET}")
        self.logger.info(f"{Colors.GREEN}🎮 Текущая игра: #{self.last_game_id}{Colors.RESET}")
        self.logger.info(f"{Colors.RED}💰 Реальный баланс: ${self.ai.real_balance:.0f}{Colors.RESET}")
        self.logger.info(f"{Colors.ORANGE}🧠 Виртуальный баланс ИИ: ${self.ai.virtual_balance:.0f}{Colors.RESET}")
        self.logger.info(
            f"{Colors.BLUE}📊 Стратегия: {TRIGGER_THRESHOLD} игр без 10X → Ставки $10→$20→$40{Colors.RESET}")
        self.logger.info(f"{Colors.BOLD}{'=' * 70}{Colors.RESET}")
        self.logger.info(f"\n{Colors.YELLOW}F4 - Старт/Стоп | 1+2 - Выход | 1+3 - Сменить папку{Colors.RESET}\n")

    def _save_ui_ref(self):
        """Создаёт эталон интерфейса ТОЛЬКО при первом запуске"""
        try:
            time.sleep(0.5)
            with mss.mss() as sct:
                x, y, w, h = ZONE_GAME_TEXT
                img = np.array(sct.grab({"left": x, "top": y, "width": w, "height": h}))
                cv2.imwrite(self.name_check, cv2.cvtColor(img, cv2.COLOR_BGRA2BGR))
        except:
            pass

    def save_state(self):
        try:
            state = {
                'last_id': self.last_game_id,
                'bet_active': self.betting.active,
                'bet_level': self.betting.level,
                'bet_att': self.betting.attempts,
                'streak': self.streak_no_10x,
                'ai_bal': self.ai.real_balance,
                'virt_bal': self.ai.virtual_balance,
                'bet_spent': self.betting.total_spent,
                'bet_won': self.betting.total_won
            }
            with open(self.state_file, 'wb') as f:
                pickle.dump(state, f)
            self.ai.save(self.ai_file)
        except:
            pass

    def load_state(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'rb') as f:
                    state = pickle.load(f)
                self.last_game_id = state.get('last_id', 0)
                self.streak_no_10x = state.get('streak', 0)
                if state.get('bet_active', False):
                    self.betting.activate()
                    self.betting.level = state.get('bet_level', 0)
                    self.betting.attempts = state.get('bet_att', 0)
                self.betting.total_spent = state.get('bet_spent', 0)
                self.betting.total_won = state.get('bet_won', 0)
                if 'ai_bal' in state:
                    self.ai.real_balance = state['ai_bal']
                if 'virt_bal' in state:
                    self.ai.virtual_balance = state['virt_bal']
            except:
                pass

        if not self.ai.load(self.ai_file):
            self.logger.warning(f"{Colors.YELLOW}⚠️ Не удалось загрузить ИИ, начинаю с нуля{Colors.RESET}")
            self.ai = AIBrain()

    def _get_id(self, sct):
        x, y, w, h = ZONE_GAME_ID
        img = np.array(sct.grab({"left": x, "top": y, "width": w, "height": h}))
        bgr = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        _, th = cv2.threshold(gray, 150, 255, cv2.THRESH_BINARY)
        raw = pytesseract.image_to_string(th, config='--psm 7').strip()
        digits = "".join(filter(str.isdigit, raw))
        return int(digits) if len(digits) >= 4 else None

    def _check_ui(self, sct):
        x, y, w, h = ZONE_GAME_TEXT
        img = np.array(sct.grab({"left": x, "top": y, "width": w, "height": h}))
        bgr = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
        if not os.path.exists(self.name_check):
            return True
        ref = cv2.imread(self.name_check)
        if ref is None or ref.shape != bgr.shape:
            return False
        diff = cv2.absdiff(ref, bgr)
        nz = cv2.countNonZero(cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY))
        return (nz / (ref.shape[0] * ref.shape[1])) <= 0.05

    def _get_history(self, sct):
        res = []
        for c in CELLS:
            x, y, w, h = c
            img = np.array(sct.grab({"left": x, "top": y, "width": w, "height": h}))
            bgr = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
            avg = cv2.mean(bgr)[:3]
            b, g, r = map(int, avg)
            min_d, best = float('inf'), "Unknown"
            for name, (tb, tg, tr) in COLOR_TARGETS.items():
                d = (b - tb) ** 2 + (g - tg) ** 2 + (r - tr) ** 2
                if d < min_d:
                    min_d, best = d, name
            res.append(best if min_d < 8000 else "Empty")
        return res

    def _format_balance_change(self, old_bal, new_bal, is_win=False):
        """Форматирует изменение баланса: 100$ -10$ = 90$"""
        change = new_bal - old_bal
        sign = "+" if change >= 0 else ""
        return f"{Colors.RED}{old_bal:.0f}${sign}{change:.0f}$ = {new_bal:.0f}${Colors.RESET}"

    def tick(self):
        if self.is_busy:
            return
        try:
            with mss.mss() as sct:
                if not self._check_ui(sct):
                    return

                current_id = self._get_id(sct)
                if current_id is None or current_id == self.last_game_id:
                    return

                history = self._get_history(sct)
                clean_hist = [x for x in history if x != "Empty"]

                # 🔴 last_result = результат ПРЕДЫДУЩЕЙ игры
                last_result = clean_hist[0] if clean_hist else "Unknown"
                previous_game_id = current_id - 1

                # 🎨 ЗАГОЛОВОК (зелёный - номера игр)
                self.logger.info(f"\n{Colors.BOLD}{'=' * 70}{Colors.RESET}")
                self.logger.info(
                    f"{Colors.GREEN}🎮 ИГРА #{current_id} | "
                    f"Завершена игра #{previous_game_id}: {last_result}{Colors.RESET}"
                )
                self.logger.info(f"{Colors.BOLD}{'=' * 70}{Colors.RESET}")

                # ==========================================
                # 1️⃣ ОБРАБОТКА РЕЗУЛЬТАТА ПРЕДЫДУЩЕЙ ИГРЫ
                # ==========================================
                if last_result != "Unknown":
                    # Обновляем ИИ (всегда)
                    self.ai.add_result(last_result)

                    # 🔑 Обновляем ОТДЕЛЬНЫЙ счётчик (не ai.history!)
                    if last_result == "10X":
                        self.streak_no_10x = 0  # ✅ СБРОС при 10X!
                    else:
                        self.streak_no_10x += 1

                # ==========================================
                # 2️⃣ ПРОГНОЗ ИИ (оранжевый цвет)
                # ==========================================
                ai_pred, ai_conf, ai_probs, ev_dict = self.ai.predict()

                if ai_pred and ev_dict:
                    sorted_ev = sorted(ev_dict.items(), key=lambda x: x[1], reverse=True)

                    self.logger.info(f"\n{Colors.ORANGE}🤖 AI ПРОГНОЗ для игры #{current_id}:{Colors.RESET}")
                    self.logger.info(
                        f"{Colors.ORANGE}   🎯 Лучший выбор: {ai_pred} (EV: {ai_conf:.2f}){Colors.RESET}"
                    )

                    for i, (label, ev) in enumerate(sorted_ev[:3], 1):
                        prob = ai_probs.get(label, 0)
                        bar = "█" * int(ev * 3)
                        self.logger.info(
                            f"{Colors.ORANGE}   {i}. {label}: EV={ev:.2f} [{bar}] (вероятность: {prob:.1f}%){Colors.RESET}"
                        )

                    if self.ai.total_preds > 0:
                        accuracy = (self.ai.correct_preds / self.ai.total_preds * 100)
                        self.logger.info(
                            f"{Colors.ORANGE}   📈 Точность: {accuracy:.1f}% ({self.ai.correct_preds}/{self.ai.total_preds}){Colors.RESET}"
                        )

                    # 🎲 ВИРТУАЛЬНЫЕ СТАВКИ ИИ
                    if ai_conf >= AI_MIN_EV_TO_BET and not self.betting.active:
                        bet_amount = min(AI_MAX_VIRTUAL_BET, max(10, int(ai_conf * AI_BASE_BET)))
                        old_bal = self.ai.virtual_balance

                        if self.ai.place_virtual_bet(bet_amount, ai_pred):
                            reward, win_info = self.ai.process_virtual_result(last_result)

                            if win_info:
                                self.logger.info(
                                    f"{Colors.ORANGE}   🎮 ИИ ставка: ${bet_amount} на {ai_pred} → {win_info} "
                                    f"(Баланс: ${old_bal:.0f} → ${self.ai.virtual_balance:.0f}){Colors.RESET}"
                                )

                # ==========================================
                # 3️⃣ ВАША СТРАТЕГИЯ 10X (красный цвет)
                # ==========================================
                action_taken = False

                if self.betting.active:
                    if last_result == "10X":
                        # ✅ ПОБЕДА!
                        won, profit = self.betting.process_result(True)
                        old_bal = self.ai.real_balance
                        profit_info, _ = self.ai.process_real_result(last_result)

                        self.logger.info(
                            f"\n{Colors.RED}💰🎉 ПОБЕДА! 10X выпал в игре #{previous_game_id}!{Colors.RESET}")
                        self.logger.info(
                            f"{Colors.RED}   💵 Выигрыш: ${profit:.0f} | "
                            f"Баланс: {self._format_balance_change(old_bal, self.ai.real_balance)}{Colors.RESET}"
                        )
                        self.logger.info(
                            f"{Colors.RED}   📊 Статистика: Потрачено ${self.betting.total_spent:.0f} | "
                            f"Выиграно ${self.betting.total_won:.0f} | "
                            f"Чистая прибыль: ${self.betting.total_won - self.betting.total_spent:.0f}{Colors.RESET}"
                        )
                        self.logger.info(
                            f"{Colors.RED}   🔄 Стратегия сброшена, ждём новые {TRIGGER_THRESHOLD} игр...{Colors.RESET}")
                        action_taken = True
                    else:
                        # ❌ Проигрыш раунда
                        _, loss = self.betting.process_result(False)
                        old_bal = self.ai.real_balance
                        _, loss_info = self.ai.process_real_result(last_result)

                        self.logger.info(
                            f"\n{Colors.RED}❌ Не 10X в игре #{previous_game_id} (выпало {last_result}){Colors.RESET}")
                        self.logger.info(
                            f"{Colors.RED}   📉 Попытка {self.betting.attempts}/{MAX_ATTEMPTS_PER_LEVEL} | "
                            f"Уровень {self.betting.level + 1} (ставка ${self.betting.amounts[self.betting.level]}){Colors.RESET}"
                        )
                        self.logger.info(
                            f"{Colors.RED}   💳 Баланс: {self._format_balance_change(old_bal, self.ai.real_balance)}{Colors.RESET}"
                        )

                        if self.betting.attempts == 0 and self.betting.level > 0:
                            self.logger.info(
                                f"{Colors.RED}   ⬆️ ПОВЫШЕНИЕ УРОВНЯ! Новая ставка: ${self.betting.amounts[self.betting.level]}{Colors.RESET}"
                            )

                # 🔥 АКТИВАЦИЯ СТРАТЕГИИ (при достижении порога)
                if not self.betting.active and not action_taken:
                    if self.streak_no_10x >= TRIGGER_THRESHOLD:
                        self.betting.activate()
                        self.logger.info(f"\n{Colors.RED}🔥 ТРИГГЕР АКТИВИРОВАН! 🔥🔥🔥{Colors.RESET}")
                        self.logger.info(
                            f"{Colors.RED}   📊 10X не было {self.streak_no_10x} игр подряд (порог: {TRIGGER_THRESHOLD}){Colors.RESET}"
                        )
                        self.logger.info(f"{Colors.RED}   💰 Начинаю серию ставок на 10X{Colors.RESET}")
                        self.logger.info(
                            f"{Colors.RED}   📈 Уровни: ${BET_LEVELS[0]} ({MAX_ATTEMPTS_PER_LEVEL}x) → "
                            f"${BET_LEVELS[1]} ({MAX_ATTEMPTS_PER_LEVEL}x) → ${BET_LEVELS[2]} (∞){Colors.RESET}"
                        )
                        action_taken = True

                # 💸 РАЗМЕЩЕНИЕ СТАВКИ
                if self.betting.active and not action_taken:
                    old_bal = self.ai.real_balance
                    if self.betting.place(lambda x, y: (
                            win32api.SetCursorPos((x, y)),
                            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, x, y, 0, 0),
                            time.sleep(0.05),
                            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, x, y, 0, 0)
                    )[0]):
                        self.ai.place_real_bet(self.betting.amounts[self.betting.level])

                        self.logger.info(f"\n{Colors.RED}💸 СТАВКА РАЗМЕЩЕНА на игру #{current_id}!{Colors.RESET}")
                        self.logger.info(f"{Colors.RED}   🎯 Цель: 10X{Colors.RESET}")
                        self.logger.info(
                            f"{Colors.RED}   💵 Сумма: ${self.betting.amounts[self.betting.level]}{Colors.RESET}")
                        self.logger.info(
                            f"{Colors.RED}   💳 Баланс: {self._format_balance_change(old_bal, self.ai.real_balance)}{Colors.RESET}"
                        )
                        action_taken = True

                # ==========================================
                # 📊 ИТОГОВАЯ СТАТИСТИКА
                # ==========================================
                bar_length = 30
                filled = int((self.streak_no_10x / TRIGGER_THRESHOLD) * bar_length)
                filled = min(filled, bar_length)
                bar = f"{Colors.GREEN}{'█' * filled}{Colors.RESET}{Colors.YELLOW}{'░' * (bar_length - filled)}{Colors.RESET}"
                percentage = min(100, (self.streak_no_10x / TRIGGER_THRESHOLD) * 100)

                status_color = Colors.RED if self.betting.active else Colors.BLUE
                status_text = "💰 АКТИВНЫЕ СТАВКИ" if self.betting.active else "⏳ НАБЛЮДЕНИЕ"

                self.logger.info(f"\n{Colors.BOLD}📊 СТАТИСТИКА:{Colors.RESET}")
                self.logger.info(
                    f"   📈 Прогресс до ставок: {Colors.BOLD}{self.streak_no_10x}/{TRIGGER_THRESHOLD}{Colors.RESET} ({percentage:.1f}%)"
                )
                self.logger.info(f"   [{bar}]")
                self.logger.info(
                    f"   {Colors.RED}💳 Реальный баланс: ${self.ai.real_balance:.0f}{Colors.RESET} | "
                    f"{Colors.ORANGE}🎮 Виртуальный: ${self.ai.virtual_balance:.0f}{Colors.RESET}"
                )
                self.logger.info(f"   {status_color}🎯 Статус: {status_text}{Colors.RESET}")
                self.logger.info(f"{Colors.BOLD}{'=' * 70}{Colors.RESET}\n")

                # Сохранение
                self.last_game_id = current_id
                self.save_state()

        except Exception as e:
            self.logger.error(f"{Colors.RED}❌ Ошибка тика: {e}{Colors.RESET}")
            import traceback
            self.logger.error(traceback.format_exc())

    def run(self):
        while True:
            if keyboard.is_pressed('f4'):
                if not self.running:
                    if not os.path.exists(self.name_check):
                        self._save_ui_ref()
                        self.logger.info(f"{Colors.YELLOW}📸 Эталон интерфейса создан{Colors.RESET}")
                self.running = not self.running
                status = f"{Colors.GREEN}▶ ЗАПУСК{Colors.RESET}" if self.running else f"{Colors.YELLOW}⏸ ПАУЗА{Colors.RESET}"
                self.logger.info(f"\n🔴 {status}\n")
                time.sleep(0.5)

            if keyboard.is_pressed('1') and keyboard.is_pressed('2'):
                self.logger.info(f"\n{Colors.YELLOW}💾 Сохранение и выход...{Colors.RESET}")
                self.save_state()
                self.survival.stop()
                break

            if keyboard.is_pressed('1') and keyboard.is_pressed('3'):
                root = Tk()
                root.withdraw()
                f = filedialog.askdirectory(initialdir=self.folder)
                if f:
                    self.folder = f
                    self.load_state()
                    self.logger.info(f"{Colors.GREEN}✅ Папка обновлена: {f}{Colors.RESET}")
                time.sleep(0.5)

            if not self.running:
                time.sleep(0.1)
                continue
            if self.is_busy:
                time.sleep(0.5)
                continue

            self.tick()
            time.sleep(0.5)


if __name__ == "__main__":
    print(f"{Colors.BOLD}🚀 Запуск Roulette Bot v4.0 (Colors + Fixed){Colors.RESET}")
    root = Tk()
    root.withdraw()
    folder = filedialog.askdirectory(title="📁 Папка для логов и памяти")
    if folder:
        os.makedirs(folder, exist_ok=True)
        RouletteBot(folder).run()
    else:
        print(f"{Colors.RED}❌ Отмена.{Colors.RESET}")

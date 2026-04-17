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


# ANSI цвета для консоли
class Colors:
    GREEN = '\033[92m'
    ORANGE = '\033[38;5;208m'
    RED = '\033[91m'
    BLUE = '\033[94m'
    YELLOW = '\033[93m'
    CYAN = '\033[96m'
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
BET_2X_BUTTON_CENTER = (429, 982)

# 🔧 СТРАТЕГИЯ 10X
TRIGGER_THRESHOLD = 20
MAX_ATTEMPTS_PER_LEVEL = 10
BET_LEVELS = [10, 20, 40, 80, 160, 320, 640, 1280, 2560, 5120]

# 🔧 СТРАТЕГИЯ 2X
BET_2X_LEVELS = [10, 10, 20, 40, 80, 160, 320, 640, 0, 10, 20, 1280, 0]
TRIGGER_2X_THRESHOLD = 0  # 0 = всегда ставит

# 🔧 ИИ
AI_MIN_EV_TO_BET = 1.2
AI_MAX_VIRTUAL_BET = 1000
AI_BASE_BET = 50

# 🔧 ТАЙМИНГИ ПИТАНИЯ
EAT_INTERVAL = 1250
EAT_KEY_DELAY = 3
EAT_CLICK_DELAY = 3
EAT_RETURN_DELAY = 3


# ==========================================
# 🧠 ИИ-МОЗГ
# ==========================================
class AIBrain(nn.Module):
    def __init__(self):
        super().__init__()
        self.embedding = nn.Embedding(4, 16)
        self.lstm = nn.LSTM(16, 32, batch_first=True)
        self.fc = nn.Linear(32, 4)
        self.opt = torch.optim.Adam(self.parameters(), lr=0.001)

        self.history = []
        self.virtual_balance = 1000.0
        self.virtual_bet_amount = 0
        self.virtual_target = None
        self.virtual_total_bets = 0
        self.virtual_wins = 0

        self.real_balance = 200.0
        self.total_preds = 0
        self.correct_preds = 0

    def add_result(self, label):
        idx = LABEL_MAP.get(label)
        if idx is None: return
        self.history.append(idx)
        if len(self.history) >= 9:
            self._train()

    def _train(self):
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
        if len(self.history) < 8:
            return None, 0.0, {}, {}

        X = torch.LongTensor([self.history[-8:]])
        with torch.no_grad():
            emb = self.embedding(X)
            out, _ = self.lstm(emb)
            logits = self.fc(out[:, -1, :])
            probs = F.softmax(logits, dim=1)[0]

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
        if target not in LABEL_MAP or amount <= 0: return False
        self.virtual_bet_amount = amount
        self.virtual_target = target
        self.virtual_balance -= amount
        self.virtual_total_bets += 1
        return True

    def process_virtual_result(self, actual_label):
        if self.virtual_target is None: return 0, None
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

    def save(self, path):
        torch.save({
            'state': self.state_dict(), 'opt': self.opt.state_dict(), 'hist': self.history,
            'virtual_balance': self.virtual_balance, 'virtual_bets': self.virtual_total_bets,
            'virtual_wins': self.virtual_wins, 'real_balance': self.real_balance,
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
# 💰 СИСТЕМА СТАВОК 10X
# ==========================================
class BettingSystem:
    def __init__(self, brain):
        self.brain = brain
        self.active = False
        self.level = 0
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
        if self.last_bet_round is not None: return False
        # 🔒 Защита от выхода за границы
        if self.level >= len(self.amounts):
            self.deactivate()
            return False
        amount = self.amounts[self.level]
        click_func(*INPUT_FIELD_CENTER)
        time.sleep(0.4)
        keyboard.press_and_release('ctrl+a')
        time.sleep(0.1)
        keyboard.write(str(amount))
        time.sleep(0.4)
        click_func(*BET_BUTTON_CENTER)
        time.sleep(0.4)
        self.last_bet_round = True
        self.total_spent += amount
        self.brain.real_balance -= amount
        return True

    def process_result(self, was_10x):
        self.last_bet_round = None
        if was_10x:
            win_amount = self.amounts[self.level] * 10
            self.total_won += win_amount
            profit = win_amount - self.amounts[self.level]
            self.brain.real_balance += win_amount
            self.deactivate()
            return True, profit
        self.attempts += 1
        if self.attempts >= self.max_attempts and self.level < len(self.amounts) - 1:
            self.level += 1
            self.attempts = 0
        # 🔒 Финальная проверка
        if self.level >= len(self.amounts):
            self.deactivate()
            return False, 0
        return False, -self.amounts[self.level]


# ==========================================
# 💰 СИСТЕМА СТАВОК 2X
# ==========================================
class BettingSystem2X:
    def __init__(self, brain):
        self.brain = brain
        self.active = False
        self.paused = False
        self.step = 0
        self.amounts = BET_2X_LEVELS
        self.last_bet_round = None
        self.total_spent = 0
        self.total_won = 0
        self.yield_to_10x = False

    def activate(self):
        self.active = True
        self.paused = False
        self.step = 0
        self.last_bet_round = None
        self.yield_to_10x = False

    def deactivate(self):
        self.active = False
        self.paused = False
        self.step = 0
        self.last_bet_round = None
        self.yield_to_10x = False

    def pause(self):
        self.paused = True
        self.last_bet_round = None

    def resume(self):
        if self.active:
            self.paused = False
            self.yield_to_10x = False

    def place(self, click_func):
        if self.last_bet_round is not None or self.paused: return False
        # 🔒 Защита от выхода за границы
        if self.step >= len(self.amounts):
            self.deactivate()
            return False
        amount = self.amounts[self.step]
        click_func(*INPUT_FIELD_CENTER)
        time.sleep(0.4)
        keyboard.press_and_release('ctrl+a')
        time.sleep(0.1)
        keyboard.write(str(amount))
        time.sleep(0.4)
        click_func(*BET_2X_BUTTON_CENTER)
        time.sleep(0.4)
        self.last_bet_round = True
        self.total_spent += amount
        self.brain.real_balance -= amount
        return True

    def process_result(self, was_win):
        self.last_bet_round = None
        if was_win:
            # 🔒 Проверка границ перед доступом
            if self.step >= len(self.amounts):
                self.deactivate()
                return False, 0
            win_amount = self.amounts[self.step] * 2
            self.total_won += win_amount
            profit = win_amount - self.amounts[self.step]
            self.brain.real_balance += win_amount

            if self.yield_to_10x:
                self.pause()
            else:
                self.deactivate()
            return True, profit
        else:
            if self.step < len(self.amounts) - 1:
                self.step += 1
            # 🔒 Финальная защита
            if self.step >= len(self.amounts):
                self.deactivate()
                return False, 0
            return False, -self.amounts[self.step]


# ==========================================
# 🛡️ МЕНЕДЖЕР ВЫЖИВАНИЯ
# ==========================================
class SurvivalManager:
    def __init__(self, bot):
        self.bot = bot
        self.next_run = time.time() + EAT_INTERVAL
        self.active = False
        self.thread = None
        self.eat_requested = False

    def start(self):
        self.active = True
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self.active = False

    def is_eat_requested(self):
        return self.eat_requested

    def reset_eat_flag(self):
        self.eat_requested = False
        self.next_run = time.time() + EAT_INTERVAL

    def _press(self, vk, sc):
        win32api.keybd_event(vk, sc, 0, 0)
        time.sleep(EAT_KEY_DELAY)
        win32api.keybd_event(vk, sc, win32con.KEYEVENTF_KEYUP, 0)

    def _click(self, x, y):
        win32api.SetCursorPos((x, y))
        time.sleep(0.1)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, x, y, 0, 0)
        time.sleep(EAT_CLICK_DELAY)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, x, y, 0, 0)
        time.sleep(EAT_CLICK_DELAY)

    def _loop(self):
        while self.active:
            time.sleep(0.5)
            if not self.eat_requested and time.time() >= self.next_run:
                self.eat_requested = True
                self.bot.logger.info(f"{Colors.YELLOW}🍽️ Запрошено питание... Ожидаем завершения ставок{Colors.RESET}")
            if self.eat_requested:
                if self.bot.betting.active or self.bot.betting_2x.active:
                    continue
                self._execute()
                self.reset_eat_flag()

    def _execute(self):
        self.bot.logger.info(f"\n{Colors.YELLOW}🍔 ПЕРСОНАЖ ГОЛОДЕН! Начинаем процедуру...{Colors.RESET}")
        self.bot.is_busy = True
        try:
            self._press(0x71, 0x3C)
            time.sleep(EAT_RETURN_DELAY)
            self._press(0xC0, 0x29)
            time.sleep(EAT_KEY_DELAY)
            self._click(1798, 951)
            time.sleep(EAT_CLICK_DELAY)
            self._click(1801, 999)
            time.sleep(EAT_CLICK_DELAY)
            self._press(0xC0, 0x29)
            time.sleep(EAT_KEY_DELAY)
            self._press(0x71, 0x3C)
            time.sleep(EAT_RETURN_DELAY)
            self._click(147, 359)
            time.sleep(EAT_RETURN_DELAY)
            self._click(158, 499)
            time.sleep(EAT_RETURN_DELAY * 2)
            time.sleep(1.0)
            self.bot.logger.info(f"{Colors.GREEN}✅ Питание завершено. Возврат в игру.{Colors.RESET}\n")
        except Exception as e:
            self.bot.logger.error(f"{Colors.RED}❌ Ошибка процедуры питания: {e}{Colors.RESET}")
            import traceback
            self.bot.logger.error(traceback.format_exc())
        finally:
            self.bot.is_busy = False


# ==========================================
# 🎨 Логгер
# ==========================================
class ColoredFormatter(logging.Formatter):
    LEVEL_COLORS = {'INFO': Colors.GREEN, 'WARNING': Colors.YELLOW, 'ERROR': Colors.RED}

    def format(self, record):
        original = super().format(record)
        if hasattr(record, 'color') and record.color:
            color = self.LEVEL_COLORS.get(record.levelname, Colors.RESET)
            return f"{color}{original}{Colors.RESET}"
        return original


# ==========================================
# 🌐 ГЛОБАЛЬНАЯ ПЕРЕМЕННАЯ
# ==========================================
bot_instance = None


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

        self.logger = logging.getLogger("Bot")
        self.logger.setLevel(logging.INFO)
        self.logger.handlers = []

        file_handler = logging.FileHandler(self.log_file, encoding='utf-8')
        file_handler.setFormatter(logging.Formatter('%(asctime)s | %(message)s'))
        self.logger.addHandler(file_handler)

        console_handler = logging.StreamHandler()
        console_handler.setFormatter(ColoredFormatter('%(asctime)s | %(message)s'))
        self.logger.addHandler(console_handler)

        if not os.path.exists(self.csv_file):
            with open(self.csv_file, 'w', newline='', encoding='utf-8') as f:
                csv.writer(f).writerow([
                    "Time", "CurrentID", "PreviousID", "Result",
                    "AI_Pred", "AI_EV", "VirtualBal", "RealBal",
                    "Bet10X_Active", "Bet2X_Active", "Win", "StreakNo10X", "StreakNo2X"
                ])

        self.ai = AIBrain()
        self.betting = BettingSystem(self.ai)
        self.betting_2x = BettingSystem2X(self.ai)
        self.survival = SurvivalManager(self)
        self.survival.start()

        self.last_game_id = 0
        self.is_busy = False
        self.running = False
        self.streak_no_10x = 0
        self.streak_no_2x = 0

        self.load_state()
        self._log_header()

        global bot_instance
        bot_instance = self

    def _log_header(self):
        self.logger.info(f"\n{Colors.BOLD}{'=' * 70}{Colors.RESET}")
        self.logger.info(f"{Colors.BOLD}🤖 ROULETTE BOT v4.4 - AI + Eat Timer 30s{Colors.RESET}")
        self.logger.info(f"{Colors.BOLD}{'=' * 70}{Colors.RESET}")
        self.logger.info(f"{Colors.GREEN}🎮 Текущая игра: #{self.last_game_id}{Colors.RESET}")
        self.logger.info(f"{Colors.RED}💰 Реальный баланс: ${self.ai.real_balance:.0f}{Colors.RESET}")
        self.logger.info(f"{Colors.ORANGE}🧠 Виртуальный баланс ИИ: ${self.ai.virtual_balance:.0f}{Colors.RESET}")
        self.logger.info(f"{Colors.RED}📊 10X: {TRIGGER_THRESHOLD} игр без → ${BET_LEVELS}{Colors.RESET}")
        self.logger.info(f"{Colors.CYAN}🔷 2X: {TRIGGER_2X_THRESHOLD} игр без → {BET_2X_LEVELS}{Colors.RESET}")
        self.logger.info(f"{Colors.YELLOW}🍽️ Питание: каждые {EAT_INTERVAL} сек (при отсутствии ставок){Colors.RESET}")
        self.logger.info(f"{Colors.BOLD}{'=' * 70}{Colors.RESET}")
        self.logger.info(f"\n{Colors.YELLOW}F4 - Старт/Стоп | 1+2 - Выход | 1+3 - Сменить папку{Colors.RESET}\n")

    def _save_ui_ref(self):
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
                'last_id': self.last_game_id, 'bet10x_active': self.betting.active,
                'bet10x_level': self.betting.level, 'bet10x_att': self.betting.attempts,
                'bet2x_active': self.betting_2x.active, 'bet2x_paused': self.betting_2x.paused,
                'bet2x_step': self.betting_2x.step, 'bet2x_yield': self.betting_2x.yield_to_10x,
                'streak10x': self.streak_no_10x, 'streak2x': self.streak_no_2x,
                'ai_bal': self.ai.real_balance, 'virt_bal': self.ai.virtual_balance,
                'bet10x_spent': self.betting.total_spent, 'bet10x_won': self.betting.total_won,
                'bet2x_spent': self.betting_2x.total_spent, 'bet2x_won': self.betting_2x.total_won
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
                self.streak_no_10x = state.get('streak10x', 0)
                self.streak_no_2x = state.get('streak2x', 0)

                if state.get('bet10x_active', False):
                    self.betting.activate()
                    loaded_level = state.get('bet10x_level', 0)
                    if 0 <= loaded_level < len(BET_LEVELS):
                        self.betting.level = loaded_level
                    else:
                        self.betting.level = 0
                        self.logger.warning(f"{Colors.YELLOW}⚠️ Сброшен некорректный уровень 10X: {loaded_level} → 0{Colors.RESET}")
                    self.betting.attempts = state.get('bet10x_att', 0)
                self.betting.total_spent = state.get('bet10x_spent', 0)
                self.betting.total_won = state.get('bet10x_won', 0)

                if state.get('bet2x_active', False):
                    self.betting_2x.activate()
                    self.betting_2x.paused = state.get('bet2x_paused', False)
                    loaded_step = state.get('bet2x_step', 0)
                    if 0 <= loaded_step < len(BET_2X_LEVELS):
                        self.betting_2x.step = loaded_step
                    else:
                        self.betting_2x.step = 0
                        self.logger.warning(f"{Colors.YELLOW}⚠️ Сброшен некорректный шаг 2X: {loaded_step} → 0{Colors.RESET}")
                    self.betting_2x.yield_to_10x = state.get('bet2x_yield', False)
                self.betting_2x.total_spent = state.get('bet2x_spent', 0)
                self.betting_2x.total_won = state.get('bet2x_won', 0)

                if 'ai_bal' in state: self.ai.real_balance = state['ai_bal']
                if 'virt_bal' in state: self.ai.virtual_balance = state['virt_bal']
            except:
                pass
        if not self.ai.load(self.ai_file):
            self.logger.warning(f"{Colors.YELLOW}⚠️ ИИ не загружен, начинаю с нуля{Colors.RESET}")
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
        if not os.path.exists(self.name_check): return True
        ref = cv2.imread(self.name_check)
        if ref is None or ref.shape != bgr.shape: return False
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
                if d < min_d: min_d, best = d, name
            res.append(best if min_d < 8000 else "Empty")
        return res

    def _format_balance_change(self, old_bal, new_bal):
        change = new_bal - old_bal
        sign = "+" if change >= 0 else ""
        return f"{Colors.RED}{old_bal:.0f}${sign}{change:.0f}$ = {new_bal:.0f}${Colors.RESET}"

    def tick(self):
        if self.survival.is_eat_requested():
            if not self.betting.active and not self.betting_2x.active:
                return
        if self.is_busy: return
        try:
            with mss.mss() as sct:
                if not self._check_ui(sct): return
                current_id = self._get_id(sct)
                if current_id is None or current_id == self.last_game_id: return

                history = self._get_history(sct)
                clean_hist = [x for x in history if x != "Empty"]
                last_result = clean_hist[0] if clean_hist else "Unknown"
                previous_game_id = current_id - 1

                self.logger.info(f"\n{Colors.BOLD}{'=' * 70}{Colors.RESET}")
                self.logger.info(
                    f"{Colors.GREEN}🎮 ИГРА #{current_id} | Завершена #{previous_game_id}: {last_result}{Colors.RESET}")
                self.logger.info(f"{Colors.BOLD}{'=' * 70}{Colors.RESET}")

                if last_result != "Unknown":
                    self.ai.add_result(last_result)
                    self.streak_no_10x = 0 if last_result == "10X" else self.streak_no_10x + 1
                    self.streak_no_2x = 0 if last_result == "2X" else self.streak_no_2x + 1

                ai_pred, ai_conf, ai_probs, ev_dict = self.ai.predict()
                if ai_pred and ev_dict:
                    sorted_ev = sorted(ev_dict.items(), key=lambda x: x[1], reverse=True)
                    self.logger.info(f"\n{Colors.ORANGE}🤖 AI ПРОГНОЗ для игры #{current_id}:{Colors.RESET}")
                    self.logger.info(f"{Colors.ORANGE}   🎯 Лучший: {ai_pred} (EV: {ai_conf:.2f}){Colors.RESET}")
                    for i, (label, ev) in enumerate(sorted_ev[:3], 1):
                        prob = ai_probs.get(label, 0)
                        bar = "█" * int(ev * 3)
                        self.logger.info(
                            f"{Colors.ORANGE}   {i}. {label}: EV={ev:.2f} [{bar}] ({prob:.1f}%){Colors.RESET}")
                    if ai_conf >= AI_MIN_EV_TO_BET and not self.betting.active and not self.betting_2x.active:
                        bet_amt = min(AI_MAX_VIRTUAL_BET, max(10, int(ai_conf * AI_BASE_BET)))
                        old_bal = self.ai.virtual_balance
                        if self.ai.place_virtual_bet(bet_amt, ai_pred):
                            _, win_info = self.ai.process_virtual_result(last_result)
                            if win_info:
                                self.logger.info(
                                    f"{Colors.ORANGE}   🎮 ИИ: ${bet_amt} на {ai_pred} → {win_info} (${old_bal:.0f}→${self.ai.virtual_balance:.0f}){Colors.RESET}")

                action_taken = False
                old_bal = self.ai.real_balance
                eat_pending = self.survival.is_eat_requested()

                if self.betting.active:
                    if last_result == "10X":
                        _, profit = self.betting.process_result(True)
                        self.betting_2x.yield_to_10x = False
                        if self.betting_2x.paused:
                            self.betting_2x.resume()
                            self.logger.info(f"{Colors.CYAN}🔓 10X завершён → 2X ВОЗОБНОВЛЁН ▶{Colors.RESET}")
                        self.logger.info(f"\n{Colors.RED}💰🎉 10X ПОБЕДА! #{previous_game_id}{Colors.RESET}")
                        self.logger.info(
                            f"{Colors.RED}   💵 +${profit:.0f} | Баланс: {self._format_balance_change(old_bal, self.ai.real_balance)}{Colors.RESET}")
                        self.logger.info(
                            f"{Colors.RED}   📊 Потрачено: ${self.betting.total_spent:.0f} | Выиграно: ${self.betting.total_won:.0f}{Colors.RESET}")
                        action_taken = True
                    else:
                        _, loss = self.betting.process_result(False)
                        self.logger.info(f"\n{Colors.RED}❌ Не 10X в #{previous_game_id} ({last_result}){Colors.RESET}")
                        self.logger.info(
                            f"{Colors.RED}   📉 Попытка {self.betting.attempts}/{MAX_ATTEMPTS_PER_LEVEL} | Ур. {self.betting.level + 1} (${self.betting.amounts[self.betting.level] if self.betting.level < len(self.betting.amounts) else 0}){Colors.RESET}")
                        self.logger.info(
                            f"{Colors.RED}   💳 Баланс: {self._format_balance_change(old_bal, self.ai.real_balance)}{Colors.RESET}")

                if not eat_pending and not self.betting.active and not action_taken and self.streak_no_10x >= TRIGGER_THRESHOLD:
                    self.betting.activate()
                    if self.betting_2x.active and not self.betting_2x.paused:
                        self.betting_2x.yield_to_10x = True
                        self.logger.info(
                            f"{Colors.YELLOW}⚠️ 10X активирован. 2X продолжит до выигрыша, затем уступит.{Colors.RESET}")
                    self.logger.info(
                        f"\n{Colors.RED}🔥 ТРИГГЕР 10X! ({self.streak_no_10x}/{TRIGGER_THRESHOLD}){Colors.RESET}")
                    action_taken = True

                if self.betting_2x.active and not self.betting_2x.paused:
                    if last_result == "2X":
                        _, profit = self.betting_2x.process_result(True)
                        if self.betting_2x.paused:
                            self.logger.info(f"\n{Colors.CYAN}💙🎉 2X ВЫИГРАЛ! → ПАУЗА (уступаем 10X){Colors.RESET}")
                        else:
                            self.logger.info(f"\n{Colors.CYAN}💙🎉 2X ВЫИГРАЛ! → СТРАТЕГИЯ ЗАВЕРШЕНА{Colors.RESET}")
                        self.logger.info(
                            f"{Colors.CYAN}   💵 +${profit:.0f} | Баланс: {self._format_balance_change(old_bal, self.ai.real_balance)}{Colors.RESET}")
                        action_taken = True
                    else:
                        _, loss = self.betting_2x.process_result(False)
                        self.logger.info(f"\n{Colors.CYAN}❌ Не 2X в #{previous_game_id} ({last_result}){Colors.RESET}")
                        self.logger.info(
                            f"{Colors.CYAN}   📉 Шаг {self.betting_2x.step + 1}/{len(BET_2X_LEVELS)} | Ставка ${self.betting_2x.amounts[self.betting_2x.step] if self.betting_2x.step < len(self.betting_2x.amounts) else 0}{Colors.RESET}")
                        self.logger.info(
                            f"{Colors.CYAN}   💳 Баланс: {self._format_balance_change(old_bal, self.ai.real_balance)}{Colors.RESET}")

                if not eat_pending and not self.betting_2x.active:
                    if TRIGGER_2X_THRESHOLD == 0:
                        self.betting_2x.activate()
                    elif self.streak_no_2x >= TRIGGER_2X_THRESHOLD:
                        self.betting_2x.activate()
                        self.logger.info(
                            f"\n{Colors.CYAN}🔷 ТРИГГЕР 2X! ({self.streak_no_2x}/{TRIGGER_2X_THRESHOLD}){Colors.RESET}")
                        action_taken = True
                    elif self.betting_2x.paused and not self.betting.active:
                        self.betting_2x.resume()
                        self.logger.info(
                            f"\n{Colors.CYAN}🔷 10X не активен → 2X ВОЗОБНОВЛЁН ▶ (Шаг {self.betting_2x.step + 1}){Colors.RESET}")
                        action_taken = True

                click_func = lambda x, y: (
                    win32api.SetCursorPos((x, y)), win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, x, y, 0, 0),
                    time.sleep(0.05), win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, x, y, 0, 0)
                )[0]

                if self.betting.active:
                    if self.betting.place(click_func):
                        self.logger.info(
                            f"{Colors.RED}💸 СТАВКА 10X РАЗМЕЩЕНА | 💵 ${self.betting.amounts[self.betting.level] if self.betting.level < len(self.betting.amounts) else 0}{Colors.RESET}")

                if self.betting.active and self.betting_2x.active and not self.betting_2x.paused:
                    time.sleep(0.3)

                if self.betting_2x.active and not self.betting_2x.paused:
                    if self.betting_2x.place(click_func):
                        self.logger.info(
                            f"{Colors.CYAN}💸 СТАВКА 2X РАЗМЕЩЕНА | 💵 ${self.betting_2x.amounts[self.betting_2x.step] if self.betting_2x.step < len(self.betting_2x.amounts) else 0}{Colors.RESET}")

                thr_10 = TRIGGER_THRESHOLD if TRIGGER_THRESHOLD > 0 else 1
                thr_2x = TRIGGER_2X_THRESHOLD if TRIGGER_2X_THRESHOLD > 0 else 1

                bar_10x = f"{Colors.GREEN}{'█' * min(30, int((self.streak_no_10x / thr_10) * 30))}{Colors.RESET}{Colors.YELLOW}{'░' * max(0, 30 - int((self.streak_no_10x / thr_10) * 30))}{Colors.RESET}"
                bar_2x = f"{Colors.CYAN}{'█' * min(30, int((self.streak_no_2x / thr_2x) * 30))}{Colors.RESET}{Colors.YELLOW}{'░' * max(0, 30 - int((self.streak_no_2x / thr_2x) * 30))}{Colors.RESET}"

                s10 = f"{Colors.RED}💰 10X: АКТИВНЫ{Colors.RESET}" if self.betting.active else f"{Colors.BLUE}⏳ 10X: НАБЛЮДЕНИЕ{Colors.RESET}"
                s2x_status = "АКТИВНЫ"
                if self.betting_2x.paused:
                    s2x_status = "ПАУЗА (ждёт 10X)"
                elif not self.betting_2x.active:
                    s2x_status = "НАБЛЮДЕНИЕ"
                if self.betting_2x.yield_to_10x: s2x_status += " 🚩YIELD"
                s2x = f"{Colors.CYAN}🔷 2X: {s2x_status}{Colors.RESET}"
                eat_status = f"{Colors.YELLOW}🍽️ ОЖИДАНИЕ...{Colors.RESET}" if eat_pending else f"{Colors.GREEN}✓ ГОТОВ{Colors.RESET}"

                self.logger.info(f"\n{Colors.BOLD}📊 СТАТИСТИКА:{Colors.RESET}")
                self.logger.info(f"   🔴 10X: {Colors.BOLD}{self.streak_no_10x}/{TRIGGER_THRESHOLD}{Colors.RESET} [{bar_10x}]")
                self.logger.info(f"   🔵 2X:  {Colors.BOLD}{self.streak_no_2x}/{TRIGGER_2X_THRESHOLD}{Colors.RESET} [{bar_2x}]")
                self.logger.info(f"   {Colors.RED}💳 Реальный: ${self.ai.real_balance:.0f}{Colors.RESET} | {Colors.ORANGE}🎮 Виртуальный: ${self.ai.virtual_balance:.0f}{Colors.RESET}")
                self.logger.info(f"   {s10} | {s2x}")
                self.logger.info(f"   🍽️ Питание: {eat_status}")
                self.logger.info(f"{Colors.BOLD}{'=' * 70}{Colors.RESET}\n")

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
                root = Tk(); root.withdraw()
                f = filedialog.askdirectory(initialdir=self.folder)
                if f: self.folder = f; self.load_state(); self.logger.info(f"{Colors.GREEN}✅ Папка: {f}{Colors.RESET}")
                time.sleep(0.5)
            if not self.running or self.is_busy:
                time.sleep(0.1); continue
            self.tick()
            time.sleep(0.5)


if __name__ == "__main__":
    print(f"{Colors.BOLD}🚀 Запуск Roulette Bot v4.4 (Eat Timer 30s + Priority Yield){Colors.RESET}")
    root = Tk(); root.withdraw()
    folder = filedialog.askdirectory(title="📁 Папка для логов и памяти")
    if folder:
        os.makedirs(folder, exist_ok=True)
        RouletteBot(folder).run()
    else:
        print(f"{Colors.RED}❌ Отмена.{Colors.RESET}")

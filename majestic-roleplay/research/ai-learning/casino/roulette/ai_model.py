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

        self.history = []
        self.virtual_balance = 1000.0
        self.virtual_bet_amount = 10
        self.virtual_target = None
        self.virtual_total_bets = 0
        self.virtual_wins = 0
        self.real_balance = 200.0
        self.real_bet_amount = 10
        self.real_target = '10X'
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
        # ✅ Всегда возвращаем 4 значения
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
        
        return best, confidence, probs_dict, ev  # ✅ 4 значения

    def place_virtual_bet(self, amount, target):
        if target not in LABEL_MAP: return False
        self.virtual_bet_amount = amount
        self.virtual_target = target
        self.virtual_balance -= amount
        self.virtual_total_bets += 1
        return True

    def process_virtual_result(self, actual_label):
        if self.virtual_target is None: return 0
        reward = 0
        if actual_label == self.virtual_target:
            mult = MULTIPLIERS[actual_label]
            win_amount = self.virtual_bet_amount * mult
            self.virtual_balance += win_amount
            reward = win_amount - self.virtual_bet_amount
            self.virtual_wins += 1
        else:
            reward = -self.virtual_bet_amount
        self.virtual_target = None
        return reward

    def place_real_bet(self, amount):
        self.real_bet_amount = amount
        self.real_balance -= amount
        return True

    def process_real_result(self, actual_label):
        if actual_label == self.real_target:
            mult = MULTIPLIERS[actual_label]
            win_amount = self.real_bet_amount * mult
            self.real_balance += win_amount
            return win_amount - self.real_bet_amount
        return -self.real_bet_amount

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
# 💰 СИСТЕМА СТАВОК
# ==========================================
class BettingSystem:
    def __init__(self):
        self.active = False
        self.level = 0
        self.attempts = 0
        self.amounts = [10, 20, 40]
        self.max_attempts = 10
        self.last_bet_round = None

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
        if self.last_bet_round is not None: return
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

    def process_result(self, was_10x):
        self.last_bet_round = None
        if was_10x:
            self.deactivate()
            return True
        self.attempts += 1
        if self.attempts >= self.max_attempts and self.level < 2:
            self.level += 1
            self.attempts = 0
        return False


# ==========================================
# 🛡️ МЕНЕДЖЕР ВЫЖИВАНИЯ
# ==========================================
class SurvivalManager:
    def __init__(self, bot):
        self.bot = bot
        self.next_run = time.time() + 3600
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
            if self.bot.betting.active: continue
            if time.time() >= self.next_run:
                self._execute()
                self.next_run = time.time() + 3600

    def _execute(self):
        self.bot.logger.info("\n🍔 ПЕРСОНАЖ ГОЛОДЕН! Пауза...")
        self.bot.is_busy = True
        try:
            self._press(0x71, 0x3C); time.sleep(2)
            self._press(0xC0, 0x29); time.sleep(1)
            self._click(1798, 951); time.sleep(2)
            self._click(1801, 999); time.sleep(2)
            self._press(0xC0, 0x29); time.sleep(1)
            self._press(0x71, 0x3C); time.sleep(2)
            self._click(147, 359); time.sleep(2)
            self._click(158, 499); time.sleep(7)
            self.bot.logger.info("✅ Выживание завершено.\n")
        except Exception as e:
            self.bot.logger.error(f"❌ Ошибка выживания: {e}")
        finally:
            self.bot.is_busy = False


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

        logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(message)s',
                            handlers=[logging.FileHandler(self.log_file, encoding='utf-8'), logging.StreamHandler()])
        self.logger = logging.getLogger("Bot")

        if not os.path.exists(self.csv_file):
            with open(self.csv_file, 'w', newline='', encoding='utf-8') as f:
                csv.writer(f).writerow(
                    ["Time", "ID", "Result", "AI_Pred", "AI_EV", "VirtualBal", "RealBal", "BetActive", "Win"])

        self.ai = AIBrain()
        self.betting = BettingSystem()
        self.survival = SurvivalManager(self)
        self.survival.start()

        self.last_game_id = 0
        self.is_busy = False
        self.running = False

        self.load_state()
        self.logger.info("🤖 Бот запущен. F4 - Старт | 1+2 - Выход")
        self.logger.info(
            f"💰 Реальный баланс: ${self.ai.real_balance:.0f} | 🎮 Виртуальный: ${self.ai.virtual_balance:.0f}")

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
                'last_id': self.last_game_id,
                'bet_active': self.betting.active,
                'bet_level': self.betting.level,
                'bet_att': self.betting.attempts,
                'ai_bal': self.ai.real_balance,
                'virt_bal': self.ai.virtual_balance
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
                if state.get('bet_active', False):
                    self.betting.activate()
                    self.betting.level = state.get('bet_level', 0)
                    self.betting.attempts = state.get('bet_att', 0)
                if 'ai_bal' in state:
                    self.ai.real_balance = state['ai_bal']
                if 'virt_bal' in state:
                    self.ai.virtual_balance = state['virt_bal']
            except:
                pass
        if not self.ai.load(self.ai_file):
            self.logger.warning("⚠️ Не удалось загрузить ИИ, начинаю с нуля")
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

    def tick(self):
        if self.is_busy: return
        try:
            with mss.mss() as sct:
                if not self._check_ui(sct): return
                current_id = self._get_id(sct)
                if current_id is None or current_id == self.last_game_id: return

                history = self._get_history(sct)
                clean_hist = [x for x in history if x != "Empty"]
                last_result = clean_hist[0] if clean_hist else "Unknown"

                self.logger.info(f"\n🔄 Игра #{current_id} | Результат: {last_result}")

                # 1. Обучаем ИИ
                self.ai.add_result(last_result)

                # 2. Прогноз ИИ (теперь всегда 4 значения)
                ai_pred, ai_ev, ai_probs, ev_dict = self.ai.predict()

                if ai_pred:
                    self.ai.total_preds += 1
                    if ai_pred == last_result:
                        self.ai.correct_preds += 1
                    status = "✅" if ai_pred == last_result else "❌"
                    acc = (self.ai.correct_preds / self.ai.total_preds * 100) if self.ai.total_preds else 0
                    ev_str = " | ".join(
                        [f"{k}: EV={v:.2f}" for k, v in sorted(ev_dict.items(), key=lambda x: x[1], reverse=True)])
                    self.logger.info(f"🤖 AI: Лучший по EV: {ai_pred} (EV={ai_ev:.2f}) {status}")
                    self.logger.info(
                        f"📊 Вероятности: {' | '.join([f'{k}:{v:.1f}%' for k, v in sorted(ai_probs.items(), key=lambda x: x[1], reverse=True)])}")
                    self.logger.info(f"📈 Ожидаемая ценность: {ev_str}")
                    self.logger.info(f"🎯 Точность: {acc:.1f}% ({self.ai.correct_preds}/{self.ai.total_preds})")

                    if ai_ev > 1.5:
                        bet_amount = min(1000, max(10, int(ai_ev * 50)))
                        self.ai.place_virtual_bet(bet_amount, ai_pred)
                        self.logger.info(f"🎮 AI виртуальная ставка: ${bet_amount} на {ai_pred}")
                    if self.ai.virtual_target:
                        reward = self.ai.process_virtual_result(last_result)
                        if reward > 0:
                            self.logger.info(
                                f"💰 AI виртуальный выигрыш: +${reward:.0f} (Баланс: ${self.ai.virtual_balance:.0f})")
                        elif reward < 0:
                            self.logger.info(
                                f"📉 AI виртуальный проигрыш: ${reward:.0f} (Баланс: ${self.ai.virtual_balance:.0f})")

                # 3. Твоя тактика 10X
                just_won = False  # ✅ Флаг: только что выиграли?
                if self.betting.active:
                    if last_result == "10X":
                        won = self.betting.process_result(True)
                        if won:
                            profit = self.ai.process_real_result(last_result)
                            self.logger.info(
                                f"💰🎉 ТВОЙ ВЫИГРЫШ! +${profit:.0f} | Реальный баланс: ${self.ai.real_balance:.0f}")
                            just_won = True  # ✅ Запоминаем победу
                    else:
                        self.betting.process_result(False)
                        self.logger.info(
                            f"📉 Не 10X. Уровень: ${self.betting.amounts[self.betting.level]}, Попытка: {self.betting.attempts}")
                        self.ai.process_real_result(last_result)
                        self.logger.info(f"💸 Реальный баланс: ${self.ai.real_balance:.0f}")

                # ✅ Активация ТОЛЬКО если не выиграли в этом раунде
                if not self.betting.active and not just_won:
                    # Считаем ПОДРЯД идущие НЕ-10X от конца истории
                    no_10x = 0
                    for h in reversed(self.ai.history):
                        if h == 3:  # 10X
                            break
                        no_10x += 1
                    if no_10x >= 20:
                        self.betting.activate()
                        self.logger.info(f"🔥 ТРИГГЕР! 10X не было {no_10x} игр. Начинаю серию ставок!")

                # Размещение ставки
                if self.betting.active:
                    self.betting.place(lambda x, y: (
                        win32api.SetCursorPos((x, y)),
                        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, x, y, 0, 0),
                        time.sleep(0.05),
                        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, x, y, 0, 0)
                    )[0])
                    self.ai.place_real_bet(self.betting.amounts[self.betting.level])
                    self.logger.info(f"💰 Твоя реальная ставка: ${self.betting.amounts[self.betting.level]} на 10X")

                self.logger.info(
                    f"💰 БАЛАНСЫ: Реальный: ${self.ai.real_balance:.0f} | Виртуальный: ${self.ai.virtual_balance:.0f}")
                self.logger.info("─" * 60)

                self.last_game_id = current_id
                self.save_state()

        except Exception as e:
            self.logger.error(f"❌ Ошибка тика: {e}")

    def run(self):
        while True:
            if keyboard.is_pressed('f4'):
                if not self.running:
                    if not os.path.exists(self.name_check):
                        self._save_ui_ref()
                        self.logger.info("📸 Эталон интерфейса создан")
                self.running = not self.running
                self.logger.info(f"\n🔴 {'▶ ЗАПУСК' if self.running else '⏸ ПАУЗА'}\n")
                time.sleep(0.5)
            if keyboard.is_pressed('1') and keyboard.is_pressed('2'):
                self.logger.info("\n💾 Сохранение и выход...")
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
                    self.logger.info(f"✅ Папка обновлена: {f}")
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
    print("🚀 Запуск Roulette Bot (Fixed v3)...")
    root = Tk()
    root.withdraw()
    folder = filedialog.askdirectory(title="📁 Папка для логов и памяти")
    if folder:
        os.makedirs(folder, exist_ok=True)
        RouletteBot(folder).run()
    else:
        print("❌ Отмена.")

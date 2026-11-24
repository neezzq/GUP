from pathlib import Path
from queue import Empty, Queue
import logging
from logging.handlers import RotatingFileHandler
import os
import platform
import sys
import threading
import tkinter
from tkinter import filedialog

import customtkinter as tk

from .api import API, normalize_server
from .app_update import ERROR_FILE, current_build, prepare_update
from .launchers import LAUNCHERS, scan_launchers
from .security import credential_store, launcher_icon, write_settings
from .updater import Updater


BG, SURFACE, BORDER = "#101114", "#1B1D22", "#292C33"
TEXT, SECONDARY, ACCENT = "#F5F5F7", "#92969F", "#548CFF"
GREEN, YELLOW, RED = "#34C759", "#FFB340", "#FF453A"
SERVICE = "GameUpdater.Device"
LABELS = {
    "waiting": ("Ожидает", "Waiting"),
    "launching": ("Запуск", "Launching"),
    "checking": ("Проверка", "Checking"),
    "authorizing": ("Авторизация", "Signing in"),
    "working": ("Установка", "Installing"),
    "downloading": ("Скачивание", "Downloading"),
    "installing": ("Установка", "Installing"),
    "updating": ("Обновление", "Updating"),
    "verifying": ("Проверка файлов", "Verifying files"),
    "patching": ("Установка", "Patching"),
    "unpacking": ("Распаковка", "Unpacking"),
    "update_available": ("Доступно обновление", "Update available"),
    "detected": ("Обнаружено обновление", "Update detected"),
    "updated": ("Обновлено", "Updated"),
    "current": ("Нет обновлений", "Up to date"),
    "confirmation_required": ("Требуется подтверждение", "Confirmation required"),
    "unverified": ("Не подтверждено", "Unverified"),
    "busy": ("Аккаунт занят", "Account busy"),
    "error": ("Ошибка", "Error"),
    "skipped": ("Пропущено", "Skipped"),
    "installed": ("Установлен", "Installed"),
    "scanning": ("Поиск…", "Detecting…"),
    "not_installed": ("Не установлен", "Not installed"),
    "login_required": ("Требуется вход", "Sign-in required"),
    "online": ("Сервер подключён", "Server connected"),
    "offline": ("Локальные сессии", "Local sessions"),
    "rejected": ("Доступ отклонён", "Access denied"),
    "challenge": ("Подтвердите действие в клиенте", "Confirm in the official client"),
    "steam_guard": ("Подтвердите Steam Guard в Steam или на телефоне", "Confirm Steam Guard in Steam or on your phone"),
    "external_login": ("Выберите провайдера в официальном окне входа", "Choose your provider in the official sign-in window"),
    "local_session_required": ("Войдите в свой аккаунт в лаунчере", "Sign in to your account in the launcher"),
    "epic_steam_session": ("Войдите в Epic через Steam, если доступно", "Sign in to Epic with Steam if available"),
    "unknown_session": ("Не подтверждена сессия клиента", "Launcher session could not be verified"),
    "unknown_update": ("Клиент не подтвердил состояние всех игр", "Launcher did not confirm all game states"),
    "previous_active": ("Предыдущая очередь может работать", "Previous queue may still be active"),
    "timeout": ("Истекло время наблюдения", "Monitoring timed out"),
    "desktop": ("Рабочий стол занят или заблокирован", "Desktop is locked or another session is open"),
    "inactive_session": ("Перейдите в активный сеанс Windows", "Switch to an active Windows session"),
    "other_session": ("Другой пользователь вошёл в Windows", "Another user is signed in to Windows"),
    "session_check_failed": ("Не удалось проверить сеансы Windows", "Could not verify Windows sessions"),
    "launcher_start_failed": ("Не удалось запустить лаунчер", "Could not start the launcher"),
    "running_game": ("Обнаружена запущенная игра", "A game is running"),
    "disk": ("Недостаточно места на диске", "Insufficient disk space"),
    "signature": ("Путь или подпись клиента не подтверждены", "Launcher path or signature could not be verified"),
    "detect_failed": ("Ошибка поиска клиента", "Launcher detection failed"),
    "lease_lost": ("Разрешение на использование аккаунта потеряно", "Account lease lost"),
    "server_error": ("Ошибка связи с сервером", "Server request failed"),
    "device_rejected": ("Проверьте подключение устройства", "Check device enrollment"),
    "automation_error": ("Автоматизация прервана", "Automation interrupted"),
    "maintenance": ("Подтвердите учётную запись обслуживания в настройках", "Confirm a maintenance account in Settings"),
    "stopped": ("Очередь остановлена", "Queue stopped"),
    "finished": ("Проверка завершена", "Check complete"),
    "paused": ("Новые задачи приостановлены", "New work paused"),
    "register_failed": ("Не удалось подключить устройство. Проверьте адрес и ключ.", "Enrollment failed. Check the address and key."),
}
COLORS = {
    "updated": GREEN, "current": GREEN, "online": GREEN, "error": RED, "rejected": RED,
    "confirmation_required": YELLOW, "unverified": YELLOW, "login_required": YELLOW,
    "busy": YELLOW, "offline": YELLOW,
}


class Window(tk.CTk):
    def __init__(self, directory: Path, settings):
        tk.set_appearance_mode("dark")
        super().__init__(fg_color=BG)
        self.directory, self.settings = directory, settings
        self.settings_path = directory / "settings.json"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.vault = credential_store()
        self.events = Queue()
        self.updater = None
        self.worker = None
        self.panel = None
        self.closing = False
        self.rows = {}
        self.inventory = {}
        self.detection_labels = {}
        self.scanning = self.update_active = self.game_busy = self.auto_after_scan = False
        self.enrolling = False
        self.logger = logging.getLogger("gup.local")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        file = RotatingFileHandler(directory / "events.log", maxBytes=512 * 1024, backupCount=2, encoding="utf-8")
        file.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        self.logger.addHandler(file)
        self.version = current_build().label
        self.title(f"Game Updater {self.version}")
        self.geometry("700x620")
        self.minsize(660, 580)
        self.protocol("WM_DELETE_WINDOW", self.shutdown)
        self.draw()
        self.after(120, self.poll)
        self.after(180, self.on_open)

    def t(self, ru, en):
        return en if self.settings.get("language") == "en" else ru

    def label(self, status):
        return self.t(*LABELS[status]) if status in LABELS else status

    def button(self, frame, label, command, *, accent=False, width=115):
        return tk.CTkButton(frame, text=label, command=command, width=width, height=34,
                            fg_color=ACCENT if accent else BORDER, text_color=BG if accent else TEXT,
                            hover_color="#6EA0FF" if accent else "#383D47")

    def draw(self):
        for widget in self.winfo_children():
            if widget is not self.panel:
                widget.destroy()
        frame = tk.CTkFrame(self, fg_color=BG)
        frame.pack(fill="both", expand=True, padx=20, pady=16)
        header = tk.CTkFrame(frame, fg_color=BG)
        header.pack(fill="x", pady=(0, 12))
        title = tk.CTkFrame(header, fg_color=BG)
        title.pack(side="left")
        tk.CTkLabel(title, text="Game Updater", text_color=TEXT, anchor="w",
                    font=("Segoe UI", 25, "bold")).pack(anchor="w")
        tk.CTkLabel(title, text=self.t(f"Версия {self.version}", f"Version {self.version}"),
                    text_color=SECONDARY, anchor="w").pack(anchor="w")
        self.network = tk.CTkLabel(title, text=self.settings.get("name", platform.node()),
                                   text_color=SECONDARY, anchor="w")
        self.network.pack(anchor="w")
        self.settings_button = self.button(header, self.t("Настройки", "Settings"), self.preferences)
        self.settings_button.pack(side="right")
        self.update_button = self.button(
            header, self.t("Обновить приложение", "Update application"), self.update_click, width=161,
        )
        self.update_button.pack(side="right", padx=(0, 7))
        self.table = tk.CTkFrame(frame, fg_color=SURFACE, border_color=BORDER, border_width=1)
        self.table.pack(fill="x")
        self.rows.clear()
        self.detection_labels.clear()
        for key in self.settings.get("order", list(LAUNCHERS)):
            row = tk.CTkFrame(self.table, height=34, fg_color=SURFACE)
            row.pack(fill="x", padx=10, pady=1)
            icon = tk.CTkLabel(row, width=28, text=LAUNCHERS[key][:2].upper(), text_color=SECONDARY,
                               font=("Segoe UI", 10, "bold"), fg_color=BORDER, corner_radius=5)
            icon.pack(side="left", padx=(0, 8))
            tk.CTkLabel(row, text=LAUNCHERS[key], font=("Segoe UI", 14, "bold")).pack(side="left")
            status = tk.CTkLabel(row, text=self.label("waiting"), text_color=SECONDARY)
            status.pack(side="right", padx=(8, 4))
            badge = tk.CTkLabel(row, text="", text_color=SECONDARY)
            badge.pack(side="right", padx=(8, 4))
            self.detection_labels[key] = badge
            self.rows[key] = (icon, status)
            self.render_detection(key)
        self.current = tk.CTkLabel(frame, text=self.t("Готов к проверке", "Ready to check"), anchor="w")
        self.current.pack(fill="x", pady=(8, 2))
        self.detail = tk.CTkProgressBar(frame, height=5, fg_color=BORDER, progress_color=ACCENT)
        self.detail.pack(fill="x")
        self.detail.set(0)
        self.overall_title = tk.CTkLabel(frame, text=self.t("Обработка клиентов", "Launcher queue"),
                                         text_color=SECONDARY, anchor="w")
        self.overall_title.pack(fill="x", pady=(4, 0))
        self.overall = tk.CTkProgressBar(frame, height=6, fg_color=BORDER, progress_color=GREEN)
        self.overall.pack(fill="x")
        self.overall.set(0)
        self.log_box = tk.CTkTextbox(frame, height=74, fg_color=SURFACE, text_color=SECONDARY, wrap="word")
        self.log_box.configure(state="disabled")
        self.log_open = False
        actions = tk.CTkFrame(frame, fg_color=BG)
        actions.pack(side="bottom", fill="x", pady=(10, 0))
        self.run_button = self.button(actions, self.t("Проверить повторно", "Check again"), self.start, accent=True, width=142)
        self.run_button.pack(side="right")
        self.pause_button = self.button(actions, self.t("Приостановить", "Pause"), self.toggle_pause)
        self.pause_button.pack(side="left", padx=(0, 6))
        self.stop_button = self.button(actions, self.t("Остановить", "Stop"), self.stop, width=88)
        self.stop_button.pack(side="left", padx=(0, 6))
        self.button(actions, self.t("Журнал", "Log"), self.toggle_log, width=70).pack(side="left")
        self.render_busy(self.game_busy)

    def toggle_log(self):
        self.log_open = not self.log_open
        if self.log_open:
            self.log_box.pack(fill="both", expand=True, pady=(5, 0))
        else:
            self.log_box.pack_forget()

    def log(self, message, code):
        safe = " ".join(message.split())[:350]
        self.logger.info("%s", code)
        self.log_box.configure(state="normal")
        self.log_box.insert("end", safe + "\n")
        if float(self.log_box.index("end")) > 200:
            self.log_box.delete("1.0", "25.0")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def render_busy(self, busy):
        self.game_busy = busy
        blocked = busy or self.scanning or self.update_active
        self.run_button.configure(state="disabled" if blocked else "normal")
        self.settings_button.configure(state="disabled" if blocked else "normal")
        self.update_button.configure(
            state="normal" if not blocked and getattr(sys, "frozen", False) else "disabled",
        )
        self.pause_button.configure(state="normal" if busy else "disabled", text=self.t("Приостановить", "Pause"))
        self.stop_button.configure(state="normal" if busy else "disabled")

    def render_detection(self, key):
        path, status = self.inventory.get(key, (None, "scanning"))
        text = self.label(status)
        if status in {"signature", "detect_failed"}:
            text = self.t("Требует проверки", "Check required")
        self.detection_labels[key].configure(text=text, text_color=GREEN if path else SECONDARY)

    def scan(self):
        self.scanning = True
        self.inventory.clear()
        for key in self.detection_labels:
            self.render_detection(key)
        self.render_busy(self.game_busy)
        paths = dict(self.settings.get("paths", {}))

        def work():
            try:
                for key, launcher, status in scan_launchers(paths):
                    path = launcher.exe if launcher else None
                    self.events.put(("detected_launcher", (key, path, status)))
                    if path and (icon := launcher_icon(path)):
                        self.events.put(("icon", (key, icon)))
            finally:
                self.events.put(("scan_done", None))

        threading.Thread(target=work, daemon=True).start()

    def update_click(self):
        if self.game_busy or self.scanning or self.update_active or self.enrolling or self.panel:
            return
        self.update_active = True
        self.render_busy(False)
        self.current.configure(text=self.t("Проверяю версию на GitHub…", "Checking GitHub for updates…"), text_color=TEXT)

        def work():
            try:
                build = prepare_update(
                    self.directory, lambda release: self.events.put(("app_download", release.label)),
                )
                self.events.put(("app_update", "restart" if build else "current"))
            except Exception:
                self.events.put(("app_update", "failed"))

        threading.Thread(target=work, daemon=True).start()

    def update_result(self, result):
        if result == "restart":
            self.close_logging()
            self.destroy()
            return
        self.update_active = False
        self.render_busy(False)
        if result == "current":
            message = self.t("Установлена последняя версия приложения", "The application is up to date")
        else:
            message = self.t("Не удалось обновить приложение. Проверьте доступ к GitHub и запись в папку EXE.",
                             "Update failed. Check GitHub access and permissions to write to the EXE folder.")
        self.current.configure(text=message, text_color=GREEN if result == "current" else RED)


    def token(self):
        identifier = self.settings.get("device_id")
        return self.vault.get_password(SERVICE, identifier) if identifier else None

    def on_open(self):
        self.scan()
        marker = self.directory / ERROR_FILE
        replacement_failed = False
        try:
            marker.unlink()
        except OSError:
            pass
        else:
            replacement_failed = True
            self.current.configure(
                text=self.t("Не удалось заменить EXE. Запущена предыдущая версия.",
                            "Could not replace the EXE. The previous version is running."),
                text_color=RED,
            )
        try:
            connected = bool(self.settings.get("server") and self.token())
        except Exception:
            connected = False
        if not connected:
            self.enrollment()
        elif self.settings.get("auto_start", True) and not replacement_failed:
            self.auto_after_scan = True

    def start(self):
        if self.update_active or self.panel or (self.worker and self.worker.is_alive()):
            return
        try:
            token = self.token()
        except Exception:
            self.network.configure(text=self.label("device_rejected"), text_color=RED)
            return
        if not token:
            self.enrollment()
            return
        if self.scanning:
            self.auto_after_scan = True
            return
        self.render_busy(True)
        for key, (_, status) in self.rows.items():
            state = "waiting" if key in self.settings.get("enabled", LAUNCHERS) else "skipped"
            status.configure(text=self.label(state), text_color=SECONDARY)
        self.overall.set(0)
        self.overall_title.configure(text=self.t("Обработка клиентов", "Launcher queue"))
        self.results = {}

        def get_icon(key, path):
            icon = launcher_icon(path)
            if icon:
                self.events.put(("icon", (key, icon)))

        def work():
            import pythoncom

            try:
                pythoncom.CoInitializeEx(pythoncom.COINIT_MULTITHREADED)
            except pythoncom.com_error:
                self.events.put(("fault", None))
                return
            try:
                with API(self.settings["server"], token) as api:
                    self.updater = Updater(
                        dict(self.settings), api, lambda item: self.events.put(("notice", item)),
                        icon_callback=get_icon,
                    )
                    if self.closing:
                        self.updater.stopped.set()
                    self.updater.run()
            except Exception:
                self.events.put(("fault", None))
            finally:
                pythoncom.CoUninitialize()

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def toggle_pause(self):
        if not self.updater:
            return
        if self.updater.paused.is_set():
            self.updater.paused.clear()
            self.pause_button.configure(text=self.t("Приостановить", "Pause"))
        else:
            self.updater.paused.set()
            self.pause_button.configure(text=self.t("Продолжить", "Resume"))
            self.log(self.label("paused"), "queue paused")

    def stop(self):
        if self.updater:
            self.updater.stopped.set()

    def close_logging(self):
        for handler in list(self.logger.handlers):
            self.logger.removeHandler(handler)
            handler.close()

    def shutdown(self):
        if self.enrolling or self.update_active:
            return
        if self.worker and self.worker.is_alive():
            self.closing = True
            self.stop()
        else:
            self.close_logging()
            self.destroy()

    def poll(self):
        try:
            while True:
                category, payload = self.events.get_nowait()
                if category == "notice":
                    self.notice(payload)
                elif category == "fault":
                    self.current.configure(text=self.label("automation_error"), text_color=RED)
                    self.log(self.label("automation_error"), "queue failure")
                    self.render_busy(False)
                elif category == "registered":
                    self.enrolled(payload)
                elif category == "detected_launcher":
                    key, path, status = payload
                    self.inventory[key] = (path, status)
                    self.render_detection(key)
                elif category == "scan_done":
                    self.scanning = False
                    for key in self.detection_labels:
                        if key not in self.inventory:
                            self.inventory[key] = (None, "detect_failed")
                            self.render_detection(key)
                    self.render_busy(self.game_busy)
                    if self.auto_after_scan:
                        self.auto_after_scan = False
                        self.start()
                elif category == "app_download":
                    self.current.configure(
                        text=self.t(f"Скачиваю версию {payload}…", f"Downloading version {payload}…"),
                        text_color=TEXT,
                    )
                elif category == "app_update":
                    self.update_result(payload)
                    if payload == "restart":
                        return
                elif category == "icon" and payload[0] in self.rows:
                    import io
                    from PIL import Image

                    try:
                        image = tk.CTkImage(Image.open(io.BytesIO(payload[1])), size=(21, 21))
                        self.rows[payload[0]][0].configure(image=image, text="")
                    except OSError:
                        pass
        except Empty:
            pass
        if self.closing and self.worker and not self.worker.is_alive():
            self.close_logging()
            self.destroy()
        else:
            self.after(120, self.poll)

    def notice(self, item):
        status = item.status
        if status in ("online", "offline", "rejected"):
            self.network.configure(text=f"{self.settings.get('name', platform.node())} · {self.label(status)}",
                                   text_color=COLORS[status])
            return
        if status == "finished":
            result = item.result
            attention = sum(r.status in {"confirmation_required", "login_required", "error", "unverified", "busy"}
                            or (r.status == "skipped" and r.reason != "not_installed")
                            for r in self.results.values())
            caption = self.t(
                f"Обновлено {result.updated} · актуально {result.current} · требуют внимания {attention}",
                f"Updated {result.updated} · current {result.current} · need attention {attention}",
            )
            if result.reason:
                caption = self.label(result.reason) + " · " + caption
            self.current.configure(text=caption, text_color=TEXT)
            if result.reason:
                self.log(self.label(result.reason), f"queue {result.reason}")
            self.detail.stop()
            self.detail.set(0)
            self.render_busy(False)
            return
        key = item.launcher
        if key not in self.rows:
            return
        reason = item.result.reason if item.result else ""
        if item.result:
            self.results[key] = item.result
        display = reason if reason in LABELS else status
        label = self.label(display)
        if item.pending is not None:
            label += f" · {item.pending}"
        if item.result and (item.result.updated or item.result.current):
            label += f" · {item.result.updated}/{item.result.current}"
        self.rows[key][1].configure(text=label, text_color=COLORS.get(status, SECONDARY))
        if status == "detected":
            self.log(f"{LAUNCHERS[key]} · {label} · {item.game}", f"{key} detected")
            return
        self.detail.stop()
        self.detail.configure(mode="determinate")
        self.detail.set(0)
        progress = item.progress
        if item.game:
            phase = self.label(progress.phase) if progress else label
            description = f"{item.game} · {phase}"
            if progress and progress.total_bytes is not None:
                self.detail.set(progress.download_bytes / progress.total_bytes)
                description += f" · {progress.download_bytes / 2**20:.0f} / {progress.total_bytes / 2**20:.0f} MiB"
            else:
                self.detail.configure(mode="indeterminate")
                self.detail.start()
            self.current.configure(text=description[:100], text_color=TEXT)
        else:
            self.current.configure(text=f"{LAUNCHERS[key]} · {label}", text_color=TEXT)
        processed = item.position if item.result else item.position - 1
        self.overall_title.configure(text=f"{self.t('Обработка клиентов', 'Launcher queue')}  {processed}/{item.total}")
        self.overall.set(processed / item.total if item.total else 0)
        if item.result or status in {"launching", "authorizing", "update_available"}:
            details = f" · {self.label(reason)}" if reason and reason != display else ""
            game = f" · {item.game}" if item.game else ""
            self.log(f"{LAUNCHERS[key]} · {label}{details}{game}", f"{key} {status} {reason}")

    def modal(self, title, geometry):
        if self.panel:
            self.panel.destroy()
        dialog = tk.CTkToplevel(self, fg_color=BG)
        dialog.title(title)
        dialog.geometry(geometry)
        dialog.transient(self)

        def close():
            if not self.enrolling:
                dialog.destroy()
                self.panel = None

        dialog.protocol("WM_DELETE_WINDOW", close)
        self.panel = dialog
        return dialog

    def enrollment(self):
        dialog = self.modal(self.t("Подключение компьютера", "Connect computer"), "500x360")
        frame = tk.CTkFrame(dialog, fg_color=SURFACE)
        frame.pack(fill="both", expand=True, padx=14, pady=14)
        fields = []
        for label, value in (
            (self.t("Адрес HTTPS-сервера", "HTTPS server address"), self.settings.get("server", "https://")),
            (self.t("Имя компьютера", "Computer name"), self.settings.get("name", platform.node())),
            (self.t("Одноразовый ключ", "One-time key"), ""),
        ):
            tk.CTkLabel(frame, text=label, anchor="w").pack(fill="x", padx=14, pady=(8, 0))
            entry = tk.CTkEntry(frame, fg_color=BG, show="*" if len(fields) == 2 else "")
            entry.pack(fill="x", padx=14)
            entry.insert(0, value)
            fields.append(entry)
        maintenance = tkinter.BooleanVar(value=self.settings.get("maintenance", False))
        tk.CTkCheckBox(frame, text=self.t("Это отдельная учётная запись обслуживания Windows",
                                         "This is a dedicated Windows maintenance account"),
                       variable=maintenance).pack(anchor="w", padx=14, pady=10)
        self.connection_status = tk.CTkLabel(frame, text="", text_color=YELLOW, wraplength=450)
        self.connection_status.pack(fill="x")

        def submit():
            if self.enrolling:
                return
            try:
                server = normalize_server(fields[0].get())
                name, key = (entry.get().strip() for entry in fields[1:])
                if not 1 <= len(name) <= 80 or len(key) != 43:
                    raise ValueError
            except ValueError:
                self.connection_status.configure(text=self.label("register_failed"))
                return
            flags = {"server": server, "name": name, "maintenance": maintenance.get()}
            fields[2].delete(0, "end")
            self.enrolling = True
            self.connection_status.configure(text=self.t("Подключение…", "Connecting…"))

            def register():
                try:
                    with API(server) as api:
                        result = api.register(name, key)
                    identifier = result["device_id"]
                    self.vault.set_password(SERVICE, identifier, result["token"])
                    self.events.put(("registered", {**flags, "device_id": identifier}))
                except Exception:
                    self.events.put(("registered", None))

            threading.Thread(target=register, daemon=True).start()

        self.button(frame, self.t("Подключить", "Connect"), submit, accent=True).pack(pady=6)

    def enrolled(self, data):
        self.enrolling = False
        if data is None:
            self.connection_status.configure(text=self.label("register_failed"))
            return
        previous = self.settings.get("device_id")
        config = {**self.settings, **data}
        try:
            write_settings(self.settings_path, config)
        except OSError:
            try:
                self.vault.delete_password(SERVICE, data["device_id"])
            except Exception:
                pass
            self.connection_status.configure(text=self.label("register_failed"))
            return
        self.settings = config
        if previous and previous != config["device_id"]:
            try:
                self.vault.delete_password(SERVICE, previous)
            except Exception:
                pass
        self.panel.destroy()
        self.panel = None
        self.draw()
        if config.get("auto_start", True):
            self.start()

    def preferences(self):
        dialog = self.modal(self.t("Настройки", "Settings"), "655x590")
        container = tk.CTkFrame(dialog, fg_color=BG)
        container.pack(fill="both", expand=True, padx=14, pady=14)
        header = tk.CTkFrame(container, fg_color=SURFACE)
        header.pack(fill="x")
        info = self.settings.get("device_id", "—")
        tk.CTkLabel(header, text=f"{self.settings.get('server', '—')}\nID  {info}", justify="left").pack(side="left", padx=10)
        self.button(header, self.t("Переподключить", "Reconnect"), self.enrollment, width=135).pack(side="right", padx=8, pady=8)
        options = tk.CTkFrame(container, fg_color=BG)
        options.pack(fill="x", pady=6)
        values = {}
        for key, label, default in (
            ("maintenance", self.t("Отдельная учётная запись обслуживания", "Dedicated Windows maintenance account"), False),
            ("auto_start", self.t("Начинать проверку при открытии", "Check on startup"), True),
            ("auto_close", self.t("Закрывать запущенные приложением клиенты", "Close launchers opened by Game Updater"), True),
        ):
            values[key] = tkinter.BooleanVar(value=self.settings.get(key, default))
            tk.CTkCheckBox(options, text=label, variable=values[key]).pack(anchor="w", pady=3)
        language = tk.CTkOptionMenu(options, values=["Русский", "English"], width=100)
        language.set(self.t("Русский", "English"))
        language.pack(anchor="e")
        items = tk.CTkScrollableFrame(container, fg_color=SURFACE, height=180)
        items.pack(fill="both", expand=True)
        order = list(self.settings.get("order", LAUNCHERS))
        rows = {}
        enabled = self.settings.get("enabled", LAUNCHERS)
        paths = self.settings.get("paths", {})

        def order_rows():
            for index, key in enumerate(order):
                rows[key][0].grid(row=index, column=0, sticky="ew", pady=2)
            items.grid_columnconfigure(0, weight=1)

        def move(key, offset):
            index = order.index(key)
            other = index + offset
            if 0 <= other < len(order):
                order[index], order[other] = order[other], order[index]
                order_rows()

        def browse(entry):
            selected = filedialog.askopenfilename(parent=dialog, filetypes=[("Windows executable", "*.exe")])
            if selected:
                entry.delete(0, "end")
                entry.insert(0, selected)

        for key in order:
            row = tk.CTkFrame(items, fg_color=SURFACE)
            value = tkinter.BooleanVar(value=key in enabled)
            tk.CTkCheckBox(row, variable=value, text=LAUNCHERS[key], width=125).pack(side="left", padx=(0, 4))
            detected_path, _ = self.inventory.get(key, (None, "scanning"))
            placeholder = str(detected_path) if detected_path else self.t("Путь к клиенту", "Launcher path")
            entry = tk.CTkEntry(row, placeholder_text=placeholder)
            if key in paths:
                entry.insert(0, paths[key])
            entry.pack(side="left", fill="x", expand=True)
            self.button(row, "…", lambda item=entry: browse(item), width=29).pack(side="left", padx=3)
            self.button(row, "↑", lambda item=key: move(item, -1), width=29).pack(side="left")
            self.button(row, "↓", lambda item=key: move(item, 1), width=29).pack(side="left", padx=3)
            rows[key] = (row, value, entry)
        order_rows()
        footer = tk.CTkFrame(container, fg_color=BG)
        footer.pack(fill="x", pady=(8, 0))

        def save():
            config = {
                **self.settings, **{key: value.get() for key, value in values.items()},
                "language": "en" if language.get() == "English" else "ru",
                "order": order, "enabled": [key for key, (_, value, _) in rows.items() if value.get()],
                "paths": {key: entry.get().strip() for key, (_, _, entry) in rows.items() if entry.get().strip()},
            }
            try:
                write_settings(self.settings_path, config)
            except OSError:
                self.current.configure(text=self.label("error"), text_color=RED)
                return
            self.settings = config
            dialog.destroy()
            self.panel = None
            self.draw()
            self.scan()

        self.button(footer, self.t("Открыть папку логов", "Open logs folder"),
                    lambda: os.startfile(self.directory), width=145).pack(side="left")
        self.button(footer, self.t("Сохранить", "Save"), save, accent=True).pack(side="right")

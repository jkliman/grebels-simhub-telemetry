"""Desktop app: set up the game, then stream telemetry to SimHub."""

import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import config as config_module
from . import installer
from .bridge import Bridge, Status

APP_TITLE = "G-Rebels Telemetry"

STATE_COLOURS = {
    Status.STREAMING: "#1e8e3e",
    Status.PAUSED: "#b06000",
    Status.CALIBRATING: "#1a73e8",
    Status.WAITING_FOR_GAME: "#5f6368",
    Status.WAITING_FOR_MOD: "#5f6368",
    Status.ERROR: "#c5221f",
}


def bundled_dir():
    """Where our own data files live, frozen or not."""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TelemetryApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("560x600")
        self.minsize(520, 560)

        self.config_data = config_module.Config.load()
        if not self.config_data.game_path:
            self.config_data.game_path = config_module.find_game_path()

        self.status = Status()
        self.bridge = None
        self.log_queue = queue.Queue()

        self._build_ui()
        self._refresh_setup_state()
        self.after(100, self._tick)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ ui --
    def _build_ui(self):
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass

        padding = dict(padx=12, pady=(10, 0))

        # --- status banner
        banner = ttk.Frame(self)
        banner.pack(fill="x", **padding)
        self.state_label = tk.Label(banner, text="Waiting for game",
                                    font=("Segoe UI", 15, "bold"), anchor="w")
        self.state_label.pack(fill="x")
        self.detail_label = tk.Label(banner, text="", anchor="w", fg="#5f6368")
        self.detail_label.pack(fill="x")

        # --- live readout
        readout = ttk.LabelFrame(self, text="Telemetry")
        readout.pack(fill="x", **padding)
        self.readout_vars = {}
        rows = [("Craft", "craft"), ("Speed", "speed"), ("Altitude", "altitude"),
                ("G (long / lat)", "gforce"), ("Sending to", "destination"),
                ("Packets", "packets"), ("Game updates", "game_hz")]
        for index, (label, key) in enumerate(rows):
            ttk.Label(readout, text=label + ":").grid(
                row=index, column=0, sticky="w", padx=(10, 6), pady=2)
            var = tk.StringVar(value="-")
            self.readout_vars[key] = var
            ttk.Label(readout, textvariable=var, font=("Consolas", 10)).grid(
                row=index, column=1, sticky="w", pady=2)
        readout.columnconfigure(1, weight=1)

        # --- destination
        destination = ttk.LabelFrame(self, text="SimHub machine")
        destination.pack(fill="x", **padding)
        ttk.Label(destination, text="IP address:").grid(
            row=0, column=0, sticky="w", padx=(10, 6), pady=6)
        self.host_var = tk.StringVar(value=self.config_data.host)
        ttk.Entry(destination, textvariable=self.host_var, width=18).grid(
            row=0, column=1, sticky="w")
        ttk.Label(destination, text="Port:").grid(row=0, column=2, sticky="w", padx=(14, 6))
        self.port_var = tk.StringVar(value=str(self.config_data.port))
        ttk.Entry(destination, textvariable=self.port_var, width=8).grid(
            row=0, column=3, sticky="w")
        ttk.Label(destination,
                  text="Use 127.0.0.1 if SimHub runs on this PC. "
                       "In SimHub, select DiRT Rally 2.0.",
                  foreground="#5f6368", wraplength=500).grid(
            row=1, column=0, columnspan=4, sticky="w", padx=10, pady=(0, 8))

        self.gforce_var = tk.BooleanVar(value=self.config_data.send_g_forces)
        ttk.Checkbutton(destination, text="Send G-forces (beta)",
                        variable=self.gforce_var).grid(
            row=2, column=0, columnspan=4, sticky="w", padx=10, pady=(0, 8))

        # --- start / stop
        controls = ttk.Frame(self)
        controls.pack(fill="x", **padding)
        self.start_button = ttk.Button(controls, text="Start streaming",
                                       command=self._toggle)
        self.start_button.pack(side="left")
        ttk.Button(controls, text="Save settings",
                   command=self._save_settings).pack(side="left", padx=8)

        # --- setup
        setup = ttk.LabelFrame(self, text="Game setup")
        setup.pack(fill="both", expand=True, **padding)
        path_row = ttk.Frame(setup)
        path_row.pack(fill="x", padx=10, pady=(8, 4))
        self.path_var = tk.StringVar(value=self.config_data.game_path or "not found")
        ttk.Entry(path_row, textvariable=self.path_var).pack(
            side="left", fill="x", expand=True)
        ttk.Button(path_row, text="Browse", command=self._browse,
                   width=9).pack(side="left", padx=(6, 0))

        self.setup_text = tk.Text(setup, height=8, wrap="word",
                                  font=("Consolas", 9), relief="flat",
                                  background="#f5f5f5")
        self.setup_text.pack(fill="both", expand=True, padx=10, pady=4)
        self.setup_text.configure(state="disabled")

        button_row = ttk.Frame(setup)
        button_row.pack(fill="x", padx=10, pady=(0, 10))
        self.install_button = ttk.Button(button_row, text="Install / repair",
                                         command=self._install)
        self.install_button.pack(side="left")
        ttk.Button(button_row, text="Re-check",
                   command=self._refresh_setup_state).pack(side="left", padx=8)
        ttk.Button(button_row, text="Remove",
                   command=self._uninstall).pack(side="right")

    # -------------------------------------------------------------- helpers --
    def log(self, message):
        self.log_queue.put(message)

    def _write_setup_text(self, lines):
        self.setup_text.configure(state="normal")
        self.setup_text.delete("1.0", "end")
        self.setup_text.insert("end", "\n".join(lines))
        self.setup_text.see("end")
        self.setup_text.configure(state="disabled")

    def _refresh_setup_state(self):
        path = self.path_var.get().strip()
        if config_module.looks_like_game_path(path):
            self.config_data.game_path = path
        binaries = self.config_data.binaries_dir
        lines = []
        if not self.config_data.game_path:
            lines.append("G-Rebels not found. Use Browse to point at the folder")
            lines.append("containing G_Rebels\\Binaries\\Win64.")
        else:
            lines.append("Game: %s" % self.config_data.game_path)
            lines.append("")
            lines.extend(installer.describe_install(binaries))
            lines.append("")
            lines.append("Ready to stream." if installer.is_installed(binaries)
                         else "Press Install / repair to set up UE4SS and the mod.")
        self._write_setup_text(lines)

    def _browse(self):
        chosen = filedialog.askdirectory(title="Select the G-Rebels folder")
        if not chosen:
            return
        chosen = os.path.normpath(chosen)
        if not config_module.looks_like_game_path(chosen):
            messagebox.showerror(
                APP_TITLE,
                "That folder does not contain G_Rebels\\Binaries\\Win64\\"
                "G_Rebels-Win64-Shipping.exe.")
            return
        self.path_var.set(chosen)
        self.config_data.game_path = chosen
        self.config_data.save()
        self._refresh_setup_state()

    def _apply_settings(self):
        self.config_data.host = self.host_var.get().strip() or "127.0.0.1"
        try:
            self.config_data.port = int(self.port_var.get())
        except ValueError:
            self.config_data.port = 20777
            self.port_var.set("20777")
        self.config_data.send_g_forces = bool(self.gforce_var.get())

    def _save_settings(self):
        self._apply_settings()
        self.config_data.save()
        self.log("Settings saved.")

    # ------------------------------------------------------------- actions --
    def _install(self):
        if not self.config_data.game_path:
            messagebox.showerror(APP_TITLE, "Point the app at your G-Rebels folder first.")
            return
        self.install_button.configure(state="disabled")
        lines = ["Setting up..."]
        self._write_setup_text(lines)

        def worker():
            def progress(message):
                lines.append(message)
                self.log_queue.put(("setup", list(lines)))
            try:
                installer.install(self.config_data.binaries_dir, bundled_dir(),
                                  progress=progress)
            except Exception as exc:
                progress("FAILED: %s" % exc)
            self.log_queue.put(("setup-done", None))

        threading.Thread(target=worker, daemon=True).start()

    def _uninstall(self):
        if not messagebox.askyesno(
                APP_TITLE,
                "Remove UE4SS and the telemetry mod from the game folder?\n\n"
                "The game's own files are not touched."):
            return
        try:
            installer.uninstall(self.config_data.binaries_dir, progress=self.log)
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))
        self._refresh_setup_state()

    def _toggle(self):
        if self.bridge and self.bridge.running:
            self.bridge.stop()
            self.bridge = None
            self.start_button.configure(text="Start streaming")
            self.title(APP_TITLE)
            return

        self._apply_settings()
        self.config_data.save()
        if not installer.is_installed(self.config_data.binaries_dir):
            if not messagebox.askyesno(
                    APP_TITLE,
                    "Setup looks incomplete, so the game may not publish any "
                    "telemetry.\n\nStart anyway?"):
                return
        self.status = Status()
        self.bridge = Bridge(self.config_data, self.status)
        self.bridge.start()
        self.start_button.configure(text="Stop")

    # ---------------------------------------------------------------- tick --
    def _tick(self):
        while True:
            try:
                item = self.log_queue.get_nowait()
            except queue.Empty:
                break
            if isinstance(item, tuple):
                kind, payload = item
                if kind == "setup":
                    self._write_setup_text(payload)
                elif kind == "setup-done":
                    self.install_button.configure(state="normal")
                    self._refresh_setup_state()
            else:
                self._write_setup_text([item])

        if self.bridge:
            snapshot = self.status.snapshot()
            state = snapshot["state"]
            self.state_label.configure(
                text=state.capitalize(),
                fg=STATE_COLOURS.get(state, "#000000"))
            self.detail_label.configure(text=snapshot["detail"])

            self.readout_vars["craft"].set(snapshot["craft"] or "-")
            self.readout_vars["speed"].set(
                "%7.1f m/s   (%.0f km/h)" % (snapshot["speed_ms"],
                                             snapshot["speed_ms"] * 3.6))
            self.readout_vars["altitude"].set("%7.1f m" % snapshot["altitude_m"])
            self.readout_vars["gforce"].set(
                "%+5.2f / %+5.2f" % (snapshot["g_longitudinal"], snapshot["g_lateral"]))
            self.readout_vars["destination"].set(snapshot["destination"] or "-")
            self.readout_vars["packets"].set(
                "%d  (%.0f/s)" % (snapshot["packets_sent"], snapshot["packet_rate"]))
            self.readout_vars["game_hz"].set("%.0f Hz" % snapshot["game_update_hz"])

            # The taskbar is the at-a-glance indicator while the game is fullscreen.
            if state == Status.STREAMING:
                self.title("%s - %.0f km/h" % (APP_TITLE, snapshot["speed_ms"] * 3.6))
            else:
                self.title("%s - %s" % (APP_TITLE, state))

        self.after(100, self._tick)

    def _on_close(self):
        if self.bridge:
            self.bridge.stop()
        self._apply_settings()
        self.config_data.save()
        self.destroy()


def main():
    app = TelemetryApp()
    app.mainloop()


if __name__ == "__main__":
    main()

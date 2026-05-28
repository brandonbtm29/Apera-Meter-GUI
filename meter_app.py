import customtkinter as ctk
import serial
import serial.tools.list_ports
import threading
import time
import datetime
import os
import struct
import tkinter as tk
from tkinter import ttk, filedialog
import csv
import matplotlib
matplotlib.use('TkAgg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import tkinter.messagebox as messagebox
import json
import openpyxl
from openpyxl.styles import Font, Alignment
import ctypes


scan_lock = threading.Lock()
ctk.set_appearance_mode("Dark")  
ctk.set_default_color_theme("blue")  

class MeterController(ctk.CTkFrame):
    def __init__(self, parent, meter_id=1, app_controller=None, **kwargs):
        super().__init__(parent, **kwargs)
        self.meter_id = meter_id
        self.app_controller = app_controller

        self.ser = None
        self.is_connected = False
        self.listen_thread = None
        self.unsaved_changes = False
        
        # New State for recording and plotting
        self.is_recording = False
        self.received_timestamps = set()
        
        self.plot_dates = []
        self.plot_conds = []
        self.plot_temps = []
        
        self.last_auto_dump_time = time.time()
        
        # Config setup
        self.config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
        self.config = {
            f"default_com_port_{meter_id}": "COM4" if meter_id == 1 else "Auto-Detect Apera Meter",
            f"default_auto_dump_hrs_{meter_id}": 2.0,
            f"default_save_location_{meter_id}": "",
            f"meter_notes_{meter_id}": "",
            f"keep_alive_{meter_id}": True
        }
        self.load_config()
        
        docs = self.config.get(f"default_save_location_{meter_id}", "")
        if not docs or not os.path.isdir(docs):
            docs = os.path.join(os.environ.get('USERPROFILE', os.path.expanduser('~')), 'Documents')
        self.emergency_backup_path = os.path.join(docs, f"Apera_Meter{meter_id}_Backup.csv")
        
        self.columns = ("DateTime", "Mode", "Cond (uS/cm)", "Cond (mS/cm)", "Temp (°C)", "Raw Cond_ADC")
        
        # --- Layout Configuration ---
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self.main_paned = tk.PanedWindow(self, orient=tk.HORIZONTAL, bg="#2b2b2b", sashwidth=6, sashrelief=tk.RAISED)
        self.main_paned.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)
        
        self.left_container = ctk.CTkFrame(self.main_paned, fg_color="transparent")
        self.mid_container = ctk.CTkFrame(self.main_paned, fg_color="transparent")
        self.right_container = ctk.CTkFrame(self.main_paned, fg_color="transparent")
        
        self.main_paned.add(self.left_container, minsize=220)
        self.main_paned.add(self.mid_container, minsize=400, stretch="always")
        self.main_paned.add(self.right_container, minsize=220)
        
        self.frame_left = ctk.CTkScrollableFrame(self.left_container, width=280)
        self.frame_left.pack(fill="both", expand=True)
        self.frame_mid = ctk.CTkFrame(self.mid_container)
        self.frame_mid.pack(fill="both", expand=True)
        self.frame_right = ctk.CTkScrollableFrame(self.right_container, width=300)
        self.frame_right.pack(fill="both", expand=True)
        
        # --- LEFT PANEL: Settings & Recording ---
        ctk.CTkLabel(self.frame_left, text=f"Meter {meter_id}", font=ctk.CTkFont(size=20, weight="bold")).pack(pady=(10, 20))
        
        ctk.CTkLabel(self.frame_left, text="Recording Settings", font=ctk.CTkFont(size=14, weight="bold")).pack(anchor="w", padx=10)
        
        self.btn_record = ctk.CTkButton(self.frame_left, text="Start Recording", fg_color="#2ecc71", hover_color="#27ae60", command=self.toggle_record)
        self.btn_record.pack(fill="x", padx=10, pady=10)
        
        self.auto_dump_var = ctk.BooleanVar(value=False)
        self.auto_dump_chk = ctk.CTkCheckBox(self.frame_left, text="Auto-Dump Memory (hrs):", variable=self.auto_dump_var)
        self.auto_dump_chk.pack(anchor="w", padx=10, pady=(10, 2))
        
        self.auto_dump_entry = ctk.CTkEntry(self.frame_left, width=100)
        self.auto_dump_entry.insert(0, str(self.config.get(f"default_auto_dump_hrs_{meter_id}", 2.0)))
        self.auto_dump_entry.pack(anchor="w", padx=10, pady=5)
        
        ctk.CTkFrame(self.frame_left, height=2, fg_color="gray").pack(fill="x", padx=10, pady=15)
        
        ctk.CTkLabel(self.frame_left, text="Axis Limits", font=ctk.CTkFont(size=14, weight="bold")).pack(anchor="w", padx=10)
        self.auto_time_var = ctk.BooleanVar(value=True)
        self.auto_cond_var = ctk.BooleanVar(value=True)
        self.auto_temp_var = ctk.BooleanVar(value=True)
        
        ctk.CTkCheckBox(self.frame_left, text="Auto Time", variable=self.auto_time_var, command=self.plot_data).pack(anchor="w", padx=10, pady=2)
        ctk.CTkCheckBox(self.frame_left, text="Auto Cond", variable=self.auto_cond_var, command=self.plot_data).pack(anchor="w", padx=10, pady=2)
        ctk.CTkCheckBox(self.frame_left, text="Auto Temp", variable=self.auto_temp_var, command=self.plot_data).pack(anchor="w", padx=10, pady=(2, 10))
        
        def make_entry(parent, text):
            f = ctk.CTkFrame(parent, fg_color="transparent")
            f.pack(fill="x", padx=10, pady=2)
            ctk.CTkLabel(f, text=text).pack(side="left")
            e = ctk.CTkEntry(f, width=70)
            e.pack(side="right")
            return e
            
        self.ent_cond_min = make_entry(self.frame_left, "Cond Min:")
        self.ent_cond_max = make_entry(self.frame_left, "Cond Max:")
        self.ent_temp_min = make_entry(self.frame_left, "Temp Min (°C):")
        self.ent_temp_max = make_entry(self.frame_left, "Temp Max (°C):")
        
        self.btn_apply_lims = ctk.CTkButton(self.frame_left, text="Apply Limits", command=self.apply_limits)
        self.btn_apply_lims.pack(fill="x", padx=10, pady=10)
        
        ctk.CTkFrame(self.frame_left, height=2, fg_color="gray").pack(fill="x", padx=10, pady=10)
        self.btn_settings = ctk.CTkButton(self.frame_left, text="Advanced Settings", command=self.open_settings, fg_color="#7f8c8d", hover_color="#95a5a6")
        self.btn_settings.pack(fill="x", padx=10, pady=(5, 20))
        
        # --- MIDDLE PANEL: Views ---
        self.view_var = ctk.StringVar(value="Combined View")
        self.view_seg = ctk.CTkSegmentedButton(self.frame_mid, values=["Data Table", "Live Plot", "Combined View"], variable=self.view_var, command=self.toggle_view)
        self.view_seg.pack(pady=10)
        
        self.views_container = ctk.CTkFrame(self.frame_mid, fg_color="transparent")
        self.views_container.pack(fill="both", expand=True, padx=5, pady=5)
        
        self.plot_frame = ctk.CTkFrame(self.views_container)
        self.table_frame = ctk.CTkFrame(self.views_container)
        
        plt.style.use('dark_background')
        self.fig, self.ax = plt.subplots(figsize=(5, 4))
        self.fig.patch.set_facecolor('#2b2b2b')
        self.ax.set_facecolor('#2b2b2b')
        self.ax.set_title(f"Live Conductivity (Meter {meter_id})")
        self.ax.set_xlabel("Time")
        self.ax.set_ylabel("Conductivity (uS/cm)", color='#1f538d')
        self.ax.grid(True, linestyle='--', alpha=0.3)
        self.ax2 = self.ax.twinx()
        self.ax2.set_ylabel("Temperature (°C)", color='#e74c3c')
        self.fig.tight_layout()
        
        self.canvas = FigureCanvasTkAgg(self.fig, master=self.plot_frame)
        self.canvas_widget = self.canvas.get_tk_widget()
        self.canvas_widget.pack(fill="both", expand=True, padx=2, pady=2)
        
        style = tk.ttk.Style()
        style.theme_use("default")
        style.configure("Treeview", background="#2b2b2b", foreground="white", fieldbackground="#2b2b2b", borderwidth=0, rowheight=25)
        style.map('Treeview', background=[('selected', '#1f538d')])
        style.configure("Treeview.Heading", background="#565b5e", foreground="white", relief="flat", font=("Arial", 10, "bold"))
        style.map("Treeview.Heading", background=[('active', '#343638')])
        
        self.table_frame.grid_columnconfigure(0, weight=1)
        self.table_frame.grid_rowconfigure(0, weight=1)
        
        self.tree = tk.ttk.Treeview(self.table_frame, columns=self.columns, show="headings", selectmode="extended")
        col_widths = [140, 60, 90, 90, 80, 100]
        for i, col in enumerate(self.columns):
            self.tree.heading(col, text=col, command=lambda c=col: self._sort_treeview(c, False))
            self.tree.column(col, width=col_widths[i], anchor="center")
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.scrollbar = tk.ttk.Scrollbar(self.table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscroll=self.scrollbar.set)
        self.scrollbar.grid(row=0, column=1, sticky="ns")
        
        self.toggle_view(self.view_var.get())
        
        # --- RIGHT PANEL: Connection & Export ---
        ctk.CTkLabel(self.frame_right, text="Connection", font=ctk.CTkFont(size=14, weight="bold")).pack(anchor="w", padx=10, pady=(10, 5))
        self.port_combo = ctk.CTkComboBox(self.frame_right, values=self.get_ports())
        self.port_combo.pack(fill="x", padx=10, pady=5)
        self.btn_refresh = ctk.CTkButton(self.frame_right, text="Refresh Ports", command=self.refresh_ports)
        self.btn_refresh.pack(fill="x", padx=10, pady=5)
        self.btn_connect = ctk.CTkButton(self.frame_right, text="Connect", command=self.toggle_connection, font=ctk.CTkFont(weight="bold"))
        self.btn_connect.pack(fill="x", padx=10, pady=(5, 10))
        
        self.lbl_status = ctk.CTkLabel(self.frame_right, text="Status: Disconnected", text_color="gray", font=ctk.CTkFont(weight="bold"))
        self.lbl_status.pack(pady=5)
        
        self.sn_label = ctk.CTkLabel(self.frame_right, text="SN: Unknown", font=ctk.CTkFont(size=11), text_color="gray")
        self.sn_label.pack(anchor="w", padx=10)
        self.model_label = ctk.CTkLabel(self.frame_right, text="Model: Unknown", font=ctk.CTkFont(size=11), text_color="gray")
        self.model_label.pack(anchor="w", padx=10)
        
        ctk.CTkFrame(self.frame_right, height=2, fg_color="gray").pack(fill="x", padx=10, pady=10)
        
        ctk.CTkLabel(self.frame_right, text="Experiment Notes", font=ctk.CTkFont(size=14, weight="bold")).pack(anchor="w", padx=10)
        self.txt_notes = ctk.CTkTextbox(self.frame_right, height=100)
        self.txt_notes.pack(fill="x", padx=10, pady=5)
        self.txt_notes.insert("0.0", self.config.get(f"meter_notes_{meter_id}", ""))
        self.txt_notes.bind("<KeyRelease>", self.save_notes)
        
        ctk.CTkFrame(self.frame_right, height=2, fg_color="gray").pack(fill="x", padx=10, pady=10)
        
        self.btn_sync = ctk.CTkButton(self.frame_right, text="Sync Meter Time", command=self.sync_time, state="disabled", fg_color="#f39c12", hover_color="#d68910")
        self.btn_sync.pack(fill="x", padx=10, pady=5)
        self.btn_download = ctk.CTkButton(self.frame_right, text="Download Raw Memory", command=self.download_memory, state="disabled")
        self.btn_download.pack(fill="x", padx=10, pady=5)
        
        ctk.CTkFrame(self.frame_right, height=2, fg_color="gray").pack(fill="x", padx=10, pady=10)
        
        self.btn_export = ctk.CTkButton(self.frame_right, text="Save Log to Excel", command=self.export_data, fg_color="#27ae60", hover_color="#219150", font=ctk.CTkFont(weight="bold"))
        self.btn_export.pack(fill="x", padx=10, pady=5)
        
        self.btn_save_graph = ctk.CTkButton(self.frame_right, text="Save Graph PNG", command=self.save_graph, fg_color="#2980b9", hover_color="#1f618d")
        self.btn_save_graph.pack(fill="x", padx=10, pady=5)
        
        self.btn_recover = ctk.CTkButton(self.frame_right, text="Recover Session", command=self.recover_session, fg_color="#8e44ad", hover_color="#732d91")
        self.btn_recover.pack(fill="x", padx=10, pady=5)
        
        self.btn_clear = ctk.CTkButton(self.frame_right, text="Clear Data", command=self.clear_log, fg_color="#c0392b", hover_color="#922b21")
        self.btn_clear.pack(fill="x", padx=10, pady=(5, 20))
        
        # Init emergency file
        self.init_backup_file()
        
        self.refresh_ports()
        target_com = self.config.get(f"default_com_port_{self.meter_id}", "Auto-Detect Apera Meter")
        if self.port_combo.get() == target_com:
            self.after(500, self.connect)

    def init_backup_file(self):
        try:
            if not os.path.exists(self.emergency_backup_path):
                with open(self.emergency_backup_path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow(self.columns)
        except Exception:
            pass

    def save_notes(self, event=None):
        self.config[f"meter_notes_{self.meter_id}"] = self.txt_notes.get("0.0", "end").strip()
        self.save_config()

    def toggle_record(self):
        if not self.is_recording:
            self.is_recording = True
            self.btn_record.configure(text="Stop Recording", fg_color="#e74c3c", hover_color="#c0392b")
            self.ax.set_title(f"Recording: Conductivity (Meter {self.meter_id})")
        else:
            self.is_recording = False
            self.btn_record.configure(text="Start Recording", fg_color="#2ecc71", hover_color="#27ae60")
            self.ax.set_title(f"Live (Not Recording): Conductivity (Meter {self.meter_id})")
        self.canvas.draw_idle()

    def recover_session(self):
        if not os.path.exists(self.emergency_backup_path):
            messagebox.showinfo("Recover", "No backup file found to recover.")
            return
            
        try:
            with open(self.emergency_backup_path, "r", encoding="utf-8") as f:
                reader = csv.reader(f)
                header = next(reader, None)
                rows = list(reader)
                
            self.clear_log()
            
            for row in rows:
                if len(row) >= 6:
                    self.received_timestamps.add(row[0])
                    self.tree.insert("", "end", values=row)
                    
                    try:
                        dt = datetime.datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
                        cond = float(row[2])
                        temp = float(row[4])
                        self.plot_dates.append(dt)
                        self.plot_conds.append(cond)
                        self.plot_temps.append(temp)
                    except Exception:
                        pass
                        
            self.plot_data()
            messagebox.showinfo("Recover", f"Recovered {len(rows)} points.")
        except Exception as e:
            messagebox.showerror("Error", f"Recovery failed: {e}")

    def apply_limits(self):
        self.auto_time_var.set(False)
        self.auto_cond_var.set(False)
        self.auto_temp_var.set(False)
        self.plot_data()

    def save_graph(self):
        filepath = filedialog.asksaveasfilename(defaultextension=".png", title="Save Graph", filetypes=[("PNG", "*.png")])
        if filepath:
            try:
                self.fig.savefig(filepath, dpi=300, bbox_inches='tight')
                messagebox.showinfo("Success", "Graph saved!")
            except Exception as e:
                messagebox.showerror("Error", str(e))

    def toggle_view(self, view_name):
        if not hasattr(self, 'inner_paned'):
            self.inner_paned = tk.PanedWindow(self.views_container, orient=tk.VERTICAL, bg="#2b2b2b", sashwidth=6)
            
        self.inner_paned.pack_forget()
        try: self.inner_paned.forget(self.plot_frame)
        except Exception: pass
        try: self.inner_paned.forget(self.table_frame)
        except Exception: pass
        
        self.plot_frame.pack_forget()
        self.table_frame.pack_forget()
            
        if view_name == "Data Table":
            self.table_frame.pack(fill="both", expand=True)
        elif view_name == "Live Plot":
            self.plot_frame.pack(fill="both", expand=True)
        elif view_name == "Combined View":
            self.inner_paned.pack(fill="both", expand=True)
            self.inner_paned.add(self.plot_frame, minsize=100, stretch="always")
            self.inner_paned.add(self.table_frame, minsize=100, stretch="always")

    def open_settings(self):
        top = ctk.CTkToplevel(self)
        top.title(f"Settings - Meter {self.meter_id}")
        top.geometry("400x300")
        top.attributes("-topmost", True)
        
        ctk.CTkLabel(top, text="Default Save Directory:", font=ctk.CTkFont(weight="bold")).pack(pady=(20,5))
        lbl_dir = ctk.CTkLabel(top, text=self.config.get(f"default_save_location_{self.meter_id}", "Not Set"), text_color="gray")
        lbl_dir.pack(pady=5)
        
        def choose_dir():
            d = filedialog.askdirectory()
            if d:
                lbl_dir.configure(text=d)
                self.config[f"default_save_location_{self.meter_id}"] = d
                self.save_config()
                
        ctk.CTkButton(top, text="Browse...", command=choose_dir).pack(pady=5)
        
        keep_alive_var = ctk.BooleanVar(value=self.config.get(f"keep_alive_{self.meter_id}", True))
        def toggle_ka():
            self.config[f"keep_alive_{self.meter_id}"] = keep_alive_var.get()
            self.save_config()
            
        chk = ctk.CTkCheckBox(top, text="Enable Keep-Alive (Ping every 2s)", variable=keep_alive_var, command=toggle_ka)
        chk.pack(pady=30)

    def _sort_treeview(self, col, reverse):
        items = [(self.tree.set(k, col), k) for k in self.tree.get_children("")]
        try:
            items.sort(key=lambda t: float(t[0]) if t[0] else -9999, reverse=reverse)
        except ValueError:
            items.sort(reverse=reverse)
        for index, (_, k) in enumerate(items):
            self.tree.move(k, "", index)
        self.tree.heading(col, command=lambda: self._sort_treeview(col, not reverse))

    def load_config(self):
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r") as f:
                    self.config.update(json.load(f))
            except Exception: pass

    def save_config(self):
        try:
            with open(self.config_path, "w") as f:
                json.dump(self.config, f, indent=4)
        except Exception: pass

    def on_closing(self):
        self.is_connected = False
        time.sleep(0.5)
        if self.ser and self.ser.is_open:
            self.ser.close()

    def get_ports(self):
        ports = serial.tools.list_ports.comports()
        return ["Auto-Detect Apera Meter"] + [p.device for p in ports]

    def refresh_ports(self):
        ports = self.get_ports()
        self.port_combo.configure(values=ports)
        target_com = self.config.get(f"default_com_port_{self.meter_id}", "Auto-Detect Apera Meter")
        if target_com in ports:
            self.port_combo.set(target_com)
        elif ports:
            self.port_combo.set(ports[0])

    def log_point(self, dt, row_data):
        self.plot_dates.append(dt)
        self.plot_conds.append(float(row_data[2]) if row_data[2] else 0.0)
        self.plot_temps.append(float(row_data[4]) if row_data[4] else 0.0)
        
        self.tree.insert("", "end", values=row_data)
        self.tree.yview_moveto(1)
        self.unsaved_changes = True
        
        try:
            with open(self.emergency_backup_path, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(row_data)
        except Exception: pass
        
        self.schedule_plot()

    def schedule_plot(self):
        if not getattr(self, "_plot_scheduled", False):
            self._plot_scheduled = True
            self.after(500, self.plot_data)

    def plot_data(self):
        self._plot_scheduled = False
        self.ax.clear()
        self.ax2.clear()

        self.ax.set_yscale('log')
        if self.is_recording:
            self.ax.set_title(f"Recording: Conductivity (Meter {self.meter_id})")
        else:
            self.ax.set_title(f"Live: Conductivity (Meter {self.meter_id})")
            
        self.ax.set_xlabel("Time")
        self.ax.set_ylabel("Conductivity (uS/cm)", color='#3498db')
        self.ax.grid(True, linestyle='--', alpha=0.3)
        self.ax2.set_ylabel("Temperature (°C)", color='#e74c3c')
        
        if self.plot_dates:
            self.ax.plot(self.plot_dates, self.plot_conds, marker='o', linestyle='-', color='#3498db', label="Cond")
            self.ax.tick_params(axis='y', labelcolor='#3498db')
            
            if not self.auto_cond_var.get():
                try:
                    cmin = float(self.ent_cond_min.get())
                    cmax = float(self.ent_cond_max.get())
                    self.ax.set_ylim(cmin, cmax)
                except ValueError: pass
                
            self.ax2.plot(self.plot_dates, self.plot_temps, marker='s', linestyle='-', color='#e74c3c', label="Temp")
            self.ax2.tick_params(axis='y', labelcolor='#e74c3c')
            
            if not self.auto_temp_var.get():
                try:
                    tmin = float(self.ent_temp_min.get())
                    tmax = float(self.ent_temp_max.get())
                    self.ax2.set_ylim(tmin, tmax)
                except ValueError: pass
                
            self.ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S'))
            self.fig.autofmt_xdate()
            
        self.fig.tight_layout()
        self.canvas.draw_idle()

    def clear_log(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.received_timestamps.clear()
        self.plot_dates.clear()
        self.plot_conds.clear()
        self.plot_temps.clear()
        self.last_auto_dump_time = time.time()
        
        # Clear backup CSV
        try:
            with open(self.emergency_backup_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(self.columns)
        except Exception: pass
        
        self.plot_data()

    def toggle_connection(self):
        if self.is_connected:
            self.disconnect()
        else:
            self.connect()

    def find_apera_meter(self):
        with scan_lock:
            ports = [p.device for p in serial.tools.list_ports.comports()]
            used_ports = self.app_controller.get_used_ports() if self.app_controller else []
            for p in ports:
                if p in used_ports: continue
                try:
                    ts = serial.Serial(p, 115200, timeout=0.5)
                    ts.setDTR(False)
                    ts.setRTS(False)
                    ts.write(b'&OK#\r\n')
                    time.sleep(0.3)
                    resp = ts.read(ts.in_waiting)
                    ts.close()
                    if b'<SH' in resp or b'<SL' in resp or b'#>' in resp:
                        return p
                except Exception: pass
            return None

    def connect(self):
        port = self.port_combo.get()
        if port == "Auto-Detect Apera Meter":
            self.lbl_status.configure(text="Status: Scanning COM ports...")
            self.btn_connect.configure(state="disabled")
            def scan_thread():
                found_port = self.find_apera_meter()
                self.after(0, self._finish_connect, found_port)
            threading.Thread(target=scan_thread, daemon=True).start()
            return
        self._finish_connect(port)
        
    def _finish_connect(self, port):
        self.btn_connect.configure(state="normal")
        if not port or port == "No Ports Found":
            self.lbl_status.configure(text="Status: Auto-Detect Failed", text_color="#E74C3C")
            return
        try:
            self.port_combo.set(port)
            self.ser = serial.Serial(port, 115200, timeout=0.1)
            self.ser.setDTR(False)
            self.ser.setRTS(False)
            self.is_connected = True
            self.btn_connect.configure(text="Disconnect", fg_color="#C0392B", hover_color="#922B21")
            self.btn_download.configure(state="normal")
            self.btn_sync.configure(state="normal")
            self.lbl_status.configure(text=f"Status: Connected to {port}", text_color="#2ECC71")
            
            self.ser.write(b"&OK#\r\n")
            self.after(500, self.sync_time)
            threading.Thread(target=self.read_serial_loop, daemon=True).start()
            threading.Thread(target=self.keep_alive_loop, daemon=True).start()
            threading.Thread(target=self.auto_dump_loop, daemon=True).start()
        except Exception as e:
            self.lbl_status.configure(text=f"Failed to connect: {e}", text_color="#E74C3C")

    def disconnect(self):
        self.is_connected = False
        time.sleep(0.5)
        if self.ser and self.ser.is_open:
            self.ser.close()
        self.btn_connect.configure(text="Connect", fg_color=["#3a7ebf", "#1f538d"], hover_color=["#325882", "#14375e"])
        self.btn_download.configure(state="disabled")
        self.btn_sync.configure(state="disabled")
        self.lbl_status.configure(text="Status: Disconnected", text_color="gray")
        self.sn_label.configure(text="SN: Unknown")
        self.model_label.configure(text="Model: Unknown")

    def read_serial_loop(self):
        buffer = b""
        self.last_data_time = 0
        while self.is_connected:
            try:
                if self.ser.in_waiting > 0:
                    data = self.ser.read(self.ser.in_waiting)
                    buffer += data
                    buffer = buffer.replace(b'#\r\n', b'')
                    while b'>' in buffer:
                        idx = buffer.find(b'>')
                        packet = buffer[:idx+1]
                        buffer = buffer[idx+1:]
                        self.process_packet(packet)
            except Exception:
                self.after(0, self.disconnect)
                break
            time.sleep(0.05)

    def auto_dump_loop(self):
        while self.is_connected:
            time.sleep(10) 
            if self.is_connected and self.auto_dump_var.get():
                try: hours = float(self.auto_dump_entry.get())
                except ValueError: continue
                if time.time() - self.last_auto_dump_time > (hours * 3600):
                    self.last_auto_dump_time = time.time()
                    if self.ser and self.ser.is_open:
                        self.ser.write(b"&WA#\r\n")

    def keep_alive_loop(self):
        while self.is_connected:
            if self.config.get(f"keep_alive_{self.meter_id}", True):
                if time.time() - self.last_data_time > 2.0:
                    try:
                        if self.ser and self.ser.is_open:
                            self.ser.write(b"&OK#\r\n")
                    except Exception: pass
            time.sleep(1.5)

    def process_packet(self, packet):
        if not packet: return
        packet = packet.strip(b'\r\n')
        p_len = len(packet)
        if p_len < 3 or packet == b'>' or packet == b'#>': return 
            
        if (packet.startswith(b'<DS') or packet.startswith(b'<DN')) and p_len >= 12:
            self.last_data_time = time.time()
            try:
                payload = packet[3:11]
                w1, w2, w3, w4 = struct.unpack(">HHHH", payload[0:8])
                
                mode = w1 // 4096
                w1_rem = w1 % 4096
                mm = w1_rem // 64
                ss = w1_rem % 64
                
                yy = w2 // 512
                w2_rem = w2 % 512
                mt = w2_rem // 32
                dd = w2_rem % 32
                
                hh = w3 // 2048
                w3_rem = w3 % 2048
                MTC = w3_rem // 1024
                NTC = w3_rem % 1024
                
                b = w4 // 8192
                w4_rem = w4 % 8192
                dot = w4_rem // 2048
                DP = w4_rem % 2048
                
                temp_c = NTC / 10.0
                mode_str = "Cond" if mode == 2 else f"Mode({mode})"
                
                val_float = DP
                if dot == 1: val_float = DP / 10.0
                elif dot == 2: val_float = DP / 100.0
                elif dot == 3: val_float = DP / 1000.0
                
                cond_us, cond_ms = "", ""
                if mode == 2:
                    u_val = b + 10
                    if u_val == 10: 
                        cond_ms = f"{val_float:.3f}"
                        cond_us = f"{val_float * 1000:.1f}"
                    elif u_val == 11: 
                        cond_ms = f"{val_float / 1000:.3f}"
                        cond_us = f"{val_float:.1f}"
                else:
                    cond_us = str(val_float)
                
                dt_str = f"20{yy:02d}-{mt:02d}-{dd:02d} {hh:02d}:{mm:02d}:{ss:02d}"
                dt_obj = datetime.datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
                
                if dt_str in self.received_timestamps:
                    return
                self.received_timestamps.add(dt_str)
                
                row_data = (dt_str, mode_str, cond_us, cond_ms, f"{temp_c:.1f}", str(w4))
                
                if self.is_recording:
                    self.after(0, self.log_point, dt_obj, row_data)
                else:
                    # Live preview updates only the plot list and plot
                    self.plot_dates.append(dt_obj)
                    self.plot_conds.append(float(cond_us) if cond_us else 0.0)
                    self.plot_temps.append(float(temp_c))
                    # keep rolling window of 100 points for live preview if not recording to save memory
                    if len(self.plot_dates) > 100:
                        self.plot_dates.pop(0)
                        self.plot_conds.pop(0)
                        self.plot_temps.pop(0)
                    self.after(0, self.schedule_plot)
                    
            except Exception: pass
                
        elif packet.startswith(b'<SH') and p_len >= 12:
            payload = packet[3:11]
            hex_str = "-".join([f"{b:02X}" for b in payload])
            self.after(0, lambda: self.sn_label.configure(text=f"SN: {hex_str}"))
        elif packet.startswith(b'<P2') and p_len >= 12:
            payload = packet[3:11]
            hex_str = "-".join([f"{b:02X}" for b in payload])
            self.after(0, lambda: self.model_label.configure(text=f"Model: {hex_str}"))

    def sync_time(self):
        if self.is_connected and self.ser:
            now = datetime.datetime.now()
            try:
                packet = bytearray([
                    ord('&'), ord('D'), ord('T'),
                    now.year - 2000, now.month, now.day,
                    now.hour, now.minute, now.second,
                    ord('#')
                ])
                packet.extend(b'\r\n')
                self.ser.write(packet)
            except Exception: pass

    def download_memory(self):
        if self.is_connected and self.ser:
            self.lbl_status.configure(text="Status: Downloading Memory...")
            self.ser.write(b"&WA#\r\n")

    def export_data(self):
        if not self.tree.get_children():
            self.lbl_status.configure(text="Status: No data to export.")
            return
            
        dt_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filepath = filedialog.asksaveasfilename(
            defaultextension=".xlsx",
            initialfile=f"Meter{self.meter_id}_{dt_str}.xlsx",
            filetypes=[("Excel files", "*.xlsx"), ("All files", "*.*")]
        )
        if not filepath: return
        
        try:
            wb = openpyxl.Workbook()
            doc_ws = wb.active
            doc_ws.title = "Documentation"
            doc_ws["A1"] = "Experiment Documentation"
            doc_ws["A1"].font = Font(size=16, bold=True)
            doc_ws["A3"] = "Export Date:"
            doc_ws["B3"] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            doc_ws["A4"] = "Meter SN:"
            doc_ws["B4"] = self.sn_label.cget("text").replace("SN: ", "")
            doc_ws["A5"] = "Experiment Notes:"
            doc_ws["A5"].font = Font(bold=True)
            doc_ws["A6"] = self.txt_notes.get("0.0", "end").strip()
            doc_ws["A6"].alignment = Alignment(wrap_text=True, vertical="top")
            doc_ws.row_dimensions[6].height = 100
            doc_ws.column_dimensions["A"].width = 25
            doc_ws.column_dimensions["B"].width = 35
            
            data_ws = wb.create_sheet("Data")
            data_ws.append(self.columns)
            for cell in data_ws[1]: cell.font = Font(bold=True)
            for item in self.tree.get_children():
                row = self.tree.item(item, 'values')
                out_row = []
                for val in row:
                    try: out_row.append(float(val))
                    except ValueError: out_row.append(val)
                data_ws.append(out_row)
                
            wb.save(filepath)
            self.lbl_status.configure(text=f"Status: Exported to Excel!")
            self.unsaved_changes = False
        except Exception as e:
            self.lbl_status.configure(text=f"Status: Export failed! {e}")

class MeterApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Apera Dual-Meter Control Center")
        self.geometry("1400x800")
        self.protocol("WM_DELETE_WINDOW", self.on_closing)
        
        self.tab_view = ctk.CTkTabview(self)
        self.tab_view.pack(fill="both", expand=True, padx=10, pady=10)
        
        self.tab1 = self.tab_view.add("Meter 1")
        self.tab2 = self.tab_view.add("Meter 2")
        
        self.meter1 = MeterController(self.tab1, meter_id=1, app_controller=self)
        self.meter1.pack(fill="both", expand=True)
        
        self.meter2 = MeterController(self.tab2, meter_id=2, app_controller=self)
        self.meter2.pack(fill="both", expand=True)
        
    def get_used_ports(self):
        used = []
        if self.meter1.is_connected and self.meter1.ser: used.append(self.meter1.ser.port)
        if self.meter2.is_connected and self.meter2.ser: used.append(self.meter2.ser.port)
        return used

    def on_closing(self):
        if self.meter1.unsaved_changes or self.meter2.unsaved_changes:
            response = messagebox.askyesno("Unsaved Data", "You have unsaved experimental data!\\nAre you sure you want to close?")
            if not response: return
        self.meter1.on_closing()
        self.meter2.on_closing()
        self.destroy()

if __name__ == "__main__":
    app = MeterApp()
    app.mainloop()

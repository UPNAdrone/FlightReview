import tkinter as tk
from tkinter import ttk, filedialog, messagebox, PhotoImage
import os
import sys
import shutil
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from pyulog import ulog2csv, ULog
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure
try:
    import contextily as ctx
    CONTEXTILY_AVAILABLE = True
except ImportError:
    CONTEXTILY_AVAILABLE = False
    print("Warning: contextily not available. Install with: pip install contextily")

# OSM's volunteer tile.openstreetmap.org servers block unidentified /
# application clients (403 "Access blocked"). Use Esri's free street
# basemap instead, and always send an identifying User-Agent.
BASEMAP_HEADERS = {
    "User-Agent": "UPNAdrone-FlightLogAnalyzer/1.0 (desktop; local GPS path viewer)"
}

APP_DISPLAY_NAME = "UPNAdrone Log Viewer"
APP_ID = "upnadrone-log-viewer"  # filesystem-safe id (data dirs, etc.)
APP_VERSION = "1.0.0"


def _app_base_dir():
    """Directory of the script, or PyInstaller extract dir when frozen."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def _resource_path(*parts):
    return os.path.join(_app_base_dir(), *parts)


def _user_data_dir():
    """Writable per-user data directory (CSV cache, etc.)."""
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(root, APP_DISPLAY_NAME)
    # Linux / macOS
    xdg = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(xdg, APP_ID)


def _basemap_source():
    """Return a tile provider suitable for desktop app use."""
    return ctx.providers.Esri.WorldStreetMap

class FlightLogAnalyzerApp:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_DISPLAY_NAME)
        self.root.minsize(1100, 700)
        self.root.geometry("1280x860")
        
        # Set the application icon (works for source and frozen builds)
        icon_path = _resource_path("flight-review-logo.png")
        if os.path.exists(icon_path):
            icon = PhotoImage(file=icon_path)
            self.root.iconphoto(True, icon)
        else:
            print(f"Icon file not found: {icon_path}")

        self.apply_ui_theme()

        # Configuration — frozen builds write under the user data dir
        if getattr(sys, "frozen", False):
            self.csv_output_dir = os.path.join(_user_data_dir(), "csv_output")
        else:
            self.csv_output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "csv_output")
        self.current_figure = None
        self.canvas = None
        self.toolbar = None
        self.ulog_file = None
        self.ulog_files = {}  # Dictionary to store log names and their topics
        self.ulog_paths = {}  # logname -> absolute .ulg path
        self.active_analysis_log = None  # Selected log for Position / Map / Vibrations
        self.plotted_topics = set()  # Track currently plotted topics (format: "logname_topic")
        self.plot_configs = {}  # Dictionary to store plot configurations
        self.all_param_names = []  # Store all parameter names for filtering
        self.all_params = {}
        
        # Split-view state for analysis tabs (key -> view dict)
        self.analysis_splits = {}
        self._split_pane_seq = 0
        self.MAX_SPLIT_PANES = 4
        
        # Flight events cache: logname -> list[{t_min, label, kind, color}]
        self._events_cache = {}
        self.overlay_events_var = tk.BooleanVar(value=False)
        
        # Global time cursor + interval (minutes from each log's own start)
        self.global_t_min = 0.0
        self.interval_start = None  # None => full log
        self.interval_end = None
        self._log_duration = {}  # logname -> duration minutes
        self._time_axis_registry = []  # [{ax, cursor_line, logname, t0, t1}]
        self._syncing_global_time = False  # guard map <-> global feedback
        
        # Legacy single-map aliases kept in sync with the first map pane
        self.map_figure = None
        self.map_canvas = None
        self.map_toolbar = None
        self.map_ax = None
        
        # Curated multi-plot tabs (topic, fields). Special topic handling remains in plotter.
        self.default_plots = {
            "Vibrations": [
                ('vehicle_attitude_0', ['roll', 'pitch', 'yaw']),
                ('vehicle_angular_velocity_0', ['xyz[0]', 'xyz[1]', 'xyz[2]']),
                ('vehicle_attitude_setpoint_0', ['roll_body', 'pitch_body', 'yaw_body']),
                ('actuator_outputs_0', ['output[0]', 'output[1]', 'output[2]', 'output[3]']),
                ('sensor_combined_0', ['accelerometer_m_s2[0]', 'accelerometer_m_s2[1]', 'accelerometer_m_s2[2]']),
                ('vehicle_imu_status_0', ['accel_vibration_metric', 'gyro_vibration_metric', 'delta_angle_coning_metric']),
                ('vehicle_imu_status_0', ['accel_clipping[0]', 'accel_clipping[1]', 'accel_clipping[2]']),
                ('vehicle_imu_status_0', ['var_accel[0]', 'var_accel[1]', 'var_accel[2]']),
                ('sensor_gyro_fft_0', ['peak_frequencies_x[0]', 'peak_frequencies_y[0]', 'peak_frequencies_z[0]']),
            ],
            "Battery": [
                ('battery_status_0', ['voltage_filtered_v', 'voltage_v']),
                ('battery_status_0', ['current_filtered_a', 'current_a', 'current_average_a']),
                ('battery_status_0', ['remaining', 'discharged_mah']),
                ('battery_status_0', ['temperature', 'max_cell_voltage_delta']),
                ('system_power_0', ['voltage5v_v']),
            ],
            "Radio": [
                ('input_rc_0', ['rssi']),
                ('input_rc_0', ['rc_lost', 'rc_failsafe']),
                ('input_rc_0', ['rc_lost_frame_count', 'rc_total_frame_count']),
                ('telemetry_status_0', ['rx_message_lost_rate', 'rx_packet_drop_count']),
                ('telemetry_status_0', ['tx_rate_avg', 'rx_rate_avg']),
                ('manual_control_setpoint_0', ['x', 'y', 'z', 'r']),
            ],
        }
        
        # PX4 vehicle_status.nav_state labels (common set; unknown -> NAV_<id>)
        self.NAV_STATE_NAMES = {
            0: "MANUAL", 1: "ALTCTL", 2: "POSCTL", 3: "AUTO_MISSION", 4: "AUTO_LOITER",
            5: "AUTO_RTL", 6: "AUTO_LANDENGFAIL", 7: "AUTO_LANDGPSFAIL", 8: "ACRO",
            9: "UNUSED", 10: "DESCEND", 11: "TERMINATION", 12: "OFFBOARD", 13: "STAB",
            14: "RATTITUDE_LEGACY", 15: "AUTO_TAKEOFF", 16: "AUTO_LAND",
            17: "AUTO_FOLLOW_TARGET", 18: "AUTO_PRECLAND", 19: "ORBIT",
            20: "AUTO_VTOL_TAKEOFF",
        }
        
        # Clear CSV output directory on startup
        self.clear_csv_output()
        
        # Create main container
        self.main_frame = ttk.Frame(root)
        self.main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        # Compact top bar for log open/convert (replaces the old fat sidebar)
        self.create_log_toolbar()
        # Shared cursor + interval controls for all time-series views
        self.create_global_timeline_bar()
        
        # Create main pane with tabs
        self.create_main_pane()
        
        # Create curated plot tabs (Vibrations / Battery / Radio)
        self.create_default_plot_tabs()
        
        # Create CSV output directory if it doesn't exist
        if not os.path.exists(self.csv_output_dir):
            os.makedirs(self.csv_output_dir)
        
        # Overview is the default landing tab for post-flight review
        try:
            self.notebook.select(self.overview_tab)
        except Exception:
            pass

    def clear_csv_output(self):
        """Clear the CSV output directory"""
        if os.path.exists(self.csv_output_dir):
            shutil.rmtree(self.csv_output_dir)
        os.makedirs(self.csv_output_dir)

    def apply_ui_theme(self):
        """Lightweight ttk restyle — clam + compact engineering palette, no extra deps."""
        style = ttk.Style(self.root)
        # clam is the most themeable built-in; default/classic look dated on Linux
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        bg = "#eef1f4"
        surface = "#f7f8fa"
        border = "#c5ccd4"
        text = "#1f2933"
        muted = "#52606d"
        accent = "#0b6e4f"       # deep teal — readable, not the usual purple AI look
        accent_hover = "#095c42"
        select_bg = "#d7ebe3"
        row_alt = "#f0f3f6"

        self.root.configure(bg=bg)
        # Prefer a clean UI font available on most Linux desktops
        ui_font = ("DejaVu Sans", 10)
        ui_font_bold = ("DejaVu Sans", 10, "bold")
        ui_font_small = ("DejaVu Sans", 9)

        style.configure(".", background=bg, foreground=text, font=ui_font, borderwidth=1)
        style.configure("TFrame", background=bg)
        style.configure("TLabel", background=bg, foreground=text, font=ui_font)
        style.configure("TLabelframe", background=bg, foreground=text, bordercolor=border, relief="solid")
        style.configure("TLabelframe.Label", background=bg, foreground=muted, font=ui_font_bold)
        style.configure("TSeparator", background=border)

        style.configure(
            "TButton",
            background=surface,
            foreground=text,
            bordercolor=border,
            lightcolor=surface,
            darkcolor=border,
            focuscolor=select_bg,
            padding=(10, 5),
            font=ui_font,
        )
        style.map(
            "TButton",
            background=[("active", select_bg), ("pressed", "#cfe3db"), ("disabled", "#e4e7eb")],
            foreground=[("disabled", "#9aa5b1")],
            bordercolor=[("active", accent)],
        )

        style.configure(
            "Accent.TButton",
            background=accent,
            foreground="#ffffff",
            bordercolor=accent,
            lightcolor=accent,
            darkcolor=accent_hover,
            padding=(10, 5),
            font=ui_font_bold,
        )
        style.map(
            "Accent.TButton",
            background=[("active", accent_hover), ("pressed", "#074a35"), ("disabled", "#9aa5b1")],
            foreground=[("disabled", "#e4e7eb")],
            bordercolor=[("active", accent_hover)],
        )

        style.configure(
            "TEntry",
            fieldbackground="#ffffff",
            foreground=text,
            bordercolor=border,
            lightcolor=border,
            darkcolor=border,
            insertcolor=text,
            padding=5,
        )
        style.map("TEntry", bordercolor=[("focus", accent)])

        style.configure(
            "TCombobox",
            fieldbackground="#ffffff",
            background=surface,
            foreground=text,
            bordercolor=border,
            arrowcolor=muted,
            padding=4,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", "#ffffff")],
            bordercolor=[("focus", accent)],
            arrowcolor=[("active", accent)],
        )

        style.configure(
            "TNotebook",
            background=bg,
            borderwidth=0,
            tabmargins=(4, 4, 4, 0),
        )
        style.configure(
            "TNotebook.Tab",
            background="#d9dee5",
            foreground=muted,
            padding=(14, 7),
            font=ui_font,
            borderwidth=1,
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", surface), ("active", select_bg)],
            foreground=[("selected", text), ("active", text)],
            expand=[("selected", (0, 0, 0, 2))],
        )

        style.configure(
            "Treeview",
            background="#ffffff",
            fieldbackground="#ffffff",
            foreground=text,
            bordercolor=border,
            rowheight=26,
            font=ui_font_small,
        )
        style.configure(
            "Treeview.Heading",
            background="#e1e6ec",
            foreground=muted,
            font=ui_font_bold,
            relief="flat",
            borderwidth=0,
            padding=6,
        )
        style.map(
            "Treeview",
            background=[("selected", select_bg)],
            foreground=[("selected", text)],
        )
        style.map("Treeview.Heading", background=[("active", select_bg)])

        style.configure(
            "TCheckbutton",
            background=bg,
            foreground=text,
            focuscolor=bg,
            font=ui_font,
        )
        style.map(
            "TCheckbutton",
            background=[("active", bg)],
            foreground=[("active", text)],
        )

        style.configure(
            "Horizontal.TScale",
            background=bg,
            troughcolor="#d5dbe3",
            bordercolor=border,
            lightcolor=accent,
            darkcolor=accent,
            sliderthickness=16,
        )
        style.configure("TScrollbar", background=surface, troughcolor=bg, bordercolor=border, arrowcolor=muted)
        style.map("TScrollbar", background=[("active", "#d7dde5")])

        # Keep a couple of palette tokens for non-ttk bits (matplotlib toolbars stay native)
        self.ui_colors = {
            "bg": bg,
            "surface": surface,
            "border": border,
            "text": text,
            "muted": muted,
            "accent": accent,
            "select": select_bg,
            "row_alt": row_alt,
        }

    def create_log_toolbar(self):
        """Compact top toolbar for browsing/converting logs and listing loaded ones."""
        toolbar = ttk.Frame(self.main_frame)
        toolbar.pack(side=tk.TOP, fill=tk.X, pady=(0, 6))
        
        ttk.Button(toolbar, text="Browse", command=self.select_ulog_file).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(
            toolbar, text="Convert", style="Accent.TButton", command=self.convert_ulog
        ).pack(side=tk.LEFT, padx=(0, 8))
        
        self.file_path = tk.StringVar()
        path_entry = ttk.Entry(toolbar, textvariable=self.file_path)
        path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        
        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=4, pady=2)
        
        ttk.Label(toolbar, text="Loaded:").pack(side=tk.LEFT, padx=(4, 4))
        self.loaded_logs_var = tk.StringVar(value="(none)")
        self.loaded_logs_combo = ttk.Combobox(
            toolbar, textvariable=self.loaded_logs_var, state="readonly", width=28
        )
        self.loaded_logs_combo.pack(side=tk.LEFT, padx=(0, 4))
        self.loaded_logs_combo.bind("<<ComboboxSelected>>", self._on_loaded_log_picked)
        
        self.loaded_logs_count = ttk.Label(toolbar, text="0 logs")
        self.loaded_logs_count.pack(side=tk.LEFT, padx=(0, 8))
        
        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=4, pady=2)
        self.overlay_events_check = ttk.Checkbutton(
            toolbar,
            text="Overlay events on plots",
            variable=self.overlay_events_var,
            command=self._on_overlay_events_toggled,
        )
        self.overlay_events_check.pack(side=tk.LEFT, padx=(4, 0))

    def create_global_timeline_bar(self):
        """Shared time cursor + interval selection under the log toolbar."""
        bar = ttk.Frame(self.main_frame)
        bar.pack(side=tk.TOP, fill=tk.X, pady=(0, 6))
        self.global_timeline_bar = bar
        
        ttk.Label(bar, text="Time:").pack(side=tk.LEFT)
        self.global_time_var = tk.DoubleVar(value=0.0)
        self.global_time_scale = ttk.Scale(
            bar,
            from_=0.0,
            to=1.0,
            orient=tk.HORIZONTAL,
            variable=self.global_time_var,
            command=self._on_global_time_scrub,
            state=tk.DISABLED,
        )
        self.global_time_scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        self.global_time_label = ttk.Label(bar, text="--:-- / --:--", width=14)
        self.global_time_label.pack(side=tk.LEFT)
        
        ttk.Separator(bar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6, pady=1)
        self.interval_label = ttk.Label(bar, text="Interval: full", width=22)
        self.interval_label.pack(side=tk.LEFT, padx=(0, 4))
        
        self.btn_interval_start = ttk.Button(
            bar, text="Set start", command=self._interval_set_start, state=tk.DISABLED, width=9
        )
        self.btn_interval_start.pack(side=tk.LEFT, padx=2)
        self.btn_interval_end = ttk.Button(
            bar, text="Set end", command=self._interval_set_end, state=tk.DISABLED, width=9
        )
        self.btn_interval_end.pack(side=tk.LEFT, padx=2)
        self.btn_interval_reset = ttk.Button(
            bar, text="Reset", command=self._interval_reset, state=tk.DISABLED, width=7
        )
        self.btn_interval_reset.pack(side=tk.LEFT, padx=2)

    def _on_overlay_events_toggled(self):
        """Re-render analysis panes and Data Plots when event overlay is toggled."""
        for key, view in self.analysis_splits.items():
            if view['kind'] in ('curated', 'position', 'overview'):
                for pane in view['panes']:
                    self._refresh_split_pane(key, pane)
        if self.plotted_topics:
            self.replot_all()

    # ----- Global time cursor + interval -----

    def _compute_log_duration(self, logname):
        """Return log duration in minutes (relative to first sample)."""
        if logname in self._log_duration:
            return self._log_duration[logname]
        duration = 1e-6
        for topic in (
            'vehicle_status_0',
            'vehicle_local_position_0',
            'sensor_combined_0',
            'battery_status_0',
            'vehicle_attitude_0',
        ):
            df = self._read_topic_csv(logname, topic, usecols=['timestamp'])
            if df is None or len(df) < 2:
                continue
            t, _ = self._topic_time_minutes(df)
            duration = max(float(t.iloc[-1]), duration)
            break
        self._log_duration[logname] = duration
        return duration

    def _active_duration(self):
        if not self.active_analysis_log or not self.ulog_files.get(self.active_analysis_log):
            return None
        return self._compute_log_duration(self.active_analysis_log)

    def _update_global_timeline_bar(self):
        """Enable/disable the bar and set scale range from the active log duration."""
        duration = self._active_duration()
        widgets = (
            self.global_time_scale,
            self.btn_interval_start,
            self.btn_interval_end,
            self.btn_interval_reset,
        )
        if duration is None:
            for w in widgets:
                w.configure(state=tk.DISABLED)
            self.global_time_label.configure(text="--:-- / --:--")
            self.interval_label.configure(text="Interval: full")
            return
        
        self.global_time_scale.configure(from_=0.0, to=duration, state=tk.NORMAL)
        for w in (self.btn_interval_start, self.btn_interval_end, self.btn_interval_reset):
            w.configure(state=tk.NORMAL)
        
        # Clamp cursor / interval into new duration
        t = min(max(self.global_t_min, 0.0), duration)
        if self.interval_start is not None:
            self.interval_start = min(max(self.interval_start, 0.0), duration)
        if self.interval_end is not None:
            self.interval_end = min(max(self.interval_end, 0.0), duration)
        self._set_global_time(t, broadcast=False)
        self._refresh_interval_label()
        self.global_time_label.configure(
            text=f"{self.format_minutes(self.global_t_min)} / {self.format_minutes(duration)}"
        )

    def _refresh_interval_label(self):
        if self.interval_start is None or self.interval_end is None:
            self.interval_label.configure(text="Interval: full")
        else:
            self.interval_label.configure(
                text=f"Interval: {self.format_minutes(self.interval_start)} – {self.format_minutes(self.interval_end)}"
            )

    def _effective_interval(self, t0=0.0, t1=None):
        """Return (start, end) minutes to display for a series spanning [t0, t1]."""
        if t1 is None:
            t1 = self._active_duration() or 1.0
        if self.interval_start is None or self.interval_end is None:
            return float(t0), float(t1)
        return float(max(t0, self.interval_start)), float(min(t1, self.interval_end))

    def _df_in_interval(self, df):
        """Restrict a topic DataFrame to the selected global interval (relative minutes)."""
        if df is None or len(df) == 0:
            return df
        if self.interval_start is None or self.interval_end is None:
            return df
        t, _ = self._topic_time_minutes(df)
        mask = (t.to_numpy() >= self.interval_start) & (t.to_numpy() <= self.interval_end)
        return df.loc[mask].copy()

    def _events_in_interval(self, events):
        if self.interval_start is None or self.interval_end is None:
            return events
        return [
            e for e in events
            if self.interval_start <= e['t_min'] <= self.interval_end
        ]

    def _set_global_time(self, t, broadcast=True):
        duration = self._active_duration()
        if duration is not None:
            t = min(max(float(t), 0.0), duration)
            if self.interval_start is not None and self.interval_end is not None:
                t = min(max(t, self.interval_start), self.interval_end)
        self.global_t_min = float(t)
        self._syncing_global_time = True
        try:
            self.global_time_var.set(self.global_t_min)
            dur = duration if duration is not None else self.global_t_min
            self.global_time_label.configure(
                text=f"{self.format_minutes(self.global_t_min)} / {self.format_minutes(dur)}"
            )
        finally:
            self._syncing_global_time = False
        if broadcast:
            self._broadcast_global_time()

    def _on_global_time_scrub(self, value):
        if self._syncing_global_time:
            return
        try:
            t = float(value)
        except (TypeError, ValueError):
            return
        if self.interval_start is not None and self.interval_end is not None:
            t = min(max(t, self.interval_start), self.interval_end)
        self.global_t_min = t
        duration = self._active_duration() or t
        self.global_time_label.configure(
            text=f"{self.format_minutes(t)} / {self.format_minutes(duration)}"
        )
        self._broadcast_global_time()

    def _interval_set_start(self):
        self.interval_start = self.global_t_min
        if self.interval_end is None:
            duration = self._active_duration()
            self.interval_end = duration if duration is not None else self.global_t_min
        if self.interval_start > self.interval_end:
            self.interval_start, self.interval_end = self.interval_end, self.interval_start
        self._refresh_interval_label()
        self._broadcast_interval_change()

    def _interval_set_end(self):
        self.interval_end = self.global_t_min
        if self.interval_start is None:
            self.interval_start = 0.0
        if self.interval_start > self.interval_end:
            self.interval_start, self.interval_end = self.interval_end, self.interval_start
        self._refresh_interval_label()
        self._broadcast_interval_change()

    def _interval_reset(self):
        self.interval_start = None
        self.interval_end = None
        self._refresh_interval_label()
        self._broadcast_interval_change()

    def _unregister_figure(self, fig):
        if fig is None:
            return
        self._time_axis_registry = [
            e for e in self._time_axis_registry
            if e.get('ax') is not None and e['ax'].figure is not fig
        ]

    def _prune_time_axis_registry(self):
        """Drop registry entries whose cursor line is no longer on the axes."""
        alive = []
        for e in self._time_axis_registry:
            ax, line = e.get('ax'), e.get('cursor_line')
            try:
                if ax is None or line is None or line not in ax.lines:
                    continue
                alive.append(e)
            except Exception:
                continue
        self._time_axis_registry = alive

    def _register_time_axis(self, ax, logname, t0=None, t1=None):
        """Register a time-series axis for global cursor/interval updates."""
        if ax is None:
            return
        self._prune_time_axis_registry()
        # Drop stale entries for this axes
        self._time_axis_registry = [e for e in self._time_axis_registry if e.get('ax') is not ax]
        
        if t0 is None or t1 is None:
            xlim = ax.get_xlim()
            t0 = xlim[0] if t0 is None else t0
            t1 = xlim[1] if t1 is None else t1
        if t1 <= t0:
            t1 = t0 + 1e-6
        
        x0, x1 = self._effective_interval(t0, t1)
        if x1 <= x0:
            x1 = x0 + 1e-6
        ax.set_xlim(x0, x1)
        
        cursor_t = min(max(self.global_t_min, x0), x1)
        line = ax.axvline(
            cursor_t, color='#d50000', linewidth=1.25, alpha=0.9, zorder=25
        )
        self._time_axis_registry.append({
            'ax': ax,
            'cursor_line': line,
            'logname': logname,
            't0': float(t0),
            't1': float(t1),
        })

    def _broadcast_global_time(self):
        """Lightweight scrub: move cursor lines + map markers only."""
        canvases = set()
        for entry in list(self._time_axis_registry):
            ax = entry.get('ax')
            line = entry.get('cursor_line')
            if ax is None or line is None:
                continue
            try:
                x0, x1 = self._effective_interval(entry['t0'], entry['t1'])
                t = min(max(self.global_t_min, x0), x1)
                line.set_xdata([t, t])
                if ax.figure is not None and ax.figure.canvas is not None:
                    canvases.add(ax.figure.canvas)
            except Exception:
                continue
        for canvas in canvases:
            try:
                canvas.draw_idle()
            except Exception:
                pass
        self._sync_maps_to_global_time()

    def _broadcast_interval_change(self):
        """Apply interval xlim to registered axes, sync maps, refresh Overview."""
        # Keep cursor inside interval
        if self.interval_start is not None and self.interval_end is not None:
            self._set_global_time(
                min(max(self.global_t_min, self.interval_start), self.interval_end),
                broadcast=False,
            )
        
        canvases = set()
        for entry in list(self._time_axis_registry):
            ax = entry.get('ax')
            line = entry.get('cursor_line')
            if ax is None:
                continue
            try:
                x0, x1 = self._effective_interval(entry['t0'], entry['t1'])
                if x1 <= x0:
                    x1 = x0 + 1e-6
                ax.set_xlim(x0, x1)
                t = min(max(self.global_t_min, x0), x1)
                if line is not None:
                    line.set_xdata([t, t])
                if ax.figure is not None and ax.figure.canvas is not None:
                    canvases.add(ax.figure.canvas)
            except Exception:
                continue
        for canvas in canvases:
            try:
                canvas.draw_idle()
            except Exception:
                pass
        
        self._apply_interval_to_maps()
        self._sync_maps_to_global_time()
        self._refresh_overview_panes()

    def _refresh_overview_panes(self):
        view = self.analysis_splits.get('overview')
        if not view:
            return
        for pane in view['panes']:
            logname = pane['log_var'].get()
            if logname:
                self.update_overview(logname, container=pane['content'])

    def _sync_maps_to_global_time(self):
        """Move map markers / local scales to the global cursor."""
        view = self.analysis_splits.get('map')
        if not view:
            return
        self._syncing_global_time = True
        try:
            for pane in view['panes']:
                m = pane.get('map')
                if not m or m.get('time_minutes') is None or m.get('marker') is None:
                    continue
                times = m['time_minutes']
                t = min(max(self.global_t_min, float(times[0])), float(times[-1]))
                idx = int(np.searchsorted(times, t, side='left'))
                idx = min(max(idx, 0), len(m['xs']) - 1)
                m['marker'].set_data([m['xs'][idx]], [m['ys'][idx]])
                m['time_var'].set(float(times[idx]))
                m['time_label'].configure(text=self.format_minutes(times[idx]))
                if m.get('canvas') is not None:
                    m['canvas'].draw_idle()
        finally:
            self._syncing_global_time = False

    def _apply_interval_to_maps(self):
        """Emphasize the in-interval path segment on each map pane."""
        view = self.analysis_splits.get('map')
        if not view:
            return
        for pane in view['panes']:
            m = pane.get('map')
            if not m or m.get('time_minutes') is None or m.get('ax') is None:
                continue
            times = m['time_minutes']
            xs, ys = m['xs'], m['ys']
            # Remove previous highlight artist
            highlight = m.get('interval_path')
            if highlight is not None:
                try:
                    highlight.remove()
                except Exception:
                    pass
                m['interval_path'] = None
            
            if self.interval_start is None or self.interval_end is None:
                if m.get('canvas') is not None:
                    m['canvas'].draw_idle()
                continue
            
            mask = (times >= self.interval_start) & (times <= self.interval_end)
            if not np.any(mask):
                continue
            (line,) = m['ax'].plot(
                xs[mask], ys[mask],
                color='#ff6f00', linewidth=4.0, alpha=0.95,
                label='Interval', zorder=11,
            )
            m['interval_path'] = line
            if m.get('canvas') is not None:
                m['canvas'].draw_idle()

    def _on_loaded_log_picked(self, event=None):
        """Selecting a loaded log in the toolbar focuses it for analysis tabs."""
        name = self.loaded_logs_var.get()
        if not name or name == "(none)":
            return
        # Strip optional status suffix from display labels
        if name.endswith(" (params only)"):
            name = name[: -len(" (params only)")]
        if name not in self.ulog_files:
            return
        path = self.ulog_paths.get(name)
        if path:
            self.file_path.set(path)
            self.ulog_file = path
        if self.ulog_files.get(name):
            self.active_analysis_log = name
            self._sync_analysis_log_selectors(name)
            self._update_global_timeline_bar()
            # Refresh only primary panes to the picked log
            for key, view in self.analysis_splits.items():
                primary = self._primary_pane(key)
                if primary is not None:
                    primary['log_var'].set(name)
                    self._refresh_split_pane(key, primary)

    def create_main_pane(self):
        """Create the main pane with tabs for plots and parameters"""
        main_pane = ttk.Frame(self.main_frame)
        main_pane.pack(fill=tk.BOTH, expand=True)
        
        self.notebook = ttk.Notebook(main_pane)
        self.notebook.pack(fill=tk.BOTH, expand=True)
        
        # --- Overview (default post-flight summary) ---
        self.overview_tab = self._create_analysis_split_tab("overview", "Overview", kind="overview")
        
        # --- Data Plots tab (topic picker lives here) ---
        self.plots_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.plots_tab, text="Data Plots")
        
        plots_split = tk.PanedWindow(self.plots_tab, orient=tk.HORIZONTAL, sashrelief=tk.RAISED)
        plots_split.pack(fill=tk.BOTH, expand=True)
        
        plotting_frame = ttk.LabelFrame(plots_split, text="Data Plotting")
        plots_split.add(plotting_frame, minsize=180)
        
        self.ulog_filter = tk.StringVar()
        ttk.Label(plotting_frame, text="Filter by ULog file:").pack(pady=(5, 0), padx=5, anchor="w")
        self.ulog_dropdown = ttk.Combobox(plotting_frame, textvariable=self.ulog_filter, state="readonly")
        self.ulog_dropdown.pack(pady=5, padx=5, fill=tk.X)
        self.ulog_dropdown.bind("<<ComboboxSelected>>", self.update_topic_list)
        
        self.csv_listbox = tk.Listbox(plotting_frame, selectmode=tk.MULTIPLE)
        self.csv_listbox.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        button_frame = ttk.Frame(plotting_frame)
        button_frame.pack(fill=tk.X, padx=5, pady=5)
        ttk.Button(button_frame, text="Add to Plot", command=self.add_to_plot).pack(side=tk.LEFT, padx=2)
        ttk.Button(button_frame, text="Clear Plots", command=self.clear_plots).pack(side=tk.LEFT, padx=2)
        
        self.plots_canvas_host = ttk.Frame(plots_split)
        plots_split.add(self.plots_canvas_host, minsize=400)
        
        plots_top = ttk.Frame(self.plots_canvas_host)
        plots_top.pack(fill=tk.X)
        self.edit_button = ttk.Button(plots_top, text="Edit Plots", command=self.edit_plots)
        self.edit_button.pack(side=tk.LEFT, pady=5, padx=5)
        
        self.plots_figure_frame = ttk.Frame(self.plots_canvas_host)
        self.plots_figure_frame.pack(fill=tk.BOTH, expand=True)
        
        # --- Parameters tab ---
        self.parameters_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.parameters_tab, text="Parameters")
        
        self.parameter_search_var = tk.StringVar()
        self.parameter_search_var.trace_add("write", self.filter_parameters)
        
        search_frame = ttk.Frame(self.parameters_tab)
        search_frame.pack(fill=tk.X, padx=5, pady=5)
        ttk.Label(search_frame, text="Search Parameters:").pack(side=tk.LEFT, padx=5)
        self.parameter_search_entry = ttk.Entry(search_frame, textvariable=self.parameter_search_var, width=30)
        self.parameter_search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        
        compare_frame = ttk.Frame(self.parameters_tab)
        compare_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=5, pady=5)
        ttk.Button(compare_frame, text="Refresh Parameters", command=self.compare_parameters).pack(side=tk.LEFT, padx=5)
        
        tree_frame = ttk.Frame(self.parameters_tab)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        
        self.compare_tree = ttk.Treeview(tree_frame, columns=("Parameter"), show="headings")
        self.compare_tree.heading("Parameter", text="Parameter")
        vsb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.compare_tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient=tk.HORIZONTAL, command=self.compare_tree.xview)
        self.compare_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.compare_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        
        # --- Position / Map (split-capable) ---
        self.position_tab = self._create_analysis_split_tab("position", "Position", kind="position")
        self.map_tab = self._create_analysis_split_tab("map", "Map", kind="map")
        self.initialize_default_map()
        
        # Curated tabs (Vibrations / Battery / Radio) are created in create_default_plot_tabs

    def create_default_plot_tabs(self):
        """Create curated multi-plot tabs (Vibrations, Battery, Radio)."""
        self.default_plot_frames = {}
        for plot_name in self.default_plots.keys():
            tab = self._create_analysis_split_tab(plot_name, plot_name, kind="curated")
            self.default_plot_frames[plot_name] = {
                'tab': tab,
                'split_key': plot_name,
            }

    def _create_analysis_split_tab(self, key, title, kind):
        """Create a notebook tab with a horizontal PanedWindow and a + button to add panes."""
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text=title)
        
        body = ttk.Frame(tab)
        body.pack(fill=tk.BOTH, expand=True)
        
        paned = tk.PanedWindow(body, orient=tk.HORIZONTAL, sashrelief=tk.RAISED, sashwidth=6)
        paned.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        
        add_col = ttk.Frame(body, width=36)
        add_col.pack(side=tk.RIGHT, fill=tk.Y, padx=(4, 2), pady=4)
        add_col.pack_propagate(False)
        add_btn = ttk.Button(add_col, text="+", width=3, command=lambda k=key: self._add_split_pane(k))
        add_btn.pack(anchor=tk.N, pady=2)
        
        view = {
            'key': key,
            'kind': kind,
            'tab': tab,
            'paned': paned,
            'add_btn': add_btn,
            'panes': [],
        }
        self.analysis_splits[key] = view
        self._add_split_pane(key, is_primary=True)
        return tab

    def _converted_log_names(self):
        converted = [n for n, topics in self.ulog_files.items() if topics]
        return converted if converted else list(self.ulog_files.keys())

    def _add_split_pane(self, key, is_primary=False):
        """Add a new log pane to a split view (Position / Map / Vibrations)."""
        view = self.analysis_splits[key]
        if len(view['panes']) >= self.MAX_SPLIT_PANES:
            messagebox.showinfo("Split view", f"Maximum of {self.MAX_SPLIT_PANES} panes reached.")
            return
        
        self._split_pane_seq += 1
        pane_id = self._split_pane_seq
        
        frame = ttk.Frame(view['paned'])
        view['paned'].add(frame, minsize=220)
        
        header = ttk.Frame(frame)
        header.pack(fill=tk.X, padx=4, pady=4)
        ttk.Label(header, text="Log:").pack(side=tk.LEFT, padx=(0, 4))
        
        log_var = tk.StringVar()
        log_combo = ttk.Combobox(header, textvariable=log_var, state="readonly", width=28)
        log_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        log_combo["values"] = self._converted_log_names()
        
        close_btn = None
        if not is_primary:
            close_btn = ttk.Button(
                header, text="x", width=3,
                command=lambda k=key, pid=pane_id: self._remove_split_pane(k, pid),
            )
            close_btn.pack(side=tk.RIGHT, padx=(4, 0))
        
        content = ttk.Frame(frame)
        content.pack(fill=tk.BOTH, expand=True)
        
        pane = {
            'id': pane_id,
            'frame': frame,
            'header': header,
            'log_var': log_var,
            'log_combo': log_combo,
            'close_btn': close_btn,
            'content': content,
            'map': None,
        }
        
        if view['kind'] == 'map':
            pane['map'] = self._build_map_pane_chrome(content)
        
        log_combo.bind(
            "<<ComboboxSelected>>",
            lambda e, k=key, p=pane: self._on_split_pane_log_selected(k, p),
        )
        
        # Default selection: active log, else last converted, else empty
        names = self._converted_log_names()
        if names:
            choice = self.active_analysis_log if self.active_analysis_log in names else names[-1]
            # For extra panes, prefer a different log if available
            if not is_primary and len(names) > 1:
                primary_log = view['panes'][0]['log_var'].get() if view['panes'] else ""
                for n in reversed(names):
                    if n != primary_log:
                        choice = n
                        break
            log_var.set(choice)
        
        view['panes'].append(pane)
        self._refresh_split_pane(key, pane)
        return pane

    def _remove_split_pane(self, key, pane_id):
        """Remove a non-primary split pane."""
        view = self.analysis_splits.get(key)
        if not view:
            return
        pane = next((p for p in view['panes'] if p['id'] == pane_id), None)
        if pane is None or pane is view['panes'][0]:
            return
        if pane.get('map'):
            self._teardown_map_pane(pane)
        try:
            view['paned'].forget(pane['frame'])
        except tk.TclError:
            pass
        try:
            pane['frame'].destroy()
        except tk.TclError:
            pass
        view['panes'] = [p for p in view['panes'] if p['id'] != pane_id]

    def _on_split_pane_log_selected(self, key, pane):
        logname = pane['log_var'].get()
        if not logname:
            return
        # Primary pane drives the global "active" log
        if self.analysis_splits[key]['panes'] and pane is self.analysis_splits[key]['panes'][0]:
            self.active_analysis_log = logname
        self._refresh_split_pane(key, pane)

    def _refresh_split_pane(self, key, pane):
        """Render the selected log into one pane."""
        view = self.analysis_splits[key]
        logname = pane['log_var'].get()
        kind = view['kind']
        if not logname:
            for w in pane['content'].winfo_children():
                if kind == 'map':
                    break
                w.destroy()
            if kind == 'map':
                self._show_map_message_in_pane(pane, "Select a converted log to show the map.")
            return
        
        if kind == 'overview':
            self.update_overview(logname, container=pane['content'])
        elif kind == 'position':
            self.update_position_plots(logname, container=pane['content'])
        elif kind == 'curated':
            self.update_default_plots(logname, plot_name=key, container=pane['content'])
        elif kind == 'map':
            try:
                self.update_map(logname, pane=pane)
            except Exception as e:
                print(f"Error updating map: {e}")
                import traceback
                traceback.print_exc()
                self._show_map_message_in_pane(pane, f"Error updating map:\n{str(e)}")

    def _refresh_all_split_panes(self, key=None):
        keys = [key] if key else list(self.analysis_splits.keys())
        for k in keys:
            for pane in self.analysis_splits[k]['panes']:
                self._refresh_split_pane(k, pane)

    def _primary_pane(self, key):
        panes = self.analysis_splits.get(key, {}).get('panes') or []
        return panes[0] if panes else None

    def select_ulog_file(self):
        """Select a ULog file and show its parameters immediately"""
        file_path = filedialog.askopenfilename(filetypes=[("ULog files", "*.ulg")])
        if file_path:
            self.file_path.set(file_path)
            self.ulog_file = file_path
            logname = os.path.splitext(os.path.basename(file_path))[0]
            self.ulog_paths[logname] = os.path.abspath(file_path)
            # Register so Parameters can show even before Convert
            if logname not in self.ulog_files:
                self.ulog_files[logname] = []
            self._refresh_loaded_logs_ui()
            self.compare_parameters(show_warnings=False)

    def convert_ulog(self):
        """Convert the selected ULog file to CSV"""
        ulog_file = self.file_path.get()
        if not ulog_file:
            messagebox.showerror("Error", "Please select a ULog file first!")
            return
        
        try:
            ulog2csv.convert_ulog2csv(
                ulog_file_name=ulog_file,
                messages=None,
                output=self.csv_output_dir,
                delimiter=',',
                time_s=0,
                time_e=None
            )
            messagebox.showinfo("Success", "Conversion completed successfully!")
            
            logname = os.path.splitext(os.path.basename(ulog_file))[0]
            self.ulog_paths[logname] = os.path.abspath(ulog_file)
            
            csv_files = [f for f in os.listdir(self.csv_output_dir) 
                        if f.startswith(logname + "_") and f.endswith(".csv")]
            
            topics = []
            for csv_file in csv_files:
                topic = csv_file[len(logname)+1:-4]
                topics.append(topic)
            
            self.ulog_files[logname] = sorted(set(topics))
            self.active_analysis_log = logname
            self._events_cache.pop(logname, None)
            self._log_duration.pop(logname, None)
            
            self._refresh_loaded_logs_ui()
            self._sync_analysis_log_selectors(logname)
            self._update_global_timeline_bar()
            
            self.ulog_dropdown["values"] = list(self.ulog_files.keys())
            if logname in self.ulog_dropdown["values"]:
                self.ulog_filter.set(logname)
            elif self.ulog_dropdown["values"]:
                self.ulog_dropdown.current(0)
            self.update_topic_list()
            
            # Point primary panes at the new log and refresh them
            for key, view in self.analysis_splits.items():
                primary = self._primary_pane(key)
                if primary is not None:
                    primary['log_var'].set(logname)
                    primary['log_combo']["values"] = self._converted_log_names()
                    self._refresh_split_pane(key, primary)
            
            # Auto-show parameters (no need to click Compare)
            self.compare_parameters(show_warnings=False)
            
        except Exception as e:
            messagebox.showerror("Conversion Error", str(e))

    def _refresh_loaded_logs_ui(self):
        """Refresh the toolbar loaded-logs combobox"""
        labels = []
        for name in self.ulog_files.keys():
            suffix = "" if self.ulog_files[name] else " (params only)"
            labels.append(f"{name}{suffix}")
        
        if labels:
            self.loaded_logs_combo["values"] = labels
            # Prefer the active / last log in the combo
            preferred = None
            if self.active_analysis_log:
                for label in labels:
                    if label.startswith(self.active_analysis_log):
                        preferred = label
                        break
            self.loaded_logs_var.set(preferred or labels[-1])
            n = len(labels)
            self.loaded_logs_count.configure(text=f"{n} log{'s' if n != 1 else ''}")
        else:
            self.loaded_logs_combo["values"] = ["(none)"]
            self.loaded_logs_var.set("(none)")
            self.loaded_logs_count.configure(text="0 logs")

    def _sync_analysis_log_selectors(self, selected=None):
        """Keep all split-pane log comboboxes populated with converted logs."""
        combo_values = self._converted_log_names()
        
        if selected and selected in combo_values:
            choice = selected
        elif self.active_analysis_log in combo_values:
            choice = self.active_analysis_log
        elif combo_values:
            choice = combo_values[-1]
        else:
            choice = ""
        
        self.active_analysis_log = choice or None
        
        for view in self.analysis_splits.values():
            for i, pane in enumerate(view['panes']):
                pane['log_combo']["values"] = combo_values
                current = pane['log_var'].get()
                if current not in combo_values:
                    # Primary pane follows active choice; others keep a valid pick if possible
                    pane['log_var'].set(choice if i == 0 else (combo_values[-1] if combo_values else ""))
        
        self._update_global_timeline_bar()

    def update_topic_list(self, event=None):
        """Update the listbox with topic names for the selected log"""
        selected_log = self.ulog_filter.get()
        self.csv_listbox.delete(0, tk.END)
        
        if selected_log in self.ulog_files:
            for topic in self.ulog_files[selected_log]:
                self.csv_listbox.insert(tk.END, topic)

    def get_x_axis_column(self, df):
        """Determine the appropriate x-axis column for a DataFrame"""
        if 'timestamp' in df.columns:
            return 'timestamp'
        elif 'timestamp_sample' in df.columns:
            return 'timestamp_sample'
        else:
            return df.columns[0]  # Use the first column as a fallback

    def convert_timestamp_to_minutes(self, timestamps):
        """Convert timestamps (in microseconds) to minutes relative to the first timestamp"""
        first_timestamp = timestamps.iloc[0]  # Get the first timestamp
        return (timestamps - first_timestamp) / 1e6 / 60  # Convert to minutes

    def format_minutes(self, minutes):
        """Format minutes as a string (e.g., 0:00, 0:10, 0:20)"""
        return f"{int(minutes // 1):02d}:{int((minutes % 1) * 60):02d}"

    def add_to_plot(self):
        """Add selected topics to the plot with column selection"""
        selected_indices = self.csv_listbox.curselection()
        if not selected_indices:
            messagebox.showwarning("Warning", "Please select at least one topic to plot!")
            return
        
        selected_topics = [self.csv_listbox.get(i) for i in selected_indices]
        selected_log = self.ulog_filter.get()
        
        # Get the number of existing plots
        num_existing_plots = len(self.plotted_topics)
        num_new_plots = len(selected_topics)
        total_plots = max(num_existing_plots + num_new_plots, 1)
        
        # Size figure so each subplot stays readable (scroll if needed)
        fig_h = max(self.MIN_CURATED_SUBPLOT_INCH * total_plots, 4.0)
        if self.current_figure is None:
            self.current_figure = Figure(figsize=(10, fig_h), dpi=100)
        else:
            self.current_figure.clear()
            self.current_figure.set_size_inches(10, fig_h, forward=True)
        
        self.current_axes = self.current_figure.subplots(total_plots, 1, squeeze=False)
        
        # Replot existing plots
        for idx, plot_id in enumerate(self.plotted_topics):
            logname, topic = plot_id.split("_", 1)
            csv_filename = f"{logname}_{topic}.csv"
            csv_path = os.path.join(self.csv_output_dir, csv_filename)
            
            if not os.path.exists(csv_path):
                messagebox.showerror("Error", f"CSV file not found: {csv_filename}")
                return
            
            df = pd.read_csv(csv_path)
            time_col = self.get_x_axis_column(df)  # Determine the x-axis column
            
            # Convert timestamps to minutes relative to the first timestamp
            if time_col in ['timestamp', 'timestamp_sample']:
                time_values = self.convert_timestamp_to_minutes(df[time_col])
            else:
                time_values = df[time_col]  # Fallback to the first column
            
            ax = self.current_axes[idx][0]
            config = self.plot_configs[plot_id]
            for col in config["columns"]:
                ax.plot(time_values, df[col], label=col, marker='o', linestyle='dashed')
            
            self._format_time_axis(ax, time_values)
            self._apply_event_overlay(ax, logname)
            
            ax.set_title(f"{logname}: {topic}")
            ax.legend()
            ax.grid(True)
            ax._ulog_meta = {
                'logname': logname,
                't0': float(np.asarray(time_values)[0]),
                't1': float(np.asarray(time_values)[-1]),
            }
        
        # Plot new selected topics
        for idx, topic in enumerate(selected_topics):
            plot_id = f"{selected_log}_{topic}"
            if plot_id in self.plotted_topics:
                continue
            
            try:
                csv_filename = f"{selected_log}_{topic}.csv"
                csv_path = os.path.join(self.csv_output_dir, csv_filename)
                
                if not os.path.exists(csv_path):
                    messagebox.showerror("Error", f"CSV file not found: {csv_filename}")
                    return
                
                df = pd.read_csv(csv_path)
                time_col = self.get_x_axis_column(df)
                
                if time_col in ['timestamp', 'timestamp_sample']:
                    time_values = self.convert_timestamp_to_minutes(df[time_col])
                else:
                    time_values = df[time_col]
                
                selected_columns = self.show_column_selection_dialog(df.columns, time_col)
                if not selected_columns:
                    continue
                
                ax = self.current_axes[num_existing_plots + idx][0]
                for col in selected_columns:
                    ax.plot(time_values, df[col], label=col, marker='o', linestyle='dashed')
                
                self._format_time_axis(ax, time_values)
                self._apply_event_overlay(ax, selected_log)
                
                ax.set_title(f"{selected_log}: {topic}")
                ax.legend()
                ax.grid(True)
                ax._ulog_meta = {
                    'logname': selected_log,
                    't0': float(np.asarray(time_values)[0]),
                    't1': float(np.asarray(time_values)[-1]),
                }
                
                self.plotted_topics.add(plot_id)
                self.plot_configs[plot_id] = {"columns": selected_columns, "ax": ax}
            except Exception as e:
                messagebox.showerror("Plot Error", f"Error plotting {topic}:\n{str(e)}")
                return
        
        self.current_figure.tight_layout()
        self._embed_data_plots()

    def edit_plots(self):
        """Edit the columns of all currently plotted topics"""
        if not self.plotted_topics:
            messagebox.showwarning("Warning", "No plots to edit!")
            return
        
        # Create a dialog to edit columns for all plots
        dialog = tk.Toplevel(self.root)
        dialog.title("Edit Plots")
        
        # Create a notebook for organizing topics
        notebook = ttk.Notebook(dialog)
        notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        # Dictionary to store checkbox states for each topic
        self.column_vars = {}
        
        # Add a tab for each plotted topic
        for plot_id in self.plotted_topics:
            logname, topic = plot_id.split("_", 1)
            csv_filename = f"{logname}_{topic}.csv"
            csv_path = os.path.join(self.csv_output_dir, csv_filename)
            
            if not os.path.exists(csv_path):
                messagebox.showerror("Error", f"CSV file not found: {csv_filename}")
                return
            
            df = pd.read_csv(csv_path)
            time_col = self.get_x_axis_column(df)  # Determine the x-axis column
            
            # Create a frame for the topic's checkboxes
            frame = ttk.Frame(notebook)
            notebook.add(frame, text=f"{logname}: {topic}")
            
            # Add checkboxes for each column (excluding time_col and timestamp_sample)
            self.column_vars[plot_id] = {}
            for col in df.columns:
                if col != time_col and col != 'timestamp_sample':
                    var = tk.BooleanVar(value=col in self.plot_configs[plot_id]["columns"])
                    self.column_vars[plot_id][col] = var
                    checkbox = ttk.Checkbutton(frame, text=col, variable=var)
                    checkbox.pack(anchor=tk.W)
        
        # Add OK and Cancel buttons
        button_frame = ttk.Frame(dialog)
        button_frame.pack(fill=tk.X, padx=10, pady=10)
        
        def on_ok():
            # Update plot configurations with selected columns
            for plot_id in self.plotted_topics:
                selected_columns = [col for col, var in self.column_vars[plot_id].items() if var.get()]
                if selected_columns:
                    self.plot_configs[plot_id]["columns"] = selected_columns
            dialog.destroy()
            self.replot_all()  # Replot all plots with updated configurations
        
        def on_cancel():
            dialog.destroy()
        
        ttk.Button(button_frame, text="OK", command=on_ok).pack(side=tk.LEFT, padx=5)
        ttk.Button(button_frame, text="Cancel", command=on_cancel).pack(side=tk.LEFT, padx=5)
        
        # Wait for the dialog to close
        dialog.transient(self.root)
        dialog.grab_set()
        self.root.wait_window(dialog)

    def replot_all(self):
        """Replot all existing plots"""
        if not self.plotted_topics:
            return
        
        n = len(self.plotted_topics)
        fig_h = max(self.MIN_CURATED_SUBPLOT_INCH * n, 4.0)
        if self.current_figure is None:
            self.current_figure = Figure(figsize=(10, fig_h), dpi=100)
        else:
            self.current_figure.clear()
            self.current_figure.set_size_inches(10, fig_h, forward=True)
        self.current_axes = self.current_figure.subplots(n, 1, squeeze=False)
        
        # Replot all plots
        for idx, plot_id in enumerate(self.plotted_topics):
            logname, topic = plot_id.split("_", 1)
            csv_filename = f"{logname}_{topic}.csv"
            csv_path = os.path.join(self.csv_output_dir, csv_filename)
            
            if not os.path.exists(csv_path):
                messagebox.showerror("Error", f"CSV file not found: {csv_filename}")
                return
            
            df = pd.read_csv(csv_path)
            time_col = self.get_x_axis_column(df)  # Determine the x-axis column
            
            # Convert timestamps to minutes relative to the first timestamp
            if time_col in ['timestamp', 'timestamp_sample']:
                time_values = self.convert_timestamp_to_minutes(df[time_col])
            else:
                time_values = df[time_col]  # Fallback to the first column
            
            ax = self.current_axes[idx][0]
            config = self.plot_configs[plot_id]
            for col in config["columns"]:
                ax.plot(time_values, df[col], label=col, marker='o', linestyle='dashed')
            
            self._format_time_axis(ax, time_values)
            self._apply_event_overlay(ax, logname)
            
            ax.set_title(f"{logname}: {topic}")
            ax.legend()
            ax.grid(True)
            ax._ulog_meta = {
                'logname': logname,
                't0': float(np.asarray(time_values)[0]),
                't1': float(np.asarray(time_values)[-1]),
            }
        
        self.current_figure.tight_layout()
        self._embed_data_plots()

    def _embed_data_plots(self):
        """Embed the current Data Plots figure with vertical scroll when needed."""
        for widget in self.plots_figure_frame.winfo_children():
            widget.destroy()
        self.canvas = None
        self.toolbar = None
        
        if self.current_figure is None:
            return
        
        # Ensure readable height before embedding into the scroll host
        n = max(len(self.plotted_topics), 1)
        fig_h = max(self.MIN_CURATED_SUBPLOT_INCH * n, 4.0)
        self.current_figure.set_size_inches(10, fig_h, forward=True)
        self._embed_scrollable_figure(self.current_figure, self.plots_figure_frame)
        
        self._unregister_figure(self.current_figure)
        if getattr(self, 'current_axes', None) is not None:
            for row in self.current_axes:
                ax = row[0]
                meta = getattr(ax, '_ulog_meta', None)
                if not meta:
                    continue
                self._register_time_axis(ax, meta['logname'], meta['t0'], meta['t1'])

    def show_column_selection_dialog(self, columns, time_col, initial_selections=None):
        """Show a dialog to select columns for plotting"""
        dialog = tk.Toplevel(self.root)
        dialog.title("Select Columns to Plot")
        
        # Create a frame for checkboxes
        checkbox_frame = ttk.Frame(dialog)
        checkbox_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        # Dictionary to store checkbox states
        self.column_vars = {}
        
        # Add checkboxes for each column (excluding time_col and timestamp_sample)
        for col in columns:
            if col != time_col and col != 'timestamp_sample':
                var = tk.BooleanVar(value=col in initial_selections if initial_selections else True)
                self.column_vars[col] = var
                checkbox = ttk.Checkbutton(checkbox_frame, text=col, variable=var)
                checkbox.pack(anchor=tk.W)
        
        # Add OK and Cancel buttons
        button_frame = ttk.Frame(dialog)
        button_frame.pack(fill=tk.X, padx=10, pady=10)
        
        selected_columns = []
        
        def on_ok():
            nonlocal selected_columns
            selected_columns = [col for col, var in self.column_vars.items() if var.get()]
            dialog.destroy()
        
        def on_cancel():
            nonlocal selected_columns
            selected_columns = []
            dialog.destroy()
        
        ttk.Button(button_frame, text="OK", command=on_ok).pack(side=tk.LEFT, padx=5)
        ttk.Button(button_frame, text="Cancel", command=on_cancel).pack(side=tk.LEFT, padx=5)
        
        # Wait for the dialog to close
        dialog.transient(self.root)
        dialog.grab_set()
        self.root.wait_window(dialog)
        
        return selected_columns

    def convert_quaternion_to_rpy(self, df):
        """Convert quaternion (q[0], q[1], q[2], q[3]) to roll, pitch, yaw"""
        q0 = df['q[0]']
        q1 = df['q[1]']
        q2 = df['q[2]']
        q3 = df['q[3]']
        
        # Roll (x-axis rotation)
        roll = np.arctan2(2 * (q0 * q1 + q2 * q3), 1 - 2 * (q1**2 + q2**2))
        
        # Pitch (y-axis rotation)
        pitch = np.arcsin(2 * (q0 * q2 - q3 * q1))
        
        # Yaw (z-axis rotation)
        yaw = np.arctan2(2 * (q0 * q3 + q1 * q2), 1 - 2 * (q2**2 + q3**2))
        
        # Convert radians to degrees for better readability
        roll = np.degrees(roll)
        pitch = np.degrees(pitch)
        yaw = np.degrees(yaw)
        
        return roll, pitch, yaw

    def _csv_path(self, logname, topic):
        return os.path.join(self.csv_output_dir, f"{logname}_{topic}.csv")

    def _read_topic_csv(self, logname, topic, usecols=None):
        path = self._csv_path(logname, topic)
        if not os.path.exists(path):
            return None
        try:
            if usecols is not None:
                # Keep only columns that exist
                header = pd.read_csv(path, nrows=0).columns.tolist()
                cols = [c for c in usecols if c in header]
                if 'timestamp' in header and 'timestamp' not in cols:
                    cols = ['timestamp'] + cols
                if not cols:
                    return None
                return pd.read_csv(path, usecols=cols)
            return pd.read_csv(path)
        except Exception as e:
            print(f"Warning: failed reading {topic}: {e}")
            return None

    def _topic_time_minutes(self, df):
        time_col = self.get_x_axis_column(df)
        if time_col in ['timestamp', 'timestamp_sample']:
            return self.convert_timestamp_to_minutes(df[time_col]), time_col
        return df[time_col], time_col

    def _nav_state_name(self, state):
        try:
            state = int(state)
        except (TypeError, ValueError):
            return str(state)
        return self.NAV_STATE_NAMES.get(state, f"NAV_{state}")

    def get_flight_events(self, logname):
        """Build a compact list of flight events for overlays and Overview."""
        if logname in self._events_cache:
            return self._events_cache[logname]
        
        events = []
        
        def add_event(t_min, label, kind, color):
            events.append({
                't_min': float(t_min),
                'label': label,
                'kind': kind,
                'color': color,
            })
        
        status = self._read_topic_csv(
            logname, 'vehicle_status_0',
            usecols=['timestamp', 'nav_state', 'arming_state', 'failsafe',
                     'rc_signal_lost', 'data_link_lost', 'geofence_violated',
                     'engine_failure', 'mission_failure'],
        )
        if status is not None and len(status):
            t, _ = self._topic_time_minutes(status)
            # Arming edges (PX4: 1=INIT, 2=STANDBY, 3=ARMED)
            if 'arming_state' in status.columns:
                armed = (status['arming_state'].astype(int) == 3).to_numpy()
                for i in np.where(np.diff(armed.astype(int)) == 1)[0]:
                    add_event(t.iloc[i + 1], "Armed", "arm", "#2e7d32")
                for i in np.where(np.diff(armed.astype(int)) == -1)[0]:
                    add_event(t.iloc[i + 1], "Disarmed", "arm", "#616161")
            for col, label, color in (
                ('failsafe', 'Failsafe', '#c62828'),
                ('rc_signal_lost', 'RC lost', '#e65100'),
                ('data_link_lost', 'Data link lost', '#ef6c00'),
                ('geofence_violated', 'Geofence', '#ad1457'),
                ('engine_failure', 'Engine failure', '#b71c1c'),
                ('mission_failure', 'Mission failure', '#880e4f'),
            ):
                if col not in status.columns:
                    continue
                vals = status[col].fillna(0).astype(int).to_numpy()
                for i in np.where(np.diff(vals) == 1)[0]:
                    add_event(t.iloc[i + 1], label, "failsafe", color)
        
        land = self._read_topic_csv(
            logname, 'vehicle_land_detected_0',
            usecols=['timestamp', 'landed', 'maybe_landed', 'freefall'],
        )
        if land is not None and len(land):
            t, _ = self._topic_time_minutes(land)
            if 'landed' in land.columns:
                landed = land['landed'].fillna(0).astype(int).to_numpy()
                for i in np.where(np.diff(landed) == -1)[0]:
                    add_event(t.iloc[i + 1], "Takeoff (in air)", "flight", "#1565c0")
                for i in np.where(np.diff(landed) == 1)[0]:
                    add_event(t.iloc[i + 1], "Landed", "flight", "#1565c0")
            if 'freefall' in land.columns:
                ff = land['freefall'].fillna(0).astype(int).to_numpy()
                for i in np.where(np.diff(ff) == 1)[0]:
                    add_event(t.iloc[i + 1], "Freefall detected", "failsafe", "#b71c1c")
        
        fd = self._read_topic_csv(
            logname, 'failure_detector_status_0',
            usecols=['timestamp', 'fd_roll', 'fd_pitch', 'fd_alt', 'fd_ext',
                     'fd_arm_escs', 'fd_high_wind', 'fd_battery', 'fd_imbalanced_prop'],
        )
        if fd is not None and len(fd):
            t, _ = self._topic_time_minutes(fd)
            for col, label in (
                ('fd_roll', 'FD: roll'),
                ('fd_pitch', 'FD: pitch'),
                ('fd_alt', 'FD: altitude'),
                ('fd_ext', 'FD: external'),
                ('fd_arm_escs', 'FD: arm ESCs'),
                ('fd_high_wind', 'FD: high wind'),
                ('fd_battery', 'FD: battery'),
                ('fd_imbalanced_prop', 'FD: imbalanced prop'),
            ):
                if col not in fd.columns:
                    continue
                vals = fd[col].fillna(0).astype(int).to_numpy()
                for i in np.where(np.diff(vals) == 1)[0]:
                    add_event(t.iloc[i + 1], label, "failsafe", "#c62828")
        
        events.sort(key=lambda e: e['t_min'])
        # De-duplicate near-identical labels at same timestamp
        deduped = []
        seen = set()
        for ev in events:
            key = (round(ev['t_min'], 3), ev['label'])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(ev)
        
        self._events_cache[logname] = deduped
        return deduped

    def _apply_event_overlay(self, ax, logname):
        """Draw event/failsafe markers on a time-series axis when overlay is enabled."""
        if not self.overlay_events_var.get():
            return
        events = self.get_flight_events(logname)
        if not events:
            return
        y0, y1 = ax.get_ylim()
        labeled = set()
        for ev in events:
            label = ev['label'] if ev['label'] not in labeled else None
            if label:
                labeled.add(ev['label'])
            ax.axvline(
                ev['t_min'], color=ev['color'], alpha=0.55, linewidth=1.0,
                linestyle='--', label=label, zorder=5,
            )
        # Keep original data limits (axvline can expand)
        ax.set_ylim(y0, y1)

    def _format_time_axis(self, ax, time_values):
        if time_values is None or len(time_values) == 0:
            return
        n = max(len(time_values) // 10, 1)
        ticks = time_values[::n]
        ax.set_xticks(ticks)
        ax.set_xticklabels([self.format_minutes(t) for t in ticks])

    def _curated_subplot_title(self, topic, fields):
        if topic == 'vehicle_imu_status_0':
            if any('clipping' in f for f in fields):
                return "IMU accel clipping"
            if any('vibration' in f or 'coning' in f for f in fields):
                return "IMU vibration metrics"
            if any(f.startswith('var_') for f in fields):
                return "IMU accel variance"
        if topic == 'sensor_gyro_fft_0':
            return "Gyro FFT peak frequencies"
        if topic == 'battery_status_0':
            if 'remaining' in fields or 'discharged_mah' in fields:
                return "Battery remaining / discharged"
            if 'temperature' in fields or 'max_cell_voltage_delta' in fields:
                return "Battery temperature / cell delta"
            if any('current' in f for f in fields):
                return "Battery current"
            if any('voltage' in f for f in fields):
                return "Battery voltage"
        if topic == 'input_rc_0':
            if 'rssi' in fields:
                return "RC RSSI"
            if 'rc_lost' in fields or 'rc_failsafe' in fields:
                return "RC lost / failsafe"
            if 'rc_lost_frame_count' in fields:
                return "RC frame counters"
        if topic == 'telemetry_status_0':
            if 'rx_message_lost_rate' in fields:
                return "Telemetry loss"
            return "Telemetry rates"
        if topic == 'manual_control_setpoint_0':
            return "Manual sticks (x/y/z/r)"
        if topic == 'system_power_0':
            return "5V rail"
        return topic

    def update_overview(self, logname, container=None):
        """Render a compact post-flight Overview for one log."""
        if container is None:
            pane = self._primary_pane('overview')
            if pane is None:
                return
            container = pane['content']
        
        for widget in container.winfo_children():
            widget.destroy()
        
        if not logname or not self.ulog_files.get(logname):
            ttk.Label(
                container,
                text="Convert a ULog to see the flight overview.",
                justify=tk.CENTER,
            ).pack(expand=True)
            return
        
        interval_active = self.interval_start is not None and self.interval_end is not None
        
        # --- Collect summary stats (restricted to interval when set) ---
        summary_rows = []
        status_full = self._read_topic_csv(
            logname, 'vehicle_status_0',
            usecols=['timestamp', 'nav_state', 'arming_state', 'failsafe',
                     'rc_signal_lost', 'data_link_lost', 'armed_time', 'takeoff_time'],
        )
        land_full = self._read_topic_csv(logname, 'vehicle_land_detected_0', usecols=['timestamp', 'landed'])
        battery = self._df_in_interval(self._read_topic_csv(
            logname, 'battery_status_0',
            usecols=['timestamp', 'voltage_filtered_v', 'voltage_v', 'remaining',
                     'discharged_mah', 'current_filtered_a'],
        ))
        gps = self._read_topic_csv(
            logname, 'sensor_gps_0',
            usecols=['timestamp', 'satellites_used', 'fix_type', 'eph', 'epv', 'jamming_state'],
        )
        if gps is None:
            gps = self._read_topic_csv(
                logname, 'vehicle_gps_position_0',
                usecols=['timestamp', 'satellites_used', 'fix_type', 'eph', 'epv'],
            )
        gps = self._df_in_interval(gps)
        local = self._df_in_interval(self._read_topic_csv(
            logname, 'vehicle_local_position_0',
            usecols=['timestamp', 'x', 'y', 'z', 'vx', 'vy', 'vz'],
        ))
        est = self._df_in_interval(self._read_topic_csv(
            logname, 'estimator_status_0',
            usecols=['timestamp', 'pos_horiz_accuracy', 'pos_vert_accuracy',
                     'mag_test_ratio', 'vel_test_ratio', 'pos_test_ratio', 'gps_check_fail_flags'],
        ))
        imu = self._df_in_interval(self._read_topic_csv(
            logname, 'vehicle_imu_status_0',
            usecols=['timestamp', 'accel_vibration_metric', 'gyro_vibration_metric',
                     'accel_clipping[0]', 'accel_clipping[1]', 'accel_clipping[2]'],
        ))
        status = self._df_in_interval(status_full)
        land = self._df_in_interval(land_full)
        
        if interval_active:
            summary_rows.append((
                "Interval",
                f"{self.format_minutes(self.interval_start)} – {self.format_minutes(self.interval_end)}",
            ))
            summary_rows.append((
                "Interval length",
                self.format_minutes(self.interval_end - self.interval_start),
            ))
        
        duration_min = None
        if status is not None and len(status):
            t, _ = self._topic_time_minutes(status)
            duration_min = float(t.iloc[-1] - t.iloc[0]) if len(t) > 1 else 0.0
            scope = "interval" if interval_active else "log"
            summary_rows.append(("Duration", f"{self.format_minutes(duration_min)} ({scope})"))
            if 'arming_state' in status.columns:
                armed_pct = 100.0 * (status['arming_state'].astype(int) == 3).mean()
                summary_rows.append(("Armed time", f"{armed_pct:.0f}% of {scope}"))
            if 'failsafe' in status.columns and status['failsafe'].fillna(0).astype(int).any():
                summary_rows.append(("Failsafe", "YES — check events"))
            else:
                summary_rows.append(("Failsafe", "No"))
            if 'nav_state' in status.columns:
                modes = status['nav_state'].astype(int)
                mode_counts = modes.value_counts()
                top = self._nav_state_name(int(mode_counts.index[0]))
                summary_rows.append(("Primary mode", top))
                unique_modes = sorted({self._nav_state_name(int(m)) for m in modes.unique()})
                summary_rows.append(("Modes used", ", ".join(unique_modes[:8])))
        elif interval_active:
            summary_rows.append(("Duration", "No samples in interval"))
        
        # Takeoff/landing from full land series, then keep only if inside interval
        if land_full is not None and 'landed' in land_full.columns and len(land_full):
            t, _ = self._topic_time_minutes(land_full)
            landed = land_full['landed'].fillna(0).astype(int).to_numpy()
            takeoffs = np.where(np.diff(landed) == -1)[0]
            landings = np.where(np.diff(landed) == 1)[0]
            if len(takeoffs):
                tt = float(t.iloc[takeoffs[0] + 1])
                if (not interval_active) or (self.interval_start <= tt <= self.interval_end):
                    summary_rows.append(("Takeoff", self.format_minutes(tt)))
            if len(landings):
                lt = float(t.iloc[landings[-1] + 1])
                if (not interval_active) or (self.interval_start <= lt <= self.interval_end):
                    summary_rows.append(("Landing", self.format_minutes(lt)))
        
        if battery is not None and len(battery):
            vcol = 'voltage_filtered_v' if 'voltage_filtered_v' in battery.columns else 'voltage_v'
            if vcol in battery.columns:
                summary_rows.append((
                    "Battery voltage",
                    f"{battery[vcol].iloc[0]:.2f} V → {battery[vcol].iloc[-1]:.2f} V",
                ))
            if 'remaining' in battery.columns:
                summary_rows.append((
                    "Battery remaining",
                    f"{100 * battery['remaining'].iloc[0]:.0f}% → {100 * battery['remaining'].iloc[-1]:.0f}%",
                ))
            if 'discharged_mah' in battery.columns:
                mah0 = float(battery['discharged_mah'].iloc[0])
                mah1 = float(battery['discharged_mah'].iloc[-1])
                if interval_active:
                    summary_rows.append(("Discharged", f"{mah1 - mah0:.0f} mAh (interval)"))
                else:
                    summary_rows.append(("Discharged", f"{mah1:.0f} mAh"))
        
        if gps is not None and len(gps):
            if 'satellites_used' in gps.columns:
                summary_rows.append((
                    "GPS sats",
                    f"min {int(gps['satellites_used'].min())} / max {int(gps['satellites_used'].max())}",
                ))
            if 'fix_type' in gps.columns:
                summary_rows.append(("GPS fix (max)", str(int(gps['fix_type'].max()))))
            if 'eph' in gps.columns:
                summary_rows.append(("GPS eph (med)", f"{gps['eph'].median():.2f} m"))
        
        if local is not None and len(local):
            if 'z' in local.columns:
                alt = -local['z']
                summary_rows.append(("Max altitude (local)", f"{alt.max():.1f} m"))
            if all(c in local.columns for c in ('x', 'y')):
                dx = np.diff(local['x'].to_numpy())
                dy = np.diff(local['y'].to_numpy())
                dist = float(np.nansum(np.sqrt(dx * dx + dy * dy)))
                summary_rows.append(("Path length (XY)", f"{dist:.1f} m"))
            if all(c in local.columns for c in ('vx', 'vy')):
                speed = np.sqrt(local['vx'] ** 2 + local['vy'] ** 2)
                summary_rows.append(("Max XY speed", f"{speed.max():.1f} m/s"))
        
        if est is not None and len(est):
            if 'pos_horiz_accuracy' in est.columns:
                summary_rows.append(("EKF horiz acc (med)", f"{est['pos_horiz_accuracy'].median():.2f} m"))
            ratios = []
            for col in ('mag_test_ratio', 'vel_test_ratio', 'pos_test_ratio'):
                if col in est.columns and est[col].max() > 0.8:
                    ratios.append(f"{col.replace('_test_ratio','')}={est[col].max():.2f}")
            summary_rows.append(("EKF test ratios", ", ".join(ratios) if ratios else "OK"))
        
        if imu is not None and len(imu):
            bits = []
            if 'accel_vibration_metric' in imu.columns:
                bits.append(f"accel vib max {imu['accel_vibration_metric'].max():.3f}")
            if 'gyro_vibration_metric' in imu.columns:
                bits.append(f"gyro vib max {imu['gyro_vibration_metric'].max():.3f}")
            clip_cols = [c for c in imu.columns if c.startswith('accel_clipping')]
            if clip_cols:
                clip_max = int(imu[clip_cols].max().max())
                bits.append(f"accel clips {clip_max}")
            if bits:
                summary_rows.append(("Vibration", "; ".join(bits)))
        
        events_all = self.get_flight_events(logname)
        events = self._events_in_interval(events_all)
        fail_n = sum(1 for e in events if e['kind'] == 'failsafe')
        if interval_active:
            summary_rows.append((
                "Events",
                f"{len(events)} in interval ({len(events_all)} total), {fail_n} failsafe-related",
            ))
        else:
            summary_rows.append(("Events", f"{len(events)} total, {fail_n} failsafe-related"))
        
        # --- Layout: summary | mode strip | events ---
        top = ttk.Frame(container)
        top.pack(fill=tk.X, padx=6, pady=4)
        title = f"Flight overview — {logname}"
        if interval_active:
            title += (
                f"  [{self.format_minutes(self.interval_start)}"
                f" – {self.format_minutes(self.interval_end)}]"
            )
        ttk.Label(top, text=title, font=('', 11, 'bold')).pack(anchor='w')
        
        summary_frame = ttk.LabelFrame(
            container,
            text="Quick facts" + (" (selected interval)" if interval_active else ""),
        )
        summary_frame.pack(fill=tk.X, padx=6, pady=4)
        for i, (k, v) in enumerate(summary_rows):
            r, c = divmod(i, 2)
            cell = ttk.Frame(summary_frame)
            cell.grid(row=r, column=c, sticky='ew', padx=8, pady=2)
            ttk.Label(cell, text=f"{k}:", width=20, anchor='w').pack(side=tk.LEFT)
            ttk.Label(cell, text=str(v), anchor='w').pack(side=tk.LEFT, fill=tk.X, expand=True)
        summary_frame.columnconfigure(0, weight=1)
        summary_frame.columnconfigure(1, weight=1)
        
        # Mode timeline (full series; xlim + cursor via global registry)
        mode_frame = ttk.LabelFrame(container, text="Flight mode timeline")
        mode_frame.pack(fill=tk.X, padx=6, pady=4)
        if status_full is not None and 'nav_state' in status_full.columns and len(status_full):
            t, _ = self._topic_time_minutes(status_full)
            fig = Figure(figsize=(10, 1.6), dpi=100)
            ax = fig.add_subplot(111)
            modes = status_full['nav_state'].astype(int).to_numpy()
            ax.step(t, modes, where='post', color='#1565c0', linewidth=1.5)
            ax.set_ylabel("nav_state")
            ax.set_xlabel("Time (min)")
            self._format_time_axis(ax, t)
            uniq = sorted(set(int(m) for m in modes))
            ax.set_yticks(uniq)
            ax.set_yticklabels([self._nav_state_name(m) for m in uniq], fontsize=8)
            ax.grid(True, axis='x', alpha=0.3)
            self._apply_event_overlay(ax, logname)
            fig.tight_layout()
            canvas = FigureCanvasTkAgg(fig, master=mode_frame)
            canvas.draw()
            canvas.get_tk_widget().pack(fill=tk.X, expand=False)
            NavigationToolbar2Tk(canvas, mode_frame).update()
            t0 = float(np.asarray(t)[0])
            t1 = float(np.asarray(t)[-1])
            self._register_time_axis(ax, logname, t0, t1)
        else:
            ttk.Label(mode_frame, text="vehicle_status_0 not available").pack(padx=8, pady=8)
        
        # Events list
        ev_title = "Events / failsafes"
        if interval_active:
            ev_title += " (selected interval)"
        ev_frame = ttk.LabelFrame(container, text=ev_title)
        ev_frame.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)
        cols = ("time", "type", "event")
        tree = ttk.Treeview(ev_frame, columns=cols, show="headings", height=10)
        tree.heading("time", text="Time")
        tree.heading("type", text="Type")
        tree.heading("event", text="Event")
        tree.column("time", width=80, stretch=False)
        tree.column("type", width=90, stretch=False)
        tree.column("event", width=280, stretch=True)
        tree.tag_configure("failsafe", foreground="#b71c1c")
        tree.tag_configure("arm", foreground="#2e7d32")
        tree.tag_configure("flight", foreground="#1565c0")
        vsb = ttk.Scrollbar(ev_frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        if events:
            for ev in events:
                tree.insert(
                    "", tk.END,
                    values=(self.format_minutes(ev['t_min']), ev['kind'], ev['label']),
                    tags=(ev['kind'],),
                )
        else:
            msg = (
                "No events in selected interval"
                if interval_active
                else "No arm/failsafe/land edges detected"
            )
            tree.insert("", tk.END, values=("--", "info", msg))
        
        hint = ttk.Label(
            container,
            text="Tip: enable “Overlay events on plots” in the top bar to show these markers on time-series plots.",
            foreground="#555555",
        )
        hint.pack(anchor='w', padx=8, pady=(0, 6))

    # Minimum readable height per curated subplot (inches @ figure dpi)
    MIN_CURATED_SUBPLOT_INCH = 2.0

    def _embed_scrollable_figure(self, fig, host):
        """Embed a matplotlib figure with a vertical scrollbar when it exceeds the pane height."""
        toolbar_host = ttk.Frame(host)
        toolbar_host.pack(side=tk.TOP, fill=tk.X)
        
        scroll_outer = ttk.Frame(host)
        scroll_outer.pack(fill=tk.BOTH, expand=True)
        
        vbar = ttk.Scrollbar(scroll_outer, orient=tk.VERTICAL)
        vbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        scroll_canvas = tk.Canvas(
            scroll_outer, highlightthickness=0, borderwidth=0, yscrollcommand=vbar.set
        )
        scroll_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vbar.configure(command=scroll_canvas.yview)
        
        inner = ttk.Frame(scroll_canvas)
        inner_window = scroll_canvas.create_window((0, 0), window=inner, anchor="nw")
        
        mpl_canvas = FigureCanvasTkAgg(fig, master=inner)
        mpl_canvas.draw()
        plot_widget = mpl_canvas.get_tk_widget()
        
        # Keep a fixed pixel height so subplots are not crushed to fit the window
        fig_h_px = max(int(fig.get_figheight() * fig.dpi), 200)
        plot_widget.configure(height=fig_h_px)
        plot_widget.pack(fill=tk.X, expand=False)
        
        toolbar = NavigationToolbar2Tk(mpl_canvas, toolbar_host)
        toolbar.update()
        
        def _sync_scrollregion(_event=None):
            scroll_canvas.configure(scrollregion=scroll_canvas.bbox("all"))
        
        def _sync_width(event):
            width = max(int(event.width), 100)
            scroll_canvas.itemconfigure(inner_window, width=width)
            plot_widget.configure(width=width)
            _sync_scrollregion()
        
        inner.bind("<Configure>", _sync_scrollregion)
        scroll_canvas.bind("<Configure>", _sync_width)
        
        def _on_mousewheel(event):
            delta = 0
            if getattr(event, "num", None) == 4:
                delta = -1
            elif getattr(event, "num", None) == 5:
                delta = 1
            elif getattr(event, "delta", 0):
                delta = -1 if event.delta > 0 else 1
            if delta:
                scroll_canvas.yview_scroll(delta, "units")
        
        def _bind_wheel(_event=None):
            scroll_canvas.bind_all("<MouseWheel>", _on_mousewheel)
            scroll_canvas.bind_all("<Button-4>", _on_mousewheel)
            scroll_canvas.bind_all("<Button-5>", _on_mousewheel)
        
        def _unbind_wheel(_event=None):
            scroll_canvas.unbind_all("<MouseWheel>")
            scroll_canvas.unbind_all("<Button-4>")
            scroll_canvas.unbind_all("<Button-5>")
        
        for w in (scroll_canvas, plot_widget, inner):
            w.bind("<Enter>", _bind_wheel)
            w.bind("<Leave>", _unbind_wheel)
        
        # Initial layout pass once the widget has a real size
        host.update_idletasks()
        _sync_scrollregion()

    def update_default_plots(self, logname, plot_name=None, container=None):
        """Update curated multi-plot tabs (Vibrations / Battery / Radio)."""
        targets = []
        if plot_name and container is not None:
            targets.append((plot_name, container))
        elif plot_name:
            pane = self._primary_pane(plot_name)
            if pane:
                targets.append((plot_name, pane['content']))
        else:
            for name in self.default_plots.keys():
                pane = self._primary_pane(name)
                if pane:
                    targets.append((name, pane['content']))
        
        for name, host in targets:
            plot_config = self.default_plots[name]
            for widget in host.winfo_children():
                widget.destroy()
            
            # Tall enough that each subplot stays readable; scroll if needed
            fig_h = max(self.MIN_CURATED_SUBPLOT_INCH * len(plot_config), 4.0)
            fig = Figure(figsize=(10, fig_h), dpi=100)
            self._unregister_figure(fig)
            axes = fig.subplots(len(plot_config), 1, squeeze=False)
            registered = []
            
            for idx, (topic, fields) in enumerate(plot_config):
                ax = axes[idx][0]
                csv_path = self._csv_path(logname, topic)
                title = self._curated_subplot_title(topic, fields)
                
                if not os.path.exists(csv_path):
                    ax.text(0.5, 0.5, f"{topic} not in this log", ha='center', va='center',
                            transform=ax.transAxes, fontsize=9, color='#666666')
                    ax.set_title(f"{title} ({logname})")
                    ax.set_xticks([])
                    ax.set_yticks([])
                    continue
                
                df = pd.read_csv(csv_path)
                time_values, time_col = self._topic_time_minutes(df)
                
                plotted = False
                if topic == 'vehicle_attitude_0' and all(q in df.columns for q in ('q[0]', 'q[1]', 'q[2]', 'q[3]')):
                    roll, pitch, yaw = self.convert_quaternion_to_rpy(df)
                    ax.plot(time_values, roll, label='Roll')
                    ax.plot(time_values, pitch, label='Pitch')
                    ax.plot(time_values, yaw, label='Yaw')
                    plotted = True
                else:
                    for field in fields:
                        if field in df.columns and field != time_col and field != 'timestamp_sample':
                            ax.plot(time_values, df[field], label=field)
                            plotted = True
                
                if not plotted:
                    ax.text(0.5, 0.5, "Requested fields missing", ha='center', va='center',
                            transform=ax.transAxes, fontsize=9, color='#666666')
                else:
                    self._format_time_axis(ax, time_values)
                    self._apply_event_overlay(ax, logname)
                    if ax.get_legend_handles_labels()[0]:
                        ax.legend(loc='upper right', fontsize=8)
                    ax.grid(True, alpha=0.35)
                    t0 = float(np.asarray(time_values)[0])
                    t1 = float(np.asarray(time_values)[-1])
                    registered.append((ax, t0, t1))
                
                ax.set_title(f"{title} ({logname})", fontsize=10)
            
            fig.tight_layout()
            self._embed_scrollable_figure(fig, host)
            for ax, t0, t1 in registered:
                self._register_time_axis(ax, logname, t0, t1)
    
    def update_position_plots(self, logname, container=None):
        """Update position plots for one log into the given container (or primary pane)."""
        if container is None:
            pane = self._primary_pane('position')
            if pane is None:
                return
            container = pane['content']
        
        for widget in container.winfo_children():
            widget.destroy()
        
        local_pos_path = os.path.join(self.csv_output_dir, f"{logname}_vehicle_local_position_0.csv")
        distance_sensor_path = os.path.join(self.csv_output_dir, f"{logname}_distance_sensor_0.csv")
        
        if not os.path.exists(local_pos_path):
            ttk.Label(
                container,
                text=f"vehicle_local_position_0 data not found for log:\n{logname}",
                justify=tk.CENTER,
            ).pack(expand=True, padx=10, pady=10)
            return
        
        split_container = tk.PanedWindow(container, orient=tk.HORIZONTAL)
        split_container.pack(fill=tk.BOTH, expand=True)
        
        left_frame = ttk.Frame(split_container)
        right_frame = ttk.Frame(split_container)
        split_container.add(left_frame)
        split_container.add(right_frame)
        
        local_pos_df = pd.read_csv(local_pos_path)
        time_col = self.get_x_axis_column(local_pos_df)
        
        if time_col in ['timestamp', 'timestamp_sample']:
            time_values = self.convert_timestamp_to_minutes(local_pos_df[time_col])
        else:
            time_values = local_pos_df[time_col]

        left_fig = Figure(figsize=(10, 8), dpi=100)
        self._unregister_figure(left_fig)
        left_axes = left_fig.subplots(2, 1)
        t0 = float(np.asarray(time_values)[0])
        t1 = float(np.asarray(time_values)[-1])
        
        ax = left_axes[0]
        x_values = local_pos_df['x'].values
        y_values = local_pos_df['y'].values
        
        valid_mask = ~np.isnan(x_values) & ~np.isnan(y_values)
        x_valid = x_values[valid_mask]
        y_valid = y_values[valid_mask]
        
        ax.plot(x_valid, y_valid, '-', label='Trajectory', linewidth=1)
        ax.plot(x_valid[0], y_valid[0], 'go', markersize=8, label='Start')
        ax.plot(x_valid[-1], y_valid[-1], 'ro', markersize=8, label='End')
        
        ax.set(
            xlabel='X Position (m)',
            ylabel='Y Position (m)',
            title=f'Vehicle XY Trajectory ({logname})'
        )
        
        def calculate_meter_ticks(data):
            min_val = np.nanmin(data)
            max_val = np.nanmax(data)
            span = max_val - min_val
            
            if span == 0:
                return [min_val-1, min_val, min_val+1]
            if span <= 10: step = 1
            elif span <= 50: step = 5
            else: step = 10
            
            return np.arange(
                np.floor(min_val/step)*step,
                np.ceil(max_val/step)*step + step/2,
                step
            )
        
        ax.set_xticks(calculate_meter_ticks(x_valid))
        ax.set_yticks(calculate_meter_ticks(y_valid))
        ax.grid(True, which='major', linestyle='--', linewidth=0.5)
        ax.set_aspect('equal', adjustable='datalim')
        ax.legend()

        ax = left_axes[1]
        ax.plot(time_values, -local_pos_df['z'], label='Z Position')
        
        if os.path.exists(distance_sensor_path):
            distance_df = pd.read_csv(distance_sensor_path)
            dist_time_col = self.get_x_axis_column(distance_df)
            dist_time_values = self.convert_timestamp_to_minutes(distance_df[dist_time_col])
            ax.plot(dist_time_values, distance_df['current_distance'], label='Distance Sensor')
        
        ax.set_xlabel('Time (minutes)')
        ax.set_ylabel('Altitude (m)')
        ax.set_title('Vertical Position')
        self._format_time_axis(ax, time_values)
        self._apply_event_overlay(ax, logname)
        ax.grid(True)
        ax.legend()

        left_fig.tight_layout()
        
        left_canvas = FigureCanvasTkAgg(left_fig, master=left_frame)
        left_canvas.draw()
        left_canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        left_toolbar = NavigationToolbar2Tk(left_canvas, left_frame)
        left_toolbar.update()
        self._register_time_axis(left_axes[1], logname, t0, t1)
        
        right_fig = Figure(figsize=(10, 8), dpi=100)
        self._unregister_figure(right_fig)
        right_axes = right_fig.subplots(3, 1)
        
        ax = right_axes[0]
        ax.plot(time_values, local_pos_df['xy_reset_counter'], label='XY Reset Counter')
        ax.plot(time_values, local_pos_df['z_reset_counter'], label='Z Reset Counter')
        ax.plot(time_values, local_pos_df['vxy_reset_counter'], label='VXY Reset Counter')
        ax.plot(time_values, local_pos_df['vz_reset_counter'], label='VZ Reset Counter')
        ax.set_xlabel('Time (minutes)')
        ax.set_ylabel('Count')
        ax.set_title('Reset Counters')
        ax.grid(True)
        ax.legend()
        
        ax = right_axes[1]
        ax.plot(time_values, local_pos_df['heading_reset_counter'], label='Heading Reset Counter')
        ax.plot(time_values, local_pos_df['heading_good_for_control'], label='Heading Good for Control')
        ax.plot(time_values, local_pos_df['dist_bottom_valid'], label='Bottom Distance Valid')
        ax.set_xlabel('Time (minutes)')
        ax.set_ylabel('Status')
        ax.set_title('Heading and Distance Validity')
        ax.grid(True)
        ax.legend()
        
        ax = right_axes[2]
        ax.plot(time_values, local_pos_df['dead_reckoning'], label='Dead Reckoning')
        ax.set_xlabel('Time (minutes)')
        ax.set_ylabel('Status')
        ax.set_title('Dead Reckoning Status')
        ax.grid(True)
        ax.legend()
        
        for ax in right_axes:
            self._format_time_axis(ax, time_values)
            self._apply_event_overlay(ax, logname)
        
        right_fig.tight_layout()
        
        right_canvas = FigureCanvasTkAgg(right_fig, master=right_frame)
        right_canvas.draw()
        right_canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        right_toolbar = NavigationToolbar2Tk(right_canvas, right_frame)
        right_toolbar.update()
        for ax in right_axes:
            self._register_time_axis(ax, logname, t0, t1)

    def _build_map_pane_chrome(self, content):
        """Create per-pane map hosts (toolbar, canvas, timeline) inside content."""
        toolbar_host = ttk.Frame(content)
        toolbar_host.pack(fill=tk.X)
        canvas_host = ttk.Frame(content)
        canvas_host.pack(fill=tk.BOTH, expand=True)
        timeline_frame = ttk.Frame(content)
        timeline_frame.pack(fill=tk.X, padx=6, pady=4)
        ttk.Label(timeline_frame, text="Time:").pack(side=tk.LEFT)
        time_var = tk.DoubleVar(value=0.0)
        time_label = ttk.Label(timeline_frame, text="--:--", width=8)
        # Scale command set after map state dict exists
        time_scale = ttk.Scale(
            timeline_frame, from_=0.0, to=1.0, orient=tk.HORIZONTAL,
            variable=time_var, state=tk.DISABLED,
        )
        time_scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)
        time_label.pack(side=tk.LEFT)
        
        state = {
            'toolbar_host': toolbar_host,
            'canvas_host': canvas_host,
            'timeline_frame': timeline_frame,
            'time_var': time_var,
            'time_scale': time_scale,
            'time_label': time_label,
            'figure': None,
            'canvas': None,
            'toolbar': None,
            'ax': None,
            'marker': None,
            'interval_path': None,
            'time_minutes': None,
            'xs': None,
            'ys': None,
            'scroll_cid': None,
            'press_cid': None,
            'motion_cid': None,
            'release_cid': None,
            'pan_start': None,
        }
        time_scale.configure(command=lambda v, s=state: self._on_map_timeline_pane(s, v))
        return state

    def _teardown_map_pane(self, pane):
        m = pane.get('map')
        if not m:
            return
        self._disconnect_map_interactions_pane(m)
        for key in ('toolbar', 'canvas'):
            widget = m.get(key)
            if widget is None:
                continue
            try:
                if key == 'canvas':
                    widget.get_tk_widget().destroy()
                else:
                    widget.destroy()
            except tk.TclError:
                pass
            m[key] = None

    def _clear_map_pane_canvas(self, pane):
        m = pane['map']
        self._disconnect_map_interactions_pane(m)
        if m.get('toolbar') is not None:
            try:
                m['toolbar'].destroy()
            except tk.TclError:
                pass
            m['toolbar'] = None
        if m.get('canvas') is not None:
            try:
                m['canvas'].get_tk_widget().destroy()
            except tk.TclError:
                pass
            m['canvas'] = None
        for host in (m['canvas_host'], m['toolbar_host']):
            for widget in host.winfo_children():
                widget.destroy()
        m['ax'] = None
        m['marker'] = None
        m['interval_path'] = None
        m['time_minutes'] = None
        m['xs'] = None
        m['ys'] = None
        m['figure'] = None

    def _show_map_message_in_pane(self, pane, text):
        self._clear_map_pane_canvas(pane)
        m = pane['map']
        m['time_scale'].configure(state=tk.DISABLED)
        m['time_label'].configure(text="--:--")
        ttk.Label(m['canvas_host'], text=text, justify=tk.LEFT).pack(expand=True, padx=10, pady=10)

    def _show_map_message(self, text):
        """Compatibility helper: show message on the primary map pane."""
        pane = self._primary_pane('map')
        if pane is not None:
            self._show_map_message_in_pane(pane, text)

    def _embed_map_figure_pane(self, pane):
        m = pane['map']
        m['canvas'] = FigureCanvasTkAgg(m['figure'], master=m['canvas_host'])
        m['canvas'].draw()
        m['canvas'].get_tk_widget().pack(fill=tk.BOTH, expand=True)
        m['toolbar'] = NavigationToolbar2Tk(m['canvas'], m['toolbar_host'])
        m['toolbar'].update()
        self._enable_map_interactions_pane(m)
        # Keep legacy aliases pointed at primary pane
        if pane is self._primary_pane('map'):
            self.map_figure = m['figure']
            self.map_canvas = m['canvas']
            self.map_toolbar = m['toolbar']
            self.map_ax = m['ax']

    def _disconnect_map_interactions_pane(self, m):
        fig = m.get('figure')
        for attr in ('scroll_cid', 'press_cid', 'motion_cid', 'release_cid'):
            cid = m.get(attr)
            if cid is not None and fig is not None:
                try:
                    fig.canvas.mpl_disconnect(cid)
                except Exception:
                    pass
            m[attr] = None
        m['pan_start'] = None

    def _enable_map_interactions_pane(self, m):
        self._disconnect_map_interactions_pane(m)
        if m.get('figure') is None:
            return
        canvas = m['figure'].canvas
        m['scroll_cid'] = canvas.mpl_connect('scroll_event', lambda e, s=m: self._on_map_scroll_pane(s, e))
        m['press_cid'] = canvas.mpl_connect('button_press_event', lambda e, s=m: self._on_map_press_pane(s, e))
        m['motion_cid'] = canvas.mpl_connect('motion_notify_event', lambda e, s=m: self._on_map_motion_pane(s, e))
        m['release_cid'] = canvas.mpl_connect('button_release_event', lambda e, s=m: self._on_map_release_pane(s, e))

    def _on_map_scroll_pane(self, m, event):
        ax = m.get('ax')
        if event.inaxes is None or ax is None or event.inaxes is not ax:
            return
        scale = 1 / 1.2 if event.button == 'up' else 1.2
        xdata, ydata = event.xdata, event.ydata
        if xdata is None or ydata is None:
            return
        xlim = ax.get_xlim()
        ylim = ax.get_ylim()
        new_width = (xlim[1] - xlim[0]) * scale
        new_height = (ylim[1] - ylim[0]) * scale
        relx = (xdata - xlim[0]) / (xlim[1] - xlim[0])
        rely = (ydata - ylim[0]) / (ylim[1] - ylim[0])
        ax.set_xlim([xdata - new_width * relx, xdata + new_width * (1 - relx)])
        ax.set_ylim([ydata - new_height * rely, ydata + new_height * (1 - rely)])
        if m.get('canvas') is not None:
            m['canvas'].draw_idle()

    def _on_map_press_pane(self, m, event):
        ax = m.get('ax')
        if ax is None or event.inaxes is not ax:
            return
        if event.button in (2, 3) and event.x is not None and event.y is not None:
            m['pan_start'] = (event.x, event.y, ax.get_xlim(), ax.get_ylim())

    def _on_map_motion_pane(self, m, event):
        if m.get('pan_start') is None or m.get('ax') is None or event.x is None or event.y is None:
            return
        x0, y0, xlim0, ylim0 = m['pan_start']
        ax = m['ax']
        bbox = ax.bbox
        if bbox.width == 0 or bbox.height == 0:
            return
        dx_data = -(event.x - x0) * (xlim0[1] - xlim0[0]) / bbox.width
        dy_data = -(event.y - y0) * (ylim0[1] - ylim0[0]) / bbox.height
        ax.set_xlim(xlim0[0] + dx_data, xlim0[1] + dx_data)
        ax.set_ylim(ylim0[0] + dy_data, ylim0[1] + dy_data)
        if m.get('canvas') is not None:
            m['canvas'].draw_idle()

    def _on_map_release_pane(self, m, event):
        if event.button in (2, 3):
            m['pan_start'] = None

    def _on_map_timeline_pane(self, m, value):
        if self._syncing_global_time:
            return
        if m.get('time_minutes') is None or m.get('xs') is None or m.get('marker') is None:
            return
        try:
            t = float(value)
        except (TypeError, ValueError):
            return
        idx = int(np.searchsorted(m['time_minutes'], t, side='left'))
        idx = min(max(idx, 0), len(m['xs']) - 1)
        m['marker'].set_data([m['xs'][idx]], [m['ys'][idx]])
        m['time_label'].configure(text=self.format_minutes(m['time_minutes'][idx]))
        if m.get('canvas') is not None:
            m['canvas'].draw_idle()
        # Drive the shared global cursor (other panes follow via broadcast)
        self._set_global_time(float(m['time_minutes'][idx]), broadcast=True)

    def _configure_map_timeline_pane(self, m, time_minutes, xs, ys):
        m['time_minutes'] = np.asarray(time_minutes, dtype=float)
        m['xs'] = np.asarray(xs, dtype=float)
        m['ys'] = np.asarray(ys, dtype=float)
        t0 = float(m['time_minutes'][0])
        t1 = float(m['time_minutes'][-1])
        if t1 <= t0:
            t1 = t0 + 1e-6
        m['time_scale'].configure(from_=t0, to=t1, state=tk.NORMAL)
        # Place at global cursor if available, else start (guard against scale callback)
        t = min(max(self.global_t_min, t0), t1)
        self._syncing_global_time = True
        try:
            m['time_var'].set(t)
            m['time_label'].configure(text=self.format_minutes(t))
        finally:
            self._syncing_global_time = False
        if m.get('marker') is not None:
            idx = int(np.searchsorted(m['time_minutes'], t, side='left'))
            idx = min(max(idx, 0), len(m['xs']) - 1)
            m['marker'].set_data([m['xs'][idx]], [m['ys'][idx]])

    def initialize_default_map(self):
        """Initialize primary map pane with a lightweight placeholder."""
        pane = self._primary_pane('map')
        if pane is None:
            return
        self._clear_map_pane_canvas(pane)
        m = pane['map']
        m['time_scale'].configure(state=tk.DISABLED)
        m['time_label'].configure(text="--:--")
        
        fig = Figure(figsize=(8, 6), dpi=100)
        ax = fig.add_subplot(111)
        ax.set_xticks([])
        ax.set_yticks([])
        
        if not CONTEXTILY_AVAILABLE:
            msg = (
                'Map feature requires contextily library.\n\n'
                'Install with: pip install contextily geopandas'
            )
        else:
            try:
                import geopandas  # noqa: F401
                msg = (
                    'GPS Flight Path Map\n\n'
                    'Convert a ULog file to view the flight path.\n'
                    'Scroll to zoom · right-drag to pan · timeline after loading.\n'
                    'Use + to compare another log side by side.'
                )
            except ImportError:
                msg = (
                    'Map will load when GPS data is available\n\n'
                    'Install geopandas for full map support:\npip install geopandas'
                )
        
        ax.text(0.5, 0.5, msg, transform=ax.transAxes, ha='center', va='center', fontsize=11)
        ax.set_title('GPS Flight Path Map', fontsize=12)
        fig.tight_layout()
        m['figure'] = fig
        m['ax'] = ax
        self._embed_map_figure_pane(pane)

    def update_map(self, logname, pane=None):
        """Update map with GPS path from the log into the given (or primary) pane."""
        if pane is None:
            pane = self._primary_pane('map')
        if pane is None or pane.get('map') is None:
            return
        
        print(f"Updating map for log: {logname}")
        self._clear_map_pane_canvas(pane)
        m = pane['map']
        
        if not CONTEXTILY_AVAILABLE:
            self._show_map_message_in_pane(
                pane, "Map feature requires contextily library.\nInstall with: pip install contextily"
            )
            return
        
        if not os.path.isdir(self.csv_output_dir):
            self._show_map_message_in_pane(pane, "No converted CSV data found. Convert a ULog first.")
            return
        
        all_csv_files = [
            f for f in os.listdir(self.csv_output_dir)
            if f.startswith(logname + "_") and f.endswith(".csv")
        ]
        
        gps_paths = [
            os.path.join(self.csv_output_dir, f"{logname}_estimator_global_position_0.csv"),
            os.path.join(self.csv_output_dir, f"{logname}_estimator_global_position_1.csv"),
            os.path.join(self.csv_output_dir, f"{logname}_vehicle_global_position_0.csv"),
            os.path.join(self.csv_output_dir, f"{logname}_vehicle_gps_position_0.csv"),
        ]
        
        gps_df = None
        lat_col = None
        lon_col = None
        
        for gps_path in gps_paths:
            if os.path.exists(gps_path):
                try:
                    gps_df = pd.read_csv(gps_path)
                    if 'lat' in gps_df.columns and 'lon' in gps_df.columns:
                        lat_col, lon_col = 'lat', 'lon'
                        break
                    if 'latitude' in gps_df.columns and 'longitude' in gps_df.columns:
                        lat_col, lon_col = 'latitude', 'longitude'
                        break
                except Exception as e:
                    print(f"Error reading GPS file {gps_path}: {e}")
                    continue
        
        if gps_df is None or lat_col is None or lon_col is None:
            gps_like_files = [f for f in all_csv_files if 'position' in f.lower() or 'gps' in f.lower()]
            error_text = (
                f"No GPS data found for log: {logname}\n\nLooking for:\n"
                "- estimator_global_position_0\n- estimator_global_position_1\n"
                "- vehicle_global_position_0\n- vehicle_gps_position_0"
            )
            if gps_like_files:
                error_text += "\n\nFound position-related files:\n" + "\n".join(gps_like_files[:5])
            self._show_map_message_in_pane(pane, error_text)
            return
        
        valid_mask = (
            gps_df[lat_col].notna()
            & gps_df[lon_col].notna()
            & (gps_df[lat_col] != 0)
            & (gps_df[lon_col] != 0)
            & (gps_df[lat_col].abs() <= 90)
            & (gps_df[lon_col].abs() <= 180)
        )
        if not valid_mask.any():
            self._show_map_message_in_pane(pane, f"No valid GPS coordinates found in log: {logname}")
            return
        
        gps_df_valid = gps_df[valid_mask].copy().reset_index(drop=True)
        time_col = self.get_x_axis_column(gps_df_valid)
        if time_col in ['timestamp', 'timestamp_sample']:
            time_minutes = self.convert_timestamp_to_minutes(gps_df_valid[time_col]).to_numpy()
        else:
            time_minutes = np.arange(len(gps_df_valid), dtype=float)
        
        fig = Figure(figsize=(8, 6), dpi=100)
        ax = fig.add_subplot(111)
        
        lat_min, lat_max = gps_df_valid[lat_col].min(), gps_df_valid[lat_col].max()
        lon_min, lon_max = gps_df_valid[lon_col].min(), gps_df_valid[lon_col].max()
        lat_padding = (lat_max - lat_min) * 0.1 or 0.01
        lon_padding = (lon_max - lon_min) * 0.1 or 0.01
        
        try:
            import geopandas as gpd
            from shapely.geometry import Point
            geopandas_available = True
        except ImportError:
            geopandas_available = False
        
        if geopandas_available:
            geometry = [
                Point(lon, lat)
                for lon, lat in zip(gps_df_valid[lon_col], gps_df_valid[lat_col])
            ]
            gdf = gpd.GeoDataFrame(gps_df_valid, geometry=geometry, crs='EPSG:4326')
            gdf_mercator = gdf.to_crs(epsg=3857)
            xs = gdf_mercator.geometry.x.to_numpy()
            ys = gdf_mercator.geometry.y.to_numpy()
            
            x_min, x_max = xs.min(), xs.max()
            y_min, y_max = ys.min(), ys.max()
            x_padding = max((x_max - x_min) * 0.1, 500)
            y_padding = max((y_max - y_min) * 0.1, 500)
            ax.set_xlim(x_min - x_padding, x_max + x_padding)
            ax.set_ylim(y_min - y_padding, y_max + y_padding)
            
            try:
                ctx.add_basemap(
                    ax,
                    crs=gdf_mercator.crs,
                    source=_basemap_source(),
                    headers=BASEMAP_HEADERS,
                )
                ax.set_xlim(x_min - x_padding, x_max + x_padding)
                ax.set_ylim(y_min - y_padding, y_max + y_padding)
            except Exception as e:
                print(f"Warning: Could not load basemap: {e}")
                ax.set_xlabel('Longitude (Web Mercator)')
                ax.set_ylabel('Latitude (Web Mercator)')
            
            ax.plot(xs, ys, 'b-', linewidth=3, label='Flight Path', zorder=10, alpha=0.8)
            ax.plot(xs[0], ys[0], 'go', markersize=12, label='Start', zorder=11, alpha=0.9)
            ax.plot(xs[-1], ys[-1], 'ro', markersize=12, label='End', zorder=11, alpha=0.9)
            (marker,) = ax.plot(
                [xs[0]], [ys[0]], 'o', color='#ff9800', markersize=14,
                markeredgecolor='black', markeredgewidth=1.5,
                label='Current', zorder=12,
            )
        else:
            xs = gps_df_valid[lon_col].to_numpy()
            ys = gps_df_valid[lat_col].to_numpy()
            ax.plot(xs, ys, 'b-', linewidth=2, label='Flight Path', marker='o', markersize=2)
            ax.plot(xs[0], ys[0], 'go', markersize=10, label='Start')
            ax.plot(xs[-1], ys[-1], 'ro', markersize=10, label='End')
            (marker,) = ax.plot(
                [xs[0]], [ys[0]], 'o', color='#ff9800', markersize=12,
                markeredgecolor='black', label='Current', zorder=12,
            )
            ax.set_xlim(lon_min - lon_padding, lon_max + lon_padding)
            ax.set_ylim(lat_min - lat_padding, lat_max + lat_padding)
            ax.set_xlabel('Longitude')
            ax.set_ylabel('Latitude')
            ax.grid(True, alpha=0.3)
        
        ax.set_title(f'GPS Flight Path - {logname}')
        ax.legend(loc='upper right')
        ax.set_aspect('equal', adjustable='datalim')
        fig.tight_layout()
        
        m['figure'] = fig
        m['ax'] = ax
        m['marker'] = marker
        m['interval_path'] = None
        self._configure_map_timeline_pane(m, time_minutes, xs, ys)
        self._embed_map_figure_pane(pane)
        # Apply current global interval highlight + cursor after a fresh draw
        self._apply_interval_to_maps()
        self._sync_maps_to_global_time()

    def clear_plots(self):
        """Clear all plots from the Data Plots pane"""
        if self.current_figure:
            plt.close(self.current_figure)
            self.current_figure = None
            self.plotted_topics = set()
            self.plot_configs = {}
        
        self.canvas = None
        self.toolbar = None
        if hasattr(self, "plots_figure_frame"):
            for widget in self.plots_figure_frame.winfo_children():
                widget.destroy()

    def compare_parameters(self, show_warnings=True):
        """Compare parameters between all loaded logs and highlight differences"""
        if len(self.ulog_files) < 1:
            if show_warnings:
                messagebox.showwarning("Warning", "At least one log is required for comparison!")
            return
        
        for item in self.compare_tree.get_children():
            self.compare_tree.delete(item)
        
        self.all_params = {}
        for logname in self.ulog_files.keys():
            params = self.load_parameters_from_log(logname, show_errors=show_warnings)
            self.all_params[logname] = params
        
        param_sets = [params.keys() for params in self.all_params.values() if params]
        if not param_sets:
            self.all_param_names = []
            return
        
        self.all_param_names = sorted(set().union(*param_sets))
        
        columns = ["Parameter"] + [f"Value {logname}" for logname in self.ulog_files.keys()]
        self.compare_tree["columns"] = columns
        for col in columns:
            self.compare_tree.heading(col, text=col)
            self.compare_tree.column(col, width=140, stretch=True)
        
        self.compare_tree.tag_configure("diff", background="yellow")
        
        for param in self.all_param_names:
            values = [param]
            param_values = []
            for logname in self.ulog_files.keys():
                value = self.all_params[logname].get(param, "N/A")
                values.append(value)
                param_values.append(value)
            
            if len(set(str(v) for v in param_values)) > 1:
                self.compare_tree.insert("", tk.END, values=values, tags=("diff",))
            else:
                self.compare_tree.insert("", tk.END, values=values)
        
        self.filter_parameters()

    def filter_parameters(self, *args):
        """Filter parameters in the treeview based on the search term"""
        if not hasattr(self, 'compare_tree'):
            return
        search_term = self.parameter_search_var.get().lower()
        
        for item in self.compare_tree.get_children():
            self.compare_tree.delete(item)
        
        if not self.all_param_names or not self.all_params:
            return
        
        for param in self.all_param_names:
            if search_term in param.lower():
                values = [param]
                param_values = []
                for logname in self.ulog_files.keys():
                    value = self.all_params.get(logname, {}).get(param, "N/A")
                    values.append(value)
                    param_values.append(value)
                
                if len(set(str(v) for v in param_values)) > 1:
                    self.compare_tree.insert("", tk.END, values=values, tags=("diff",))
                else:
                    self.compare_tree.insert("", tk.END, values=values)

    def load_parameters_from_log(self, logname, show_errors=True):
        """Load parameters from a specific log using the stored absolute path"""
        ulog_file = self.ulog_paths.get(logname)
        if not ulog_file:
            # Fallback for older sessions / renamed logs
            candidate = os.path.join(os.path.dirname(self.file_path.get() or ""), f"{logname}.ulg")
            ulog_file = candidate if os.path.exists(candidate) else None
        
        if not ulog_file or not os.path.exists(ulog_file):
            if show_errors:
                messagebox.showerror("Error", f"ULog file not found for log: {logname}")
            return {}
        
        try:
            ulog = ULog(ulog_file)
            return ulog.initial_parameters
        except Exception as e:
            if show_errors:
                messagebox.showerror("Error", f"Failed to load parameters from {logname}:\n{str(e)}")
            return {}

if __name__ == "__main__":
    root = tk.Tk()
    app = FlightLogAnalyzerApp(root)
    root.mainloop()
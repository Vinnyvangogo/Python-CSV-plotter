#!/usr/bin/env python3
"""
CDAQ CSV Channel Plot Viewer
=============================

A desktop GUI (Tkinter + Matplotlib) for loading a cDAQ-style CSV log
(one "Timestamp" column followed by any number of channel columns).

Each "module" (columns are auto-grouped by their name prefix, e.g. TC,
Mod2, Mod3, ...) gets its own stacked subplot with a shared, synchronized
x-axis. Every channel has a checkbox to toggle its trace on/off within
its module's subplot. Hovering over any subplot shows a snapping cursor
(vertical line + point markers) with a readout box listing the value of
every visible channel in that subplot at the cursor's time position.

Usage:
    python cdaq_plot_viewer.py [optional/path/to/file.csv]

Requirements:
    pip install pandas matplotlib numpy
"""

import os
import sys
import re
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.backends.backend_tkagg import (
    FigureCanvasTkAgg, NavigationToolbar2Tk
)
from matplotlib.figure import Figure

# --- Dark theme palette ---
BG = "#1e1e1e"          # main window / panel background
BG_LIGHT = "#2b2b2b"    # slightly lighter panels (checkbox list, entries)
FG = "#e0e0e0"          # primary text
FG_DIM = "#9a9a9a"      # secondary/status text
ACCENT = "#3a7ca5"      # selection/active accent
BORDER = "#444444"

plt.style.use("dark_background")
COLOR_CYCLE = plt.rcParams['axes.prop_cycle'].by_key()['color']
SUBPLOT_HEIGHT_IN = 2.3   # inches of figure height per module subplot
SUBPLOT_WIDTH_IN = 11.0


def guess_group(col_name: str) -> str:
    """Bucket a channel name into a module/group.

    Handles two naming conventions seen in cDAQ exports:
      - 'ModN_CHxx_...'  -> group 'ModN' (the trailing number is a distinct module ID,
                            and channels are distinguished by the following CHxx token)
      - 'TCnn_...'       -> group 'TC'   (the trailing number is just the channel's own
                            index, so all thermocouples share a single group)
    """
    parts = col_name.split("_")
    first = parts[0]
    m = re.match(r"^([A-Za-z]+)(\d*)$", first)
    if not m:
        return first
    letters, digits = m.groups()
    if digits and len(parts) > 1 and re.match(r"^CH\d+$", parts[1], re.IGNORECASE):
        return first  # e.g. "Mod2" is the module id; "CH00" distinguishes the channel
    return letters or first  # e.g. "TC00" -> "TC"; the "00" is just the channel index


class CdaqPlotViewer(tk.Tk):
    def __init__(self, initial_file=None):
        super().__init__()
        self.title("CDAQ CSV Channel Plot Viewer")
        self.geometry("1500x900")
        self.configure(bg=BG)
        self._apply_dark_style()

        self.df = None
        self.x_col = None
        self.xnum = None                 # numeric/date2num version of x for nearest-point lookups
        self.groups = []                 # ordered list of module/group names
        self.channel_cols = []
        self.channel_vars = {}           # col -> tk.BooleanVar
        self.axes = {}                   # group -> Axes
        self.ax_to_group = {}            # Axes -> group
        self.group_lines = {}            # group -> {col: Line2D}
        self.group_markers = {}          # group -> {col: Line2D (point marker)}
        self.group_vlines = {}           # group -> Line2D (vertical cursor line)
        self.group_texts = {}            # group -> Text (readout box)

        self._build_layout()

        if initial_file and os.path.isfile(initial_file):
            self.load_csv(initial_file)

    # --------------------------------------------------------------- theme
    def _apply_dark_style(self):
        style = ttk.Style(self)
        # 'clam' is the most reliably re-themeable built-in ttk theme
        style.theme_use("clam")

        style.configure(".", background=BG, foreground=FG,
                         fieldbackground=BG_LIGHT, bordercolor=BORDER,
                         darkcolor=BG, lightcolor=BG, troughcolor=BG,
                         font=("TkDefaultFont", 9))
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("TButton", background=BG_LIGHT, foreground=FG,
                         bordercolor=BORDER, focuscolor=ACCENT, padding=4)
        style.map("TButton",
                   background=[("active", ACCENT), ("pressed", ACCENT)],
                   foreground=[("active", "#ffffff")])
        style.configure("TEntry", fieldbackground=BG_LIGHT, foreground=FG,
                         insertcolor=FG, bordercolor=BORDER)
        style.configure("TCheckbutton", background=BG, foreground=FG,
                         focuscolor=BG)
        style.map("TCheckbutton",
                   background=[("active", BG)],
                   foreground=[("active", "#ffffff")],
                   indicatorcolor=[("selected", ACCENT), ("!selected", BG_LIGHT)])
        style.configure("Vertical.TScrollbar", background=BG_LIGHT, troughcolor=BG,
                         bordercolor=BG, arrowcolor=FG)
        style.configure("TPanedwindow", background=BG)
        style.configure("TSeparator", background=BORDER)

    # ------------------------------------------------------------------ UI
    def _build_layout(self):
        top = ttk.Frame(self, padding=6)
        top.pack(side=tk.TOP, fill=tk.X)

        ttk.Button(top, text="Open CSV...", command=self.open_file_dialog).pack(side=tk.LEFT)
        ttk.Button(top, text="Select All", command=self.select_all).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(top, text="Clear All", command=self.clear_all).pack(side=tk.LEFT, padx=(4, 0))

        ttk.Label(top, text="Filter:").pack(side=tk.LEFT, padx=(20, 4))
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *a: self.apply_filter())
        ttk.Entry(top, textvariable=self.filter_var, width=30).pack(side=tk.LEFT)

        self.status_var = tk.StringVar(value="No file loaded.")
        ttk.Label(top, textvariable=self.status_var, foreground=FG_DIM).pack(side=tk.RIGHT)

        main = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        main.pack(fill=tk.BOTH, expand=True)

        # --- Left panel: scrollable checkbox list ---
        left_container = ttk.Frame(main, width=340)
        main.add(left_container, weight=0)

        cb_canvas = tk.Canvas(left_container, borderwidth=0, highlightthickness=0, bg=BG)
        cb_vscroll = ttk.Scrollbar(left_container, orient=tk.VERTICAL, command=cb_canvas.yview)
        self.checkbox_frame = ttk.Frame(cb_canvas)

        self.checkbox_frame.bind(
            "<Configure>", lambda e: cb_canvas.configure(scrollregion=cb_canvas.bbox("all"))
        )
        cb_canvas.create_window((0, 0), window=self.checkbox_frame, anchor="nw")
        cb_canvas.configure(yscrollcommand=cb_vscroll.set)
        cb_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        cb_vscroll.pack(side=tk.RIGHT, fill=tk.Y)

        def _on_cb_mousewheel(event):
            cb_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        cb_canvas.bind("<MouseWheel>", _on_cb_mousewheel)
        self.checkbox_frame.bind("<MouseWheel>", _on_cb_mousewheel)

        # --- Right panel: toolbar (fixed) + scrollable stack of subplots ---
        right = ttk.Frame(main)
        main.add(right, weight=1)

        self.fig = Figure(figsize=(SUBPLOT_WIDTH_IN, SUBPLOT_HEIGHT_IN), dpi=100)
        self.fig.patch.set_facecolor(BG)

        toolbar_holder = tk.Frame(right, bg=BG_LIGHT)
        toolbar_holder.pack(side=tk.TOP, fill=tk.X)

        plot_area = ttk.Frame(right)
        plot_area.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        self.plot_outer_canvas = tk.Canvas(plot_area, borderwidth=0, highlightthickness=0, bg=BG)
        plot_vscroll = ttk.Scrollbar(plot_area, orient=tk.VERTICAL, command=self.plot_outer_canvas.yview)
        self.plot_outer_canvas.configure(yscrollcommand=plot_vscroll.set)
        self.plot_outer_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        plot_vscroll.pack(side=tk.RIGHT, fill=tk.Y)

        self.canvas = FigureCanvasTkAgg(self.fig, master=self.plot_outer_canvas)
        self._canvas_window_id = self.plot_outer_canvas.create_window(
            (0, 0), window=self.canvas.get_tk_widget(), anchor="nw"
        )

        toolbar = NavigationToolbar2Tk(self.canvas, toolbar_holder)
        toolbar.config(background=BG_LIGHT)
        for child in toolbar.winfo_children():
            child.config(background=BG_LIGHT)
        toolbar.update()

        def _on_plot_mousewheel(event):
            self.plot_outer_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        self.plot_outer_canvas.bind("<MouseWheel>", _on_plot_mousewheel)
        self.canvas.get_tk_widget().bind("<MouseWheel>", _on_plot_mousewheel)

        self.canvas.mpl_connect("motion_notify_event", self._on_motion)
        self.canvas.mpl_connect("figure_leave_event", lambda e: self._hide_all_cursors())

    # --------------------------------------------------------------- data
    def open_file_dialog(self):
        path = filedialog.askopenfilename(
            title="Select CDAQ CSV file",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")]
        )
        if path:
            self.load_csv(path)

    def load_csv(self, path):
        try:
            df = pd.read_csv(path)
        except Exception as e:
            messagebox.showerror("Error loading CSV", str(e))
            return

        if df.shape[1] < 2:
            messagebox.showerror("Error", "CSV needs at least one timestamp column and one data column.")
            return

        self.x_col = df.columns[0]
        try:
            df[self.x_col] = pd.to_datetime(df[self.x_col])
        except Exception:
            pass

        self.df = df
        self.channel_cols = [c for c in df.columns if c != self.x_col]

        if pd.api.types.is_datetime64_any_dtype(self.df[self.x_col]):
            self.xnum = mdates.date2num(self.df[self.x_col])
        else:
            xnum = pd.to_numeric(self.df[self.x_col], errors="coerce").values
            self.xnum = xnum if not np.isnan(xnum).all() else np.arange(len(self.df), dtype=float)

        self.status_var.set(
            f"{os.path.basename(path)}  |  {len(df):,} rows  |  {len(self.channel_cols)} channels"
        )

        self._build_checkboxes()
        self._build_plot_axes()

    # ---------------------------------------------------------- checkboxes
    def _build_checkboxes(self):
        for w in self.checkbox_frame.winfo_children():
            w.destroy()
        self.channel_vars.clear()

        groups = {}
        for col in self.channel_cols:
            groups.setdefault(guess_group(col), []).append(col)
        self.groups = sorted(groups.keys())

        row = 0
        for group in self.groups:
            cols = groups[group]
            header = ttk.Frame(self.checkbox_frame)
            header.grid(row=row, column=0, sticky="w", pady=(10 if row else 0, 2), padx=4)
            row += 1

            ttk.Label(header, text=group, font=("TkDefaultFont", 10, "bold")).pack(side=tk.LEFT)
            ttk.Button(header, text="all", width=4,
                       command=lambda cols=cols: self._set_group(cols, True)).pack(side=tk.LEFT, padx=(8, 2))
            ttk.Button(header, text="none", width=5,
                       command=lambda cols=cols: self._set_group(cols, False)).pack(side=tk.LEFT)

            for col in cols:
                var = tk.BooleanVar(value=False)
                self.channel_vars[col] = var
                cb = ttk.Checkbutton(
                    self.checkbox_frame, text=col, variable=var,
                    command=lambda c=col: self._toggle_channel(c)
                )
                cb.grid(row=row, column=0, sticky="w", padx=18)
                row += 1

    def _set_group(self, cols, value):
        for c in cols:
            self.channel_vars[c].set(value)
            self._toggle_channel(c)

    def select_all(self):
        for c, var in self.channel_vars.items():
            var.set(True)
            self._toggle_channel(c)

    def clear_all(self):
        for c, var in self.channel_vars.items():
            var.set(False)
            self._toggle_channel(c)

    def apply_filter(self):
        text = self.filter_var.get().lower().strip()
        for widget in self.checkbox_frame.winfo_children():
            if isinstance(widget, ttk.Checkbutton):
                label = widget.cget("text").lower()
                if (text in label) if text else True:
                    widget.grid()
                else:
                    widget.grid_remove()

    # --------------------------------------------------------- subplot setup
    def _build_plot_axes(self):
        self.fig.clf()
        self.axes.clear()
        self.ax_to_group.clear()
        self.group_lines.clear()
        self.group_markers.clear()
        self.group_vlines.clear()
        self.group_texts.clear()

        n = len(self.groups)
        if n == 0:
            self.canvas.draw_idle()
            return

        first_ax = None
        for i, group in enumerate(self.groups):
            ax = self.fig.add_subplot(n, 1, i + 1, sharex=first_ax)
            if first_ax is None:
                first_ax = ax
            ax.set_facecolor(BG_LIGHT)
            ax.set_title(group, loc="left", fontsize=10, fontweight="bold", color=FG)
            ax.grid(True, alpha=0.25, color=FG_DIM)
            ax.tick_params(labelsize=8, colors=FG_DIM)
            for spine in ax.spines.values():
                spine.set_color(BORDER)
            if i < n - 1:
                ax.tick_params(labelbottom=False)
            else:
                ax.set_xlabel(str(self.x_col), fontsize=9, color=FG)

            vline = ax.axvline(color="#cccccc", linestyle="--", linewidth=0.8, visible=False, zorder=4)
            ax.relim(visible_only=True)  # clear vline's phantom x=0 before it poisons shared-x autoscale
            txt = ax.text(
                0.01, 0.98, "", transform=ax.transAxes, va="top", ha="left",
                fontsize=8, family="monospace", visible=False, color=FG,
                bbox=dict(boxstyle="round", fc=BG, ec=BORDER, alpha=0.92)
            )

            self.axes[group] = ax
            self.ax_to_group[ax] = group
            self.group_lines[group] = {}
            self.group_markers[group] = {}
            self.group_vlines[group] = vline
            self.group_texts[group] = txt

        self.fig.set_size_inches(SUBPLOT_WIDTH_IN, SUBPLOT_HEIGHT_IN * n)
        self.fig.subplots_adjust(left=0.06, right=0.98, top=1 - 0.15 / n, bottom=0.25 / n, hspace=0.45)

        self._resize_plot_canvas()
        self.canvas.draw_idle()

    def _resize_plot_canvas(self):
        self.canvas.draw()
        w_px = int(self.fig.get_figwidth() * self.fig.dpi)
        h_px = int(self.fig.get_figheight() * self.fig.dpi)
        self.canvas.get_tk_widget().config(width=w_px, height=h_px)
        self.plot_outer_canvas.configure(scrollregion=(0, 0, w_px, h_px))

    # ------------------------------------------------------------- plotting
    def _toggle_channel(self, col):
        group = guess_group(col)
        ax = self.axes.get(group)
        if ax is None:
            return
        is_on = self.channel_vars[col].get()
        lines = self.group_lines[group]
        markers = self.group_markers[group]

        if is_on:
            if col not in lines:
                color = COLOR_CYCLE[len(lines) % len(COLOR_CYCLE)]
                line, = ax.plot(self.df[self.x_col], self.df[col], label=col, linewidth=1, color=color)
                marker, = ax.plot([], [], "o", color=color, markersize=5, visible=False, zorder=5)
                lines[col] = line
                markers[col] = marker
            else:
                lines[col].set_visible(True)
                markers.setdefault(col, None)
        else:
            if col in lines:
                lines[col].set_visible(False)
                if markers.get(col) is not None:
                    markers[col].set_visible(False)

        self._refresh_axis(group)

    def _refresh_axis(self, group):
        ax = self.axes[group]
        lines = self.group_lines.get(group, {})
        visible = [l for l in lines.values() if l.get_visible()]
        if visible:
            leg = ax.legend(visible, [l.get_label() for l in visible],
                             loc="upper right", fontsize=7, framealpha=0.85, ncol=min(len(visible), 3),
                             facecolor=BG, edgecolor=BORDER, labelcolor=FG)
            ax.relim(visible_only=True)
            ax.autoscale_view()
        else:
            leg = ax.get_legend()
            if leg:
                leg.remove()
        self.canvas.draw_idle()

    # --------------------------------------------------------------- cursor
    def _format_x(self, val):
        if isinstance(val, pd.Timestamp):
            return val.strftime("%H:%M:%S.%f")[:-3]
        try:
            return f"{val:.6g}"
        except (TypeError, ValueError):
            return str(val)

    def _hide_all_cursors(self, except_group=None):
        changed = False
        for group in self.groups:
            if group == except_group:
                continue
            vline = self.group_vlines.get(group)
            txt = self.group_texts.get(group)
            if vline is not None and vline.get_visible():
                vline.set_visible(False)
                changed = True
            if txt is not None and txt.get_visible():
                txt.set_visible(False)
                changed = True
            for marker in self.group_markers.get(group, {}).values():
                if marker is not None and marker.get_visible():
                    marker.set_visible(False)
                    changed = True
        if changed:
            self.canvas.draw_idle()

    def _on_motion(self, event):
        if self.df is None or event.inaxes is None or event.inaxes not in self.ax_to_group:
            self._hide_all_cursors()
            return

        group = self.ax_to_group[event.inaxes]
        self._hide_all_cursors(except_group=group)

        xdata = event.xdata
        if xdata is None or self.xnum is None or len(self.xnum) == 0:
            return

        idx = int(np.searchsorted(self.xnum, xdata))
        idx = min(max(idx, 0), len(self.xnum) - 1)
        if idx > 0 and abs(self.xnum[idx - 1] - xdata) < abs(self.xnum[idx] - xdata):
            idx -= 1

        lines = self.group_lines.get(group, {})
        markers = self.group_markers.get(group, {})
        visible_cols = [c for c, l in lines.items() if l.get_visible()]
        if not visible_cols:
            return

        xval = self.xnum[idx]
        text_parts = [self._format_x(self.df[self.x_col].iloc[idx])]
        for col in visible_cols:
            y = self.df[col].iloc[idx]
            marker = markers.get(col)
            if marker is not None:
                marker.set_data([xval], [y])
                marker.set_visible(True)
            short = col if len(col) <= 28 else col[:25] + "..."
            text_parts.append(f"{short}: {y:.4f}")

        vline = self.group_vlines[group]
        vline.set_xdata([xval, xval])
        vline.set_visible(True)

        txt = self.group_texts[group]
        txt.set_text("\n".join(text_parts))
        txt.set_visible(True)

        self.canvas.draw_idle()


def main():
    initial_file = sys.argv[1] if len(sys.argv) > 1 else None
    app = CdaqPlotViewer(initial_file=initial_file)
    app.mainloop()


if __name__ == "__main__":
    main()

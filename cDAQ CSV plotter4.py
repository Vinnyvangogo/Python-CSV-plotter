#!/usr/bin/env python3
"""
CDAQ CSV Channel Plot Viewer
=============================

A dark-themed desktop GUI (Tkinter + Matplotlib) for loading a cDAQ-style
CSV log (one "Timestamp" column followed by any number of channel columns).

- Every channel has a checkbox (grouped by module for convenience) that
  toggles its trace on/off. All enabled channels are drawn together on a
  single shared plot.
- Next to each channel's checkbox is a small "R" toggle. Checking it sends
  that channel to a secondary y-axis on the right side of the plot, so
  small-scale signals aren't flattened by large-scale ones sharing the axis.
- Moving the mouse over the plot shows a live crosshair readout (dashed
  gray) of every visible channel's value at that time.
- LEFT-CLICK freezes that crosshair in place: it turns into a solid
  yellow cursor that stays exactly where you clicked, no matter where you
  move the mouse afterward. Click elsewhere to move it. Right-click (or
  the "Unlock Cursor" button) releases it back to live hover-tracking.
- "Save Image..." exports the plot exactly as currently displayed
  (including a frozen cursor, if any) to a PNG/PDF/SVG file.

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

HOVER_COLOR = "#888888"   # live, unlocked crosshair (dashed)
LOCK_COLOR = "#ffe066"    # frozen/locked cursor (solid)

plt.style.use("dark_background")
COLOR_CYCLE = plt.rcParams['axes.prop_cycle'].by_key()['color']


def guess_group(col_name: str) -> str:
    """Bucket a channel name into a module/group, for sidebar organization only.

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
        self.groups = []                 # ordered list of module/group names (sidebar only)
        self.channel_cols = []
        self.channel_vars = {}           # col -> tk.BooleanVar (enabled?)
        self.axis_vars = {}              # col -> tk.BooleanVar (True = right axis)
        self.axis_side = {}              # col -> "left" | "right"
        self.row_frames = {}             # col -> row Frame (for filtering)
        self.lines = {}                  # col -> Line2D (on whichever axes it's assigned to)
        self.cur_markers = {}            # col -> Line2D (cursor dot on that channel's line)
        self.cursor_locked = False
        self.cursor_idx = None

        self._build_layout()

        if initial_file and os.path.isfile(initial_file):
            self.load_csv(initial_file)

    # --------------------------------------------------------------- theme
    def _apply_dark_style(self):
        style = ttk.Style(self)
        style.theme_use("clam")  # the built-in theme most reliably re-themeable

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
        style.configure("TCheckbutton", background=BG, foreground=FG, focuscolor=BG)
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
        ttk.Separator(top, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)
        ttk.Button(top, text="Unlock Cursor", command=self.unlock_cursor).pack(side=tk.LEFT)
        ttk.Button(top, text="Save Image...", command=self.save_image).pack(side=tk.LEFT, padx=(4, 0))

        ttk.Label(top, text="Filter:").pack(side=tk.LEFT, padx=(20, 4))
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *a: self.apply_filter())
        ttk.Entry(top, textvariable=self.filter_var, width=30).pack(side=tk.LEFT)

        self.status_var = tk.StringVar(
            value="No file loaded.  |  Click plot to freeze cursor.  |  Check 'R' to move a channel to the right axis."
        )
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

        # --- Right panel: toolbar + single shared plot (with twin y-axis) ---
        right = ttk.Frame(main)
        main.add(right, weight=1)

        self.fig = Figure(figsize=(10, 6), dpi=100)
        self.fig.patch.set_facecolor(BG)

        toolbar_holder = tk.Frame(right, bg=BG_LIGHT)
        toolbar_holder.pack(side=tk.TOP, fill=tk.X)

        canvas_holder = ttk.Frame(right)
        canvas_holder.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        self.canvas = FigureCanvasTkAgg(self.fig, master=canvas_holder)
        self.canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        self.toolbar = NavigationToolbar2Tk(self.canvas, toolbar_holder)
        self.toolbar.config(background=BG_LIGHT)
        for child in self.toolbar.winfo_children():
            child.config(background=BG_LIGHT)
        self.toolbar.update()

        self.canvas.mpl_connect("motion_notify_event", self._on_motion)
        self.canvas.mpl_connect("figure_leave_event", self._on_figure_leave)
        self.canvas.mpl_connect("button_press_event", self._on_click)

        self._build_axes()

    def _build_axes(self):
        """(Re)build the figure's primary + secondary (twin) axes from scratch."""
        self.fig.clf()
        self.ax = self.fig.add_subplot(111)
        self.ax2 = self.ax.twinx()
        self._style_axes()
        self._init_cursor_artists()

    def _style_axes(self):
        ax = self.ax
        ax.set_facecolor(BG_LIGHT)
        ax.grid(True, alpha=0.25, color=FG_DIM)
        ax.tick_params(labelsize=8, colors=FG_DIM)
        for spine in ax.spines.values():
            spine.set_color(BORDER)
        ax.set_ylabel("Value (left axis)", color=FG)

        ax2 = self.ax2
        ax2.grid(False)
        ax2.tick_params(labelsize=8, colors=ACCENT)
        ax2.spines["right"].set_color(ACCENT)
        ax2.set_ylabel("Value (right axis)", color=ACCENT)

    def _init_cursor_artists(self):
        """(Re)create the single crosshair/cursor artifact set on the current axes."""
        self.cur_vline = self.ax.axvline(
            color=HOVER_COLOR, linestyle="--", linewidth=0.8, visible=False, zorder=6
        )
        self.ax.relim(visible_only=True)  # don't let the phantom x=0 poison autoscale
        self.cur_markers = {}
        self.cur_text = self.ax.text(
            0.01, 0.98, "", transform=self.ax.transAxes, va="top", ha="left",
            fontsize=8, family="monospace", visible=False, color=FG,
            bbox=dict(boxstyle="round", fc=BG, ec=BORDER, alpha=0.9), zorder=9
        )
        self.cursor_locked = False
        self.cursor_idx = None

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

        self.axis_side = {}
        self.lines = {}
        self._build_axes()
        self.ax.set_xlabel(str(self.x_col), fontsize=9, color=FG)
        self.canvas.draw_idle()
        self._build_checkboxes()

    # ---------------------------------------------------------- checkboxes
    def _build_checkboxes(self):
        for w in self.checkbox_frame.winfo_children():
            w.destroy()
        self.channel_vars.clear()
        self.axis_vars.clear()
        self.row_frames.clear()

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
                enable_var = tk.BooleanVar(value=False)
                axis_var = tk.BooleanVar(value=(self.axis_side.get(col) == "right"))
                self.channel_vars[col] = enable_var
                self.axis_vars[col] = axis_var

                row_frame = ttk.Frame(self.checkbox_frame)
                row_frame.grid(row=row, column=0, sticky="w", padx=18)
                self.row_frames[col] = row_frame
                row += 1

                ttk.Checkbutton(
                    row_frame, text=col, variable=enable_var,
                    command=lambda c=col: self._toggle_channel(c)
                ).pack(side=tk.LEFT)
                ttk.Checkbutton(
                    row_frame, text="R", variable=axis_var, width=2,
                    command=lambda c=col: self._toggle_axis_side(c)
                ).pack(side=tk.LEFT, padx=(4, 0))

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
        for col, row_frame in self.row_frames.items():
            visible = (text in col.lower()) if text else True
            if visible:
                row_frame.grid()
            else:
                row_frame.grid_remove()

    # ------------------------------------------------------------- plotting
    def _target_axis(self, col):
        return self.ax2 if self.axis_side.get(col, "left") == "right" else self.ax

    def _autoscale(self, ax):
        ax.relim(visible_only=True)
        ax.autoscale_view()

    def _toggle_channel(self, col):
        is_on = self.channel_vars[col].get()
        target_ax = self._target_axis(col)

        if is_on:
            if col not in self.lines:
                color = COLOR_CYCLE[len(self.lines) % len(COLOR_CYCLE)]
                line, = target_ax.plot(self.df[self.x_col], self.df[col], label=col, linewidth=1, color=color)
                self.lines[col] = line
                marker, = target_ax.plot([], [], "o", color=LOCK_COLOR, markersize=6,
                                          markeredgecolor="black", markeredgewidth=0.6,
                                          visible=False, zorder=8)
                self.cur_markers[col] = marker
            else:
                self.lines[col].set_visible(True)
        else:
            if col in self.lines:
                self.lines[col].set_visible(False)
                if col in self.cur_markers:
                    self.cur_markers[col].set_visible(False)

        self._refresh_legend()
        if self.cursor_locked and self.cursor_idx is not None:
            self._update_cursor_display(self.cursor_idx)
        self.canvas.draw_idle()

    def _toggle_axis_side(self, col):
        side = "right" if self.axis_vars[col].get() else "left"
        self.axis_side[col] = side

        if col in self.lines:
            target_ax = self._target_axis(col)
            other_ax = self.ax if target_ax is self.ax2 else self.ax2

            old_line = self.lines[col]
            visible = old_line.get_visible()
            x_data, y_data, color = old_line.get_xdata(), old_line.get_ydata(), old_line.get_color()
            old_line.remove()
            new_line, = target_ax.plot(x_data, y_data, label=col, linewidth=1, color=color)
            new_line.set_visible(visible)
            self.lines[col] = new_line

            old_marker = self.cur_markers.get(col)
            marker_visible = old_marker.get_visible() if old_marker is not None else False
            marker_xy = old_marker.get_data() if old_marker is not None else ([], [])
            if old_marker is not None:
                old_marker.remove()
            new_marker, = target_ax.plot(*marker_xy, marker="o", linestyle="None", color=LOCK_COLOR,
                                          markersize=6, markeredgecolor="black", markeredgewidth=0.6,
                                          visible=marker_visible, zorder=8)
            self.cur_markers[col] = new_marker

            self._autoscale(target_ax)
            self._autoscale(other_ax)

        self._refresh_legend()
        if self.cursor_locked and self.cursor_idx is not None:
            self._update_cursor_display(self.cursor_idx)
        self.canvas.draw_idle()

    def _refresh_legend(self):
        visible = [l for l in self.lines.values() if l.get_visible()]
        if visible:
            self.ax.legend(visible, [l.get_label() for l in visible],
                            loc="upper left", bbox_to_anchor=(1.08, 1.0),
                            fontsize=8, borderaxespad=0., facecolor=BG,
                            edgecolor=BORDER, labelcolor=FG)
            self._autoscale(self.ax)
            self._autoscale(self.ax2)
            self.fig.subplots_adjust(right=0.76)
        else:
            leg = self.ax.get_legend()
            if leg:
                leg.remove()
        self.canvas.draw_idle()

    # ------------------------------------------------------- shared helpers
    def _format_x(self, val):
        if isinstance(val, pd.Timestamp):
            return val.strftime("%H:%M:%S.%f")[:-3]
        try:
            return f"{val:.6g}"
        except (TypeError, ValueError):
            return str(val)

    def _nearest_index(self, xdata):
        idx = int(np.searchsorted(self.xnum, xdata))
        idx = min(max(idx, 0), len(self.xnum) - 1)
        if idx > 0 and abs(self.xnum[idx - 1] - xdata) < abs(self.xnum[idx] - xdata):
            idx -= 1
        return idx

    def _visible_cols(self):
        return [c for c, l in self.lines.items() if l.get_visible()]

    # ------------------------------------------------------- cursor (1 total)
    def _update_cursor_display(self, idx):
        """Move the single cursor (vline + markers + readout) to row `idx` and show it."""
        self.cursor_idx = idx
        xval = self.xnum[idx]
        visible_cols = self._visible_cols()

        for col in visible_cols:
            y = self.df[col].iloc[idx]
            self.cur_markers[col].set_data([xval], [y])
            self.cur_markers[col].set_visible(True)
        for col, marker in self.cur_markers.items():
            if col not in visible_cols:
                marker.set_visible(False)

        self.cur_vline.set_xdata([xval, xval])
        self.cur_vline.set_visible(True)

        parts = [self._format_x(self.df[self.x_col].iloc[idx])]
        for col in visible_cols:
            y = self.df[col].iloc[idx]
            short = col if len(col) <= 26 else col[:23] + "..."
            tag = " (R)" if self.axis_side.get(col) == "right" else ""
            parts.append(f"{short}{tag}: {y:.4f}")
        label = "LOCKED\n" if self.cursor_locked else ""
        self.cur_text.set_text(label + "\n".join(parts))
        self.cur_text.set_visible(True)

    def _hide_cursor(self):
        changed = False
        if self.cur_vline.get_visible():
            self.cur_vline.set_visible(False)
            changed = True
        if self.cur_text.get_visible():
            self.cur_text.set_visible(False)
            changed = True
        for m in self.cur_markers.values():
            if m.get_visible():
                m.set_visible(False)
                changed = True
        return changed

    def _set_locked_style(self, locked):
        if locked:
            self.cur_vline.set_linestyle("-")
            self.cur_vline.set_linewidth(1.4)
            self.cur_vline.set_color(LOCK_COLOR)
            self.cur_text.set_bbox(dict(boxstyle="round", fc=LOCK_COLOR, ec="black", alpha=0.95))
            self.cur_text.set_color("black")
        else:
            self.cur_vline.set_linestyle("--")
            self.cur_vline.set_linewidth(0.8)
            self.cur_vline.set_color(HOVER_COLOR)
            self.cur_text.set_bbox(dict(boxstyle="round", fc=BG, ec=BORDER, alpha=0.9))
            self.cur_text.set_color(FG)

    def _on_motion(self, event):
        if self.cursor_locked:
            return  # frozen cursor ignores mouse movement entirely
        if self.df is None or event.inaxes not in (self.ax, self.ax2):
            if self._hide_cursor():
                self.canvas.draw_idle()
            return
        if event.xdata is None or self.xnum is None or len(self.xnum) == 0:
            return
        if not self._visible_cols():
            return

        idx = self._nearest_index(event.xdata)
        self._set_locked_style(False)
        self._update_cursor_display(idx)
        self.canvas.draw_idle()

    def _on_figure_leave(self, event):
        if self.cursor_locked:
            return
        if self._hide_cursor():
            self.canvas.draw_idle()

    def _on_click(self, event):
        if self.df is None or event.inaxes not in (self.ax, self.ax2):
            return
        if self.toolbar.mode != "":   # a zoom/pan tool is active in the toolbar; don't touch the cursor
            return

        if event.button == 3:        # right-click: release the cursor back to live hover-tracking
            self.unlock_cursor()
            return

        if event.button != 1 or event.xdata is None:
            return
        if not self._visible_cols():
            messagebox.showinfo("No channels enabled", "Enable at least one channel checkbox first.")
            return

        idx = self._nearest_index(event.xdata)
        self.cursor_locked = True
        self._set_locked_style(True)
        self._update_cursor_display(idx)
        self.canvas.draw_idle()

    def unlock_cursor(self):
        self.cursor_locked = False
        self._set_locked_style(False)
        self._hide_cursor()
        self.canvas.draw_idle()

    # ------------------------------------------------------------- export
    def save_image(self):
        if self.df is None:
            messagebox.showinfo("Nothing to save", "Load a CSV and enable some channels first.")
            return
        path = filedialog.asksaveasfilename(
            title="Save plot as image",
            defaultextension=".png",
            filetypes=[("PNG image", "*.png"), ("PDF", "*.pdf"), ("SVG", "*.svg"), ("All files", "*.*")]
        )
        if not path:
            return
        try:
            self.fig.savefig(path, dpi=150, facecolor=self.fig.get_facecolor())
            messagebox.showinfo("Saved", f"Plot saved to:\n{path}")
        except Exception as e:
            messagebox.showerror("Error saving image", str(e))


def main():
    initial_file = sys.argv[1] if len(sys.argv) > 1 else None
    app = CdaqPlotViewer(initial_file=initial_file)
    app.mainloop()


if __name__ == "__main__":
    main()

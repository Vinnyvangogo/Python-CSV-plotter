# Python-CSV-plotter

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

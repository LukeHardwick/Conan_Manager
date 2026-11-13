"""Light and dark colours for the Conan Mirror GUI."""
from __future__ import annotations

import sys
import tkinter as tk
from tkinter import ttk

# Text colours for log lines and tree rows (cmd/ok/err/warn), per theme.
# Every Tk widget sharing this dict means one apply_theme() call recolours everything.
LIGHT_TAGS = {"cmd": "#2060c0", "ok": "#208020", "err": "#c02020", "warn": "#b07000"}
DARK_TAGS = {"cmd": "#6ea8ff", "ok": "#5cc16a", "err": "#ff6b6b", "warn": "#e0a040"}
TAGS = dict(LIGHT_TAGS)

DARK = {
    "bg": "#202124",        # windows, frames, tabs
    "field": "#2b2c2f",     # entries, text boxes, lists
    "raised": "#34363a",    # buttons, selected tab, headings
    "hover": "#3e4045",
    "border": "#4a4c50",
    "fg": "#e6e6e6",
    "fg_dim": "#8a8a8a",    # disabled text
    "select": "#2f5d9e",    # selection highlight
}


def apply_theme(root: tk.Tk, dark: bool) -> None:
    """Pick the ttk theme and colours. Call before any widget is created: plain Tk widgets
    (Text, Listbox, dialogs) take their colours from the option database at creation time."""
    style = ttk.Style(root)
    if not dark:
        TAGS.update(LIGHT_TAGS)
        if sys.platform == "win32":
            style.theme_use("vista")
        return

    TAGS.update(DARK_TAGS)
    c = DARK
    style.theme_use("clam")  # the built-in theme that honours custom colours on every platform
    style.configure(".", background=c["bg"], foreground=c["fg"], fieldbackground=c["field"],
                    bordercolor=c["border"], darkcolor=c["bg"], lightcolor=c["raised"],
                    troughcolor=c["field"], selectbackground=c["select"], selectforeground="#ffffff",
                    insertcolor=c["fg"], arrowcolor=c["fg"], focuscolor=c["border"])
    style.map(".", foreground=[("disabled", c["fg_dim"])],
              background=[("disabled", c["bg"])])
    style.configure("TButton", background=c["raised"])
    style.map("TButton", background=[("disabled", c["bg"]), ("pressed", c["border"]),
                                     ("active", c["hover"])])
    for w in ("TCheckbutton", "TRadiobutton"):
        style.configure(w, indicatorbackground=c["field"], indicatorforeground=c["fg"])
        style.map(w, background=[("active", c["bg"])],
                  indicatorbackground=[("pressed", c["raised"]), ("selected", c["select"])])
    style.configure("TNotebook", background=c["bg"], bordercolor=c["border"])
    style.configure("TNotebook.Tab", background=c["bg"], padding=(10, 3))
    style.map("TNotebook.Tab", background=[("selected", c["raised"]), ("active", c["hover"])])
    style.configure("Treeview", background=c["field"], fieldbackground=c["field"], foreground=c["fg"])
    style.map("Treeview", background=[("selected", c["select"])], foreground=[("selected", "#ffffff")])
    style.configure("Treeview.Heading", background=c["raised"], foreground=c["fg"])
    style.map("Treeview.Heading", background=[("active", c["hover"])])
    style.map("TCombobox", fieldbackground=[("readonly", c["field"]), ("disabled", c["bg"])],
              foreground=[("readonly", c["fg"]), ("disabled", c["fg_dim"])],
              selectbackground=[("readonly", c["field"])], selectforeground=[("readonly", c["fg"])])
    style.map("TEntry", fieldbackground=[("readonly", c["bg"]), ("disabled", c["bg"])])
    style.configure("TScrollbar", background=c["raised"], arrowcolor=c["fg"])
    style.map("TScrollbar", background=[("active", c["hover"])])
    style.configure("TProgressbar", background=c["select"])
    style.configure("TLabelframe", background=c["bg"], bordercolor=c["border"])
    style.configure("TLabelframe.Label", background=c["bg"], foreground=c["fg"])

    # plain Tk widgets: Text, Listbox, the combobox drop-down, Toplevels and simpledialog boxes
    root.configure(background=c["bg"])
    for pattern, value in (
            ("*Background", c["bg"]), ("*Foreground", c["fg"]),
            ("*Text.Background", c["field"]), ("*Listbox.Background", c["field"]),
            ("*Entry.Background", c["field"]), ("*Button.Background", c["raised"]),
            ("*activeBackground", c["hover"]), ("*activeForeground", c["fg"]),
            ("*selectBackground", c["select"]), ("*selectForeground", "#ffffff"),
            ("*insertBackground", c["fg"]), ("*highlightBackground", c["bg"]),
            ("*highlightColor", c["border"]), ("*disabledForeground", c["fg_dim"]),
            ("*TCombobox*Listbox.background", c["field"]),
            ("*TCombobox*Listbox.foreground", c["fg"])):
        root.option_add(pattern, value)

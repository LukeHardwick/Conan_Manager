"""Light and dark mode for the Conan Mirror GUI. Switches while the GUI is running.

macOS: Tk's native (aqua) theme, with each window's appearance set to light or dark, so the GUI
looks like any other Mac app in either mode, whatever the system setting is.

Windows / Linux: the native theme for light mode. Dark mode uses the built-in `clam` theme (the
only one whose colours can be changed) with a dark palette, plus matching colours for the plain
Tk widgets (text boxes, lists, dialogs) that ttk themes don't cover.
"""
from __future__ import annotations

import sys
import tkinter as tk
from tkinter import ttk

# Text colours for log lines and tree rows. Widgets read TAGS when they are created, and
# Theme.apply() recolours the ones that already exist.
LIGHT_TAGS = {"cmd": "#2060c0", "ok": "#208020", "err": "#c02020", "warn": "#b07000"}
DARK_TAGS = {"cmd": "#6ea8ff", "ok": "#5cc16a", "err": "#ff6b6b", "warn": "#e0a040"}
TAGS = dict(LIGHT_TAGS)
TREE_TAGS = {"good": "ok", "bad": "err", "warn": "warn"}     # Treeview tag -> TAGS key
TEXT_TAGS = ("cmd", "ok", "err")                              # tags used in the log box

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

MAC = sys.platform == "darwin"

# Colours of plain Tk widgets in dark mode (not used on macOS), by widget class.
_FIELD = {"background": DARK["field"], "foreground": DARK["fg"], "selectbackground": DARK["select"],
          "selectforeground": "#ffffff", "insertbackground": DARK["fg"],
          "highlightbackground": DARK["bg"], "highlightcolor": DARK["border"]}
_PLAIN = {"background": DARK["bg"], "foreground": DARK["fg"], "highlightbackground": DARK["bg"]}
DARK_CLASSIC: dict[str, dict[str, str]] = {
    "Text": _FIELD, "Listbox": _FIELD, "Entry": _FIELD,
    "Tk": {"background": DARK["bg"]}, "Toplevel": {"background": DARK["bg"]},
    "Frame": _PLAIN, "Label": _PLAIN,
    "Button": {**_PLAIN, "background": DARK["raised"], "activebackground": DARK["hover"],
               "activeforeground": DARK["fg"]},
}
# macOS: text boxes and lists get explicit colours matching the system ones. Tk resolves names
# like systemTextColor only when a widget is drawn in the current appearance, and widgets on a
# hidden notebook page keep the old ones.
_MAC_FIELD = {
    False: {"background": "#ffffff", "foreground": "#000000", "selectbackground": "#b3d7ff",
            "selectforeground": "#000000", "insertbackground": "#000000"},
    True: {"background": "#1e1e1e", "foreground": "#dfdfdf", "selectbackground": "#3f638b",
           "selectforeground": "#ffffff", "insertbackground": "#dfdfdf"},
}
MAC_CLASSIC: dict[bool, dict[str, dict[str, str]]] = {
    dark: {"Text": colours, "Listbox": colours} for dark, colours in _MAC_FIELD.items()}
# The same colours for widgets created later (dialogs, the combobox drop-down list).
DARK_OPTIONS = (
    ("*Background", DARK["bg"]), ("*Foreground", DARK["fg"]),
    ("*Text.Background", DARK["field"]), ("*Listbox.Background", DARK["field"]),
    ("*Entry.Background", DARK["field"]), ("*Button.Background", DARK["raised"]),
    ("*activeBackground", DARK["hover"]), ("*activeForeground", DARK["fg"]),
    ("*selectBackground", DARK["select"]), ("*selectForeground", "#ffffff"),
    ("*insertBackground", DARK["fg"]), ("*highlightBackground", DARK["bg"]),
    ("*highlightColor", DARK["border"]), ("*disabledForeground", DARK["fg_dim"]),
)


class Theme:
    """Create once, before the other widgets; then apply(dark) at any time."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.style = ttk.Style(root)
        self.dark = False
        names = self.style.theme_names()
        self.light_theme = "vista" if "vista" in names else self.style.theme_use()
        self.light_classic = {} if MAC else self._default_colours()
        self._stale_tabs: set[str] = set()     # notebook pages still drawn in the old appearance
        if MAC:     # windows opened later (dialogs, the profile wizard) get the same appearance
            root.bind_class("Toplevel", "<Map>", self._on_map, add="+")
            root.bind_class("TNotebook", "<<NotebookTabChanged>>", self._on_tab, add="+")

    def apply(self, dark: bool) -> None:
        self.dark = dark
        TAGS.update(DARK_TAGS if dark else LIGHT_TAGS)
        if MAC:
            for path in self._walk(str(self.root)):
                if self._class(path) in ("Tk", "Toplevel"):
                    self._mac_appearance(path)
            self._recolour(MAC_CLASSIC[dark])
            self._redraw()
            # Pages of a notebook that are hidden now miss that redraw: do it when one is shown.
            for path in self._walk(str(self.root)):
                if self._class(path) == "TNotebook":
                    current = str(self.root.tk.call(path, "select"))
                    self._stale_tabs |= {str(t) for t in self.root.tk.splitlist(
                        self.root.tk.call(path, "tabs")) if str(t) != current}
        elif dark:
            self._dark_ttk()
            for pattern, value in DARK_OPTIONS:
                self.root.option_add(pattern, value)
            self._recolour(DARK_CLASSIC)
        else:
            self.style.theme_use(self.light_theme)
            self.root.option_clear()
            self._recolour(self.light_classic)
        self._recolour_tags()

    # ---- macOS ------------------------------------------------------------
    def _mac_appearance(self, path: str) -> None:
        try:
            self.root.tk.call("::tk::unsupported::MacWindowStyle", "appearance", path,
                              "darkaqua" if self.dark else "aqua")
        except tk.TclError:
            pass    # Tk older than 8.6.10: the window keeps following the system setting

    def _on_map(self, event: tk.Event[tk.Misc]) -> None:
        self._mac_appearance(str(event.widget))

    def _redraw(self) -> None:
        """Existing ttk widgets keep their old rendering after an appearance change. Selecting
        the theme again sends them <<ThemeChanged>>, so they redraw."""
        self.style.theme_use(self.style.theme_use())

    def _on_tab(self, event: tk.Event[tk.Misc]) -> None:
        page = str(self.root.tk.call(str(event.widget), "select"))
        if page in self._stale_tabs:
            self._stale_tabs.discard(page)
            self.root.after_idle(self._redraw)
            self.root.after_idle(self._repaint, page)

    def _repaint(self, page: str) -> None:
        """Lists and text boxes on a page that was hidden during the switch don't repaint their
        contents when shown. A geometry change (border width up and back) makes them."""
        call = self.root.tk.call
        for path in self._walk(page):
            if self._class(path) in ("Text", "Listbox"):
                width = call(path, "cget", "-borderwidth")
                call(path, "configure", "-borderwidth", int(self.root.winfo_fpixels(width)) + 1)
                call(path, "configure", "-borderwidth", width)

    # ---- Windows / Linux --------------------------------------------------
    def _dark_ttk(self) -> None:
        c, style = DARK, self.style
        style.theme_use("clam")
        style.configure(".", background=c["bg"], foreground=c["fg"], fieldbackground=c["field"],
                        bordercolor=c["border"], darkcolor=c["bg"], lightcolor=c["raised"],
                        troughcolor=c["field"], selectbackground=c["select"], selectforeground="#ffffff",
                        insertcolor=c["fg"], arrowcolor=c["fg"], focuscolor=c["border"])
        style.map(".", foreground=[("disabled", c["fg_dim"])], background=[("disabled", c["bg"])])
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

    def _default_colours(self) -> dict[str, dict[str, str]]:
        """The platform's own colours for plain Tk widgets, to go back to in light mode."""
        out: dict[str, dict[str, str]] = {
            "Tk": {"background": str(self.root.cget("background"))}}
        probes: dict[str, tk.Widget] = {"Text": tk.Text(self.root), "Listbox": tk.Listbox(self.root),
                                        "Entry": tk.Entry(self.root), "Frame": tk.Frame(self.root),
                                        "Label": tk.Label(self.root), "Button": tk.Button(self.root)}
        for cls, w in probes.items():
            out[cls] = {opt: str(w.cget(opt)) for opt in DARK_CLASSIC[cls] if opt in w.keys()}
            w.destroy()
        out["Toplevel"] = {"background": out["Frame"]["background"]}
        return out

    def _recolour(self, colours: dict[str, dict[str, str]]) -> None:
        for path in self._walk(str(self.root)):
            for opt, value in colours.get(self._class(path), {}).items():
                try:
                    self.root.tk.call(path, "configure", f"-{opt}", value)
                except tk.TclError:
                    pass    # this widget has no such option

    # ---- shared -------------------------------------------------------------
    def _recolour_tags(self) -> None:
        for path in self._walk(str(self.root)):
            cls = self._class(path)
            if cls == "Text":
                for tag in TEXT_TAGS:
                    self.root.tk.call(path, "tag", "configure", tag, "-foreground", TAGS[tag])
            elif cls == "Treeview":
                for tag, key in TREE_TAGS.items():
                    self.root.tk.call(path, "tag", "configure", tag, "-foreground", TAGS[key])

    def _class(self, path: str) -> str:
        return str(self.root.tk.call("winfo", "class", path))

    def _walk(self, path: str) -> list[str]:
        """Every widget path under `path`, including Tk-internal ones Python doesn't know."""
        out = [path]
        for child in self.root.tk.splitlist(self.root.tk.call("winfo", "children", path)):
            out += self._walk(str(child))
        return out

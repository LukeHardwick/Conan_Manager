"""The Conan Mirror GUI: the main window and its tabs, plus the profile wizard."""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, TypeVarTuple

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText

import internal_recipes as ir
import team_config as tc
from gui_theme import TAGS, TREE_TAGS, Theme
from mirror_core import (APP_NAME, HERE, MANIFEST_FILE, MSVC_VERSIONS, SETTINGS_FILE, WORK_DIR, Cancelled,
                         Conan, ConanError, Mirror, Options, PlanRow, apply_config, load_manifest,
                         load_settings, make_profile, natural_key, open_folder, run_jobs, run_verify,
                         save_manifest, save_settings, slash, source_remotes, split_refs)

Ts = TypeVarTuple("Ts")

# Settings edited as text on the Settings tab. Literal keys let mypy check them against Settings.
TextSetting = Literal["conan_exe", "center_remote", "center_url", "proxy", "nexus_remote", "nexus_url",
                      "raw_url", "cacert_path", "nexus_user", "internal_remote", "conan_home",
                      "build_profile"]


class Tree(ttk.Treeview):
    """A Treeview inside its own frame with scrollbars: place `frame`, not the tree."""
    frame: ttk.Frame


class ProfileWizard(tk.Toplevel):
    """Generate a matrix of profiles (cppstd x build_type)."""

    def __init__(self, master: tk.Tk, on_create: Callable[[dict[str, str]], None]) -> None:
        super().__init__(master)
        self.title("New profiles from template")
        self.on_create = on_create
        self.transient(master)
        f = ttk.Frame(self, padding=12)
        f.pack(fill="both", expand=True)

        self.v = {k: tk.StringVar(value=d) for k, d in {
            "os": "Windows", "arch": "x86_64", "compiler": "msvc", "version": "194",
            "runtime": "dynamic", "cmake_gen": "", "prefix": "win-msvc194"}.items()}
        choices = {
            "os": ["Windows", "Linux", "Macos"],
            "arch": ["x86_64", "armv8", "x86"],
            "compiler": ["msvc", "gcc", "clang", "apple-clang"],
            "version": MSVC_VERSIONS + ["13", "12", "11", "17", "16"],
            "runtime": ["dynamic", "static"],
            "cmake_gen": ["", "Ninja", "Visual Studio 17 2022"],
        }
        labels = {"os": "OS", "arch": "Arch", "compiler": "Compiler", "version": "Compiler version",
                  "runtime": "MSVC runtime", "cmake_gen": "CMake generator (optional)",
                  "prefix": "Name prefix"}
        for i, k in enumerate(labels):
            ttk.Label(f, text=labels[k]).grid(row=i, column=0, sticky="w", pady=2)
            w: ttk.Entry
            if k in choices:
                w = ttk.Combobox(f, textvariable=self.v[k], values=choices[k], width=28)
            else:
                w = ttk.Entry(f, textvariable=self.v[k], width=30)
            w.grid(row=i, column=1, sticky="ew", pady=2)
        for k in ("os", "compiler", "version"):
            self.v[k].trace_add("write", lambda *_: self._auto_prefix())

        r = len(labels)
        ttk.Label(f, text="C++ standards").grid(row=r, column=0, sticky="nw", pady=(8, 2))
        sf = ttk.Frame(f)
        sf.grid(row=r, column=1, sticky="w", pady=(8, 2))
        self.stds = {s: tk.BooleanVar(value=s in ("20",)) for s in ("11", "14", "17", "20", "23")}
        for s, var in self.stds.items():
            ttk.Checkbutton(sf, text=f"C++{s}", variable=var).pack(side="left")
        ttk.Label(f, text="Build types").grid(row=r + 1, column=0, sticky="nw", pady=2)
        bf = ttk.Frame(f)
        bf.grid(row=r + 1, column=1, sticky="w", pady=2)
        self.bts = {b: tk.BooleanVar(value=b in ("Release", "Debug"))
                    for b in ("Release", "Debug", "RelWithDebInfo")}
        for b, var in self.bts.items():
            ttk.Checkbutton(bf, text=b, variable=var).pack(side="left")
        ttk.Label(f, foreground="gray", wraplength=380, justify="left",
                  text="MSVC has no C++11 mode (minimum is 14). ConanCenter binaries are usually "
                       "built with the compiler's default standard; Conan's compatibility plugin "
                       "reuses them for other standards where possible.").grid(
            row=r + 2, column=0, columnspan=2, sticky="w", pady=6)
        btns = ttk.Frame(f)
        btns.grid(row=r + 3, column=0, columnspan=2, sticky="e")
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="Create", command=self._create).pack(side="right", padx=6)
        f.columnconfigure(1, weight=1)
        self.grab_set()

    def _auto_prefix(self) -> None:
        os_short = {"Windows": "win", "Linux": "linux", "Macos": "mac"}.get(self.v["os"].get(), "x")
        self.v["prefix"].set(f"{os_short}-{self.v['compiler'].get()}{self.v['version'].get()}")

    def _create(self) -> None:
        v = {k: x.get().strip() for k, x in self.v.items()}
        stds = [s for s, x in self.stds.items() if x.get()]
        bts = [b for b, x in self.bts.items() if x.get()]
        if not stds or not bts:
            messagebox.showwarning(APP_NAME, "Pick at least one C++ standard and build type.", parent=self)
            return
        out: dict[str, str] = {}
        for std in stds:
            if v["compiler"] == "msvc" and std == "11":
                continue
            for bt in bts:
                name = f"{v['prefix']}-cpp{std}-{bt.lower()}"
                out[name] = make_profile(v["os"], v["arch"], v["compiler"], v["version"], std, bt,
                                         v["runtime"], v["cmake_gen"])
        self.destroy()
        self.on_create(out)


# --------------------------------------------------------------------------- GUI
class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1200x800")
        self.minsize(900, 600)
        self.settings = load_settings()
        self.creds: tuple[str, str] = ("", "")
        self.conan = Conan(self)
        # worker threads never touch widgets: they queue log lines (text, tag) or UI callbacks
        self.q: queue.Queue[tuple[str, str] | Callable[[], object]] = queue.Queue()
        self.working = False    # a task is running (not `busy`: that would hide Tk's busy())
        self.profiles: list[str] = []
        self.current_profile: str | None = None

        self._build_ui()
        self.after(100, self._drain)
        self.run_task("Loading profiles", self._task_load_profiles)

    # ---- threading plumbing ---------------------------------------------
    def log(self, text: str, tag: str = "") -> None:
        self.q.put((text, tag))

    def ui(self, fn: Callable[[*Ts], object], *args: *Ts) -> None:
        """Run fn(*args) on the Tk thread. Safe to call from a worker thread."""
        self.q.put(lambda: fn(*args))

    def _drain(self) -> None:
        try:
            while True:
                item = self.q.get_nowait()
                try:
                    if isinstance(item, tuple):
                        self._append_log(*item)
                    else:
                        item()
                except Exception as e:  # noqa: BLE001 - one bad update must not stop the queue
                    try:
                        self._append_log(f"ERROR in UI update: {type(e).__name__}: {e}", "err")
                    except Exception:  # noqa: BLE001
                        pass
        except queue.Empty:
            pass
        finally:
            self.after(80, self._drain)

    def run_task(self, title: str, fn: Callable[[*Ts], object], *args: *Ts) -> None:
        if self.working:
            messagebox.showinfo(APP_NAME, "Another task is running - wait or press Cancel.")
            return
        self.working = True
        self.conan.cancelled = False
        self.status.set(f"{title}...")
        self.progress.start(12)
        self.cancel_btn.state(["!disabled"])

        def worker() -> None:
            try:
                fn(*args)
                self.ui(self.status.set, f"{title}: done")
            except Cancelled:
                self.log("Cancelled.", "err")
                self.ui(self.status.set, f"{title}: cancelled")
            except ConanError as e:
                self.log(f"ERROR: {e}", "err")
                self.ui(self.status.set, f"{title}: failed")
            except Exception as e:  # noqa: BLE001
                self.log(f"ERROR: {type(e).__name__}: {e}", "err")
                self.ui(self.status.set, f"{title}: failed")
            finally:
                self.ui(self._task_done)

        threading.Thread(target=worker, daemon=True).start()

    def _task_done(self) -> None:
        self.working = False
        self.progress.stop()
        self.cancel_btn.state(["disabled"])

    # ---- layout -----------------------------------------------------------
    def _build_ui(self) -> None:
        self.theme = Theme(self)
        self.theme.apply(bool(self.settings.get("dark_mode")))
        paned = ttk.PanedWindow(self, orient="vertical")
        paned.pack(fill="both", expand=True, padx=6, pady=(6, 0))
        self.nb = ttk.Notebook(paned)
        paned.add(self.nb, weight=3)

        logf = ttk.Frame(paned)
        paned.add(logf, weight=1)
        bar = ttk.Frame(logf)
        bar.pack(fill="x")
        ttk.Label(bar, text="Log").pack(side="left")
        ttk.Button(bar, text="Clear", command=lambda: self.logbox.delete("1.0", "end")).pack(side="right")
        mono = ("Consolas", 9) if sys.platform == "win32" else ("Menlo", 11)
        self.logbox = ScrolledText(logf, height=10, font=mono, wrap="none")
        self.logbox.pack(fill="both", expand=True)
        for tag in ("cmd", "err", "ok"):
            self.logbox.tag_configure(tag, foreground=TAGS[tag])

        sb = ttk.Frame(self)
        sb.pack(fill="x", padx=6, pady=4)
        self.status = tk.StringVar(value="Ready")
        ttk.Label(sb, textvariable=self.status).pack(side="left")
        self.cancel_btn = ttk.Button(sb, text="Cancel", command=self.conan.cancel)
        self.cancel_btn.pack(side="right")
        self.cancel_btn.state(["disabled"])
        self.progress = ttk.Progressbar(sb, mode="indeterminate", length=160)
        self.progress.pack(side="right", padx=6)

        self._build_search_tab()
        self._build_plan_tab()
        self._build_internal_tab()
        self._build_profiles_tab()
        self._build_settings_tab()

    def _append_log(self, text: str, tag: str) -> None:
        at_end = self.logbox.yview()[1] > 0.98
        self.logbox.insert("end", text + "\n", tag)
        if int(self.logbox.index("end-1c").split(".")[0]) > 20000:
            self.logbox.delete("1.0", "2000.0")
        if at_end:
            self.logbox.see("end")

    @staticmethod
    def _tree(parent: tk.Misc, cols: dict[str, int],
              selectmode: Literal["extended", "browse", "none"] = "browse") -> Tree:
        frame = ttk.Frame(parent)
        tv = Tree(frame, columns=list(cols)[1:], selectmode=selectmode)
        for i, (c, w) in enumerate(cols.items()):
            cid = "#0" if i == 0 else c
            tv.heading(cid, text=c, anchor="w")
            tv.column(cid, width=w, stretch=(i == 0), anchor="w")
        ys = ttk.Scrollbar(frame, orient="vertical", command=tv.yview)
        xs = ttk.Scrollbar(frame, orient="horizontal", command=tv.xview)
        tv.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        tv.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        tv.frame = frame
        for tag, key in TREE_TAGS.items():
            tv.tag_configure(tag, foreground=TAGS[key])
        return tv

    # ---- Search tab -----------------------------------------------------
    def _build_search_tab(self) -> None:
        tab = ttk.Frame(self.nb, padding=6)
        self.nb.add(tab, text="Search & Mirror")

        top = ttk.Frame(tab)
        top.pack(fill="x")
        ttk.Label(top, text="Search ConanCenter + internal:").pack(side="left")
        self.search_var = tk.StringVar()
        e = ttk.Entry(top, textvariable=self.search_var, width=30)
        e.pack(side="left", padx=4)
        e.bind("<Return>", lambda _e: self.on_search())
        ttk.Button(top, text="Search", command=self.on_search).pack(side="left")
        self.check_nexus = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="compare with Nexus", variable=self.check_nexus).pack(side="left", padx=8)
        ttk.Label(top, text="(wildcards ok: boost*, *json*)", foreground="gray").pack(side="left")

        body = ttk.PanedWindow(tab, orient="horizontal")
        body.pack(fill="both", expand=True, pady=6)

        # results
        self.results = self._tree(body, {"Reference": 220, "In Nexus": 80})
        body.add(self.results.frame, weight=1)
        self.results.bind("<<TreeviewSelect>>", self.on_result_select)

        # binaries
        mid = ttk.Frame(body)
        body.add(mid, weight=3)
        self.bin_label = tk.StringVar(value="Select a version to list its ConanCenter binaries")
        ttk.Label(mid, textvariable=self.bin_label).pack(anchor="w")
        self.bins = self._tree(mid, {"Package ID": 120, "os": 70, "arch": 60, "compiler": 80,
                                     "ver": 45, "cppstd": 50, "build_type": 80, "runtime": 65,
                                     "shared": 55, "In Nexus": 70})
        self.bins.frame.pack(fill="both", expand=True)

        # mirror panel
        right = ttk.LabelFrame(body, text="Mirror", padding=6)
        body.add(right, weight=1)
        ttk.Label(right, text="References or conanfiles - one per line:").pack(anchor="w")
        self.refs_text = tk.Text(right, height=5, width=28, wrap="none")
        self.refs_text.pack(fill="x")
        rf = ttk.Frame(right)
        rf.pack(fill="x", pady=(2, 0))
        for txt, cmd in (("Add conanfile...", self.on_add_conanfile),
                         ("Load manifest", self.on_manifest_load),
                         ("Save manifest", self.on_manifest_save)):
            ttk.Button(rf, text=txt, command=cmd).pack(side="left", expand=True, fill="x")
        ttk.Label(right, text="Profiles (multi-select):").pack(anchor="w", pady=(6, 0))
        lf = ttk.Frame(right)
        lf.pack(fill="both", expand=True)
        self.prof_list = tk.Listbox(lf, selectmode="extended", exportselection=False, height=8)
        self.prof_list.pack(side="left", fill="both", expand=True)
        ps = ttk.Scrollbar(lf, command=self.prof_list.yview)
        ps.pack(side="right", fill="y")
        self.prof_list.configure(yscrollcommand=ps.set)

        self.opt = {
            "build_missing": tk.BooleanVar(value=True),
            "fetch_sources": tk.BooleanVar(value=True),
            "skip_existing": tk.BooleanVar(value=True),
            "dry_run": tk.BooleanVar(value=False),
            "verify": tk.BooleanVar(value=True),
            "verify_sources": tk.BooleanVar(value=True),
        }
        for k, txt in (("build_missing", "Build missing binaries from source"),
                       ("fetch_sources", "Fetch sources (upload to raw repo)"),
                       ("skip_existing", "Skip if already complete in Nexus"),
                       ("dry_run", "Dry run (don't actually upload)"),
                       ("verify", "Verify afterwards (install from Nexus only)"),
                       ("verify_sources", "Verify sources too (raw repo, no login)")):
            ttk.Checkbutton(right, text=txt, variable=self.opt[k]).pack(anchor="w")
        bf = ttk.Frame(right)
        bf.pack(fill="x", pady=(6, 0))
        ttk.Button(bf, text="Plan", command=self.on_plan).pack(side="left", expand=True, fill="x")
        ttk.Button(bf, text="Mirror to Nexus", command=self.on_mirror).pack(side="left", expand=True,
                                                                             fill="x", padx=(4, 0))
        ttk.Button(right, text="Verify only (install from Nexus)", command=self.on_verify).pack(
            fill="x", pady=(4, 0))

    def options(self) -> Options:
        return Options(**{k: v.get() for k, v in self.opt.items()})

    def selected_refs(self) -> list[str]:
        return split_refs(self.refs_text.get("1.0", "end"))

    def _set_refs(self, refs: list[str]) -> None:
        self.refs_text.delete("1.0", "end")
        self.refs_text.insert("1.0", "\n".join(refs))

    def on_add_conanfile(self) -> None:
        f = filedialog.askopenfilename(title="Project conanfile", filetypes=[
            ("conanfile", "conanfile.py conanfile.txt"), ("All files", "*")])
        if f and f not in self.selected_refs():
            self._set_refs(self.selected_refs() + [str(Path(f))])

    def on_manifest_load(self) -> None:
        f = filedialog.askopenfilename(title="Load manifest", initialdir=str(HERE),
                                       initialfile=MANIFEST_FILE.name, filetypes=[("JSON", "*.json")])
        if not f:
            return
        try:
            jobs = load_manifest(Path(f))
        except ConanError as e:
            messagebox.showerror(APP_NAME, str(e))
            return
        refs = list(dict.fromkeys(r for r, _ in jobs))
        profs = set(p for _, p in jobs)
        self._set_refs(refs)
        self.prof_list.selection_clear(0, "end")
        for i, name in enumerate(self.profiles):
            if name in profs:
                self.prof_list.selection_set(i)
        missing = sorted(profs - set(self.profiles))
        self.log(f"Loaded {len(refs)} package(s) x {len(profs)} profile(s) from {f}", "ok")
        if missing:
            self.log(f"WARNING: profiles in the manifest that do not exist here: {', '.join(missing)}", "err")
        if len(jobs) != len(refs) * len(profs):
            self.log("NOTE: this manifest gives some packages their own profile list; the GUI mirrors "
                     "every listed package with every selected profile. Use --manifest on the command "
                     "line to follow it exactly.")

    def on_manifest_save(self) -> None:
        refs, profs = self.selected_refs(), self.selected_profiles()
        if not refs or not profs:
            messagebox.showwarning(APP_NAME, "Enter at least one package and select at least one profile.")
            return
        f = filedialog.asksaveasfilename(title="Save manifest", initialdir=str(HERE),
                                         initialfile=MANIFEST_FILE.name, defaultextension=".json",
                                         filetypes=[("JSON", "*.json")])
        if f:
            save_manifest(Path(f), refs, profs)
            self.log(f"Manifest written to {f} - commit it so the mirror can be repeated", "ok")

    def selected_profiles(self) -> list[str]:
        box = self.prof_list
        return [str(box.get(i)) for i in range(box.size()) if box.selection_includes(i)]

    def _show_tab(self, index: int) -> None:
        self.nb.select(index)  # type: ignore[no-untyped-call]  # unannotated in typeshed

    def on_search(self) -> None:
        pat = self.search_var.get().strip()
        if not pat:
            return
        if "*" not in pat and "/" not in pat:
            pat = f"*{pat}*"
        self.run_task(f"Searching '{pat}'", self._task_search, pat, self.check_nexus.get())

    def _task_search(self, pat: str, compare: bool) -> None:
        s = self.settings
        refs: list[str] = []
        for remote in source_remotes(self.conan, s):
            refs += [r for r in self.conan.search(pat, remote) if r not in refs]
        in_nexus: set[str] | None = None
        if compare and s["nexus_url"]:
            try:
                in_nexus = set(self.conan.search(pat, s["nexus_remote"]))
            except ConanError as e:
                self.log(f"Nexus search failed ({e}); falling back to per-name list", "err")
                in_nexus = set()
                for name in sorted({r.split("/")[0] for r in refs}):
                    in_nexus |= set(self.conan.list_remote(f"{name}/*", s["nexus_remote"]))
        self.log(f"{len(refs)} versions found", "ok")
        self.ui(self._fill_results, refs, in_nexus)

    def _fill_results(self, refs: list[str], in_nexus: set[str] | None) -> None:
        tv = self.results
        tv.delete(*tv.get_children())
        groups: dict[str, list[str]] = {}
        for r in refs:
            groups.setdefault(r.split("/")[0], []).append(r)
        for name in sorted(groups, key=natural_key):
            vers = sorted(groups[name], key=natural_key, reverse=True)
            have = sum(1 for v in vers if in_nexus and v in in_nexus)
            summary = "?" if in_nexus is None else f"{have}/{len(vers)}"
            parent = tv.insert("", "end", text=name, values=(summary,), open=len(groups) == 1)
            for v in vers:
                mark = "?" if in_nexus is None else ("yes" if v in in_nexus else "no")
                tv.insert(parent, "end", iid=v, text=v, values=(mark,),
                          tags=("good",) if mark == "yes" else ())

    def on_result_select(self, _e: object = None) -> None:
        sel = self.results.selection()
        if not sel or "/" not in sel[0]:
            return
        ref = sel[0]
        current = self.selected_refs()
        if ref not in current:
            # replace an existing line for the same package, otherwise append
            name = ref.split("/")[0]
            current = [r for r in current if r.split("/")[0] != name] + [ref]
            self.refs_text.delete("1.0", "end")
            self.refs_text.insert("1.0", "\n".join(current))
        self.run_task(f"Listing binaries of {ref}", self._task_bins, ref)

    def _task_bins(self, ref: str) -> None:
        s = self.settings
        center = self.conan.list_remote(f"{ref}#latest:*", s["center_remote"], quiet=False)
        revs = center.get(ref, {}).get("revisions", {})
        if not revs:
            self.ui(self._fill_bins, ref, "", {}, None)
            return
        rrev, data = next(iter(revs.items()))
        pkgs: dict[str, Any] = data.get("packages", {}) or {}
        nexus_pkgs: set[str] | None = None
        if s["nexus_url"]:
            try:
                n = self.conan.list_remote(f"{ref}#{rrev}:*", s["nexus_remote"])
                nrevs = n.get(ref, {}).get("revisions", {})
                nexus_pkgs = set((nrevs.get(rrev) or {}).get("packages", {}) or {})
            except ConanError as e:
                self.log(str(e), "err")
        self.ui(self._fill_bins, ref, rrev, pkgs, nexus_pkgs)

    def _fill_bins(self, ref: str, rrev: str, pkgs: dict[str, Any], nexus_pkgs: set[str] | None) -> None:
        tv = self.bins
        tv.delete(*tv.get_children())
        self.bin_label.set(f"{ref}#{rrev[:8]} - {len(pkgs)} prebuilt binaries on ConanCenter"
                           + ("" if pkgs else " (will need building from source)"))

        def key(item: tuple[str, Any]) -> tuple[str, str, str, str]:
            st: dict[str, str] = item[1].get("info", {}).get("settings", {})
            return (st.get("os", ""), st.get("compiler", ""), st.get("compiler.version", ""),
                    st.get("build_type", ""))

        for pid, p in sorted(pkgs.items(), key=key):
            info = p.get("info", {})
            st, op = info.get("settings", {}), info.get("options", {})
            have = "?" if nexus_pkgs is None else ("yes" if pid in nexus_pkgs else "no")
            tv.insert("", "end", text=pid, values=(
                st.get("os", "-"), st.get("arch", "-"), st.get("compiler", "-"),
                st.get("compiler.version", "-"), st.get("compiler.cppstd", "-"),
                st.get("build_type", "-"),
                st.get("compiler.runtime", st.get("compiler.libcxx", "-")),
                op.get("shared", "-"), have), tags=("good",) if have == "yes" else ())

    def _check_ready(self, need_nexus: bool = True) -> tuple[list[str], list[str]] | None:
        refs, profs = self.selected_refs(), self.selected_profiles()
        if not refs:
            messagebox.showwarning(APP_NAME, "Enter or select at least one reference (e.g. fmt/11.0.2).")
            return None
        if not profs:
            messagebox.showwarning(APP_NAME, "Select at least one profile in the list.")
            return None
        if need_nexus and not self.settings["nexus_url"]:
            messagebox.showwarning(APP_NAME, "Set the Nexus URL in Settings and press 'Apply to Conan'.")
            return None
        return refs, profs

    def on_plan(self) -> None:
        ready = self._check_ready()
        if ready:
            self.plan_tree.delete(*self.plan_tree.get_children())
            self._show_tab(1)
            self.run_task("Planning", self._task_plan, *ready, self.options())

    def _task_plan(self, refs: list[str], profs: list[str], opts: Options) -> None:
        m = Mirror(self.conan, self.settings)
        for ref in refs:
            for p in profs:
                self.log(f"== Plan {ref} [{p}]", "cmd")
                self.ui(self._add_plan_rows, ref, m.plan(ref, p, opts))

    def on_mirror(self) -> None:
        ready = self._check_ready()
        if not ready:
            return
        opts = self.options()
        # conan upload sends backup sources whenever the raw repo is configured, whatever the
        # checkboxes say, and internal packages download their sources from it.
        if self.settings["raw_url"] and not self.creds[0]:
            if messagebox.askyesno(APP_NAME, "The Nexus raw repo needs your Nexus credentials for this "
                                             "session (to upload sources, and to download the sources "
                                             "of internal packages). Log in now?"):
                if not self.ask_creds():
                    return
        refs, profs = ready
        n = len(refs) * len(profs)
        if not opts.dry_run and not messagebox.askyesno(
                APP_NAME, f"Mirror {len(refs)} package(s) x {len(profs)} profile(s) "
                          f"({n} job(s)) to '{self.settings['nexus_remote']}'?"):
            return
        self.plan_tree.delete(*self.plan_tree.get_children())
        self._show_tab(1)
        self.run_task("Mirroring", self._task_mirror, refs, profs, opts)

    def _task_mirror(self, refs: list[str], profs: list[str], opts: Options) -> None:
        m = Mirror(self.conan, self.settings)
        results = run_jobs(m, [(r, p) for r in refs for p in profs], opts, self.log,
                           lambda r, rows: self.ui(self._add_plan_rows, r, rows))
        if any("FAILED" in r[2] for r in results):
            raise ConanError("some jobs failed - see the log")

    def on_verify(self) -> None:
        ready = self._check_ready()
        if not ready:
            return
        refs, profs = ready
        self.run_task("Verifying", self._task_verify, refs, profs, self.options())

    def _task_verify(self, refs: list[str], profs: list[str], opts: Options) -> None:
        m = Mirror(self.conan, self.settings)
        results = run_verify(m, [(r, p) for r in refs for p in profs], opts, self.log)
        if any("FAILED" in r[2] for r in results):
            raise ConanError("some packages cannot be installed from Nexus alone - see the log")

    # ---- Plan tab ---------------------------------------------------------
    def _build_plan_tab(self) -> None:
        tab = ttk.Frame(self.nb, padding=6)
        self.nb.add(tab, text="Plan / Status")
        ttk.Label(tab, text="Every package in the dependency graph per profile, compared with Nexus. "
                            "Green = already in Nexus, orange = will be built from source, "
                            "red = cannot be satisfied.", foreground="gray").pack(anchor="w")
        self.plan_tree = self._tree(tab, {"Reference": 260, "Profile": 190, "Context": 60,
                                          "Package ID": 110, "ConanCenter": 90, "Nexus recipe": 90,
                                          "Nexus binary": 90, "Action": 220})
        self.plan_tree.frame.pack(fill="both", expand=True, pady=4)

    def _add_plan_rows(self, root_ref: str, rows: list[PlanRow]) -> None:
        tv = self.plan_tree
        parent = tv.insert("", "end", text=f"{root_ref}  [{rows[0].profile if rows else '?'}]",
                           values=("", "", "", "", "", "", "",
                                   "complete" if rows and all(r.done for r in rows) else "needs work"),
                           open=True)
        for r in rows:
            tag = ("good" if r.done else "bad" if r.binary in ("Missing", "Invalid")
                   else "warn" if r.binary == "Build" else ())
            tv.insert(parent, "end", text=r.ref, tags=(tag,) if tag else (), values=(
                r.profile, r.context, r.package_id[:10], r.binary, r.nexus_recipe,
                "yes" if r.nexus_binary else "no", r.action))

    # ---- Internal packages tab ------------------------------------------
    def _build_internal_tab(self) -> None:
        tab = ttk.Frame(self.nb, padding=6)
        self.nb.add(tab, text="Internal packages")
        ttk.Label(tab, foreground="gray", justify="left", wraplength=1100, text=(
            "For libraries that are not on ConanCenter. Download the source archive yourself, then "
            "'Create draft': the archive is uploaded to the Nexus raw repo (upstream/<name>/<version>/) "
            "and a draft recipe is written to ./recipes. Edit the recipe, 'Test build' until it passes, "
            "then 'Add to mirror list' and mirror it like any other package. "
            "See docs/INTERNAL_RECIPES.md.")).pack(anchor="w")
        body = ttk.PanedWindow(tab, orient="horizontal")
        body.pack(fill="both", expand=True, pady=6)

        self.int_tree = self._tree(body, {"Package / version": 220, "Recipe folder": 200})
        body.add(self.int_tree.frame, weight=1)
        self.int_tree.bind("<<TreeviewSelect>>", self.on_internal_select)

        form = ttk.LabelFrame(body, text="New package or new version", padding=8)
        body.add(form, weight=2)
        self.ivars = {k: tk.StringVar(value=d) for k, d in {
            "name": "", "version": "", "archive": "", "start": "cmake template", "base": "",
            "origin": ""}.items()}
        rows = [("name", "Package name", "lower case, e.g. cigi-ccl"),
                ("version", "Version", "e.g. 3.3.3a"),
                ("archive", "Source archive", "the zip / tar.gz you downloaded"),
                ("start", "Start the recipe from", ""),
                ("base", "ConanCenter reference", "only for 'ConanCenter recipe', e.g. cigi-ccl/4.0.6a"),
                ("origin", "Where the archive came from", "recorded as a comment in conandata.yml")]
        for i, (k, label, hint) in enumerate(rows):
            ttk.Label(form, text=label).grid(row=i, column=0, sticky="w", pady=3)
            if k == "start":
                w: ttk.Entry = ttk.Combobox(form, textvariable=self.ivars[k], state="readonly", width=47, values=[
                    "cmake template", "header-only template", "ConanCenter recipe"])
            else:
                w = ttk.Entry(form, textvariable=self.ivars[k], width=50)
            w.grid(row=i, column=1, sticky="ew", pady=3, padx=6)
            if k == "archive":
                ttk.Button(form, text="Browse...", command=self.on_internal_browse).grid(row=i, column=2, sticky="w")
            else:
                ttk.Label(form, text=hint, foreground="gray").grid(row=i, column=2, sticky="w")
        form.columnconfigure(1, weight=1)
        self.int_upload = tk.BooleanVar(value=True)
        ttk.Checkbutton(form, text="Upload the archive to the Nexus raw repo", variable=self.int_upload).grid(
            row=len(rows), column=1, sticky="w", pady=(4, 0))
        ttk.Label(form, foreground="gray", justify="left", wraplength=560, text=(
            "An existing recipe is never overwritten: for a package that is already listed, this only "
            "adds the new version to it. 'Test build' and 'Add to mirror list' use the name and "
            "version above and the profiles selected on the Search & Mirror tab.")).grid(
            row=len(rows) + 1, column=0, columnspan=3, sticky="w", pady=8)
        bf = ttk.Frame(form)
        bf.grid(row=len(rows) + 2, column=0, columnspan=3, sticky="w")
        for txt, cmd in (("Create draft / add version", self.on_internal_create),
                         ("Open recipe folder", self.on_internal_open),
                         ("Test build", self.on_internal_test),
                         ("Add to mirror list", self.on_internal_add_ref)):
            ttk.Button(bf, text=txt, command=cmd).pack(side="left", padx=(0, 6))
        self._fill_internal()

    def _fill_internal(self) -> None:
        tv = self.int_tree
        tv.delete(*tv.get_children())
        for name, versions in ir.list_packages().items():
            parent = tv.insert("", "end", text=name, values=("",), open=True)
            for v, folder in versions.items():
                tv.insert(parent, "end", iid=f"{name}/{v}", text=v, values=(f"recipes/{name}/{folder}",))

    def on_internal_select(self, _e: object = None) -> None:
        sel = self.int_tree.selection()
        if sel and "/" in sel[0]:
            name, version = sel[0].split("/", 1)
            self.ivars["name"].set(name)
            self.ivars["version"].set(version)

    def on_internal_browse(self) -> None:
        f = filedialog.askopenfilename(title="Source archive", filetypes=[
            # single extensions only: file dialogs don't match double ones like *.tar.gz
            ("Archives", "*.zip *.gz *.tgz *.bz2 *.xz *.tar"), ("All files", "*")])
        if f:
            self.ivars["archive"].set(f)

    def _internal_ref(self) -> tuple[str, str] | None:
        name, version = self.ivars["name"].get().strip(), self.ivars["version"].get().strip()
        if not name or not version:
            messagebox.showwarning(APP_NAME, "Fill in the package name and version (or pick one in the list).")
            return None
        return name, version

    def on_internal_create(self) -> None:
        ref = self._internal_ref()
        if not ref:
            return
        v = {k: x.get().strip() for k, x in self.ivars.items()}
        base = v["base"] if v["start"] == "ConanCenter recipe" else ""
        if v["start"] == "ConanCenter recipe" and not base:
            messagebox.showwarning(APP_NAME, "Enter the ConanCenter reference to start from.")
            return
        if not v["archive"]:
            messagebox.showwarning(APP_NAME, "Choose the source archive you downloaded.")
            return
        upload = self.int_upload.get()
        if upload and not self.creds[0] and not self.ask_creds():
            return
        s = self.settings

        def task() -> None:
            d = ir.create_draft(
                ref[0], ref[1], Path(v["archive"]), s["raw_url"], base=base,
                template="header-only" if v["start"].startswith("header") else "cmake",
                origin=v["origin"], upload=upload, user=self.creds[0], password=self.creds[1],
                cacert=s["cacert_path"], center_remote=s["center_remote"],
                run_conan=lambda a: self.conan.run(a, quiet=True), log=self.log)
            ir.ensure_remote(lambda a: self.conan.run(a, quiet=True), s["internal_remote"], log=self.log)
            self.log(f"Now edit {d / 'conanfile.py'} and press 'Test build'.", "ok")
            self.ui(self._fill_internal)

        self.run_task(f"Creating draft {ref[0]}/{ref[1]}", self._internal_task, task)

    @staticmethod
    def _internal_task(task: Callable[[], None]) -> None:
        try:
            task()
        except ir.RecipeError as e:
            raise ConanError(str(e))

    def _internal_dir(self) -> Path | None:
        ref = self._internal_ref()
        if not ref:
            return None
        folder = ir.list_packages().get(ref[0], {}).get(ref[1])
        if not folder:
            messagebox.showwarning(APP_NAME, f"{ref[0]}/{ref[1]} has no recipe yet - create the draft first.")
            return None
        return ir.INDEX_ROOT / "recipes" / ref[0] / folder

    def on_internal_open(self) -> None:
        d = self._internal_dir()
        if d:
            open_folder(d)

    def on_internal_test(self) -> None:
        ref = self._internal_ref() if self._internal_dir() else None
        if ref is None:
            return
        name, version = ref
        profs = self.selected_profiles()
        if not profs:
            messagebox.showwarning(APP_NAME, "Select at least one profile on the Search & Mirror tab.")
            return
        if self.settings["raw_url"] and not self.creds[0] and not self.ask_creds():
            return
        s = self.settings

        def task() -> None:
            ir.ensure_remote(lambda a: self.conan.run(a, quiet=True), s["internal_remote"], log=self.log)
            for p in profs:
                self.log(f"== Test build {name}/{version} [{p}]", "cmd")
                self.conan.run(ir.test_build_args(name, version, p, s["internal_remote"],
                                                  s["center_remote"], s["build_profile"]))
                self.log(f"== {name}/{version} [{p}]: built", "ok")

        self.run_task(f"Test build {name}/{version}", self._internal_task, task)

    def on_internal_add_ref(self) -> None:
        name_version = self._internal_ref() if self._internal_dir() else None
        if name_version is None:
            return
        ref = "/".join(name_version)
        current = self.selected_refs()
        if ref not in current:
            self.refs_text.delete("1.0", "end")
            self.refs_text.insert("1.0", "\n".join(current + [ref]))
        self._show_tab(0)

    # ---- Profiles tab -----------------------------------------------------
    def _build_profiles_tab(self) -> None:
        tab = ttk.Frame(self.nb, padding=6)
        self.nb.add(tab, text="Profiles")
        left = ttk.Frame(tab)
        left.pack(side="left", fill="y")
        self.prof_box = tk.Listbox(left, width=34, exportselection=False)
        self.prof_box.pack(fill="y", expand=True)
        self.prof_box.bind("<<ListboxSelect>>", self.on_profile_select)
        for txt, cmd in (("Refresh", lambda: self.run_task("Loading profiles", self._task_load_profiles)),
                         ("New from template...", self.on_profile_wizard),
                         ("Detect from this machine...", self.on_profile_detect),
                         ("Import from my normal Conan home", self.on_profile_import),
                         ("Duplicate...", self.on_profile_duplicate),
                         ("Delete", self.on_profile_delete),
                         ("Open profiles folder", self.on_profile_folder)):
            ttk.Button(left, text=txt, command=cmd).pack(fill="x", pady=1)

        right = ttk.Frame(tab)
        right.pack(side="left", fill="both", expand=True, padx=(8, 0))
        self.prof_title = tk.StringVar(value="(no profile selected)")
        ttk.Label(right, textvariable=self.prof_title).pack(anchor="w")
        mono = ("Consolas", 10) if sys.platform == "win32" else ("Menlo", 12)
        self.prof_edit = tk.Text(right, font=mono, undo=True)
        self.prof_edit.pack(fill="both", expand=True)
        bb = ttk.Frame(right)
        bb.pack(fill="x", pady=4)
        ttk.Button(bb, text="Save", command=self.on_profile_save).pack(side="right")
        ttk.Button(bb, text="Show effective profile", command=self.on_profile_show).pack(side="right", padx=4)

    def profiles_dir(self) -> Path:
        return self.conan.home() / "profiles"

    def _task_load_profiles(self) -> None:
        self.conan.home()
        profs: list[str] = self.conan.json(["profile", "list"], quiet=True)
        remotes: list[dict[str, Any]] = self.conan.json(["remote", "list"], quiet=True)
        self.log(f"Conan home: {self.conan.home()}  ({len(profs)} profiles)")
        s = self.settings
        if s["nexus_url"] and s["nexus_remote"] not in {r["name"] for r in remotes}:
            self.log(f"This Conan home has no '{s['nexus_remote']}' remote yet - press 'Apply to Conan' "
                     "on the Settings tab.", "err")
        if not profs:
            self.log("No profiles here yet. On the Profiles tab: 'New from template', or 'Import from "
                     "my normal Conan home' to copy the ones you already have.")
        self.ui(self._fill_profiles, sorted(profs, key=natural_key))

    def _fill_profiles(self, profs: list[str]) -> None:
        prev = set(self.selected_profiles())
        self.profiles = profs
        for box in (self.prof_box, self.prof_list):
            box.delete(0, "end")
            for p in profs:
                box.insert("end", p)
        for i, p in enumerate(profs):
            if p in prev:
                self.prof_list.selection_set(i)

    def on_profile_select(self, _e: object = None) -> None:
        box = self.prof_box
        sel = [i for i in range(box.size()) if box.selection_includes(i)]
        if not sel:
            return
        name = str(box.get(sel[0]))
        path = self.profiles_dir() / name
        self.current_profile = name
        self.prof_title.set(f"{name}   ({path})")
        self.prof_edit.delete("1.0", "end")
        try:
            self.prof_edit.insert("1.0", path.read_text(encoding="utf-8"))
        except OSError as e:
            self.prof_edit.insert("1.0", f"# could not read: {e}")
        self.prof_edit.edit_reset()

    def on_profile_save(self) -> None:
        if not self.current_profile:
            return
        path = self.profiles_dir() / self.current_profile
        path.write_text(self.prof_edit.get("1.0", "end-1c").rstrip() + "\n", encoding="utf-8")
        self.log(f"Saved profile {path}", "ok")

    def on_profile_show(self) -> None:
        name = self.current_profile
        if name:
            self.run_task("Show profile", lambda: self.conan.show(
                ["profile", "show", "-pr:h", name, "-pr:b", name]))

    def _write_profiles(self, profiles: dict[str, str]) -> None:
        d = self.profiles_dir()
        existing = [n for n in profiles if (d / n).exists()]
        if existing and not messagebox.askyesno(
                APP_NAME, "Overwrite existing profiles?\n\n" + "\n".join(existing)):
            profiles = {k: v for k, v in profiles.items() if k not in existing}
        for name, text in profiles.items():
            path = d / name
            try:
                # a new Conan home has no profiles/ folder yet; imported names may have sub-folders
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            except OSError as e:
                messagebox.showerror(APP_NAME, f"Could not write profile {name}:\n{e}")
                break
            self.log(f"Created profile {name}", "ok")
        self.run_task("Loading profiles", self._task_load_profiles)

    def on_profile_import(self) -> None:
        """Copy profiles from the Conan home you use outside this tool."""
        normal = Path(os.environ.get("CONAN_HOME") or Path.home() / ".conan2")
        src = normal / "profiles"
        if normal.resolve() == self.conan.home().resolve():
            messagebox.showinfo(APP_NAME, "The mirror is using your normal Conan home - nothing to import.")
            return
        found = {p.relative_to(src).as_posix(): p for p in sorted(src.rglob("*")) if p.is_file()} \
            if src.is_dir() else {}
        found.pop("default", None)      # machine-detected; make one here with 'Detect' if needed
        if not found:
            messagebox.showinfo(APP_NAME, f"No profiles found in {src}.")
            return
        if messagebox.askyesno(APP_NAME, f"Copy {len(found)} profile(s) from {src}?\n\n" + "\n".join(found)):
            self._write_profiles({n: p.read_text(encoding="utf-8") for n, p in found.items()})

    def on_profile_wizard(self) -> None:
        ProfileWizard(self, self._write_profiles)

    def on_profile_detect(self) -> None:
        name = simpledialog.askstring(APP_NAME, "Name for the detected profile:", parent=self)
        if not name:
            return

        def task() -> None:
            self.conan.run(["profile", "detect", "--name", name, "--force"])
            self._task_load_profiles()

        self.run_task("Detect profile", task)

    def on_profile_duplicate(self) -> None:
        if not self.current_profile:
            return
        name = simpledialog.askstring(APP_NAME, "New profile name:", parent=self,
                                      initialvalue=self.current_profile + "-copy")
        if name:
            self._write_profiles({name: self.prof_edit.get("1.0", "end-1c").rstrip() + "\n"})

    def on_profile_delete(self) -> None:
        if not self.current_profile:
            return
        if messagebox.askyesno(APP_NAME, f"Delete profile '{self.current_profile}'?"):
            (self.profiles_dir() / self.current_profile).unlink(missing_ok=True)
            self.log(f"Deleted profile {self.current_profile}")
            self.current_profile = None
            self.prof_edit.delete("1.0", "end")
            self.run_task("Loading profiles", self._task_load_profiles)

    def on_profile_folder(self) -> None:
        open_folder(self.profiles_dir())

    # ---- Settings tab -----------------------------------------------------
    def _build_settings_tab(self) -> None:
        tab = ttk.Frame(self.nb, padding=10)
        self.nb.add(tab, text="Settings")
        self.svars: dict[TextSetting, tk.StringVar] = {}
        fields: list[tuple[TextSetting, str, str]] = [
            ("conan_exe", "Conan executable", "conan, or full path to conan.exe"),
            ("center_remote", "ConanCenter remote name", ""),
            ("center_url", "ConanCenter URL", ""),
            ("proxy", "Proxy (for ConanCenter)", "e.g. 10.1.2.3:8080 - Nexus bypasses it"),
            ("nexus_remote", "Nexus remote name", ""),
            ("nexus_url", "Nexus Conan repo URL", "e.g. https://nexus.corp/repository/conan-hosted/"),
            ("raw_url", "Nexus raw (sources) URL", "e.g. https://nexus.corp/repository/conan-sources/"),
            ("cacert_path", "CA bundle (optional)", "only if the proxy intercepts TLS"),
            ("nexus_user", "Nexus username", "password is asked per session, never saved"),
            ("internal_remote", "Internal recipes remote name", "points at ./recipes next to this script"),
            ("conan_home", "Conan home for mirroring", "kept apart from your everyday Conan; "
                                                       "empty = use your normal one"),
            ("build_profile", "Build profile (for tools)", "a Release profile, e.g. win-msvc194-cpp20-release; "
                                                           "empty = same as each host profile"),
        ]
        for i, (k, label, hint) in enumerate(fields):
            ttk.Label(tab, text=label).grid(row=i, column=0, sticky="w", pady=3)
            v = tk.StringVar(value=self.settings.get(k, ""))
            self.svars[k] = v
            ttk.Entry(tab, textvariable=v, width=60).grid(row=i, column=1, sticky="ew", pady=3, padx=6)
            ttk.Label(tab, text=hint, foreground="gray").grid(row=i, column=2, sticky="w")
        tab.columnconfigure(1, weight=1)

        r = len(fields)
        self.dark_var = tk.BooleanVar(value=bool(self.settings.get("dark_mode")))
        ttk.Checkbutton(tab, text="Dark mode", variable=self.dark_var, command=self.on_dark_toggle).grid(
            row=r, column=1, sticky="w", padx=6, pady=3)
        ttk.Label(tab, text="switches and is saved straight away", foreground="gray").grid(
            row=r, column=2, sticky="w")

        r += 1
        bf = ttk.Frame(tab)
        bf.grid(row=r, column=0, columnspan=3, sticky="w", pady=(12, 4))
        for txt, cmd in (("Save", self.on_settings_save),
                         ("Apply to Conan (remotes + global.conf)", self.on_apply_conan),
                         ("Log in to Nexus...", self.on_login),
                         ("Test network", self.on_test_network)):
            ttk.Button(bf, text=txt, command=cmd).pack(side="left", padx=(0, 6))
        bf2 = ttk.Frame(tab)
        bf2.grid(row=r + 1, column=0, columnspan=3, sticky="w", pady=4)
        for txt, cmd in (("Upload all cached source backups", self.on_backup_upload),
                         ("Export team config...", self.on_export_team),
                         ("Open Conan home", lambda: open_folder(self.conan.home()))):
            ttk.Button(bf2, text=txt, command=cmd).pack(side="left", padx=(0, 6))

        ttk.Label(tab, foreground="gray", justify="left", wraplength=900, text=(
            "'Apply to Conan' adds/updates both remotes, and writes a clearly marked block to Conan's "
            "global.conf (backup-sources upload/download URLs, optional CA bundle) plus a "
            "source_credentials.json that reads your session credentials from environment variables. "
            "A .bak copy of each file is kept the first time.\n\n"
            "'Export team config' creates a zip in Conan's own config format (Nexus remote, source "
            "location, MSVC fallbacks, your profiles) and can upload it to the Nexus raw repo. "
            "Developers install or update it with one command:  conan config install <zip or URL>")
        ).grid(row=r + 2, column=0, columnspan=3, sticky="w", pady=10)

    def on_settings_save(self) -> bool:
        new: dict[TextSetting, str] = {k: v.get().strip() for k, v in self.svars.items()}
        for k in ("nexus_url", "raw_url"):
            new[k] = slash(new[k])
            self.svars[k].set(new[k])
        if len({new["nexus_remote"], new["center_remote"], new["internal_remote"]}) < 3:
            messagebox.showerror(APP_NAME, "Nexus, ConanCenter and internal remote names must differ.")
            return False
        home_changed = new["conan_home"] != self.settings.get("conan_home")
        for k, value in new.items():
            self.settings[k] = value
        self.settings["dark_mode"] = self.dark_var.get()
        save_settings(self.settings)
        self.log(f"Settings saved to {SETTINGS_FILE}", "ok")
        if home_changed:
            self.conan._home = None
            self.log("Conan home changed - press 'Apply to Conan', then check the Profiles tab.", "err")
            self.after(200, lambda: self.run_task("Loading profiles", self._task_load_profiles))
        return True

    def on_dark_toggle(self) -> None:
        """Switch now, and remember it (only this setting: the other fields still need Save)."""
        dark = self.dark_var.get()
        self.theme.apply(dark)
        self.settings["dark_mode"] = dark
        save_settings(self.settings)

    def on_apply_conan(self) -> None:
        if self.on_settings_save():
            self.run_task("Applying Conan config", self._task_apply)

    def _task_apply(self) -> None:
        apply_config(self.conan, self.settings, self.log)

    def ask_creds(self) -> bool:
        user = simpledialog.askstring(APP_NAME, "Nexus username:", parent=self,
                                      initialvalue=self.settings.get("nexus_user", ""))
        if not user:
            return False
        pw = simpledialog.askstring(APP_NAME, f"Nexus password / token for {user}:", parent=self, show="*")
        if pw is None:
            return False
        self.creds = (user, pw)
        if user != self.settings.get("nexus_user"):
            self.settings["nexus_user"] = user
            self.svars["nexus_user"].set(user)
            save_settings(self.settings)
        return True

    def on_login(self) -> None:
        if self.ask_creds():
            remote = self.settings["nexus_remote"]
            # password goes via CONAN_PASSWORD_<REMOTE> env var, never on the command line
            self.run_task("Logging in", lambda: self.conan.run(["remote", "login", remote, self.creds[0]]))

    def on_test_network(self) -> None:
        if not self.on_settings_save():
            return
        script = HERE / "check_network.py"
        if not script.exists():
            messagebox.showerror(APP_NAME, f"{script} not found (keep it next to this script).")
            return

        def task() -> None:
            p = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                               creationflags=0x08000000 if os.name == "nt" else 0)
            for line in (p.stdout + p.stderr).splitlines():
                tag = "ok" if "[ OK ]" in line else "err" if "[FAIL]" in line else ""
                self.log(line, tag)

        self.run_task("Network test", task)

    def on_backup_upload(self) -> None:
        if not self.settings["raw_url"]:
            messagebox.showwarning(APP_NAME, "Set the raw (sources) URL first and Apply to Conan.")
            return
        if not self.creds[0] and not self.ask_creds():
            return
        self.run_task("Uploading source backups", lambda: self.conan.run(["cache", "backup-upload"]))

    def on_export_team(self) -> None:
        s = self.settings
        if not s["nexus_url"]:
            messagebox.showwarning(APP_NAME, "Set the Nexus URL first.")
            return
        dest = filedialog.asksaveasfilename(title="Save team config", defaultextension=".zip",
                                            initialfile="team-config.zip",
                                            filetypes=[("Zip", "*.zip")])
        if not dest:
            return
        # never ship 'default': it is machine-detected and would clobber each developer's own
        profs = [p for p in (self.selected_profiles() or self.profiles) if p != "default"]
        bp = s["build_profile"]
        if bp == "default":
            messagebox.showwarning(APP_NAME, "The build profile is 'default', which is never exported "
                                             "(it is different on every machine). Choose a named "
                                             "Release profile under Settings first.")
            return
        if bp and bp not in profs:
            if bp not in self.profiles:
                messagebox.showwarning(APP_NAME, f"Build profile '{bp}' does not exist - fix it under Settings.")
                return
            profs.append(bp)
        if not messagebox.askyesno(APP_NAME, f"Include {len(profs)} profile(s)?\n\n" + "\n".join(profs)
                                   + "\n\n(Select profiles on the Search tab to limit this.)"):
            return
        url = ""
        if s["raw_url"] and messagebox.askyesno(
                APP_NAME, "Also upload it to the Nexus raw repo, so developers can install it by URL?\n\n"
                          f"{s['raw_url']}{tc.CONFIG_PATH_IN_RAW}\n\n"
                          "(An existing team config there is replaced. Developers can only fetch it "
                          "if the raw repo allows anonymous read.)"):
            if not self.creds[0] and not self.ask_creds():
                return
            url = s["raw_url"] + tc.CONFIG_PATH_IN_RAW

        def task() -> None:
            try:
                version = self.conan.run(["--version"], quiet=True).strip().split()[-1]
                folder = tc.build(WORK_DIR / "team_config", s, profs, self.conan.home(), version, url, self.log)
                tc.make_zip(folder, Path(dest))
                self.log(f"Team config written to {dest}", "ok")
                if url:
                    ir.NexusRaw(self.creds[0], self.creds[1], s["cacert_path"]).put(url, Path(dest))
                    self.log(f"Uploaded to {url}", "ok")
            except (tc.TeamConfigError, ir.RecipeError, OSError) as e:
                raise ConanError(str(e))
            self.log(f"Developers run:  conan config install {url or Path(dest).name}", "ok")
            self.log(f"          then:  conan remote login {s['nexus_remote']} <username>")

        self.run_task("Exporting team config", task)

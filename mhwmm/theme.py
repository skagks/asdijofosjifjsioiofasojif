"""Colours, fonts, images and ttk styles for the GUI.

Everything is drawn with the cross-platform "clam" theme so the dark and light
looks are the same on every Windows version.  Plain Tk widgets (Text, Menu,
Canvas, Toplevel) are not styled by ttk; they are tagged with a role through
``Theme.mark`` and repainted whenever the theme changes.
"""

from __future__ import annotations

import sys
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

from .assets import IMAGES

PALETTES = {
    "dark": {
        "bg": "#121418", "surface": "#1a1d23", "raised": "#252932", "hover": "#2e333e",
        "pressed": "#363c48", "field": "#20242c", "border": "#2c313b",
        "text": "#ece7dd", "muted": "#9298a3", "subtle": "#5d636e",
        "accent": "#e6a74b", "accent_hover": "#f2b964", "accent_press": "#cc8f36", "on_accent": "#1d1408",
        "select": "#3b3122", "select_text": "#fff4e0", "row_alt": "#1d2027",
        "scroll": "#353a45", "scroll_hover": "#4a505d",
        "success": "#63c98d", "success_bg": "#1c2c24", "warn": "#eaa646", "warn_bg": "#30271a",
        "danger": "#f0736a", "danger_bg": "#33201f", "chip": "#2a2720", "chip_text": "#e6c48f",
        "info": "#62b6c4", "info_bg": "#1a2a2e",
        "problem_bg": "#3d2222", "over": "#b6a1ee", "over_bg": "#2b2540",
    },
    "light": {
        "bg": "#efece6", "surface": "#fbfaf8", "raised": "#ffffff", "hover": "#f3eee5",
        "pressed": "#e8e1d4", "field": "#ffffff", "border": "#ddd7cc",
        "text": "#24211c", "muted": "#6f695f", "subtle": "#a8a296",
        "accent": "#e09a34", "accent_hover": "#eaa846", "accent_press": "#c9851f", "on_accent": "#2a1a04",
        "select": "#f5e3c3", "select_text": "#24211c", "row_alt": "#f6f3ee",
        "scroll": "#d6cfc2", "scroll_hover": "#bfb7a8",
        "success": "#2e8f57", "success_bg": "#e3f2e8", "warn": "#a8650f", "warn_bg": "#f8ecd6",
        "danger": "#c93c33", "danger_bg": "#f8e1df", "chip": "#f3e7d2", "chip_text": "#8a5a12",
        "info": "#237785", "info_bg": "#dff0f2",
        "problem_bg": "#fbdcd8", "over": "#6a4bb0", "over_bg": "#ebe4f7",
    },
}
THEME_NAMES = {"dark": "다크", "light": "라이트"}

FONT_CANDIDATES = (
    "Malgun Gothic", "맑은 고딕", "Apple SD Gothic Neo", "Pretendard", "NanumGothic", "나눔고딕",
    "Noto Sans CJK KR", "Noto Sans KR",
)


def set_dark_titlebar(window: tk.Misc, dark: bool) -> None:
    """Ask Windows 10/11 to draw the title bar dark (ignored elsewhere)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())  # type: ignore[attr-defined]
        value = ctypes.c_int(1 if dark else 0)
        for attribute in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (new, pre-20H1)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(  # type: ignore[attr-defined]
                    hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                break
    except Exception:
        pass


class Theme:
    def __init__(self, root: tk.Misc, name: str = "dark"):
        self.root = root
        self.name = name if name in PALETTES else "dark"
        self.p = PALETTES[self.name]
        try:
            self.scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
        except tk.TclError:
            self.scale = 1.0
        self.fonts: dict[str, tkfont.Font] = {}
        self._images: dict[tuple[str, int], tk.PhotoImage] = {}
        self._setup_fonts()

    def px(self, n: float) -> int:
        return int(round(n * self.scale))

    # ------------------------------------------------------------------ fonts
    def _setup_fonts(self) -> None:
        families = set(tkfont.families(self.root))
        family = next((f for f in FONT_CANDIDATES if f in families), None)
        base = tkfont.nametofont("TkDefaultFont")
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont",
                     "TkSmallCaptionFont", "TkTooltipFont"):
            try:
                f = tkfont.nametofont(name)
            except tk.TclError:
                continue
            f.configure(size=10, weight="normal")
            if family:
                f.configure(family=family)
        family = base.cget("family")

        def make(key, size, weight="normal"):
            self.fonts[key] = tkfont.Font(root=self.root, family=family, size=size, weight=weight)

        make("base", 10)
        make("small", 9)
        make("small_bold", 9, "bold")
        make("bold", 10, "bold")
        make("section", 11, "bold")
        make("title", 14, "bold")
        make("eyebrow", 8, "bold")
        make("icon", 12)
        make("display", 30, "bold")
        make("detail_title", 16, "bold")

    # ------------------------------------------------------------------ images
    def image(self, name: str, size: int, exact: bool = False) -> tk.PhotoImage:
        """Embedded image closest to ``size`` logical pixels at the current DPI
        (or to ``size`` device pixels when ``exact``)."""
        variants = IMAGES[name]
        want = size if exact else size * self.scale
        best = min(variants, key=lambda n: (abs(n - want), -n))
        key = (name, best)
        if key not in self._images:
            self._images[key] = tk.PhotoImage(master=self.root, data=variants[best])
        return self._images[key]

    def checkbutton(self, parent, text: str, variable: tk.Variable, card: bool = False, **kwargs) -> ttk.Checkbutton:
        """Checkbutton drawn with the same checkbox images as the mod list."""
        return ttk.Checkbutton(parent, text="  " + text, variable=variable,
                               style="CardImage.TCheckbutton" if card else "Image.TCheckbutton",
                               compound="left", image=(self.image("check_off", 18), "selected",
                                                       self.image("check_on", 18)), **kwargs)

    # ------------------------------------------------------------------ plain Tk widgets
    def mark(self, widget, role: str):
        widget.theme_role = role
        self.paint(widget, role)
        return widget

    def paint(self, w, role: str) -> None:
        p = self.p
        try:
            if role == "window":
                w.configure(bg=p["bg"])
            elif role == "card":
                w.configure(bg=p["surface"], highlightbackground=p["border"], highlightcolor=p["border"],
                            highlightthickness=1, borderwidth=0)
            elif role in ("text", "log"):
                w.configure(bg=p["surface"], fg=p["text"] if role == "text" else p["muted"],
                            insertbackground=p["text"], selectbackground=p["select"],
                            selectforeground=p["select_text"], highlightthickness=0, borderwidth=0,
                            relief="flat", inactiveselectbackground=p["select"])
            elif role in ("canvas", "canvas_bg"):
                w.configure(bg=p["surface"] if role == "canvas" else p["bg"], highlightthickness=0, borderwidth=0)
            elif role == "menu":
                w.configure(bg=p["surface"], fg=p["text"], activebackground=p["select"],
                            activeforeground=p["select_text"], disabledforeground=p["subtle"],
                            selectcolor=p["accent"], borderwidth=1, activeborderwidth=0, relief="solid")
            elif role == "tooltip":
                w.configure(bg=p["raised"], fg=p["text"], highlightbackground=p["border"],
                            highlightthickness=1, borderwidth=0)
        except tk.TclError:
            pass

    def repaint_all(self, widget) -> None:
        role = getattr(widget, "theme_role", None)
        if role:
            self.paint(widget, role)
        for child in widget.winfo_children():
            self.repaint_all(child)

    # ------------------------------------------------------------------ ttk styles
    def use(self, name: str) -> None:
        self.name = name if name in PALETTES else "dark"
        self.p = PALETTES[self.name]
        self._configure_styles()
        self._configure_option_db()
        self.repaint_all(self.root)

    def _configure_option_db(self) -> None:
        p, r = self.p, self.root
        r.option_add("*TCombobox*Listbox.background", p["surface"])
        r.option_add("*TCombobox*Listbox.foreground", p["text"])
        r.option_add("*TCombobox*Listbox.selectBackground", p["select"])
        r.option_add("*TCombobox*Listbox.selectForeground", p["select_text"])
        r.option_add("*TCombobox*Listbox.font", self.fonts["base"])
        r.option_add("*TCombobox*Listbox.borderWidth", 0)

    def _configure_styles(self) -> None:
        p, f, px = self.p, self.fonts, self.px
        s = ttk.Style(self.root)
        s.theme_use("clam")

        s.configure(".", background=p["bg"], foreground=p["text"], fieldbackground=p["field"],
                    bordercolor=p["border"], lightcolor=p["bg"], darkcolor=p["bg"], troughcolor=p["field"],
                    focuscolor=p["accent"], selectbackground=p["select"], selectforeground=p["select_text"],
                    insertcolor=p["text"], font=f["base"], relief="flat")
        s.map(".", foreground=[("disabled", p["subtle"])])

        # surfaces and text
        for prefix, bg in (("", p["bg"]), ("Card.", p["surface"]), ("Header.", p["surface"]),
                           ("Status.", p["surface"])):
            s.configure(f"{prefix}TFrame", background=bg)
            s.configure(f"{prefix}TLabel", background=bg, foreground=p["text"])
            s.configure(f"{prefix.rstrip('.')}Muted.TLabel", background=bg, foreground=p["muted"])
        s.configure("Title.TLabel", background=p["surface"], font=f["title"])
        s.configure("Eyebrow.TLabel", background=p["surface"], foreground=p["accent"], font=f["eyebrow"])
        s.configure("Section.TLabel", background=p["surface"], font=f["section"])
        s.configure("Hint.TLabel", background=p["surface"], foreground=p["subtle"], font=f["small"])
        s.configure("Display.TLabel", background=p["surface"], foreground=p["accent"], font=f["display"])
        s.configure("EmptyTitle.TLabel", background=p["surface"], font=f["section"])
        s.configure("DialogTitle.TLabel", background=p["bg"], font=f["section"])
        s.configure("DialogSection.TLabel", background=p["bg"], foreground=p["accent"], font=f["small_bold"])
        s.configure("CardTitle.TLabel", background=p["surface"], font=f["detail_title"])
        s.configure("Callout.TFrame", background=p["warn_bg"])
        s.configure("CalloutTitle.TLabel", background=p["warn_bg"], foreground=p["warn"], font=f["bold"])
        s.configure("Callout.TLabel", background=p["warn_bg"], foreground=p["text"])
        # the 원인 모드 찾기 bar under the toolbar (Banner) and how a game ended in its question (Banner, Alert)
        for prefix, fg, bg in (("Banner", "info", "info_bg"), ("Alert", "danger", "danger_bg")):
            s.configure(f"{prefix}.TFrame", background=p[bg])
            s.configure(f"{prefix}Title.TLabel", background=p[bg], foreground=p[fg], font=f["bold"])
            s.configure(f"{prefix}.TLabel", background=p[bg], foreground=p["text"])
            s.configure(f"{prefix}Muted.TLabel", background=p[bg], foreground=p["muted"], font=f["small"])
        s.configure("FieldHint.TLabel", background=p["field"], foreground=p["subtle"], font=f["small"])
        s.configure("Thumb.TFrame", background=p["bg"])
        s.configure("Thumb.TLabel", background=p["bg"], foreground=p["subtle"], font=f["small"])
        # equipment window: tiles (one per piece of equipment), the selected one highlighted
        for prefix, bg in (("Tile", p["raised"]), ("TileSel", p["select"])):
            s.configure(f"{prefix}.TFrame", background=bg)
            s.configure(f"{prefix}.TLabel", background=bg, foreground=p["text"])
            s.configure(f"{prefix}Muted.TLabel", background=bg, foreground=p["muted"], font=f["small"])
            s.configure(f"{prefix}Title.TLabel", background=bg, foreground=p["text"], font=f["bold"])
        for name, fg, bg in (("PartOn", "chip_text", "chip"), ("PartOff", "subtle", "bg"),
                             ("PartConflict", "danger", "danger_bg")):
            s.configure(f"{name}.TLabel", background=p[bg], foreground=p[fg], font=f["small"],
                        padding=(px(6), px(1)))
        for kind, fg, bg in (("Ok", "success", "success_bg"), ("Warn", "warn", "warn_bg"),
                             ("Bad", "danger", "danger_bg"), ("Neutral", "chip_text", "chip"),
                             ("Info", "info", "info_bg"), ("Over", "over", "over_bg")):
            s.configure(f"Badge{kind}.TLabel", background=p[bg], foreground=p[fg], font=f["small_bold"],
                        padding=(px(10), px(4)))
            s.configure(f"Legend{kind}.TLabel", background=p[bg], foreground=p[fg], font=f["small"],
                        padding=(px(8), px(1)))

        # buttons
        def button(style, bg, fg, hover, press, border=None, padding=(14, 7), font=None, width=-6):
            border = border or bg
            s.configure(style, background=bg, foreground=fg, bordercolor=border, lightcolor=bg, darkcolor=bg,
                        focuscolor=bg, focusthickness=0, padding=(px(padding[0]), px(padding[1])),
                        relief="flat", anchor="center", font=font or f["base"], width=width)
            s.map(style,
                  background=[("disabled", bg), ("pressed", press), ("active", hover)],
                  lightcolor=[("disabled", bg), ("pressed", press), ("active", hover)],
                  darkcolor=[("disabled", bg), ("pressed", press), ("active", hover)],
                  bordercolor=[("disabled", border), ("pressed", press), ("active", hover if border == bg else border)],
                  foreground=[("disabled", p["subtle"])])

        button("TButton", p["raised"], p["text"], p["hover"], p["pressed"], border=p["border"])
        button("Accent.TButton", p["accent"], p["on_accent"], p["accent_hover"], p["accent_press"],
               font=f["bold"])
        s.map("Accent.TButton", background=[("disabled", p["raised"]), ("pressed", p["accent_press"]),
                                            ("active", p["accent_hover"])],
              lightcolor=[("disabled", p["raised"])], darkcolor=[("disabled", p["raised"])],
              bordercolor=[("disabled", p["border"])], foreground=[("disabled", p["subtle"])])
        button("Compact.TButton", p["raised"], p["text"], p["hover"], p["pressed"], border=p["border"],
               padding=(12, 7), width=-2)
        # small buttons in a card's text (실행 전 점검: what may help with an issue)
        button("Action.TButton", p["raised"], p["text"], p["hover"], p["pressed"], border=p["border"],
               padding=(10, 3), font=f["small"], width=-2)
        button("Ghost.TButton", p["surface"], p["muted"], p["hover"], p["pressed"], padding=(9, 5),
               font=f["icon"], width=-2)
        s.map("Ghost.TButton", foreground=[("active", p["text"]), ("disabled", p["subtle"])])
        button("Link.TButton", p["surface"], p["muted"], p["hover"], p["pressed"], padding=(8, 2),
               font=f["small"], width=-2)
        button("Update.TButton", p["accent"], p["on_accent"], p["accent_hover"], p["accent_press"],
               padding=(12, 5), font=f["small_bold"], width=-2)
        # coloured pills next to the mod count: problems (opens the filter) and conflicts (opens the overview)
        for style, fg, bg, hover in (("Problem.TButton", p["danger"], p["problem_bg"], p["danger_bg"]),
                                     ("Conflict.TButton", p["warn"], p["warn_bg"], p["select"])):
            button(style, bg, fg, hover, hover, padding=(10, 3), font=f["small_bold"], width=-2)
        # the notice bar under the toolbar (a crash record of the last game): its close button
        button("CalloutClose.TButton", p["warn_bg"], p["muted"], p["select"], p["select"], padding=(8, 3), width=-2)
        s.map("CalloutClose.TButton", foreground=[("active", p["text"])])
        button("BannerGhost.TButton", p["info_bg"], p["muted"], p["hover"], p["pressed"], padding=(10, 5),
               font=f["small"], width=-2)
        s.map("BannerGhost.TButton", foreground=[("active", p["text"]), ("disabled", p["subtle"])])
        # table of contents in the usage guide
        button("Nav.TButton", p["bg"], p["muted"], p["hover"], p["pressed"], padding=(14, 6), width=-2)
        s.configure("Nav.TButton", anchor="w")
        s.map("Nav.TButton", foreground=[("active", p["text"]), ("disabled", p["subtle"])])
        button("NavActive.TButton", p["select"], p["select_text"], p["select"], p["select"], padding=(14, 6),
               font=f["bold"], width=-2)
        s.configure("NavActive.TButton", anchor="w")

        for style, bg, border in (("Chip.TMenubutton", p["raised"], p["border"]),
                                  ("Ghost.TMenubutton", p["surface"], p["surface"])):
            s.configure(style, background=bg, foreground=p["text"], bordercolor=border, lightcolor=bg,
                        darkcolor=bg, arrowcolor=p["muted"], padding=(px(12), px(6)), relief="flat",
                        focuscolor=bg, width=-2)
            s.map(style, background=[("pressed", p["pressed"]), ("active", p["hover"])],
                  lightcolor=[("active", p["hover"])], darkcolor=[("active", p["hover"])],
                  arrowcolor=[("active", p["text"])])
        s.configure("Ghost.TMenubutton", foreground=p["muted"], font=f["icon"], padding=(px(10), px(5)))
        s.layout("Ghost.TMenubutton", [("Menubutton.border", {"sticky": "nswe", "children": [
            ("Menubutton.padding", {"sticky": "nswe", "children": [
                ("Menubutton.label", {"sticky": ""})]})]})])

        # checkbuttons, switch, entries
        for style, bg in (("TCheckbutton", p["bg"]), ("Card.TCheckbutton", p["surface"])):
            s.configure(style, background=bg, foreground=p["text"], indicatorbackground=p["field"],
                        indicatorforeground=p["on_accent"], upperbordercolor=p["subtle"],
                        lowerbordercolor=p["subtle"], focuscolor=bg, padding=(px(2), px(3)))
            s.map(style, background=[("active", bg)],
                  indicatorbackground=[("selected", p["accent"]), ("active", p["hover"])],
                  upperbordercolor=[("selected", p["accent"])], lowerbordercolor=[("selected", p["accent"])])
        for style, bg in (("Switch.TCheckbutton", p["bg"]), ("Image.TCheckbutton", p["bg"]),
                          ("CardImage.TCheckbutton", p["surface"])):
            s.configure(style, background=bg, foreground=p["text"], focuscolor=bg, padding=(px(4), px(4)))
            s.map(style, background=[("active", bg)])
            s.layout(style, [("Checkbutton.padding", {"sticky": "nswe", "children": [
                ("Checkbutton.label", {"sticky": "nswe"})]})])

        # radio buttons (원인 모드 찾기: 언제 튕기나요?)
        s.configure("TRadiobutton", background=p["bg"], foreground=p["text"], indicatorbackground=p["field"],
                    indicatorforeground=p["on_accent"], upperbordercolor=p["subtle"], lowerbordercolor=p["subtle"],
                    focuscolor=p["bg"], padding=(px(2), px(3)), indicatormargin=(0, 0, px(8), 0))
        s.map("TRadiobutton", background=[("active", p["bg"])],
              indicatorbackground=[("selected", p["accent"]), ("active", p["hover"])],
              upperbordercolor=[("selected", p["accent"])], lowerbordercolor=[("selected", p["accent"])],
              foreground=[("disabled", p["subtle"])])

        for style in ("TEntry", "TCombobox", "TSpinbox"):
            s.configure(style, fieldbackground=p["field"], foreground=p["text"], background=p["field"],
                        bordercolor=p["border"], lightcolor=p["field"], darkcolor=p["field"],
                        insertcolor=p["text"], arrowcolor=p["muted"], padding=(px(9), px(6)),
                        selectbackground=p["select"], selectforeground=p["select_text"])
            s.map(style, bordercolor=[("focus", p["accent"])], lightcolor=[("focus", p["field"])],
                  fieldbackground=[("readonly", p["field"])], foreground=[("disabled", p["subtle"])],
                  arrowcolor=[("active", p["text"])], background=[("active", p["field"])])

        # lists
        s.configure("Treeview", background=p["surface"], fieldbackground=p["surface"], foreground=p["text"],
                    bordercolor=p["surface"], lightcolor=p["surface"], darkcolor=p["surface"],
                    rowheight=px(34), font=f["base"], indent=0)
        s.map("Treeview", background=[("selected", p["select"])], foreground=[("selected", p["select_text"])])
        s.configure("Treeview.Heading", background=p["surface"], foreground=p["muted"], font=f["small_bold"],
                    bordercolor=p["border"], lightcolor=p["surface"], darkcolor=p["surface"],
                    relief="flat", padding=(px(8), px(8)))
        s.map("Treeview.Heading", background=[("active", p["hover"])], lightcolor=[("active", p["hover"])],
              darkcolor=[("active", p["hover"])], foreground=[("active", p["text"])])
        s.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        s.layout("Treeview.Item", [("Treeitem.padding", {"sticky": "nswe", "children": [
            ("Treeitem.image", {"side": "left", "sticky": ""}),
            ("Treeitem.text", {"side": "left", "sticky": ""})]})])
        s.configure("Treeview.Item", padding=(px(12), 0, 0, 0))

        # notebook
        s.configure("TNotebook", background=p["surface"], bordercolor=p["surface"], lightcolor=p["surface"],
                    darkcolor=p["surface"], tabmargins=(px(12), px(12), px(12), 0), padding=0)
        s.configure("TNotebook.Tab", background=p["surface"], foreground=p["muted"], bordercolor=p["surface"],
                    lightcolor=p["surface"], darkcolor=p["surface"], padding=(px(16), px(7)), font=f["bold"],
                    focuscolor=p["surface"])
        s.map("TNotebook.Tab",
              background=[("selected", p["raised"]), ("active", p["hover"])],
              lightcolor=[("selected", p["raised"]), ("active", p["hover"])],
              bordercolor=[("selected", p["border"]), ("active", p["hover"])],
              foreground=[("selected", p["text"]), ("active", p["text"])],
              expand=[("selected", (0, 0, 0, 0))])

        # scrollbars, progress, panes, separators
        for orient in ("Vertical", "Horizontal"):
            style = f"{orient}.TScrollbar"
            s.configure(style, background=p["scroll"], troughcolor=p["surface"], bordercolor=p["surface"],
                        lightcolor=p["scroll"], darkcolor=p["scroll"], arrowcolor=p["muted"], gripcount=0,
                        arrowsize=px(10), relief="flat")
            s.map(style, background=[("active", p["scroll_hover"])], lightcolor=[("active", p["scroll_hover"])],
                  darkcolor=[("active", p["scroll_hover"])])
            sticky = "ns" if orient == "Vertical" else "we"
            s.layout(style, [(f"{orient}.Scrollbar.trough", {"sticky": sticky, "children": [
                (f"{orient}.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
        s.configure("Accent.Horizontal.TProgressbar", troughcolor=p["field"], background=p["accent"],
                    bordercolor=p["field"], lightcolor=p["accent"], darkcolor=p["accent"], thickness=px(6))
        s.configure("TPanedwindow", background=p["bg"])
        s.configure("Sash", sashthickness=px(12), gripcount=0, background=p["bg"], lightcolor=p["bg"],
                    darkcolor=p["bg"], bordercolor=p["bg"])
        s.configure("TSeparator", background=p["border"])

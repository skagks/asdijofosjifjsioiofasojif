"""The 장비 외형 window: which in-game equipment the installed mods change the look of, with the
game's own names and preview pictures."""

from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .equip import overview as ov
from .equip.model import ARMOR_PARTS, BODIES, PART_LABELS, Target, parse_armor_key, parse_weapon_key
from .gui import Image, ImageTk, Tooltip, autohide, load_image, set_dark_titlebar

log = logging.getLogger(__name__)

# rarity colours when the game's own colour table cannot be read (close to the in-game ones)
RARITY_COLORS = {1: "#c9c9c9", 2: "#ececec", 3: "#a7d46b", 4: "#5eb3dc", 5: "#a283e8", 6: "#e69a4a",
                 7: "#e9655b", 8: "#55cfd6", 9: "#f0cf55", 10: "#d9d8ff", 11: "#f58fc6", 12: "#fff1c9"}
MAX_TILES = 120  # more tiles take long to draw (narrow the list with the groups or the search instead)
ALL = "all"


class EquipmentWindow(tk.Toplevel):
    THUMB = 72  # tile picture (logical px)
    PICTURE = 220  # picture in the detail panel
    PART = 52  # per-part pictures in the detail panel

    def __init__(self, app, mod_id: str | None = None):
        super().__init__(app.root)
        self.app, self.manager, self.theme = app, app.manager, app.theme
        t, px = self.theme, self.theme.px
        self.withdraw()
        self.title("장비 외형")
        t.mark(self, "window")
        self.minsize(px(1080), px(600))  # room for all six part chips next to the details
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda e: self.close())
        self.closed = False
        self.overview: ov.Overview | None = None
        self.catalog = None
        self.ui: dict = {}
        self.group = ALL
        self.mod_filter = mod_id
        self.selected: Target | None = None
        self.tiles: dict[Target, ttk.Frame] = {}
        self.images: dict[tuple[str, int], object] = {}  # PhotoImages by (picture, size): Tk drops unheld ones
        self.waiting: dict[tuple[str, int], list] = {}  # labels waiting for a picture
        self.icons: dict[tuple[int, str], object] = {}  # weapon-type icons by (type, theme)
        self.gen = 0
        self._reload_after = None
        self._seen = None
        self._thumbs: queue.Queue = queue.Queue()
        threading.Thread(target=self._thumb_worker, daemon=True).start()

        self._build_header()
        self._build_body()
        self.status_var = tk.StringVar(value="")
        status = ttk.Frame(self, padding=(px(22), 0, px(22), px(14)))
        status.pack(side="bottom", fill="x")
        ttk.Label(status, textvariable=self.status_var, style="Muted.TLabel", font=t.fonts["small"]).pack(side="left")
        self.more = ttk.Menubutton(status, text="⋯", style="Ghost.TMenubutton")
        self.more.pack(side="right")
        menu = t.mark(tk.Menu(self.more, tearoff=False), "menu")
        menu.add_command(label="게임 데이터 다시 읽기", command=lambda: self.reload(rebuild=True))
        menu.add_command(label="아이콘 확인용 그림 저장...", command=self.save_icon_sheet)
        menu.add_command(label="진단 파일 저장...", command=self.save_report)
        self.more.configure(menu=menu)

        self.geometry(f"{px(1200)}x{px(780)}")
        set_dark_titlebar(self, t.name == "dark")
        self.deiconify()
        self.bring_to_front()
        self.reload()

    # ------------------------------------------------------------------ layout
    def _build_header(self):
        t, px = self.theme, self.theme.px
        outer = ttk.Frame(self, padding=(px(22), px(18), px(22), px(12)))
        outer.pack(fill="x")
        head = ttk.Frame(outer)
        head.pack(fill="x")
        ttk.Label(head, text="장비 외형", style="DialogTitle.TLabel").pack(side="left")
        ttk.Label(outer, text="모드가 게임 속 어떤 장비의 모습을 바꾸는지 게임의 이름과 그림으로 보여 줍니다",
                  style="Muted.TLabel", font=t.fonts["small"]).pack(anchor="w", pady=(px(2), 0))
        self.refresh_button = ttk.Button(head, text="새로 고침", style="Compact.TButton", command=self.reload)
        self.refresh_button.pack(side="right")
        self.enabled_var = tk.BooleanVar(value=False)
        t.checkbutton(head, "켠 모드만", self.enabled_var, command=self.render).pack(side="right", padx=(0, px(12)))
        self.mod_var = tk.StringVar(value="")
        self.mod_button = ttk.Menubutton(head, text="모든 모드", style="Chip.TMenubutton")
        self.mod_button.pack(side="right", padx=(0, px(10)))
        self.mod_menu = t.mark(tk.Menu(self.mod_button, tearoff=False, postcommand=self._fill_mod_menu), "menu")
        self.mod_button.configure(menu=self.mod_menu)
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *a: self._search_later())
        self._search_after = None
        search = ttk.Entry(head, textvariable=self.search_var, width=24)
        search.pack(side="right", padx=(0, px(10)), ipady=px(1))

        def escape(_event):
            if self.search_var.get():
                self.search_var.set("")
                return "break"  # clear the search first; another Escape closes the window
            return None

        search.bind("<Escape>", escape)
        hint = ttk.Label(head, text="⌕  장비 · 모드 이름 검색", style="FieldHint.TLabel", cursor="xterm")
        hint.bind("<Button-1>", lambda e: search.focus_set())

        def update_hint(*_args):
            if self.search_var.get() or self.focus_get() is search:
                hint.place_forget()
            else:
                hint.place(in_=search, x=px(10), rely=0.5, anchor="w")

        search.bind("<FocusIn>", update_hint, add="+")
        search.bind("<FocusOut>", update_hint, add="+")
        self.search_var.trace_add("write", update_hint)
        self.after_idle(update_hint)

    def _search_later(self):
        """Filter shortly after typing stops (drawing the tiles again on every key is slow)."""
        if self._search_after:
            self.after_cancel(self._search_after)
        self._search_after = self.after(250, self._search_now)

    def _search_now(self):
        self._search_after = None
        self.render()
        self.list_canvas.yview_moveto(0)

    def _build_body(self):
        t, px = self.theme, self.theme.px
        body = ttk.Frame(self, padding=(px(22), 0, px(22), px(10)))
        body.pack(fill="both", expand=True)
        nav = ttk.Frame(body, width=px(180))
        nav.pack(side="left", fill="y", padx=(0, px(14)))
        nav.pack_propagate(False)
        self.nav_canvas, self.nav = self._scroller(nav, padding=0, plain=True)

        self.detail_card = t.mark(tk.Frame(body, width=px(340)), "card")
        self.detail_card.pack(side="right", fill="y", padx=(px(14), 0))
        self.detail_card.pack_propagate(False)
        self.detail_canvas, self.detail = self._scroller(self.detail_card, padding=(px(18), px(16)))

        card = t.mark(tk.Frame(body), "card")
        card.pack(side="left", fill="both", expand=True)
        self.list_canvas, self.list = self._scroller(card, padding=(px(12), px(12)))

    def _scroller(self, parent, padding, plain: bool = False):
        """A scrolling frame (``plain``: on the window's background instead of a card)."""
        canvas = self.theme.mark(tk.Canvas(parent, width=1, height=1), "canvas_bg" if plain else "canvas")
        bar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=autohide(bar, side="right", fill="y"))
        canvas.pack(side="left", fill="both", expand=True)
        inner = ttk.Frame(canvas, style="TFrame" if plain else "Card.TFrame", padding=padding)
        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
        return canvas, inner

    def _bind_wheel(self, widget, canvas):
        self.app._bind_wheel(widget, canvas)

    # ------------------------------------------------------------------ loading
    def _mods_state(self):
        m = self.manager
        return m.revision, tuple((mod.id, mod.name, mod.enabled, tuple(mod.selected), mod.installed_at, mod.updated_at)
                                 for mod in m.mods)

    def mods_changed(self):
        """Called whenever the main list refreshes: reload when a mod was added, removed, updated,
        renamed, switched or reordered."""
        state = self._mods_state()
        if state != self._seen:
            self._seen = state
            self.schedule_reload()

    def schedule_reload(self):
        """The mods changed: read them again shortly (several changes in a row cost one reload)."""
        if self.closed:
            return
        if self._reload_after:
            self.after_cancel(self._reload_after)
        self._reload_after = self.after(400, self.reload)

    def reload(self, rebuild: bool = False):
        self._reload_after = None
        self._seen = self._mods_state()
        self.gen += 1
        gen, m = self.gen, self.manager
        jobs = m.equipment_jobs()
        if self.overview is None:
            self._message("게임에서 장비 이름과 그림을 읽는 중...", "처음 한 번은 조금 걸립니다.")
        self.refresh_button.state(["disabled"])
        self.status_var.set("읽는 중...")

        def progress(done, total, msg):
            self.app.events.put(("call", self._progress, gen, msg))

        def worker():
            try:
                catalog = m.equipment_catalog(progress, rebuild=rebuild)
                scans = m.equipment_scans(jobs, progress)
                ui = m.equipment_ui() if catalog is not None else {}
            except Exception as exc:  # shown in the window; the details go to the log
                log.warning("장비 외형을 읽지 못했습니다: %s", exc, exc_info=not isinstance(exc, OSError))
                self.app.events.put(("call", self._loaded, gen, jobs, None, None, {}, exc))
            else:
                self.app.events.put(("call", self._loaded, gen, jobs, catalog, scans, ui, None))

        threading.Thread(target=worker, daemon=True).start()

    def _progress(self, gen: int, msg: str):
        if not self.closed and gen == self.gen:
            self.status_var.set(msg if len(msg) < 90 else "…" + msg[-88:])

    def _loaded(self, gen, jobs, catalog, scans, ui, error):
        if self.closed or gen != self.gen:
            return
        self.refresh_button.state(["!disabled"])
        if error is not None:
            self.status_var.set("")
            self._message("장비 외형을 읽지 못했습니다", str(error))
            return
        self.catalog, self.ui = catalog, ui or {}
        self.overview = ov.build([(j.mod_id, j.name, j.enabled, j.order, scans[j.mod_id]) for j in jobs], catalog)
        self.icons = {}
        if self.mod_filter and not any(j.mod_id == self.mod_filter for j in jobs):
            self.mod_filter = None
        self.render()
        self.app._update_details()  # the info tab's 외형 row could not be shown while this was read

    def _icon(self, name: str, color: str = "text", size: int = 18):
        """One of the game's icons ("w<type>" for a weapon type, "helm"... "leg", "kinsect", "palico") in a
        theme colour (the game's glyphs are white); None when the game's icon could not be read."""
        key = (name, self.theme.name, color, size)
        if key not in self.icons:
            self.icons[key] = None
            if name.startswith("w"):
                path = (self.ui.get("weapon_icons") or {}).get(name[1:])
            else:
                path = (self.ui.get("icons") or {}).get(name)
            if path and Image is not None:
                try:
                    from .equip.icons import tint

                    rgb = tuple(c // 256 for c in self.winfo_rgb(self.theme.p[color]))
                    px = self.theme.px(size)
                    with Image.open(path) as img:
                        glyph = tint(img.convert("RGBA"), rgb)
                    self.icons[key] = ImageTk.PhotoImage(glyph.resize((px, px), Image.LANCZOS), master=self)
                except Exception as exc:  # no icon is fine
                    log.debug("아이콘을 읽지 못했습니다 (%s): %s", name, exc)
        return self.icons[key]

    def _group_icon(self, group: str):
        if group.startswith("weapon/"):
            return self._icon("w" + str(int(group[7:]))) if group[7:].isdigit() else None
        name = {"armor": "body", "weapon": "w0", "kinsect": "kinsect", "palico": "palico"}.get(group)
        return self._icon(name) if name else None

    def _chip(self, parent, part: str, style: str) -> ttk.Label:
        """A part label; armor pieces with the game's icon in the label's colour."""
        color = {"PartOn.TLabel": "chip_text", "PartConflict.TLabel": "danger"}.get(style, "subtle")
        image = self._icon(part, color, 13) if part in ARMOR_PARTS[:5] else None
        return ttk.Label(parent, text=PART_LABELS.get(part, part), style=style, image=image or "", compound="left")

    # ------------------------------------------------------------------ rendering
    def _visible_items(self) -> list[ov.Item]:
        if self.overview is None:
            return []
        query = self.search_var.get().strip().lower()
        only_enabled = self.enabled_var.get()
        out = []
        for item in self.overview.items:
            if self.mod_filter and not any(u.mod_id == self.mod_filter for u in item.uses):
                continue
            if only_enabled and not item.enabled:
                continue
            if query and query not in item.search_text():
                continue
            out.append(item)
        return out

    def render(self):
        if self.closed or self.overview is None:
            return
        items = self._visible_items()
        self._render_nav(items)
        shown = [i for i in items if self._in_group(i)]
        if self.selected is not None and not any(i.target == self.selected for i in shown[:MAX_TILES]):
            self.selected = None
        if self.selected is None and shown:
            self.selected = shown[0].target
        self._render_tiles(shown)
        self._update_mod_button()
        o = self.overview
        changed = sum(1 for i in o.items if i.enabled)
        mods = len({u.mod_id for i in o.items for u in i.uses})
        bits = [f"모드 {mods}개가 장비 {len(o.items)}개의 모습을 바꿉니다 (켠 모드 기준 {changed}개)"]
        conflicts = sum(1 for i in o.items if i.conflicts())
        if conflicts:
            bits.append(f"겹치는 장비 {conflicts}개")
        if self.catalog is None:
            bits.append("게임을 읽지 못해 장비 번호로 보여 줍니다")
        elif self.catalog.problems:
            bits.append("일부 게임 데이터를 읽지 못했습니다")
        self.status_var.set(" · ".join(bits))
        self._render_detail()

    def _in_group(self, item: ov.Item) -> bool:
        return self.group == ALL or item.group == self.group or item.group.split("/")[0] == self.group

    def _render_nav(self, items: list[ov.Item]):
        px = self.theme.px
        for child in self.nav.winfo_children():
            child.destroy()
        groups = self.overview.subset(items).groups()
        if self.group != ALL and not any(g == self.group for g, _l, _n in groups):
            self.group = ALL
        entries = [(ALL, "전체", len(items))] + groups
        for key, label, count in entries:
            sub = "/" in key
            image = self._group_icon(key)
            button = ttk.Button(self.nav, text=f"{label}  {count}", compound="left", image=image or "",
                                style="NavActive.TButton" if key == self.group else "Nav.TButton",
                                command=lambda k=key: self._set_group(k))
            button.pack(fill="x", pady=(0, px(2)), padx=(px(16) if sub else 0, 0))
        self._bind_wheel(self.nav, self.nav_canvas)

    def _set_group(self, key: str):
        self.group = key
        self.render()
        self.list_canvas.yview_moveto(0)

    def _render_tiles(self, items: list[ov.Item]):
        px = self.theme.px
        for child in self.list.winfo_children():
            child.destroy()
        self.tiles = {}
        if not items:
            if self.overview is not None and not self.overview.items:
                self._message("모습을 바꾸는 장비가 없습니다",
                              "설치된 모드에서 장비 모델·텍스처 파일을 찾지 못했습니다.", clear=False)
            elif self.mod_filter and not any(u.mod_id == self.mod_filter for i in self.overview.items for u in i.uses):
                self._message("이 모드는 장비의 모습을 바꾸지 않습니다",
                              "스크립트나 설정 파일만 있거나, 장비가 아닌 게임 파일을 바꾸는 모드입니다.", clear=False)
            else:
                self._message("조건에 맞는 장비가 없습니다", "검색어나 필터를 바꿔 보세요.", clear=False)
            return
        for item in items[:MAX_TILES]:
            self.tiles[item.target] = self._tile(item)
        if len(items) > MAX_TILES:
            ttk.Label(self.list, text=f"… 외 {len(items) - MAX_TILES}개 (검색이나 분류로 좁혀 보세요)",
                      style="CardMuted.TLabel").pack(anchor="w", pady=(px(6), 0))
        self._bind_wheel(self.list, self.list_canvas)
        if self.selected in self.tiles:
            self._restyle(self.tiles[self.selected], True)

    def _message(self, title: str, text: str, clear: bool = True):
        if clear:
            for child in self.list.winfo_children():
                child.destroy()
            for child in self.detail.winfo_children():
                child.destroy()
        box = ttk.Frame(self.list, style="Card.TFrame", padding=(self.theme.px(12), self.theme.px(40)))
        box.pack(fill="x")
        ttk.Label(box, text=title, style="EmptyTitle.TLabel").pack()
        width = self.list_canvas.winfo_width() - self.theme.px(60)
        ttk.Label(box, text=text, style="CardMuted.TLabel", wraplength=width if width > 200 else self.theme.px(420),
                  justify="center").pack(pady=(self.theme.px(6), 0))

    def _rarity_color(self, rarity: int | None) -> str | None:
        if not rarity:
            return None
        colors = self.ui.get("rarity") or {}
        return colors.get(str(rarity)) or colors.get(rarity) or RARITY_COLORS.get(rarity)

    def _rarity_text(self, rarity: int | None) -> str | None:
        """The rarity colour for text: the game's colours are made for a dark background, so on the light
        theme they are darkened."""
        color = self._rarity_color(rarity)
        if color is None or self.theme.name == "dark":
            return color
        r, g, b = (c // 256 for c in self.winfo_rgb(color))
        return "#%02x%02x%02x" % (r * 55 // 100, g * 55 // 100, b * 55 // 100)

    def _picture_box(self, parent, size: int, rarity: int | None, style: str = "Thumb.TLabel"):
        """A square picture frame with the rarity colour along its bottom edge, like the game's icons."""
        px = self.theme.px
        frame = ttk.Frame(parent, style="Thumb.TFrame", width=size, height=size)
        frame.pack_propagate(False)
        label = ttk.Label(frame, style=style, anchor="center")
        label.pack(fill="both", expand=True)
        color = self._rarity_color(rarity)
        if color:
            tk.Frame(frame, bg=color, height=px(3)).pack(side="bottom", fill="x")
        return frame, label

    def _tile(self, item: ov.Item) -> ttk.Frame:
        t, px = self.theme, self.theme.px
        tile = ttk.Frame(self.list, style="Tile.TFrame", padding=(px(10), px(9)))
        tile.pack(fill="x", pady=(0, px(6)))
        box, picture = self._picture_box(tile, px(self.THUMB), item.rarity)
        box.pack(side="left", anchor="n")
        self._picture(picture, item, item.target.with_part(self._show_part(item)), px(self.THUMB))

        right = ttk.Frame(tile, style="Tile.TFrame")
        right.pack(side="right", anchor="n", padx=(px(8), 0))
        if item.rarity:
            ttk.Label(right, text=f"레어 {item.rarity}", style="TileMuted.TLabel",
                      foreground=self._rarity_text(item.rarity)).pack(anchor="e")
        conflicts = item.conflicts()
        if conflicts:
            chip = ttk.Label(right, text="겹침", style="PartConflict.TLabel")
            chip.pack(anchor="e", pady=(px(4), 0))
            Tooltip(t, chip, self._conflict_text(conflicts))

        text = ttk.Frame(tile, style="Tile.TFrame")
        text.pack(side="left", fill="x", expand=True, padx=(px(12), 0))
        ttk.Label(text, text=item.name, style="TileTitle.TLabel").pack(anchor="w")
        ttk.Label(text, text=item.subtitle, style="TileMuted.TLabel").pack(anchor="w")
        if item.parts or item.changed:
            row = ttk.Frame(text, style="Tile.TFrame", height=px(22))
            row.pack(fill="x", pady=(px(5), 0))
            changed = set(item.changed)
            for part in self._parts_shown(item):
                style = "PartConflict.TLabel" if part in conflicts else (
                    "PartOn.TLabel" if part in changed else "PartOff.TLabel")
                self._chip(row, part, style)
            self._flow(row, px(4))
        mods = [u for u in item.uses if u.enabled or not self.enabled_var.get()]
        names = " · ".join(u.name if u.enabled else f"{u.name} (꺼짐)" for u in mods[:4])
        if len(mods) > 4:
            names += f" 외 {len(mods) - 4}개"
        users = ttk.Label(text, text=names, style="TileMuted.TLabel", wraplength=px(460), justify="left")
        users.pack(anchor="w", pady=(px(5), 0))
        text.bind("<Configure>", lambda e: users.configure(wraplength=max(px(160), e.width - px(4))), add="+")

        def click(_event=None, target=item.target):
            self.select(target)

        def open_mod(_event=None, item=item):
            uses = [u for u in item.uses if u.enabled] or item.uses
            if len(uses) == 1:
                self.app.show_mod(uses[0].mod_id)

        for widget in self._descendants(tile):
            widget.bind("<Button-1>", click, add="+")
            widget.bind("<Double-Button-1>", open_mod, add="+")
        return tile

    @staticmethod
    def _flow(frame, gap: int):
        """Lay the frame's children out left to right, going on in a new line when one does not fit."""
        def layout(_event=None):
            width = frame.winfo_width()
            if width <= 1:
                return
            x = y = line = 0
            for child in frame.winfo_children():
                w, h = child.winfo_reqwidth(), child.winfo_reqheight()
                if x and x + w > width:
                    x, y, line = 0, y + line + gap, 0
                child.place(x=x, y=y)
                x, line = x + w + gap, max(line, h)
            if int(frame.cget("height")) != y + line:
                frame.configure(height=y + line)

        frame.bind("<Configure>", layout, add="+")

    @staticmethod
    def _parts_shown(item: ov.Item) -> list[str]:
        """Armor shows all five pieces (changed ones lit); other equipment only the parts changed."""
        if item.target.kind == "armor":
            return list(item.parts) + [p for p in item.changed if p not in item.parts]
        return [p for p in item.changed if p]

    @staticmethod
    def _show_part(item: ov.Item) -> str:
        """The part whose picture stands for the item: the body when it is changed, else the first changed."""
        changed = [p for p in item.changed if p]
        if item.target.kind == "armor":
            for part in ("body", "helm", "arm", "waist", "leg"):
                if part in changed:
                    return part
            return "body"
        return changed[0] if changed and item.target.kind == "palico" else ""

    def _descendants(self, widget):
        yield widget
        for child in widget.winfo_children():
            yield from self._descendants(child)

    def _restyle(self, tile, selected: bool):
        for widget in self._descendants(tile):
            try:
                style = str(widget.cget("style"))
            except tk.TclError:
                continue
            if selected and style.startswith("Tile") and not style.startswith("TileSel"):
                widget.configure(style="TileSel" + style[4:])
            elif not selected and style.startswith("TileSel"):
                widget.configure(style="Tile" + style[7:])

    def select(self, target: Target):
        if self.selected in self.tiles:
            self._restyle(self.tiles[self.selected], False)
        self.selected = target
        if target in self.tiles:
            self._restyle(self.tiles[target], True)
        self._render_detail()

    # ------------------------------------------------------------------ details
    def _render_detail(self):
        t, px = self.theme, self.theme.px
        for child in self.detail.winfo_children():
            child.destroy()
        item = next((i for i in (self.overview.items if self.overview else []) if i.target == self.selected), None)
        if item is None:
            ttk.Label(self.detail, text="장비를 고르면 자세한 내용이 보입니다.", style="CardMuted.TLabel",
                      wraplength=px(296)).pack(anchor="w")
            return
        d = self.detail
        box, picture = self._picture_box(d, px(self.PICTURE), item.rarity)
        box.pack(anchor="center")
        self._picture(picture, item, item.target.with_part(self._show_part(item)), px(self.PICTURE))
        if item.target.kind == "armor":
            self._part_pictures(d, item)
        ttk.Label(d, text=item.name, style="CardTitle.TLabel", wraplength=px(296), justify="left").pack(
            anchor="w", pady=(px(14), 0))
        bits = [item.subtitle] + ([f"레어 {item.rarity}"] if item.rarity else [])
        ttk.Label(d, text=" · ".join(bits), style="CardMuted.TLabel").pack(anchor="w", pady=(px(2), 0))
        if len(item.names) > 1 and item.name != " · ".join(item.names):
            ttk.Label(d, text=" / ".join(item.names), style="CardMuted.TLabel", wraplength=px(296),
                      justify="left", font=t.fonts["small"]).pack(anchor="w", pady=(px(6), 0))

        self._section(d, "바꾸는 모드")
        conflicts = item.conflicts()
        for use in item.uses:
            row = ttk.Frame(d, style="Card.TFrame")
            row.pack(fill="x", pady=(px(8), 0))
            self._mod_picture(row, use)
            text = ttk.Frame(row, style="Card.TFrame")
            text.pack(side="left", fill="x", expand=True)
            title = use.name + ("" if use.enabled else "  (꺼짐)")
            ttk.Label(text, text=title, style="Card.TLabel", font=t.fonts["bold"], wraplength=px(190),
                      justify="left").pack(anchor="w")
            parts = [PART_LABELS.get(p, p) for p in (item.parts or ()) if p in use.parts]
            parts += [PART_LABELS.get(p, p) for p in use.parts if p and p not in (item.parts or ())]
            detail = (" · ".join(parts) + " · " if parts else "") + f"파일 {use.files}개"
            ttk.Label(text, text=detail, style="CardMuted.TLabel", font=t.fonts["small"], wraplength=px(190),
                      justify="left").pack(anchor="w")
            ttk.Button(text, text="목록에서 보기", style="Link.TButton",
                       command=lambda mod_id=use.mod_id: self.app.show_mod(mod_id)).pack(anchor="w")
        if conflicts:
            self._section(d, "겹치는 부위")
            ttk.Label(d, text=self._conflict_text(conflicts), style="CardMuted.TLabel", wraplength=px(296),
                      justify="left").pack(anchor="w", pady=(px(4), 0))
        if item.notes:
            self._section(d, "참고")
            for note in item.notes:
                ttk.Label(d, text=note, style="CardMuted.TLabel", wraplength=px(296), justify="left").pack(
                    anchor="w", pady=(px(4), 0))
        ttk.Label(d, text=self._code(item.target), style="CardMuted.TLabel", font=t.fonts["small"]).pack(
            anchor="w", pady=(px(16), 0))
        self._bind_wheel(d, self.detail_canvas)
        self.detail_canvas.yview_moveto(0)

    def _mod_picture(self, parent, use: ov.ModUse):
        """The mod's own preview picture (what it looks like applied, as its author shows it); a click
        shows it large."""
        try:
            path = self.manager.mod_picture(use.mod_id)
        except KeyError:  # removed meanwhile
            path = None
        if path is None:
            return
        px = self.theme.px
        image = self.app._thumbnail(path, px(96), px(72))
        if image is None:
            return
        label = ttk.Label(parent, image=image, style="Card.TLabel", cursor="hand2")
        label.image = image
        label.pack(side="left", anchor="n", padx=(0, px(10)))
        label.bind("<Button-1>", lambda e: self.app.show_picture(path, use.name, parent=self))
        Tooltip(self.theme, label, "모드에 들어 있는 미리보기 그림 · 누르면 크게 봅니다")

    def _section(self, parent, title: str):
        ttk.Label(parent, text=title, style="Section.TLabel").pack(anchor="w", pady=(self.theme.px(16), 0))

    def _part_pictures(self, parent, item: ov.Item):
        px = self.theme.px
        row = ttk.Frame(parent, style="Card.TFrame")
        row.pack(anchor="center", pady=(px(10), 0))
        changed = set(item.changed)
        for part in ARMOR_PARTS[:5]:
            cell = ttk.Frame(row, style="Card.TFrame")
            cell.pack(side="left", padx=px(3))
            box, picture = self._picture_box(cell, px(self.PART), item.rarity if part in changed else None)
            box.pack()
            self._picture(picture, item, item.target.with_part(part), px(self.PART))
            ttk.Label(cell, text=PART_LABELS[part], style="PartOn.TLabel" if part in changed else "PartOff.TLabel"
                      ).pack(pady=(px(3), 0))

    @staticmethod
    def _conflict_text(conflicts: dict) -> str:
        lines = []
        for part, users in conflicts.items():
            where = PART_LABELS.get(part, "") or "같은 장비"
            names = ", ".join(f"'{u.name}'" for u in users)
            lines.append(f"{where}: {names}이(가) 함께 바꿉니다. 같은 파일을 바꾸면 아래쪽 '{users[-1].name}'이(가) "
                         "우선합니다.")
        return "\n".join(lines)

    @staticmethod
    def _code(target: Target) -> str:
        """The game's own numbering (model folders), for modders."""
        try:
            if target.kind == "armor":
                body, model, design = parse_armor_key(target.key)
                return f"{'ch02' if body == 'm' else 'ch03'} · 모델 {model:03d} · {design:03d} ({BODIES.get(body, '')})"
            if target.kind == "weapon":
                wtype, model = parse_weapon_key(target.key)
                return f"it{wtype:02d} · 모델 {model // 10000:02d} · {model % 10000:04d}"
        except ValueError:
            pass
        folder = {"kinsect": "it10 · 03 · ", "palico": "ch05 · ", "monster": ""}.get(target.kind, "")
        return f"{folder}{target.key}"

    # ------------------------------------------------------------------ pictures
    def _picture(self, label: ttk.Label, item: ov.Item, target: Target, size: int):
        """Show the game's preview picture of ``target`` in ``label`` (loaded in the background)."""
        tex = self.catalog.thumb(target) if self.catalog is not None else None
        if not tex:
            label.configure(text=self._placeholder(item), image="")
            return
        key = (tex, size)
        image = self.images.get(key)
        if image is not None:
            label.configure(image=image, text="")
            return
        label.configure(text="…")
        waiting = self.waiting.setdefault(key, [])
        waiting.append(label)
        if len(waiting) == 1:
            self._thumbs.put((tex, size))

    def _placeholder(self, item: ov.Item) -> str:
        if item.target.kind == "weapon":
            return item.subtitle.split(" · ")[0]
        return {"armor": "방어구", "palico": "오토모", "seikret": "세크레트", "npc": "NPC",
                "monster": "몬스터"}.get(item.target.kind, "")

    def _thumb_worker(self):
        while True:
            request = self._thumbs.get()
            if request is None:
                return
            tex, size = request
            try:
                path = self.manager.equipment_thumbnail(tex, size)
            except Exception as exc:  # a picture that cannot be read just stays empty
                log.debug("장비 그림을 읽지 못했습니다 (%s): %s", tex, exc)
                path = None
            self.app.events.put(("call", self._thumb_ready, tex, size, path))

    def _thumb_ready(self, tex: str, size: int, path: Path | None):
        if self.closed:
            return
        image = load_image(path, size, size) if path is not None else None
        if image is not None:
            self.images[(tex, size)] = image
        for label in self.waiting.pop((tex, size), []):
            try:
                if label.winfo_exists():
                    label.configure(image=image or "", text="" if image is not None else "그림 없음")
            except tk.TclError:
                pass

    # ------------------------------------------------------------------ mods filter
    def _fill_mod_menu(self):
        menu = self.mod_menu
        menu.delete(0, "end")
        self.mod_var.set(self.mod_filter or "")
        menu.add_radiobutton(label="모든 모드", variable=self.mod_var, value="", command=lambda: self.filter_mod(None))
        if self.overview is None:
            return
        uses = {}
        for item in self.overview.items:
            for use in item.uses:
                uses.setdefault(use.mod_id, (use.order, use.name))
        if uses:
            menu.add_separator()
        for mod_id, (_order, name) in sorted(uses.items(), key=lambda kv: kv[1][0]):
            menu.add_radiobutton(label=name, variable=self.mod_var, value=mod_id, command=lambda m=mod_id: self.filter_mod(m))

    def filter_mod(self, mod_id: str | None):
        self.mod_filter = mod_id
        self.mod_var.set(mod_id or "")
        self.group = ALL
        self.render()
        self.list_canvas.yview_moveto(0)

    def _update_mod_button(self):
        name = None
        if self.mod_filter:
            mod = next((m for m in self.manager.mods if m.id == self.mod_filter), None)
            name = mod.name if mod else None
        text = f"모드: {name}" if name else "모든 모드"
        self.mod_button.configure(text=text if len(text) < 18 else text[:16] + "…")

    def show(self, mod_id: str | None = None):
        """Bring the window up, optionally showing only one mod's equipment."""
        if mod_id is not None:
            self.filter_mod(mod_id)
        self.bring_to_front()

    def bring_to_front(self):
        """In front of the main window. Opened by a click in the main window, which Windows brings
        back to the front once the click is over: so once more a moment later."""
        def front():
            if self.closed:
                return
            self.deiconify()
            self.lift()
            try:  # Windows does not always let lift() put a window over the active one
                self.attributes("-topmost", True)
                self.attributes("-topmost", False)
            except tk.TclError:
                pass
            self.focus_force()

        front()
        self.after(150, front)

    # ------------------------------------------------------------------ misc
    def save_icon_sheet(self):
        target = filedialog.asksaveasfilename(parent=self, title="아이콘 확인용 그림 저장", defaultextension=".png",
                                              initialfile="mhwilds-icons.png", filetypes=[("PNG", "*.png")])
        if not target:
            return
        self.status_var.set("아이콘 그림 저장 중...")

        def worker():
            try:
                result = self.manager.save_equipment_icon_sheet(Path(target))
            except Exception as exc:  # reported in the window
                result = exc
            self.app.events.put(("call", self._icon_sheet_saved, target, result))

        threading.Thread(target=worker, daemon=True).start()

    def save_report(self):
        """A zip of what the window worked out and how the game's files read (to send to the developer
        when names or pictures are missing)."""
        target = filedialog.asksaveasfilename(parent=self, title="장비 외형 진단 파일 저장", defaultextension=".zip",
                                              initialfile="mhwilds-equipment-report.zip", filetypes=[("zip", "*.zip")])
        if not target:
            return
        self.status_var.set("진단 파일 저장 중...")

        def worker():
            try:
                self.manager.export_equipment_report(Path(target))
                result = True
            except Exception as exc:  # reported in the window
                result = exc
            self.app.events.put(("call", self._report_saved, target, result))

        threading.Thread(target=worker, daemon=True).start()

    def _report_saved(self, target: str, result):
        if self.closed:
            return
        self.status_var.set("")
        if isinstance(result, Exception):
            messagebox.showerror("진단 파일", str(result), parent=self)
        else:
            messagebox.showinfo("진단 파일", f"저장했습니다:\n{target}", parent=self)

    def _icon_sheet_saved(self, target: str, result):
        if self.closed:
            return
        self.status_var.set("")
        if isinstance(result, Exception):
            messagebox.showerror("아이콘 확인용 그림", str(result), parent=self)
        elif result:
            messagebox.showinfo("아이콘 확인용 그림", f"저장했습니다:\n{target}", parent=self)
        else:
            messagebox.showwarning("아이콘 확인용 그림", "게임에서 아이콘 그림을 읽지 못했습니다.", parent=self)

    def close(self):
        self.closed = True
        self._thumbs.put(None)
        self.gen += 1
        for pending in (self._reload_after, self._search_after):
            if pending:
                self.after_cancel(pending)
        self.app.equipment_window = None
        self.destroy()

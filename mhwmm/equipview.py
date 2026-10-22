"""The 장비 외형 window: which in-game equipment the installed mods change the look of, with the
game's own names and preview pictures."""

from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
from collections import deque
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .equip import look as equip_look
from .equip import overview as ov
from .equip.model import (ARMOR_PARTS, BODIES, KIND_LABELS, PART_LABELS, Target, parse_armor_key,
                          parse_weapon_key)
from .gui import Dialog, FollowWidth, Image, ImageTk, Tooltip, autohide, load_image, set_dark_titlebar

log = logging.getLogger(__name__)

# rarity colours when the game's own colour table cannot be read (close to the in-game ones)
RARITY_COLORS = {1: "#c9c9c9", 2: "#ececec", 3: "#a7d46b", 4: "#5eb3dc", 5: "#a283e8", 6: "#e69a4a",
                 7: "#e9655b", 8: "#55cfd6", 9: "#f0cf55", 10: "#d9d8ff", 11: "#f58fc6", 12: "#fff1c9"}
MAX_TILES = 120  # more tiles take long to draw (narrow the list with the groups or the search instead)
ALL = "all"
LOOK_NOTE = "간단한 3D 미리보기 · 게임과 색·광택이 다를 수 있습니다"
LOOK_SHORT = {"hidden": "숨김", "no_mesh": "미리보기 없음", "unsupported": "지원 안 함", "too_big": "너무 큼",
              "missing": "파일 누락", "error": "오류"}  # in the small "적용 후" pictures


class EquipmentWindow(tk.Toplevel):
    THUMB = 72  # tile picture (logical px)
    PICTURE = 220  # picture in the detail panel
    PART = 52  # per-part pictures in the detail panel
    LOOK = 52  # the "적용 후" 3D pictures under each mod in the detail panel
    LOOK_FEW = 72  # ... when the mod changes three parts or fewer

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
        self.jobs: dict = {}  # mod id -> EquipJob of the last reload (for the 3D previews)
        self.looks: dict[tuple, object] = {}  # look.Preview by request (see _want_look)
        self.look_waiting: dict[tuple, list] = {}  # request -> callbacks waiting for it
        self.look_cells: dict[tuple[str, str], ttk.Label] = {}  # (mod id, part) -> its "적용 후" picture
        self._look_queue: deque = deque()  # (urgent, request, job, target, size, compare); a dialog's first
        self._look_cv = threading.Condition()
        threading.Thread(target=self._look_worker, daemon=True).start()

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
        FollowWidth(canvas, window)  # (all the tiles are laid out again for a new width: not on every drag step)
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
            self._drop_looks(cells=False)  # pictures not drawn yet may be of files the mods no longer use
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
        self.jobs = {j.mod_id: j for j in jobs}
        self.looks.clear()  # a material repair changes the files without the stamp: ask the manager again
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
        box, picture = self._picture_box(tile, px(self.THUMB), item.top_rarity)
        box.pack(side="left", anchor="n")
        self._picture(picture, item, item.target.with_part(self._show_part(item)), px(self.THUMB))

        right = ttk.Frame(tile, style="Tile.TFrame")
        right.pack(side="right", anchor="n", padx=(px(8), 0))
        if item.rarity:
            ttk.Label(right, text=item.rarity_text, style="TileMuted.TLabel",
                      foreground=self._rarity_text(item.top_rarity)).pack(anchor="e")
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
        wrap = [px(460)]

        def follow(event):  # (not when the new wraplength made it taller or it only moved)
            width = max(px(160), event.width - px(4))
            if width != wrap[0]:
                wrap[0] = width
                users.configure(wraplength=width)

        text.bind("<Configure>", follow, add="+")

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
        laid: dict = {"width": None, "spots": None}

        def layout(_event=None):
            width = frame.winfo_width()
            if width <= 1 or width == laid["width"]:
                return  # moved, or the height below changed: the chips stay where they are
            laid["width"] = width
            children = frame.winfo_children()
            x = y = line = 0
            spots = []
            for child in children:
                w, h = child.winfo_reqwidth(), child.winfo_reqheight()
                if x and x + w > width:
                    x, y, line = 0, y + line + gap, 0
                spots.append((x, y))
                x, line = x + w + gap, max(line, h)
            if spots != laid["spots"]:  # (a little wider or narrower mostly keeps every chip where it is)
                laid["spots"] = spots
                for child, (cx, cy) in zip(children, spots):
                    child.place(x=cx, y=cy)
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
        self._drop_looks()
        item = next((i for i in (self.overview.items if self.overview else []) if i.target == self.selected), None)
        if item is None:
            ttk.Label(self.detail, text="장비를 고르면 자세한 내용이 보입니다.", style="CardMuted.TLabel",
                      wraplength=px(296)).pack(anchor="w")
            return
        d = self.detail
        box, picture = self._picture_box(d, px(self.PICTURE), item.top_rarity)
        box.pack(anchor="center")
        self._picture(picture, item, item.target.with_part(self._show_part(item)), px(self.PICTURE))
        if item.target.kind == "armor":
            self._part_pictures(d, item)
        ttk.Label(d, text=item.name, style="CardTitle.TLabel", wraplength=px(296), justify="left").pack(
            anchor="w", pady=(px(14), 0))
        bits = [item.subtitle] + ([item.rarity_text] if item.rarity else [])
        ttk.Label(d, text=" · ".join(bits), style="CardMuted.TLabel").pack(anchor="w", pady=(px(2), 0))
        if len(item.names) > 1:
            self._names(d, item)

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
            self._look_strip(d, item, use)
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

    # ------------------------------------------------------------------ 3D previews ("적용 후")
    @staticmethod
    def _look_parts(item: ov.Item, use: ov.ModUse) -> list[str]:
        """The parts a mod changes, in the game's order; "" for equipment drawn whole (no parts)."""
        parts = [p for p in item.parts if p in use.parts]
        parts += sorted(p for p in use.parts if p and p not in parts)
        if parts or "" not in use.parts or item.target.kind == "armor":
            return parts  # an armor's files of no part (its prefab settings) change no model
        return ["main"] if item.target.kind == "weapon" else [""]

    def _look_strip(self, parent, item: ov.Item, use: ov.ModUse):
        """Small 3D pictures of the parts the mod changes, drawn with its files (in the background);
        a click shows one next to the game's own look."""
        job = self.jobs.get(use.mod_id)
        parts = self._look_parts(item, use)
        if job is None or not parts or Image is None:
            return
        t, px = self.theme, self.theme.px
        size = px(self.LOOK_FEW if len(parts) <= 3 else self.LOOK)
        box = ttk.Frame(parent, style="Card.TFrame")
        box.pack(fill="x", pady=(px(8), 0))
        ttk.Label(box, text="적용 후 · 간단한 3D", style="CardMuted.TLabel", font=t.fonts["small"]).pack(anchor="w")
        row = ttk.Frame(box, style="Card.TFrame", height=size + px(22))
        row.pack(fill="x", pady=(px(3), 0))
        for part in parts:
            cell = ttk.Frame(row, style="Card.TFrame")
            frame = ttk.Frame(cell, style="Thumb.TFrame", width=size, height=size, cursor="hand2")
            frame.pack_propagate(False)
            frame.pack()
            label = ttk.Label(frame, text="…", style="Thumb.TLabel", anchor="center", justify="center",
                              wraplength=size - px(4), cursor="hand2")
            label.pack(fill="both", expand=True)
            label.look = None
            name = PART_LABELS.get(part) or KIND_LABELS.get(item.target.kind, "")
            label.caption = ttk.Label(cell, text=name, style="CardMuted.TLabel", font=t.fonts["small"])
            label.caption.pack(pady=(px(2), 0))
            for widget in (frame, label):
                widget.bind("<Button-1>", lambda e, p=part: self.show_look(item, use, p))
            Tooltip(t, label, lambda label=label: self._look_tip(label))
            self.look_cells[(use.mod_id, part)] = label
            self._want_look(job, item.target.with_part(part), size, False,
                            lambda p, label=label: self._show_look(label, p, size))
        self._flow(row, px(6))

    def _show_look(self, label: ttk.Label, preview, size: int):
        try:
            if not label.winfo_exists():
                return
            label.look = preview
            image = self._look_image(preview.after, size)
            if image is not None:
                label.configure(image=image, text="")
            else:
                label.configure(image="", text=LOOK_SHORT.get(preview.status, "미리보기 없음"))
            if preview.status in ("missing", "error"):  # the game would hang or show it broken
                caption = label.caption
                caption.configure(text=f"{caption.cget('text')} ⚠", foreground=self.theme.p["danger"])
        except tk.TclError:  # closed meanwhile
            pass

    def _look_image(self, path: Path | None, size: int):
        """A preview PNG as a PhotoImage (kept: Tk drops unheld ones), None when there is none."""
        if path is None:
            return None
        key = (str(path), size)
        if key not in self.images:
            self.images[key] = load_image(path, size, size)
        return self.images[key]

    @staticmethod
    def _look_tip(label) -> str:
        p = getattr(label, "look", None)
        if p is None:
            return "그리는 중... · 누르면 원래 모습과 나란히 봅니다"
        lines = ([p.text] if p.text else []) + [n for n in p.notes[:4] if n != p.text]
        return "\n".join(lines + ["누르면 원래 모습과 나란히 봅니다"])

    def show_look(self, item: ov.Item, use: ov.ModUse, part: str):
        """The comparison dialog of one part (원래 모습 | 적용 후), over this window."""
        job = self.jobs.get(use.mod_id)
        if job is not None and not self.closed:
            LookDialog(self, item, use, part, job).show()

    def _want_look(self, job, target: Target, size: int, compare: bool, done, urgent: bool = False) -> tuple:
        """Call ``done(look.Preview)`` on the Tk thread once the 3D preview is drawn (at once when it is
        known). ``urgent`` (the dialog) goes before the small pictures. Returns the request."""
        request = (job.mod_id, job.stamp, target, size, compare)
        found = self.looks.get(request)
        if found is not None:
            done(found)
            return request
        waiting = self.look_waiting.setdefault(request, [])
        waiting.append(done)
        if len(waiting) == 1:
            with self._look_cv:
                entry = (urgent, request, job, target, size, compare)
                if urgent:
                    self._look_queue.appendleft(entry)
                else:
                    self._look_queue.append(entry)
                self._look_cv.notify()
        return request

    def _drop_looks(self, cells: bool = True):
        """Forget the small pictures not drawn yet (the details now show something else, or will once the
        changed mods are read again); ``cells``: also the pictures shown."""
        with self._look_cv:
            dropped = [e[1] for e in self._look_queue if not e[0]]
            self._look_queue = deque(e for e in self._look_queue if e[0])
        for request in dropped:
            self.look_waiting.pop(request, None)
        if cells:
            self.look_cells = {}

    def cancel_look(self, request: tuple):
        """A dialog was closed before its picture came."""
        with self._look_cv:
            self._look_queue = deque(e for e in self._look_queue if e[1] != request)
        self.look_waiting.pop(request, None)

    def _look_worker(self):
        while True:
            with self._look_cv:
                while not self.closed and not self._look_queue:
                    self._look_cv.wait()
                if self.closed:
                    return
                _urgent, request, job, target, size, compare = self._look_queue.popleft()
            try:  # the small pictures show no swatches or colour chips: not made for them
                preview = self.manager.equipment_preview(job, target, size, compare, extras=compare)
            except Exception as exc:  # equipment_preview reports problems itself; this would be a bug
                log.warning("3D 미리보기를 만들지 못했습니다: %s", exc, exc_info=True)
                preview = equip_look.Preview("error", notes=[f"{equip_look.STATUS['error']}: {exc}"])
            self.app.events.put(("call", self._look_ready, request, preview))

    def _look_ready(self, request: tuple, preview):
        if self.closed:
            return
        self.looks[request] = preview
        for done in self.look_waiting.pop(request, []):
            done(preview)

    def _names(self, parent, item: ov.Item):
        """Every equipment that looks like this, each in its rarity's colour (low rank, α, β ...)."""
        px = self.theme.px
        box = ttk.Frame(parent, style="Card.TFrame")
        box.pack(anchor="w", fill="x", pady=(px(6), 0))
        rarities = item.rarities if len(item.rarities) == len(item.names) else [None] * len(item.names)
        for name, rarity in zip(item.names, rarities):
            row = ttk.Frame(box, style="Card.TFrame")
            row.pack(anchor="w", fill="x")
            ttk.Label(row, text=name, style="CardMuted.TLabel", wraplength=px(230), justify="left").pack(side="left")
            if rarity:
                ttk.Label(row, text=f"레어 {rarity}", style="CardMuted.TLabel", font=self.theme.fonts["small"],
                          foreground=self._rarity_text(rarity)).pack(side="right")

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
            box, picture = self._picture_box(cell, px(self.PART), item.top_rarity if part in changed else None)
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

        def progress(done, total, msg):  # the 3D preview's self-test takes up to a minute
            self.app.events.put(("call", self._report_progress, msg))

        def worker():
            try:
                self.manager.export_equipment_report(Path(target), progress)
                result = True
            except Exception as exc:  # reported in the window
                result = exc
            self.app.events.put(("call", self._report_saved, target, result))

        threading.Thread(target=worker, daemon=True).start()

    def _report_progress(self, msg: str):
        if not self.closed:
            self.status_var.set(f"진단 파일 저장 중... {msg}")

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
        with self._look_cv:
            self._look_queue.clear()
            self._look_cv.notify_all()
        self.gen += 1
        for pending in (self._reload_after, self._search_after):
            if pending:
                self.after_cancel(pending)
        self.app.equipment_window = None
        self.destroy()


class LookDialog(Dialog):
    """One part of an equipment as the game shows it and with a mod's files, side by side ("원래 모습 |
    적용 후"), drawn on demand by the window's preview worker; with the notes, the replaced textures and
    the changed colour values below."""

    MIN_LOOK = 240  # the pictures are made smaller down to this before what is below them scrolls

    def __init__(self, window: EquipmentWindow, item: ov.Item, use: ov.ModUse, part: str, job):
        theme = window.theme
        super().__init__(window, theme, "적용 후 미리보기")
        t, px = theme, theme.px
        self.window = window
        self.preview = None
        self.images: list = []  # PhotoImages shown (Tk drops unheld ones)
        width, height = self.winfo_screenwidth(), self.winfo_screenheight()
        self.size = max(px(160), min(px(512), (width * 9 // 10 - px(110)) // 2, height * 45 // 100))
        self.shown = self.size  # the pictures' side once _fit made them smaller (self.size is in the request)
        name = " · ".join(x for x in (item.name, PART_LABELS.get(part, "")) if x)
        ttk.Label(self.body, text=name, style="DialogTitle.TLabel").pack(anchor="w")
        ttk.Label(self.body, text=use.name + ("" if use.enabled else "  (꺼짐)"), style="Muted.TLabel").pack(
            anchor="w", pady=(px(2), 0))
        pictures = ttk.Frame(self.body)
        pictures.pack(anchor="w", pady=(px(14), 0))
        self.before_label = self._box(pictures, "원래 모습", (0, px(12)))
        self.after_label = self._box(pictures, "적용 후", (0, 0))
        ttk.Label(self.body, text=LOOK_NOTE, style="Muted.TLabel", font=t.fonts["small"]).pack(
            anchor="w", pady=(px(10), 0))
        holder = ttk.Frame(self.body)  # the notes, swatches and chips; they scroll when the screen is too low
        holder.pack(fill="x")
        self.extra_canvas, self.extra = window._scroller(holder, (0, 0), plain=True)
        row = ttk.Frame(self.body)
        row.pack(fill="x", pady=(px(16), 0))
        ttk.Button(row, text="닫기", command=self.cancel).pack(side="right")
        self.bind("<Return>", lambda e: self.cancel())
        self.request = window._want_look(job, item.target.with_part(part), self.size, True, self._ready, urgent=True)

    def _box(self, parent, title: str, padx) -> ttk.Label:
        t, px = self.theme, self.theme.px
        card = t.mark(tk.Frame(parent), "card")
        card.pack(side="left", padx=padx)
        ttk.Label(card, text=title, style="CardMuted.TLabel", font=t.fonts["small_bold"]).pack(
            anchor="w", padx=px(12), pady=(px(10), 0))
        frame = ttk.Frame(card, style="Card.TFrame", width=self.size, height=self.size)
        frame.pack_propagate(False)
        frame.pack(padx=px(12), pady=(px(6), px(12)))
        label = ttk.Label(frame, text="그리는 중...", style="CardMuted.TLabel", anchor="center", justify="center",
                          wraplength=self.size - px(40))
        label.pack(fill="both", expand=True)
        return label

    def _image(self, path: Path | None, size: int):
        image = load_image(path, size, size) if path is not None else None
        if image is not None:
            self.images.append(image)
        return image

    def _ready(self, preview):
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        self.preview = preview
        after, before = self._image(preview.after, self.size), self._image(preview.before, self.size)
        self.after_label.configure(image=after or "", text="" if after else (preview.text or "그림이 없습니다"))
        self.before_label.configure(image=before or "", text="" if before else "원래 모습을 그릴 수 없습니다")
        self._extras(preview)
        self.window._bind_wheel(self.extra, self.extra_canvas)
        self._fit(preview)
        self._keep_on_screen()

    def _room(self) -> int:
        """The height the dialog may take: the screen less the taskbar and the title bar, or the window
        manager's limit (on Windows the work area) when that is less."""
        px = self.theme.px
        return min(self.winfo_screenheight() - px(80), self.wm_maxsize()[1] - px(16))

    def _fit(self, preview):
        """Made to fit the screen once its content is known: the pictures smaller (down to MIN_LOOK), then
        what is below them scrolling, then the pictures smaller still."""
        px = self.theme.px
        self.update_idletasks()
        need = self.extra.winfo_reqheight()
        self.extra_canvas.configure(width=max(1, self.extra.winfo_reqwidth()), height=max(1, need))
        room = self._room()

        def over() -> int:
            self.update_idletasks()
            return self.winfo_reqheight() - room

        if over() > 0:
            self._resize(preview, max(min(self.shown, px(self.MIN_LOOK)), self.shown - over()))
        if over() > 0 and need > px(80):
            self.extra_canvas.configure(height=max(px(80), need - over()))
        if over() > 0:
            self._resize(preview, max(px(120), self.shown - over()))

    def _resize(self, preview, size: int):
        """Both pictures made ``size`` square (only ever smaller)."""
        if size >= self.shown:
            return
        self.shown = size
        for label, path in ((self.after_label, preview.after), (self.before_label, preview.before)):
            label.master.configure(width=size, height=size)  # load_image shrinks the drawn PNG
            image = self._image(path, size)
            label.configure(wraplength=size - self.theme.px(40), **({"image": image} if image is not None else {}))

    def _extras(self, p):
        """The notes, the textures the mod replaces (game -> mod) and its changed colour values."""
        t, px = self.theme, self.theme.px
        wrap = 2 * self.size + px(60)
        for note in (n for n in p.notes if n != p.text):  # the status is in the picture's place
            ttk.Label(self.extra, text=f"· {note}", style="Muted.TLabel", font=t.fonts["small"], wraplength=wrap,
                      justify="left").pack(anchor="w", pady=(px(3), 0))
        side, small = px(48), t.fonts["small"]
        if p.swatches:  # in a grid across the dialog (a list of six would reach below the screen)
            grid = self._grid("텍스처 (게임 → 모드)")
            columns = max(1, wrap // px(190))
            for i, swatch in enumerate(p.swatches):
                cell = ttk.Frame(grid)
                cell.grid(row=i // columns, column=i % columns, sticky="nw", padx=(0, px(18)), pady=(px(6), 0))
                pair = ttk.Frame(cell)
                pair.pack(anchor="w")
                self._swatch(pair, swatch.before, side, "새 텍스처" if swatch.new else "?")
                ttk.Label(pair, text="→", style="Muted.TLabel").pack(side="left", padx=px(6))
                self._swatch(pair, swatch.after, side, "")
                name = swatch.name[:-4] if swatch.name.lower().endswith(".tex") else swatch.name
                ttk.Label(cell, text=name, style="Muted.TLabel", font=small, wraplength=px(180),
                          justify="left").pack(anchor="w", pady=(px(2), 0))
        if p.chips:
            grid = self._grid("색 값 (재질 파일, 게임 → 모드)")
            columns = max(1, wrap // px(270))
            for i, chip in enumerate(p.chips):
                cell = ttk.Frame(grid)
                cell.grid(row=i // columns, column=i % columns, sticky="w", padx=(0, px(18)), pady=(px(5), 0))
                self._colour(cell, chip.before, chip.before_on)
                ttk.Label(cell, text="→", style="Muted.TLabel").pack(side="left", padx=px(6))
                self._colour(cell, chip.after, chip.after_on)
                text = f"{chip.material} · {equip_look.COLOR_LABELS.get(chip.name, chip.name)}"
                ttk.Label(cell, text=text + ("" if chip.after_on else " (꺼짐)"), style="Muted.TLabel",
                          font=small).pack(side="left", padx=(px(8), 0))

    def _grid(self, title: str) -> ttk.Frame:
        ttk.Label(self.extra, text=title, style="DialogSection.TLabel").pack(anchor="w", pady=(self.theme.px(12), 0))
        grid = ttk.Frame(self.extra)
        grid.pack(anchor="w")
        return grid

    def _swatch(self, parent, path: Path | None, side: int, empty: str):
        image = self._image(path, side)
        frame = ttk.Frame(parent, style="Thumb.TFrame", width=side, height=side)
        frame.pack_propagate(False)
        frame.pack(side="left")
        ttk.Label(frame, image=image or "", text="" if image else empty, style="Thumb.TLabel", anchor="center",
                  justify="center", wraplength=side).pack(fill="both", expand=True)

    def _colour(self, parent, values, on: bool):
        """A square of a colour value (0..1 RGB); "?" when the game's value is unknown."""
        px, p = self.theme.px, self.theme.p
        colour = equip_look.Chip.hex(values)
        if colour is None:
            ttk.Label(parent, text="?", style="Muted.TLabel", width=2, anchor="center").pack(side="left")
            return
        tk.Frame(parent, width=px(18), height=px(18), bg=colour, highlightthickness=1,
                 highlightbackground=p["muted"] if on else p["border"]).pack(side="left")

    def _on_screen(self, y: int) -> int:
        """``y`` moved up so that the dialog ends above the bottom of the screen (taskbar and title bar);
        a dialog on another monitor stays where it is."""
        screen = self.winfo_screenheight()
        bottom = screen - self.theme.px(80)
        if 0 <= y < screen and y + self.winfo_reqheight() > bottom:
            return max(0, bottom - self.winfo_reqheight())
        return y

    def _position(self):
        x, y = super()._position()
        return x, self._on_screen(y)  # a kept preview fills the dialog before it is shown

    def _keep_on_screen(self):
        """Grown by the notes and swatches after it was shown: moved up when it would reach below the screen."""
        self.update_idletasks()
        if not self.winfo_viewable():
            return  # not shown yet: show() places it
        y = self._on_screen(self.winfo_y())
        if y != self.winfo_y():
            self.geometry(f"+{self.winfo_x()}+{y}")

    def cancel(self):
        self.window.cancel_look(self.request)
        super().cancel()

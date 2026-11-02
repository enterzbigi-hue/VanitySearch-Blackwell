"""
Okienkowa wersja szukacza Bitcoin puzzle (wszystkie nierozwiązane zagadki 71-160), 10 języków do wyboru.

- Zagadka nr N: klucz leży w zakresie 2^(N-1) ... 2^N - 1, adres pochodzi z oryginalnej transakcji (puzzle_list.py).
- Losuje paczki 2^bits kluczy w zakresie wybranej zagadki i uruchamia na nich VanitySearch (bez okna konsoli).
- RÓWNOLEGLE co kilka minut sprawdza saldo adresu celu: własny węzeł, blockchain.info albo mempool.space.
  Gdy adres jest pusty (albo jego środki są właśnie wydawane w mempoolu), przerywa szukanie.
- Każda zagadka ma własną bazę sprawdzonych paczek; puzzle 71 używa dotychczasowego checked_ranges.json.

Uruchomienie: dwuklik na pliku albo  pythonw szukacz_puzzle_gui.pyw [--start]
"""

import json
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)
import jezyki                  # noqa: E402  (tłumaczenia okna)
import puzzle_list as pz       # noqa: E402  (adresy i zakresy zagadek)
import szukacz_puzzle as core  # noqa: E402  (baza zakresów, losowanie paczek)

SETTINGS_FILE = "szukacz_gui.json"
DEFAULTS = {
    "exe": core.EXE_PATH,
    "lang": "pl",
    "puzzle": 71,
    "unsolved": pz.UNSOLVED_AT_RELEASE,
    "source": "node",           # node | blockchain | mempool
    "interval_min": 10,
    "bits": core.RANGE_BITS,    # rozmiar paczki: 2^bits kluczy
    "bitcoin_cli": core.BITCOIN_CLI,
    "datadir": core.BITCOIN_DATADIR,
}
SOURCE_KEYS = ["node", "blockchain", "mempool"]
MIN_BITS, MAX_BITS = 32, 46
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
MAX_FAILS_IN_ROW = 3

PROGRESS_RE = re.compile(r"([\d.]+) MK/s - (\d+) BKeys - 2\^([\d.]+) \[([\d.]+)%\]")
END_RE = re.compile(r"END: (\d\d:\d\d:\d\d)")

# --- kolory ---
BG, CARD, BORDER = "#0f1115", "#171a21", "#262b36"
TEXT, MUTED = "#e6e8ee", "#8b93a7"
ACCENT, GREEN, RED, YELLOW = "#f7931a", "#3fb950", "#f85149", "#d29922"
FONT = "Segoe UI"


# ---------------------------------------------------------------- saldo
def _http_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "szukacz-puzzle/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def balances_node(addrs, cli, datadir):
    """Saldo każdego adresu z UTXO własnego węzła (scantxoutset, działa też z prune)."""
    if not os.path.exists(cli):
        raise RuntimeError(f"{cli}?")
    desc = json.dumps([f"addr({a})" for a in addrs])
    r = subprocess.run([cli, f"-datadir={datadir}", "scantxoutset", "start", desc],
                       capture_output=True, text=True, timeout=1800, creationflags=NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or f"exit {r.returncode}")
    out = dict.fromkeys(addrs, 0.0)
    for u in json.loads(r.stdout)["unspents"]:
        a = re.search(r"addr\(([^)]+)\)", u["desc"]).group(1)
        out[a] = out.get(a, 0.0) + float(u["amount"])
    return out


def balances_blockchain(addrs):
    data = _http_json("https://blockchain.info/balance?active=" + "|".join(addrs))
    return {a: data[a]["final_balance"] / 1e8 for a in addrs}


def balance_mempool(addr):
    """(potwierdzone, zmiana w mempoolu) dla jednego adresu."""
    d = _http_json(f"https://mempool.space/api/address/{addr}")
    c, m = d["chain_stats"], d["mempool_stats"]
    return (c["funded_txo_sum"] - c["spent_txo_sum"]) / 1e8, (m["funded_txo_sum"] - m["spent_txo_sum"]) / 1e8


# ---------------------------------------------------------------- aplikacja
class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.settings = self._load_settings()
        self.snap = dict(self.settings)
        self.stop_evt = threading.Event()
        self.balance_lock = threading.Lock()
        self.proc = None
        self.running = False
        self.chunk_t0 = None
        self.last_speed = None

        root.configure(bg=BG)
        root.geometry("960x810")
        root.minsize(840, 680)
        self._styles()
        self._build()
        self._apply_puzzle()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(150, self._poll)

    def t(self, key, **kw):
        return jezyki.tr(self.settings["lang"], key, **kw)

    def src_name(self, src):
        return self.t("src_node") if src == "node" else {"blockchain": "blockchain.info", "mempool": "mempool.space"}[src]

    def puzzle_label(self, n):
        return f"#{n}   ·   2^{n - 1} … 2^{n}−1" + (f"   ·   {self.t('pubkey_tag')}" if pz.pubkey_known(n) else "")

    # ---------- ustawienia
    def _load_settings(self):
        s = dict(DEFAULTS)
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                s.update(json.load(f))
        except (OSError, ValueError):
            pass
        s.pop("targets", None)        # ze starszej wersji (plik adresów zastąpiony listą zagadek)
        if s["lang"] not in jezyki.T:
            s["lang"] = "pl"
        return s

    def _save_settings(self):
        """Zapisuje ustawienia i odświeża ich kopię czytaną przez wątki (tkinter nie jest wielowątkowy)."""
        self.settings.update(exe=self.v_exe.get(), source=self.v_source.get(),
                             interval_min=self._interval(), bits=int(self.v_bits.get()))
        self.snap = dict(self.settings)
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.settings, f, indent=2, ensure_ascii=False)
        except OSError:
            pass

    def _interval(self):
        try:
            return max(1, int(self.v_interval.get()))
        except (ValueError, tk.TclError):
            return DEFAULTS["interval_min"]

    @property
    def puzzle(self):
        return int(self.settings["puzzle"])

    # ---------- wygląd
    def _styles(self):
        st = ttk.Style(self.root)
        st.theme_use("clam")
        st.configure("A.Horizontal.TProgressbar", troughcolor=BORDER, background=ACCENT,
                     bordercolor=BORDER, lightcolor=ACCENT, darkcolor=ACCENT, thickness=10)
        st.configure("D.TCombobox", fieldbackground=BG, background=BORDER, foreground=TEXT, arrowcolor=TEXT,
                     bordercolor=BORDER, lightcolor=BG, darkcolor=BG, padding=4)
        st.map("D.TCombobox", fieldbackground=[("readonly", BG)], foreground=[("readonly", TEXT)],
               selectbackground=[("readonly", BG)], selectforeground=[("readonly", TEXT)])
        self.root.option_add("*TCombobox*Listbox.background", CARD)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#111")
        self.root.option_add("*TCombobox*Listbox.font", (FONT, 10))

    def _card(self, parent, title):
        outer = tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        tk.Label(outer, text=title, bg=CARD, fg=MUTED, font=(FONT, 9, "bold")).pack(anchor="w", padx=14, pady=(10, 0))
        inner = tk.Frame(outer, bg=CARD)
        inner.pack(fill="both", expand=True, padx=14, pady=(4, 12))
        return outer, inner

    def _label(self, parent, text="", size=10, color=TEXT, bold=False, mono=False, **kw):
        font = ("Consolas" if mono else FONT, size, "bold" if bold else "normal")
        return tk.Label(parent, text=text, bg=CARD, fg=color, font=font, anchor="w", **kw)

    def _button(self, parent, text, cmd, primary=False):
        b = tk.Button(parent, text=text, command=cmd, relief="flat", bd=0, cursor="hand2",
                      font=(FONT, 10, "bold" if primary else "normal"), padx=16, pady=7,
                      bg=ACCENT if primary else BORDER, fg="#111" if primary else TEXT,
                      activebackground="#ffad42" if primary else "#333949",
                      activeforeground="#111" if primary else TEXT)
        return b

    def _build(self):
        t = self.t
        # pasek języka
        lang_bar = tk.Frame(self.root, bg=BG)
        lang_bar.pack(fill="x", padx=18, pady=(10, 0))
        self.v_lang = tk.StringVar(value=dict(jezyki.LANGS)[self.settings["lang"]])
        cb_lang = ttk.Combobox(lang_bar, textvariable=self.v_lang, state="readonly", style="D.TCombobox",
                               values=[name for _, name in jezyki.LANGS], font=(FONT, 9), width=12)
        cb_lang.pack(side="right")
        cb_lang.bind("<<ComboboxSelected>>", self._on_lang)
        tk.Label(lang_bar, text="🌐  " + t("language"), bg=BG, fg=MUTED, font=(FONT, 9)).pack(side="right", padx=6)

        # nagłówek
        top = tk.Frame(self.root, bg=BG)
        top.pack(fill="x", padx=18, pady=(4, 8))
        self.l_title = tk.Label(top, text="", bg=BG, fg=TEXT, font=(FONT, 18, "bold"))
        self.l_title.pack(side="left")
        self.pill = tk.Label(top, text="", bg=BORDER, fg=TEXT, font=(FONT, 9, "bold"), padx=6, pady=3)
        self.pill.pack(side="left", padx=12)
        self.btn_stop = self._button(top, t("btn_stop"), self.stop)
        self.btn_stop.pack(side="right")
        self.btn_start = self._button(top, t("btn_start"), self.start, primary=True)
        self.btn_start.pack(side="right", padx=8)

        # wybór zagadki
        pc, pb = self._card(self.root, t("card_puzzle"))
        pc.pack(fill="x", padx=18, pady=(0, 6))
        prow = tk.Frame(pb, bg=CARD)
        prow.pack(fill="x")
        self.v_puzzle = tk.StringVar()
        self.cb_puzzle = ttk.Combobox(prow, textvariable=self.v_puzzle, state="readonly", style="D.TCombobox",
                                      font=(FONT, 10), width=52, height=20)
        self.cb_puzzle.pack(side="left")
        self.cb_puzzle.bind("<<ComboboxSelected>>", self._on_puzzle)
        self.btn_list = self._button(prow, t("btn_refresh"), self.refresh_list)
        self.btn_list.configure(pady=4)
        self.btn_list.pack(side="left", padx=8)
        self.l_list_info = self._label(prow, "", 9, MUTED)
        self.l_list_info.pack(side="left", padx=4)
        self.l_hint = self._label(pb, "", 9, YELLOW, wraplength=880, justify="left")
        self.l_hint.pack(fill="x", pady=(4, 0))

        grid = tk.Frame(self.root, bg=BG)
        grid.pack(fill="x", padx=18)
        grid.columnconfigure(0, weight=1, uniform="c")
        grid.columnconfigure(1, weight=1, uniform="c")

        # --- karta: cel i saldo
        c1, b1 = self._card(grid, t("card_target"))
        c1.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=6)
        self.l_addr = self._label(b1, "-", 10, mono=True)
        self.l_addr.pack(fill="x")
        self.l_balance = self._label(b1, "— BTC", 22, ACCENT, bold=True)
        self.l_balance.pack(fill="x", pady=(6, 0))
        self.l_bal_info = self._label(b1, t("bal_unknown"), 9, MUTED)
        self.l_bal_info.pack(fill="x")
        row = tk.Frame(b1, bg=CARD)
        row.pack(fill="x", pady=(10, 0))
        self.btn_check = self._button(row, t("btn_check"), self.check_now)
        self.btn_check.pack(side="left")
        self.l_next = self._label(row, "", 9, MUTED)
        self.l_next.pack(side="left", padx=10)

        # --- karta: postęp
        c2, b2 = self._card(grid, t("card_chunk"))
        c2.grid(row=0, column=1, sticky="nsew", padx=(6, 0), pady=6)
        self.l_range = self._label(b2, "", 10, mono=True, justify="left")
        self.l_range.pack(fill="x")
        self.pb = ttk.Progressbar(b2, style="A.Horizontal.TProgressbar", maximum=100)
        self.pb.pack(fill="x", pady=(10, 4))
        self.l_prog = self._label(b2, "0.00 %", 9, MUTED)
        self.l_prog.pack(fill="x")
        stats = tk.Frame(b2, bg=CARD)
        stats.pack(fill="x", pady=(8, 0))
        self.l_speed = self._stat(stats, t("speed"), "—", 0)
        self.l_eta = self._stat(stats, t("eta"), "—", 1)

        # --- karta: statystyki
        c3, b3 = self._card(grid, t("card_stats"))
        c3.grid(row=1, column=0, sticky="nsew", padx=(0, 6), pady=6)
        st = tk.Frame(b3, bg=CARD)
        st.pack(fill="x")
        self.l_chunks = self._stat(st, t("chunks"), "0", 0)
        self.l_keys = self._stat(st, t("keys"), "0", 1)
        self.l_share = self._stat(st, t("share"), "0 %", 2)
        self.l_zakres = self._label(b3, "", 9, MUTED, wraplength=420, justify="left")
        self.l_zakres.pack(fill="x", pady=(8, 0))

        # --- karta: ustawienia
        c4, b4 = self._card(grid, t("card_settings"))
        c4.grid(row=1, column=1, sticky="nsew", padx=(6, 0), pady=6)
        self._label(b4, t("source"), 9, MUTED).pack(fill="x")
        srow = tk.Frame(b4, bg=CARD)
        srow.pack(fill="x", pady=(2, 6))
        self.v_source = tk.StringVar(value=self.settings["source"])
        for key in SOURCE_KEYS:
            tk.Radiobutton(srow, text=self.src_name(key), value=key, variable=self.v_source,
                           command=self._save_settings, bg=CARD, fg=TEXT, selectcolor=BG, activebackground=CARD,
                           activeforeground=TEXT, font=(FONT, 10), highlightthickness=0, bd=0
                           ).pack(side="left", padx=(0, 12))
        irow = tk.Frame(b4, bg=CARD)
        irow.pack(fill="x", pady=(0, 6))
        self._label(irow, t("every"), 10).pack(side="left")
        self.v_interval = tk.StringVar(value=str(self.settings["interval_min"]))
        tk.Spinbox(irow, from_=1, to=240, width=5, textvariable=self.v_interval, font=(FONT, 10),
                   bg=BG, fg=TEXT, buttonbackground=BORDER, insertbackground=TEXT, relief="flat",
                   highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT
                   ).pack(side="left", padx=6, ipady=2)
        self._label(irow, t("min"), 10).pack(side="left")
        self.v_interval.trace_add("write", lambda *_: self._save_settings())
        brow = tk.Frame(b4, bg=CARD)
        brow.pack(fill="x", pady=(0, 6))
        self._label(brow, t("chunk_size"), 10).pack(side="left")
        self.v_bits = tk.StringVar(value=str(self.settings["bits"]))
        self.sp_bits = tk.Spinbox(brow, values=[str(b) for b in range(MIN_BITS, MAX_BITS + 1)], width=4,
                                  textvariable=self.v_bits, font=(FONT, 10), state="readonly",
                                  readonlybackground=BG, fg=TEXT, buttonbackground=BORDER, relief="flat",
                                  highlightthickness=1, highlightbackground=BORDER, command=self._on_bits)
        self.sp_bits.pack(side="left", padx=6, ipady=2)
        self.v_bits.set(str(self.settings["bits"]))
        self.l_bits_info = self._label(brow, "", 9, MUTED)
        self.l_bits_info.pack(side="left")
        self.v_exe = tk.StringVar(value=self.settings["exe"])
        self._path_row(b4, "VanitySearch", self.v_exe, [("VanitySearch", "*.exe")])

        # --- dziennik
        lc, lb = self._card(self.root, t("card_log"))
        lc.pack(fill="both", expand=True, padx=18, pady=(6, 16))
        self.log_box = tk.Text(lb, bg=BG, fg=TEXT, insertbackground=TEXT, relief="flat", font=("Consolas", 9),
                               height=8, wrap="word", highlightthickness=0, padx=8, pady=6)
        sb = tk.Scrollbar(lb, command=self.log_box.yview, bg=CARD, troughcolor=BG, relief="flat", bd=0)
        self.log_box.configure(yscrollcommand=sb.set, state="disabled")
        sb.pack(side="right", fill="y")
        self.log_box.pack(fill="both", expand=True)
        for tag, col in (("ok", GREEN), ("err", RED), ("warn", YELLOW), ("muted", MUTED), ("acc", ACCENT)):
            self.log_box.tag_configure(tag, foreground=col)
        self._set_state("idle")

    def _stat(self, parent, title, value, col):
        f = tk.Frame(parent, bg=CARD)
        f.grid(row=0, column=col, sticky="w", padx=(0, 28))
        self._label(f, title, 9, MUTED).pack(anchor="w")
        v = self._label(f, value, 14, bold=True)
        v.pack(anchor="w")
        return v

    def _path_row(self, parent, title, var, types, after=None):
        r = tk.Frame(parent, bg=CARD)
        r.pack(fill="x", pady=2)
        self._label(r, title, 9, MUTED, width=12).pack(side="left")
        e = tk.Entry(r, textvariable=var, bg=BG, fg=TEXT, insertbackground=TEXT, relief="flat",
                     font=(FONT, 9), highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT)
        e.pack(side="left", fill="x", expand=True, ipady=3)

        def browse():
            p = filedialog.askopenfilename(initialdir=HERE, filetypes=types)
            if p:
                var.set(os.path.relpath(p, HERE) if p.lower().startswith(HERE.lower()) else p)
                self._save_settings()
                if after:
                    after()
        b = self._button(r, "…", browse)
        b.configure(padx=8, pady=2)
        b.pack(side="left", padx=(6, 0))

    # ---------- pomocnicze
    def log(self, msg, tag=None):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", datetime.now().strftime("%H:%M:%S  "), "muted")
        self.log_box.insert("end", msg + "\n", tag)
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def post(self, kind, *args):
        self.q.put((kind, args))

    def _on_lang(self, _event=None):
        code = {name: c for c, name in jezyki.LANGS}[self.v_lang.get()]
        if code == self.settings["lang"]:
            return
        if self.running:
            self.v_lang.set(dict(jezyki.LANGS)[self.settings["lang"]])
            messagebox.showinfo(self.t("busy_title"), self.t("busy_lang"))
            return
        self._save_settings()
        self.settings["lang"] = code
        for w in self.root.winfo_children():   # przebudowa okna w nowym języku
            w.destroy()
        self._build()
        self._apply_puzzle()

    def _fill_puzzle_list(self):
        unsolved = sorted(set(self.settings["unsolved"]) | {self.puzzle})
        self.cb_puzzle["values"] = [self.puzzle_label(n) for n in unsolved]
        self.v_puzzle.set(self.puzzle_label(self.puzzle))
        self.l_list_info.configure(text=self.t("n_unsolved", n=len(self.settings["unsolved"])))

    def _apply_puzzle(self):
        """Ustawia okno pod wybraną zagadkę: tytuł, adres, zakres, limit rozmiaru paczki, statystyki."""
        n = self.puzzle
        self._fill_puzzle_list()
        self.root.title(self.t("title", n=n))
        self.l_title.configure(text=self.t("title", n=n))
        self.l_addr.configure(text=pz.ADDRESSES[n], fg=TEXT)
        self.l_balance.configure(text="— BTC", fg=ACCENT)
        self.l_bal_info.configure(text=self.t("bal_unknown"), fg=MUTED)
        self.l_zakres.configure(text=self.t("range_info", n=n, a=n - 1, b=n, db=os.path.basename(core.db_file_for(n))))
        self.l_hint.configure(text=self.t("hint_pubkey") if pz.pubkey_known(n) else "", fg=YELLOW)
        top = min(MAX_BITS, n - 1)
        cur = int(self.v_bits.get())
        self.sp_bits.configure(values=[str(b) for b in range(MIN_BITS, top + 1)])   # Tk ustawia wtedy 1. wartość
        self.v_bits.set(str(min(cur, top)))
        self.l_range.configure(text=f"{self.t('from')}: -\n{self.t('to')}: -")
        self.pb["value"] = 0
        self.l_prog.configure(text="0.00 %")
        self.l_eta.configure(text="—")
        self._refresh_db_count()
        self._save_settings()
        self._update_bits_info()
        if not self.running:
            self._set_state("idle")

    def _on_puzzle(self, _event=None):
        n = int(self.v_puzzle.get().split()[0].lstrip("#"))
        if n == self.puzzle:
            return
        if self.running:
            self.v_puzzle.set(self.puzzle_label(self.puzzle))
            messagebox.showinfo(self.t("busy_title"), self.t("busy_switch"))
            return
        self.settings["puzzle"] = n
        self._apply_puzzle()
        self.log(self.t("log_picked", n=n, a=n - 1, b=n, addr=pz.ADDRESSES[n]), "acc")

    def _refresh_db_count(self, checked=None):
        if checked is None:
            try:
                checked = core.load_db(core.db_file_for(self.puzzle))
            except SystemExit:
                checked = set()
        keys = core.checked_keys(checked)
        self.l_chunks.configure(text=f"{len(checked):,}".replace(",", " "))
        self.l_keys.configure(text=f"2^{math.log2(keys):.2f}" if keys else "0")
        share = keys / (1 << (self.puzzle - 1)) * 100
        self.l_share.configure(text=f"{share:.6f} %" if share >= 1e-4 or not keys else f"{share:.2e} %")

    def _on_bits(self):
        self._save_settings()
        self._update_bits_info()
        if self.running:
            self.log(self.t("log_new_bits", b=self.v_bits.get()), "acc")

    def _update_bits_info(self):
        size = 1 << int(self.v_bits.get())
        if self.last_speed:
            secs = size / self.last_speed
            d = f"{secs:.0f} s" if secs < 120 else f"{secs / 60:.0f} min" if secs < 7200 else f"{secs / 3600:.1f} h"
            self.l_bits_info.configure(text=self.t("per_chunk", t=d))
        else:
            self.l_bits_info.configure(text=self.t("n_keys", n=f"{size:,}".replace(",", " ")))

    def _set_state(self, state):
        look = {"idle": ("st_idle", BORDER, TEXT), "run": ("st_run", GREEN, "#0b1f10"),
                "solved": ("st_solved", RED, "#fff"), "found": ("st_found", ACCENT, "#111")}
        key, bg, fg = look[state]
        self.state = state
        self.pill.configure(text=f"  {self.t(key)}  ", bg=bg, fg=fg)
        self.btn_start.configure(state="disabled" if self.running else "normal",
                                 bg=BORDER if self.running else ACCENT, disabledforeground=MUTED)
        self.btn_stop.configure(state="normal" if self.running else "disabled",
                                bg=RED if self.running else BORDER, disabledforeground=MUTED)
        self.cb_puzzle.configure(state="disabled" if self.running else "readonly")

    # ---------- start / stop
    def start(self):
        if self.running:
            return
        self._save_settings()
        if not os.path.exists(self.v_exe.get()):
            messagebox.showerror(self.t("no_exe_title"), self.t("no_exe", p=self.v_exe.get()))
            return
        self.running = True
        # Nowe zdarzenie na każdy start: wątki z poprzedniego startu na pewno się zakończą
        ev = self.stop_evt = threading.Event()
        self._set_state("run")
        n = self.puzzle
        self.log(self.t("log_start", n=n, m=self._interval(), src=self.src_name(self.v_source.get())), "acc")
        lo, hi = pz.key_range(n)
        cfg = dict(self.snap, addr=pz.ADDRESSES[n], lo=lo, hi=hi, db=core.db_file_for(n), n=n)
        threading.Thread(target=self._search_loop, args=(ev, cfg), daemon=True).start()
        threading.Thread(target=self._balance_loop, args=(ev,), daemon=True).start()

    def stop(self, reason=None):
        if not self.running:
            return
        self.stop_evt.set()
        p = self.proc
        if p and p.poll() is None:
            p.terminate()
        self.running = False
        self._set_state("idle")
        self.l_next.configure(text="")
        self.log(reason or self.t("log_stopped"), "warn")

    def _on_close(self):
        if self.running and not messagebox.askyesno(self.t("close_title"), self.t("close_q")):
            return
        self.stop()
        self._save_settings()
        self.root.after(300, self.root.destroy)

    # ---------- wątek szukania
    def _search_loop(self, ev, cfg):
        t = lambda key, **kw: jezyki.tr(cfg["lang"], key, **kw)  # noqa: E731
        exe = os.path.abspath(cfg["exe"])
        out = os.path.abspath(core.OUTPUT_FILE)
        try:
            checked = core.load_db(cfg["db"])
        except SystemExit:
            self.post("fatal", t("db_err", db=cfg["db"]))
            return
        fails = 0
        while not ev.is_set():
            bits = min(self.snap["bits"], cfg["n"] - 1)    # zmiana rozmiaru działa od następnej paczki
            try:
                start = core.get_random_start(checked, bits, cfg["lo"], cfg["hi"])
            except Exception:  # noqa: BLE001  (cały zakres sprawdzony)
                self.post("fatal", t("all_done"))
                return
            self.post("chunk", start, bits)
            size0 = os.path.getsize(out) if os.path.exists(out) else 0
            cmd = [exe, "-o", out, "-start", f"{start:X}", "-range", str(bits), "-m", "1048576", cfg["addr"]]
            t0 = time.time()
            try:
                self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                             creationflags=NO_WINDOW, cwd=os.path.dirname(exe))
            except OSError as e:
                self.post("fatal", t("run_err", e=e))
                return
            hit = self._read_output(self.proc)
            ret = self.proc.wait()
            elapsed = time.time() - t0

            if hit or (os.path.exists(out) and os.path.getsize(out) > size0):
                self.post("found", out)
            if ev.is_set():
                break
            if ret != 0:
                fails += 1
                self.post("log", t("exit_err", r=ret, h=f"{ret & 0xFFFFFFFF:X}", s=f"{elapsed:.0f}"), "err")
                if fails >= MAX_FAILS_IN_ROW:
                    self.post("fatal", t("too_many", f=fails))
                    return
                ev.wait(10)
                continue
            fails = 0
            checked.add((start, bits))
            core.save_db(checked, cfg["db"])
            self.post("done", start, bits, elapsed, set(checked))

    def _read_output(self, proc):
        """Czyta wyjście VanitySearch: linie z \\r to postęp, z \\n trafiają do dziennika."""
        buf, hit = "", False
        fd = proc.stdout.fileno()
        while True:
            data = os.read(fd, 4096)
            if not data:
                break
            buf += data.decode("utf-8", "replace")
            while True:
                m = re.search(r"[\r\n]", buf)
                if not m:
                    break
                line, sep, buf = buf[:m.start()].strip(), m.group(), buf[m.end():]
                if not line:
                    continue
                p = PROGRESS_RE.search(line)
                if p:
                    end = END_RE.search(line)
                    self.post("progress", float(p.group(1)), float(p.group(4)), end and end.group(1))
                    continue
                if "Priv" in line or "Public Addr" in line:
                    hit = True          # klucz idzie tylko do pliku, nie do dziennika
                    continue
                if sep == "\n" and not line.startswith(("Setting starting keys", "GPU Started")):
                    self.post("log", line, "muted")
        return hit

    # ---------- wątek salda
    def _balance_loop(self, ev):
        while not ev.is_set():
            self._check_balance()
            nxt = time.time() + self.snap["interval_min"] * 60
            self.post("next", nxt)
            while not ev.is_set() and time.time() < nxt:
                ev.wait(1)

    def check_now(self):
        self._save_settings()
        threading.Thread(target=self._check_balance, daemon=True).start()

    def _check_balance(self):
        if not self.balance_lock.acquire(blocking=False):
            return
        try:
            snap = self.snap
            n, src = int(snap["puzzle"]), snap["source"]
            addr = pz.ADDRESSES[n]
            self.post("bal_start", src)
            t0 = time.time()
            try:
                if src == "node":
                    conf, pend = balances_node([addr], snap["bitcoin_cli"], snap["datadir"])[addr], None
                elif src == "blockchain":
                    conf, pend = balances_blockchain([addr])[addr], None
                else:
                    conf, pend = balance_mempool(addr)
            except Exception as e:  # noqa: BLE001  (sieć, węzeł, zły JSON - wszystko tylko raportujemy)
                self.post("bal_err", src, str(e))
                return
            self.post("balance", n, src, conf, pend, time.time() - t0)
        finally:
            self.balance_lock.release()

    def refresh_list(self):
        """Sprawdza saldo wszystkich 160 adresów i zostawia na liście tylko zagadki ze środkami."""
        self._save_settings()
        self.btn_list.configure(state="disabled", text=self.t("checking"))
        threading.Thread(target=self._refresh_list_worker, args=(dict(self.snap),), daemon=True).start()

    def _refresh_list_worker(self, snap):
        addrs = list(pz.ADDRESSES.values())
        try:
            if snap["source"] == "node":
                bal, src = balances_node(addrs, snap["bitcoin_cli"], snap["datadir"]), "node"
            else:   # mempool.space nie ma zapytania o wiele adresów naraz - lista zawsze z blockchain.info
                bal, src = balances_blockchain(addrs), "blockchain"
        except Exception as e:  # noqa: BLE001
            self.post("list_err", str(e))
            return
        self.post("list", [n for n, a in pz.ADDRESSES.items() if bal.get(a, 0) > 0], src)

    # ---------- obsługa zdarzeń w wątku okna
    def _poll(self):
        try:
            while True:
                kind, a = self.q.get_nowait()
                getattr(self, "_ev_" + kind)(*a)
        except queue.Empty:
            pass
        except tk.TclError:
            pass                    # zdarzenie dotarło w trakcie przebudowy okna
        self.root.after(150, self._poll)

    def _ev_log(self, msg, tag=None):
        self.log(msg, tag)

    def _ev_fatal(self, msg):
        self.log(msg, "err")
        self.stop(self.t("stopped_err"))
        messagebox.showerror(self.t("err_title"), msg)

    def _ev_chunk(self, start, bits):
        self.chunk_t0 = time.time()
        self.l_range.configure(text=f"{self.t('from')}: {start:X}\n{self.t('to')}: {start + (1 << bits) - 1:X}   (2^{bits})")
        self.pb["value"] = 0
        self.l_prog.configure(text="0.00 %   ·   " + self.t("setting_keys"))
        self.l_eta.configure(text="—")
        self.log(self.t("log_chunk", b=bits, s=f"{start:X}"))

    def _ev_progress(self, mks, perc, end):
        self.pb["value"] = min(perc, 100)
        self.l_prog.configure(text=f"{perc:.2f} %")
        self.l_speed.configure(text=f"{mks / 1000:.2f} GK/s")
        if end:
            self.l_eta.configure(text=end)

    def _ev_done(self, start, bits, elapsed, checked):
        self.pb["value"] = 100
        self.l_prog.configure(text="100 %")
        self._refresh_db_count(checked)
        speed = (1 << bits) / elapsed
        self.last_speed = speed
        self._update_bits_info()
        self.log(self.t("log_done", b=bits, s=f"{start:X}", m=f"{elapsed / 60:.1f}", g=f"{speed / 1e9:.2f}"), "ok")

    def _ev_found(self, path):
        self._set_state("found")
        self.log(self.t("log_found", p=path), "acc")
        self.root.deiconify()
        self.root.lift()
        core.show_alert_box(self.t("found_box", p=path), self.t("found_title"))

    def _ev_bal_start(self, src):
        self.btn_check.configure(state="disabled", text=self.t("checking"))
        self.l_bal_info.configure(text=self.t("asking", src=self.src_name(src)), fg=MUTED)

    def _ev_bal_err(self, src, err):
        self.btn_check.configure(state="normal", text=self.t("btn_check"))
        self.l_bal_info.configure(text=self.t("bal_err_short", src=self.src_name(src)), fg=RED)
        self.log(self.t("log_bal_err", src=self.src_name(src), e=err), "err")

    def _ev_balance(self, n, src, conf, pend, secs):
        self.btn_check.configure(state="normal", text=self.t("btn_check"))
        if n != self.puzzle:      # wynik dla zagadki, którą w międzyczasie zmieniono
            return
        now = datetime.now().strftime("%H:%M:%S")
        extra = f"   ·   {self.t('in_mempool')} {pend:+.8f}" if pend else ""
        self.l_balance.configure(text=f"{conf:.8f} BTC", fg=ACCENT if conf > 0 else RED)
        self.l_bal_info.configure(text=f"{self.src_name(src)}   ·   {now}   ·   {secs:.0f} s{extra}", fg=MUTED)
        effective = conf + (pend or 0)
        self.log(self.t("log_bal", n=n, v=f"{conf:.8f}", x=extra, src=self.src_name(src)),
                 "ok" if effective > 0 else "err")
        if effective <= 0:
            why = self.t("spending") if conf > 0 else self.t("empty", n=n)
            if self.running:
                self.stop(f"{why} {self.t('search_stopped')}")
            self._set_state("solved")
            messagebox.showwarning(self.t("solved_title"), why)

    def _ev_list(self, unsolved, src):
        self.btn_list.configure(state="normal", text=self.t("btn_refresh"))
        gone = sorted(set(self.settings["unsolved"]) - set(unsolved))
        self.settings["unsolved"] = unsolved
        self._fill_puzzle_list()
        self._save_settings()
        msg = self.t("log_list", src=self.src_name(src), n=len(unsolved))
        if gone:
            msg += " " + self.t("log_list_new", l=", ".join("#" + str(g) for g in gone))
        self.log(msg, "ok")
        if self.puzzle not in unsolved:
            self.l_hint.configure(text=self.t("hint_solved", n=self.puzzle), fg=RED)

    def _ev_list_err(self, err):
        self.btn_list.configure(state="normal", text=self.t("btn_refresh"))
        self.log(self.t("log_list_err", e=err), "err")

    def _ev_next(self, ts):
        if self.running:
            self.l_next.configure(text=self.t("next_check", t=datetime.fromtimestamp(ts).strftime("%H:%M")))


def main():
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        pass
    root = tk.Tk()
    app = App(root)
    if "--start" in sys.argv:          # od razu zacznij szukać (np. przy autostarcie Windows)
        root.after(800, app.start)
    root.mainloop()


if __name__ == "__main__":
    main()

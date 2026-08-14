import os
import sys
import zlib
import shutil
import random
import struct
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

APP_TITLE = "BLACK SOULS II Simple Randomizer"
MOD_NAME = b"BS2 Simple Randomizer v0.6"
ARCHIVE_NAME = "Game.rgss3a"
ARCHIVE_BACKUP_SUFFIX = ".bs2randomizer_backup"
SCRIPTS_ENTRY_NAME = "data/scripts.rvdata2"
RGSS3A_HEADER = b"RGSSAD\x00\x03"
DEFAULT_FILE_MAGIC = 0xDEADCAFE

DEFAULTS = {
    "seed": "12345",
    "enemy_randomization": "true",
    "item_randomization": "true",
    "room_transition_randomization": "false",
    "regional_transition_randomization": "false",
    "regional_randomization": "false",
}


# -----------------------------------------------------------------------------
# Ruby Marshal reader/writer for VX Ace Scripts.rvdata2
# -----------------------------------------------------------------------------

class MarshalReader:
    def __init__(self, data):
        self.d = data
        self.p = 0
        self.symbols = []
        self.objects = []

    def b(self):
        if self.p >= len(self.d):
            raise EOFError("Unexpected end of file")
        v = self.d[self.p]
        self.p += 1
        return v

    def read_fixnum(self):
        c = self.b()
        if c == 0:
            return 0
        if 5 <= c <= 127:
            return c - 5
        if 128 <= c <= 250:
            return c - 256 + 5
        if 1 <= c <= 4:
            n = 0
            for i in range(c):
                n |= self.b() << (8 * i)
            return n
        if 252 <= c <= 255:
            count = 256 - c
            n = 0
            for i in range(count):
                n |= self.b() << (8 * i)
            return n - (1 << (count * 8))
        raise ValueError(f"Invalid Marshal fixnum marker {c}")

    def read(self):
        tag = chr(self.b())
        if tag == "0":
            return None
        if tag == "T":
            return True
        if tag == "F":
            return False
        if tag == "i":
            return self.read_fixnum()
        if tag == '"':
            n = self.read_fixnum()
            s = self.d[self.p:self.p + n]
            self.p += n
            self.objects.append(s)
            return s
        if tag == "[":
            n = self.read_fixnum()
            a = []
            self.objects.append(a)
            for _ in range(n):
                a.append(self.read())
            return a
        if tag == ":":
            n = self.read_fixnum()
            s = self.d[self.p:self.p + n]
            self.p += n
            self.symbols.append(s)
            return ("sym", s)
        if tag == ";":
            return ("sym", self.symbols[self.read_fixnum()])
        if tag == "@":
            return self.objects[self.read_fixnum()]
        if tag == "I":
            obj = self.read()
            n = self.read_fixnum()
            for _ in range(n):
                self.read()
                self.read()
            return obj
        raise ValueError(
            f"Unsupported Ruby Marshal tag {tag!r}. "
            "This Scripts.rvdata2 format is not recognized."
        )


def enc_fixnum(n):
    if n == 0:
        return b"\x00"
    if 0 < n < 123:
        return bytes([n + 5])
    if -124 < n < 0:
        return bytes([(n - 5) & 0xff])
    if n > 0:
        raw = []
        x = n
        while x:
            raw.append(x & 255)
            x >>= 8
        if len(raw) > 4:
            raise ValueError("Integer too large for Ruby Marshal fixnum")
        return bytes([len(raw)]) + bytes(raw)
    for count in range(1, 5):
        if -(1 << (count * 8 - 1)) <= n <= (1 << (count * 8 - 1)) - 1:
            val = (1 << (count * 8)) + n
            return bytes([256 - count]) + bytes(
                (val >> (8 * i)) & 255 for i in range(count)
            )
    raise ValueError("Integer too small for Ruby Marshal fixnum")


def enc_str(v):
    b = v.encode("utf-8") if isinstance(v, str) else bytes(v)
    return b'"' + enc_fixnum(len(b)) + b


def enc_obj(o):
    if o is None:
        return b"0"
    if o is True:
        return b"T"
    if o is False:
        return b"F"
    if isinstance(o, int):
        return b"i" + enc_fixnum(o)
    if isinstance(o, (bytes, bytearray, str)):
        return enc_str(o)
    if isinstance(o, list):
        return b"[" + enc_fixnum(len(o)) + b"".join(enc_obj(x) for x in o)
    raise TypeError(f"Cannot encode {type(o)}")


def load_scripts_bytes(data):
    if data[:2] != b"\x04\x08":
        raise ValueError("Embedded Data\\Scripts.rvdata2 is not Ruby Marshal 4.8 data.")
    r = MarshalReader(data[2:])
    obj = r.read()
    if not isinstance(obj, list):
        raise ValueError("Scripts archive root is not an array")
    for i, e in enumerate(obj):
        if not (
            isinstance(e, list)
            and len(e) == 3
            and isinstance(e[2], (bytes, bytearray))
        ):
            raise ValueError(f"Unexpected script entry at index {i}")
    return obj


def save_scripts_bytes(entries):
    return b"\x04\x08" + enc_obj(entries)


def script_name(entry):
    name = entry[1]
    return name.decode("utf-8", "replace") if isinstance(name, bytes) else str(name)


def is_our_randomizer(entry):
    return script_name(entry).startswith("BS2 Simple Randomizer")


def patch_scripts_bytes(original_data, source):
    entries = load_scripts_bytes(original_data)
    clean = [e for e in entries if not is_our_randomizer(e)]
    main_idx = next((i for i, e in enumerate(clean) if script_name(e) == "Main"), None)
    if main_idx is None:
        raise RuntimeError("Could not find the 'Main' script entry inside Scripts.rvdata2.")
    clean.insert(main_idx, [98941041, MOD_NAME, zlib.compress(source, 9)])
    patched = save_scripts_bytes(clean)

    verify = load_scripts_bytes(patched)
    matches = [e for e in verify if is_our_randomizer(e)]
    if len(matches) != 1 or zlib.decompress(matches[0][2]) != source:
        raise RuntimeError("Verification failed after creating patched Scripts.rvdata2.")
    return patched


def clean_scripts_bytes(original_data):
    entries = load_scripts_bytes(original_data)
    clean = [e for e in entries if not is_our_randomizer(e)]
    return save_scripts_bytes(clean)


# -----------------------------------------------------------------------------
# RGSS3A version 3 archive access
# Only the embedded Scripts.rvdata2 entry is decrypted/re-encrypted.
# Other archive data is never extracted.
# -----------------------------------------------------------------------------

class RGSS3AEntry:
    def __init__(self, name, offset, size, file_magic, metadata_pos):
        self.name = name
        self.offset = offset
        self.size = size
        self.file_magic = file_magic
        self.metadata_pos = metadata_pos


class RGSS3AArchive:
    def __init__(self, path):
        self.path = os.path.abspath(path)
        self.table_magic = None
        self.raw_magic = None
        self.entries = []
        self._read_table()

    @staticmethod
    def _u32(data):
        return struct.unpack("<I", data)[0]

    @staticmethod
    def _p32(value):
        return struct.pack("<I", value & 0xFFFFFFFF)

    def _read_table(self):
        file_size = os.path.getsize(self.path)
        with open(self.path, "rb") as f:
            header = f.read(8)
            if header != RGSS3A_HEADER:
                raise ValueError(
                    "Game.rgss3a is not an RPG Maker VX Ace RGSS3A version 3 archive."
                )
            raw = f.read(4)
            if len(raw) != 4:
                raise ValueError("Game.rgss3a is truncated before its archive key.")
            self.raw_magic = self._u32(raw)
            self.table_magic = (self.raw_magic * 9 + 3) & 0xFFFFFFFF

            entries = []
            while True:
                metadata_pos = f.tell()
                raw_offset = f.read(4)
                if len(raw_offset) != 4:
                    raise ValueError("Game.rgss3a metadata table is truncated.")
                offset = self._u32(raw_offset) ^ self.table_magic
                if offset == 0:
                    break

                fields = f.read(12)
                if len(fields) != 12:
                    raise ValueError("Game.rgss3a metadata entry is truncated.")
                size = self._u32(fields[0:4]) ^ self.table_magic
                file_magic = self._u32(fields[4:8]) ^ self.table_magic
                name_len = self._u32(fields[8:12]) ^ self.table_magic
                if name_len > 1024 * 1024:
                    raise ValueError("Game.rgss3a contains an invalid filename length.")

                encrypted_name = bytearray(f.read(name_len))
                if len(encrypted_name) != name_len:
                    raise ValueError("Game.rgss3a filename metadata is truncated.")
                for i in range(name_len):
                    encrypted_name[i] ^= (
                        self.table_magic >> ((i % 4) * 8)
                    ) & 0xFF
                name = bytes(encrypted_name).replace(b"\\", b"/").decode(
                    "utf-8", "replace"
                )

                if offset + size > file_size:
                    raise ValueError(
                        f"Archive entry {name!r} points outside Game.rgss3a."
                    )
                entries.append(
                    RGSS3AEntry(name, offset, size, file_magic, metadata_pos)
                )

            self.entries = entries

    def find_entry(self, name):
        wanted = name.replace("\\", "/").lower()
        for entry in self.entries:
            if entry.name.replace("\\", "/").lower() == wanted:
                return entry
        raise FileNotFoundError(f"{name} was not found inside Game.rgss3a.")

    @staticmethod
    def crypt_bytes(data, magic):
        out = bytearray(data)
        pos = 0
        current = magic & 0xFFFFFFFF
        aligned = (len(out) // 4) * 4
        while pos < aligned:
            value = struct.unpack_from("<I", out, pos)[0] ^ current
            struct.pack_into("<I", out, pos, value & 0xFFFFFFFF)
            current = (current * 7 + 3) & 0xFFFFFFFF
            pos += 4
        remainder_index = 0
        while pos < len(out):
            out[pos] ^= (current >> (remainder_index * 8)) & 0xFF
            remainder_index += 1
            pos += 1
        return bytes(out)

    def read_entry(self, entry):
        with open(self.path, "rb") as f:
            f.seek(entry.offset)
            encrypted = f.read(entry.size)
        if len(encrypted) != entry.size:
            raise IOError(f"Could not read the complete archive entry {entry.name}.")
        return self.crypt_bytes(encrypted, entry.file_magic)

    def replace_entry_by_append(self, entry_name, new_plain_data):
        """
        Append the replacement entry and redirect the existing metadata record.
        This avoids extracting or rewriting every other file in the archive.
        """
        entry = self.find_entry(entry_name)
        new_magic = DEFAULT_FILE_MAGIC
        encrypted = self.crypt_bytes(new_plain_data, new_magic)

        # Append first. If interrupted before metadata is changed, the old archive
        # still points to the original data and remains usable.
        with open(self.path, "r+b") as f:
            f.seek(0, os.SEEK_END)
            new_offset = f.tell()
            f.write(encrypted)
            f.flush()
            os.fsync(f.fileno())

            # Redirect only this entry. Filename length/name remain unchanged.
            f.seek(entry.metadata_pos)
            f.write(self._p32(new_offset ^ self.table_magic))
            f.write(self._p32(len(new_plain_data) ^ self.table_magic))
            f.write(self._p32(new_magic ^ self.table_magic))
            f.flush()
            os.fsync(f.fileno())

        # Re-read and verify the exact data now referenced by the archive.
        check = RGSS3AArchive(self.path)
        check_entry = check.find_entry(entry_name)
        actual = check.read_entry(check_entry)
        if actual != new_plain_data:
            raise RuntimeError(
                "Archive verification failed after replacing Data\\Scripts.rvdata2. "
                "Use Restore Original Archive to recover the backup."
            )


# -----------------------------------------------------------------------------
# Installer / restore logic
# -----------------------------------------------------------------------------

def app_resource(filename):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, filename)


def validate_game_dir(game_dir):
    game_dir = os.path.abspath(game_dir.strip().strip('"'))
    game_exe = os.path.join(game_dir, "Game.exe")
    archive = os.path.join(game_dir, ARCHIVE_NAME)
    if not os.path.isfile(game_exe):
        raise FileNotFoundError("Game.exe was not found in the selected folder.")
    if not os.path.isfile(archive):
        raise FileNotFoundError(
            "Game.rgss3a was not found. Select the original BLACK SOULS II game folder."
        )
    # Validate archive structure and required embedded script file.
    rgss = RGSS3AArchive(archive)
    rgss.find_entry(SCRIPTS_ENTRY_NAME)
    return game_dir


def ensure_clean_archive_backup(archive_path):
    backup = archive_path + ARCHIVE_BACKUP_SUFFIX
    if os.path.isfile(backup):
        # Validate an existing backup before trusting it.
        check = RGSS3AArchive(backup)
        entry = check.find_entry(SCRIPTS_ENTRY_NAME)
        load_scripts_bytes(check.read_entry(entry))
        return backup

    # Make the backup before changing the user's archive.
    shutil.copy2(archive_path, backup)

    # If the selected archive already contains one of our older injected builds,
    # clean that injected script out of the backup so Restore returns to vanilla.
    check = RGSS3AArchive(backup)
    entry = check.find_entry(SCRIPTS_ENTRY_NAME)
    scripts = check.read_entry(entry)
    script_entries = load_scripts_bytes(scripts)
    if any(is_our_randomizer(e) for e in script_entries):
        cleaned = clean_scripts_bytes(scripts)
        check.replace_entry_by_append(SCRIPTS_ENTRY_NAME, cleaned)

    # Final validation.
    verify = RGSS3AArchive(backup)
    ventry = verify.find_entry(SCRIPTS_ENTRY_NAME)
    ventries = load_scripts_bytes(verify.read_entry(ventry))
    if any(is_our_randomizer(e) for e in ventries):
        raise RuntimeError("Could not create a clean original archive backup.")
    return backup


def install_randomizer(game_dir):
    game_dir = validate_game_dir(game_dir)
    archive_path = os.path.join(game_dir, ARCHIVE_NAME)
    source_path = app_resource("BS2_Simple_Randomizer.rb")
    if not os.path.isfile(source_path):
        raise FileNotFoundError("Embedded randomizer source is missing from the patcher package.")
    with open(source_path, "rb") as f:
        source = f.read()

    backup = ensure_clean_archive_backup(archive_path)

    # Always start an install/update from the clean backup. This prevents the
    # archive growing every time the user changes settings or updates the mod.
    shutil.copy2(backup, archive_path)

    archive = RGSS3AArchive(archive_path)
    scripts_entry = archive.find_entry(SCRIPTS_ENTRY_NAME)
    original_scripts = archive.read_entry(scripts_entry)
    patched_scripts = patch_scripts_bytes(original_scripts, source)
    archive.replace_entry_by_append(SCRIPTS_ENTRY_NAME, patched_scripts)

    # Verify injection from the finished archive.
    verify = RGSS3AArchive(archive_path)
    ventry = verify.find_entry(SCRIPTS_ENTRY_NAME)
    entries = load_scripts_bytes(verify.read_entry(ventry))
    matches = [e for e in entries if is_our_randomizer(e)]
    if len(matches) != 1 or zlib.decompress(matches[0][2]) != source:
        raise RuntimeError("Final Game.rgss3a verification failed after installation.")
    return archive_path, backup


def restore_randomizer(game_dir):
    game_dir = os.path.abspath(game_dir.strip().strip('"'))
    game_exe = os.path.join(game_dir, "Game.exe")
    archive_path = os.path.join(game_dir, ARCHIVE_NAME)
    backup = archive_path + ARCHIVE_BACKUP_SUFFIX
    if not os.path.isfile(game_exe):
        raise FileNotFoundError("Game.exe was not found in the selected folder.")
    if not os.path.isfile(backup):
        raise FileNotFoundError("No original Game.rgss3a randomizer backup was found.")

    # Validate backup before overwriting anything.
    check = RGSS3AArchive(backup)
    entry = check.find_entry(SCRIPTS_ENTRY_NAME)
    entries = load_scripts_bytes(check.read_entry(entry))
    if any(is_our_randomizer(e) for e in entries):
        raise RuntimeError("The archive backup still contains a randomizer injection; restore was cancelled.")

    shutil.copy2(backup, archive_path)
    verify = RGSS3AArchive(archive_path)
    ventry = verify.find_entry(SCRIPTS_ENTRY_NAME)
    ventries = load_scripts_bytes(verify.read_entry(ventry))
    if any(is_our_randomizer(e) for e in ventries):
        raise RuntimeError("Restore verification failed.")
    return archive_path


def read_config(game_dir):
    values = dict(DEFAULTS)
    path = os.path.join(game_dir, "RandomizerConfig.txt")
    if not os.path.isfile(path):
        return values
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()
                if key in values:
                    values[key] = value
    except OSError:
        pass
    return values


def write_config(game_dir, values):
    path = os.path.join(game_dir, "RandomizerConfig.txt")
    text = (
        "# BLACK SOULS II Simple Randomizer\n"
        f"seed={values['seed']}\n"
        f"enemy_randomization={values['enemy_randomization']}\n"
        f"item_randomization={values['item_randomization']}\n"
        f"room_transition_randomization={values['room_transition_randomization']}\n"
        f"regional_transition_randomization={values['regional_transition_randomization']}\n"
        f"regional_randomization={values['regional_randomization']}\n"
    )
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return path


# -----------------------------------------------------------------------------
# GUI
# -----------------------------------------------------------------------------

class RandomizerGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("700x540")
        self.minsize(650, 500)

        self.game_dir = tk.StringVar()
        self.seed = tk.StringVar(value=DEFAULTS["seed"])
        self.enemy = tk.BooleanVar(value=True)
        self.items = tk.BooleanVar(value=True)
        self.transitions = tk.BooleanVar(value=False)
        self.regional = tk.BooleanVar(value=False)
        self.status = tk.StringVar(
            value="Select the original BLACK SOULS II folder containing Game.exe and Game.rgss3a."
        )
        self._build()

    def _build(self):
        outer = ttk.Frame(self, padding=18)
        outer.pack(fill="both", expand=True)

        ttk.Label(
            outer,
            text="BLACK SOULS II Simple Randomizer",
            font=("Segoe UI", 17, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            outer,
            text="Direct Game.rgss3a patcher — no extracted Data or Graphics folders required.",
        ).pack(anchor="w", pady=(2, 16))

        folder = ttk.LabelFrame(outer, text="Game Folder", padding=10)
        folder.pack(fill="x")
        row = ttk.Frame(folder)
        row.pack(fill="x")
        ttk.Entry(row, textvariable=self.game_dir).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Browse...", command=self.browse).pack(side="left", padx=(8, 0))
        ttk.Button(folder, text="Load Existing Settings", command=self.load_settings).pack(
            anchor="e", pady=(8, 0)
        )

        settings = ttk.LabelFrame(outer, text="Randomizer Settings", padding=10)
        settings.pack(fill="x", pady=(14, 0))

        seedrow = ttk.Frame(settings)
        seedrow.pack(fill="x")
        ttk.Label(seedrow, text="Seed:", width=12).pack(side="left")
        ttk.Entry(seedrow, textvariable=self.seed, width=24).pack(side="left")
        ttk.Button(seedrow, text="Random Seed", command=self.random_seed).pack(
            side="left", padx=(8, 0)
        )

        checks = ttk.Frame(settings)
        checks.pack(fill="x", pady=(10, 0))
        ttk.Checkbutton(checks, text="Enemy randomization", variable=self.enemy).grid(
            row=0, column=0, sticky="w", padx=(0, 26), pady=4
        )
        ttk.Checkbutton(checks, text="Item + shop randomization", variable=self.items).grid(
            row=0, column=1, sticky="w", pady=4
        )
        ttk.Checkbutton(checks, text="Room transition shuffle", variable=self.transitions).grid(
            row=1, column=0, sticky="w", padx=(0, 26), pady=4
        )
        ttk.Checkbutton(checks, text="Shuffle within regions", variable=self.regional).grid(
            row=1, column=1, sticky="w", pady=4
        )

        note = (
            "The installer creates Game.rgss3a.bs2randomizer_backup before modifying the archive. "
            "Only the embedded Data\\Scripts.rvdata2 entry is replaced; maps, graphics, audio, and other files stay packed."
        )
        ttk.Label(settings, text=note, wraplength=620).pack(anchor="w", pady=(10, 0))

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(18, 0))
        ttk.Button(actions, text="Install / Update Randomizer", command=self.install).pack(side="left")
        ttk.Button(actions, text="Restore Original Archive", command=self.restore).pack(
            side="left", padx=(10, 0)
        )
        ttk.Button(actions, text="Open Game Folder", command=self.open_folder).pack(side="right")

        status_box = ttk.LabelFrame(outer, text="Status", padding=10)
        status_box.pack(fill="both", expand=True, pady=(14, 0))
        ttk.Label(
            status_box, textvariable=self.status, wraplength=620, justify="left"
        ).pack(anchor="nw")

    def browse(self):
        path = filedialog.askdirectory(title="Select BLACK SOULS II folder")
        if path:
            self.game_dir.set(path)
            self.load_settings(silent=True)

    def random_seed(self):
        self.seed.set(str(random.SystemRandom().randint(1, 2147483647)))

    def _bool_text(self, value):
        return "true" if value else "false"

    def config_values(self):
        seed = self.seed.get().strip()
        if not seed:
            raise ValueError("Seed cannot be empty.")
        try:
            int(seed)
        except ValueError:
            raise ValueError("Seed must be a whole number.")
        regional = self._bool_text(self.regional.get())
        return {
            "seed": seed,
            "enemy_randomization": self._bool_text(self.enemy.get()),
            "item_randomization": self._bool_text(self.items.get()),
            "room_transition_randomization": self._bool_text(self.transitions.get()),
            "regional_transition_randomization": regional,
            "regional_randomization": regional,
        }

    def load_settings(self, silent=False):
        try:
            game = validate_game_dir(self.game_dir.get())
            cfg = read_config(game)
            self.seed.set(cfg.get("seed", DEFAULTS["seed"]))
            self.enemy.set(cfg.get("enemy_randomization", "true").lower() == "true")
            self.items.set(cfg.get("item_randomization", "true").lower() == "true")
            self.transitions.set(
                cfg.get("room_transition_randomization", "false").lower() == "true"
            )
            reg = cfg.get(
                "regional_randomization",
                cfg.get("regional_transition_randomization", "false"),
            ).lower() == "true"
            self.regional.set(reg)
            archive = RGSS3AArchive(os.path.join(game, ARCHIVE_NAME))
            self.status.set(
                f"Game archive validated ({len(archive.entries)} packed files). "
                + (
                    "Existing RandomizerConfig.txt settings loaded."
                    if os.path.isfile(os.path.join(game, "RandomizerConfig.txt"))
                    else "No existing config found; defaults loaded."
                )
            )
        except Exception as e:
            self.status.set(str(e))
            if not silent:
                messagebox.showerror(APP_TITLE, str(e))

    def install(self):
        try:
            game = validate_game_dir(self.game_dir.get())
            values = self.config_values()
            cfg = write_config(game, values)
            archive, backup = install_randomizer(game)
            self.status.set(
                "Randomizer installed/updated successfully.\n"
                f"Patched archive: {archive}\n"
                f"Config: {cfg}\n"
                f"Original archive backup: {backup}"
            )
            messagebox.showinfo(APP_TITLE, "Randomizer installed successfully into Game.rgss3a.")
        except Exception as e:
            self.status.set(f"ERROR: {e}")
            messagebox.showerror(APP_TITLE, str(e))

    def restore(self):
        try:
            game = os.path.abspath(self.game_dir.get().strip().strip('"'))
            archive = restore_randomizer(game)
            self.status.set(
                f"Original Game.rgss3a restored successfully.\nRestored: {archive}\n"
                "RandomizerConfig.txt was left in place."
            )
            messagebox.showinfo(APP_TITLE, "Original Game.rgss3a restored successfully.")
        except Exception as e:
            self.status.set(f"ERROR: {e}")
            messagebox.showerror(APP_TITLE, str(e))

    def open_folder(self):
        path = self.game_dir.get().strip().strip('"')
        if not os.path.isdir(path):
            messagebox.showerror(APP_TITLE, "Select a valid game folder first.")
            return
        try:
            os.startfile(path)
        except AttributeError:
            messagebox.showinfo(APP_TITLE, path)
        except OSError as e:
            messagebox.showerror(APP_TITLE, str(e))


if __name__ == "__main__":
    RandomizerGUI().mainloop()

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, scrolledtext
import ctypes
from ctypes import wintypes
import hashlib
import os
import pickle
import platform
import random
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    import xxhash

    def _state_fingerprint(obj: object) -> str:
        return xxhash.xxh64(pickle.dumps(obj, protocol=4)).hexdigest()
except ImportError:
    def _state_fingerprint(obj: object) -> str:
        return hashlib.sha256(pickle.dumps(obj, protocol=4)).hexdigest()

TARGET_FPS = 60.0
VERBOSE_EMU = False

LIBRETRO_BUILDBOT = "https://buildbot.libretro.com/nightly"
RETROARCH_STABLE_WIN64_7Z = "https://buildbot.libretro.com/stable/1.22.0/windows/x86_64/RetroArch.7z"
CATHLE_RETROARCH_EXE_HINT = ".cathle_retroarch_exe"
CATHLE0_1A_SKIP_WIZARD = ".cathle0.1a_skip_setup_wizard"
_DEFAULT_N64_CORES = ("parallel_n64", "mupen64plus_next", "mupen64plus")


def _libretro_ext() -> str:
    if os.name == "nt":
        return "dll"
    if sys.platform == "darwin":
        return "dylib"
    return "so"


def _buildbot_platform() -> tuple[str, str]:
    m = (platform.machine() or "").lower()
    big = sys.maxsize > 2**32
    if os.name == "nt" or sys.platform.startswith("win"):
        if "arm" in m and big:
            return "windows/arm64", "dll"
        return ("windows/x86_64", "dll") if big or m in ("amd64", "x86_64") else ("windows/x86", "dll")
    if sys.platform == "darwin":
        return ("apple/osx/arm64", "dylib") if "arm" in m or m == "arm64" else ("apple/osx/x86_64", "dylib")
    if "aarch64" in m or "arm64" in m:
        return "linux/armv8", "so"
    if "arm" in m:
        return "linux/armhf", "so"
    return ("linux/x86_64", "so") if big else ("linux/i686", "so")


def _retro_search_roots(script_dir: str) -> list[Path]:
    roots: list[Path] = []
    if script_dir:
        sd = Path(script_dir).resolve()
        for sub in ("RetroArch-Win64", "RetroArch", "RetroArch-Win32"):
            roots.append(sd / sub)
        roots.append(sd)
    if sys.platform.startswith("win"):
        for ev in ("USERPROFILE", "APPDATA", "LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
            b = os.environ.get(ev)
            if not b:
                continue
            for sub in (r"RetroArch-Win64", r"RetroArch", r"RetroArch-Win32"):
                roots.append(Path(b) / sub)
        roots += [
            Path(r"C:\Program Files (x86)\Steam\steamapps\common\RetroArch"),
            Path(r"C:\Program Files\Steam\steamapps\common\RetroArch"),
        ]
    elif sys.platform == "darwin":
        h = Path.home()
        roots += [h / "Library/Application Support/RetroArch", Path("/Applications/RetroArch.app/Contents/MacOS")]
    else:
        h = Path.home()
        roots += [h / ".config/retroarch", Path("/usr/bin")]
    roots.append(Path(script_dir).resolve() / "cores" if script_dir else Path("cores"))
    return roots


def find_retroarch_exe(script_dir: str = "") -> Optional[Path]:
    envp = (os.environ.get("RETROARCH_EXE") or "").strip()
    if envp:
        p = Path(envp)
        if p.is_file():
            return p.resolve()
    hinted = _read_retroarch_hint(script_dir)
    if hinted:
        return hinted
    for name in ("retroarch", "retroarch.exe"):
        w = shutil.which(name)
        if w:
            return Path(w).resolve()
    for root in _retro_search_roots(script_dir):
        exe = root / "retroarch.exe"
        if exe.is_file():
            return exe.resolve()
        mac = root / "RetroArch"
        if mac.is_file():
            return mac.resolve()
        lx = root / "retroarch"
        if lx.is_file():
            return lx.resolve()
    if script_dir:
        sd = Path(script_dir).resolve()
        for base in (sd, sd.parent):
            for sub in ("RetroArch-Win64", "RetroArch", "RetroArch-Win32"):
                nested = _pick_retroarch_exe_under(base / sub)
                if nested:
                    return nested
    return None


def discover_n64_cores(script_dir: str) -> list[Path]:
    ext = _libretro_ext()
    found: dict[str, Path] = {}
    for base in _retro_search_roots(script_dir):
        try:
            cdir = base / "cores" if base.name != "cores" else base
            if not cdir.is_dir():
                continue
            for f in cdir.glob(f"*_libretro.{ext}"):
                n = f.name.lower()
                if not any(k in n for k in ("n64", "mupen", "parallel")):
                    continue
                found.setdefault(f.name, f.resolve())
        except OSError:
            pass

    def rank(p: Path) -> tuple[int, str]:
        name = p.name.lower()
        for i, pref in enumerate(_DEFAULT_N64_CORES):
            if name.startswith(pref + "_libretro"):
                return (i, name)
        return (99, name)

    return sorted(found.values(), key=rank)


def download_n64_core(
    core_name: str,
    dest: Path,
    *,
    timeout: float = 120.0,
    prog: Optional[Callable[[int, int, str], None]] = None,
) -> tuple[Optional[Path], str]:
    try:
        plat, x = _buildbot_platform()
    except Exception as e:
        return None, str(e)
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return None, str(e)
    fn = f"{core_name}_libretro.{x}"
    url = f"{LIBRETRO_BUILDBOT}/{plat}/latest/{fn}.zip"
    if prog:
        prog(0, 0, url)
    tmp: Optional[Path] = None
    try:
        req = Request(url, headers={"User-Agent": "cathle0.1a/1.0"})
        with urlopen(req, timeout=timeout) as resp:
            tot = int(resp.headers.get("Content-Length", "0") or 0)
            done = 0
            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as t:
                tmp = Path(t.name)
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    t.write(chunk)
                    done += len(chunk)
                    if prog:
                        prog(done, tot or done, fn)
        with zipfile.ZipFile(tmp, "r") as zf:
            zf.extractall(dest)
        p = dest / fn
        if not p.exists():
            for g in dest.glob(f"{core_name}_libretro*.{x}"):
                p = g
                break
        if not p.exists():
            return None, "core not found in zip"
        return p, ""
    except HTTPError as e:
        return None, f"HTTP {e.code}"
    except URLError as e:
        return None, str(e.reason)
    except Exception as e:
        return None, str(e)
    finally:
        if tmp and tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _read_retroarch_hint(script_dir: str) -> Optional[Path]:
    if not script_dir:
        return None
    h = Path(script_dir) / CATHLE_RETROARCH_EXE_HINT
    try:
        if not h.is_file():
            return None
        line = h.read_text(encoding="utf-8").strip().splitlines()[0].strip()
        if not line:
            return None
        p = Path(line)
        if p.is_file():
            return p.resolve()
    except (OSError, IndexError):
        pass
    return None


def _write_retroarch_hint(script_dir: str, exe: Path) -> None:
    if not script_dir:
        return
    try:
        (Path(script_dir) / CATHLE_RETROARCH_EXE_HINT).write_text(str(exe.resolve()) + "\n", encoding="utf-8")
    except OSError:
        pass


def _seven_zip_cli() -> Optional[Path]:
    for candidate in (
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "7-Zip" / "7z.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "7-Zip" / "7z.exe",
        Path(os.environ.get("LocalAppData", "")) / "Programs" / "7-Zip" / "7z.exe",
        Path(os.environ.get("LocalAppData", "")) / "7-Zip" / "7z.exe",
    ):
        if candidate and candidate.is_file():
            return candidate
    w = shutil.which("7z")
    return Path(w).resolve() if w else None


def _extract_7z_archive(archive: Path, out_dir: Path) -> tuple[bool, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        import py7zr  # type: ignore[import-untyped]
    except ImportError:
        py7zr = None
    if py7zr is not None:
        try:
            with py7zr.SevenZipFile(archive, mode="r") as zf:
                zf.extractall(path=out_dir)
            return True, ""
        except Exception as e:
            return False, f"py7zr: {e}"
    seven = _seven_zip_cli()
    if not seven:
        return (
            False,
            "Need 7-Zip (https://www.7-zip.org/) on PATH or in Program Files, or: pip install py7zr",
        )
    try:
        cflags = 0
        if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            cflags = subprocess.CREATE_NO_WINDOW
        r = subprocess.run(
            [str(seven), "x", "-y", f"-o{out_dir}", str(archive)],
            capture_output=True,
            text=True,
            timeout=900,
            creationflags=cflags,
        )
        if r.returncode != 0:
            tail = (r.stderr or r.stdout or "").strip()[-400:]
            return False, tail or f"7z exit {r.returncode}"
        return True, ""
    except Exception as e:
        return False, str(e)


def _pick_retroarch_exe_under(root: Path) -> Optional[Path]:
    if not root.is_dir():
        return None
    candidates: list[Path] = []
    try:
        for p in root.rglob("*"):
            if p.is_file() and p.name.lower() == "retroarch.exe":
                candidates.append(p.resolve())
    except OSError:
        return None
    if not candidates:
        return None
    return sorted(candidates, key=lambda x: len(x.parts))[0]


def download_retroarch_portable_windows(
    script_dir: str,
    *,
    timeout: float = 900.0,
    prog: Optional[Callable[[int, int, str], None]] = None,
) -> tuple[Optional[Path], str]:
    """Download stable RetroArch.7z into script_dir/RetroArch-Win64 (Windows x86_64)."""
    if os.name != "nt":
        return None, "RetroArch portable fetch is implemented for Windows only"
    sd = Path(script_dir).resolve()
    final = sd / "RetroArch-Win64"
    hit = _pick_retroarch_exe_under(final)
    if hit:
        _write_retroarch_hint(script_dir, hit)
        return hit, ""
    tmp_7z: Optional[Path] = None
    stage = Path(tempfile.mkdtemp(prefix="cathle_ra_", dir=str(sd)))
    stage_leftovers = True
    try:
        if prog:
            prog(0, 0, RETROARCH_STABLE_WIN64_7Z)
        req = Request(RETROARCH_STABLE_WIN64_7Z, headers={"User-Agent": "cathle0.1a/1.0"})
        with urlopen(req, timeout=timeout) as resp:
            tot = int(resp.headers.get("Content-Length", "0") or 0)
            done = 0
            with tempfile.NamedTemporaryFile(suffix=".7z", delete=False, dir=str(sd)) as t:
                tmp_7z = Path(t.name)
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    t.write(chunk)
                    done += len(chunk)
                    if prog:
                        prog(done, tot or done, "RetroArch.7z")
        ok, err = _extract_7z_archive(tmp_7z, stage)
        if not ok:
            return None, err
        exe = _pick_retroarch_exe_under(stage)
        if not exe:
            return None, "retroarch.exe not found inside RetroArch.7z"
        if final.exists():
            shutil.rmtree(final, ignore_errors=True)
        try:
            stage.rename(final)
            stage_leftovers = False
        except OSError:
            shutil.copytree(stage, final)
            shutil.rmtree(stage, ignore_errors=True)
            stage_leftovers = False
        exe = _pick_retroarch_exe_under(final)
        if not exe:
            return None, "retroarch.exe missing after install"
        _write_retroarch_hint(script_dir, exe)
        return exe, ""
    except HTTPError as e:
        return None, f"HTTP {e.code}"
    except URLError as e:
        return None, str(e.reason)
    except Exception as e:
        return None, str(e)
    finally:
        if stage_leftovers:
            shutil.rmtree(stage, ignore_errors=True)
        if tmp_7z and tmp_7z.exists():
            try:
                tmp_7z.unlink()
            except OSError:
                pass


CATHLE_WINDOW_TITLE = "cathle 0.1a [C] A.C Holdings 1999-2026 [C] Nintendo 1985-2026"


class RealityCoprocessor:
    def __init__(self, canvas):
        self.canvas = canvas
        self.framebuffer = [[0] * 320 for _ in range(240)]  # 320x240 framebuffer
        self.rsp_active = False
        self.rdp_active = False
        self.cycle_count = 0
        self.vu_registers = [0] * 32  # Vector Unit registers
        self._pixel_tag = "emuai4k_pixels"
        self.rsp_pc = 0
        self.rsp_status = 0
        self.rsp_memory = [0] * 0x1000  # 4KB RSP DMEM
        self.use_test_pattern = True  # Toggle for test pattern
        self.freeze_raster = False  # When True, RSP does not overwrite framebuffer (toy in-game scene)
        self.suppress_pixel_push = False  # When True, skip RSP full-frame fill (black boot window)
        self.boot_hint_text: Optional[str] = None  # Shown on canvas when framebuffer is all black

    def rsp_execute(self, rom_data, pc):
        """Execute RSP instructions, with SM64-compatible ops."""
        if VERBOSE_EMU:
            print(f"[RSP] Executing at PC: {hex(pc)}")
        self.rsp_active = True
        self.cycle_count += 10
        try:
            if pc + 4 > len(rom_data):
                raise ValueError("PC out of ROM bounds")
            opcode = struct.unpack('>I', rom_data[pc:pc+4])[0]
            op = (opcode >> 26) & 0x3F
            if VERBOSE_EMU:
                print(f"[RSP] Opcode: {hex(opcode)}, Op: {hex(op)}")
            if op == 0x32:  # VADD
                rs = (opcode >> 21) & 0x1F
                rt = (opcode >> 16) & 0x1F
                rd = (opcode >> 11) & 0x1F
                self.vu_registers[rd] = (self.vu_registers[rs] + self.vu_registers[rt]) & 0xFFFF
                self.cycle_count += 8
                if VERBOSE_EMU:
                    print(f"[RSP] VADD: vu[{rd}] = vu[{rs}] + vu[{rt}] = {self.vu_registers[rd]}")
            elif op == 0x33:  # VMUL
                rs = (opcode >> 21) & 0x1F
                rt = (opcode >> 16) & 0x1F
                rd = (opcode >> 11) & 0x1F
                self.vu_registers[rd] = (self.vu_registers[rs] * self.vu_registers[rt]) & 0xFFFF
                self.cycle_count += 10
                if VERBOSE_EMU:
                    print(f"[RSP] VMUL: vu[{rd}] = vu[{rs}] * vu[{rt}] = {self.vu_registers[rd]}")
            elif op == 0x34:  # VMOV (SM64 uses similar vector ops)
                rs = (opcode >> 21) & 0x1F
                rd = (opcode >> 11) & 0x1F
                self.vu_registers[rd] = self.vu_registers[rs]
                self.cycle_count += 6
                if VERBOSE_EMU:
                    print(f"[RSP] VMOV: vu[{rd}] = vu[{rs}] = {self.vu_registers[rd]}")
            # Generate pixel data for SM64 (skipped once toy "in-level" frame is shown)
            if not self.freeze_raster and not self.suppress_pixel_push:
                for i in range(320):
                    for j in range(240):
                        self.framebuffer[j][i] = (
                            (self.vu_registers[0] % 256) << 16
                            | (self.vu_registers[1] % 256) << 8
                            | (self.vu_registers[2] % 256)
                        )
            self.rsp_memory[self.rsp_pc % 0x1000] = opcode & 0xFFFF
            self.rsp_pc += 4
            if VERBOSE_EMU:
                print(f"[RSP] Framebuffer sample: {self.framebuffer[0][0]:08x}")
        except Exception as e:
            print(f"[RSP] Error: {e}")
        self.rsp_active = False
        return pc + 4

    def rdp_render(self):
        """Render framebuffer (tk main thread). Skips heavy fills when black; still draws boot hint."""
        self.rdp_active = True
        pixel_size = 2
        self.canvas.delete(self._pixel_tag)
        has_nonzero = any(any(pixel != 0 for pixel in row) for row in self.framebuffer)
        if not has_nonzero and not self.use_test_pattern:
            self.canvas.create_rectangle(
                0, 0, 640, 480, fill="#0a0a12", outline="#4a4a5a", width=1, tags=(self._pixel_tag,)
            )
            if self.boot_hint_text:
                lines = self.boot_hint_text.split("\n")[:4]
                font = ("Segoe UI", 11) if sys.platform == "win32" else ("Arial", 11)
                for i, line in enumerate(lines):
                    self.canvas.create_text(
                        320,
                        205 + i * 22,
                        text=line,
                        fill="#c8c8d8",
                        font=font,
                        tags=(self._pixel_tag,),
                    )
            self.rdp_active = False
            self.cycle_count += 1
            return
        if VERBOSE_EMU:
            print("[RDP] Rendering framebuffer")
        if not has_nonzero and self.use_test_pattern:
            if VERBOSE_EMU:
                print("[RDP] Framebuffer empty, rendering test pattern")
            for y in range(240):
                for x in range(320):
                    r = (x * 255 // 320) % 256
                    g = (y * 255 // 240) % 256
                    b = ((x + y) * 255 // 560) % 256
                    self.framebuffer[y][x] = (r << 16) | (g << 8) | b
        for y in range(240):
            for x in range(320):
                color = self.framebuffer[y][x]
                r, g, b = (color >> 16) & 0xFF, (color >> 8) & 0xFF, color & 0xFF
                hex_color = "#%02x%02x%02x" % (r, g, b)
                self.canvas.create_rectangle(
                    x * pixel_size, y * pixel_size,
                    (x + 1) * pixel_size, (y + 1) * pixel_size,
                    fill=hex_color, outline="",
                    tags=(self._pixel_tag,),
                )
        self.rdp_active = False
        self.cycle_count += 1000

class N64Emulator:
    def __init__(self, root):
        self.root = root
        # Project 640 · 0.1 — tkinter shell; 320×240 framebuffer scaled ×2 → 640×480
        self.root.title(CATHLE_WINDOW_TITLE)
        self.root.geometry("720x640")
        self.root.minsize(680, 600)
        self.root.configure(bg="#d4d0c8")
        self.is_running = False
        self.rom_path = None
        self.rom_title = "Unknown"
        self.rom_data = b""
        self.rom_md5 = ""
        self.emulation_thread = None
        self.emulation_speed = 1.0
        self.frame_count = 0
        self.pc = 0x1000  # ROM file offset for this toy fetch-decode path
        self._is_sm64_rom = False
        self._is_commercial_cart = False  # valid cartridge ROM after header byteswap
        self._boot_phase = 0  # frames since last boot (black → SM64 toy in-game)
        self._pending_soft_reset = False
        self.save_states = {}
        self.cheat_codes = {}
        self.controller_config = {
            "Up": "w", "Down": "s", "Left": "a", "Right": "d",
            "A": "j", "B": "k", "Start": "Return", "Z": "z"
        }
        self.cpu_registers = [0] * 32
        self.rcp = None
        self.script_dir = os.path.dirname(os.path.abspath(__file__))
        self.cores_dir = Path(self.script_dir) / "cores"
        self.retroarch_exe: Optional[Path] = None
        self.libretro_core_path: Optional[Path] = None
        self._ext_proc: Optional[subprocess.Popen] = None
        self._use_external = False
        self._ra_child_hwnd: Optional[int] = None
        self._ra_cfg_bind: Optional[str] = None
        self._core_download_thread: Optional[threading.Thread] = None
        self._retroarch_download_thread: Optional[threading.Thread] = None
        self._setup_wizard_window: Optional[tk.Toplevel] = None
        self.rdram = [0] * 0x400000
        self.rdram_size = 0x400000
        self.rdram_bytes = bytearray(self.rdram_size)
        self._ipl_copy_len = 0
        self._rom_entry_virt = 0x80000400
        self.plugins = {
            "video": "glide64mk2",
            "audio": "sdl",
            "input": "sdl",
            "rsp": "hle"
        }
        self.netplay_enabled = False
        self.netplay_socket = None
        self._auto_play_var = tk.BooleanVar(master=root, value=True)
        self._auto_fetch_retroarch_var = tk.BooleanVar(master=root, value=True)
        self.setup_gui()
        self.auto_install_core()
        self.root.after(700, self._maybe_open_setup_wizard)

    def auto_install_core(self) -> None:
        self.cores_dir.mkdir(parents=True, exist_ok=True)
        self.retroarch_exe = find_retroarch_exe(self.script_dir)
        self._refresh_libretro_core_path()
        if self.libretro_core_path:
            print(f"[Core] N64 libretro core: {self.libretro_core_path}")
        if self.retroarch_exe:
            print(f"[Core] RetroArch: {self.retroarch_exe}")
        else:
            print("[Core] RetroArch not found on PATH or common install locations.")
            if os.name == "nt":
                if self._auto_fetch_retroarch_var.get():
                    print(
                        "[Core] If a download does not start, use File → Download RetroArch (portable). "
                        "Extraction needs 7-Zip or: pip install py7zr"
                    )
                else:
                    print(
                        "[Core] Portable RetroArch auto-download is off — enable it in Options, "
                        "or set RETROARCH_EXE to your retroarch.exe path."
                    )
        self._start_core_download_if_needed()
        if os.name == "nt" and not self.retroarch_exe:
            self._start_retroarch_download_if_needed()

    def _refresh_libretro_core_path(self) -> None:
        cores = list(discover_n64_cores(self.script_dir))
        if not cores:
            ext = _libretro_ext()
            try:
                for f in sorted(self.cores_dir.glob(f"*_libretro.{ext}")):
                    n = f.name.lower()
                    if any(k in n for k in ("n64", "mupen", "parallel")):
                        cores.append(f.resolve())
            except OSError:
                pass
        self.libretro_core_path = cores[0] if cores else None

    def _write_retroarch_overlay_cfg(self) -> Path:
        """Minimal RetroArch overrides: windowed + libretro dir (helps cores load in embedded window)."""
        p = Path(self.script_dir) / ".cathle0.1a_retroarch_overlay.cfg"
        cores = self.cores_dir.resolve().as_posix()
        p.write_text(
            "\n".join(
                (
                    'video_fullscreen = "false"',
                    'video_windowed_fullscreen = "false"',
                    'rgui_show_start_screen = "false"',
                    f'libretro_directory = "{cores}"',
                    'video_scale_integer = "false"',
                    'pause_nonactive = "false"',
                )
            )
            + "\n",
            encoding="utf-8",
        )
        return p

    def _retroarch_argv(self, rom_abs: str) -> list[str]:
        cfg = self._write_retroarch_overlay_cfg()
        return [
            str(self.retroarch_exe),
            "--appendconfig",
            str(cfg),
            "-L",
            str(self.libretro_core_path.resolve()),
            rom_abs,
        ]

    def _maybe_auto_play_retroarch(self) -> None:
        """After ROM load with auto-start on, defer Start: RetroArch if core is ready, else toy CPU.

        Previously we only scheduled Start when both RetroArch and a core existed; then the canvas
        stayed on the static IPL3/header boot hint forever while the core downloaded or was missing.
        """
        if not self._auto_play_var.get() or not self.rom_path:
            return
        if self._ext_proc is not None and self._ext_proc.poll() is None:
            return
        self._refresh_libretro_core_path()
        self.retroarch_exe = find_retroarch_exe(self.script_dir)
        self.root.after(100, self.start_emulation)

    def _start_core_download_if_needed(self) -> None:
        if self.libretro_core_path:
            return
        if self._core_download_thread and self._core_download_thread.is_alive():
            return

        def worker() -> None:
            last_err = ""
            for name in _DEFAULT_N64_CORES:

                def prog(done: int, tot: int, msg: str, _name: str = name) -> None:
                    def ui() -> None:
                        if tot:
                            pct = min(100, 100 * done // max(tot, 1))
                            self.status_label.config(text=f"Downloading {_name}… {pct}%")
                        else:
                            self.status_label.config(text=f"Downloading {_name}… {msg}")

                    self._gui_safe(ui)

                path, err = download_n64_core(name, self.cores_dir, prog=prog)
                if err:
                    last_err = err
                if path:

                    def ok() -> None:
                        self._refresh_libretro_core_path()
                        p = self.libretro_core_path or path
                        self.status_label.config(text=f"Libretro N64 core ready: {p.name}")
                        print(f"[Core] Installed {p}")
                        self._start_retroarch_download_if_needed()
                        self._maybe_auto_play_retroarch()

                    self._gui_safe(ok)
                    return

            def fail(le: str = last_err) -> None:
                self.status_label.config(
                    text="N64 core download failed — toy CPU only. Try File → Download N64 core."
                )
                print(f"[Core] All buildbot downloads failed: {le}")

            self._gui_safe(fail)

        self._core_download_thread = threading.Thread(target=worker, daemon=True)
        self._core_download_thread.start()

    def _start_retroarch_download_if_needed(self, *, force: bool = False) -> None:
        if os.name != "nt":
            return
        if not force and not self._auto_fetch_retroarch_var.get():
            return
        self.retroarch_exe = find_retroarch_exe(self.script_dir)
        if self.retroarch_exe:
            return
        if self._retroarch_download_thread and self._retroarch_download_thread.is_alive():
            return

        print("[Core] Queuing portable RetroArch download from buildbot (~200 MB)…")

        sd = self.script_dir

        def worker() -> None:
            def prog(done: int, tot: int, msg: str) -> None:
                def ui() -> None:
                    if tot:
                        pct = min(100, 100 * done // max(tot, 1))
                        self.status_label.config(text=f"Downloading RetroArch (portable)… {pct}%")
                    else:
                        self.status_label.config(text=f"Downloading RetroArch… {msg}")

                self._gui_safe(ui)

            path, err = download_retroarch_portable_windows(sd, prog=prog)
            if path:

                def ok() -> None:
                    self.retroarch_exe = find_retroarch_exe(self.script_dir)
                    ex = self.retroarch_exe or path
                    self.status_label.config(text=f"RetroArch ready — {ex.name}")
                    print(f"[Core] RetroArch installed: {ex}")
                    self._maybe_auto_play_retroarch()

                self._gui_safe(ok)
            else:

                def fail() -> None:
                    short = (err[:140] + "…") if len(err) > 140 else err
                    self.status_label.config(text=f"RetroArch download failed — {short}")
                    print(f"[Core] RetroArch download failed: {err}")

                self._gui_safe(fail)

        self._retroarch_download_thread = threading.Thread(target=worker, daemon=True)
        self._retroarch_download_thread.start()

    def download_retroarch_manual(self) -> None:
        if os.name != "nt":
            messagebox.showinfo("RetroArch", "Portable download is only implemented on Windows.")
            return
        if find_retroarch_exe(self.script_dir):
            messagebox.showinfo("RetroArch", "RetroArch is already detected.")
            return
        if self._retroarch_download_thread and self._retroarch_download_thread.is_alive():
            messagebox.showinfo("RetroArch", "A RetroArch download is already in progress.")
            return
        self._start_retroarch_download_if_needed(force=True)
        messagebox.showinfo("RetroArch", "Download started (~200 MB). Watch the status bar.")

    def download_n64_core_manual(self) -> None:
        self.cores_dir.mkdir(parents=True, exist_ok=True)
        self._refresh_libretro_core_path()
        if self.libretro_core_path:
            messagebox.showinfo("N64 core", f"Already installed:\n{self.libretro_core_path}")
            return
        if self._core_download_thread and self._core_download_thread.is_alive():
            messagebox.showinfo("N64 core", "A download is already in progress.")
            return
        self._start_core_download_if_needed()
        messagebox.showinfo("N64 core", "Download started in the background. Watch the status bar.")

    def _skip_wizard_marker_path(self) -> Path:
        return Path(self.script_dir) / CATHLE0_1A_SKIP_WIZARD

    def _should_offer_auto_setup_wizard(self) -> bool:
        """True when first-run setup is useful (missing core and/or missing RetroArch on Windows)."""
        if os.environ.get("CATHLE_NO_SETUP_WIZARD", "").strip():
            return False
        if self._skip_wizard_marker_path().is_file():
            return False
        self._refresh_libretro_core_path()
        self.retroarch_exe = find_retroarch_exe(self.script_dir)
        if not self.libretro_core_path:
            return True
        if os.name == "nt" and not self.retroarch_exe:
            return True
        return False

    def _maybe_open_setup_wizard(self) -> None:
        if not self._should_offer_auto_setup_wizard():
            return
        w = self._setup_wizard_window
        if w is not None and w.winfo_exists():
            return
        self._open_setup_wizard(auto=True)

    def open_setup_wizard_menu(self) -> None:
        self._open_setup_wizard(auto=False)

    def _open_setup_wizard(self, *, auto: bool) -> None:
        w = self._setup_wizard_window
        if w is not None and w.winfo_exists():
            try:
                w.lift()
                w.focus_force()
            except tk.TclError:
                pass
            return

        top = tk.Toplevel(self.root)
        self._setup_wizard_window = top
        top.title("cathle — first-time setup (core + RetroArch)")
        top.configure(bg="#d4d0c8")
        top.transient(self.root)
        top.grab_set()
        top.geometry("640x520")
        skip_var = tk.BooleanVar(master=top, value=False)

        intro = (
            "cathle did not find everything needed for libretro N64 + RetroArch.\n\n"
            "On Windows, “Install everything” downloads an N64 core from the Libretro buildbot, "
            "then the official portable RetroArch package (~200 MB). Extraction needs 7-Zip or "
            "`pip install py7zr`.\n\n"
            "Set environment variable CATHLE_NO_SETUP_WIZARD=1 to skip this dialog. "
            "Checking “Don’t show again” creates a small marker file next to this script."
            if auto
            else "Install or refresh the N64 libretro core and portable RetroArch (Windows). "
            "You can leave this window open while downloads run; messages appear in the log below."
        )
        tk.Label(top, text=intro, bg="#d4d0c8", font=("Arial", 10), justify="left", wraplength=600).pack(
            anchor="w", padx=12, pady=(12, 6)
        )
        status = tk.Label(top, text="Ready.", bg="#d4d0c8", font=("Arial", 10, "bold"))
        status.pack(anchor="w", padx=12, pady=4)
        log = scrolledtext.ScrolledText(top, height=14, width=78, font=("Consolas", 9), state="disabled")
        log.pack(fill="both", expand=True, padx=12, pady=6)

        def log_line(msg: str) -> None:
            def _append() -> None:
                log.configure(state="normal")
                log.insert("end", msg + "\n")
                log.see("end")
                log.configure(state="disabled")

            self._gui_safe(_append)

        def set_status(t: str) -> None:
            self._gui_safe(lambda: status.config(text=t))

        install_btn = tk.Button(top, text="Install everything", bg="#c0c0c0", font=("Arial", 10, "bold"))

        def run_install() -> None:
            install_btn.configure(state="disabled")

            def worker() -> None:
                log_line("— Setup: starting —")
                self.cores_dir.mkdir(parents=True, exist_ok=True)
                ct = self._core_download_thread
                if ct and ct.is_alive():
                    log_line("Waiting for in-progress N64 core download (from startup)…")
                    ct.join(timeout=300.0)
                self._refresh_libretro_core_path()
                last_core_err = ""
                if not self.libretro_core_path:
                    for name in _DEFAULT_N64_CORES:

                        def prog(done: int, tot: int, msg: str, _name: str = name) -> None:
                            if tot:
                                pct = min(100, 100 * done // max(tot, 1))
                                set_status(f"N64 core {_name}: {pct}%")
                            else:
                                set_status(f"N64 core {_name}: {msg}")
                            log_line(f"[core:{_name}] {msg}" + (f" ({done}/{tot})" if tot else ""))

                        path, err = download_n64_core(name, self.cores_dir, prog=prog)
                        if err:
                            last_core_err = err
                            log_line(f"[core:{name}] error: {err}")
                        if path:
                            log_line(f"[core:{name}] installed: {path}")

                            def ok_core() -> None:
                                self._refresh_libretro_core_path()
                                p = self.libretro_core_path or path
                                set_status(f"N64 core ready: {p.name}")
                                self.status_label.config(text=f"Libretro N64 core ready: {p.name}")

                            self._gui_safe(ok_core)
                            break
                else:
                    log_line(f"N64 core already present: {self.libretro_core_path}")

                self.retroarch_exe = find_retroarch_exe(self.script_dir)
                if os.name != "nt":
                    log_line("Portable RetroArch download is only implemented on Windows; install RetroArch separately.")
                    set_status("Done (non-Windows: install RetroArch via your OS).")
                elif self.retroarch_exe:
                    log_line(f"RetroArch already detected: {self.retroarch_exe}")
                    set_status("Done.")
                else:
                    rt = self._retroarch_download_thread
                    if rt and rt.is_alive():
                        log_line("Waiting for in-progress RetroArch download (from startup)…")
                        rt.join(timeout=900.0)
                    self.retroarch_exe = find_retroarch_exe(self.script_dir)
                    if self.retroarch_exe:
                        log_line(f"RetroArch ready: {self.retroarch_exe}")
                        set_status("Done.")
                    else:

                        def prog_ra(done: int, tot: int, msg: str) -> None:
                            if tot:
                                pct = min(100, 100 * done // max(tot, 1))
                                set_status(f"RetroArch (portable): {pct}%")
                            else:
                                set_status(f"RetroArch: {msg}")
                            log_line(f"[retroarch] {msg}" + (f" ({done}/{tot})" if tot else ""))

                        path_ra, err_ra = download_retroarch_portable_windows(self.script_dir, prog=prog_ra)
                        if path_ra:
                            log_line(f"RetroArch installed: {path_ra}")

                            def ok_ra() -> None:
                                self.retroarch_exe = find_retroarch_exe(self.script_dir)
                                ex = self.retroarch_exe or path_ra
                                set_status(f"RetroArch ready — {ex.name}")
                                self.status_label.config(text=f"RetroArch ready — {ex.name}")

                            self._gui_safe(ok_ra)
                        else:
                            log_line(f"RetroArch download failed: {err_ra}")
                            set_status("RetroArch download failed — see log.")

                def finish() -> None:
                    self._refresh_libretro_core_path()
                    self.retroarch_exe = find_retroarch_exe(self.script_dir)
                    install_btn.configure(state="normal")
                    if not self.libretro_core_path:
                        set_status(
                            "N64 core still missing — see log / File → Download N64 core."
                            + (f" Last error: {last_core_err}" if last_core_err else "")
                        )
                    log_line("— Setup: finished —")
                    self._maybe_auto_play_retroarch()

                self._gui_safe(finish)

            threading.Thread(target=worker, daemon=True).start()

        install_btn.configure(command=run_install)
        install_btn.pack(pady=(4, 2))

        def on_close() -> None:
            if skip_var.get():
                try:
                    self._skip_wizard_marker_path().write_text(
                        "Skip first-time setup wizard (delete this file to show again).\n", encoding="utf-8"
                    )
                except OSError as e:
                    messagebox.showwarning("Setup wizard", f"Could not write skip marker: {e}")
            try:
                top.grab_release()
            except tk.TclError:
                pass
            top.destroy()
            self._setup_wizard_window = None

        btn_row = tk.Frame(top, bg="#d4d0c8")
        btn_row.pack(fill="x", padx=12, pady=(4, 12))
        tk.Button(btn_row, text="Close", command=on_close, bg="#c0c0c0", font=("Arial", 10)).pack(side="right")
        tk.Checkbutton(
            btn_row,
            text="Don’t show this setup wizard again",
            variable=skip_var,
            bg="#d4d0c8",
            font=("Arial", 9),
        ).pack(side="left")
        top.protocol("WM_DELETE_WINDOW", on_close)

    def _poll_external_emulator(self) -> None:
        if not self._use_external or not self._ext_proc:
            return
        code = self._ext_proc.poll()
        if code is None:
            self.root.after(1000, self._poll_external_emulator)
        else:
            self._detach_embedded_retroarch_win32()
            self._ext_proc = None
            self._use_external = False
            self.is_running = False

            def done() -> None:
                self.status_label.config(text=f"RetroArch exited (code {code}).")
                self.bind_controls()

            self._gui_safe(done)
            print(f"[Emulation] RetroArch process ended: {code}")

    def _ra_embed_on_configure(self, event: tk.Event) -> None:
        if not self._ra_child_hwnd or not self._use_external or os.name != "nt":
            return
        if event.widget is not self.canvas:
            return
        try:
            ctypes.windll.user32.MoveWindow(int(self._ra_child_hwnd), 0, 0, int(event.width), int(event.height), True)
        except OSError:
            pass

    def _detach_embedded_retroarch_win32(self) -> None:
        if os.name != "nt" or not self._ra_child_hwnd:
            return
        user32 = ctypes.windll.user32
        hwnd = int(self._ra_child_hwnd)
        self._ra_child_hwnd = None
        if self._ra_cfg_bind:
            try:
                self.canvas.unbind("<Configure>", self._ra_cfg_bind)
            except tk.TclError:
                pass
            self._ra_cfg_bind = None
        try:
            user32.SetParent(hwnd, user32.GetDesktopWindow())
        except Exception as e:
            print(f"[Emulation] Detach RetroArch window: {e}")

    def _child_hwnds_ra(self, user32, parent: int) -> list[int]:
        acc: list[int] = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def cb(hwnd, _lp):
            acc.append(int(hwnd))
            return True

        user32.EnumChildWindows(int(parent), cb, 0)
        return acc

    def _hwnd_area_ra(self, user32, hwnd: int) -> int:
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return 0
        w = max(0, int(rect.right - rect.left))
        h = max(0, int(rect.bottom - rect.top))
        return w * h

    def _best_ra_embed_hwnd(self, user32, top_hwnd: int) -> int:
        descendants: list[int] = []

        def walk(parent: int, depth: int) -> None:
            if depth > 10:
                return
            for ch in self._child_hwnds_ra(user32, parent):
                descendants.append(ch)
                walk(ch, depth + 1)

        walk(int(top_hwnd), 0)
        ta = self._hwnd_area_ra(user32, top_hwnd)
        best = int(top_hwnd)
        best_a = ta
        for ch in descendants:
            if not user32.IsWindowVisible(ch):
                continue
            ca = self._hwnd_area_ra(user32, ch)
            if ca > best_a and ca > 40_000:
                best = ch
                best_a = ca
        return best

    def _retroarch_ui_tick(self) -> None:
        if not self._use_external or self._ext_proc is None:
            return
        if self._ext_proc.poll() is not None:
            self.canvas.delete("boot_hint")
            return
        self.canvas.delete("boot_hint")
        if not self._ra_child_hwnd:
            font_m = ("Segoe UI", 12) if sys.platform == "win32" else ("Arial", 12)
            font_s = ("Segoe UI", 9) if sys.platform == "win32" else ("Arial", 9)
            self.canvas.create_text(
                320,
                185,
                text="Starting RetroArch…",
                fill="#cccccc",
                font=font_m,
                tags=("boot_hint",),
            )
            self.canvas.create_text(
                320,
                230,
                text="If this panel stays blank, check the taskbar for a separate RetroArch window.",
                fill="#888899",
                font=font_s,
                width=560,
                tags=("boot_hint",),
            )
            self.canvas.create_text(
                320,
                295,
                text=self.rom_title[:48],
                fill="#666677",
                font=font_s,
                tags=("boot_hint",),
            )
        if self._use_external and self._ext_proc.poll() is None and not self._ra_child_hwnd:
            self.root.after(350, self._retroarch_ui_tick)

    def _embed_retroarch_in_canvas_win32(self, attempt: int = 0) -> None:
        if os.name != "nt":
            return
        if not self._use_external or self._ext_proc is None:
            return
        if self._ext_proc.poll() is not None:
            return
        if self._ra_child_hwnd:
            return

        user32 = ctypes.windll.user32
        pid_target = int(self._ext_proc.pid)
        matches: list[tuple[int, str, int]] = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def _enum_cb(hwnd, _lp):
            if not user32.IsWindowVisible(hwnd):
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if int(pid.value) != pid_target:
                return True
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            w = int(rect.right - rect.left)
            h = int(rect.bottom - rect.top)
            if w < 80 or h < 60:
                return True
            n = user32.GetWindowTextLengthW(hwnd) + 1
            buf = ctypes.create_unicode_buffer(max(n, 2))
            user32.GetWindowTextW(hwnd, buf, n)
            title = buf.value or ""
            matches.append((int(hwnd), title, w * h))
            return True

        user32.EnumWindows(_enum_cb, 0)

        if not matches:
            if attempt < 35:
                self.root.after(180, lambda: self._embed_retroarch_in_canvas_win32(attempt + 1))
            else:

                def warn() -> None:
                    self.status_label.config(
                        text="RetroArch is running in its own window (could not attach to canvas)."
                    )

                self._gui_safe(warn)
            return

        matches.sort(key=lambda x: -x[2])
        top_hwnd = matches[0][0]
        hwnd = self._best_ra_embed_hwnd(user32, top_hwnd)
        self.root.update_idletasks()
        try:
            canvas_hwnd = int(self.canvas.winfo_id())
        except tk.TclError:
            return
        cw = max(int(self.canvas.winfo_width()), 2)
        ch = max(int(self.canvas.winfo_height()), 2)

        GWL_STYLE = -16
        WS_CHILD = 0x40000000
        WS_VISIBLE = 0x10000000
        WS_POPUP = 0x80000000
        WS_CAPTION = 0x00C00000
        WS_THICKFRAME = 0x00040000
        try:
            get_long = user32.GetWindowLongPtrW
            set_long = user32.SetWindowLongPtrW
        except AttributeError:
            get_long = user32.GetWindowLongW
            set_long = user32.SetWindowLongW

        try:
            user32.SetParent(hwnd, canvas_hwnd)
            style = int(get_long(hwnd, GWL_STYLE))
            style = (style | WS_CHILD | WS_VISIBLE) & ~(WS_POPUP | WS_CAPTION | WS_THICKFRAME)
            set_long(hwnd, GWL_STYLE, style)
            user32.MoveWindow(hwnd, 0, 0, cw, ch, True)
            user32.ShowWindow(hwnd, 9)
        except OSError as e:
            print(f"[Emulation] Embed RetroArch: {e}")
            if attempt < 12:
                self.root.after(350, lambda: self._embed_retroarch_in_canvas_win32(attempt + 1))
            return

        self._ra_child_hwnd = hwnd
        if self._ra_cfg_bind:
            try:
                self.canvas.unbind("<Configure>", self._ra_cfg_bind)
            except tk.TclError:
                pass
        self._ra_cfg_bind = self.canvas.bind("<Configure>", self._ra_embed_on_configure, add="+")

        def ok() -> None:
            self.canvas.delete("boot_hint")
            t = matches[0][1] or self.rom_title
            self.status_label.config(text=f"RetroArch in canvas — {t}")
            if os.name == "nt" and self._ra_child_hwnd:
                try:
                    u = ctypes.windll.user32
                    h = int(self._ra_child_hwnd)
                    u.SetFocus(h)
                    u.BringWindowToTop(h)
                except Exception:
                    pass

        self._gui_safe(ok)
        print(f"[Emulation] Embedded RetroArch HWND {hwnd} (from top {top_hwnd}) into canvas {canvas_hwnd}")

    def setup_gui(self):
        menubar = tk.Menu(self.root, bg="#d4d0c8", font=("Arial", 10))
        file_menu = tk.Menu(menubar, tearoff=0, bg="#d4d0c8", font=("Arial", 10))
        file_menu.add_command(label="Open ROM...", command=self.load_rom)
        file_menu.add_command(label="ROM Information", command=self.show_rom_info)
        file_menu.add_command(label="Open 64DD Disk", command=self.load_64dd_disk)
        file_menu.add_command(label="Toggle Test Pattern", command=self.toggle_test_pattern)
        file_menu.add_command(label="Download N64 core…", command=self.download_n64_core_manual)
        file_menu.add_command(label="Download RetroArch (portable, ~200MB)…", command=self.download_retroarch_manual)
        file_menu.add_command(label="Setup wizard (install core + RetroArch)…", command=self.open_setup_wizard_menu)
        file_menu.add_separator()
        file_menu.add_command(label="End Emulation", command=self.stop_emulation)
        file_menu.add_command(label="Exit", command=self.root.quit)
        menubar.add_cascade(label="File", menu=file_menu)
        system_menu = tk.Menu(menubar, tearoff=0, bg="#d4d0c8", font=("Arial", 10))
        system_menu.add_command(label="Save State", command=self.save_state)
        system_menu.add_command(label="Load State", command=self.load_state)
        system_menu.add_command(label="Pause", command=self.pause_emulation)
        system_menu.add_command(label="Reset", command=self.reset_emulation)
        system_menu.add_command(label="Soft Reset", command=self.soft_reset)
        menubar.add_cascade(label="System", menu=system_menu)
        options_menu = tk.Menu(menubar, tearoff=0, bg="#d4d0c8", font=("Arial", 10))
        options_menu.add_command(label="Configure Controller", command=self.configure_controller)
        options_menu.add_command(label="Set Emulation Speed", command=self.set_emulation_speed)
        options_menu.add_command(label="Cheat Codes", command=self.add_cheat_code)
        options_menu.add_command(label="Select Plugins", command=self.select_plugins)
        options_menu.add_command(label="Enable Netplay", command=self.toggle_netplay)
        options_menu.add_checkbutton(
            label="Auto-start RetroArch when ROM loads",
            variable=self._auto_play_var,
        )
        options_menu.add_checkbutton(
            label="Auto-download portable RetroArch (Windows) if missing",
            variable=self._auto_fetch_retroarch_var,
        )
        menubar.add_cascade(label="Options", menu=options_menu)
        self.root.config(menu=menubar)
        main_frame = tk.Frame(self.root, bg="#d4d0c8")
        main_frame.pack(fill="both", expand=True, padx=10, pady=10)
        browser_frame = tk.Frame(main_frame, bg="#d4d0c8", relief="sunken", borderwidth=2)
        browser_frame.pack(fill="x", padx=5, pady=5)
        tk.Label(browser_frame, text="ROM Browser", bg="#d4d0c8", font=("Arial", 10, "bold")).pack(anchor="w")
        self.rom_listbox = tk.Listbox(browser_frame, height=5, bg="white", font=("Arial", 10))
        self.rom_listbox.pack(fill="x", padx=5, pady=5)
        self.rom_listbox.bind("<<ListboxSelect>>", self.select_rom)
        self.canvas = tk.Canvas(main_frame, width=640, height=480, bg="black", relief="sunken", borderwidth=2)
        self.canvas.pack(pady=10)
        self.canvas.bind("<Button-1>", self._on_canvas_click_focus_ra, add="+")
        self.rcp = RealityCoprocessor(self.canvas)
        self.canvas.frame_count = self.frame_count
        control_frame = tk.Frame(main_frame, bg="#d4d0c8")
        control_frame.pack(fill="x")
        tk.Button(control_frame, text="Start", command=self.start_emulation, bg="#c0c0c0", relief="raised", font=("Arial", 10)).pack(side="left", padx=5)
        tk.Button(control_frame, text="Pause", command=self.pause_emulation, bg="#c0c0c0", relief="raised", font=("Arial", 10)).pack(side="left", padx=5)
        tk.Button(control_frame, text="Stop", command=self.stop_emulation, bg="#c0c0c0", relief="raised", font=("Arial", 10)).pack(side="left", padx=5)
        self.status_label = tk.Label(main_frame, text="No ROM loaded", bg="#d4d0c8", font=("Arial", 10))
        self.status_label.pack(pady=5)
        self.bind_controls()
        self.update_rom_browser()

    def _gui_safe(self, fn):
        """Run Tk updates on the main thread only (never call Tk from worker threads)."""
        if threading.current_thread() is threading.main_thread():
            fn()
        else:
            self.root.after(0, fn)

    def update_rom_browser(self):
        self.rom_listbox.delete(0, tk.END)
        for file in os.listdir(self.script_dir):
            if file.lower().endswith((".n64", ".z64", ".v64", ".ndd")):
                self.rom_listbox.insert(tk.END, file)

    def select_rom(self, event):
        selection = self.rom_listbox.curselection()
        if selection:
            file_name = self.rom_listbox.get(selection[0])
            self.rom_path = os.path.join(self.script_dir, file_name)
            if file_name.lower().endswith(".ndd"):
                self.load_64dd_disk()
            else:
                self.load_rom()

    def bind_controls(self):
        for seq in getattr(self, "_control_bindings", ()):
            try:
                self.root.unbind(seq)
            except tk.TclError:
                pass
        self._control_bindings = []
        for action, key in self.controller_config.items():
            seq = f"<KeyPress-{key}>"
            self.root.bind(seq, lambda e, a=action: self.handle_input(a))
            self._control_bindings.append(seq)

    def _suspend_kb_for_ra(self) -> None:
        """Tk root key bindings steal WM_KEY* from embedded RetroArch; drop them while RA runs."""
        for seq in getattr(self, "_control_bindings", ()):
            try:
                self.root.unbind(seq)
            except tk.TclError:
                pass
        self._control_bindings = []

    def _on_canvas_click_focus_ra(self, event) -> None:
        if os.name != "nt" or not self._use_external or not self._ra_child_hwnd:
            return
        try:
            u = ctypes.windll.user32
            u.SetFocus(int(self._ra_child_hwnd))
            u.BringWindowToTop(int(self._ra_child_hwnd))
        except Exception:
            pass

    def handle_input(self, action):
        if self._use_external:
            return
        if self.is_running:
            self.cpu_registers[5] = hash(action) % 0xFFFF
            self.status_label.config(text=f"Input: {action} pressed! | Game: {self.rom_title}")
            print(f"[Input] {action} pressed, r[5] = {self.cpu_registers[5]}")
            if self.netplay_enabled and self.netplay_socket:
                try:
                    self.netplay_socket.send(action.encode())
                except OSError:
                    print("[Netplay] Input send failed")

    def show_rom_info(self):
        if self.rom_path:
            messagebox.showinfo("ROM Info", f"Title: {self.rom_title}\nPath: {self.rom_path}\nRDRAM Size: {self.rdram_size//1024}KB\nROM Size: {len(self.rom_data)} bytes\nMD5: {self.rom_md5}")
        else:
            messagebox.showwarning("No ROM", "No ROM or 64DD disk loaded, CatSama!")

    def toggle_test_pattern(self):
        self.rcp.use_test_pattern = not self.rcp.use_test_pattern
        state = "enabled" if self.rcp.use_test_pattern else "disabled"
        messagebox.showinfo("Test Pattern", f"Test pattern {state}, CatSama!")
        print(f"[RDP] Test pattern {state}")

    def load_rom(self):
        if not self.rom_path:
            self.rom_path = filedialog.askopenfilename(filetypes=[("N64 ROMs", "*.n64 *.z64 *.v64")])
        if self.rom_path:
            try:
                self.stop_emulation()
                with open(self.rom_path, 'rb') as f:
                    self.rom_data = f.read()
                if len(self.rom_data) < 64:
                    raise ValueError("ROM too small")
                print(f"[ROM] Loaded ROM: {self.rom_path}, Size: {len(self.rom_data)} bytes")
                header = self.rom_data[:64]
                if header[0:4] == b'\x80\x37\x12\x40':
                    print("[ROM] Big-endian (.z64)")
                elif header[0:4] == b'\x37\x80\x40\x12':
                    bswap = bytearray(self.rom_data)
                    for i in range(0, len(bswap) - 1, 2):
                        bswap[i], bswap[i + 1] = bswap[i + 1], bswap[i]
                    self.rom_data = bytes(bswap)
                    print("[ROM] Byte-swapped (.v64)")
                elif header[0:4] == b'\x40\x12\x37\x80':
                    self.rom_data = self.rom_data[::-1]
                    print("[ROM] Little-endian (.n64)")
                else:
                    raise ValueError("Invalid N64 ROM header")
                self.rom_title = self.rom_data[0x20:0x34].decode('ascii', errors='ignore').strip() or os.path.basename(self.rom_path)
                self.rom_md5 = hashlib.md5(self.rom_data).hexdigest()
                print(f"[ROM] Title: {self.rom_title}, MD5: {self.rom_md5}")
                self.status_label.config(text=f"ROM Loaded: {self.rom_title}")
                self.canvas.delete("all")
                self.rdram = [0] * self.rdram_size
                self.rdram_bytes[:] = b"\x00" * self.rdram_size
                self._is_sm64_rom = "mario" in self.rom_title.lower()
                self._is_commercial_cart = True
                self._parse_rom_header_boot()
                self._apply_commercial_boot(clear_fb=True)
                print(f"[ROM] vu_registers: {self.rcp.vu_registers[:3]}")
                self._gui_safe(lambda: self.rcp.rdp_render())
                self._maybe_auto_play_retroarch()
            except Exception as e:
                messagebox.showerror("ROM Error", f"Failed to load ROM: {str(e)}")
                print(f"[ROM] Error: {str(e)}")
                self.rom_path = None
                self.rom_data = b""
                self.rom_md5 = ""
                self._is_sm64_rom = False
                self._is_commercial_cart = False

    def load_64dd_disk(self):
        if not self.rom_path:
            self.rom_path = filedialog.askopenfilename(filetypes=[("64DD Disks", "*.ndd")])
        if self.rom_path:
            try:
                with open(self.rom_path, 'rb') as f:
                    self.rom_data = f.read()
                self.rom_title = os.path.basename(self.rom_path)
                print(f"[64DD] Loaded disk: {self.rom_title}, Size: {len(self.rom_data)} bytes")
                self.status_label.config(text=f"64DD Disk Loaded: {self.rom_title}")
                self.canvas.delete("all")
                self.pc = 0x1000
                self.rcp.vu_registers[0] = random.randint(1, 255)
                self.rcp.vu_registers[1] = random.randint(1, 255)
                self.rcp.vu_registers[2] = random.randint(1, 255)
                print(f"[64DD] vu_registers: {self.rcp.vu_registers[:3]}")
            except Exception as e:
                messagebox.showerror("64DD Error", f"Failed to load 64DD disk: {str(e)}")
                print(f"[64DD] Error: {str(e)}")
                self.rom_path = None

    def start_emulation(self):
        if not self.rom_path:
            messagebox.showwarning("No ROM", "Please load a ROM or 64DD disk first, CatSama!")
            return
        self.stop_emulation()
        self._refresh_libretro_core_path()
        self.retroarch_exe = find_retroarch_exe(self.script_dir)
        if self.retroarch_exe and self.libretro_core_path and self.rom_path:
            try:
                self.root.update_idletasks()
                rom_abs = str(Path(self.rom_path).resolve())
                argv = self._retroarch_argv(rom_abs)
                self._ext_proc = subprocess.Popen(
                    argv,
                    cwd=str(self.retroarch_exe.parent),
                )
                self._use_external = True
                self.is_running = True
                self._suspend_kb_for_ra()
                self.status_label.config(text=f"Starting RetroArch ({self.libretro_core_path.name})…")
                print(f"[Emulation] RetroArch: {' '.join(argv)}")
                self.root.after(120, self._retroarch_ui_tick)
                self.root.after(280, lambda: self._embed_retroarch_in_canvas_win32(0))
                self.root.after(500, self._poll_external_emulator)
            except Exception as e:
                self._ext_proc = None
                self._use_external = False
                self.is_running = False
                messagebox.showerror("RetroArch", f"Could not start RetroArch:\n{e}")
                print(f"[Emulation] RetroArch launch failed: {e}")
            return
        if not self.libretro_core_path:
            self.status_label.config(
                text="No libretro N64 core yet — using toy CPU (download running or failed)."
            )
        elif not self.retroarch_exe:
            self.status_label.config(text="RetroArch not found — using toy CPU in this window.")
        if not self.is_running:
            self.is_running = True
            self.emulation_thread = threading.Thread(target=self.emulation_loop)
            self.emulation_thread.daemon = True
            self.emulation_thread.start()
            self.status_label.config(text=f"Toy CPU: {self.rom_title}")
            print("[Emulation] Started (internal toy CPU)")

    def pause_emulation(self):
        if self._use_external:
            where = "the canvas" if (os.name == "nt" and self._ra_child_hwnd) else "its window"
            messagebox.showinfo(
                "Pause",
                f"RetroArch is in {where}. Use RetroArch's menu (F1) to pause, or Stop here.",
            )
            return
        if self.is_running:
            self.is_running = False
            self.status_label.config(text="Emulation Paused")
            print("[Emulation] Paused")

    def stop_emulation(self):
        self.is_running = False
        self._detach_embedded_retroarch_win32()
        if self._ext_proc is not None:
            try:
                self._ext_proc.terminate()
                try:
                    self._ext_proc.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    self._ext_proc.kill()
            except Exception as e:
                print(f"[Emulation] Error stopping RetroArch: {e}")
            self._ext_proc = None
        self._use_external = False
        self.frame_count = 0
        self.pc = self._rom_entry_virt & ~3
        self.canvas.delete("boot_hint")
        self.canvas.delete("all")
        self.status_label.config(text="Emulation Stopped")
        print("[Emulation] Stopped")
        self.bind_controls()
        if self.netplay_socket:
            self.netplay_socket.close()
            self.netplay_socket = None

    def reset_emulation(self):
        self.stop_emulation()
        self.cpu_registers = [0] * 32
        self.rcp.vu_registers = [0] * 32
        self.rcp.rsp_memory = [0] * 0x1000
        self.rdram = [0] * self.rdram_size
        self.status_label.config(text="Emulation Reset")
        print("[Emulation] Hard Reset")
        if self._is_commercial_cart:
            self._apply_commercial_boot(clear_fb=True)
            print("[Reset] Commercial cart toy boot state restored")

    def soft_reset(self):
        """Soft reset: replay black-window boot for any commercial cartridge (toy CPU)."""
        if self._use_external:
            messagebox.showinfo("Soft Reset", "Use RetroArch's own reset while the external core is running.")
            return
        if self._is_commercial_cart:
            self._pending_soft_reset = True
            self.status_label.config(text="Soft reset — replaying cart boot (black window)…")
            print("[Emulation] Soft reset (commercial cart)")
        else:
            self._normalize_rom_pc(force_entry=True)
            self.status_label.config(text="Soft Reset")
            print("[Emulation] Soft Reset")

    @staticmethod
    def _mips_se16(imm: int) -> int:
        imm &= 0xFFFF
        return imm - 0x10000 if imm & 0x8000 else imm

    def _parse_rom_header_boot(self) -> None:
        """Read cart ROM header (big-endian .z64) for IPL3 entry PC."""
        rd = self.rom_data
        if len(rd) < 0x40:
            self._rom_entry_virt = 0x80000400
            return
        entry = struct.unpack_from(">I", rd, 8)[0]
        if (entry & 3) != 0:
            entry &= ~3
        if not (0x80000000 <= entry < 0x80800000 or 0xA0000000 <= entry < 0xA8000000):
            entry = 0x80000400
        self._rom_entry_virt = entry & 0xFFFFFFFF
        print(f"[Boot] ROM header entry PC: {hex(self._rom_entry_virt)}")

    def _ipl3_install_to_rdram(self) -> None:
        """Copy bootstrap segment from cart (file offset 0x1000) into KSEG0 RDRAM mirror (toy IPL3)."""
        self.rdram_bytes[:] = b"\x00" * self.rdram_size
        rd = self.rom_data
        chunk = min(len(rd) - 0x1000, self.rdram_size)
        if chunk <= 0:
            self._ipl_copy_len = 0
            self.pc = self._rom_entry_virt
            return
        self.rdram_bytes[0:chunk] = rd[0x1000 : 0x1000 + chunk]
        self._ipl_copy_len = chunk
        self.pc = self._rom_entry_virt & ~3
        print(f"[Boot] IPL3 copy {chunk} bytes from ROM+0x1000 → RDRAM mirror @ 0x80000000, PC={hex(self.pc)}")

    def _rdram_offset(self, addr: int) -> Optional[int]:
        """Byte offset into rdram_bytes for KSEG0/KSEG1 mirrors, or None."""
        a = addr & 0xFFFFFFFF
        if 0x80000000 <= a < 0x80000000 + self.rdram_size:
            return a - 0x80000000
        if 0xA0000000 <= a < 0xA0000000 + self.rdram_size:
            return a - 0xA0000000
        return None

    def _fetch_u32_exec(self, pc: int) -> int:
        """Fetch big-endian instruction from RDRAM mirror or cart ROM."""
        off = self._rdram_offset(pc)
        if off is not None and off + 4 <= self.rdram_size:
            return struct.unpack_from(">I", self.rdram_bytes, off)[0]
        base = 0x80000000
        fo = pc - base + 0x1000
        if 0x1000 <= fo <= len(self.rom_data) - 4:
            return struct.unpack_from(">I", self.rom_data, fo)[0]
        if 0x1000 <= pc <= len(self.rom_data) - 4:
            return struct.unpack_from(">I", self.rom_data, pc)[0]
        return 0

    def _normalize_exec_pc(self, force_entry: bool = False) -> None:
        """Keep PC in RDRAM mirrors or valid ROM file range."""
        if len(self.rom_data) < 0x1010:
            self.pc = 0
            return
        base0, base1 = 0x80000000, 0xA0000000
        top0 = base0 + self.rdram_size - 4
        top1 = base1 + self.rdram_size - 4
        if force_entry:
            self.pc = self._rom_entry_virt & ~3
            return
        if (self.pc & 3) != 0:
            self.pc = self._rom_entry_virt & ~3
            return
        if base0 <= self.pc <= top0 or base1 <= self.pc <= top1:
            return
        lo, hi = 0x1000, len(self.rom_data) - 4
        if lo <= self.pc <= hi:
            return
        self.pc = self._rom_entry_virt & ~3

    def _normalize_rom_pc(self, force_entry: bool = False) -> None:
        """Compatibility name — toy uses RDRAM-backed IPL3 boot."""
        self._normalize_exec_pc(force_entry=force_entry)

    def _apply_commercial_boot(self, clear_fb: bool = False) -> None:
        """PIF/IPL3 toy: copy bootstrap from cart into RDRAM mirror; PC from ROM header."""
        self._boot_phase = 0
        self.rcp.freeze_raster = False
        self.rcp.suppress_pixel_push = True
        self.rcp.use_test_pattern = False
        if clear_fb:
            self.rcp.framebuffer = [[0] * 320 for _ in range(240)]
        self._sm64_game_visual_done = False
        self._ipl3_install_to_rdram()
        self._normalize_exec_pc(force_entry=False)
        if self._is_sm64_rom:
            self.cpu_registers = [0] * 32
            self.cpu_registers[29] = 0x8033B400
            self.rcp.vu_registers[0] = 0xFF
            self.rcp.vu_registers[1] = 0xA0
            self.rcp.vu_registers[2] = 0x00
            self.rcp.boot_hint_text = (
                f"{self.rom_title}\nSM64 — IPL3 loaded; CPU @ {hex(self.pc)} (header entry)"
            )
            print("[ROM] SM64 toy boot: black window then RSP tint (not a real N64 core)")
        else:
            self.cpu_registers = [0] * 32
            self.cpu_registers[29] = 0x8033B400
            h = int(self.rom_md5[:8], 16) if len(self.rom_md5) >= 8 else 0
            self.rcp.vu_registers[0] = (h >> 16) & 0xFF
            self.rcp.vu_registers[1] = (h >> 8) & 0xFF
            self.rcp.vu_registers[2] = h & 0xFF
            self.rcp.boot_hint_text = (
                f"{self.rom_title}\nIPL3 segment copied — CPU @ {hex(self.pc)} (toy MIPS, not full emu)"
            )
            print("[ROM] Commercial cart toy boot: black window @ 60fps (toy shell)")

    def _paint_rom_byte_preview(self) -> None:
        rd = self.rom_data
        if len(rd) < 0x1000:
            return
        n = len(rd)
        step = max(1, n // (320 * 240))
        for j in range(240):
            row = self.rcp.framebuffer[j]
            base = (j * 320 * 17) % n
            for i in range(320):
                idx = (base + i * step) % (n - 2)
                r = rd[idx] & 0xFF
                g = rd[(idx + n // 5) % n] & 0xFF
                b = rd[(idx + 2 * n // 5) % n] & 0xFF
                row[i] = (r << 16) | (g << 8) | b

    def _commercial_boot_tick(self) -> None:
        """Per-frame boot: SM64 unlocks RSP then toy level; other carts get a ROM byte preview."""
        if not self._is_commercial_cart:
            return
        self._boot_phase += 1
        if self._is_sm64_rom:
            if self._boot_phase == 12:
                self.rcp.suppress_pixel_push = False
                self.rcp.boot_hint_text = None
            if self._boot_phase == 45:
                self._gui_safe(lambda: self.status_label.config(text="SM64: entering game (toy display)…"))
            if self._boot_phase >= 90:
                self.rcp.use_test_pattern = False
                if not self._sm64_game_visual_done:
                    self._sm64_game_visual_done = True
                    for y in range(240):
                        for x in range(320):
                            sky_b = min(255, 90 + int(y * 0.55))
                            sky_g = min(255, 120 + int(y * 0.35))
                            if y >= 168:
                                gnd = min(255, 40 + (y - 168) * 8 + (x % 17))
                                r, g, b = gnd, gnd // 2, gnd // 4
                            else:
                                r, g, b = 32, sky_g, sky_b
                            self.rcp.framebuffer[y][x] = (r << 16) | (g << 8) | b
                    self.rcp.vu_registers[0] = 0x40
                    self.rcp.vu_registers[1] = 0xA0
                    self.rcp.vu_registers[2] = 0xE0
                    self.rcp.freeze_raster = True
                    self.rcp.boot_hint_text = None
                    print("[SM64] Toy in-game frame (use a real N64 core for actual gameplay)")
        else:
            self.rcp.suppress_pixel_push = True
            if self._boot_phase == 1:
                t = self.rom_title[:28]
                self._gui_safe(
                    lambda tt=t: self.status_label.config(
                        text=f"{tt} — boot (black) @ {int(TARGET_FPS)}fps toy"
                    )
                )
            if self._boot_phase == 36:
                self._paint_rom_byte_preview()
                self.rcp.suppress_pixel_push = False
                self.rcp.boot_hint_text = None
                self._gui_safe(
                    lambda: self.status_label.config(
                        text=f"{self.rom_title[:32]} — ROM byte preview (toy, not real video)"
                    )
                )

    def emulation_loop(self):
        dynarec_cache: dict[int, object] = {}
        while self.is_running and len(self.rom_data) >= 0x1010:
            loop_t0 = time.perf_counter()
            if self._pending_soft_reset:
                self._pending_soft_reset = False
                dynarec_cache.clear()
                self._apply_commercial_boot(clear_fb=True)
                print("[CPU] Soft reset — dynarec cleared, commercial boot replay")

            self.frame_count += 1
            try:
                self._normalize_rom_pc()
                pc_fetch = self.pc
                opcode = self._fetch_u32_exec(pc_fetch)
                op = (opcode >> 26) & 0x3F
                funct = opcode & 0x3F
                if pc_fetch not in dynarec_cache:
                    if op == 0:  # SPECIAL / R-type
                        rs = (opcode >> 21) & 0x1F
                        rt = (opcode >> 16) & 0x1F
                        rd = (opcode >> 11) & 0x1F
                        sa = (opcode >> 6) & 0x1F
                        if funct == 0:  # SLL
                            dynarec_cache[pc_fetch] = lambda rt=rt, rd=rd, sa=sa: setattr(
                                self,
                                "cpu_registers",
                                self.cpu_registers[:rd]
                                + [(self.cpu_registers[rt] << sa) & 0xFFFFFFFF]
                                + self.cpu_registers[rd + 1 :],
                            )
                        elif funct in (0x20, 0x21):  # ADD / ADDU
                            dynarec_cache[pc_fetch] = lambda rs=rs, rt=rt, rd=rd: setattr(
                                self,
                                "cpu_registers",
                                self.cpu_registers[:rd]
                                + [(self.cpu_registers[rs] + self.cpu_registers[rt]) & 0xFFFFFFFF]
                                + self.cpu_registers[rd + 1 :],
                            )
                        elif funct == 0x24:  # AND
                            dynarec_cache[pc_fetch] = lambda rs=rs, rt=rt, rd=rd: setattr(
                                self,
                                "cpu_registers",
                                self.cpu_registers[:rd]
                                + [(self.cpu_registers[rs] & self.cpu_registers[rt]) & 0xFFFFFFFF]
                                + self.cpu_registers[rd + 1 :],
                            )
                        elif funct == 0x25:  # OR
                            dynarec_cache[pc_fetch] = lambda rs=rs, rt=rt, rd=rd: setattr(
                                self,
                                "cpu_registers",
                                self.cpu_registers[:rd]
                                + [(self.cpu_registers[rs] | self.cpu_registers[rt]) & 0xFFFFFFFF]
                                + self.cpu_registers[rd + 1 :],
                            )
                        elif funct == 0x08:  # JR
                            def _jr():
                                self.pc = self.cpu_registers[rs] & 0xFFFFFFFC
                                self._normalize_exec_pc()

                            dynarec_cache[pc_fetch] = _jr
                        elif funct == 0x09:  # JALR
                            def _jalr():
                                tgt = self.cpu_registers[rs] & 0xFFFFFFFC
                                regs = list(self.cpu_registers)
                                regs[rd] = (pc_fetch + 8) & 0xFFFFFFFF
                                self.cpu_registers = regs
                                self.pc = tgt
                                self._normalize_exec_pc()

                            dynarec_cache[pc_fetch] = _jalr
                        else:
                            dynarec_cache[pc_fetch] = lambda: None
                    elif op == 0x09:  # ADDIU
                        rs = (opcode >> 21) & 0x1F
                        rt = (opcode >> 16) & 0x1F
                        imm = self._mips_se16(opcode & 0xFFFF)
                        dynarec_cache[pc_fetch] = lambda rs=rs, rt=rt, imm=imm: setattr(
                            self,
                            "cpu_registers",
                            self.cpu_registers[:rt]
                            + [(self.cpu_registers[rs] + imm) & 0xFFFFFFFF]
                            + self.cpu_registers[rt + 1 :],
                        )
                    elif op == 0x0D:  # ORI
                        rs = (opcode >> 21) & 0x1F
                        rt = (opcode >> 16) & 0x1F
                        imm = opcode & 0xFFFF
                        dynarec_cache[pc_fetch] = lambda rs=rs, rt=rt, imm=imm: setattr(
                            self,
                            "cpu_registers",
                            self.cpu_registers[:rt]
                            + [(self.cpu_registers[rs] | imm) & 0xFFFFFFFF]
                            + self.cpu_registers[rt + 1 :],
                        )
                    elif op == 0x0C:  # ANDI
                        rs = (opcode >> 21) & 0x1F
                        rt = (opcode >> 16) & 0x1F
                        imm = opcode & 0xFFFF
                        dynarec_cache[pc_fetch] = lambda rs=rs, rt=rt, imm=imm: setattr(
                            self,
                            "cpu_registers",
                            self.cpu_registers[:rt]
                            + [(self.cpu_registers[rs] & imm) & 0xFFFFFFFF]
                            + self.cpu_registers[rt + 1 :],
                        )
                    elif op == 0x04:  # BEQ
                        rs = (opcode >> 21) & 0x1F
                        rt = (opcode >> 16) & 0x1F
                        imm = self._mips_se16(opcode & 0xFFFF)
                        pc_here = pc_fetch

                        def _beq():
                            if self.cpu_registers[rs] == self.cpu_registers[rt]:
                                self.pc = (pc_here + 4 + (imm << 2)) & 0xFFFFFFFF
                            else:
                                self.pc = (pc_here + 4) & 0xFFFFFFFF
                            self._normalize_exec_pc()

                        dynarec_cache[pc_fetch] = _beq
                    elif op == 0x05:  # BNE
                        rs = (opcode >> 21) & 0x1F
                        rt = (opcode >> 16) & 0x1F
                        imm = self._mips_se16(opcode & 0xFFFF)
                        pc_here = pc_fetch

                        def _bne():
                            if self.cpu_registers[rs] != self.cpu_registers[rt]:
                                self.pc = (pc_here + 4 + (imm << 2)) & 0xFFFFFFFF
                            else:
                                self.pc = (pc_here + 4) & 0xFFFFFFFF
                            self._normalize_exec_pc()

                        dynarec_cache[pc_fetch] = _bne
                    elif op == 0x2B:  # SW
                        rs = (opcode >> 21) & 0x1F
                        rt = (opcode >> 16) & 0x1F
                        imm = self._mips_se16(opcode & 0xFFFF)

                        def _sw():
                            addr = (self.cpu_registers[rs] + imm) & 0xFFFFFFFF
                            roff = self._rdram_offset(addr)
                            if roff is not None and roff + 4 <= self.rdram_size:
                                struct.pack_into(
                                    ">I",
                                    self.rdram_bytes,
                                    roff,
                                    self.cpu_registers[rt] & 0xFFFFFFFF,
                                )
                                self.pc = (pc_fetch + 4) & 0xFFFFFFFF
                                self._normalize_exec_pc()
                            else:
                                self.rcp.rsp_execute(self.rom_data, 0x1000)
                                self.pc = (pc_fetch + 4) & 0xFFFFFFFF
                                self._normalize_exec_pc()

                        dynarec_cache[pc_fetch] = _sw
                    elif op == 0x23:  # LW
                        rs = (opcode >> 21) & 0x1F
                        rt = (opcode >> 16) & 0x1F
                        imm = self._mips_se16(opcode & 0xFFFF)

                        def _lw():
                            addr = (self.cpu_registers[rs] + imm) & 0xFFFFFFFF
                            roff = self._rdram_offset(addr)
                            if roff is not None and roff + 4 <= self.rdram_size:
                                val = struct.unpack_from(">I", self.rdram_bytes, roff)[0]
                                regs = list(self.cpu_registers)
                                regs[rt] = val
                                self.cpu_registers = regs
                            else:
                                regs = list(self.cpu_registers)
                                regs[rt] = 0
                                self.cpu_registers = regs
                            self.pc = (pc_fetch + 4) & 0xFFFFFFFF
                            self._normalize_exec_pc()

                        dynarec_cache[pc_fetch] = _lw
                    elif op == 0x02:  # J
                        target = (opcode & 0x3FFFFFF) << 2
                        pc_here = pc_fetch

                        def _j():
                            self.pc = (pc_here & 0xF0000000) | target
                            self._normalize_exec_pc()

                        dynarec_cache[pc_fetch] = _j
                    elif op == 0x03:  # JAL
                        target = (opcode & 0x3FFFFFF) << 2
                        pc_here = pc_fetch
                        ra_val = (pc_here + 8) & 0xFFFFFFFF
                        new_pc = (pc_here & 0xF0000000) | target

                        def _jal():
                            regs = list(self.cpu_registers)
                            regs[31] = ra_val
                            self.cpu_registers = regs
                            self.pc = new_pc
                            self._normalize_exec_pc()

                        dynarec_cache[pc_fetch] = _jal
                    elif op == 0x0F:  # LUI
                        rt = (opcode >> 16) & 0x1F
                        imm = (opcode & 0xFFFF) << 16
                        dynarec_cache[pc_fetch] = lambda rt=rt, imm=imm: setattr(
                            self,
                            "cpu_registers",
                            self.cpu_registers[:rt] + [imm] + self.cpu_registers[rt + 1 :],
                        )
                    else:
                        dynarec_cache[pc_fetch] = lambda: None

                dynarec_cache[pc_fetch]()
                advance_pc = True
                if op == 0x02 or op == 0x03:
                    advance_pc = False
                elif op == 0x04 or op == 0x05:
                    advance_pc = False
                elif op == 0 and funct in (0x08, 0x09):
                    advance_pc = False
                elif op == 0x2B or op == 0x23:
                    advance_pc = False
                if advance_pc:
                    self.pc += 4
                    self._normalize_exec_pc()

                if self.cheat_codes:
                    code = next(iter(self.cheat_codes))
                    try:
                        self.cpu_registers[1] = int(code, 16) & 0xFFFF
                    except ValueError:
                        pass

                if self.netplay_enabled and self.netplay_socket:
                    try:
                        self.netplay_socket.setblocking(False)
                        data = self.netplay_socket.recv(1024)
                        if data:
                            self.cpu_registers[6] = int.from_bytes(data[:8], "big") & 0xFFFF
                    except BlockingIOError:
                        pass
                    except OSError:
                        pass

                self._commercial_boot_tick()

                fc, pcv, title = self.frame_count, self.pc, self.rom_title

                def _present():
                    self.rcp.rdp_render()
                    phase = f" | boot {self._boot_phase}" if self._is_commercial_cart else ""
                    self.status_label.config(
                        text=f"{int(TARGET_FPS)}fps | frame {fc} | PC {hex(pcv)} | {title}{phase}"
                    )

                self._gui_safe(_present)
                frame_budget = 1.0 / (TARGET_FPS * max(0.25, self.emulation_speed))
                spent = time.perf_counter() - loop_t0
                delay = frame_budget - spent
                if delay > 0:
                    time.sleep(delay)
            except Exception as e:
                print(f"[CPU] Error: {e}")
                self._normalize_rom_pc(force_entry=True)
                self._gui_safe(lambda err=str(e): self.status_label.config(text=f"CPU: {err}"))

    def save_state(self):
        if not self.rom_path:
            messagebox.showwarning("No ROM", "No ROM loaded to save state, CatSama!")
            return
        slot = simpledialog.askinteger("Save State", "Enter slot number (1-10):", minvalue=1, maxvalue=10)
        if slot:
            state_data = {
                "frame_count": self.frame_count,
                "pc": self.pc,
                "cpu_registers": self.cpu_registers[:],
                "rom_title": self.rom_title,
                "framebuffer": [row[:] for row in self.rcp.framebuffer],
                "rdram": self.rdram[:],
                "rsp_memory": self.rcp.rsp_memory[:],
                "rdram_bytes": bytes(self.rdram_bytes),
                "ipl_copy_len": self._ipl_copy_len,
                "rom_entry_virt": self._rom_entry_virt,
            }
            state_hash = _state_fingerprint(state_data)
            self.save_states[slot] = state_data
            messagebox.showinfo("Saved", f"State saved to slot {slot}! Hash: {state_hash} Keep rocking, CatSama!")
            print(f"[Save] State saved to slot {slot}, Hash: {state_hash}")

    def load_state(self):
        if not self.rom_path:
            messagebox.showwarning("No ROM", "No ROM loaded to load state, CatSama!")
            return
        slot = simpledialog.askinteger("Load State", "Enter slot number (1-10):", minvalue=1, maxvalue=10)
        if slot in self.save_states:
            state = self.save_states[slot]
            state_hash = _state_fingerprint(state)
            self.frame_count = state["frame_count"]
            self.pc = state["pc"]
            self.cpu_registers = state["cpu_registers"][:]
            self.rom_title = state["rom_title"]
            self.rcp.framebuffer = [row[:] for row in state["framebuffer"]]
            self.rdram = state["rdram"][:]
            self.rcp.rsp_memory = state["rsp_memory"][:]
            rb = state.get("rdram_bytes")
            if isinstance(rb, (bytes, bytearray)) and len(rb) == len(self.rdram_bytes):
                self.rdram_bytes[:] = rb
            self._ipl_copy_len = int(state.get("ipl_copy_len", self._ipl_copy_len))
            self._rom_entry_virt = int(state.get("rom_entry_virt", self._rom_entry_virt)) & 0xFFFFFFFF
            self.status_label.config(text=f"Loaded state from slot {slot}! Hash: {state_hash} Game: {self.rom_title}")
            print(f"[Load] State loaded from slot {slot}, Hash: {state_hash}")
        else:
            messagebox.showwarning("No State", f"No state found in slot {slot}, CatSama!")

    def configure_controller(self):
        for action in self.controller_config:
            key = simpledialog.askstring("Controller Config", f"Enter key for {action}:", initialvalue=self.controller_config[action])
            if key:
                self.controller_config[action] = key
        self.bind_controls()
        messagebox.showinfo("Controller", "Controller updated! Ready for action, CatSama!")

    def set_emulation_speed(self):
        speed = simpledialog.askfloat("Emulation Speed", "Enter speed (0.5 to 20.0):", minvalue=0.5, maxvalue=20.0)
        if speed:
            self.emulation_speed = speed
            self.status_label.config(text=f"Emulation speed set to {speed}x")
            print(f"[Emulation] Speed set to {speed}x")

    def add_cheat_code(self):
        code = simpledialog.askstring("Cheat Code", "Enter cheat code (hex):")
        if code:
            self.cheat_codes[code] = True
            messagebox.showinfo("Cheat", f"Cheat code {code} activated! Let's break the game, CatSama!")
            print(f"[Cheat] Added code: {code}")

    def select_plugins(self):
        plugin_types = ["video", "audio", "input", "rsp"]
        plugin_options = {
            "video": ["glide64mk2", "rice", "arachnoid", "z64"],
            "audio": ["sdl", "jttl_audio"],
            "input": ["sdl"],
            "rsp": ["hle", "cxd4", "z64"]
        }
        for ptype in plugin_types:
            plugin = simpledialog.askstring("Plugin Config", f"Select {ptype} plugin:", initialvalue=self.plugins[ptype])
            if plugin in plugin_options[ptype]:
                self.plugins[ptype] = plugin
        messagebox.showinfo("Plugins", f"Plugins updated: {self.plugins}! Ready to roll, CatSama!")
        print(f"[Plugins] Updated: {self.plugins}")

    def toggle_netplay(self):
        if not self.netplay_enabled:
            host = simpledialog.askstring("Netplay", "Enter host IP (leave blank for server):")
            self.netplay_enabled = True
            try:
                self.netplay_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self.netplay_socket.setblocking(False)
                if host:
                    self.netplay_socket.connect((host, 45000))
                    messagebox.showinfo("Netplay", f"Connected to {host}! Let's sync, CatSama!")
                    print(f"[Netplay] Connected to {host}:45000")
                else:
                    self.netplay_socket.bind(("0.0.0.0", 45000))
                    messagebox.showinfo("Netplay", "Netplay server started! Waiting for players, CatSama!")
                    print("[Netplay] Server started on port 45000")
            except Exception as e:
                messagebox.showerror("Netplay Error", f"Failed to start netplay: {str(e)}")
                print(f"[Netplay] Error: {str(e)}")
                self.netplay_enabled = False
                self.netplay_socket = None
        else:
            self.netplay_enabled = False
            if self.netplay_socket:
                self.netplay_socket.close()
                self.netplay_socket = None
            messagebox.showinfo("Netplay", "Netplay disabled! Solo gaming time, CatSama!")
            print("[Netplay] Disabled")

if __name__ == "__main__":
    print("12ABKKK2NNAAAA THE YOSHI DUDES ARE HEREE WA WA")
    root = tk.Tk()
    emulator = N64Emulator(root)
    root.mainloop()

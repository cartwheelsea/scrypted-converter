#!/usr/bin/env python3
"""
Scrypted Recording Converter
Converts .rtsp NVR recordings to MP4. Double-click to run.
"""

import base64
import json
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk


def _ffmpeg() -> str:
    """Return path to ffmpeg — bundled inside the app, or from PATH."""
    if hasattr(sys, "_MEIPASS"):
        bundled = os.path.join(sys._MEIPASS, "ffmpeg")
        if os.path.isfile(bundled):
            return bundled
    return "ffmpeg"


def ts_to_str(ts_ms: int) -> str:
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).astimezone()
    return dt.strftime("%Y-%m-%d  %H:%M:%S")


def detect_payload_types(rtsp_file: Path) -> tuple[int, int | None]:
    data = rtsp_file.read_bytes()
    video_pt = audio_pt = None
    i = 0
    while i < min(len(data) - 4, 50_000):
        if data[i] == 0x24:
            ch = data[i + 1]
            length = struct.unpack(">H", data[i + 2:i + 4])[0]
            if i + 5 < len(data):
                pt = data[i + 5] & 0x7F
                if ch == 0 and video_pt is None:
                    video_pt = pt
                elif ch == 2 and audio_pt is None:
                    audio_pt = pt
            i += 4 + max(length, 1)
        else:
            i += 1
        if video_pt is not None and audio_pt is not None:
            break
    return video_pt or 96, audio_pt


def build_sdp(session_json: dict | None, rtsp_files: list[Path]) -> str:
    if session_json and "sdp" in session_json:
        body = session_json["sdp"]
    else:
        video_pt, audio_pt = detect_payload_types(rtsp_files[0])
        body = _fallback_sdp_body(video_pt, audio_pt)
    return "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=Recording\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n" + body


def _fallback_sdp_body(video_pt: int, audio_pt: int | None) -> str:
    if video_pt == 98:
        video = f"m=video 0 RTP/AVP {video_pt}\r\na=rtpmap:{video_pt} H265/90000\r\na=control:trackID=0\r\n"
    else:
        video = (f"m=video 0 RTP/AVP {video_pt}\r\na=rtpmap:{video_pt} H264/90000\r\n"
                 f"a=fmtp:{video_pt} packetization-mode=1\r\na=control:trackID=0\r\n")
    audio = ""
    if audio_pt == 8:
        audio = "m=audio 2 RTP/AVP 8\r\na=rtpmap:8 PCMA/8000\r\na=control:trackID=1\r\n"
    elif audio_pt == 97:
        audio = ("m=audio 2 RTP/AVP 97\r\na=rtpmap:97 MPEG4-GENERIC/8000\r\n"
                 "a=fmtp:97 streamtype=5;profile-level-id=1;mode=AAC-hbr;"
                 "sizelength=13;indexlength=3;indexdeltalength=3\r\na=control:trackID=1\r\n")
    return video + audio


def _extract_sprop(sdp: str) -> tuple[bytes | None, bytes | None]:
    """Extract SPS and PPS bytes from sprop-parameter-sets in an SDP string."""
    m = re.search(r'sprop-parameter-sets=([^;\s\r\n]+)', sdp)
    if not m:
        return None, None
    parts = m.group(1).rstrip('\r\n').split(',')
    try:
        sps = base64.b64decode(parts[0]) if parts else None
        pps = base64.b64decode(parts[1]) if len(parts) > 1 else None
    except Exception:
        return None, None
    return sps, pps


def _first_video_rtp_info(data: bytes) -> tuple[int, int, int] | None:
    """Return (ssrc, timestamp, seq) from the first video-channel RTP packet."""
    i = 0
    while i < len(data) - 16:
        if data[i] == 0x24 and data[i + 1] == 0:  # interleaved, channel 0 = video
            length = struct.unpack('>H', data[i + 2:i + 4])[0]
            rtp = data[i + 4: i + 4 + length]
            if len(rtp) >= 12:
                seq  = struct.unpack('>H', rtp[2:4])[0]
                ts   = struct.unpack('>I', rtp[4:8])[0]
                ssrc = struct.unpack('>I', rtp[8:12])[0]
                return ssrc, ts, seq
            i += 4 + max(length, 1)
        elif data[i] == 0x24:
            length = struct.unpack('>H', data[i + 2:i + 4])[0]
            i += 4 + max(length, 1)
        else:
            i += 1
    return None


def _make_rtp_nal_packet(nal: bytes, ssrc: int, ts: int, seq: int,
                         ch: int = 0, pt: int = 96) -> bytes:
    """Wrap a single NAL unit in an interleaved RTP packet."""
    rtp = struct.pack('>BBHII', 0x80, pt, seq & 0xFFFF, ts, ssrc) + nal
    return struct.pack('>BBH', 0x24, ch, len(rtp)) + rtp


class RTSPServer:
    def __init__(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port: int = self._sock.getsockname()[1]

    def serve(self, sdp: str, rtsp_files: list[Path]) -> None:
        self._sock.settimeout(30)
        try:
            conn, _ = self._sock.accept()
        except socket.timeout:
            return
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8 * 1024 * 1024)
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
        try:
            self._handle(conn, sdp, rtsp_files)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _read_request(self, conn: socket.socket) -> str:
        buf = b""
        conn.settimeout(15)
        while b"\r\n\r\n" not in buf:
            chunk = conn.recv(4096)
            if not chunk:
                return ""
            buf += chunk
        return buf.split(b"\r\n\r\n")[0].decode("utf-8", errors="replace")

    def _handle(self, conn: socket.socket, sdp: str, rtsp_files: list[Path]) -> None:
        while True:
            req = self._read_request(conn)
            if not req:
                return
            lines = req.strip().split("\r\n")
            first = lines[0].split()
            if not first:
                return
            method = first[0]
            cseq = next((l.split(":", 1)[1].strip() for l in lines if l.lower().startswith("cseq:")), "0")

            if method == "OPTIONS":
                conn.sendall(f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\nPublic: OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN\r\n\r\n".encode())
            elif method == "DESCRIBE":
                sdp_b = sdp.encode()
                conn.sendall(f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\nContent-Type: application/sdp\r\nContent-Length: {len(sdp_b)}\r\n\r\n".encode() + sdp_b)
            elif method == "SETUP":
                transport = next((l.split(":", 1)[1].strip() for l in lines if l.lower().startswith("transport:")), "RTP/AVP/TCP;unicast;interleaved=0-1")
                conn.sendall(f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\nSession: 1\r\nTransport: {transport}\r\n\r\n".encode())
            elif method == "PLAY":
                conn.sendall(f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\nSession: 1\r\n\r\n".encode())
                # In-band SPS/PPS before IDR so ffmpeg has codec params without relying on sprop-parameter-sets.
                sps, pps = _extract_sprop(sdp)
                if (sps or pps) and rtsp_files:
                    first_data = rtsp_files[0].read_bytes()
                    info = _first_video_rtp_info(first_data)
                    if info:
                        ssrc, ts, first_seq = info
                        nalus = [n for n in (sps, pps) if n]
                        for idx, nal in enumerate(nalus):
                            seq = (first_seq - len(nalus) + idx) & 0xFFFF
                            conn.send(_make_rtp_nal_packet(nal, ssrc, ts, seq))
                for f in rtsp_files:
                    data = f.read_bytes()
                    if data:
                        view, offset = memoryview(data), 0
                        while offset < len(data):
                            sent = conn.send(view[offset:offset + 65536])
                            if sent == 0:
                                return
                            offset += sent
                return
            elif method == "TEARDOWN":
                conn.sendall(f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\nSession: 1\r\n\r\n".encode())
                return

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


def session_subdir(session_dir: Path) -> str:
    """Return 'events' or 'recordings' based on .rtsp file count (each file ≈ 60 s)."""
    n = sum(1 for _ in session_dir.rglob("*.rtsp"))
    return "events" if n <= 8 else "recordings"


def get_rtsp_files(session_dir: Path) -> list[Path]:
    segments = sorted((d for d in session_dir.iterdir() if d.is_dir()), key=lambda d: int(d.name))
    files: list[Path] = []
    for seg in segments:
        files.extend(sorted(seg.glob("*.rtsp"), key=lambda f: int(f.stem)))
    return files


def find_sessions(root: Path, skip_remote: bool) -> list[tuple[Path, Path]]:
    # Support selecting a camera dir directly instead of the parent recordings dir.
    if root.name.startswith("scrypted-"):
        camera_dirs = [root]
    else:
        try:
            camera_dirs = sorted(d for d in root.iterdir() if d.is_dir() and d.name.startswith("scrypted-"))
        except PermissionError:
            return []

    sessions = []
    for camera_dir in camera_dirs:
        name = camera_dir.name
        if name.endswith(".events"):
            continue
        if skip_remote and (name.endswith(".remote") or name.endswith(".low-resolution")):
            continue
        try:
            for session_dir in sorted((d for d in camera_dir.iterdir() if d.is_dir()), key=lambda d: int(d.name)):
                sessions.append((camera_dir, session_dir))
        except (PermissionError, ValueError):
            continue
    return sessions


def convert_session(
    session_dir: Path,
    output_file: Path,
    session_json: dict | None,
    cancel_event: threading.Event,
) -> tuple[bool, str]:
    rtsp_files = get_rtsp_files(session_dir)
    if not rtsp_files:
        return False, "no RTSP files"

    sdp = build_sdp(session_json, rtsp_files)
    server = RTSPServer()
    server_thread = threading.Thread(target=server.serve, args=(sdp, rtsp_files), daemon=True)
    server_thread.start()
    output_file.parent.mkdir(parents=True, exist_ok=True)

    audio_codec = (session_json or {}).get("mediaStreamOptions", {}).get("audio", {}).get("codec", "")
    audio_args = ["-c:a", "aac", "-ac", "1", "-ar", "8000", "-b:a", "32k"] if audio_codec == "pcm_alaw" else ["-c:a", "copy"]

    proc = subprocess.Popen(
        [_ffmpeg(), "-y", "-loglevel", "warning",
         "-rtsp_transport", "tcp",
         "-analyzeduration", "30000000",  # recordings can start mid-IDR with large RTP timestamp gaps
         "-i", f"rtsp://127.0.0.1:{server.port}/session",
         "-c:v", "copy", *audio_args, str(output_file)],
        stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True,
    )

    stderr_lines: list[str] = []
    def _drain():
        for line in proc.stderr:
            stderr_lines.append(line.rstrip())
    threading.Thread(target=_drain, daemon=True).start()

    while proc.poll() is None:
        if cancel_event.is_set():
            proc.terminate()
            server.close()
            server_thread.join(timeout=3)
            return False, "cancelled"
        time.sleep(0.3)

    server.close()
    server_thread.join(timeout=5)

    if proc.returncode != 0:
        return False, "\n".join(stderr_lines[-5:]).strip()
    if not output_file.exists() or output_file.stat().st_size < 1000:
        return False, "output empty or missing"
    return True, ""


APP_VERSION = "1.0.0"

_GREEN  = "#28a745"
_RED    = "#dc3545"
_GRAY   = "#888888"
_BLUE   = "#0d6efd"
_BG_LOG = "#1c1c1e"
_FG_LOG = "#f0f0f0"



class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Scrypted Recording Converter")
        self.resizable(True, True)
        self.minsize(640, 580)

        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._ffmpeg_proc: subprocess.Popen | None = None
        self._start_time: float | None = None
        self._timer_id: str | None = None

        self._build_menu()
        self._build()
        self._set_state("idle")

        # Pre-fill: if run as a script from inside the recordings folder use that,
        # otherwise default to the user's home directory.
        if not hasattr(sys, "_MEIPASS"):
            default = Path(__file__).parent
            if any(default.glob("scrypted-*")):
                self._src_var.set(str(default))
                self._dst_var.set(str(default / "MP4"))
                self._on_src_changed()

    def _build_menu(self):
        self.option_add("*tearOff", False)
        menubar = tk.Menu(self)
        if sys.platform == "darwin":
            apple = tk.Menu(menubar, name="apple")
            menubar.add_cascade(menu=apple)
            apple.add_command(label="About Scrypted Converter", command=self._show_about)
        self.config(menu=menubar)

    def _show_about(self):
        messagebox.showinfo(
            "Scrypted Converter",
            f"Scrypted Converter\nVersion {APP_VERSION}\n\nConverts Scrypted NVR recordings to MP4.",
        )

    def _tick(self):
        if self._start_time is None:
            return
        elapsed = int(time.time() - self._start_time)
        m, s = divmod(elapsed, 60)
        h, m = divmod(m, 60)
        self._elapsed_var.set(f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}")
        self._timer_id = self.after(1000, self._tick)

    def _fmt_duration(self, seconds: int) -> str:
        m, s = divmod(seconds, 60)
        h, m = divmod(m, 60)
        if h:
            return f"{h}h {m:02d}m {s:02d}s"
        return f"{m}m {s:02d}s" if m else f"{s}s"

    def _build(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        root = ttk.Frame(self, padding=20)
        root.grid(sticky="nsew")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(5, weight=1)

        # Title
        ttk.Label(root, text="Scrypted Recording Converter", font=("", 15, "bold")).grid(
            row=0, column=0, sticky="w", pady=(0, 4))
        ttk.Label(root, text="Converts .rtsp camera recordings to standard MP4 files.",
                  foreground="#666").grid(row=1, column=0, sticky="w", pady=(0, 12))

        ttk.Separator(root).grid(row=2, column=0, sticky="ew", pady=(0, 14))

        # Folder form
        form = ttk.Frame(root)
        form.grid(row=3, column=0, sticky="ew")
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text="Recordings folder:").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=4)
        self._src_var = tk.StringVar()
        self._src_var.trace_add("write", lambda *_: self.after(100, self._on_src_changed))
        ttk.Entry(form, textvariable=self._src_var).grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Button(form, text="Browse…", command=self._browse_src).grid(row=0, column=2, padx=(8, 0), pady=4)

        ttk.Label(form, text="Output folder:").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=4)
        self._dst_var = tk.StringVar()
        ttk.Entry(form, textvariable=self._dst_var).grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Button(form, text="Browse…", command=self._browse_dst).grid(row=1, column=2, padx=(8, 0), pady=4)

        # Session count hint
        hint_row = ttk.Frame(root)
        hint_row.grid(row=4, column=0, sticky="ew", pady=(10, 14))
        self._hint_var = tk.StringVar()
        ttk.Label(hint_row, textvariable=self._hint_var, foreground="#555").pack(side="right")

        # Convert button
        self._convert_btn = ttk.Button(root, text="Convert All Recordings", command=self._start)
        self._convert_btn.grid(row=5, column=0, sticky="ew", ipady=6)

        # Progress
        prog = ttk.Frame(root)
        prog.grid(row=6, column=0, sticky="ew", pady=(12, 4))
        prog.columnconfigure(0, weight=1)
        self._status_var = tk.StringVar(value="Select a recordings folder to get started.")
        ttk.Label(prog, textvariable=self._status_var, foreground="#555").grid(row=0, column=0, sticky="w")
        self._pct_var = tk.StringVar(value="")
        ttk.Label(prog, textvariable=self._pct_var, foreground="#555").grid(row=0, column=1, sticky="e")
        self._elapsed_var = tk.StringVar(value="")
        ttk.Label(prog, textvariable=self._elapsed_var, foreground="#555").grid(row=0, column=2, sticky="e", padx=(12, 0))
        self._progressbar = ttk.Progressbar(prog, mode="determinate")
        self._progressbar.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(2, 0))

        # Log
        log_frame = ttk.Frame(root)
        log_frame.grid(row=7, column=0, sticky="nsew", pady=(8, 0))
        root.rowconfigure(7, weight=1)
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)

        self._log = scrolledtext.ScrolledText(
            log_frame, state="disabled", height=12,
            font=("Menlo", 11), background=_BG_LOG, foreground=_FG_LOG,
            insertbackground="white", relief="flat", borderwidth=1,
        )
        self._log.grid(sticky="nsew")
        self._log.tag_config("ok",   foreground="#4ec9b0")
        self._log.tag_config("fail", foreground="#f48771")
        self._log.tag_config("skip", foreground=_GRAY)
        self._log.tag_config("info", foreground="#9cdcfe")
        self._log.tag_config("warn", foreground="#dcdcaa")

        # Bottom bar
        bar = ttk.Frame(root)
        bar.grid(row=8, column=0, sticky="ew", pady=(10, 0))
        self._cancel_btn = ttk.Button(bar, text="Cancel", command=self._do_cancel)
        self._cancel_btn.pack(side="left")
        self._summary_var = tk.StringVar()
        ttk.Label(bar, textvariable=self._summary_var).pack(side="left", padx=14)
        self._reveal_btn = ttk.Button(bar, text="Open Output Folder ▸", command=self._reveal)
        self._reveal_btn.pack(side="right")

    def _browse_src(self):
        d = filedialog.askdirectory(title="Select Recordings Folder",
                                    initialdir=self._src_var.get() or str(Path.home()))
        if d:
            self._src_var.set(d)
            if not self._dst_var.get():
                self._dst_var.set(str(Path(d) / "MP4"))

    def _browse_dst(self):
        d = filedialog.askdirectory(title="Select Output Folder",
                                    initialdir=self._dst_var.get() or str(Path.home()))
        if d:
            self._dst_var.set(d)

    def _on_src_changed(self):
        src = self._src_var.get().strip()
        if not src or not Path(src).is_dir():
            self._hint_var.set("")
            return
        sessions = find_sessions(Path(src), True)
        n = len(sessions)
        self._hint_var.set(f"{n} session{'s' if n != 1 else ''} found" if n else "No recordings found")

    def _log_line(self, text: str, tag: str = ""):
        self._log.configure(state="normal")
        self._log.insert("end", text + "\n", tag)
        self._log.see("end")
        self._log.configure(state="disabled")

    def _set_state(self, state: str):
        if state == "idle":
            self._convert_btn.configure(state="normal")
            self._cancel_btn.configure(state="disabled")
            self._reveal_btn.configure(state="disabled")
        elif state == "running":
            self._convert_btn.configure(state="disabled")
            self._cancel_btn.configure(state="normal")
            self._reveal_btn.configure(state="disabled")
        elif state == "done":
            self._convert_btn.configure(state="normal")
            self._cancel_btn.configure(state="disabled")
            self._reveal_btn.configure(state="normal")

    def _reveal(self):
        dst = self._dst_var.get()
        if dst:
            subprocess.run(["open", dst])

    def _start(self):
        src = self._src_var.get().strip()
        dst = self._dst_var.get().strip()

        if not src:
            messagebox.showwarning("No folder selected", "Please select a recordings folder first.")
            return
        if not Path(src).is_dir():
            messagebox.showwarning("Folder not found", f"The folder does not exist:\n{src}")
            return
        if not dst:
            dst = str(Path(src) / "MP4")
            self._dst_var.set(dst)

        # Verify ffmpeg is available
        if subprocess.run(["which", _ffmpeg()], capture_output=True).returncode != 0 and not os.path.isfile(_ffmpeg()):
            messagebox.showerror(
                "ffmpeg not found",
                "ffmpeg is required but was not found.\n\nInstall it with:\n  brew install ffmpeg"
            )
            return

        self._cancel.clear()
        self._log.configure(state="normal")
        self._log.delete("1.0", "end")
        self._log.configure(state="disabled")
        self._summary_var.set("")
        self._progressbar["value"] = 0
        self._pct_var.set("")
        self._elapsed_var.set("00:00")
        self._set_state("running")
        self._start_time = time.time()
        self._tick()

        self._thread = threading.Thread(
            target=self._run, args=(Path(src), Path(dst)), daemon=True
        )
        self._thread.start()

    def _do_cancel(self):
        self._cancel.set()
        self._cancel_btn.configure(state="disabled")
        self._status_var.set("Cancelling after current session finishes…")

    def _run(self, src: Path, dst: Path):
        import concurrent.futures
        import os

        sessions = find_sessions(src, True)
        total = len(sessions)

        if total == 0:
            self.after(0, lambda: self._status_var.set("No Scrypted recordings found in that folder."))
            self.after(0, lambda: self._set_state("idle"))
            return

        # Use half the CPU cores — each worker runs ffmpeg which is already multi-threaded
        workers = max(1, (os.cpu_count() or 2) // 2)
        self.after(0, lambda: self._log_line(f"Found {total} sessions — converting.\n", "info"))

        succeeded = failed = skipped = 0
        completed = 0
        lock = threading.Lock()

        def process(args):
            nonlocal succeeded, failed, skipped, completed
            i, (camera_dir, session_dir) = args

            if self._cancel.is_set():
                return

            ts_ms = int(session_dir.name)
            date_str = ts_to_str(ts_ms)
            label = f"{camera_dir.name}   {date_str}"
            subdir = session_subdir(session_dir)
            stem = date_str.replace("  ", "_")
            output_file = dst / camera_dir.name / subdir / f"{stem}.mp4"
            old_output  = dst / camera_dir.name / f"{stem}.mp4"

            if (output_file.exists() and output_file.stat().st_size > 1000) or \
               (old_output.exists() and old_output.stat().st_size > 1000):
                with lock:
                    skipped += 1
                    completed += 1
                self.after(0, lambda l=label: self._log_line(f"↷  {l}  (already done)", "skip"))
                return

            rtsp_count = sum(1 for _ in session_dir.rglob("*.rtsp"))
            if rtsp_count == 0:
                with lock:
                    skipped += 1
                    completed += 1
                return

            session_json = None
            sj = session_dir / "session.json"
            if sj.exists():
                try:
                    session_json = json.loads(sj.read_text())
                except Exception:
                    pass

            ok, msg = convert_session(session_dir, output_file, session_json, self._cancel)

            with lock:
                completed += 1
                pct = int(completed / total * 100)
                if ok:
                    succeeded += 1
                    size_mb = output_file.stat().st_size / 1024 ** 2
                    self.after(0, lambda l=label, s=size_mb, p=pct, c=completed, t=total: (
                        self._log_line(f"✓  {l}  ({s:.0f} MB)", "ok"),
                        self._progressbar.configure(value=p),
                        self._pct_var.set(f"{p}%"),
                        self._status_var.set(f"{c}/{t} done"),
                    ))
                else:
                    failed += 1
                    if output_file.exists():
                        output_file.unlink(missing_ok=True)
                    self.after(0, lambda l=label, m=msg, p=pct, c=completed, t=total: (
                        self._log_line(f"✗  {l}\n   {m}", "fail"),
                        self._progressbar.configure(value=p),
                        self._pct_var.set(f"{p}%"),
                        self._status_var.set(f"{c}/{t} done"),
                    ))

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            pool.map(process, enumerate(sessions, 1))

        cancelled = self._cancel.is_set()
        elapsed_secs = int(time.time() - self._start_time) if self._start_time else 0
        parts = []
        if succeeded: parts.append(f"{succeeded} converted")
        if failed:    parts.append(f"{failed} failed")
        if skipped:   parts.append(f"{skipped} skipped")
        summary = "  ·  ".join(parts)

        def _finish():
            if self._timer_id:
                self.after_cancel(self._timer_id)
                self._timer_id = None
            self._start_time = None
            duration = self._fmt_duration(elapsed_secs)
            self._progressbar.configure(value=100)
            self._pct_var.set("100%" if not cancelled else "")
            self._elapsed_var.set(duration)
            self._status_var.set(f"Cancelled  ·  {duration}" if cancelled else f"Done  ·  {duration}")
            self._summary_var.set(summary)
            self._log_line(f"\n{'Cancelled. ' if cancelled else ''}Finished — {summary}  ·  {duration}", "info")
            self._set_state("done")

        self.after(0, _finish)


if __name__ == "__main__":
    app = App()
    app.mainloop()

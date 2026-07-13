import subprocess
import os
import re
import signal
import threading
from .gpu import probe_file
from .logger import log_job_start, log_job_progress, log_job_done, log_job_error, log_job_cancelled

TIME_RE = re.compile(r"out_time=(\d+):(\d+):(\d+\.\d+)")
FRAME_RE = re.compile(r"frame=\s*(\d+)")
FPS_RE = re.compile(r"fps=\s*([\d.]+)")
SPEED_RE = re.compile(r"speed=\s*([\d.]+)x")


class ConversionJob:

    def __init__(self, filepath, output_dir, settings):
        self.filepath = filepath
        self.filename = os.path.basename(filepath)
        self.output_dir = output_dir
        self.settings = settings
        self.status = "queued"
        self.progress = 0.0
        self.current_fps = 0.0
        self.speed = 0.0
        self.duration = 0.0
        self.error = None
        self.output_path = None
        self.process = None
        self._cancelled = False
        self.video_codec = ""
        self.resolution = ""
        self.container_format = ""
        self.file_info = {}

    def cancel(self):
        self._cancelled = True
        if self.process and self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.status = "cancelled" if not self.error else self.status

    def build_output_path(self):
        name_no_ext = os.path.splitext(self.filename)[0]
        container = self.settings.get("container", "mov")
        output_name = f"prorez_convert_{name_no_ext}.{container}"
        output_dir = self.output_dir
        if not output_dir:
            output_dir = os.path.dirname(self.filepath)
        self.output_path = os.path.join(output_dir, output_name)
        os.makedirs(output_dir, exist_ok=True)
        return self.output_path

    def build_command(self):
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]

        encoder = self.settings.get("encoder", "libx264")
        encoder_info = self.settings.get("encoder_info", {})
        hw_group = encoder_info.get("group", "cpu")
        src_codec = self.settings.get("src_codec", "")

        # Fallback: deteksi dari nama encoder kalau group tidak ketemu
        if hw_group == "cpu":
            if "nvenc" in encoder:
                hw_group = "nvidia"
            elif "vaapi" in encoder:
                hw_group = "vaapi"
            elif "qsv" in encoder:
                hw_group = "qsv"
            elif "amf" in encoder:
                hw_group = "amf"

        # Hardware decode for HEVC if source is HEVC and NVIDIA available
        use_cuvid = hw_group == "nvidia" and src_codec in ("hevc", "h265")

        if use_cuvid:
            cmd.extend(["-hwaccel", "cuda", "-c:v", "hevc_cuvid"])
        elif hw_group == "nvidia":
            cmd.extend(["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"])
        elif hw_group == "vaapi":
            devices = self.settings.get("vaapi_devices", [])
            if devices:
                cmd.extend(["-vaapi_device", devices[0],
                           "-vf", "format=nv12,hwupload"])
        elif hw_group == "qsv":
            cmd.extend(["-hwaccel", "qsv", "-hwaccel_output_format", "qsv"])

        cmd.extend(["-i", self.filepath, "-c:v", encoder])

        profile = self.settings.get("profile")
        if profile:
            cmd.extend(["-profile:v", profile])

        if encoder in ("prores_ks", "prores"):
            pass
        elif encoder in ("dnxhd",):
            if not profile:
                cmd.extend(["-profile:v", "dnxhr_hq"])
        elif hw_group in ("nvidia", "vaapi", "amf", "qsv"):
            cq = self.settings.get("cq")
            if cq is not None:
                cmd.extend(["-cq", str(cq)])
            else:
                bitrate = self.settings.get("bitrate", "10M")
                if bitrate and bitrate != "auto":
                    cmd.extend(["-b:v", bitrate])
            qp = self.settings.get("qp")
            if qp is not None:
                cmd.extend(["-qp", str(qp)])
        else:
            crf = self.settings.get("crf")
            if crf is not None:
                cmd.extend(["-crf", str(crf)])
            bitrate = self.settings.get("bitrate", "10M")
            if bitrate and bitrate != "auto":
                cmd.extend(["-b:v", bitrate])

        # NVENC preset mapping: CPU preset -> NVENC preset
        preset = self.settings.get("preset")
        if preset:
            nvenc_preset_map = {
                "ultrafast": "p1", "superfast": "p2", "veryfast": "p3",
                "faster": "p3", "fast": "p4", "medium": "p4",
                "slow": "p5", "slower": "p6", "veryslow": "p7",
            }
            if hw_group == "nvidia" and encoder in ("h264_nvenc", "hevc_nvenc", "av1_nvenc"):
                cmd.extend(["-preset", nvenc_preset_map.get(preset, "p4")])
            elif hw_group in ("vaapi", "qsv", "amf"):
                pass
            else:
                cmd.extend(["-preset", preset])

        is_intra = encoder in ("prores_ks", "prores", "dnxhd")
        container = self.settings.get("container", "mp4")
        audio_codec = self.settings.get("audio_codec", "aac")
        audio_bitrate = self.settings.get("audio_bitrate", "192k")
        is_davinci_h264 = (
            container == "mov"
            and encoder in ("h264_nvenc", "hevc_nvenc", "av1_nvenc")
        )

        def _add_audio():
            cmd.extend(["-c:a", audio_codec])
            if audio_codec not in (
                "copy", "pcm_s16le", "pcm_s24le", "pcm_f32le", "flac"
            ):
                cmd.extend(["-b:a", audio_bitrate])

        if is_intra:
            _add_audio()
            if encoder == "dnxhd":
                cmd.extend(["-vf", "format=yuv422p"])
        elif is_davinci_h264:
            _add_audio()
            if hw_group == "nvidia" and encoder == "h264_nvenc":
                cmd.extend(["-pix_fmt", "yuv422p"])
                cmd.extend(["-codec_tag:v", "avc1"])
        else:
            _add_audio()
            if hw_group == "nvidia" and encoder == "h264_nvenc":
                cmd.extend(["-pix_fmt", "yuv420p"])

        # NVENC-specific quality optimizations
        if hw_group == "nvidia" and encoder in ("h264_nvenc", "hevc_nvenc", "av1_nvenc"):
            # Quality tuning
            cmd.extend([
                "-spatial_aq", "1",      # Spatial adaptive quantization
                "-temporal_aq", "1",     # Temporal adaptive quantization
                "-rc-lookahead", "32",   # Lookahead frames for better RC
            ])
            # RC mode: use CQ if set, else VBR with lookahead

        extra_args = self.settings.get("extra_args", "")
        if extra_args:
            cmd.extend(extra_args.split())

        # MOV/MP4 faststart for delivery codecs (not intra)
        if container in ("mov", "mp4") and not is_intra:
            cmd.extend(["-movflags", "+faststart"])

        self.build_output_path()
        cmd.extend(["-progress", "pipe:1", "-nostats", self.output_path])

        return cmd

    def run(self, progress_callback=None, status_callback=None):
        self._cancelled = False

        data = probe_file(self.filepath)
        if data:
            self.duration = float(data.get("format", {}).get("duration", 0))

        cmd = self.build_command()

        self.status = "converting"
        if status_callback:
            status_callback(self, "converting")

        log_job_start(self)

        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                bufsize=1
            )
        except FileNotFoundError:
            self.status = "error"
            self.error = "ffmpeg not found. Please install ffmpeg first."
            if status_callback:
                status_callback(self, "error")
            return False
        except Exception as e:
            self.status = "error"
            self.error = str(e)
            if status_callback:
                status_callback(self, "error")
            return False

        out_time = 0.0
        stderr_lines = []
        last_logged_progress = -1

        def read_stderr():
            for line in iter(self.process.stderr.readline, ""):
                stderr_lines.append(line)

        stderr_thread = threading.Thread(target=read_stderr, daemon=True)
        stderr_thread.start()

        for line in iter(self.process.stdout.readline, ""):
            if self._cancelled:
                break

            m = TIME_RE.search(line)
            if m:
                h, m_, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
                out_time = h * 3600 + m_ * 60 + s
                if self.duration > 0:
                    self.progress = min((out_time / self.duration) * 100, 100)
                    if int(self.progress) // 10 != last_logged_progress // 10:
                        last_logged_progress = int(self.progress)
                        log_job_progress(self)
                    if progress_callback:
                        progress_callback(self)

            fm = FPS_RE.search(line)
            if fm:
                self.current_fps = float(fm.group(1))

            sm = SPEED_RE.search(line)
            if sm:
                self.speed = float(sm.group(1))

        self.process.stdout.close()
        returncode = self.process.wait()

        stderr_output = "".join(stderr_lines)
        self.process.stderr.close()

        if self._cancelled:
            self.status = "cancelled"
            log_job_cancelled(self)
            if self.output_path and os.path.exists(self.output_path):
                try:
                    os.remove(self.output_path)
                except OSError:
                    pass
            if status_callback:
                status_callback(self, "cancelled")
            return False

        if returncode == 0:
            self.status = "completed"
            self.progress = 100.0
            log_job_done(self)
            if progress_callback:
                progress_callback(self)
            if status_callback:
                status_callback(self, "completed")
            return True
        else:
            self.status = "error"
            self.error = stderr_output.strip() or "Unknown error (exit code {})".format(returncode)
            log_job_error(self)
            if status_callback:
                status_callback(self, "error")
            return False


def format_duration(seconds):
    if not seconds or seconds <= 0:
        return "?"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def format_size(size_bytes):
    if size_bytes >= 1_000_000_000:
        return f"{size_bytes / 1_000_000_000:.2f} GB"
    if size_bytes >= 1_000_000:
        return f"{size_bytes / 1_000_000:.2f} MB"
    if size_bytes >= 1_000:
        return f"{size_bytes / 1_000:.2f} KB"
    return f"{size_bytes} B"
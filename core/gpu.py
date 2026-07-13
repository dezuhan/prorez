import subprocess
import os
import json
import re

ENC_MAP = {
    "h264_nvenc": {"label": "NVIDIA NVENC (H.264)", "group": "nvidia", "type": "h264"},
    "hevc_nvenc": {"label": "NVIDIA NVENC (HEVC)", "group": "nvidia", "type": "hevc"},
    "av1_nvenc": {"label": "NVIDIA NVENC (AV1)", "group": "nvidia", "type": "av1"},
    "h264_vaapi": {"label": "VA-API (H.264)", "group": "vaapi", "type": "h264"},
    "hevc_vaapi": {"label": "VA-API (HEVC)", "group": "vaapi", "type": "hevc"},
    "av1_vaapi": {"label": "VA-API (AV1)", "group": "vaapi", "type": "av1"},
    "h264_amf": {"label": "AMD AMF (H.264)", "group": "amf", "type": "h264"},
    "hevc_amf": {"label": "AMD AMF (HEVC)", "group": "amf", "type": "hevc"},
    "av1_amf": {"label": "AMD AMF (AV1)", "group": "amf", "type": "av1"},
    "h264_qsv": {"label": "Intel QSV (H.264)", "group": "qsv", "type": "h264"},
    "hevc_qsv": {"label": "Intel QSV (HEVC)", "group": "qsv", "type": "hevc"},
    "av1_qsv": {"label": "Intel QSV (AV1)", "group": "qsv", "type": "av1"},
    "h264_vulkan": {"label": "Vulkan (H.264)", "group": "vulkan", "type": "h264"},
    "hevc_vulkan": {"label": "Vulkan (HEVC)", "group": "vulkan", "type": "hevc"},
}

SOFT_ENC_MAP = {
    "libx264": {"label": "CPU (libx264)", "group": "cpu", "type": "h264"},
    "libx265": {"label": "CPU (libx265)", "group": "cpu", "type": "hevc"},
    "libopenh264": {"label": "CPU (OpenH264)", "group": "cpu", "type": "h264"},
    "libsvtav1": {"label": "CPU (SVT-AV1)", "group": "cpu", "type": "av1"},
    "libaom-av1": {"label": "CPU (libaom-av1)", "group": "cpu", "type": "av1"},
    "prores_ks": {"label": "CPU (Apple ProRes)", "group": "prores", "type": "prores"},
    "prores": {"label": "CPU (Apple ProRes)", "group": "prores", "type": "prores"},
    "dnxhd": {"label": "CPU (DNxHD/HR)", "group": "dnx", "type": "dnxhd"},
    "mpeg4": {"label": "CPU (MPEG-4)", "group": "cpu", "type": "mpeg4"},
    "mjpeg": {"label": "CPU (MJPEG)", "group": "cpu", "type": "mjpeg"},
    "libxvid": {"label": "CPU (Xvid MPEG-4)", "group": "cpu", "type": "mpeg4"},
}

FORMAT_CONTAINERS = {
    "mp4": "mp4",
    "mkv": "matroska",
    "webm": "webm",
    "mov": "mov",
    "avi": "avi",
    "ts": "mpegts",
}


def get_available_encoders():
    try:
        result = subprocess.run(
            ["ffmpeg", "-encoders"],
            capture_output=True, text=True, timeout=10
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}, {}

    output = result.stdout + result.stderr
    hw_available = {}
    sw_available = {}

    for enc_name, info in ENC_MAP.items():
        if enc_name in output:
            hw_available[enc_name] = info

    for enc_name, info in SOFT_ENC_MAP.items():
        if enc_name in output:
            sw_available[enc_name] = info

    return hw_available, sw_available


def get_encoder_groups():
    hw, sw = get_available_encoders()
    hw_groups = {}
    sw_groups = {}

    for enc_name, info in hw.items():
        group = info["group"]
        if group not in hw_groups:
            hw_groups[group] = []
        hw_groups[group].append({"name": enc_name, "label": info["label"], "type": info["type"]})

    for enc_name, info in sw.items():
        group = info["group"]
        if group not in sw_groups:
            sw_groups[group] = []
        sw_groups[group].append({"name": enc_name, "label": info["label"], "type": info["type"]})

    return hw_groups, sw_groups


def probe_file(filepath):
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format",
             "-show_streams", filepath],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None


def get_file_info(filepath):
    data = probe_file(filepath)
    if not data:
        return None

    info = {
        "filename": os.path.basename(filepath),
        "path": filepath,
        "duration": float(data.get("format", {}).get("duration", 0)),
        "size": int(data.get("format", {}).get("size", 0)),
        "bitrate": data.get("format", {}).get("bit_rate", "0"),
        "streams": [],
    }

    for stream in data.get("streams", []):
        codec_type = stream.get("codec_type", "")
        sinfo = {
            "type": codec_type,
            "codec": stream.get("codec_name", ""),
            "codec_long": stream.get("codec_long_name", ""),
            "width": stream.get("width", 0),
            "height": stream.get("height", 0),
            "fps": None,
            "pix_fmt": stream.get("pix_fmt", ""),
            "bitrate": stream.get("bit_rate", "0"),
            "channels": stream.get("channels", 0),
            "sample_rate": stream.get("sample_rate", "0"),
        }

        if codec_type == "video":
            fps_str = stream.get("r_frame_rate", "0/1")
            try:
                num, den = fps_str.split("/")
                sinfo["fps"] = round(float(num) / float(den), 2) if float(den) != 0 else 0
            except (ValueError, ZeroDivisionError):
                sinfo["fps"] = 0

        info["streams"].append(sinfo)

    return info


def detect_vaapi_devices():
    devices = []
    for i in range(128):
        path = f"/dev/dri/renderD{i}"
        if os.path.exists(path):
            devices.append(path)
    return devices

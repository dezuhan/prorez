import os
import json

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".prorez")
CONFIG_FILE = os.path.join(CONFIG_DIR, "settings.json")


DEFAULTS = {
    "theme": "dark",  # "light", "dark"
    "language": "en",
    "notifications": True,
    "first_launch": True,
}


STRINGS_EN = {
    "app_title": "Prorez - Video Converter",
    "app_subtitle": "FFmpeg GUI Converter",
    "add_files": "Add Files",
    "add_folder": "Add Folder",
    "start": "Start",
    "stop": "Stop",
    "remove_selected": "Remove Selected",
    "clear_queue": "Clear Queue",
    "settings": "Settings",
    "theme": "Theme",
    "light": "Light",
    "dark": "Dark",
    "system": "System",
    "davinci_tab": "DaVinci Linux",
    "web_tab": "Web/Social",
    "custom_tab": "Custom",
    "queue": "Conversion Queue",
    "status": "Status",
    "progress": "Progress",
    "duration": "Duration",
    "filename": "Filename",
    "video_codec": "Video Codec",
    "resolution": "Resolution",
    "format": "Format",
    "waiting": "Waiting",
    "converting": "Converting...",
    "completed": "Completed",
    "failed": "Failed",
    "cancelled": "Cancelled",
    "ready": "Ready. Detecting GPU...",
    "converting_status": "Converting: {} — {}%",
    "all_done": "Ready. All conversions complete.",
    "file_count": "{} file(s) in queue",
    "output_dir": "Output Dir",
    "choose": "Choose",
    "extra_args": "Extra Args",
    "notifications": "Notifications",
    "show_notif": "Show popup when file completes",
    "preset": "Preset",
    "encoder": "Encoder",
    "bitrate": "Bitrate",
    "audio_encoder": "Audio Encoder",
    "audio_bitrate": "Audio Bitrate",
    "video_quality": "Video Bitrate / Quality",
    "cpu_preset": "Preset (CPU)",
    "platform": "Platform",
    "resolution_label": "Resolution",
    "custom_label": "Custom / Manual",
    "custom_desc": "Full control: encoder, container, audio codec, bitrate, filters, etc.",
    "davinci_desc": "Intermediate codecs (ProRes/DNxHR) with PCM audio for full DaVinci Resolve Linux compatibility.",
    "web_desc": "H.264/HEVC for YouTube, Instagram, TikTok. AAC/Opus audio.",
    "quality_mode": "Quality Mode",
    "estimate": "Estimated size: —",
    "log_console": "Log / Console",
    "clear_log": "Clear Log",
    "view_error": "View Error Details",
    "copy_error": "Copy Error to Clipboard",
    "open_output": "Open Output Folder",
    "requeue": "Re-Queue (Convert Again)",
    "convert_stop": "Conversion in progress.",
    "stop_confirm": "Are you sure you want to quit? Conversion will be stopped.",
    "all_done_title": "All conversions complete!",
    "success": "Success: {}",
    "failed_count": "Failed: {}",
    "credits": "Credits",
    "credits_text": "Prorez - FFmpeg GPU Video Converter\n\nCreated by Dezuhan\n\nInstagram: instagram.com/dezuhan\nLinkedIn: linkedin.com/in/dzuhan\nGitHub: github.com/dezuhan",
}


def get_setting(key):
    return load_settings().get(key, DEFAULTS.get(key))


def set_setting(key, value):
    s = load_settings()
    s[key] = value
    save_settings(s)


def load_settings():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    try:
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(DEFAULTS)


def save_settings(s):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(s, f, indent=2)

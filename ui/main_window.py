import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, Gdk, Pango
import os
import threading

from core.gpu import get_encoder_groups, get_file_info, FORMAT_CONTAINERS, detect_vaapi_devices
from core.converter import ConversionJob, format_duration, format_size
from core.queue import ConversionQueue
from core.logger import log_session_summary, log_file_probe
from core.settings_manager import get_setting, set_setting, STRINGS_EN, DEFAULTS


class ProrezApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="com.prorez.app")
        self.queue = ConversionQueue()
        self.queue.on_job_update = self.on_queue_update
        self.queue.on_queue_finished = self.on_queue_finished
        self.all_jobs = []
        self.running = False

    def do_activate(self):
        win = ProrezWindow(self)
        win.connect("delete-event", self.on_window_delete)
        win.show_all()

    def on_window_delete(self, widget, event):
        if self.queue.running:
            dialog = Gtk.MessageDialog(
                transient_for=widget,
                modal=True,
                message_type=Gtk.MessageType.QUESTION,
                buttons=Gtk.ButtonsType.YES_NO,
                text="Conversion in progress."
            )
            dialog.format_secondary_text("Are you sure you want to quit? Conversion will be stopped.")
            response = dialog.run()
            dialog.destroy()
            if response == Gtk.ResponseType.YES:
                self.queue.stop()
            else:
                return True
        return False

    def on_queue_update(self, job, status):
        GLib.idle_add(self._update_ui, job, status)

    def on_queue_finished(self):
        GLib.idle_add(self._on_queue_finished)

    def _update_ui(self, job, status):
        for window in self.get_windows():
            if hasattr(window, "refresh_queue"):
                window.refresh_queue()
                window.update_controls()
                if status == "converting":
                    window._log_job_started(job, job.build_command())
                elif status == "completed":
                    window._log_job_result(job, True)
                elif status == "error":
                    window._log_job_result(job, False, job.error)
                if status in ("completed", "error") and window.chk_notify.get_active():
                    GLib.idle_add(window._show_file_notification, job, status)
        return False

    def _on_queue_finished(self):
        for window in self.get_windows():
            if hasattr(window, "on_queue_done"):
                window.on_queue_done()
        return False


class ProrezWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Prorez")
        self.app = app
        self.set_default_size(900, 600)
        self.set_border_width(12)

        icon_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "icon.png")
        if os.path.exists(icon_path):
            self.set_icon_from_file(icon_path)

        self.hw_encoders, self.sw_encoders = get_encoder_groups()
        all_encs = {**self.hw_encoders, **self.sw_encoders}
        default_enc = self._pick_default_encoder(all_encs)
        self.settings = {
            "container": "mp4",
            "encoder": default_enc["name"],
            "encoder_info": default_enc,
            "bitrate": "10M",
            "crf": None,
            "cq": None,
            "qp": None,
            "profile": None,
            "preset": "medium",
            "output_dir": os.path.expanduser("~/Videos"),
            "extra_args": "",
            "vaapi_devices": detect_vaapi_devices(),
        }

        self._build_ui()
        self._populate_encoders()
        self._apply_theme(get_setting("theme"))

    def _build_ui(self):
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)

        header = Gtk.HeaderBar()
        header.set_show_close_button(True)
        header.set_title("Prorez - Video Converter")
        header.set_subtitle("FFmpeg GPU Converter")

        btn_add = Gtk.Button("+ Add Files")
        btn_add.connect("clicked", self.on_add_files)
        header.pack_start(btn_add)

        btn_add_dir = Gtk.Button("Add Folder")
        btn_add_dir.connect("clicked", self.on_add_folder)
        header.pack_start(btn_add_dir)

        # Theme toggle button (top-right icon)
        self.btn_theme = Gtk.Button()
        self.btn_theme.set_tooltip_text("Toggle theme (Light/Dark/System)")
        self._update_theme_button_label()
        self.btn_theme.connect("clicked", self._on_theme_toggle)
        header.pack_end(self.btn_theme)

        # Settings button
        btn_settings = Gtk.Button("⚙")
        btn_settings.set_tooltip_text("Settings")
        btn_settings.connect("clicked", self._on_settings_clicked)
        header.pack_end(btn_settings)

        self.btn_start = Gtk.Button("▶ Start")
        self.btn_start.set_sensitive(False)
        self.btn_start.connect("clicked", self.on_start)
        header.pack_end(self.btn_start)

        self.btn_stop = Gtk.Button("■ Stop")
        self.btn_stop.set_sensitive(False)
        self.btn_stop.connect("clicked", self.on_stop)
        header.pack_end(self.btn_stop)

        self.set_titlebar(header)

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_wide_handle(True)

        queue_frame = Gtk.Frame()
        queue_frame.get_style_context().add_class("queue-frame")
        queue_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        queue_box.set_margin_start(8)
        queue_box.set_margin_end(8)
        queue_box.set_margin_top(8)
        queue_box.set_margin_bottom(8)

        queue_label = Gtk.Label()
        queue_label.set_markup("<b>Conversion Queue</b>")
        queue_label.set_halign(Gtk.Align.START)
        queue_box.pack_start(queue_label, False, False, 0)

        self.queue_store = Gtk.ListStore(str, str, str, int, str, str, str, str, object, str)
        self.queue_view = Gtk.TreeView(model=self.queue_store)
        self.queue_view.set_rubber_banding(True)
        self.queue_view.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        self.queue_view.set_tooltip_column(9)
        self.queue_view.connect("button-press-event", self._on_queue_button_press)

        columns_spec = [
            ("#", 0, 40, False),
            ("Nama File", 1, 220, False),
            ("Video Codec", 5, 100, False),
            ("Resolution", 6, 90, False),
            ("Format", 7, 70, False),
        ]

        for title, col_id, width, is_progress in columns_spec:
            renderer = Gtk.CellRendererText()
            renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
            col = Gtk.TreeViewColumn(title, renderer, text=col_id)
            col.set_resizable(True)
            col.set_min_width(width)
            col.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
            self.queue_view.append_column(col)

        self.status_column = Gtk.TreeViewColumn("Status")
        status_renderer = Gtk.CellRendererText()
        status_renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
        self.status_column.pack_start(status_renderer, True)
        self.status_column.add_attribute(status_renderer, "text", 2)
        self.status_column.set_resizable(True)
        self.status_column.set_min_width(120)
        self.status_column.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
        self.status_column.set_cell_data_func(status_renderer, self._status_cell_data)
        self.queue_view.append_column(self.status_column)

        progress_renderer = Gtk.CellRendererProgress()
        progress_col = Gtk.TreeViewColumn("Progress", progress_renderer, value=3)
        progress_col.set_resizable(True)
        progress_col.set_min_width(100)
        progress_col.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
        self.queue_view.append_column(progress_col)

        dur_renderer = Gtk.CellRendererText()
        dur_col = Gtk.TreeViewColumn("Durasi", dur_renderer, text=4)
        dur_col.set_resizable(True)
        dur_col.set_min_width(70)
        self.queue_view.append_column(dur_col)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.add(self.queue_view)
        queue_box.pack_start(scroll, True, True, 0)

        queue_actions = Gtk.Box(spacing=6)
        btn_remove = Gtk.Button("Remove Selected")
        btn_remove.connect("clicked", self.on_remove_selected)
        queue_actions.pack_start(btn_remove, False, False, 0)

        btn_clear = Gtk.Button("Clear Queue")
        btn_clear.connect("clicked", self.on_clear_queue)
        queue_actions.pack_start(btn_clear, False, False, 0)

        self.lbl_queue_count = Gtk.Label("0 file(s) in queue")
        self.lbl_queue_count.set_halign(Gtk.Align.START)
        queue_actions.pack_start(self.lbl_queue_count, True, True, 0)

        queue_box.pack_start(queue_actions, False, False, 0)
        queue_frame.add(queue_box)
        paned.pack1(queue_frame, True, True)

        settings_frame = Gtk.Frame()
        settings_frame.get_style_context().add_class("settings-frame")
        self._build_settings(settings_frame)
        paned.pack2(settings_frame, False, False)

        paned.set_position(580)

        main_box.pack_start(paned, True, True, 0)

        # Log panel (collapsible)
        self.log_expander = Gtk.Expander(label="Log / Console")
        self.log_expander.set_expanded(False)

        log_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        log_box.set_margin_start(8)
        log_box.set_margin_end(8)
        log_box.set_margin_bottom(4)

        self.log_view = Gtk.TextView()
        self.log_view.set_editable(False)
        self.log_view.set_monospace(True)
        self.log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.log_buffer = self.log_view.get_buffer()

        log_scroll = Gtk.ScrolledWindow()
        log_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        log_scroll.set_min_content_height(100)
        log_scroll.add(self.log_view)
        log_box.pack_start(log_scroll, True, True, 0)

        # Log actions
        log_actions = Gtk.Box(spacing=4)
        btn_clear_log = Gtk.Button("Clear Log")
        btn_clear_log.connect("clicked", self._on_clear_log)
        log_actions.pack_end(btn_clear_log, False, False, 0)
        log_box.pack_start(log_actions, False, False, 0)

        self.log_expander.add(log_box)
        main_box.pack_start(self.log_expander, False, False, 0)

        self.statusbar = Gtk.Statusbar()
        ctx = self.statusbar.get_context_id("prorez")
        self.statusbar.push(ctx, "Ready. Detecting GPU...")
        main_box.pack_start(self.statusbar, False, False, 0)

        self.add(main_box)

    def _build_settings(self, frame):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_start(8)
        box.set_margin_end(8)
        box.set_margin_top(8)
        box.set_margin_bottom(8)

        # Tab notebook
        self.notebook = Gtk.Notebook()
        self.notebook.set_show_tabs(True)
        self.notebook.set_show_border(False)

        # === TAB 1: DaVinci Linux ===
        davinci_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        davinci_box.set_margin_start(8)
        davinci_box.set_margin_end(8)
        davinci_box.set_margin_top(12)
        davinci_box.set_margin_bottom(8)
        self._build_davinci_tab(davinci_box)
        self.notebook.append_page(davinci_box, Gtk.Label(label="DaVinci Linux"))

        # === TAB 2: Web/Social ===
        web_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        web_box.set_margin_start(8)
        web_box.set_margin_end(8)
        web_box.set_margin_top(12)
        web_box.set_margin_bottom(8)
        self._build_web_tab(web_box)
        self.notebook.append_page(web_box, Gtk.Label(label="Web/Social"))

        # === TAB 3: Custom ===
        custom_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        custom_box.set_margin_start(8)
        custom_box.set_margin_end(8)
        custom_box.set_margin_top(12)
        custom_box.set_margin_bottom(8)
        self._build_custom_tab(custom_box)
        self.notebook.append_page(custom_box, Gtk.Label(label="Custom"))

        box.pack_start(self.notebook, True, True, 0)

        self.lbl_gpu_status = Gtk.Label()
        self.lbl_gpu_status.set_halign(Gtk.Align.START)
        self.lbl_gpu_status.set_line_wrap(True)
        box.pack_start(self.lbl_gpu_status, False, False, 4)

        frame.add(box)

    def _build_davinci_tab(self, box):
        """Preset optimized for DaVinci Resolve Linux"""
        lbl = Gtk.Label()
        lbl.set_markup("<b>Preset DaVinci Resolve Linux</b>\n<span size='small'>Intermediate codecs (ProRes/DNxHR) with PCM audio for full DaVinci Resolve Linux compatibility.</span>")
        lbl.set_halign(Gtk.Align.START)
        lbl.set_line_wrap(True)
        lbl.set_max_width_chars(45)
        box.pack_start(lbl, False, False, 0)

        grid = Gtk.Grid()
        grid.set_column_spacing(8)
        grid.set_row_spacing(8)

        row = 0
        grid.attach(Gtk.Label(label="Preset:"), 0, row, 1, 1)
        self.combo_davinci_preset = Gtk.ComboBoxText()
        self.davinci_presets = self._build_davinci_presets()
        for name in self.davinci_presets:
            self.combo_davinci_preset.append_text(name)
        self.combo_davinci_preset.set_active(0)
        self.combo_davinci_preset.connect("changed", self.on_davinci_preset_changed)
        grid.attach(self.combo_davinci_preset, 1, row, 1, 1)

        row += 1
        grid.attach(Gtk.Label(label="Audio Format:"), 0, row, 1, 1)
        self.combo_davinci_audio = Gtk.ComboBoxText()
        self.audio_formats_davinci = {
            "PCM 16-bit (pcm_s16le) – Recommended": {"audio_codec": "pcm_s16le", "audio_bitrate": None},
            "PCM 24-bit (pcm_s24le)": {"audio_codec": "pcm_s24le", "audio_bitrate": None},
            "PCM 32-bit float (pcm_f32le)": {"audio_codec": "pcm_f32le", "audio_bitrate": None},
            "AAC 192k": {"audio_codec": "aac", "audio_bitrate": "192k"},
            "AAC 256k": {"audio_codec": "aac", "audio_bitrate": "256k"},
            "AAC 320k": {"audio_codec": "aac", "audio_bitrate": "320k"},
            "Copy (passthrough)": {"audio_codec": "copy", "audio_bitrate": None},
        }
        for name in self.audio_formats_davinci:
            self.combo_davinci_audio.append_text(name)
        self.combo_davinci_audio.set_active(0)
        self.combo_davinci_audio.connect("changed", self.on_davinci_audio_changed)
        grid.attach(self.combo_davinci_audio, 1, row, 1, 1)

        row += 1
        grid.attach(Gtk.Label(label="Output Dir:"), 0, row, 1, 1)
        dir_box = Gtk.Box(spacing=4)
        self.lbl_outdir = Gtk.Label(label=self.settings["output_dir"])
        self.lbl_outdir.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.lbl_outdir.set_max_width_chars(25)
        dir_box.pack_start(self.lbl_outdir, True, True, 0)
        btn_outdir = Gtk.Button("Choose")
        btn_outdir.connect("clicked", self.on_choose_output_dir)
        dir_box.pack_start(btn_outdir, False, False, 0)
        grid.attach(dir_box, 1, row, 1, 1)

        box.pack_start(grid, False, False, 0)

        # Estimated size
        self.lbl_davinci_estimate = Gtk.Label()
        self.lbl_davinci_estimate.set_markup("<i>Estimated size: —</i>")
        self.lbl_davinci_estimate.set_halign(Gtk.Align.START)
        box.pack_start(self.lbl_davinci_estimate, False, False, 4)

    def _build_davinci_presets(self):
        presets = {}
        all_encs = {**self.hw_encoders, **self.sw_encoders}

        # GPU H.264 NVENC MOV
        gpu_h264 = self._find_encoder_by_group("nvidia", "h264")
        if gpu_h264:
            presets["NVENC H.264 MOV (PCM audio)"] = {
                "container": "mov", "encoder": gpu_h264["name"], "group": "nvidia",
                "cq": 18, "src_codec": "hevc", "audio_codec": "pcm_s16le",
                "quality_label": "Visually lossless", "target": "DaVinci Edit"
            }

        # ProRes
        prores = self._find_encoder_by_group("prores", "prores")
        if prores:
            presets["ProRes 422 HQ (Best Quality)"] = {
                "container": "mov", "encoder": prores["name"], "group": "prores",
                "profile": "3", "audio_codec": "pcm_s16le",
                "quality_label": "422 HQ ~220 Mbps", "target": "DaVinci Master"
            }
            presets["ProRes 422 LT (Balanced)"] = {
                "container": "mov", "encoder": prores["name"], "group": "prores",
                "profile": "1", "audio_codec": "pcm_s16le",
                "quality_label": "422 LT ~100 Mbps", "target": "DaVinci Proxy"
            }

        # DNxHR
        dnx = self._find_encoder_by_group("dnx", "dnxhd")
        if dnx:
            presets["DNxHR HQ (Cross-platform)"] = {
                "container": "mov", "encoder": dnx["name"], "group": "dnx",
                "profile": "dnxhr_hq", "audio_codec": "pcm_s16le",
                "quality_label": "DNxHR HQ ~220 Mbps", "target": "DaVinci Master"
            }
            presets["DNxHR SQ (Smaller)"] = {
                "container": "mov", "encoder": dnx["name"], "group": "dnx",
                "profile": "dnxhr_sq", "audio_codec": "pcm_s16le",
                "quality_label": "DNxHR SQ ~145 Mbps", "target": "DaVinci Proxy"
            }

        return presets

    def _build_web_tab(self, box):
        """Preset optimized for Web/Social Media"""
        lbl = Gtk.Label()
        lbl.set_markup("<b>Preset Web / Social Media</b>\n<span size='small'>H.264/HEVC for YouTube, Instagram, TikTok, etc. AAC/Opus audio.</span>")
        lbl.set_halign(Gtk.Align.START)
        lbl.set_line_wrap(True)
        lbl.set_max_width_chars(45)
        box.pack_start(lbl, False, False, 0)

        grid = Gtk.Grid()
        grid.set_column_spacing(8)
        grid.set_row_spacing(8)

        row = 0
        grid.attach(Gtk.Label(label="Platform:"), 0, row, 1, 1)
        self.combo_web_preset = Gtk.ComboBoxText()
        self.web_presets = self._build_web_presets()
        for name in self.web_presets:
            self.combo_web_preset.append_text(name)
        self.combo_web_preset.set_active(0)
        self.combo_web_preset.connect("changed", self.on_web_preset_changed)
        grid.attach(self.combo_web_preset, 1, row, 1, 1)

        row += 1
        grid.attach(Gtk.Label(label="Resolution:"), 0, row, 1, 1)
        self.combo_web_res = Gtk.ComboBoxText()
        for res in ["Asli", "1080p", "720p", "480p"]:
            self.combo_web_res.append_text(res)
        self.combo_web_res.set_active(0)
        self.combo_web_res.connect("changed", self.on_web_res_changed)
        grid.attach(self.combo_web_res, 1, row, 1, 1)

        row += 1
        grid.attach(Gtk.Label(label="Output Dir:"), 0, row, 1, 1)
        dir_box = Gtk.Box(spacing=4)
        self.lbl_outdir_web = Gtk.Label(label=self.settings["output_dir"])
        self.lbl_outdir_web.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.lbl_outdir_web.set_max_width_chars(25)
        dir_box.pack_start(self.lbl_outdir_web, True, True, 0)
        btn_outdir = Gtk.Button("Choose")
        btn_outdir.connect("clicked", self.on_choose_output_dir)
        dir_box.pack_start(btn_outdir, False, False, 0)
        grid.attach(dir_box, 1, row, 1, 1)

        box.pack_start(grid, False, False, 0)

        self.lbl_web_estimate = Gtk.Label()
        self.lbl_web_estimate.set_markup("<i>Estimated size: —</i>")
        self.lbl_web_estimate.set_halign(Gtk.Align.START)
        box.pack_start(self.lbl_web_estimate, False, False, 4)

    def _build_web_presets(self):
        presets = {}
        gpu_h264 = self._find_encoder_by_group("nvidia", "h264") or \
                   self._find_encoder_by_group("vaapi", "h264") or \
                   self._find_encoder_by_group("qsv", "h264")
        cpu_h264 = self._find_encoder_by_group("cpu", "h264")
        gpu_hevc = self._find_encoder_by_group("nvidia", "hevc") or \
                   self._find_encoder_by_group("vaapi", "hevc")

        # YouTube 1080p
        if gpu_h264:
            presets["YouTube 1080p (H.264 GPU)"] = {
                "container": "mp4", "encoder": gpu_h264["name"], "group": gpu_h264["group"],
                "bitrate": "8M", "cq": 23, "audio_codec": "aac", "audio_bitrate": "192k",
                "res": "1080p", "src_codec": "hevc", "target": "YouTube/General"
            }
        if cpu_h264:
            presets["YouTube 1080p (H.264 CPU)"] = {
                "container": "mp4", "encoder": cpu_h264["name"], "group": "cpu",
                "bitrate": "8M", "crf": 23, "audio_codec": "aac", "audio_bitrate": "192k",
                "res": "1080p", "src_codec": "hevc", "target": "YouTube/General"
            }

        # YouTube 4K
        if gpu_hevc:
            presets["YouTube 4K (HEVC GPU)"] = {
                "container": "mp4", "encoder": gpu_hevc["name"], "group": gpu_hevc["group"],
                "bitrate": "25M", "cq": 24, "audio_codec": "aac", "audio_bitrate": "192k",
                "res": "4K", "src_codec": "hevc", "target": "YouTube 4K"
            }

        # Instagram Reels/Stories (vertical)
        if gpu_h264:
            presets["Instagram Reels (H.264 GPU)"] = {
                "container": "mp4", "encoder": gpu_h264["name"], "group": gpu_h264["group"],
                "bitrate": "5M", "cq": 23, "audio_codec": "aac", "audio_bitrate": "128k",
                "res": "1080x1920", "src_codec": "hevc", "target": "Instagram/TikTok"
            }

        return presets

    def _build_custom_tab(self, box):
        """Full manual control"""
        lbl = Gtk.Label()
        lbl.set_markup("<b>Custom / Manual</b>\n<span size='small'>Full control: encoder, container, audio codec, bitrate, filters, etc.</span>")
        lbl.set_halign(Gtk.Align.START)
        lbl.set_line_wrap(True)
        box.pack_start(lbl, False, False, 0)

        grid = Gtk.Grid()
        grid.set_column_spacing(8)
        grid.set_row_spacing(8)

        row = 0
        grid.attach(Gtk.Label(label="Format:"), 0, row, 1, 1)
        self.combo_container = Gtk.ComboBoxText()
        for fmt in ["mp4", "mkv", "webm", "mov", "avi", "ts"]:
            self.combo_container.append_text(fmt)
        self.combo_container.set_active(0)
        self.combo_container.connect("changed", self.on_container_changed)
        grid.attach(self.combo_container, 1, row, 1, 1)

        row += 1
        grid.attach(Gtk.Label(label="Video Encoder:"), 0, row, 1, 1)
        self.combo_encoder = Gtk.ComboBoxText()
        self.combo_encoder.set_wrap_width(1)
        self.combo_encoder.connect("changed", self.on_encoder_changed)
        grid.attach(self.combo_encoder, 1, row, 1, 1)

        row += 1
        grid.attach(Gtk.Label(label="Audio Encoder:"), 0, row, 1, 1)
        self.combo_audio_encoder = Gtk.ComboBoxText()
        for acodec in ["pcm_s16le", "aac", "opus", "mp3", "flac", "copy"]:
            self.combo_audio_encoder.append_text(acodec)
        self.combo_audio_encoder.set_active(1)  # AAC default
        self.combo_audio_encoder.connect("changed", self.on_audio_encoder_changed)
        grid.attach(self.combo_audio_encoder, 1, row, 1, 1)

        row += 1
        grid.attach(Gtk.Label(label="Audio Bitrate:"), 0, row, 1, 1)
        self.entry_audio_bitrate = Gtk.Entry()
        self.entry_audio_bitrate.set_text("192k")
        self.entry_audio_bitrate.set_placeholder_text("128k, 192k, 256k, 320k")
        self.entry_audio_bitrate.connect("changed", self.on_audio_bitrate_changed)
        grid.attach(self.entry_audio_bitrate, 1, row, 1, 1)

        row += 1
        grid.attach(Gtk.Label(label="Video Bitrate / Quality:"), 0, row, 1, 1)
        bitrate_box = Gtk.Box(spacing=4)
        self.entry_bitrate = Gtk.Entry()
        self.entry_bitrate.set_text("10M")
        self.entry_bitrate.set_placeholder_text("e.g. 10M, 2000k, auto")
        self.entry_bitrate.connect("changed", self.on_bitrate_changed)
        bitrate_box.pack_start(self.entry_bitrate, True, True, 0)

        self.combo_quality_mode = Gtk.ComboBoxText()
        for mode in ["Bitrate (CBR/VBR)", "CQ (Constant Quality)", "CRF (CPU)"]:
            self.combo_quality_mode.append_text(mode)
        self.combo_quality_mode.set_active(0)
        self.combo_quality_mode.connect("changed", self.on_quality_mode_changed)
        bitrate_box.pack_start(self.combo_quality_mode, False, False, 0)
        grid.attach(bitrate_box, 1, row, 1, 1)

        row += 1
        grid.attach(Gtk.Label(label="Preset (CPU):"), 0, row, 1, 1)
        self.combo_preset = Gtk.ComboBoxText()
        for p in ["ultrafast", "superfast", "veryfast", "faster", "fast",
                   "medium", "slow", "slower", "veryslow"]:
            self.combo_preset.append_text(p)
        self.combo_preset.set_active(5)
        self.combo_preset.connect("changed", self.on_preset_changed)
        grid.attach(self.combo_preset, 1, row, 1, 1)

        row += 1
        grid.attach(Gtk.Label(label="Output Dir:"), 0, row, 1, 1)
        dir_box = Gtk.Box(spacing=4)
        self.lbl_outdir_custom = Gtk.Label(label=self.settings["output_dir"])
        self.lbl_outdir_custom.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.lbl_outdir_custom.set_max_width_chars(25)
        dir_box.pack_start(self.lbl_outdir_custom, True, True, 0)
        btn_outdir = Gtk.Button("Choose")
        btn_outdir.connect("clicked", self.on_choose_output_dir)
        dir_box.pack_start(btn_outdir, False, False, 0)
        grid.attach(dir_box, 1, row, 1, 1)

        row += 1
        grid.attach(Gtk.Label(label="Extra Args:"), 0, row, 1, 1)
        self.entry_extra = Gtk.Entry()
        self.entry_extra.set_placeholder_text("Additional ffmpeg args (e.g. -vf scale=1920:1080)")
        self.entry_extra.set_tooltip_text("Additional ffmpeg arguments")
        grid.attach(self.entry_extra, 1, row, 1, 1)

        box.pack_start(grid, False, False, 0)

        # Estimate label
        self.lbl_custom_estimate = Gtk.Label()
        self.lbl_custom_estimate.set_markup("<i>Estimated size: —</i>")
        self.lbl_custom_estimate.set_halign(Gtk.Align.START)
        box.pack_start(self.lbl_custom_estimate, False, False, 4)

    def _build_presets(self):
        presets = {}

        presets["Custom (Manual)"] = {}

        gpu_h264 = self._find_encoder_by_group("nvidia", "h264") or \
                   self._find_encoder_by_group("vaapi", "h264") or \
                   self._find_encoder_by_group("amf", "h264") or \
                   self._find_encoder_by_group("qsv", "h264")
        cpu_h264 = self._find_encoder_by_group("cpu", "h264")
        prores_enc = self._find_encoder_by_group("prores", "prores")
        dnx_enc = self._find_encoder_by_group("dnx", "dnxhd")

        if prores_enc:
            presets["DaVinci Resolve (ProRes 422 HQ)"] = {
                "container": "mov",
                "encoder": prores_enc["name"],
                "encoder_info": prores_enc,
                "profile": "3",
                "bitrate": "auto",
                "crf": None, "cq": None, "qp": None, "preset": None,
            }
            presets["DaVinci Resolve (ProRes 422 LT)"] = {
                "container": "mov",
                "encoder": prores_enc["name"],
                "encoder_info": prores_enc,
                "profile": "1",
                "bitrate": "auto",
                "crf": None, "cq": None, "qp": None, "preset": None,
            }

        if dnx_enc:
            presets["DaVinci Resolve (DNxHR HQ)"] = {
                "container": "mov",
                "encoder": dnx_enc["name"],
                "encoder_info": dnx_enc,
                "profile": "dnxhr_hq",
                "bitrate": "auto",
                "crf": None, "cq": None, "qp": None, "preset": None,
            }
            presets["DaVinci Resolve (DNxHR SQ)"] = {
                "container": "mov",
                "encoder": dnx_enc["name"],
                "encoder_info": dnx_enc,
                "profile": "dnxhr_sq",
                "bitrate": "auto",
                "crf": None, "cq": None, "qp": None, "preset": None,
            }

        if gpu_h264:
            presets["Phone to Edit (GPU H.264 MOV)"] = {
                "container": "mov",
                "encoder": gpu_h264["name"],
                "encoder_info": gpu_h264,
                "cq": 18,
                "bitrate": "auto",
                "crf": None, "qp": None, "preset": None, "profile": None,
            }

        if cpu_h264:
            presets["Phone to Edit (CPU H.264 High)"] = {
                "container": "mov",
                "encoder": cpu_h264["name"],
                "encoder_info": cpu_h264,
                "crf": 18, "preset": "medium",
                "bitrate": "auto",
                "cq": None, "qp": None, "profile": None,
            }

        if gpu_h264:
            presets["Web / Sharing (GPU H.264 MP4)"] = {
                "container": "mp4",
                "encoder": gpu_h264["name"],
                "encoder_info": gpu_h264,
                "cq": 23,
                "bitrate": "auto",
                "crf": None, "qp": None, "preset": None, "profile": None,
            }

        if cpu_h264:
            presets["Web / Sharing (CPU H.264 MP4)"] = {
                "container": "mp4",
                "encoder": cpu_h264["name"],
                "encoder_info": cpu_h264,
                "crf": 23, "preset": "medium",
                "bitrate": "auto",
                "cq": None, "qp": None, "profile": None,
            }

        return presets

    def _find_encoder_by_group(self, group, etype):
        all_encs = {**self.hw_encoders, **self.sw_encoders}
        for gname, encoders in all_encs.items():
            if gname == group:
                for enc in encoders:
                    if enc["type"] == etype:
                        result = enc.copy()
                        result["group"] = gname
                        return result
        return None

    def _find_encoder_combo_index(self, enc_data):
        for i in range(self.combo_encoder.get_model().iter_n_children(None)):
            active = self.combo_encoder.get_model()[i][0]
            if active in self.all_encoder_data and self.all_encoder_data[active] == enc_data:
                return i
        return 0

    def _pick_default_encoder(self, all_encs):
        for group in ["nvidia", "vaapi", "qsv", "amf", "cpu", "prores", "dnx"]:
            if group in all_encs:
                for enc in all_encs[group]:
                    if enc["type"] in ("h264", "hevc"):
                        return enc
        for group in ["nvidia", "vaapi", "qsv", "amf", "cpu", "prores", "dnx"]:
            if group in all_encs:
                return all_encs[group][0]
        return {"name": "libx264", "group": "cpu", "type": "h264"}

    def _populate_encoders(self):
        self.combo_encoder.remove_all()
        self.all_encoder_data = {}

        gpu = self.hw_encoders
        cpu = self.sw_encoders

        active_groups = []
        group_labels = {
            "nvidia": "NVIDIA NVENC",
            "vaapi": "VA-API",
            "amf": "AMD AMF",
            "qsv": "Intel QSV",
            "vulkan": "Vulkan",
            "prores": "Intermediate (ProRes)",
            "dnx": "Intermediate (DNxHD)",
            "cpu": "CPU (Software)",
        }

        first_active = None

        order = ["nvidia", "vaapi", "amf", "qsv", "vulkan", "prores", "dnx", "cpu"]
        for group_key in order:
            for src in [gpu, cpu]:
                if group_key in src:
                    active_groups.append(group_key)
                    label = group_labels.get(group_key, group_key)
                    for enc in src[group_key]:
                        display = f"{label}: {enc['label']}"
                        self.combo_encoder.append_text(display)
                        self.all_encoder_data[display] = enc
                        if first_active is None:
                            first_active = display

        if first_active:
            self.combo_encoder.set_active(0)
        else:
            self.combo_encoder.append_text("mpeg4 (CPU)")
            self.all_encoder_data["mpeg4 (CPU)"] = {"name": "mpeg4", "group": "cpu", "type": "mpeg4"}
            self.combo_encoder.set_active(0)

        self._update_gpu_status(active_groups)

    def _update_gpu_status(self, groups):
        status_parts = []
        if "nvidia" in groups:
            status_parts.append("NVIDIA NVENC detected")
        if "vaapi" in groups:
            status_parts.append("VA-API detected")
        if "amf" in groups:
            status_parts.append("AMD AMF detected")
        if "qsv" in groups:
            status_parts.append("Intel QSV detected")
        if "vulkan" in groups:
            status_parts.append("Vulkan detected")
        if "cpu" in groups:
            status_parts.append("CPU encoder available")

        if not status_parts:
            self.lbl_gpu_status.set_markup(
                "<span foreground='orange'>No GPU encoder detected. Using CPU.</span>"
            )
        else:
            self.lbl_gpu_status.set_markup(
                f"<span foreground='green'>GPU: {', '.join(status_parts)}</span>"
            )

    def on_container_changed(self, combo):
        self.settings["container"] = combo.get_active_text()

    def _update_theme_button_label(self):
        theme = get_setting("theme")
        labels = {"light": "☀", "dark": "☾"}
        self.btn_theme.set_label(labels.get(theme, "☾"))

    def _on_theme_toggle(self, btn):
        current = get_setting("theme")
        order = ["light", "dark"]
        next_theme = order[(order.index(current) + 1) % len(order)]
        set_setting("theme", next_theme)
        self._apply_theme(next_theme)
        self._update_theme_button_label()

    def _apply_theme(self, theme):
        if theme == "dark":
            Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", True)
        else:
            Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", False)

    def _on_settings_clicked(self, btn):
        dialog = Gtk.Dialog(
            title="Settings",
            transient_for=self,
            modal=True,
            destroy_with_parent=True
        )
        dialog.add_buttons(Gtk.STOCK_CLOSE, Gtk.ResponseType.CLOSE)
        dialog.set_default_size(420, 350)

        notebook = Gtk.Notebook()

        # === Theme tab ===
        theme_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        theme_box.set_margin_start(16)
        theme_box.set_margin_end(16)
        theme_box.set_margin_top(16)

        lbl_theme = Gtk.Label()
        lbl_theme.set_markup("<b>Theme</b>")
        lbl_theme.set_halign(Gtk.Align.START)
        theme_box.pack_start(lbl_theme, False, False, 0)

        current_theme = get_setting("theme")
        self._settings_theme_combo = Gtk.ComboBoxText()
        for t, label in [("light", "☀ Light"), ("dark", "☾ Dark")]:
            self._settings_theme_combo.append_text(label)
        idx = 0 if current_theme == "light" else 1
        self._settings_theme_combo.set_active(idx)
        self._settings_theme_combo.connect("changed", lambda c: self._on_settings_theme_changed(c, dialog))
        theme_box.pack_start(self._settings_theme_combo, False, False, 0)

        # Notification checkbox
        self._settings_chk_notify = Gtk.CheckButton("Show popup when file completes")
        self._settings_chk_notify.set_active(get_setting("notifications"))
        self._settings_chk_notify.connect("toggled", lambda c: set_setting("notifications", c.get_active()))
        theme_box.pack_start(self._settings_chk_notify, False, False, 6)

        notebook.append_page(theme_box, Gtk.Label(label="General"))

        # === Credits tab ===
        credit_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        credit_box.set_margin_start(16)
        credit_box.set_margin_end(16)
        credit_box.set_margin_top(16)

        lbl_credit = Gtk.Label()
        lbl_credit.set_markup(
            "<b>Prorez</b> - FFmpeg GPU Video Converter\n\n"
            "Created by <b>Dezuhan</b>\n\n"
            "📷 <a href='https://instagram.com/dezuhan'>instagram.com/dezuhan</a>\n"
            "🔗 <a href='https://linkedin.com/in/dzuhan'>linkedin.com/in/dzuhan</a>\n"
            "💻 <a href='https://github.com/dezuhan'>github.com/dezuhan</a>"
        )
        lbl_credit.set_halign(Gtk.Align.START)
        lbl_credit.set_line_wrap(True)
        credit_box.pack_start(lbl_credit, False, False, 0)

        notebook.append_page(credit_box, Gtk.Label(label="Credits"))

        dialog.get_content_area().add(notebook)
        dialog.show_all()
        dialog.run()
        dialog.destroy()

    def _on_settings_theme_changed(self, combo, dialog):
        idx = combo.get_active()
        theme = "light" if idx == 0 else "dark"
        set_setting("theme", theme)
        self._apply_theme(theme)
        self._update_theme_button_label()

    def _on_clear_log(self, btn):
        self.log_buffer.set_text("")

    def _log_to_console(self, text, tag=None):
        end_iter = self.log_buffer.get_end_iter()
        self.log_buffer.insert(end_iter, text + "\n")
        # Auto-scroll to bottom
        end_iter = self.log_buffer.get_end_iter()
        mark = self.log_buffer.create_mark("end", end_iter, False)
        self.log_view.scroll_to_mark(mark, 0.0, True, 0.0, 0.0)

    def _log_job_started(self, job, cmd):
        GLib.idle_add(self._log_to_console, f"━━━ {job.filename} ━━━")
        GLib.idle_add(self._log_to_console, f"  ▶ CMD: {' '.join(cmd)}")
        self.log_expander.set_expanded(True)

    def _log_job_result(self, job, success, error=None):
        if success:
            msg = f"  ✅ DONE: {job.filename} → {job.output_path or ''}"
        else:
            msg = f"  ❌ FAILED: {job.filename}"
            if error:
                err_short = error.strip().split('\n')[-1]
                msg += f" | {err_short}"
        GLib.idle_add(self._log_to_console, msg)
        GLib.idle_add(self._log_to_console, "")

    def on_encoder_changed(self, combo):
        active_text = combo.get_active_text()
        if active_text and active_text in self.all_encoder_data:
            enc_data = self.all_encoder_data[active_text]
            self.settings["encoder"] = enc_data["name"]
            self.settings["encoder_info"] = enc_data
        else:
            self.settings["encoder"] = "libx264"
            self.settings["encoder_info"] = {"group": "cpu"}

    def on_bitrate_changed(self, entry):
        self.settings["bitrate"] = entry.get_text()

    def on_preset_changed(self, combo):
        active = combo.get_active_text()
        if not active or active not in self.preset_data:
            return
        data = self.preset_data[active]
        if not data:
            return

        if "container" in data:
            for i, fmt in enumerate(["mp4", "mkv", "webm", "mov", "avi", "ts"]):
                if fmt == data["container"]:
                    self.combo_container.set_active(i)
                    self.settings["container"] = data["container"]
                    break

        if "encoder" in data and "encoder_info" in data:
            idx = self._find_encoder_combo_index(data["encoder_info"])
            self.combo_encoder.set_active(idx)
            self.settings["encoder"] = data["encoder"]
            self.settings["encoder_info"] = data["encoder_info"]

        if "bitrate" in data:
            self.entry_bitrate.set_text(data["bitrate"])
            self.settings["bitrate"] = data["bitrate"]

        for k in ("crf", "cq", "qp", "preset", "profile"):
            if k in data:
                self.settings[k] = data[k]

    def on_choose_output_dir(self, btn):
        dialog = Gtk.FileChooserDialog(
            title="Select Output Folder", parent=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER
        )
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Choose", Gtk.ResponseType.OK)
        current = self.settings.get("output_dir", "")
        if current and os.path.isdir(current):
            dialog.set_current_folder(current)

        if dialog.run() == Gtk.ResponseType.OK:
            path = dialog.get_filename()
            self.settings["output_dir"] = path
            # Update all output dir labels
            if hasattr(self, 'lbl_outdir'):
                self.lbl_outdir.set_text(path)
            if hasattr(self, 'lbl_outdir_web'):
                self.lbl_outdir_web.set_text(path)
            if hasattr(self, 'lbl_outdir_custom'):
                self.lbl_outdir_custom.set_text(path)
        dialog.destroy()

    def on_notebook_page_changed(self, notebook, page, page_num):
        # Page 0: DaVinci, 1: Web, 2: Custom
        self.update_estimate_size()

    def on_davinci_preset_changed(self, combo):
        active = combo.get_active_text()
        if not active or active not in self.davinci_presets:
            return
        data = self.davinci_presets[active]
        if not data:
            return

        self.settings["container"] = data["container"]
        self.settings["encoder"] = data["encoder"]
        self.settings["encoder_info"] = {"group": data["group"], "type": "h264"}
        if "cq" in data:
            self.settings["cq"] = data["cq"]
        if "src_codec" in data:
            self.settings["src_codec"] = data["src_codec"]
        if "profile" in data:
            self.settings["profile"] = data["profile"]

        # Auto-select PCM as default audio when preset changes
        if "audio_codec" in data:
            self.combo_davinci_audio.set_active(0)  # PCM 16-bit
            self.settings["audio_codec"] = data["audio_codec"]
        self.update_estimate_size()

    def on_davinci_audio_changed(self, combo):
        active = combo.get_active_text()
        if active and active in getattr(self, 'audio_formats_davinci', {}):
            info = self.audio_formats_davinci[active]
            self.settings["audio_codec"] = info["audio_codec"]
            self.settings["audio_bitrate"] = info.get("audio_bitrate", "192k")

    def on_web_preset_changed(self, combo):
        active = combo.get_active_text()
        if not active or active not in self.web_presets:
            return
        data = self.web_presets[active]
        if not data:
            return

        self.settings["container"] = data["container"]
        self.settings["encoder"] = data["encoder"]
        self.settings["encoder_info"] = {"group": data["group"], "type": "h264"}
        self.settings["bitrate"] = data.get("bitrate", "10M")
        if "cq" in data:
            self.settings["cq"] = data["cq"]
        if "crf" in data:
            self.settings["crf"] = data["crf"]
        self.settings["audio_codec"] = data.get("audio_codec", "aac")
        self.settings["audio_bitrate"] = data.get("audio_bitrate", "192k")
        if "res" in data:
            self.settings["target_res"] = data["res"]
            # Update resolution combo
            res_map = {"Asli": 0, "1080p": 1, "720p": 2, "480p": 3, "1080x1920": 4}
            if data["res"] in res_map and hasattr(self, 'combo_web_res'):
                self.combo_web_res.set_active(res_map[data["res"]])
        self.update_estimate_size()

    def on_web_res_changed(self, combo):
        active = combo.get_active_text()
        if active:
            self.settings["target_res"] = active
            self.update_estimate_size()

    def on_audio_encoder_changed(self, combo):
        active = combo.get_active_text()
        if active:
            self.settings["audio_codec"] = active
            self.update_estimate_size()

    def on_audio_bitrate_changed(self, entry):
        self.settings["audio_bitrate"] = entry.get_text()
        self.update_estimate_size()

    def on_quality_mode_changed(self, combo):
        active = combo.get_active_text()
        if active:
            self.settings["quality_mode"] = active
            self.update_estimate_size()

    def update_estimate_size(self):
        # Update all estimate labels based on current settings
        pass  # Will implement with actual calculation

    def _pick_default_encoder(self, all_encs):
        for group in ["nvidia", "vaapi", "qsv", "amf", "cpu", "prores", "dnx"]:
            if group in all_encs:
                for enc in all_encs[group]:
                    if enc["type"] in ("h264", "hevc"):
                        return enc
        for group in ["nvidia", "vaapi", "qsv", "amf", "cpu", "prores", "dnx"]:
            if group in all_encs:
                return all_encs[group][0]
        return {"name": "mpeg4", "group": "cpu", "type": "mpeg4"}

    def update_estimate_size(self):
        """Calculate and display estimated output size"""
        # Get source file info for duration
        duration = 0
        if self.app.all_jobs:
            for job in self.app.all_jobs:
                if job.duration > 0:
                    duration = job.duration
                    break
        
        if duration <= 0:
            est_text = "<i>Estimated size: — (duration unknown)</i>"
            self._set_estimate_labels(est_text)
            return

        # Get video bitrate from settings
        vbitrate_str = self.settings.get("bitrate", "10M")
        abitrate_str = self.settings.get("audio_bitrate", "192k")

        def parse_bitrate(s):
            s = s.strip().upper()
            if s.endswith("K"):
                return int(s[:-1]) * 1000
            elif s.endswith("M"):
                return int(s[:-1]) * 1000000
            elif s.endswith("G"):
                return int(s[:-1]) * 1000000000
            try:
                return int(s)
            except:
                return 10000000

        vbitrate = parse_bitrate(vbitrate_str)
        abitrate = parse_bitrate(abitrate_str)

        # Quality mode adjustments
        quality_mode = self.settings.get("quality_mode", "Bitrate (CBR/VBR)")
        if quality_mode == "CQ (Constant Quality)" and self.settings.get("cq"):
            # CQ is roughly equivalent to some bitrate, estimate
            cq = self.settings.get("cq", 23)
            # Rough estimate: CQ 18 ~ 10M, CQ 23 ~ 5M, CQ 28 ~ 2.5M for 1080p
            vbitrate = max(10000000 * (18 / cq) ** 2, 500000)
        elif quality_mode == "CRF (CPU)" and self.settings.get("crf"):
            crf = self.settings.get("crf", 23)
            vbitrate = max(10000000 * (23 / crf) ** 2, 500000)

        total_bitrate = vbitrate + abitrate
        est_bytes = (total_bitrate / 8) * duration
        est_mb = est_bytes / 1024 / 1024

        est_text = f"<i>Estimated size: ~{est_mb:.1f} MB ({est_bytes/1024/1024/1024:.2f} GB)</i>"
        self._set_estimate_labels(est_text)

    def _set_estimate_labels(self, text):
        if hasattr(self, 'lbl_davinci_estimate'):
            self.lbl_davinci_estimate.set_markup(text)
        if hasattr(self, 'lbl_web_estimate'):
            self.lbl_web_estimate.set_markup(text)
        if hasattr(self, 'lbl_custom_estimate'):
            self.lbl_custom_estimate.set_markup(text)

    def _status_cell_data(self, column, renderer, model, tree_iter, user_data):
        job = model.get_value(tree_iter, 8)
        if job and job.error and job.status == "error":
            error_short = job.error.strip().split("\n")[-1]
            if len(error_short) > 120:
                error_short = error_short[:120] + "..."
            renderer.set_property("text", f"Failed: {error_short}")

    def _on_queue_button_press(self, treeview, event):
        if event.button != 3:
            return False

        path_info = treeview.get_path_at_pos(int(event.x), int(event.y))
        if not path_info:
            return False

        path, column, cell_x, cell_y = path_info
        treeview.get_selection().select_path(path)

        tree_iter = treeview.get_model().get_iter(path)
        job = treeview.get_model().get_value(tree_iter, 8)

        menu = Gtk.Menu()

        if job and job.status == "error" and job.error:
            item_view = Gtk.MenuItem(label="View Error Details")
            item_view.connect("activate", lambda _: self._show_error_dialog(job))
            menu.append(item_view)

            item_copy = Gtk.MenuItem(label="Copy Error to Clipboard")
            item_copy.connect("activate", lambda _: self._copy_to_clipboard(job.error))
            menu.append(item_copy)

            menu.append(Gtk.SeparatorMenuItem())

        if job and job.status in ("completed", "error", "cancelled") and not self.app.queue.running:
            item_requeue = Gtk.MenuItem(label="Re-Queue (Convert Again)")
            item_requeue.connect("activate", lambda _: self._requeue_job(job))
            menu.append(item_requeue)
            menu.append(Gtk.SeparatorMenuItem())

        item_open_out = Gtk.MenuItem(label="Open Output Folder")
        item_open_out.connect("activate", lambda _: self._open_output_folder(job))
        item_open_out.set_sensitive(job and job.output_path and os.path.exists(os.path.dirname(job.output_path)))
        menu.append(item_open_out)

        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    def _requeue_job(self, job):
        job.settings["output_dir"] = self.settings["output_dir"]
        for k in ("encoder", "encoder_info", "container", "bitrate",
                   "crf", "cq", "qp", "preset", "profile", "extra_args", "vaapi_devices",
                   "audio_codec", "audio_bitrate", "quality_mode", "target_res", "src_codec"):
            job.settings[k] = self.settings.get(k)
        self.app.queue.requeue(job)
        self.refresh_queue()
        if self.app.queue.queue_size > 0:
            self.btn_start.set_sensitive(True)
        dialog = Gtk.Dialog(
            title=f"Error Detail: {job.filename}",
            transient_for=self,
            modal=True,
            destroy_with_parent=True
        )
        dialog.add_buttons(Gtk.STOCK_CLOSE, Gtk.ResponseType.CLOSE)

        textview = Gtk.TextView()
        textview.set_editable(False)
        textview.set_cursor_visible(True)
        textview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        textview.set_monospace(True)
        buffer = textview.get_buffer()
        buffer.set_text(job.error)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_min_content_width(600)
        scrolled.set_min_content_height(350)
        scrolled.add(textview)

        # Copy button
        btn_copy = Gtk.Button(label="📋 Copy Error")
        btn_copy.connect("clicked", lambda _: self._copy_to_clipboard(job.error))

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.pack_start(scrolled, True, True, 0)
        box.pack_end(btn_copy, False, False, 0)

        dialog.get_content_area().add(box)
        dialog.show_all()

        # Focus textview so user can select/copy immediately
        textview.grab_focus()
        dialog.run()
        dialog.destroy()

    def _copy_to_clipboard(self, text):
        clip = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        clip.set_text(text, -1)
        self.statusbar.push(self.statusbar.get_context_id("prorez"), "Error copied to clipboard")

    def _open_output_folder(self, job):
        if job and job.output_path:
            folder = os.path.dirname(job.output_path)
            if os.path.isdir(folder):
                Gtk.show_uri_on_window(None, f"file://{folder}", Gtk.get_current_event_time())
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Add", Gtk.ResponseType.OK)

        filter_vid = Gtk.FileFilter()
        filter_vid.set_name("Video Files")
        for ext in ["*.mp4", "*.mkv", "*.avi", "*.mov", "*.wmv", "*.flv", "*.webm",
                     "*.m4v", "*.ts", "*.mts", "*.m2ts", "*.3gp", "*.ogv", "*.vob"]:
            filter_vid.add_pattern(ext)
        dialog.add_filter(filter_vid)

        filter_all = Gtk.FileFilter()
        filter_all.set_name("All Files")
        filter_all.add_pattern("*")
        dialog.add_filter(filter_all)

        if dialog.run() == Gtk.ResponseType.OK:
            filenames = dialog.get_filenames()
            self._add_files(filenames)
        dialog.destroy()

    def on_add_files(self, btn):
        dialog = Gtk.FileChooserDialog(
            title="Select Video Files", parent=self,
            action=Gtk.FileChooserAction.OPEN
        )
        dialog.set_select_multiple(True)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Add", Gtk.ResponseType.OK)

        filter_vid = Gtk.FileFilter()
        filter_vid.set_name("Video Files")
        for ext in ["*.mp4", "*.mkv", "*.avi", "*.mov", "*.wmv", "*.flv", "*.webm",
                     "*.m4v", "*.ts", "*.mts", "*.m2ts", "*.3gp", "*.ogv", "*.vob"]:
            filter_vid.add_pattern(ext)
        dialog.add_filter(filter_vid)

        filter_all = Gtk.FileFilter()
        filter_all.set_name("All Files")
        filter_all.add_pattern("*")
        dialog.add_filter(filter_all)

        if dialog.run() == Gtk.ResponseType.OK:
            filenames = dialog.get_filenames()
            self._add_files(filenames)
        dialog.destroy()

    def on_add_folder(self, btn):
        dialog = Gtk.FileChooserDialog(
            title="Select Folder with Videos", parent=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER
        )
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Buka", Gtk.ResponseType.OK)

        if dialog.run() == Gtk.ResponseType.OK:
            folder = dialog.get_filename()
            video_exts = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm",
                          ".m4v", ".ts", ".mts", ".m2ts", ".3gp", ".ogv", ".vob", ".mpg", ".mpeg"}
            files = []
            for f in os.listdir(folder):
                _, ext = os.path.splitext(f)
                if ext.lower() in video_exts:
                    files.append(os.path.join(folder, f))
            self._add_files(files)
        dialog.destroy()

    def _add_files(self, filenames):
        jobs = []
        for f in filenames:
            file_info = get_file_info(f)
            duration = format_duration(file_info["duration"]) if file_info else "?"
            job = ConversionJob(f, self.settings["output_dir"], dict(self.settings))
            job.duration = file_info["duration"] if file_info else 0

            if file_info:
                log_file_probe(f, file_info)
                video_stream = None
                for s in file_info.get("streams", []):
                    if s["type"] == "video":
                        video_stream = s
                        break
                if video_stream:
                    job.video_codec = video_stream.get("codec", "")
                    job.settings["src_codec"] = job.video_codec
                    w = video_stream.get("width", 0)
                    h = video_stream.get("height", 0)
                    job.resolution = f"{w}x{h}" if w and h else ""
                else:
                    job.video_codec = "-"
                    job.resolution = "-"
                job.container_format = os.path.splitext(f)[1].lstrip(".").upper()
            else:
                job.video_codec = "?"
                job.resolution = "?"
                job.container_format = "?"

            self.app.all_jobs.append(job)
            jobs.append(job)

            dur_str = format_duration(file_info["duration"]) if file_info else "?"
            self.queue_store.append([
                f"{len(self.queue_store) + 1}",
                os.path.basename(f),
                "Waiting",
                0,
                dur_str,
                job.video_codec,
                job.resolution,
                job.container_format,
                job,
                f,
            ])

        self.app.queue.add_batch(jobs)
        self.refresh_queue()
        if self.app.queue.queue_size > 0:
            self.btn_start.set_sensitive(True)

    def on_remove_selected(self, btn):
        if self.app.queue.running:
            dialog = Gtk.MessageDialog(
                transient_for=self, modal=True,
                message_type=Gtk.MessageType.WARNING,
                buttons=Gtk.ButtonsType.OK_CANCEL,
                text="Stop conversion before removing items."
            )
            dialog.run()
            dialog.destroy()
            return

        _, paths = self.queue_view.get_selection().get_selected_rows()
        if not paths:
            return

        to_remove = []
        for path in reversed(paths):
            tree_iter = self.queue_store.get_iter(path)
            job = self.queue_store.get_value(tree_iter, 8)
            if job and job.status not in ("converting",):
                to_remove.append(job)

        self.app.queue.remove_many(to_remove)
        for j in to_remove:
            if j in self.app.all_jobs:
                self.app.all_jobs.remove(j)

        self.refresh_queue()

    def on_clear_queue(self, btn):
        if self.app.queue.running:
            dialog = Gtk.MessageDialog(
                transient_for=self, modal=True,
                message_type=Gtk.MessageType.WARNING,
                buttons=Gtk.ButtonsType.OK_CANCEL,
                text="Stop conversion first."
            )
            dialog.run()
            dialog.destroy()
            return

        self.app.queue.clear()
        self.app.all_jobs.clear()
        self.queue_store.clear()
        self.lbl_queue_count.set_text("0 file(s) in queue")
        self.btn_start.set_sensitive(False)

    def on_start(self, btn):
        if self.app.queue.running:
            return

        jobs = self.app.queue.get_queue()
        for job in jobs:
            job.output_dir = self.settings["output_dir"]
            for k in ("encoder", "encoder_info", "container", "bitrate",
                       "crf", "cq", "qp", "preset", "profile", "extra_args", "vaapi_devices",
                       "audio_codec", "audio_bitrate", "quality_mode", "target_res", "src_codec"):
                job.settings[k] = self.settings.get(k)

        self.app.queue.start()
        self.btn_start.set_sensitive(False)
        self.btn_stop.set_sensitive(True)

        if self.app.queue.queue_size == 0:
            ctx = self.statusbar.get_context_id("prorez")
            self.statusbar.push(ctx, "No files in queue.")
            self.btn_start.set_sensitive(False)
            self.btn_stop.set_sensitive(False)
            self.app.queue.running = False
            return

    def on_stop(self, btn):
        self.app.queue.stop()
        self.btn_stop.set_sensitive(False)
        self.btn_start.set_sensitive(self.app.queue.queue_size > 0)
        self.refresh_queue()

    def refresh_queue(self):
        self.queue_store.clear()
        items = self.app.queue.get_all_items()

        idx = 1
        for job in items:
            if job.status == "completed":
                status_text = "Completed"
                progress = 100
            elif job.status == "converting":
                status_text = "Converting..."
                progress = int(job.progress)
            elif job.status == "error":
                status_text = "Failed"
                progress = int(job.progress)
            elif job.status == "cancelled":
                status_text = "Cancelled"
                progress = int(job.progress)
            else:
                status_text = "Waiting"
                progress = 0

            dur_str = format_duration(job.duration) if job.duration else "?"

            tooltip = job.filepath
            if job.status == "error" and job.error:
                tooltip = f"ERROR:\n{job.error.strip()}"
            elif job.status == "completed":
                tooltip = f"Output: {job.output_path or ''}"
            elif job.status == "converting":
                tooltip = f"Converting: {job.filename}\nProgress: {int(job.progress)}%\nFPS: {job.current_fps:.1f}\nSpeed: {job.speed:.2f}x"

            self.queue_store.append([
                str(idx),
                job.filename,
                status_text,
                progress,
                dur_str,
                job.video_codec or "",
                job.resolution or "",
                job.container_format or "",
                job,
                tooltip,
            ])
            idx += 1

        self.lbl_queue_count.set_text(f"{len(items)} file(s) in queue")
        self.btn_start.set_sensitive(
            not self.app.queue.running and self.app.queue.queue_size > 0
        )
        self.btn_stop.set_sensitive(self.app.queue.running)

        ctx = self.statusbar.get_context_id("prorez")
        if self.app.queue.running:
            current = self.app.queue.current_job
            if current:
                self.statusbar.push(ctx, f"Converting: {current.filename} — {int(current.progress)}%")
        elif self.app.queue.is_idle:
            self.statusbar.push(ctx, "Ready. All conversions complete.")

    def update_controls(self):
        self.refresh_queue()

    def on_queue_done(self):
        self.btn_start.set_sensitive(False)
        self.btn_stop.set_sensitive(False)
        self.refresh_queue()

        log_session_summary(self.app.all_jobs)

        dialog = Gtk.MessageDialog(
            transient_for=self, modal=True,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK,
            text="All conversions complete!"
        )
        completed = sum(1 for j in self.app.all_jobs if j.status == "completed")
        failed = sum(1 for j in self.app.all_jobs if j.status == "error")
        dialog.format_secondary_text(
            f"Success: {completed}\nFailed: {failed}"
        )
        dialog.run()
        dialog.destroy()

    def _show_file_notification(self, job, status):
        if status == "completed":
            msg = f"Completed: {job.filename}"
            detail = f"Output: {job.output_path}"
            msg_type = Gtk.MessageType.INFO
        elif status == "error":
            msg = f"Failed: {job.filename}"
            detail = job.error.split("\n")[0] if job.error else "Unknown error"
            msg_type = Gtk.MessageType.ERROR
        else:
            return

        dialog = Gtk.MessageDialog(
            transient_for=self, modal=False,
            message_type=msg_type,
            buttons=Gtk.ButtonsType.OK,
            text=msg
        )
        dialog.format_secondary_text(detail)
        dialog.set_destroy_with_parent(True)
        dialog.run()
        dialog.destroy()

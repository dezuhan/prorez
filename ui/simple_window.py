import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, Gdk, Pango
import os

from core.gpu import get_encoder_groups, get_file_info, detect_vaapi_devices
from core.converter import ConversionJob, format_duration
from core.queue import ConversionQueue
from core.logger import log_session_summary, log_file_probe
from core.settings_manager import get_setting, set_setting

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm",
    ".m4v", ".ts", ".mts", ".m2ts", ".3gp", ".ogv", ".vob",
    ".mpg", ".mpeg", ".asf", ".rmvb", ".divx",
}


class DropZone(Gtk.EventBox):

    def __init__(self, on_files_received):
        super().__init__()
        self.on_files_received = on_files_received

        self.drag_dest_set(
            Gtk.DestDefaults.ALL,
            [],
            Gdk.DragAction.COPY
        )
        self.drag_dest_add_uri_targets()
        self.connect("drag-data-received", self._on_drag_data_received)
        self.connect("drag-motion", self._on_drag_motion)
        self.connect("drag-leave", self._on_drag_leave)

        self.set_above_child(False)
        self.on_checked_changed = None

        self.frame = Gtk.Frame()
        self.frame.get_style_context().add_class("drop-zone-frame")
        self.frame.set_shadow_type(Gtk.ShadowType.NONE)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_transition_duration(150)

        empty_outer = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=8
        )
        empty_outer.set_valign(Gtk.Align.CENTER)

        icon = Gtk.Label()
        icon.set_markup(
            '<span font_desc="Sans 48">\u2b07</span>'
        )

        text = Gtk.Label()
        text.set_markup(
            '<span size="x-large" weight="medium">Drop video files here</span>\n'
            '<span size="small" foreground="#888">or click to browse</span>'
        )
        text.set_justify(Gtk.Justification.CENTER)

        empty_outer.pack_start(icon, False, False, 0)
        empty_outer.pack_start(text, False, False, 0)

        empty_clickable = Gtk.EventBox()
        empty_clickable.add(empty_outer)
        empty_clickable.connect(
            "button-press-event", self._on_empty_click
        )

        self.drag_overlay = Gtk.Label()
        self.drag_overlay.set_markup(
            '<span size="x-large" weight="bold">\u2b07\nRelease to add files</span>'
        )
        self.drag_overlay.set_justify(Gtk.Justification.CENTER)

        # ListStore columns: checked, #, filename, progress, status, info, job
        self.queue_store = Gtk.ListStore(bool, str, str, int, str, str, object)
        self.queue_view = Gtk.TreeView(model=self.queue_store)
        self.queue_view.get_style_context().add_class("drop-zone-list")
        self.queue_view.set_headers_visible(True)

        # Checkbox column (hidden by default)
        toggle_r = Gtk.CellRendererToggle()
        toggle_r.set_property("activatable", True)
        toggle_r.connect("toggled", self._on_toggle)
        self.col_check = Gtk.TreeViewColumn("", toggle_r, active=0)
        self.col_check.set_min_width(30)
        self.col_check.set_max_width(30)
        self.col_check.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
        self.col_check.set_visible(True)
        self.queue_view.append_column(self.col_check)

        col_num = Gtk.TreeViewColumn("#", Gtk.CellRendererText(), text=1)
        col_num.set_min_width(35)
        col_num.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
        self.queue_view.append_column(col_num)

        name_r = Gtk.CellRendererText()
        name_r.set_property("ellipsize", Pango.EllipsizeMode.MIDDLE)
        col_name = Gtk.TreeViewColumn("File", name_r, text=2)
        col_name.set_expand(True)
        self.queue_view.append_column(col_name)

        info_r = Gtk.CellRendererText()
        info_r.set_property("ellipsize", Pango.EllipsizeMode.END)
        col_info = Gtk.TreeViewColumn("Info", info_r, text=5)
        col_info.set_min_width(140)
        self.queue_view.append_column(col_info)

        prog_r = Gtk.CellRendererProgress()
        col_prog = Gtk.TreeViewColumn("Progress", prog_r, value=3)
        col_prog.set_min_width(100)
        col_prog.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
        self.queue_view.append_column(col_prog)

        status_r = Gtk.CellRendererText()
        col_status = Gtk.TreeViewColumn("Status", status_r, text=4)
        col_status.set_min_width(90)
        col_status.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
        col_status.set_cell_data_func(status_r, self._status_cell_data)
        self.queue_view.append_column(col_status)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.add(self.queue_view)

        self.stack.add_named(empty_clickable, "empty")
        self.stack.add_named(self.drag_overlay, "drag")
        self.stack.add_named(scroll, "list")
        self.stack.set_visible_child_name("empty")

        self.frame.add(self.stack)
        self.add(self.frame)

        self._drag_hover = False
        self._visible_state = "empty"

    def _status_cell_data(self, column, renderer, model, tree_iter, user_data):
        job = model.get_value(tree_iter, 6)
        if job and job.status == "error":
            renderer.set_property("foreground", "#e53935")
        elif job and job.status == "completed":
            renderer.set_property("foreground", "#43a047")
        elif job and job.status == "converting":
            renderer.set_property("foreground", "#1e88e5")
        else:
            renderer.set_property("foreground", None)

    def _on_toggle(self, renderer, path):
        tree_iter = self.queue_store.get_iter(path)
        current = self.queue_store.get_value(tree_iter, 0)
        self.queue_store.set_value(tree_iter, 0, not current)
        if self.on_checked_changed:
            self.on_checked_changed()

    def _on_drag_motion(self, widget, context, x, y, time):
        if not self._drag_hover:
            self._drag_hover = True
            self.frame.get_style_context().add_class("drop-zone-hover")
            self._show_state("drag")
        Gdk.drag_status(context, Gdk.DragAction.COPY, time)
        return True

    def _on_drag_leave(self, widget, context, time):
        self._drag_hover = False
        self.frame.get_style_context().remove_class("drop-zone-hover")
        self._show_state(self._visible_state)

    def _on_drag_data_received(self, widget, context, x, y, data, info, time):
        self._drag_hover = False
        self.frame.get_style_context().remove_class("drop-zone-hover")
        self._show_state(self._visible_state)

        uris = data.get_uris()
        if uris:
            files = []
            for uri in uris:
                path = GLib.filename_from_uri(uri)[0]
                if os.path.isfile(path):
                    _, ext = os.path.splitext(path)
                    if ext.lower() in VIDEO_EXTENSIONS:
                        files.append(path)
            if files:
                self.on_files_received(files)
        Gtk.drag_finish(context, True, False, time)

    def _on_empty_click(self, widget, event):
        if event.type == Gdk.EventType.BUTTON_PRESS and event.button == 1:
            self._open_file_chooser()
            return True
        return False

    def _open_file_chooser(self):
        dialog = Gtk.FileChooserDialog(
            title="Select Video Files",
            action=Gtk.FileChooserAction.OPEN
        )
        dialog.set_select_multiple(True)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Add", Gtk.ResponseType.OK)

        filter_vid = Gtk.FileFilter()
        filter_vid.set_name("Video Files")
        for ext in VIDEO_EXTENSIONS:
            filter_vid.add_pattern(f"*{ext}")
        dialog.add_filter(filter_vid)

        filter_all = Gtk.FileFilter()
        filter_all.set_name("All Files")
        filter_all.add_pattern("*")
        dialog.add_filter(filter_all)

        if dialog.run() == Gtk.ResponseType.OK:
            self.on_files_received(dialog.get_filenames())
        dialog.destroy()

    def _show_state(self, name):
        if self.stack.get_visible_child_name() != name:
            self.stack.set_visible_child_name(name)
        self._visible_state = name

    def get_checked_jobs(self):
        checked = []
        for row in self.queue_store:
            if row[0]:
                job = row[6]
                if job and job.status not in ("converting",):
                    checked.append(job)
        return checked

    def has_files(self):
        return len(self.queue_store) > 0

    def set_empty(self):
        self._visible_state = "empty"
        self._show_state("empty")

    def set_list(self):
        self._visible_state = "list"
        self._show_state("list")

    def refresh_list(self):
        checked_jobs = set()
        for row in self.queue_store:
            if row[0] and row[6]:
                checked_jobs.add(id(row[6]))

        self.queue_store.clear()
        items = self._get_all_jobs()

        idx = 1
        for job in items:
            if job.status == "completed":
                status_text = "Done"
                progress = 100
            elif job.status == "converting":
                status_text = f"Converting {int(job.progress)}%"
                progress = int(job.progress)
            elif job.status == "error":
                status_text = "Failed"
                progress = int(job.progress)
            elif job.status == "cancelled":
                status_text = "Cancelled"
                progress = int(job.progress)
            else:
                status_text = "Queued"
                progress = 0

            dur = format_duration(job.duration) if job.duration else ""
            res = job.resolution or ""
            vc = job.video_codec or ""
            info_parts = [p for p in [res, vc, dur] if p]
            info_text = " | ".join(info_parts) if info_parts else ""

            was_checked = id(job) in checked_jobs

            self.queue_store.append([
                was_checked,
                str(idx),
                job.filename,
                progress,
                status_text,
                info_text,
                job,
            ])
            idx += 1

    def _get_all_jobs(self):
        win = self.get_toplevel()
        if hasattr(win, "app") and hasattr(win.app, "queue"):
            return win.app.queue.get_all_items()
        return []


class OnboardingDialog(Gtk.Dialog):

    def __init__(self, parent):
        super().__init__(
            title="Welcome to Prorez",
            transient_for=parent,
            flags=Gtk.DialogFlags.MODAL | Gtk.DialogFlags.DESTROY_WITH_PARENT,
        )
        self.set_default_size(600, 480)
        self.get_content_area().get_style_context().add_class("help-dialog")

        self.pages = []
        self.current_page = 0

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT)
        self.stack.set_transition_duration(300)

        self._build_pages()

        self.btn_back = Gtk.Button("Back")
        self.btn_back.connect("clicked", self._on_back)
        self.btn_next = Gtk.Button("Next")
        self.btn_next.connect("clicked", self._on_next)
        self.btn_skip = Gtk.Button("Skip")
        self.btn_skip.connect("clicked", self._on_skip)

        self.dots = Gtk.Box(spacing=8)
        self.dots.set_halign(Gtk.Align.CENTER)
        self._update_dots()

        nav = Gtk.Box(spacing=8)
        nav.set_halign(Gtk.Align.CENTER)
        nav.pack_start(self.btn_back, False, False, 0)
        nav.pack_start(self.dots, True, True, 0)
        nav.pack_end(self.btn_skip, False, False, 0)
        nav.pack_end(self.btn_next, False, False, 0)

        box = self.get_content_area()
        box.set_spacing(16)
        box.set_margin_start(24)
        box.set_margin_end(24)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.pack_start(self.stack, True, True, 0)
        box.pack_start(nav, False, False, 0)

        self.add_button("Get Started", Gtk.ResponseType.OK)
        self.btn_get_started = self.get_widget_for_response(Gtk.ResponseType.OK)
        self.btn_get_started.set_no_show_all(True)
        self.btn_get_started.hide()

        self._update_nav()
        self.show_all()
        self.btn_get_started.hide()

    def _build_pages(self):
        assets_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "assets"
        )
        screenshot_path = os.path.join(assets_dir, "screenshot.png")

        pages_data = [
            (
                "Drag &amp; Drop",
                screenshot_path,
                "\n\n"
                "Simply drag and drop your video files onto the window "
                "to add them to the queue.\n\n"
                "You can also click \"+ Add File\" to browse and select "
                "files manually.\n\n"
                "Files are converted one by one in order."
            ),
            (
                "Presets",
                None,
                "\n\n"
                "<b>ProRes 422 HQ</b> — High-quality mastering codec, "
                "best for color grading in DaVinci Resolve.\n\n"
                "<b>ProRes 422 LT</b> — Lighter proxy version, "
                "smaller files for smoother editing.\n\n"
                "<b>DNxHR HQ</b> — Avid's professional codec, "
                "industry standard for post-production.\n\n"
                "<b>DNxHR SQ</b> — Smaller proxy variant, "
                "faster timeline performance.\n\n"
                "<b>Custom Arguments</b> — Enter your own FFmpeg "
                "flags for full control."
            ),
            (
                "Output Folder",
                None,
                "\n\n"
                "By default, converted files are saved in the same "
                "folder as the source file with the prefix "
                "<i>prorez_convert_</i>.\n\n"
                "Click <b>Browse</b> to choose a different output "
                "folder.\n\n"
                "Click <b>Same as source</b> to reset back to the "
                "source file's folder."
            ),
            (
                "Theme",
                None,
                "\n\n"
                "Switch between <b>light</b> and <b>dark</b> mode "
                "using the theme toggle button in the header bar.\n\n"
                "Your preference is saved automatically."
            ),
            (
                "Queue Controls",
                None,
                "\n\n"
                "<b>Checkboxes</b> — Select specific files for "
                "removal.\n\n"
                "<b>Remove Selected</b> — Remove checked files "
                "from the queue.\n\n"
                "<b>Clear All</b> — Clear the entire queue.\n\n"
                "<b>Convert All</b> — Start converting all queued "
                "files.\n\n"
                "<b>Stop</b> — Cancel the current conversion."
            ),
        ]

        for title, image_path, body in pages_data:
            page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)

            title_lbl = Gtk.Label()
            title_lbl.set_markup(
                f'<span size="xx-large" weight="bold">{title}</span>'
            )
            title_lbl.set_halign(Gtk.Align.CENTER)
            page.pack_start(title_lbl, False, False, 0)

            if image_path and os.path.isfile(image_path):
                img = Gtk.Image.new_from_file(image_path)
                img.set_halign(Gtk.Align.CENTER)
                img.set_valign(Gtk.Align.CENTER)
                page.pack_start(img, True, True, 0)

            body_lbl = Gtk.Label()
            body_lbl.set_markup(body.strip())
            body_lbl.set_xalign(0)
            body_lbl.set_line_wrap(True)
            page.pack_start(body_lbl, False, False, 0)

            self.stack.add_named(page, title)
            self.pages.append(page)

    def _update_dots(self):
        for child in self.dots.get_children():
            self.dots.remove(child)
        for i in range(len(self.pages)):
            dot = Gtk.Label()
            if i == self.current_page:
                dot.set_markup('<span size="large" foreground="white">●</span>')
            else:
                dot.set_markup('<span size="large" foreground="#666">○</span>')
            self.dots.pack_start(dot, False, False, 0)
        self.dots.show_all()

    def _update_nav(self):
        self.btn_back.set_sensitive(self.current_page > 0)
        self.btn_next.set_sensitive(
            self.current_page < len(self.pages) - 1
        )
        is_last = self.current_page == len(self.pages) - 1
        self.btn_next.set_visible(not is_last)
        self.btn_skip.set_visible(not is_last)
        if is_last:
            self.btn_get_started.show()
        else:
            self.btn_get_started.hide()

    def _on_back(self, btn):
        if self.current_page > 0:
            self.current_page -= 1
            self.stack.set_visible_child(self.pages[self.current_page])
            self._update_dots()
            self._update_nav()

    def _on_next(self, btn):
        if self.current_page < len(self.pages) - 1:
            self.current_page += 1
            self.stack.set_visible_child(self.pages[self.current_page])
            self._update_dots()
            self._update_nav()

    def _on_skip(self, btn):
        self.response(Gtk.ResponseType.OK)


class SimpleProrezApp(Gtk.Application):

    def __init__(self):
        super().__init__(application_id="com.prorez.simple")
        self.queue = ConversionQueue()
        self.queue.on_job_update = self.on_queue_update
        self.queue.on_queue_finished = self.on_queue_finished
        self.all_jobs = []

    def do_activate(self):
        win = SimpleWindow(self)
        win.connect("delete-event", self.on_window_delete)
        win.show_all()
        active = win.combo_preset.get_active_text()
        if active and active in win.presets:
            is_custom = win.presets[active].get("custom_args", False)
            win.custom_args_row.set_visible(is_custom)

        if get_setting("first_launch"):
            dialog = OnboardingDialog(win)
            response = dialog.run()
            dialog.destroy()
            set_setting("first_launch", False)

    def on_window_delete(self, widget, event):
        if self.queue.running:
            dialog = Gtk.MessageDialog(
                transient_for=widget,
                modal=True,
                message_type=Gtk.MessageType.QUESTION,
                buttons=Gtk.ButtonsType.YES_NO,
                text="Conversion in progress."
            )
            dialog.format_secondary_text(
                "Are you sure you want to quit? Conversion will be stopped."
            )
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
        return False

    def _on_queue_finished(self):
        for window in self.get_windows():
            if hasattr(window, "on_queue_done"):
                window.on_queue_done()
        return False


class SimpleWindow(Gtk.ApplicationWindow):

    def __init__(self, app):
        super().__init__(application=app, title="Prorez")
        self.app = app
        self.set_default_size(900, 600)
        self.set_border_width(12)

        self.hw_encoders, self.sw_encoders = get_encoder_groups()
        self.presets = self._build_presets()

        first_key = next(iter(self.presets))
        first_preset = self.presets[first_key]
        self.settings = dict(first_preset)
        self.settings["vaapi_devices"] = detect_vaapi_devices()

        self._build_ui()
        self._apply_theme(get_setting("theme"))
        self._populate_preset_combo()

    def _build_ui(self):
        self._inject_css()

        self.get_style_context().add_class("main-window")

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)

        header = Gtk.HeaderBar()
        header.set_show_close_button(True)
        header.set_title("Prorez")
        header.set_subtitle("Drag & Drop Converter")

        self.btn_guide = Gtk.Button("!")
        self.btn_guide.set_tooltip_text("Quick Start Guide")
        self.btn_guide.get_style_context().add_class("circular")
        self.btn_guide.connect("clicked", self._on_guide)
        header.pack_end(self.btn_guide)

        self.btn_help = Gtk.Button("?")
        self.btn_help.set_tooltip_text("Help & About")
        self.btn_help.get_style_context().add_class("circular")
        self.btn_help.connect("clicked", self._on_help)
        header.pack_end(self.btn_help)

        self.btn_theme = Gtk.Button()
        self.btn_theme.set_tooltip_text("Toggle theme")
        self._update_theme_btn()
        self.btn_theme.connect("clicked", self._on_theme_toggle)
        header.pack_end(self.btn_theme)

        self.btn_convert = Gtk.Button("Convert All")
        self.btn_convert.set_sensitive(False)
        self.btn_convert.get_style_context().add_class("suggested-action")
        self.btn_convert.connect("clicked", self.on_convert)
        header.pack_end(self.btn_convert)

        self.btn_stop = Gtk.Button("Stop")
        self.btn_stop.set_sensitive(False)
        self.btn_stop.connect("clicked", self.on_stop)
        header.pack_end(self.btn_stop)

        self.set_titlebar(header)

        settings_row = Gtk.Box(spacing=12)
        settings_row.set_margin_bottom(4)

        settings_row.pack_start(Gtk.Label(label="Preset:"), False, False, 0)

        self.combo_preset = Gtk.ComboBoxText()
        self.combo_preset.connect("changed", self.on_preset_changed)
        settings_row.pack_start(self.combo_preset, False, False, 0)

        settings_row.pack_start(Gtk.Label(label="  Output:"), False, False, 0)

        outdir = self.settings.get("output_dir")
        self.lbl_outdir = Gtk.Label(
            label=outdir if outdir else "Same as source"
        )
        self.lbl_outdir.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.lbl_outdir.set_max_width_chars(30)
        self.lbl_outdir.set_xalign(0.0)
        settings_row.pack_start(self.lbl_outdir, True, True, 0)

        btn_outdir = Gtk.Button(label="Browse")
        btn_outdir.connect("clicked", self.on_choose_output_dir)
        settings_row.pack_start(btn_outdir, False, False, 0)

        self.btn_reset_outdir = Gtk.Button(label="Same as source")
        self.btn_reset_outdir.set_tooltip_text("Reset output to same folder as source")
        self.btn_reset_outdir.connect("clicked", self.on_reset_output_dir)
        self.btn_reset_outdir.set_visible(False)
        settings_row.pack_start(self.btn_reset_outdir, False, False, 0)

        btn_add_file = Gtk.Button(label="+ Add File")
        btn_add_file.set_tooltip_text("Add video files via file chooser")
        btn_add_file.connect("clicked", self.on_add_file)
        settings_row.pack_start(btn_add_file, False, False, 0)

        self.chk_auto = Gtk.CheckButton(label="Auto-convert on drop")
        self.chk_auto.set_active(True)
        settings_row.pack_end(self.chk_auto, False, False, 0)

        main_box.pack_start(settings_row, False, False, 0)

        custom_args_row = Gtk.Box(spacing=8)
        custom_args_row.set_margin_bottom(4)

        custom_args_row.pack_start(
            Gtk.Label(label="FFmpeg args:"), False, False, 0
        )

        self.entry_custom_args = Gtk.Entry()
        self.entry_custom_args.set_placeholder_text(
            "e.g. -crf 18 -preset slow -c:a copy"
        )
        self.entry_custom_args.set_hexpand(True)
        custom_args_row.pack_start(self.entry_custom_args, True, True, 0)

        self.custom_args_row = custom_args_row
        main_box.pack_start(custom_args_row, False, False, 0)
        custom_args_row.set_visible(False)

        self.drop_zone = DropZone(self.on_files_dropped)
        self.drop_zone.on_checked_changed = self._on_check_changed
        main_box.pack_start(self.drop_zone, True, True, 0)

        bottom_row = Gtk.Box(spacing=8)
        bottom_row.set_margin_top(2)

        self.lbl_status = Gtk.Label(label="Ready. Drop files to begin.")
        self.lbl_status.set_halign(Gtk.Align.START)
        bottom_row.pack_start(self.lbl_status, True, True, 0)

        self.lbl_count = Gtk.Label(label="0 files")
        self.lbl_count.set_halign(Gtk.Align.END)
        bottom_row.pack_start(self.lbl_count, False, False, 0)

        btn_clear = Gtk.Button(label="Clear All")
        btn_clear.connect("clicked", self.on_clear)
        bottom_row.pack_end(btn_clear, False, False, 0)

        self.btn_remove = Gtk.Button(label="Remove Selected")
        self.btn_remove.set_sensitive(False)
        self.btn_remove.connect("clicked", self.on_remove_selected)
        bottom_row.pack_end(self.btn_remove, False, False, 0)

        main_box.pack_start(bottom_row, False, False, 0)

        self.add(main_box)

    def _build_presets(self):
        presets = {}
        all_encs = {**self.hw_encoders, **self.sw_encoders}

        prores = self._find_encoder("prores", "prores")
        dnx = self._find_encoder("dnx", "dnxhd")

        if prores:
            presets["ProRes 422 HQ (DaVinci Master)"] = {
                "container": "mov",
                "encoder": prores["name"],
                "encoder_info": prores,
                "audio_codec": "pcm_s24le",
                "audio_bitrate": None,
                "bitrate": "auto",
                "crf": None,
                "cq": None,
                "qp": None,
                "preset": None,
                "profile": "3",
                "output_dir": None,
            }
            presets["ProRes 422 LT (DaVinci Proxy)"] = {
                "container": "mov",
                "encoder": prores["name"],
                "encoder_info": prores,
                "audio_codec": "pcm_s24le",
                "audio_bitrate": None,
                "bitrate": "auto",
                "crf": None,
                "cq": None,
                "qp": None,
                "preset": None,
                "profile": "1",
                "output_dir": None,
            }

        if dnx:
            presets["DNxHR HQ (DaVinci Master)"] = {
                "container": "mov",
                "encoder": dnx["name"],
                "encoder_info": dnx,
                "audio_codec": "pcm_s24le",
                "audio_bitrate": None,
                "bitrate": "auto",
                "crf": None,
                "cq": None,
                "qp": None,
                "preset": None,
                "profile": "dnxhr_hq",
                "output_dir": None,
            }
            presets["DNxHR SQ (DaVinci Proxy)"] = {
                "container": "mov",
                "encoder": dnx["name"],
                "encoder_info": dnx,
                "audio_codec": "pcm_s24le",
                "audio_bitrate": None,
                "bitrate": "auto",
                "crf": None,
                "cq": None,
                "qp": None,
                "preset": None,
                "profile": "dnxhr_sq",
                "output_dir": None,
            }

        presets["Custom Arguments"] = {
            "container": "mov",
            "encoder": "libx264",
            "encoder_info": {"name": "libx264", "group": "cpu", "type": "h264"},
            "audio_codec": "pcm_s24le",
            "audio_bitrate": None,
            "bitrate": "auto",
            "crf": None,
            "cq": None,
            "qp": None,
            "preset": None,
            "profile": None,
            "output_dir": None,
            "custom_args": True,
        }

        return presets

    def _find_encoder(self, group, etype):
        all_encs = {**self.hw_encoders, **self.sw_encoders}
        for gname, encoders in all_encs.items():
            if gname == group:
                for enc in encoders:
                    if enc["type"] == etype:
                        result = enc.copy()
                        result["group"] = gname
                        return result
        return None

    def _populate_preset_combo(self):
        self.combo_preset.remove_all()
        for name in self.presets:
            self.combo_preset.append_text(name)
        self.combo_preset.set_active(0)

    def on_preset_changed(self, combo):
        active = combo.get_active_text()
        if active and active in self.presets:
            self.settings = dict(self.presets[active])
            self.settings["vaapi_devices"] = detect_vaapi_devices()
            is_custom = self.settings.get("custom_args", False)
            self.custom_args_row.set_visible(is_custom)

    def on_choose_output_dir(self, btn):
        dialog = Gtk.FileChooserDialog(
            title="Select Output Folder",
            parent=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Choose", Gtk.ResponseType.OK)

        current = self.settings.get("output_dir")
        if current and os.path.isdir(current):
            dialog.set_current_folder(current)

        if dialog.run() == Gtk.ResponseType.OK:
            path = dialog.get_filename()
            self.settings["output_dir"] = path
            self.lbl_outdir.set_text(path)
            self.btn_reset_outdir.set_visible(True)
        dialog.destroy()

    def on_reset_output_dir(self, btn):
        self.settings["output_dir"] = None
        self.lbl_outdir.set_text("Same as source")
        self.btn_reset_outdir.set_visible(False)

    def on_add_file(self, btn):
        dialog = Gtk.FileChooserDialog(
            title="Select Video Files",
            parent=self,
            action=Gtk.FileChooserAction.OPEN,
        )
        dialog.set_select_multiple(True)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Add", Gtk.ResponseType.OK)

        filter_vid = Gtk.FileFilter()
        filter_vid.set_name("Video Files")
        for ext in VIDEO_EXTENSIONS:
            filter_vid.add_pattern(f"*{ext}")
        dialog.add_filter(filter_vid)

        filter_all = Gtk.FileFilter()
        filter_all.set_name("All Files")
        filter_all.add_pattern("*")
        dialog.add_filter(filter_all)

        if dialog.run() == Gtk.ResponseType.OK:
            self.on_files_dropped(dialog.get_filenames())
        dialog.destroy()

    def _on_check_changed(self):
        dz = self.drop_zone
        self._update_remove_button()

    def _update_remove_button(self):
        dz = self.drop_zone
        checked = dz.get_checked_jobs()
        self.btn_remove.set_sensitive(len(checked) > 0)

    def on_files_dropped(self, files):
        dz = self.drop_zone
        for f in files:
            file_info = get_file_info(f)
            job = ConversionJob(
                f,
                self.settings.get("output_dir"),
                dict(self.settings),
            )

            if file_info:
                log_file_probe(f, file_info)
                job.duration = file_info.get("duration", 0)
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
                job.duration = 0

            self.app.all_jobs.append(job)
            self.app.queue.add(job)

        dz.refresh_list()

        if dz.has_files():
            dz.set_list()

        self.lbl_count.set_text(
            f"{len(dz.queue_store)} file{'s' if len(dz.queue_store) != 1 else ''}"
        )
        self.btn_convert.set_sensitive(len(dz.queue_store) > 0)
        self._update_remove_button()

        if self.chk_auto.get_active() and not self.app.queue.running:
            self._sync_settings_to_queue()
            self.app.queue.start()
            self.update_controls()

    def _sync_settings_to_queue(self):
        custom_args = self.entry_custom_args.get_text().strip()
        for job in self.app.queue.get_queue():
            job.output_dir = self.settings.get("output_dir")
            for k in (
                "encoder", "encoder_info", "container", "bitrate",
                "crf", "cq", "qp", "preset", "profile",
                "audio_codec", "audio_bitrate", "src_codec",
            ):
                if k in self.settings:
                    job.settings[k] = self.settings.get(k)
            if custom_args:
                job.settings["extra_args"] = custom_args

    def on_convert(self, btn):
        if self.app.queue.running:
            return
        if self.app.queue.queue_size == 0 and self.app.all_jobs:
            for job in self.app.all_jobs:
                if job.status in ("completed", "error", "cancelled"):
                    self.app.queue.requeue(job)
        self._sync_settings_to_queue()
        self.app.queue.start()
        self.update_controls()

    def on_stop(self, btn):
        self.app.queue.stop()
        self.update_controls()

    def on_clear(self, btn):
        if self.app.queue.running:
            return
        self.app.queue.clear()
        self.app.all_jobs.clear()
        self.drop_zone.queue_store.clear()
        self.drop_zone.set_empty()
        self.lbl_count.set_text("0 files")
        self.lbl_status.set_text("Ready. Drop files to begin.")
        self.btn_convert.set_sensitive(False)
        self.btn_remove.set_sensitive(False)

    def on_remove_selected(self, btn):
        if self.app.queue.running:
            return
        dz = self.drop_zone

        checked = dz.get_checked_jobs()
        if not checked:
            return

        self.app.queue.remove_many(checked)
        for j in checked:
            if j in self.app.all_jobs:
                self.app.all_jobs.remove(j)
        self.refresh_queue()

    def refresh_queue(self):
        dz = self.drop_zone
        dz.refresh_list()

        total = len(dz.queue_store)
        self.lbl_count.set_text(
            f"{total} file{'s' if total != 1 else ''}"
        )

        if total == 0:
            dz.set_empty()
        else:
            dz.set_list()

        self.btn_convert.set_sensitive(total > 0)
        self.btn_stop.set_sensitive(self.app.queue.running)
        self._update_remove_button()

        if self.app.queue.running:
            current = self.app.queue.current_job
            if current:
                speed = f" ({current.speed:.1f}x)" if current.speed > 0 else ""
                self.lbl_status.set_text(
                    f"Converting {current.filename} — {int(current.progress)}%{speed}"
                )
        elif self.app.queue.is_idle and total > 0:
            completed = sum(
                1 for j in self.app.all_jobs if j.status == "completed"
            )
            failed = sum(1 for j in self.app.all_jobs if j.status == "error")
            self.lbl_status.set_text(
                f"Done. {completed} succeeded, {failed} failed."
            )
        elif not self.app.queue.running and self.app.queue.queue_size > 0:
            self.lbl_status.set_text(
                f"{self.app.queue.queue_size} file(s) queued. Click Convert All."
            )
        else:
            self.lbl_status.set_text("Ready. Drop files to begin.")

    def update_controls(self):
        self.refresh_queue()

    def on_queue_done(self):
        log_session_summary(self.app.all_jobs)
        self.update_controls()

    def _inject_css(self):
        screen = Gdk.Screen.get_default()
        provider = Gtk.CssProvider()
        css = b"""
            frame {
                border: none;
                box-shadow: none;
            }
            .drop-zone-frame {
                border: 2px dashed alpha(@borders, 0.35);
                padding: 6px;
            }
            .drop-zone-hover {
                border: 2px dashed alpha(@theme_selected_bg_color, 0.7);
                background: alpha(@theme_selected_bg_color, 0.06);
                padding: 6px;
            }
            .drop-zone-list {
                background: transparent;
                color: @theme_fg_color;
            }
            .drop-zone-list header button {
                padding: 4px 8px;
            }
            .drop-zone-list header button:first-child {
                border-radius: 8px 0 0 0;
            }
            .drop-zone-list header button:last-child {
                border-radius: 0 8px 0 0;
            }
            .drop-zone-list:selected {
                border-radius: 4px;
            }
            treeview button {
                min-height: 20px;
            }
            decoration {
                border-radius: 0;
            }
            .main-window {
                background: @theme_bg_color;
            }
            .circular {
                border-radius: 9999px;
                min-width: 32px;
                min-height: 32px;
                font-weight: bold;
            }
            .help-dialog link {
                color: white;
                text-decoration: none;
            }
            .help-dialog link:hover {
                color: #aaa;
            }
            .help-dialog link:hover {
                color: #aaa;
            }
        """
        provider.load_from_data(css)
        Gtk.StyleContext.add_provider_for_screen(
            screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def _apply_theme(self, theme):
        if theme == "dark":
            Gtk.Settings.get_default().set_property(
                "gtk-application-prefer-dark-theme", True
            )
        else:
            Gtk.Settings.get_default().set_property(
                "gtk-application-prefer-dark-theme", False
            )

    def _update_theme_btn(self):
        theme = get_setting("theme")
        labels = {"light": "\u2600", "dark": "\u263e"}
        self.btn_theme.set_label(labels.get(theme, "\u263e"))

    def _on_theme_toggle(self, btn):
        current = get_setting("theme")
        order = ["light", "dark"]
        next_theme = order[(order.index(current) + 1) % len(order)]
        set_setting("theme", next_theme)
        self._apply_theme(next_theme)
        self._update_theme_btn()

    def _on_guide(self, btn):
        dialog = OnboardingDialog(self)
        dialog.run()
        dialog.destroy()

    def _on_help(self, btn):
        dialog = Gtk.Dialog(
            title="Help & About",
            parent=self,
            flags=Gtk.DialogFlags.MODAL | Gtk.DialogFlags.DESTROY_WITH_PARENT,
        )
        dialog.add_button("Close", Gtk.ResponseType.CLOSE)
        dialog.set_default_size(520, 600)
        dialog.get_content_area().get_style_context().add_class("help-dialog")

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        content.set_margin_start(24)
        content.set_margin_end(24)
        content.set_margin_top(16)
        content.set_margin_bottom(16)

        def add_section(title, lines):
            lbl = Gtk.Label()
            lbl.set_xalign(0)
            lbl.set_markup(f'<span size="large" weight="bold">{title}</span>')
            content.pack_start(lbl, False, False, 8)
            for line in lines:
                lbl = Gtk.Label()
                lbl.set_xalign(0)
                lbl.set_markup(line)
                lbl.set_line_wrap(True)
                content.pack_start(lbl, False, False, 4)

        def add_separator():
            sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            content.pack_start(sep, False, False, 16)

        add_section("Presets", [
            "<b>ProRes 422 HQ (DaVinci Master)</b>\n"
            "High-quality intra-frame codec, ideal for mastering and color grading. "
            "Outputs MOV container. Large file size but zero compression artifacts.",

            "<b>ProRes 422 LT (DaVinci Proxy)</b>\n"
            "Lighter version of ProRes HQ. Smaller files, suitable for proxy editing "
            "on lower-end hardware.",

            "<b>DNxHR HQ (DaVinci Master)</b>\n"
            "Avid's high-quality intra-frame codec. MOV container. "
            "Industry standard for professional post-production.",

            "<b>DNxHR SQ (DaVinci Proxy)</b>\n"
            "Smaller DNxHR variant for proxy workflows. Fast timeline performance "
            "with reduced storage.",

            "<b>Custom Arguments</b>\n"
            "Enter your own FFmpeg arguments. The input field will appear below "
            "the preset dropdown. Output folder still follows the Output setting.",
        ])

        add_separator()

        add_section("Buttons", [
            "<b>Convert All</b> — Start converting all queued files.",
            "<b>Stop</b> — Cancel the current conversion.",
            "<b>Browse</b> — Choose a different output folder.",
            "<b>Same as source</b> — Reset output to source file's folder.",
            "<b>+ Add File</b> — Open file chooser to add video files.",
            "<b>Remove Selected</b> — Remove checked files from the queue.",
            "<b>Clear All</b> — Clear the entire queue.",
            "<b>Theme toggle</b> — Switch between light and dark mode.",
        ])

        add_separator()

        add_section("How to Use", [
            "1. Drag and drop video files onto the window, or click "
            "\"+ Add File\" to browse.",
            "2. Select a preset from the dropdown that matches your workflow.",
            "3. Optionally click \"Browse\" to change the output folder "
            "(defaults to same folder as source).",
            "4. Check the files you want to convert, then click \"Convert All\".",
            "5. The output file will be named "
            "<i>prorez_convert_[original_name].[ext]</i>.",
        ])

        add_separator()

        credit_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        credit_box.set_halign(Gtk.Align.CENTER)

        credit_lbl = Gtk.Label()
        credit_lbl.set_markup(
            '<span size="small" foreground="#aaa">Created by  '
            '<a href="https://instagram.com/dezuhan">Instagram @dezuhan</a>'
            '  |  '
            '<a href="https://linkedin.com/in/dzuhan">LinkedIn in/dzuhan</a>'
            '  |  '
            '<a href="https://github.com/dezuhan">GitHub @dezuhan</a>'
            '</span>'
        )
        credit_box.pack_start(credit_lbl, False, False, 0)

        content.pack_start(credit_box, False, False, 0)

        scroll.add(content)
        box = dialog.get_content_area()
        box.pack_start(scroll, True, True, 0)
        box.show_all()
        dialog.run()
        dialog.destroy()

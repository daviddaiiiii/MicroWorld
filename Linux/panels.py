"""Native stage, capture, gallery, and settings panels for MicroWorld on Linux."""

import json
from pathlib import Path
from urllib.parse import urlsplit

from gi.repository import Gdk, GdkPixbuf, Gtk, Pango

from instrument import CAMERA_SETTINGS, FOCUS, SCAN, SESSION_ID


def column(spacing=10):
    return Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing)


def pack(container, child, expand=False):
    container.pack_start(child, expand, expand, 0)
    return child


def label(text="", title=False):
    result = Gtk.Label(label=text, xalign=0)
    if title:
        result.get_style_context().add_class("panel-heading")
    return result


def caption(text=""):
    result = label(text)
    result.get_style_context().add_class("dim-label")
    result.set_line_wrap(True)
    return result


def button(text, action, primary=False):
    entry = Gtk.Button(label=text)
    entry.connect("clicked", action)
    if primary:
        entry.get_style_context().add_class("suggested-action")
    return entry


def number(value, low, high, step=1, digits=0):
    field = Gtk.SpinButton.new_with_range(low, high, step)
    field.set_digits(digits)
    field.set_value(value)
    field.set_numeric(True)
    field.set_width_chars(7)
    return field


def choices(*labels):
    combo = Gtk.ComboBoxText()
    for choice in labels:
        combo.append(choice, choice)
    combo.set_active(0)
    return combo


def check(text, value=False):
    widget = Gtk.CheckButton(label=text, active=value)
    return widget


def scrolled(content):
    scroll = Gtk.ScrolledWindow()
    scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scroll.add(content)
    return scroll


def section(container, heading):
    frame = Gtk.Frame()
    content = column()
    content.set_border_width(12)
    pack(content, label(heading, True))
    frame.add(content)
    pack(container, frame)
    return content


def field(container, name, widget):
    row = Gtk.Box(spacing=10)
    heading = pack(row, label(name), True)
    heading.set_mnemonic_widget(widget)
    widget.get_accessible().set_name(name)
    row.pack_end(widget, False, False, 0)
    pack(container, row)
    return widget


def axes(container, name, initial, low, high):
    pack(container, caption(name))
    row = Gtk.Box(homogeneous=True, spacing=9)
    values = []
    for axis, value in zip("XYZ", initial):
        cell = pack(row, column(5), True)
        pack(cell, label(axis))
        spin = pack(cell, number(value, low, high))
        spin.get_accessible().set_name(f"{name} {axis}")
        values.append(spin)
    pack(container, row)
    return values


def values(inputs):
    return [input.get_value_as_int() for input in inputs]


def refresh_text(widget, value):
    if widget.get_text() != value:
        widget.set_text(value)


class Panels:
    def __init__(self, app):
        self.app = app
        self.frame = None
        self.canvases = []
        self.gallery_items = []
        self.gallery_token = None
        self.gallery_error = ""
        self.calibration_data = None
        self.camera_loaded = False
        self.sensor_loaded = False
        self.camera_areas = []
        self.control_buttons = []
        self.canvas_widgets = {
            "navigate": self.navigate(),
            "capture": self.capture(),
            "gallery": self.gallery(),
            "settings": self.settings(),
        }

    def connected(self):
        self.frame = None
        self.calibration_data = None
        self.camera_loaded = False
        self.sensor_loaded = False
        self.canvases = []
        self.gallery_items = []
        self.gallery_error = ""
        self.gallery_token = None
        self.initial_target = False
        self.center.set_active(False)
        self.render_gallery()
        self.position.set_text("Reading position…")
        self.mapping_status.set_text("Checking camera–stage mapping…")
        self.status_changed()

    def status_changed(self):
        state = self.app.actual
        attachment = self.app.active or {}
        online = self.app.online
        allows = self.app.can_control
        for action in self.control_buttons:
            action.set_sensitive(allows)
        self.apply_camera.set_sensitive(allows and self.camera_loaded)
        has_scan_extension = bool(attachment.get("openmhs"))
        self.scale_calibrate.set_sensitive(allows and has_scan_extension)
        self.navigate_scale.set_sensitive(allows and has_scan_extension)
        self.caps_changed()
        self.capture_start.set_sensitive(allows)
        self.stop.set_sensitive(self.app.action_route is not None)
        self.center.set_sensitive(bool(allows and self.calibration_data and self.app.api and
                                       self.app.api.capabilities.get("center")))
        self.mapping_export.set_sensitive(bool(self.calibration_data))
        self.settings_identity.set_text(attachment.get("device_id") or "—")
        self.settings_address.set_text(attachment.get("microscope") or "—")
        self.settings_worker.set_text(self.app.worker_label())
        position = state.get("position", {})
        if online and all(axis in position for axis in "xyz"):
            new_value = [position[axis] for axis in "xyz"]
            for widget, value in zip(self.position_fields, new_value):
                refresh_text(widget, f"{value:,}")
            refresh_text(self.position, "")
            if not self.initial_target:
                self.use_current()
                self.initial_target = True
        elif not online:
            refresh_text(self.position, "Position unavailable")
            for widget in self.position_fields:
                refresh_text(widget, "—")
        refresh_text(self.scope, "Live" if self.app.camera_live else
                     "Waiting for camera…" if online else "Camera offline")

    def caps_changed(self):
        caps = self.app.api.capabilities if self.app.api else {}
        modes = ["Single Image"]
        if caps.get("mosaic") or caps.get("stock"):
            modes.append("Scan")
        if caps.get("smart"):
            modes.append("Smart Stack")
        if modes != self.capture_modes:
            previous = self.capture_mode.get_active_id()
            self.capture_mode.remove_all()
            for name in modes:
                self.capture_mode.append(name, name)
            self.capture_mode.set_active_id(previous if previous in modes else modes[0])
            self.capture_modes = modes
        self.skip_empty.set_visible(bool(caps.get("skip_empty")))
        self.mapping_calibrate.set_sensitive(self.app.can_control and bool(caps.get("calibrate")))

    def controlled(self, name, callback, primary=False):
        action = button(name, callback, primary)
        self.control_buttons.append(action)
        return action

    def camera_area(self, allow_click=False):
        layout = column(5)
        area = Gtk.DrawingArea()
        area.set_size_request(300, 220)
        area.get_style_context().add_class("microscope-surface")
        area.get_accessible().set_name("Live camera image")
        area.connect("draw", self.draw_camera)
        if allow_click:
            area.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
            area.connect("button-press-event", self.center_view)
        self.camera_areas.append(area)
        pack(layout, area, True)
        return layout

    def draw_camera(self, area, context):
        width, height = area.get_allocated_width(), area.get_allocated_height()
        context.set_source_rgb(.035, .045, .05)
        context.paint()
        if self.frame is not None:
            w, h = self.frame.get_width(), self.frame.get_height()
            scale = min(width / w, height / h)
            context.save()
            context.translate((width - w * scale) / 2, (height - h * scale) / 2)
            context.scale(scale, scale)
            Gdk.cairo_set_source_pixbuf(context, self.frame, 0, 0)
            context.paint()
            context.restore()
        return False

    def update_frame(self, raw):
        try:
            decoder = GdkPixbuf.PixbufLoader.new_with_type("jpeg")
            decoder.write(raw)
            decoder.close()
            self.frame = decoder.get_pixbuf()
        except Exception:
            return
        for area in self.camera_areas:
            if area.get_mapped():
                area.queue_draw()
        refresh_text(self.scope, "Live")

    def center_view(self, area, event):
        if not self.center.get_active() or not self.center.get_sensitive() or self.frame is None:
            return False
        width, height = area.get_allocated_width(), area.get_allocated_height()
        w, h = self.frame.get_width(), self.frame.get_height()
        scale = min(width / w, height / h)
        dx, dy = (event.x - width / 2) / scale, (event.y - height / 2) / scale
        if abs(dx) <= w / 2 and abs(dy) <= h / 2:
            self.app.action("Centering camera…", lambda api: api.run(api.capabilities["center"], {"x": dy, "y": dx}))
        return True

    def split(self, camera, controls, width=338):
        area = Gtk.Box(spacing=14, margin=14)
        pack(area, camera, True)
        scroll = scrolled(controls)
        scroll.set_size_request(width, -1)
        pack(area, scroll)
        return area

    def navigate(self):
        controls = column()
        coords = section(controls, "Current position")
        self.position_fields = [field(coords, axis, label("—")) for axis in "XYZ"]
        for value in self.position_fields:
            value.get_style_context().add_class("numeric")
        self.position = pack(coords, caption("Position unavailable"))
        group = section(controls, "Jog")
        pack(group, caption("Arrow keys move XY. Page Up and Page Down move Z."))
        self.xy_step = field(group, "XY step", number(100, 1, 20_000))
        self.z_step = field(group, "Z step", number(25, 1, 5000))
        jog = Gtk.Grid(column_spacing=6, row_spacing=6, column_homogeneous=True)
        for text, col, row, delta in (
            ("↑", 1, 0, (0, 1, 0)), ("←", 0, 1, (-1, 0, 0)),
            ("↓", 1, 1, (0, -1, 0)), ("→", 2, 1, (1, 0, 0)),
            ("Z +", 0, 2, (0, 0, 1)), ("Z −", 2, 2, (0, 0, -1)),
        ):
            go = self.controlled(text, lambda _button, delta=delta: self.jog(delta))
            go.set_tooltip_text(f"Jog {text}")
            jog.attach(go, col, row, 1, 1)
        pack(group, jog)
        self.center = pack(group, check("Click to Center"))
        target = section(controls, "Go to")
        self.destination = [field(target, axis, number(0, -2_000_000, 2_000_000)) for axis in "XYZ"]
        row = pack(target, Gtk.Box(spacing=7))
        pack(row, button("Use Current", self.use_current))
        pack(row, self.controlled("Move", self.go_to, True))
        focus = section(controls, "Focus and calibration")
        span = field(focus, "AF span", number(2000, 1, 10_000))
        pack(focus, self.controlled("Fast Autofocus", lambda *_: self.app.focus(span.get_value_as_int())))
        self.navigate_scale = pack(focus, self.controlled("Estimate Scale", self.estimate_scale))
        preview = self.camera_area(allow_click=True)
        self.scope = pack(preview, caption("Waiting for camera…"))
        return self.split(preview, controls, 292)

    def jog(self, direction):
        xy, z = self.xy_step.get_value_as_int(), self.z_step.get_value_as_int()
        self.app.action("Moving stage…", lambda api: api.move(tuple(n * step for n, step in zip(direction, (xy, xy, z)))))

    def go_to(self, *_):
        point = values(self.destination)
        self.app.action("Moving stage…", lambda api: api.move(point, absolute=True))

    def use_current(self, *_):
        position = self.app.actual.get("position", {})
        for name, field in zip("xyz", self.destination):
            if name in position:
                field.set_value(position[name])

    def capture(self):
        controls = column()
        acquisition = section(controls, "Acquisition")
        self.capture_mode = field(acquisition, "Mode", choices("Single Image"))
        self.capture_modes = ["Single Image"]
        self.capture_filename = field(acquisition, "File name", Gtk.Entry())
        self.temporary_capture = pack(acquisition, check("Temporary"))
        self.full_capture = pack(acquisition, check("Full resolution", True))
        self.bayer = pack(acquisition, check("Store Bayer data"))
        self.resize = pack(acquisition, check("Resize capture"))
        self.width = field(acquisition, "Width (px)", number(640, 1, 16000))
        self.height = field(acquisition, "Height (px)", number(480, 1, 16000))
        metadata = section(controls, "Metadata")
        self.tags = field(metadata, "Tags", Gtk.Entry(placeholder_text="Comma separated"))
        self.notes = field(metadata, "Notes", Gtk.Entry())
        self.annotations = pack(metadata, column(5))
        pack(metadata, button("Add Annotation", self.add_annotation))
        self.annotation_fields = []
        self.add_annotation(key="Client", value="MicroWorld")
        scan = self.scan_fields = section(controls, "Scan and Z stack")
        self.scan_group = scan.get_parent()
        self.grid = axes(scan, "Images per axis", (3, 3, 1), 1, 32)
        self.stride = axes(scan, "Stride in motor steps", (800, 640, 50), -50_000, 50_000)
        self.automatic_stride = pack(scan, check("Calculate XY stride automatically", True))
        self.scan_focus = field(scan, "Autofocus", choices("Fast", "Off", "Coarse", "Medium", "Fine"))
        self.scan_pattern = field(scan, "Traversal", choices("Raster", "Snake", "Spiral"))
        self.scan_naming = field(scan, "Naming", choices("Coordinates", "Number"))
        self.skip_empty = pack(scan, check("Skip autofocus in empty fields"))
        self.smart_focus = field(scan, "Autofocus range", number(3000, 1000, 10000))
        self.smart_align = field(scan, "Alignment range", number(900, 100, 10000))
        actions = section(controls, "Capture")
        self.progress = pack(actions, Gtk.ProgressBar(show_text=True))
        self.progress.set_no_show_all(True)
        self.stop = pack(actions, button("Stop Scan", lambda *_: self.app.stop_scan()))
        self.stop.set_no_show_all(True)
        self.capture_start = pack(actions, self.controlled("Capture to Microscope", self.start_capture, True))
        pack(actions, self.controlled("Save Live Frame to Computer", lambda *_: self.app.snapshot()))
        for widget, signal in ((self.capture_mode, "changed"), (self.bayer, "toggled"), (self.resize, "toggled")):
            widget.connect(signal, self.capture_visibility)
        for widget in (self.scan_group, self.width.get_parent(), self.height.get_parent(),
                       self.full_capture, self.resize, self.automatic_stride, self.smart_focus.get_parent(),
                       self.smart_align.get_parent(), self.skip_empty):
            widget.set_no_show_all(True)
        self.capture_visibility()
        return self.split(self.camera_area(), controls, 374)

    def capture_visibility(self, *_):
        mode = self.capture_mode.get_active_id() or "Single Image"
        image = mode == "Single Image"
        bayer = self.bayer.get_active()
        self.capture_start.set_label("Capture to Microscope" if image else f"Start {mode}")
        self.scan_group.set_visible(not image)
        self.full_capture.set_visible(image and not bayer)
        self.resize.set_visible(not bayer)
        for item in (self.width, self.height):
            item.get_parent().set_visible(not bayer and self.resize.get_active())
        self.automatic_stride.set_visible(bool(mode == "Scan" and self.app.api and self.app.api.capabilities.get("mosaic")))
        self.smart_focus.get_parent().set_visible(mode == "Smart Stack")
        self.smart_align.get_parent().set_visible(mode == "Smart Stack")
        self.scan_fields.show_all()

    def add_annotation(self, *_unused, key="", value=""):
        row = Gtk.Box(spacing=5)
        left = pack(row, Gtk.Entry(text=key, placeholder_text="Key", width_chars=9), True)
        right = pack(row, Gtk.Entry(text=value, placeholder_text="Value", width_chars=9), True)
        record = (left, right)

        def remove(*_):
            self.annotation_fields.remove(record)
            row.destroy()

        pack(row, button("Remove", remove))
        self.annotation_fields.append(record)
        pack(self.annotations, row)
        row.show_all()

    def start_capture(self, *_):
        api = self.app.api
        mode = self.capture_mode.get_active_id() or "Single Image"
        bayer = self.bayer.get_active()
        metadata = {}
        for key_field, value in self.annotation_fields:
            key = key_field.get_text().strip()
            if key in metadata:
                self.app.notice("Annotation keys must be unique.", True)
                return
            if key:
                metadata[key] = value.get_text()
        if self.notes.get_text():
            metadata["Notes"] = self.notes.get_text()
        body = {"temporary": self.temporary_capture.get_active(), "bayer": bayer,
                "use_video_port": not self.full_capture.get_active() and not bayer, "annotations": metadata,
                "tags": [tag.strip() for tag in self.tags.get_text().split(",") if tag.strip()]}
        if self.capture_filename.get_text():
            body["filename"] = self.capture_filename.get_text()
        if self.resize.get_active() and not bayer:
            body["resize"] = dict(zip(("width", "height"), values((self.width, self.height))))
        if mode == "Single Image":
            self.app.action("Capturing…", lambda current: current.run("/api/v2/actions/camera/capture", body))
            return
        if mode == "Scan" and api and api.capabilities.get("mosaic") and not self.app.worker_available:
            self.app.notice("Connect a PC Worker before starting a Canvas scan.", True)
            return
        if not api or not self.app.confirm(f"Start {mode}?", "The stage will move through the selected X, Y and Z grid."):
            return
        scan = {"grid": values(self.grid), "stride_size": values(self.stride),
                "focus": self.scan_focus.get_active_id(), "style": self.scan_pattern.get_active_id(),
                "naming": self.scan_naming.get_active_id(), "auto_stride": self.automatic_stride.get_active(),
                "skip_empty": self.skip_empty.get_active(), "align_dist": self.smart_align.get_value_as_int(),
                "focus_span": self.smart_focus.get_value_as_int()}
        self.app.action("Acquiring scan…", lambda current: current.scan(mode, body, scan, self.app.report_scan))

    def capture_progress(self, progress):
        self.progress.set_visible(self.app.action_route is not None)
        self.stop.set_visible(self.app.action_route is not None)
        if isinstance(progress, (int, float)):
            self.progress.set_fraction(max(0, min(100, progress)) / 100)
        else:
            self.progress.pulse()

    def gallery(self):
        pane = column()
        pane.set_border_width(14)
        toolbar = Gtk.Box(spacing=9)
        self.search = pack(toolbar, Gtk.SearchEntry(placeholder_text="Search gallery"), True)
        self.search.connect("search-changed", lambda *_: self.render_gallery())
        pack(toolbar, button("Refresh", lambda *_: self.reload_gallery(force=True)))
        pack(pane, toolbar)
        self.gallery_rows = column(10)
        pack(pane, scrolled(self.gallery_rows), True)
        self.render_gallery()
        return pane

    def reload_gallery(self, force=False):
        if not self.app.api or (self.gallery_token == self.app.api and not force):
            return
        self.gallery_token = self.app.api
        self.gallery_error = "Loading gallery…"
        self.render_gallery()

        def display(result):
            self.canvases, local, remote, self.gallery_error = result
            self.gallery_items = [
                ("Canvases", [(item.get("title") or item["session_id"],
                               f'{item.get("cells", 0)} tiles  •  {"Slide" if item.get("refined") else "Tiles"}',
                               "view-grid-symbolic",
                               lambda sid=item["session_id"]: self.app.open_canvas(sid)) for item in self.canvases
                              if SESSION_ID.fullmatch(item.get("session_id") or "")]),
                ("This computer", [(file.name, "", "image-x-generic-symbolic",
                                    lambda file=file: self.preview_file(file)) for file in local]),
                ("Microscope captures", [(item.get("name") or item.get("filename") or item.get("id", ""),
                                          str(item.get("time") or "")[:19], "image-x-generic-symbolic",
                                          lambda item=item: self.preview_remote(item)) for item in remote
                                         if isinstance(item, dict)]),
            ]
            self.render_gallery()

        self.app.read_async(lambda api: api.gallery(self.app.worker_available), display)

    def render_gallery(self):
        for row in self.gallery_rows.get_children():
            row.destroy()
        query = self.search.get_text().casefold()
        for category, records in self.gallery_items:
            pack(self.gallery_rows, label(category, True))
            matches = [record for record in records if query in record[0].casefold()]
            if not matches:
                pack(self.gallery_rows, caption("No matching items." if query else "No items yet."))
            for name, detail, icon, activate in matches:
                item = Gtk.Button()
                item.get_style_context().add_class("gallery-item")
                line = Gtk.Box(spacing=9)
                pack(line, Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON))
                title = pack(line, label(name), True)
                title.set_ellipsize(Pango.EllipsizeMode.END)
                pack(line, caption(detail))
                item.add(line)
                item.connect("clicked", lambda _button, activate=activate: activate())
                pack(self.gallery_rows, item)
        if self.gallery_error:
            pack(self.gallery_rows, caption(self.gallery_error))
        self.gallery_rows.show_all()

    def preview_file(self, path):
        self.app.read_async(lambda _api: path.read_bytes(), lambda data: self.preview(path.name, data))

    def preview_remote(self, item):
        uri = item.get("links", {}).get("download", {}).get("href", "")
        path = urlsplit(uri).path
        if path.startswith("/api/v2/"):
            self.app.read_async(lambda api: api.request(path, timeout=12),
                                lambda data: self.preview(item.get("name") or item.get("filename", "Capture"), data))

    def preview(self, name, data):
        try:
            decoder = GdkPixbuf.PixbufLoader()
            def resize(loader, width, height):
                scale = min(1, 720 / width, 500 / height)
                loader.set_size(max(1, round(width * scale)), max(1, round(height * scale)))
            decoder.connect("size-prepared", resize)
            decoder.write(data)
            decoder.close()
        except Exception:
            self.app.notice("Could not display the capture.", True)
            return
        dialog = Gtk.Dialog(title=name, transient_for=self.app.window, modal=True)
        dialog.add_button("Done", Gtk.ResponseType.CLOSE)
        dialog.get_content_area().add(Gtk.Image.new_from_pixbuf(decoder.get_pixbuf()))
        dialog.show_all()
        dialog.run()
        dialog.destroy()

    def settings(self):
        pane = column(14)
        pane.set_border_width(15)
        link = section(pane, "Connection and Storage")
        self.settings_address = field(link, "Microscope", caption("—"))
        self.settings_identity = field(link, "Device ID", caption("—"))
        self.settings_worker = field(link, "PC Worker", caption("—"))
        for value in (self.settings_address, self.settings_identity, self.settings_worker):
            value.set_selectable(True)
        pack(link, button("Open Workspace", self.app.open_workspace))
        mapping = section(pane, "Camera–Stage Mapping")
        self.mapping_status = pack(mapping, caption("Checking device support…"))
        self.mapping_calibrate = pack(mapping, self.controlled("Auto-Calibrate Using Camera", self.calibrate))
        self.mapping_export = pack(mapping, button("Export Calibration JSON…", self.export_mapping))
        camera = section(pane, "Camera")
        self.camera_fields = {}
        for key, title, initial, low, high, step, digits in (
            ("jpeg_quality", "JPEG quality", 90, 1, 100, 1, 0),
            ("mjpeg_quality", "MJPEG quality", 75, 1, 100, 1, 0),
            ("width", "Stream width", 640, 1, 8192, 1, 0),
            ("height", "Stream height", 480, 1, 8192, 1, 0),
            ("shutter_speed", "Shutter (µs)", 0, 0, 10_000_000, 1, 0),
            ("analog_gain", "Analog gain", 1, 0, 32, .01, 2),
            ("digital_gain", "Digital gain", 1, 0, 32, .01, 2),
            ("framerate", "Frames per second", 30, 1, 120, 1, 2),
            ("red", "Red gain", 1, 0, 16, .01, 2),
            ("blue", "Blue gain", 1, 0, 16, .01, 2),
        ):
            self.camera_fields[key] = field(camera, title, number(initial, low, high, step, digits))
            if key not in ("jpeg_quality", "mjpeg_quality", "width", "height"):
                self.camera_fields[key].get_parent().set_no_show_all(True)
        camera_actions = pack(camera, Gtk.Box(spacing=8))
        pack(camera_actions, button("Reload Settings", lambda *_: self.load_settings()))
        self.apply_camera = pack(camera_actions, self.controlled("Apply Camera Settings", self.save_settings, True))
        stage = section(pane, "Stage")
        pack(stage, caption("Position units: motor steps"))
        self.scale_calibrate = pack(stage, self.controlled("Estimate Camera–Stage Scale", self.estimate_scale))
        pack(stage, self.controlled("Zero Stage…", self.zero_stage))
        return scrolled(pane)

    def load_settings(self):
        def show(camera):
            self.camera_loaded = True
            sensor = camera.get("picamera")
            self.sensor_loaded = isinstance(sensor, dict)
            width, height = camera.get("stream_resolution", (640, 480))
            relevant = {"width": width, "height": height, **{k: v for k, v in camera.items() if k in self.camera_fields}}
            if self.sensor_loaded:
                relevant.update(sensor)
                relevant["red"], relevant["blue"] = sensor.get("awb_gains", (1, 1))
            for name, widget in self.camera_fields.items():
                widget.get_parent().set_visible(self.sensor_loaded or name in ("jpeg_quality", "mjpeg_quality", "width", "height"))
                if isinstance(relevant.get(name), (float, int)):
                    widget.set_value(relevant[name])
            self.status_changed()
        self.app.read_async(lambda api: api.request(CAMERA_SETTINGS), show)

    def save_settings(self, *_):
        params = self.camera_fields
        body = {name: params[name].get_value_as_int() for name in ("jpeg_quality", "mjpeg_quality")}
        body["stream_resolution"] = values((params["width"], params["height"]))
        if self.sensor_loaded:
            body["picamera"] = {key: params[key].get_value() for key in
                               ("shutter_speed", "analog_gain", "digital_gain", "framerate")}
            body["picamera"]["awb_gains"] = [params["red"].get_value(), params["blue"].get_value()]
        self.app.action("Applying camera settings…", lambda api: api.write_settings(body),
                        done=lambda _: self.load_settings())

    def load_calibration(self):
        def show(data):
            self.calibration_data = data or None
            self.mapping_status.set_text("Calibration available" if data else "No camera–stage calibration loaded.")
            self.status_changed()
        self.app.read_async(lambda api: api.calibration(), show)

    def calibrate(self, *_):
        if self.app.confirm("Recalibrate the camera and stage?", "The stage will move to measure the camera–stage mapping."):
            self.app.action("Calibrating camera and stage…", lambda api: api.run(api.capabilities["calibrate"], {}, timeout=1800),
                            done=lambda _: self.load_calibration())

    def export_mapping(self, *_):
        if not self.calibration_data:
            return
        dialog = Gtk.FileChooserDialog(title="Export calibration", transient_for=self.app.window,
                                      action=Gtk.FileChooserAction.SAVE)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Save", Gtk.ResponseType.OK)
        dialog.set_current_name("csm_calibration.json")
        dialog.set_do_overwrite_confirmation(True)
        if dialog.run() == Gtk.ResponseType.OK:
            try:
                Path(dialog.get_filename()).write_text(json.dumps(self.calibration_data, indent=2) + "\n")
            except OSError as error:
                self.app.notice(str(error), True)
        dialog.destroy()

    def estimate_scale(self, *_):
        if self.app.confirm("Estimate camera–stage scale?", "The stage will make several short movements."):
            def calibrate(api):
                api.idle()
                api.request("/desktop/session")
                return api.request(SCAN + "/calibrate_scale", {"probe_steps": 600}, timeout=65)
            self.app.action("Estimating scale…", calibrate,
                            done=lambda result: self.app.notice(f'Scale: {result.get("px_per_step", "estimated")} px/step'))

    def zero_stage(self, *_):
        if self.app.confirm("Zero the stage coordinates?", "Saved Canvases refer to the current stage origin."):
            self.app.action("Zeroing stage…", lambda api: api.run("/api/v2/actions/stage/zero", {}))

"""MicroWorld's Linux workspace and connection lifecycle."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
from threading import Thread
import time
from urllib.parse import urlencode, urlsplit

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("WebKit2", "4.1")
gi.require_foreign("cairo")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango, WebKit2

RESOURCES = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get("MICROWORLD_HOME", Path.home() / "MicroWorld")).expanduser().resolve()
sys.path.insert(0, str(RESOURCES / "Runtime"))

from desktop import LOCAL, write_atomic
from instrument import Api, CAMERA, FOCUS, HARDWARE, PROMPTS, SESSION_ID, STAGE
from network import discover, identify, read_json
from panels import Panels, button, caption, choices, column, label, pack, scrolled

KNOWN = ROOT / "State" / "known-devices.json"
LAST = ROOT / "State" / "linux-device.json"
WORKSPACE_TABS = (
    ("Observe", "openmhs", "view-reveal-symbolic"),
    ("Gallery", "gallery", "folder-pictures-symbolic"),
    ("Navigate", "navigate", "transform-move-symbolic"),
    ("Capture", "capture", "camera-photo-symbolic"),
    ("Settings", "settings", "emblem-system-symbolic"),
)


def read_saved(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save(path, data):
    write_atomic(path, json.dumps(data, indent=2) + "\n")


def async_task(operation, finished):
    def run():
        try:
            value = operation()
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            GLib.idle_add(finished, None, str(error))
        else:
            GLib.idle_add(finished, value, None)
    Thread(target=run, daemon=True).start()


class MicroWorld(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="org.microworld.app",
                         flags=Gio.ApplicationFlags.HANDLES_OPEN)
        self.window = None
        self.runtime = None
        self.active = None
        self.api = None
        self.working = False
        self.action_name = ""
        self.action_route = None
        self.actual = {}
        self.online = False
        self.health_failures = 0
        self.last_probe = 0.0
        self.probing = False
        self.camera_pending = False
        self.frame_time = 0.0
        self.last_camera_probe = 0.0
        self.bridge_ready = False
        self.canvas_visible = False
        self.worker_available = False
        self.devices = read_saved(KNOWN, [])
        self.online_ids = set()
        self.last = read_saved(LAST, {})
        self.pending_slide = None
        self.selected_tab = "openmhs"

    def do_activate(self):
        if self.window:
            self.window.present()
            return
        self.window = Gtk.ApplicationWindow(application=self, title="MicroWorld")
        self.window.set_default_size(1360, 884)
        self.window.set_size_request(930, 570)
        self.window.set_icon_name("org.microworld.app")
        self.window.connect("delete-event", self.close)
        self.window.connect("key-press-event", self.keyboard)
        self.theme()
        header = Gtk.HeaderBar(title="MicroWorld", show_close_button=True)
        self.window.set_titlebar(header)
        self.workspace = button("Workspace", self.open_workspace)
        self.history = button("History", self.open_history)
        header.pack_end(self.history)
        header.pack_end(self.workspace)
        self.workspace.set_sensitive(False)
        self.history.set_sensitive(False)

        split = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)
        self.window.add(split)
        split.pack1(self.sidebar(), False, False)
        main = column(0)
        split.pack2(main, True, False)
        split.set_position(232)
        self.header_bar = Gtk.Box(spacing=12, margin_start=18, margin_end=18, margin_top=10, margin_bottom=10)
        self.page_heading = pack(self.header_bar, label("Observe", True), True)
        self.page_heading.get_style_context().add_class("workspace-heading")
        self.address_badge = pack(self.header_bar, label(""))
        self.address_badge.set_valign(Gtk.Align.CENTER)
        self.address_badge.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.address_badge.set_max_width_chars(39)
        self.address_badge.get_style_context().add_class("address-badge")
        pack(main, self.header_bar)
        pack(main, Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        self.stack = Gtk.Stack()
        pack(main, self.stack, True)
        self.alert = Gtk.Revealer()
        self.alert.set_transition_type(Gtk.RevealerTransitionType.NONE)
        notice = Gtk.Box(spacing=8, margin_start=12, margin_end=12, margin_top=6, margin_bottom=6)
        self.message = pack(notice, caption(""), True)
        self.message.set_selectable(True)
        pack(notice, button("Dismiss", lambda *_: self.notice("")))
        self.alert.add(notice)
        pack(main, self.alert)

        self.panels = Panels(self)
        self.create_observe()
        for name, widget in self.panels.canvas_widgets.items():
            self.stack.add_named(widget, name)
        welcome = column(10)
        welcome.set_halign(Gtk.Align.CENTER)
        welcome.set_valign(Gtk.Align.CENTER)
        pack(welcome, label("Connect to a microscope", True))
        pack(welcome, caption("Select a microscope in the sidebar or add its address."))
        self.stack.add_named(welcome, "welcome")
        self.fill_devices()
        self.window.show_all()
        self.show_page()
        GLib.timeout_add(330, self.tick)
        if self.last.get("id") and self.last.get("url"):
            self.connect_device(self.last["url"], self.last["id"], self.last.get("mode", "auto"))
        else:
            self.refresh()

    def theme(self):
        provider = Gtk.CssProvider()
        provider.load_from_path(str(RESOURCES / "Linux" / "workspace.css"))
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        source = Gio.SettingsSchemaSource.get_default()
        if source and source.lookup("org.gnome.desktop.interface", True):
            settings = Gio.Settings.new("org.gnome.desktop.interface")
            if "color-scheme" in settings.props.settings_schema.list_keys():
                def update(*_):
                    Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme",
                                                            settings.get_string("color-scheme") == "prefer-dark")
                settings.connect("changed::color-scheme", update)
                self.desktop_theme = settings
                update()

    def sidebar(self):
        shell = column(10)
        shell.set_size_request(218, -1)
        shell.get_style_context().add_class("mw-sidebar")
        shell.set_border_width(12)
        pack(shell, caption("Workspace"))
        self.nav = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.nav.get_style_context().add_class("mw-navigation")
        self.workspace_tabs = {}
        for text, route, icon in WORKSPACE_TABS:
            row = Gtk.ListBoxRow()
            row.route = route
            line = Gtk.Box(spacing=10, margin_start=10, margin_end=8, margin_top=8, margin_bottom=8)
            pack(line, Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON))
            pack(line, label(text))
            row.add(line)
            self.nav.add(row)
            self.workspace_tabs[route] = row
        self.nav.select_row(self.workspace_tabs["openmhs"])
        self.nav.connect("row-selected", self.select_tab)
        pack(shell, self.nav)
        pack(shell, Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        tools = Gtk.Box(spacing=6)
        pack(tools, caption("Microscopes"), True)
        self.find = pack(tools, self.icon_button("view-refresh-symbolic", "Refresh devices", self.refresh))
        self.add = pack(tools, self.icon_button("list-add-symbolic", "Add by address", self.add_address))
        pack(shell, tools)
        self.device_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.device_list.get_style_context().add_class("mw-navigation")
        self.device_list.connect("row-activated", lambda *_: self.connect_selected())
        pack(shell, scrolled(self.device_list), True)
        self.mode = choices("Automatic", "Run on this computer", "Use existing worker")
        modes = {"auto": "Automatic", "worker": "Run on this computer", "viewer": "Use existing worker"}
        self.mode.set_active_id(modes.get(self.last.get("mode"), "Automatic"))
        self.mode.set_tooltip_text("PC Worker")
        pack(shell, self.mode)
        self.connect_button = pack(shell, button("Connect", self.connect_selected, True))
        self.disconnect_button = pack(shell, button("Disconnect", self.disconnect))
        self.disconnect_button.set_sensitive(False)
        pack(shell, Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        self.connection_badge = pack(shell, label("Microscope offline"))
        self.worker_badge = pack(shell, caption("PC Worker offline"))
        self.hardware_badge = pack(shell, caption(""))
        return shell

    def icon_button(self, icon, text, action):
        widget = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
        widget.set_tooltip_text(text)
        widget.get_accessible().set_name(text)
        widget.get_style_context().add_class("flat")
        widget.connect("clicked", action)
        return widget

    def create_observe(self):
        observe = column(0)
        toolbar = Gtk.Box(spacing=8, margin_start=18, margin_end=18, margin_top=8, margin_bottom=8)
        self.observe_bar = pack(observe, toolbar)
        self.camera_badge = pack(toolbar, label("Waiting for camera…"), True)
        self.camera_badge.set_ellipsize(Pango.EllipsizeMode.END)
        pack(toolbar, button("Stage", lambda *_: self.select("navigate")))
        self.roi = pack(toolbar, button("Mark ROI", lambda *_: self.viewer("roi")))
        self.af = pack(toolbar, button("Fast AF", lambda *_: self.focus()))
        self.snap = pack(toolbar, button("Snapshot", lambda *_: self.snapshot()))
        self.recent = pack(toolbar, button("Recent", self.open_recent))
        self.scan = pack(toolbar, button("New Scan", lambda *_: self.viewer("newScan"), True))
        for control in toolbar.get_children():
            control.set_valign(Gtk.Align.CENTER)
        manager = WebKit2.UserContentManager()
        manager.register_script_message_handler("microworldCanvas")
        manager.connect("script-message-received::microworldCanvas", self.observe_state)
        self.browser = WebKit2.WebView.new_with_user_content_manager(manager)
        self.browser.connect("decide-policy", self.navigate_uri)
        self.browser.connect("load-changed", self.viewer_loaded)
        pack(observe, self.browser, True)
        self.stack.add_named(observe, "openmhs")

    def do_open(self, files, _count, _hint):
        self.activate()
        for file in files:
            self.open_slide(file.get_uri())

    def select(self, tab):
        self.nav.select_row(self.workspace_tabs[tab])

    def select_tab(self, _list, row):
        if not row:
            return
        self.selected_tab = row.route
        self.show_page()
        if self.active and row.route == "gallery":
            self.panels.reload_gallery()
        if self.active and row.route == "settings":
            self.panels.load_settings()

    def show_page(self):
        self.stack.set_visible_child_name(self.selected_tab if self.active else "welcome")
        name = next(text for text, tab, _ in WORKSPACE_TABS if tab == self.selected_tab)
        self.page_heading.set_text(name if self.active else "Microscopes")
        address = self.active["microscope"].removeprefix("http://") if self.active else ""
        self.address_badge.set_text(address)
        self.address_badge.set_tooltip_text(address or None)
        self.address_badge.set_visible(bool(address))
        self.viewer("visible", self.selected_tab == "openmhs")
        self.update_controls()

    def viewer_loaded(self, _browser, event):
        if event == WebKit2.LoadEvent.STARTED:
            self.bridge_ready = False
        if event == WebKit2.LoadEvent.FINISHED:
            self.viewer("visible", self.selected_tab == "openmhs")

    def observe_state(self, _manager, result):
        self.bridge_ready = True
        self.canvas_visible = result.get_js_value().to_boolean()
        self.observe_bar.set_visible(not self.canvas_visible)
        self.viewer("visible", self.selected_tab == "openmhs")
        self.update_controls()

    def viewer(self, action, *args):
        if (self.browser.get_uri() or "").startswith(LOCAL + "/"):
            self.browser.evaluate_javascript(
                f"window.microworldWorkspace?.{action}({', '.join(json.dumps(arg) for arg in args)});", -1
            )

    def fill_devices(self):
        current = self.device_list.get_selected_row()
        previous = getattr(current, "device", {}).get("id") or self.last.get("id")
        for row in self.device_list.get_children():
            row.destroy()
        for item in self.devices:
            if not item.get("addresses"):
                continue
            row = Gtk.ListBoxRow()
            row.device = item
            lines = column(4)
            lines.set_border_width(8)
            pack(lines, label(f'OpenFlexure {item["id"][-5:]}'))
            if self.active and item["id"] == self.active["device_id"]:
                detail = ("Connected" if self.online else "Offline" if self.health_failures >= 3
                          else "Reconnecting")
            elif item["id"] in self.online_ids:
                detail = "Available"
            else:
                detail = item["addresses"][0]["url"].removeprefix("http://")
            pack(lines, caption(detail))
            row.add(lines)
            self.device_list.add(row)
            if item["id"] == previous:
                self.device_list.select_row(row)
        if self.device_list.get_selected_row() is None:
            self.device_list.select_row(self.device_list.get_row_at_index(0))
        self.device_list.show_all()

    def operate(self, message, action, finished):
        if self.working or self.action_name:
            return
        self.working = True
        self.notice(message)
        self.update_controls()

        def finish(result, error):
            self.working = False
            if error:
                if self.runtime is None:
                    self.clear_connection()
                self.notice(error, True)
            else:
                self.notice("")
                finished(result)
            self.update_controls()

        async_task(action, finish)

    def refresh(self, *_):
        saved = [entry["url"] for device in self.devices for entry in device["addresses"]]

        def display(result):
            online = result["devices"]
            self.online_ids = {device["id"] for device in online}
            # Keep validated alternate addresses for devices with multiple interfaces.
            self.devices = online + [device for device in self.devices if device["id"] not in self.online_ids]
            save(KNOWN, self.devices)
            self.fill_devices()
            if not online:
                self.notice("No microscopes found. You can enter an address.")

        self.operate("Looking for microscopes…", lambda: discover(saved), display)

    def add_address(self, *_):
        dialog = Gtk.Dialog(title="Add a microscope", transient_for=self.window, modal=True)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Find Device", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)
        entry = Gtk.Entry(placeholder_text="microscope.local:5000", activates_default=True, margin=16)
        dialog.get_content_area().add(entry)
        dialog.show_all()
        answer = dialog.run()
        address = entry.get_text().strip()
        dialog.destroy()
        if answer != Gtk.ResponseType.OK or not address:
            return

        def display(device):
            self.remember(device["id"], device["url"], device["openmhs"])
            self.online_ids.add(device["id"])
            self.fill_devices()
            for row in self.device_list.get_children():
                if row.device["id"] == device["id"]:
                    self.device_list.select_row(row)
                    break
        self.operate("Checking microscope address…", lambda: identify(address, 5), display)

    def remember(self, device_id, address, openmhs):
        device = next((row for row in self.devices if row["id"] == device_id), None)
        if not device:
            device = {"id": device_id, "openmhs": openmhs, "addresses": []}
            self.devices.append(device)
        routes = device["addresses"]
        existing = next((entry for entry in routes if entry["url"] == address), None)
        if not existing or routes[0] != existing:
            device["addresses"] = [existing or {"url": address, "source": "Saved"}] + [
                entry for entry in routes if entry["url"] != address
            ]
            save(KNOWN, self.devices)

    def connect_selected(self, *_):
        selected = self.device_list.get_selected_row()
        if not selected:
            self.add_address()
            return
        device = selected.device
        url = device["addresses"][0]["url"]
        mode = {"Automatic": "auto", "Run on this computer": "worker", "Use existing worker": "viewer"}
        self.connect_device(url, device["id"], mode[self.mode.get_active_id()])

    def connect_device(self, url, expected, mode):
        def start():
            aliases = next(([entry["url"] for entry in item["addresses"]]
                            for item in self.devices if item["id"] == expected), [])
            if self.last.get("id") == expected:
                aliases = list(dict.fromkeys(aliases + self.last.get("aliases", [])))
            device = None
            for address in list(dict.fromkeys([url, *aliases]))[:5]:
                try:
                    candidate = identify(address, 3)
                except (OSError, ValueError):
                    continue
                if expected and expected != candidate["id"]:
                    if address == url:
                        raise RuntimeError("This address belongs to a different microscope. Refresh the device list.")
                    continue
                device = candidate
                break
            if device is None:
                raise OSError("The microscope is unreachable. Check the connection or add its current address.")
            self.stop_runtime()
            log = ROOT / "Logs" / "desktop-linux.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            command = [sys.executable, "-B", "-s", str(RESOURCES / "Runtime" / "desktop.py"),
                       "serve", "--root", str(ROOT), "--url", device["url"], "--id", device["id"],
                       "--mode", mode, "--parent", str(os.getpid())]
            for alias in aliases:
                command.extend(("--alias", alias))
            environment = {**os.environ, "PYTHONPATH": str(RESOURCES / "Python"), "PYTHONUNBUFFERED": "1"}
            with log.open("w", encoding="utf-8") as output:
                runtime = subprocess.Popen(command, env=environment, stdout=output, stderr=subprocess.STDOUT)
            for _ in range(60):
                if runtime.poll() is not None:
                    break
                try:
                    status = read_json(LOCAL + "/desktop/status", .5)
                    state = read_saved(ROOT / "State" / "connection.json", {})
                    if status["device_id"] == device["id"] and state.get("pid") == runtime.pid:
                        self.runtime = runtime
                        self.last = {"url": device["url"], "id": device["id"], "mode": mode, "aliases": aliases}
                        save(LAST, self.last)
                        return status
                except (OSError, ValueError):
                    pass
                time.sleep(.25)
            if runtime.poll() is None:
                runtime.terminate()
                runtime.wait(timeout=7)
            tail = log.read_text(encoding="utf-8").splitlines()
            raise RuntimeError(tail[-1] if tail else "MicroWorld could not start the local service.")

        def display(status):
            self.active, self.api, self.actual = status, Api(status), {}
            self.online = False
            self.health_failures = 0
            self.last_probe = 0.0
            self.frame_time = 0.0
            self.bridge_ready = False
            self.canvas_visible = False
            self.worker_available = bool(status["worker_running"])
            self.remember(status["device_id"], status["microscope"], status["openmhs"])
            self.fill_devices()
            self.panels.connected()
            self.select("openmhs")
            self.observe_bar.set_visible(True)
            self.browser.load_uri(LOCAL + "/?shell=linux&native=1")
            self.show_page()
            self.read_async(lambda api: api.identify_controls(), self.load_capabilities)
            self.tick()
            if self.pending_slide:
                self.open_slide(self.pending_slide)

        self.operate("Connecting to microscope…", start, display)

    def load_capabilities(self, _caps):
        self.panels.status_changed()
        self.panels.load_calibration()

    def clear_connection(self):
        self.active = None
        self.api = None
        self.actual = {}
        self.online = False
        self.worker_available = False
        self.health_failures = 0
        self.browser.load_uri("about:blank")
        self.fill_devices()
        self.show_page()

    def stop_runtime(self):
        if self.runtime is None or self.runtime.poll() is not None:
            self.runtime = None
            return
        if self.active and self.active.get("worker_running"):
            try:
                hardware = read_json(LOCAL + HARDWARE, 3)
            except OSError:
                hardware = {}
            if hardware.get("busy"):
                raise RuntimeError("Wait for the current acquisition before disconnecting.")
        self.runtime.terminate()
        self.runtime.wait(timeout=8)
        self.runtime = None

    def disconnect(self, *_):
        self.operate("Disconnecting…", self.stop_runtime, lambda _: self.clear_connection())

    def close(self, *_):
        if self.action_name:
            self.notice("Wait for the current operation before closing MicroWorld.", True)
        elif not self.working:
            self.operate("Closing MicroWorld…", self.stop_runtime, lambda _: self.quit())
        return True

    @property
    def can_control(self):
        return bool(self.api and self.online and not self.working and not self.action_name and
                    not self.actual.get("hardware_error") and not self.actual.get("hardware", {}).get("busy"))

    @property
    def camera_live(self):
        return self.online and time.monotonic() - self.frame_time < 4.0

    def worker_label(self):
        if self.actual.get("worker_error") or (not self.online and self.active):
            return "PC Worker status unconfirmed"
        if self.active and self.active["worker_running"]:
            return "PC Worker on this computer" if self.worker_available else "PC Worker connecting…"
        return "PC Worker on another computer" if self.worker_available else "PC Worker offline"

    def update_controls(self):
        if not hasattr(self, "panels"):
            return
        self.nav.set_sensitive(bool(self.active))
        self.workspace.set_sensitive(bool(self.active))
        self.history.set_sensitive(bool(self.active))
        for control in (self.find, self.add, self.connect_button, self.disconnect_button, self.mode):
            control.set_sensitive(not self.working and not self.action_name)
        self.disconnect_button.set_sensitive(bool(self.active and not self.working and not self.action_name))
        text = "Microscope connected" if self.online else ("Microscope offline" if self.health_failures >= 3 or not self.active
                                                           else "Reconnecting…")
        if self.working and not self.active:
            text = "Connecting…"
        self.connection_badge.set_text(text)
        context = self.connection_badge.get_style_context()
        (context.add_class if self.online else context.remove_class)("status-live")
        self.worker_badge.set_text(self.worker_label())
        hardware = self.actual.get("hardware", {})
        operation = hardware.get("operation") or hardware.get("label")
        if self.actual.get("hardware_error") and self.online:
            self.hardware_badge.set_text("Hardware status unavailable")
        else:
            self.hardware_badge.set_text(
                (str(operation).capitalize() if operation else "Hardware busy") if hardware.get("busy")
                else ("Hardware idle" if self.online else "")
            )
        camera_live = self.camera_live
        self.camera_badge.set_text("Live" if camera_live else
                                   "Waiting for camera…" if self.online or self.health_failures < 3 else "Camera offline")
        self.roi.set_sensitive(bool(camera_live and self.bridge_ready and self.active and self.active["openmhs"]))
        self.snap.set_sensitive(camera_live and not self.working and not self.action_name)
        self.af.set_sensitive(self.can_control)
        self.recent.set_sensitive(self.online)
        self.scan.set_sensitive(self.can_control and self.worker_available and self.bridge_ready)
        self.panels.status_changed()

    def tick(self):
        if not self.api or self.working:
            return True
        now = time.monotonic()
        if not self.probing and now - self.last_probe >= 1.7:
            self.probe(now)
        if self.online and self.selected_tab in ("navigate", "capture") and not self.camera_pending:
            self.load_frame()
        if self.bridge_ready and self.selected_tab == "openmhs" and now - self.last_camera_probe >= 1.4:
            self.last_camera_probe = now
            self.browser.evaluate_javascript(
                'Boolean(document.querySelector("#stream-display img")?.naturalWidth)',
                -1, None, None, None, self.camera_check, None
            )
        return True

    def camera_check(self, web, result, _data):
        try:
            visible = web.evaluate_javascript_finish(result).to_boolean()
        except GLib.Error:
            return
        if visible:
            self.frame_time = time.monotonic()
        self.update_controls()

    def probe(self, now):
        api = self.api
        self.probing = True
        self.last_probe = now
        need_worker = now - self.actual.get("worker_at", 0) >= 5

        def load():
            result = {"position": api.request(STAGE, timeout=1.4)}
            if api.connection["openmhs"]:
                try:
                    result["hardware"] = api.request(HARDWARE, timeout=1.5)
                    result["hardware_error"] = False
                except OSError:
                    result["hardware_error"] = True
                if need_worker:
                    try:
                        result["local_worker"] = bool(api.request("/desktop/status").get("worker_running"))
                        result["worker"] = api.request(PROMPTS + "/agent", timeout=2.0)
                        result["worker_error"] = False
                    except OSError:
                        result["worker_error"] = True
                    result["worker_at"] = now
            return result

        def finish(value, error):
            self.probing = False
            if self.api is not api:
                return
            reached = self.online
            worker_available = self.worker_available
            if error:
                self.health_failures += 1
                self.online = False
            else:
                self.actual.update(value)
                self.health_failures = 0
                self.online = True
                if "local_worker" in self.actual:
                    api.connection["worker_running"] = self.actual["local_worker"]
                self.worker_available = bool(not self.actual.get("worker_error") and
                                             self.actual.get("worker", {}).get("worker_online"))
            if reached != self.online or self.health_failures == 3:
                self.fill_devices()
            if worker_available != self.worker_available and self.selected_tab == "gallery":
                self.panels.reload_gallery(force=True)
            self.update_controls()
        async_task(load, finish)

    def load_frame(self):
        api = self.api
        self.camera_pending = True

        def display(raw, error):
            self.camera_pending = False
            if self.api is api and not error and self.selected_tab in ("navigate", "capture"):
                self.frame_time = time.monotonic()
                self.panels.update_frame(raw)
        async_task(lambda: api.request(CAMERA, timeout=1.4), display)

    def read_async(self, task, done):
        api = self.api
        if not api:
            return

        def finish(value, error):
            if self.api is not api:
                return
            if error:
                self.notice(error, True)
            else:
                done(value)
        async_task(lambda: task(api), finish)

    def action(self, progress, task, done=None):
        if not self.can_control:
            return
        self.action_name = progress
        self.notice(progress)
        self.update_controls()

        def finish(output):
            if done:
                done(output)

        api = self.api

        def report(value, error):
            self.action_name = ""
            self.action_route = None
            self.panels.capture_progress(None)
            self.last_probe = 0.0
            self.notice(error or "", bool(error))
            self.update_controls()
            if not error:
                finish(value)
        async_task(lambda: task(api), report)

    def report_scan(self, route, state):
        def display():
            self.action_route = route
            self.panels.capture_progress(state.get("progress"))
            self.update_controls()
        GLib.idle_add(display)

    def stop_scan(self):
        route = self.action_route
        if not route:
            return
        self.panels.stop.set_sensitive(False)
        def cancel(api):
            api.request("/desktop/session")
            return api.request(route, method="DELETE")
        self.read_async(cancel, lambda _: self.notice("Stopping scan…"))

    def focus(self, span=2000):
        self.action("Focusing…", lambda api: api.run(FOCUS, {"dz": span}))

    def snapshot(self):
        self.read_async(lambda api: api.snapshot(), lambda path: self.notice(f"Saved {path.name}"))

    def keyboard(self, _window, event):
        if self.selected_tab != "navigate" or not self.can_control:
            return False
        if isinstance(self.window.get_focus(), (Gtk.Entry, Gtk.SpinButton)):
            return False
        if event.state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK | Gdk.ModifierType.SUPER_MASK):
            return False
        move = {
            Gdk.KEY_Up: (0, 1, 0), Gdk.KEY_Down: (0, -1, 0),
            Gdk.KEY_Left: (-1, 0, 0), Gdk.KEY_Right: (1, 0, 0),
            Gdk.KEY_Page_Up: (0, 0, 1), Gdk.KEY_Page_Down: (0, 0, -1),
        }.get(event.keyval)
        if move:
            self.panels.jog(move)
        return move is not None

    def notice(self, message, error=False):
        self.message.set_text(message)
        (self.message.get_style_context().add_class if error else self.message.get_style_context().remove_class)("error")
        self.alert.set_reveal_child(bool(message))

    def confirm(self, title, message):
        dialog = Gtk.MessageDialog(transient_for=self.window, modal=True, text=title, secondary_text=message,
                                   message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.CANCEL)
        dialog.add_button("Continue", Gtk.ResponseType.OK)
        response = dialog.run()
        dialog.destroy()
        return response == Gtk.ResponseType.OK

    def open_recent(self, anchor):
        def display(items):
            popover = Gtk.Popover.new(anchor)
            content = column(5)
            content.set_border_width(10)
            for item in items[:8]:
                sid = item.get("session_id") or ""
                if SESSION_ID.fullmatch(sid):
                    def activate(_button, sid=sid):
                        popover.popdown()
                        self.open_canvas(sid)
                    pack(content, button(item.get("title") or sid, activate))
            def open_gallery(*_):
                popover.popdown()
                self.select("gallery")
            pack(content, button("Browse Gallery", open_gallery))
            popover.add(content)
            popover.show_all()
            popover.popup()
        if self.worker_available:
            self.read_async(lambda api: api.canvases(), display)
        else:
            display([])

    def open_workspace(self, *_):
        if self.active:
            Gio.AppInfo.launch_default_for_uri(Path(self.active["workspace"]).as_uri(), None)

    def open_history(self, *_):
        if self.active:
            Gio.AppInfo.launch_default_for_uri(Path(self.active["history"]).as_uri(), None)

    def open_canvas(self, sid):
        if SESSION_ID.fullmatch(sid):
            self.select("openmhs")
            self.viewer("canvas", sid)

    def open_slide(self, uri):
        parts = urlsplit(uri)
        match = re.fullmatch(r"/([a-zA-Z0-9-]{1,120})/([a-zA-Z0-9_-]{1,120})", parts.path)
        if parts.scheme != "microworld" or parts.netloc != "slide" or not match:
            return
        device_id, slide = match.groups()
        if self.active and self.active["device_id"] == device_id:
            self.pending_slide = None
            if self.bridge_ready:
                self.open_canvas(slide)
            else:
                self.select("openmhs")
                self.browser.load_uri(LOCAL + "/?" + urlencode({
                    "device": device_id, "slide": slide, "native": "1", "shell": "linux",
                }))
            return
        self.pending_slide = uri
        if self.working:
            return
        device = next((item for item in self.devices if item["id"] == device_id), None)
        if device:
            self.connect_device(device["addresses"][0]["url"], device_id, "auto")
        else:
            self.notice("Connect to the microscope for this Slide link.")

    def navigate_uri(self, _view, decision, kind):
        if kind not in (WebKit2.PolicyDecisionType.NAVIGATION_ACTION,
                         WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION):
            return False
        uri = decision.get_navigation_action().get_request().get_uri()
        if uri == "about:blank" or uri.startswith(LOCAL + "/"):
            if kind == WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION:
                decision.ignore()
                self.browser.load_uri(uri)
                return True
            return False
        decision.ignore()
        if uri.startswith("microworld://"):
            self.open_slide(uri)
        elif uri.startswith("https://"):
            Gio.AppInfo.launch_default_for_uri(uri, None)
        return True


if __name__ == "__main__":
    sys.exit(MicroWorld().run(sys.argv))

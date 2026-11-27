"""Linux desktop window for the shared MicroWorld service and viewer."""

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
gi.require_version("WebKit2", "4.1")
from gi.repository import Gio, GLib, Gtk, WebKit2

RESOURCES = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get("MICROWORLD_HOME", Path.home() / "MicroWorld")).expanduser().resolve()
sys.path.insert(0, str(RESOURCES / "Runtime"))

from desktop import LOCAL, write_atomic
from network import discover, identify, read_json

KNOWN = ROOT / "State" / "known-devices.json"
LAST = ROOT / "State" / "linux-device.json"
WORKSPACE_TABS = (
    ("Observe", "openmhs"),
    ("Gallery", "gallery"),
    ("Navigate", "navigate"),
    ("Capture", "capture"),
    ("Settings", "settings"),
)


def read_saved(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def put_button(container, label, action, right=False):
    button = Gtk.Button(label=label)
    button.connect("clicked", action)
    if isinstance(container, Gtk.Box):
        container.pack_start(button, False, False, 0)
    else:
        (container.pack_end if right else container.pack_start)(button)
    return button


def title(text):
    label = Gtk.Label(label=text, xalign=0)
    label.get_style_context().add_class("title")
    return label


class MicroWorld(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="org.microworld.app",
                         flags=Gio.ApplicationFlags.HANDLES_OPEN)
        self.window = None
        self.runtime = None
        self.active = None
        self.working = False
        self.devices = read_saved(KNOWN, [])
        self.last = read_saved(LAST, {})
        self.pending_slide = None
        self.selected_tab = "openmhs"

    def do_activate(self):
        if self.window:
            self.window.present()
            return

        self.window = Gtk.ApplicationWindow(application=self, title="MicroWorld")
        self.window.set_default_size(1320, 880)
        self.window.set_size_request(720, 500)
        self.window.set_icon_name("org.microworld.app")
        self.window.connect("delete-event", self.close)
        header = Gtk.HeaderBar(
            title="MicroWorld", subtitle="Microscopes", show_close_button=True
        )
        self.header = header
        self.window.set_titlebar(header)
        put_button(header, "Microscopes", self.show_picker)
        self.resume = put_button(header, "Resume view", self.show_viewer)
        self.workspace = put_button(header, "Workspace", self.open_workspace, right=True)
        self.history = put_button(header, "History", self.open_history, right=True)
        self.resume.set_sensitive(False)
        self.workspace.set_sensitive(False)
        self.history.set_sensitive(False)

        self.stack = Gtk.Stack()
        self.window.add(self.stack)
        self.browser = WebKit2.WebView()
        self.browser.connect("decide-policy", self.navigate)
        self.browser.connect("load-changed", self.viewer_loaded)
        viewer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        toolbar = Gtk.Box(spacing=12, margin_start=14, margin_end=14,
                          margin_top=8, margin_bottom=8)
        tabs = Gtk.Box()
        tabs.get_style_context().add_class("linked")
        self.workspace_tabs = {}
        for label, route in WORKSPACE_TABS:
            button = Gtk.RadioButton.new_with_label_from_widget(
                self.workspace_tabs.get("openmhs"), label
            )
            button.set_mode(False)
            self.workspace_tabs[route] = button
            tabs.pack_start(button, False, False, 0)
            button.connect("toggled", self.change_workspace_tab, route)
        toolbar.pack_start(tabs, False, False, 0)
        self.saved_canvases = put_button(toolbar, "Saved canvases", self.open_saved_canvases, right=True)
        self.saved_canvases.set_no_show_all(True)
        viewer.pack_start(toolbar, False, False, 0)
        viewer.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)
        viewer.pack_start(self.browser, True, True, 0)
        self.stack.add_named(viewer, "viewer")

        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                        halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        panel.set_size_request(478, -1)
        panel.set_border_width(24)
        self.stack.add_named(panel, "devices")
        panel.add(title("Connect to a microscope"))
        self.device_menu = Gtk.ComboBoxText()
        panel.add(self.device_menu)
        self.address = Gtk.Entry(placeholder_text="Or enter an address, e.g. microscope.local:5000")
        self.address.connect("activate", self.connect_selected)
        panel.add(self.address)
        self.mode = Gtk.ComboBoxText()
        self.mode.append("auto", "PC Worker · Automatic")
        self.mode.append("worker", "PC Worker · Run on this computer")
        self.mode.append("viewer", "PC Worker · Keep the current computer")
        self.mode.set_active_id(self.last.get("mode", "auto"))
        panel.add(self.mode)
        buttons = Gtk.Box(spacing=8)
        self.find = put_button(buttons, "Refresh devices", self.refresh)
        self.connect_button = put_button(buttons, "Connect", self.connect_selected)
        self.connect_button.get_style_context().add_class("suggested-action")
        self.disconnect_button = put_button(buttons, "Disconnect", self.disconnect)
        self.disconnect_button.set_sensitive(False)
        self.spinner = Gtk.Spinner()
        buttons.pack_end(self.spinner, False, False, 0)
        panel.add(buttons)
        self.message = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.message.set_max_width_chars(55)
        panel.add(self.message)
        self.fill_devices()
        self.show_picker()
        self.window.show_all()
        if self.last.get("id") and self.last.get("url"):
            self.connect_device(self.last["url"], self.last["id"], self.last.get("mode", "auto"))
        else:
            self.refresh()

    def do_open(self, files, _count, _hint):
        self.activate()
        for file in files:
            self.open_slide(file.get_uri())

    def show_picker(self, *_):
        self.stack.set_visible_child_name("devices")
        self.header.set_subtitle("Microscopes")

    def show_viewer(self, *_):
        if self.active:
            self.stack.set_visible_child_name("viewer")
            mode = "Local PC Worker" if self.active["worker_running"] else "Camera and stage"
            self.header.set_subtitle(f'{self.active["device_id"][-5:]} · {mode}')

    def change_workspace_tab(self, button, route):
        if button.get_active():
            self.selected_tab = route
            self.saved_canvases.set_visible(route == "gallery")
            self.apply_workspace_tab()

    def viewer_loaded(self, _view, event):
        if event == WebKit2.LoadEvent.FINISHED:
            self.apply_workspace_tab()

    def apply_workspace_tab(self):
        if (self.browser.get_uri() or "").startswith(LOCAL + "/"):
            route = json.dumps(self.selected_tab)
            self.browser.evaluate_javascript(
                f"window.microworldRequestedTab = {route}; "
                f"window.microworldSelectTab?.({route});", -1
            )

    def open_saved_canvases(self, *_):
        self.workspace_tabs["openmhs"].set_active(True)
        # Let Vue finish switching panels before opening the existing Canvas picker.
        self.browser.evaluate_javascript(
            """requestAnimationFrame(() => {
                const canvas = document.querySelector('.mhs-tools .mw-primary');
                if (canvas && !document.querySelector('.camera-tiles-menu')) canvas.click();
            });""", -1
        )

    def fill_devices(self):
        choice = self.device_menu.get_active_id() or self.last.get("url")
        self.device_menu.remove_all()
        for device in self.devices:
            for entry in device["addresses"]:
                self.device_menu.append(entry["url"], f'{device["id"][-5:]} · {entry["url"]}')
        if not choice or not self.device_menu.set_active_id(choice):
            self.device_menu.set_active(0)

    def operate(self, message, action, finished):
        if self.working:
            return
        self.working = True
        for control in (self.find, self.connect_button, self.disconnect_button):
            control.set_sensitive(False)
        self.message.set_text(message)
        self.spinner.start()

        def finish(result, error):
            self.working = False
            self.spinner.stop()
            self.find.set_sensitive(True)
            self.connect_button.set_sensitive(True)
            self.disconnect_button.set_sensitive(self.runtime is not None)
            if error:
                if self.runtime is None:
                    self.active = None
                    for control in (self.resume, self.workspace, self.history):
                        control.set_sensitive(False)
                self.show_picker()
                self.message.set_text(error)
            else:
                finished(result)

        def run():
            try:
                result = action()
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                GLib.idle_add(finish, None, str(error))
            else:
                GLib.idle_add(finish, result, None)

        Thread(target=run, daemon=True).start()

    def refresh(self, *_):
        saved = [entry["url"] for device in self.devices for entry in device["addresses"]]

        def display(result):
            online = result["devices"]
            ids = {device["id"] for device in online}
            self.devices = online + [device for device in self.devices if device["id"] not in ids]
            write_atomic(KNOWN, json.dumps(self.devices, indent=2) + "\n")
            self.fill_devices()
            self.message.set_text(f"{len(online)} microscope(s) found. You can also enter an address.")

        self.operate("Looking for microscopes…", lambda: discover(saved), display)

    def connect_selected(self, *_):
        custom = self.address.get_text().strip()
        url = custom or self.device_menu.get_active_id()
        if not url:
            self.message.set_text("Enter the address shown on your microscope.")
            return
        expected = next((device["id"] for device in self.devices
                         if any(entry["url"] == url for entry in device["addresses"])), None)
        self.connect_device(url, expected, self.mode.get_active_id())

    def connect_device(self, url, expected, mode):
        def start():
            device = identify(url, 5)
            if expected and expected != device["id"]:
                raise RuntimeError("This address belongs to a different microscope. Refresh the device list.")
            aliases = next(([entry["url"] for entry in item["addresses"]]
                            for item in self.devices if item["id"] == device["id"]), [])
            if self.last.get("id") == device["id"]:
                aliases = list(dict.fromkeys(aliases + self.last.get("aliases", [])))
            # The shared server verifies each alias against the pinned microscope ID.
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
                runtime = subprocess.Popen(command, env=environment, stdout=output,
                                           stderr=subprocess.STDOUT)
            for _ in range(60):
                if runtime.poll() is not None:
                    break
                try:
                    status = read_json(LOCAL + "/desktop/status", .5)
                    state = read_saved(ROOT / "State" / "connection.json", {})
                    if status["device_id"] == device["id"] and state.get("pid") == runtime.pid:
                        self.runtime = runtime
                        self.last = {"url": device["url"], "id": device["id"], "mode": mode,
                                     "aliases": aliases}
                        write_atomic(LAST, json.dumps(self.last, indent=2) + "\n")
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
            self.active = status
            if not any(item["id"] == self.last["id"] for item in self.devices):
                self.devices.append({"id": self.last["id"], "openmhs": status["openmhs"],
                                     "addresses": [{"url": self.last["url"], "source": "Saved"}]})
                write_atomic(KNOWN, json.dumps(self.devices, indent=2) + "\n")
            self.fill_devices()
            self.resume.set_sensitive(True)
            self.workspace.set_sensitive(True)
            self.history.set_sensitive(True)
            self.message.set_text("")
            self.workspace_tabs["openmhs"].set_active(True)
            self.browser.load_uri(LOCAL + "/")
            self.show_viewer()
            if self.pending_slide:
                self.open_slide(self.pending_slide)
        self.operate("Connecting to microscope…", start, display)

    def stop_runtime(self):
        if self.runtime is None or self.runtime.poll() is not None:
            self.runtime = None
            return
        if self.active and self.active.get("worker_running"):
            try:
                hardware = read_json(LOCAL + "/api/v2/extensions/org.microworld.scan-mosaic/hardware", 3)
            except OSError:
                hardware = {}
            if hardware.get("busy"):
                raise RuntimeError("Wait for the current acquisition before disconnecting.")
        self.runtime.terminate()
        self.runtime.wait(timeout=8)
        self.runtime = None

    def disconnect(self, *_):
        def display(_):
            self.active = None
            for control in (self.resume, self.workspace, self.history, self.disconnect_button):
                control.set_sensitive(False)
            self.browser.load_uri("about:blank")
            self.show_picker()
            self.message.set_text("Disconnected.")
        self.operate("Disconnecting…", self.stop_runtime, display)

    def close(self, *_):
        if self.working:
            return True

        def finish(_):
            self.quit()

        self.operate("Closing MicroWorld…", self.stop_runtime, finish)
        return True

    def open_workspace(self, *_):
        if self.active:
            Gio.AppInfo.launch_default_for_uri(Path(self.active["workspace"]).as_uri(), None)

    def open_history(self, *_):
        if self.active:
            Gio.AppInfo.launch_default_for_uri(Path(self.active["history"]).as_uri(), None)

    def open_slide(self, uri):
        parts = urlsplit(uri)
        match = re.fullmatch(r"/([a-zA-Z0-9-]{1,120})/([a-zA-Z0-9_-]{1,120})", parts.path)
        if parts.scheme != "microworld" or parts.netloc != "slide" or not match:
            return
        device_id, slide = match.groups()
        if self.active and self.active["device_id"] == device_id:
            self.pending_slide = None
            self.workspace_tabs["openmhs"].set_active(True)
            self.browser.load_uri(LOCAL + "/?" + urlencode({
                "device": device_id, "slide": slide, "native": "1",
            }))
            self.show_viewer()
            return
        self.pending_slide = uri
        if self.working:
            return
        device = next((item for item in self.devices if item["id"] == device_id), None)
        if device:
            self.connect_device(device["addresses"][0]["url"], device_id, "auto")
        else:
            self.show_picker()
            self.message.set_text("Connect to the microscope for this Slide link.")

    def navigate(self, _view, decision, kind):
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

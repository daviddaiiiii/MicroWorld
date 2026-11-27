"""Open the official local DeepSeek Harness Web profile in a GTK window."""

import os
from pathlib import Path
import re
import subprocess
import sys
from threading import Thread

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gio, GLib, Gtk, WebKit2

ROOT = Path(os.environ.get("DEEPSEEK_HARNESS_HOME", Path.home() / "DeepSeek-Harness"))
LOCAL_URL = re.compile(r"^dsh web: (http://127\.0\.0\.1:\d+/[^\s]*)")


class Harness(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="org.microworld.deepseek-harness")
        self.window = None
        self.process = None
        self.origin = None

    def do_activate(self):
        if self.window:
            self.window.present()
            return
        self.window = Gtk.ApplicationWindow(application=self, title="DeepSeek Harness")
        self.window.set_default_size(1280, 870)
        self.window.set_icon_name("utilities-terminal")
        self.browser = WebKit2.WebView()
        self.browser.connect("decide-policy", self.navigate)
        self.status = Gtk.Label(label="Starting DeepSeek Harness…", wrap=True)
        self.stack = Gtk.Stack()
        self.stack.add_named(self.status, "status")
        self.stack.add_named(self.browser, "web")
        self.window.add(self.stack)
        self.window.show_all()
        self.stack.set_visible_child_name("status")
        Thread(target=self.launch, daemon=True).start()

    def launch(self):
        workspace = ROOT / "Workspace"
        state = ROOT / "State"
        try:
            for folder in (workspace, state):
                folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.process = subprocess.Popen(
                [str(ROOT / "node_modules/.bin/dsh"), "web", "--host", "127.0.0.1",
                 "--port", "0", "--no-open"],
                cwd=workspace, env={**os.environ, "DSH_HOME": str(state)},
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            for line in self.process.stdout:
                match = LOCAL_URL.match(line)
                if match and "?" in match[1]:
                    GLib.idle_add(self.open_session, match[1])
            code = self.process.wait()
            GLib.idle_add(self.failure, f"DeepSeek Harness exited with status {code}.")
        except OSError:
            GLib.idle_add(self.failure, "The official DSH runtime is unavailable. Install it in ~/DeepSeek-Harness.")

    def open_session(self, uri):
        self.origin = uri.split("/", 3)[2]
        self.browser.load_uri(uri)
        self.stack.set_visible_child_name("web")

    def failure(self, message):
        self.status.set_text(message)
        self.stack.set_visible_child_name("status")

    def navigate(self, _view, decision, kind):
        if kind not in (WebKit2.PolicyDecisionType.NAVIGATION_ACTION,
                         WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION):
            return False
        uri = decision.get_navigation_action().get_request().get_uri()
        if self.origin and uri.startswith("http://" + self.origin + "/"):
            if kind == WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION:
                decision.ignore()
                self.browser.load_uri(uri)
                return True
            return False
        decision.ignore()
        if uri.startswith("https://"):
            Gio.AppInfo.launch_default_for_uri(uri, None)
        return True

    def do_shutdown(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
        Gtk.Application.do_shutdown(self)


if __name__ == "__main__":
    sys.exit(Harness().run(sys.argv))

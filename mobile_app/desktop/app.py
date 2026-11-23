#!/usr/bin/python3
"""PaperScan desktop window for an existing board workbench."""
import html
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
from urllib.parse import urlsplit

import gi

gi.require_version('Gtk', '3.0')
try:
    gi.require_version('WebKit2', '4.1')
except ValueError:
    gi.require_version('WebKit2', '4.0')
from gi.repository import Gio, GLib, Gtk, WebKit2

APP_ID = 'com.cix.PaperScan'
TITLE = '此芯扫描 · PaperScan'
DEFAULT_URL = 'http://127.0.0.1:8088/'
TUNNEL = 'cix-paperscan-workbench-tunnel.service'
ASSETS = Path(__file__).resolve().parent
CONFIG = Path(GLib.get_user_config_dir()) / 'paperscan' / 'desktop.json'


def validate_address(address):
    parts = urlsplit(address.strip())
    if (parts.scheme not in ('http', 'https') or not parts.hostname
            or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment or parts.port == 0
            or any(character.isspace() for character in address.strip())):
        raise ValueError('请输入完整的 HTTP 或 HTTPS 地址。')
    return parts.geturl() if parts.path else parts.geturl() + '/'


def origin(address):
    parts = urlsplit(address)
    return parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == 'https' else 80)


def stored_address():
    try:
        return validate_address(json.loads(CONFIG.read_text())['address'])
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return DEFAULT_URL


class PaperScan(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        self.address = stored_address()
        self.window = None
        self.view = None
        self.connection_generation = 0

    def do_activate(self):
        if self.window is not None:
            self.window.present()
            return
        self.window = Gtk.ApplicationWindow(application=self, title=TITLE)
        self.window.set_default_size(1180, 850)
        self.window.set_icon_from_file(str(ASSETS / 'icon.svg'))
        self.window.connect('destroy', self.on_close)

        bar = Gtk.HeaderBar(title=TITLE, show_close_button=True)
        self.window.set_titlebar(bar)
        reload_button = Gtk.Button.new_from_icon_name('view-refresh-symbolic', Gtk.IconSize.BUTTON)
        reload_button.set_tooltip_text('重新连接')
        reload_button.get_accessible().set_name('重新连接')
        reload_button.connect('clicked', lambda *_: self.connect_workbench())
        bar.pack_start(reload_button)
        settings_button = Gtk.Button(label='连接设置')
        settings_button.connect('clicked', self.edit_address)
        bar.pack_end(settings_button)

        self.view = WebKit2.WebView()
        self.view.get_settings().set_enable_developer_extras(False)
        self.view.connect('decide-policy', self.navigation_policy)
        self.view.connect('load-failed', self.load_failed)
        self.view.connect('permission-request', self.deny_permission)
        self.view.get_context().connect('download-started', self.download_started)
        self.window.add(self.view)
        self.window.show_all()
        self.connect_workbench()

    def on_close(self, _window):
        self.connection_generation += 1
        self.window = None
        self.view = None

    def connect_workbench(self):
        self.connection_generation += 1
        generation = self.connection_generation
        address = self.address
        if origin(address) != origin(DEFAULT_URL) or not shutil.which('systemctl'):
            self.view.load_uri(address)
            return
        self.show_connection_message('正在连接板子', '正在打开扫描工作台。')

        def start_tunnel():
            try:
                subprocess.run(['systemctl', '--user', 'start', TUNNEL],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=7)
            except (OSError, subprocess.TimeoutExpired):
                pass
            GLib.idle_add(self.load_workbench, generation, address)

        threading.Thread(target=start_tunnel, daemon=True).start()

    def load_workbench(self, generation, address):
        if self.view is not None and generation == self.connection_generation:
            self.view.load_uri(address)
        return GLib.SOURCE_REMOVE

    def show_connection_message(self, title, message):
        self.view.load_html(f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>{html.escape(title)}</title><style>
body{{background:#111513;color:#eef5ec;font:16px system-ui;margin:0;display:grid;min-height:100vh;place-items:center}}
main{{max-width:440px;padding:28px}}p{{color:#a4b3a8;line-height:1.7}}code{{overflow-wrap:anywhere}}
</style><main><h1>{html.escape(title)}</h1><p>{html.escape(message)}</p>
<p><code>{html.escape(self.address)}</code></p></main></html>''')

    def load_failed(self, _view, _event, uri, error):
        if error.matches(WebKit2.network_error_quark(), WebKit2.NetworkError.CANCELLED):
            return False
        if uri.startswith(('http://', 'https://')):
            self.show_connection_message(
                '工作台暂时无法连接',
                '检查板子和网线后，点击左上角的重新连接。也可以在连接设置中切换工作台地址。')
            return True
        return False

    def edit_address(self, _button):
        dialog = Gtk.Dialog(title='连接设置', transient_for=self.window, modal=True)
        dialog.add_button('取消', Gtk.ResponseType.CANCEL)
        dialog.add_button('保存并连接', Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)
        box = dialog.get_content_area()
        box.set_border_width(16)
        box.set_spacing(8)
        label = Gtk.Label(label='工作台地址', xalign=0)
        entry = Gtk.Entry(text=self.address, width_chars=46, activates_default=True)
        label.set_mnemonic_widget(entry)
        feedback = Gtk.Label(xalign=0)
        box.pack_start(label, False, False, 0)
        box.pack_start(entry, False, False, 0)
        box.pack_start(feedback, False, False, 0)
        dialog.show_all()
        while dialog.run() == Gtk.ResponseType.OK:
            try:
                address = validate_address(entry.get_text())
                CONFIG.parent.mkdir(parents=True, exist_ok=True)
                temporary = CONFIG.with_suffix('.tmp')
                temporary.write_text(json.dumps({'address': address}, indent=2) + '\n')
                os.replace(temporary, CONFIG)
            except (OSError, ValueError) as error:
                feedback.set_text(str(error))
                continue
            self.address = address
            self.connect_workbench()
            break
        dialog.destroy()

    def navigation_policy(self, view, decision, kind):
        if kind in (WebKit2.PolicyDecisionType.NAVIGATION_ACTION, WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION):
            uri = decision.get_navigation_action().get_request().get_uri()
            if uri == 'about:blank':
                return False
            if origin(uri) != origin(self.address):
                decision.ignore()
                return True
            if kind == WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION:
                decision.ignore()
                view.load_uri(uri)
                return True
        if kind == WebKit2.PolicyDecisionType.RESPONSE:
            response = decision.get_response()
            if origin(response.get_uri()) != origin(self.address):
                decision.ignore()
                return True
            headers = response.get_http_headers()
            disposition = headers.get_one('Content-Disposition') if headers else ''
            if (disposition or '').lower().startswith('attachment') or not decision.is_mime_type_supported():
                decision.download()
                return True
        return False

    @staticmethod
    def deny_permission(_view, request):
        request.deny()
        return True

    def download_started(self, _context, download):
        if download.get_web_view() != self.view:
            return
        download.connect('decide-destination', self.choose_download)

    def choose_download(self, download, suggested_name):
        dialog = Gtk.FileChooserNative.new('保存 PaperScan 文件', self.window,
                                           Gtk.FileChooserAction.SAVE, '保存', '取消')
        directory = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD)
        if directory and Path(directory).is_dir():
            dialog.set_current_folder(directory)
        dialog.set_current_name(Path(suggested_name).name)
        dialog.set_do_overwrite_confirmation(True)
        if dialog.run() == Gtk.ResponseType.ACCEPT:
            download.set_allow_overwrite(True)
            download.set_destination(dialog.get_uri())
        else:
            download.cancel()
        dialog.destroy()
        return True


if __name__ == '__main__':
    GLib.set_prgname('paperscan')
    GLib.set_application_name(TITLE)
    raise SystemExit(PaperScan().run())

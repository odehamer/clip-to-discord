import gc
import hashlib
import http.server
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402
import clip_watcher  # noqa: E402


def make_zip(path: Path, hostile: bool = True) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("x/bin/ffmpeg.exe", b"FFMPEG")
        zf.writestr("x/bin/ffprobe.exe", b"FFPROBE")
        zf.writestr("x/doc/readme.txt", b"hi")
        if hostile:
            zf.writestr("../evil.exe", b"EVIL")
            zf.writestr("../bin/ffmpeg.exe", b"EVIL2")


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def serve_dir(directory: str):
    handler = lambda *a, **k: QuietHandler(*a, directory=directory, **k)  # noqa: E731
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class VersionTests(unittest.TestCase):
    def test_compare(self):
        self.assertTrue(app.is_newer("v1.0.1", "1.0.0"))
        self.assertTrue(app.is_newer("1.10.0", "1.9.9"))
        self.assertTrue(app.is_newer("v2", "1.9.9"))
        self.assertFalse(app.is_newer("v1.0.0", "1.0.0"))
        self.assertFalse(app.is_newer("1.0", "1.0.0"))
        self.assertFalse(app.is_newer("v0.9", "1.0.0"))
        self.assertFalse(app.is_newer("garbage", "1.0.0"))
        self.assertFalse(app.is_newer("", "1.0.0"))


class ZipTests(unittest.TestCase):
    def test_select_members(self):
        names = ["x/bin/ffmpeg.exe", "x/bin/ffprobe.exe", "x/doc/readme.txt",
                 "../evil.exe", "../bin/ffmpeg.exe", "/abs/bin/ffmpeg.exe", "C:/bin/ffprobe.exe"]
        self.assertEqual(app.select_zip_members(names),
                         {"ffmpeg.exe": "x/bin/ffmpeg.exe", "ffprobe.exe": "x/bin/ffprobe.exe"})

    def test_extract_only_tools(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            z = d / "t.zip"
            make_zip(z)
            out = d / "sub" / "bin"
            app.extract_tools(z, out)
            self.assertEqual(sorted(p.name for p in out.iterdir()), ["ffmpeg.exe", "ffprobe.exe"])
            self.assertEqual((out / "ffmpeg.exe").read_bytes(), b"FFMPEG")
            self.assertFalse((d / "evil.exe").exists())
            self.assertFalse((d / "sub" / "evil.exe").exists())

    def test_extract_missing_tools(self):
        with tempfile.TemporaryDirectory() as d:
            z = Path(d) / "t.zip"
            with zipfile.ZipFile(z, "w") as zf:
                zf.writestr("x/doc/readme.txt", b"hi")
            with self.assertRaises(app.ToolsError):
                app.extract_tools(z, Path(d) / "bin")


class ShaAndDownloadTests(unittest.TestCase):
    def test_verify(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "f"
            f.write_bytes(b"hello")
            good = hashlib.sha256(b"hello").hexdigest()
            self.assertTrue(app.verify_sha256(f, good))
            self.assertTrue(app.verify_sha256(f, good.upper() + "\n"))
            self.assertFalse(app.verify_sha256(f, "0" * 64))

    def test_download_and_install(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            web = d / "web"
            web.mkdir()
            make_zip(web / "tools.zip")
            digest = hashlib.sha256((web / "tools.zip").read_bytes()).hexdigest()
            (web / "tools.zip.sha256").write_text(digest + "\n")
            srv = serve_dir(str(web))
            try:
                base = f"http://127.0.0.1:{srv.server_port}/tools.zip"
                calls = []
                part = d / "dl" / "x.part"
                app.download_file(base, part, lambda a, b: calls.append((a, b)))
                self.assertEqual(part.stat().st_size, (web / "tools.zip").stat().st_size)
                self.assertEqual(calls[-1][0], calls[-1][1])

                ff = d / "ffmpeg"
                app.install_tools(base, base + ".sha256", ff, lambda a, b: None)
                self.assertTrue((ff / "bin" / "ffmpeg.exe").is_file())
                self.assertTrue((ff / "bin" / "ffprobe.exe").is_file())
                self.assertFalse((ff / "download.zip.part").exists())

                # bad checksum: everything cleaned up
                (web / "tools.zip.sha256").write_text("0" * 64)
                ff2 = d / "ffmpeg2"
                with self.assertRaises(app.ToolsError):
                    app.install_tools(base, base + ".sha256", ff2)
                self.assertFalse((ff2 / "download.zip.part").exists())
                self.assertFalse((ff2 / "bin" / "ffmpeg.exe").exists())

                # 404 on download
                with self.assertRaises(app.ToolsError):
                    app.download_file(base + ".nope", d / "n.part")
            finally:
                srv.shutdown()


class ConfigTests(unittest.TestCase):
    def test_merge_preserves_keys(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.json"
            p.write_text(json.dumps({"webhook_url": "old", "clip_seconds": 20, "max_upload_mb": 50,
                                     "custom": "x"}))
            url = "https://discord.com/api/webhooks/1/abc"
            app.save_config(p, url, False)
            data = json.loads(p.read_text())
            self.assertEqual(data["clip_seconds"], 20)
            self.assertEqual(data["max_upload_mb"], 50)
            self.assertEqual(data["custom"], "x")
            self.assertEqual(data["webhook_url"], url)
            self.assertIs(data["delete_original_after_upload"], False)
            self.assertTrue(app.has_valid_config(p))

    def test_new_file_and_invalid(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sub" / "config.json"
            self.assertFalse(app.has_valid_config(p))
            app.save_config(p, "https://discord.com/api/webhooks/1/abc", True)
            self.assertTrue(p.is_file())

    def test_default_paths_env(self):
        with mock.patch.dict(os.environ, {app.HOME_ENV: r"C:\somewhere"}):
            self.assertEqual(app.default_paths().app_dir, Path(r"C:\somewhere"))
            self.assertEqual(app.default_paths().ffmpeg_bin, Path(r"C:\somewhere\ffmpeg\bin"))


class WatchTests(unittest.TestCase):
    def _cfg(self, d, poll=0.05):
        return clip_watcher.Config(webhook_url="x", watch_dir=str(d), poll_seconds=poll)

    def test_stop_returns_promptly(self):
        with tempfile.TemporaryDirectory() as d:
            stop = threading.Event()
            t = threading.Thread(target=clip_watcher.watch,
                                 args=(self._cfg(d, poll=30), (Path("a"), Path("b")), stop))
            t.start()
            time.sleep(0.2)
            t0 = time.time()
            stop.set()
            t.join(3)
            self.assertFalse(t.is_alive())
            self.assertLess(time.time() - t0, 2)

    def test_paused_files_are_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            stop, paused = threading.Event(), threading.Event()
            processed = []
            with mock.patch.object(clip_watcher, "wait_until_stable", return_value=True), \
                    mock.patch.object(clip_watcher, "process_clip",
                                      side_effect=lambda cfg, tools, p, dry_run: processed.append(p.name)):
                t = threading.Thread(target=clip_watcher.watch,
                                     args=(self._cfg(d), (Path("a"), Path("b")), stop, paused))
                t.start()
                try:
                    paused.set()
                    time.sleep(0.2)
                    (d / "one.DVR.mp4").write_bytes(b"1")
                    time.sleep(0.4)
                    paused.clear()
                    time.sleep(0.3)
                    (d / "two.DVR.mp4").write_bytes(b"2")
                    time.sleep(0.5)
                finally:
                    stop.set()
                    t.join(3)
            self.assertEqual(processed, ["two.DVR.mp4"])


class MockDiscord(http.server.BaseHTTPRequestHandler):
    bodies: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        if self.path.startswith("/ok"):
            MockDiscord.bodies.append((self.path, json.loads(body), self.headers.get("User-Agent")))
            payload, code = b'{"id":"1"}', 200
        else:
            payload, code = b'{"message":"Unknown Webhook","code":10015}', 404
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class PostTextTests(unittest.TestCase):
    def test_success_and_error(self):
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MockDiscord)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{srv.server_port}"
            clip_watcher.post_text(base + "/ok/SECRETTOKEN", "hello")
            path, body, ua = MockDiscord.bodies[-1]
            self.assertTrue(path.endswith("?wait=true"))
            self.assertEqual(body, {"content": "hello", "allowed_mentions": {"parse": []}})
            self.assertTrue(ua.startswith("clip-to-discord/"))
            with self.assertRaises(clip_watcher.ClipError) as cm:
                clip_watcher.post_text(base + "/bad/SECRETTOKEN", "hello")
            msg = str(cm.exception)
            self.assertIn("404", msg)
            self.assertIn("Unknown Webhook", msg)
            self.assertNotIn("SECRETTOKEN", msg)
            self.assertNotIn("127.0.0.1", msg)
        finally:
            srv.shutdown()

    def test_network_error_has_no_url(self):
        with self.assertRaises(clip_watcher.ClipError) as cm:
            clip_watcher.post_text("http://127.0.0.1:1/SECRETTOKEN", "x")
        self.assertNotIn("SECRETTOKEN", str(cm.exception))


class AutostartTests(unittest.TestCase):
    KEY = r"Software\ClipToDiscordTest\Run"

    def tearDown(self):
        import winreg
        for k in (self.KEY, r"Software\ClipToDiscordTest"):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, k)
            except OSError:
                pass

    def test_roundtrip(self):
        import winreg
        exe = Path(r"C:\Test Dir\ClipToDiscord.exe")
        self.assertFalse(app.autostart_enabled(self.KEY))
        self.assertTrue(app.set_autostart(True, exe, self.KEY, require_frozen=False))
        self.assertTrue(app.autostart_enabled(self.KEY))
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.KEY) as k:
            self.assertEqual(winreg.QueryValueEx(k, app.RUN_VALUE)[0], f'"{exe}"')
        app.set_autostart(False, exe, self.KEY, require_frozen=False)
        self.assertFalse(app.autostart_enabled(self.KEY))
        app.set_autostart(False, exe, self.KEY, require_frozen=False)  # idempotent

    def test_skipped_when_not_frozen(self):
        self.assertFalse(app.set_autostart(True, Path("x.exe"), self.KEY))
        self.assertFalse(app.autostart_enabled(self.KEY))


class RemoveSafetyTests(unittest.TestCase):
    def test_safety(self):
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "Documents"
            bad.mkdir()
            good = Path(d) / "ClipToDiscord"
            good.mkdir()
            self.assertFalse(app.safe_to_remove(bad))
            self.assertFalse(app.safe_to_remove(Path(d)))
            self.assertTrue(app.safe_to_remove(good))
            self.assertTrue(app.safe_to_remove(bad, str(bad)))
            self.assertFalse(app.safe_to_remove(bad, str(good)))

    def test_remove_local_data_refuses(self):
        with tempfile.TemporaryDirectory() as d:
            p = app.Paths(Path(d) / "Docs")
            p.ffmpeg_dir.mkdir(parents=True)
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop(app.HOME_ENV, None)
                with self.assertRaises(app.ToolsError):
                    app.remove_local_data(p)
            self.assertTrue(p.ffmpeg_dir.exists())

    def test_remove_local_data_ok(self):
        with tempfile.TemporaryDirectory() as d:
            p = app.Paths(Path(d) / "ClipToDiscord")
            p.ffmpeg_bin.mkdir(parents=True)
            p.config.write_text("{}")
            p.log.write_text("keep")
            app.remove_local_data(p)
            self.assertFalse(p.config.exists())
            self.assertFalse(p.ffmpeg_dir.exists())
            self.assertTrue(p.log.exists())


class ControlTests(unittest.TestCase):
    def test_commands_and_port_free(self):
        got = []
        srv = app.ControlServer(got.append, port=0)
        self.assertTrue(srv.start())
        try:
            self.assertTrue(app.send_command("show", srv.port))
            self.assertTrue(app.send_command("quit", srv.port))
            self.assertFalse(app.send_command("bogus", srv.port))
            time.sleep(0.1)
            self.assertEqual(got, ["show", "quit"])
            self.assertFalse(app.ControlServer(got.append, port=srv.port).start())
        finally:
            srv.close()
        self.assertTrue(app.wait_port_free(srv.port, 3))


class IconTests(unittest.TestCase):
    def test_icon(self):
        im = app.draw_icon(64)
        self.assertEqual(im.size, (64, 64))
        self.assertNotEqual(app.draw_icon(32).getpixel((3, 16)), app.draw_icon(32, True).getpixel((3, 16)))


class GuiSmokeTests(unittest.TestCase):
    def test_settings_window_defaults(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {app.HOME_ENV: d}):
            a = app.App(app.default_paths())
            try:
                a.create_root()
            except Exception as e:  # noqa: BLE001
                self.skipTest(f"Tk unavailable: {e}")
            try:
                a.window = app.SettingsWindow(a)
                for _ in range(3):
                    a.root.update()
                w = a.window
                self.assertTrue(w.autostart_var.get())
                self.assertTrue(w.delete_var.get())
                self.assertEqual(w.delete_cb.cget("text"),
                                 "Delete the original clip after it's posted. "
                                 "The Discord copy will be the only copy.")
                self.assertEqual(w.entry.cget("show"), "•")
                # invalid URL gives an inline error, no popup
                w.webhook_var.set("nonsense")
                w.on_test()
                self.assertIn("webhook", w.msg_var.get())
                w.show_progress("Downloading video tools… 1 of 110 MB", 0.01)
                a.root.update()
                w.hide_progress()
            finally:
                a.root.destroy()
                # release Tk variables on this thread so they are not finalised elsewhere
                a.window = None
                a.root = None
                w = None
                gc.collect()


if __name__ == "__main__":
    unittest.main()

import gc
import hashlib
import http.server
import json
import os
import re
import shutil
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

DEST_A = clip_watcher.Destination("Friends #clips", "https://discord.com/api/webhooks/1/aaa", True)
DEST_B = clip_watcher.Destination("Work", "https://discord.com/api/webhooks/2/bbb", True)
SYNTH_CLIP = Path("C:/Users/oscar/AppData/Local/Temp/claude/4770acc3d7bcc157/7eaf93fb-4e67-494b-bd92-d4c4351be103/"
                  "scratchpad/cliptest/Test Game/Test Game 2026.11.10 - 12.00.00.01.DVR.mp4")


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
            app.save_config(p, [clip_watcher.Destination("Mine", url, True)], False)
            data = json.loads(p.read_text())
            self.assertEqual(data["clip_seconds"], 20)
            self.assertEqual(data["max_upload_mb"], 50)
            self.assertEqual(data["custom"], "x")
            self.assertNotIn("webhook_url", data)
            self.assertEqual(data["destinations"], [{"name": "Mine", "url": url, "enabled": True}])
            self.assertIs(data["delete_original_after_upload"], False)
            self.assertTrue(app.has_valid_config(p))

    def test_new_file_and_invalid(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sub" / "config.json"
            self.assertFalse(app.has_valid_config(p))
            app.save_config(p, [DEST_A], True)
            self.assertTrue(p.is_file())
            self.assertTrue(app.has_valid_config(p))

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
                                      side_effect=lambda cfg, tools, p, dry_run, source=None:
                                      processed.append(p.name)):
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
                self.assertEqual(w.name_var.get(), "")
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


class SourceTests(unittest.TestCase):
    def test_old_config_is_nvidia_only(self):
        cfg = clip_watcher.Config(watch_dir=r"C:\V", filename_suffix=".DVR.mp4",
                                  delete_original_after_upload=False)
        self.assertEqual(clip_watcher.get_sources(cfg),
                         [clip_watcher.Source("NVIDIA", Path(r"C:\V"), ".DVR.mp4", False)])

    def test_both_enabled_and_order(self):
        cfg = clip_watcher.Config(watch_dir=r"C:\V", medal_enabled=True, medal_dir=r"D:\M\Clips",
                                  medal_delete_original=True)
        srcs = clip_watcher.get_sources(cfg)
        self.assertEqual([s.name for s in srcs], ["NVIDIA", "Medal"])
        self.assertEqual(srcs[1], clip_watcher.Source("Medal", Path(r"D:\M\Clips"), ".mp4", True))

    def test_nvidia_can_be_disabled(self):
        cfg = clip_watcher.Config(watch_dir=r"C:\V", nvidia_enabled=False,
                                  medal_enabled=True, medal_dir=r"D:\M")
        self.assertEqual([s.name for s in clip_watcher.get_sources(cfg)], ["Medal"])

    def test_medal_without_folder_is_left_out(self):
        cfg = clip_watcher.Config(watch_dir=r"C:\V", medal_enabled=True)
        with mock.patch.object(clip_watcher, "detect_medal_dir", return_value=None):
            self.assertEqual([s.name for s in clip_watcher.get_sources(cfg)], ["NVIDIA"])
        with mock.patch.object(clip_watcher, "detect_medal_dir", return_value=r"E:\Medal\Clips"):
            srcs = clip_watcher.get_sources(cfg)
            self.assertEqual(srcs[1].dir, Path(r"E:\Medal\Clips"))

    def test_detect_medal_dir(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "b").mkdir()
            (d / "c").mkdir()
            self.assertEqual(clip_watcher.detect_medal_dir([d / "a", d / "b", d / "c"]), str(d / "b"))
            self.assertIsNone(clip_watcher.detect_medal_dir([d / "a"]))

    def test_old_config_file_loads(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.json"
            p.write_text(json.dumps({"webhook_url": "x", "watch_dir": d}))
            cfg = clip_watcher.load_config(p, False)
            self.assertTrue(cfg.nvidia_enabled)
            self.assertFalse(cfg.medal_enabled)
            self.assertEqual(cfg.medal_dir, "")

    def test_should_delete_original(self):
        cfg = clip_watcher.Config(delete_original_after_upload=True, medal_delete_original=False)
        nv = clip_watcher.Source("NVIDIA", Path("a"), ".DVR.mp4", True)
        md = clip_watcher.Source("Medal", Path("b"), ".mp4", False)
        self.assertTrue(clip_watcher.should_delete_original(cfg, None))
        self.assertTrue(clip_watcher.should_delete_original(cfg, nv))
        self.assertFalse(clip_watcher.should_delete_original(cfg, md))
        cfg2 = clip_watcher.Config(delete_original_after_upload=False)
        self.assertFalse(clip_watcher.should_delete_original(cfg2, None))
        self.assertTrue(clip_watcher.should_delete_original(
            cfg2, clip_watcher.Source("Medal", Path("b"), ".mp4", True)))


class GameNameTests(unittest.TestCase):
    def test_medal_name(self):
        f = clip_watcher.game_from_medal_name
        self.assertEqual(f("MedalTVValorant20240115123456"), "Valorant")
        self.assertEqual(f("medaltv_Rocket League_2024-01-15"), "Rocket League")
        self.assertEqual(f("Apex Legends 2024"), "Apex Legends")
        self.assertIsNone(f("20240115123456"))
        self.assertIsNone(f("MedalTV123"))
        self.assertIsNone(f("a1"))

    def test_derive(self):
        base = Path(r"C:\Videos")
        d = clip_watcher.derive_game_name
        self.assertEqual(d(base / "Test Game 2026.11.10 - 12.00.00.01.DVR.mp4", base), "Test Game")
        self.assertEqual(d(base / "Test Game" / "x 2026.11.10 - 12.00.00.01.DVR.mp4", base), "Test Game")
        self.assertEqual(d(base / "MedalTVValorant20240115123456.mp4", base), "Valorant")
        self.assertEqual(d(base / "Valorant" / "whatever.mp4", base), "Valorant")
        self.assertEqual(d(base / "12345.mp4", base), "Clip")
        self.assertEqual(d(base / "Desktop 2026.DVR.mp4", base), "Desktop")

    def test_upload_filename(self):
        up = clip_watcher.upload_filename
        self.assertEqual(up("Test Game", Path("Test Game 2026.11.10 - 12.00.00.01.DVR.mp4")),
                         "Test Game 2026-11-10 12.00.mp4")
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "MedalTVValorant1.mp4"
            f.write_bytes(b"x")
            ts = time.mktime((2024, 1, 15, 12, 34, 56, 0, 0, -1))
            os.utime(f, (ts, ts))
            self.assertEqual(up("Valorant", f), "Valorant 2024-01-15 12.34.mp4")
        self.assertEqual(up("Valorant", Path(r"Z:\missing\a.mp4")), "Valorant.mp4")


class MultiSourceWatchTests(unittest.TestCase):
    def setUp(self):
        self._scans: list[str] = []
        self._scan_lock = threading.Lock()

    def wait_scans(self, folder, extra=2, timeout=10.0):
        """Block until the watcher has scanned `folder` `extra` more times than it had so far,
        so the test acts on the watcher's progress instead of on fixed sleeps."""
        folder = str(folder)
        with self._scan_lock:
            target = self._scans.count(folder) + extra
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._scan_lock:
                if self._scans.count(folder) >= target:
                    return
            time.sleep(0.01)
        self.fail(f"watcher did not scan {folder} in time")

    def _run(self, cfg, actions, watched):
        """Run watch() with process_clip recording (file name, source); actions runs once every
        folder in `watched` has been scanned, and the run ends after two more full polls."""
        stop = threading.Event()
        calls = []
        real_scan = clip_watcher.scan

        def counting_scan(d, suffix):
            result = real_scan(d, suffix)
            with self._scan_lock:
                self._scans.append(str(d))
            return result

        with mock.patch.object(clip_watcher, "wait_until_stable", return_value=True), \
                mock.patch.object(clip_watcher, "scan", side_effect=counting_scan), \
                mock.patch.object(clip_watcher, "process_clip",
                                  side_effect=lambda cfg, tools, p, dry_run, source=None:
                                  calls.append((p.name, source))):
            t = threading.Thread(target=clip_watcher.watch,
                                 args=(cfg, (Path("a"), Path("b")), stop))
            t.start()
            try:
                for folder in watched:
                    self.wait_scans(folder, extra=1)
                actions()
                for folder in watched:
                    self.wait_scans(folder, extra=2)
            finally:
                stop.set()
                t.join(3)
        return calls

    def test_both_sources(self):
        with tempfile.TemporaryDirectory() as d:
            nv, md = Path(d) / "nv", Path(d) / "md"
            nv.mkdir()
            md.mkdir()
            (md / "old.mp4").write_bytes(b"0")
            cfg = clip_watcher.Config(webhook_url="x", watch_dir=str(nv), medal_enabled=True,
                                      medal_dir=str(md), poll_seconds=0.05)

            def act():
                (nv / "G 2026.01.02 - 03.04.05.06.DVR.mp4").write_bytes(b"1")
                (nv / "plain.mp4").write_bytes(b"2")
                (md / "MedalTVValorant1.mp4").write_bytes(b"3")

            calls = self._run(cfg, act, [nv, md])
            by_name = {n: s for n, s in calls}
            self.assertEqual(len(calls), 2)
            self.assertEqual(by_name["G 2026.01.02 - 03.04.05.06.DVR.mp4"].name, "NVIDIA")
            self.assertEqual(by_name["MedalTVValorant1.mp4"].name, "Medal")
            self.assertEqual(by_name["MedalTVValorant1.mp4"].dir, md)

    def test_overlap_first_source_wins(self):
        with tempfile.TemporaryDirectory() as d:
            nv = Path(d) / "videos"
            nv.mkdir()
            cfg = clip_watcher.Config(webhook_url="x", watch_dir=str(nv), medal_enabled=True,
                                      medal_dir=str(nv), filename_suffix=".mp4", poll_seconds=0.05)
            calls = self._run(cfg, lambda: (nv / "a.mp4").write_bytes(b"1"), [nv])
            self.assertEqual([(n, s.name) for n, s in calls], [("a.mp4", "NVIDIA")])

    def test_medal_dir_created_later(self):
        with tempfile.TemporaryDirectory() as d:
            nv, md = Path(d) / "nv", Path(d) / "md"
            nv.mkdir()
            cfg = clip_watcher.Config(webhook_url="x", watch_dir=str(nv), medal_enabled=True,
                                      medal_dir=str(md), poll_seconds=0.05)

            def act():
                md.mkdir()
                (md / "initial.mp4").write_bytes(b"1")
                self.wait_scans(md, extra=1)  # the Medal folder's first scan is its baseline
                (md / "later.mp4").write_bytes(b"2")

            calls = self._run(cfg, act, [nv])
            self.assertEqual([(n, s.name) for n, s in calls], [("later.mp4", "Medal")])


class MedalConfigTests(unittest.TestCase):
    def test_merge_new_keys_keeps_unknown(self):
        merged = app.merge_config({"custom": 1, "clip_seconds": 20, "watch_dir": "V"},
                                  [DEST_A], True, False, True, r"D:\Medal\Clips")
        self.assertEqual(merged["custom"], 1)
        self.assertEqual(merged["clip_seconds"], 20)
        self.assertEqual(merged["watch_dir"], "V")
        self.assertIs(merged["nvidia_enabled"], False)
        self.assertIs(merged["medal_enabled"], True)
        self.assertEqual(merged["medal_dir"], r"D:\Medal\Clips")
        self.assertIs(merged["delete_original_after_upload"], True)

    def test_save_config_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.json"
            app.save_config(p, [DEST_A], False, True, True, "M")
            cfg = clip_watcher.load_config(p, False)
            self.assertTrue(cfg.nvidia_enabled and cfg.medal_enabled)
            self.assertEqual(cfg.medal_dir, "M")
            self.assertFalse(cfg.medal_delete_original)

    def test_normalize_medal_dir(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "Medal"
            (root / "Clips").mkdir(parents=True)
            (root / "Editor").mkdir()
            self.assertEqual(app.normalize_medal_dir(str(root)), str(root / "Clips"))
            self.assertEqual(app.normalize_medal_dir(str(root / "Clips")), str(root / "Clips"))
            self.assertEqual(app.normalize_medal_dir(str(root / "Editor")), str(root / "Editor"))
            self.assertEqual(app.normalize_medal_dir(""), "")

    def test_initial_sources(self):
        f = app.initial_sources
        self.assertEqual(f({}, False, True, False), (True, False))
        self.assertEqual(f({}, False, False, True), (False, True))
        self.assertEqual(f({}, False, True, True), (True, True))
        self.assertEqual(f({}, False, False, False), (True, False))
        self.assertEqual(f({"webhook_url": "x"}, True, False, True), (True, False))
        self.assertEqual(f({"nvidia_enabled": False, "medal_enabled": True}, True, True, False),
                         (False, True))

    def test_version(self):
        self.assertEqual(app.__version__, "1.1.0")


class SourceWindowTests(unittest.TestCase):
    def _window(self):
        a = app.App(app.default_paths())
        try:
            a.create_root()
        except Exception as e:  # noqa: BLE001
            self.skipTest(f"Tk unavailable: {e}")
        a.window = app.SettingsWindow(a)
        for _ in range(3):
            a.root.update()
        return a

    def _close(self, a):
        a.root.destroy()
        a.window = None
        a.root = None
        gc.collect()

    def test_checkboxes_and_validation(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {app.HOME_ENV: d}), \
                mock.patch.object(app, "nvidia_detected", return_value=False), \
                mock.patch.object(app, "medal_detected", return_value=False), \
                mock.patch.object(clip_watcher, "detect_medal_dir", return_value=None):
            a = self._window()
            try:
                w = a.window
                self.assertEqual(w.nvidia_cb.cget("text"), "NVIDIA Instant Replay")
                self.assertEqual(w.medal_cb.cget("text"), "Medal")
                # nothing detected: NVIDIA ticked, warning shown, folder row disabled
                self.assertTrue(w.nvidia_var.get())
                self.assertFalse(w.medal_var.get())
                self.assertTrue(hasattr(w, "warning"))
                self.assertEqual(str(w.medal_browse_btn.cget("state")), "disabled")
                w.destinations.append(DEST_A)

                w.nvidia_var.set(False)
                w.on_save()
                self.assertEqual(w.msg_var.get(), app.NO_SOURCE_ERROR)

                w.medal_var.set(True)
                w._update_medal_state()
                self.assertEqual(str(w.medal_browse_btn.cget("state")), "normal")
                w.medal_dir_var.set("")
                w.on_save()
                self.assertIn("Medal clips folder", w.msg_var.get())

                w.medal_dir_var.set(str(Path(d) / "does-not-exist"))
                w.on_save()
                self.assertIn("Medal clips folder", w.msg_var.get())
                self.assertFalse(app.default_paths().config.exists())
            finally:
                self._close(a)

    def test_medal_detected_defaults_and_save(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {app.HOME_ENV: d}):
            medal = Path(d) / "Medal"
            (medal / "Clips").mkdir(parents=True)
            with mock.patch.object(app, "nvidia_detected", return_value=False), \
                    mock.patch.object(app, "medal_detected", return_value=True), \
                    mock.patch.object(clip_watcher, "detect_medal_dir", return_value=None), \
                    mock.patch.object(app.App, "prepare_and_start"), \
                    mock.patch.object(app, "set_autostart"):
                a = self._window()
                try:
                    w = a.window
                    self.assertFalse(w.nvidia_var.get())
                    self.assertTrue(w.medal_var.get())
                    self.assertFalse(hasattr(w, "warning"))
                    w.destinations.append(DEST_A)
                    w.medal_dir_var.set(str(medal))  # capture folder -> Clips inside it
                    w.on_save()
                    saved = json.loads(app.default_paths().config.read_text())
                    self.assertIs(saved["medal_enabled"], True)
                    self.assertIs(saved["nvidia_enabled"], False)
                    self.assertEqual(saved["medal_dir"], str(medal / "Clips"))
                finally:
                    self._close(a)


class PlayerNameTests(unittest.TestCase):
    def test_validate(self):
        v = clip_watcher.validate_player_name
        self.assertIsNone(v(""))
        self.assertIsNone(v("   "))
        self.assertIsNone(v("a" * 32))
        self.assertIsNotNone(v("a" * 33))
        self.assertIsNone(v("  " + "a" * 32 + "  "))   # trimmed before counting
        self.assertIsNotNone(v("Discord Dad"))
        self.assertIsNotNone(v("clydeX"))
        self.assertIsNotNone(v("my CLYDE"))
        self.assertIsNone(v("Oscar"))

    def test_payload(self):
        p = clip_watcher.build_payload("**Game**")
        self.assertEqual(p, {"content": "**Game**", "allowed_mentions": {"parse": []}})
        p = clip_watcher.build_payload("**Game**", "  Oscar ")
        self.assertEqual(p["username"], "Oscar")
        self.assertEqual(p["allowed_mentions"], {"parse": []})
        self.assertNotIn("username", clip_watcher.build_payload("x", "   "))

    def test_effective_name_drops_rejected_names(self):
        self.assertEqual(clip_watcher.effective_player_name(clip_watcher.Config(player_name=" Bob ")), "Bob")
        self.assertEqual(clip_watcher.effective_player_name(clip_watcher.Config(player_name="Discord")), "")


class GetDestinationsTests(unittest.TestCase):
    def test_old_config_migrates(self):
        url = "https://discord.com/api/webhooks/1/abc"
        for cfg in (clip_watcher.Config(webhook_url=url), {"webhook_url": url}):
            self.assertEqual(clip_watcher.get_destinations(cfg),
                             [clip_watcher.Destination("My server", url, True)])
        self.assertEqual(clip_watcher.get_destinations(clip_watcher.Config(webhook_url="PASTE_YOUR_URL")), [])
        self.assertEqual(clip_watcher.get_destinations(clip_watcher.Config()), [])

    def test_explicit_list(self):
        cfg = clip_watcher.Config(webhook_url="https://discord.com/api/webhooks/9/old", destinations=[
            {"name": "A", "url": "https://discord.com/api/webhooks/1/a", "enabled": True},
            {"name": "B", "url": "https://discord.com/api/webhooks/2/b", "enabled": False},
            {"name": "Bad", "url": "https://example.com/nope", "enabled": True},
            {"name": "  C ", "url": "https://discordapp.com/api/webhooks/3/c"},
        ])
        with self.assertLogs("clip_watcher", "WARNING") as logs:
            got = clip_watcher.get_destinations(cfg)
        self.assertEqual([(d.name, d.enabled) for d in got], [("A", True), ("B", False), ("C", True)])
        self.assertNotIn("example.com", "\n".join(logs.output))
        self.assertEqual([d.name for d in clip_watcher.enabled_destinations(cfg)], ["A", "C"])

    def test_empty_list_is_authoritative(self):
        cfg = clip_watcher.Config(webhook_url="https://discord.com/api/webhooks/9/old", destinations=[])
        self.assertEqual(clip_watcher.get_destinations(cfg), [])

    def test_load_config_keeps_the_difference(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            p.write_text(json.dumps({"webhook_url": "https://discord.com/api/webhooks/1/a"}))
            self.assertIsNone(clip_watcher.load_config(p, False).destinations)
            p.write_text(json.dumps({"destinations": []}))
            self.assertEqual(clip_watcher.load_config(p, False).destinations, [])


class PostDecisionTests(unittest.TestCase):
    def test_decision(self):
        ok = clip_watcher.PostResult("A", True)
        bad = clip_watcher.PostResult("Friends #clips", False, "Discord said no (HTTP 404)")
        self.assertTrue(clip_watcher.all_posted([ok, ok]))
        self.assertFalse(clip_watcher.all_posted([ok, bad]))
        self.assertFalse(clip_watcher.all_posted([]))
        self.assertIsNone(clip_watcher.failure_notice([ok]))
        title, msg = clip_watcher.failure_notice([ok, bad])
        self.assertEqual(title, "Clip not posted everywhere")
        self.assertEqual(msg, "Not posted to Friends #clips: Discord said no (HTTP 404).")


class QualityTests(unittest.TestCase):
    def test_low_quality(self):
        f = app.is_low_quality
        self.assertFalse(f(30, 10, 720))
        self.assertTrue(f(90, 10, 720))
        self.assertTrue(f(30, 10, 1080))
        self.assertFalse(f(30, 50, 1080))
        self.assertEqual(app.min_good_video_kbps(720), 1500)
        self.assertEqual(app.min_good_video_kbps(1080), 3000)
        self.assertGreater(app.min_good_video_kbps(900), 1500)
        self.assertLess(app.min_good_video_kbps(900), 3000)

    def test_custom_option(self):
        opts = app.with_custom(app.CLIP_SECONDS_CHOICES, 25, " seconds")
        self.assertEqual(opts[-1], ("25 seconds (custom)", 25))
        self.assertEqual(app.with_custom(app.CLIP_SECONDS_CHOICES, 30.0, " seconds"), app.CLIP_SECONDS_CHOICES)


class ValidateDestinationTests(unittest.TestCase):
    def test_rules(self):
        v = app.validate_destination
        url = "https://discord.com/api/webhooks/1/abc"
        self.assertIsNone(v("  Friends  ", url, []))
        self.assertIn("name", v("   ", url, []).lower())
        self.assertIsNone(v("x" * 40, url, []))
        self.assertIsNotNone(v("x" * 41, url, []))
        self.assertIsNotNone(v("A", "https://example.com/x", []))
        self.assertEqual(v("A", url, [url]), "That webhook is already in your list.")
        self.assertEqual(v("A", url + " ", [url]), "That webhook is already in your list.")


class MergeConfigTests(unittest.TestCase):
    def test_new_keys_and_migration(self):
        old = {"webhook_url": "https://discord.com/api/webhooks/1/a", "custom": [1], "watch_dir": "V"}
        m = app.merge_config(old, [DEST_A, clip_watcher.Destination("Off", DEST_B.url, False)], True,
                             player_name=" Oscar ", clip_seconds=60, max_upload_mb=50, height=1080, fps=30)
        self.assertNotIn("webhook_url", m)
        self.assertEqual(m["destinations"], [
            {"name": DEST_A.name, "url": DEST_A.url, "enabled": True},
            {"name": "Off", "url": DEST_B.url, "enabled": False}])
        self.assertEqual(m["player_name"], "Oscar")
        self.assertEqual((m["clip_seconds"], m["max_upload_mb"], m["height"], m["fps"]), (60, 50, 1080, 30))
        self.assertEqual(m["custom"], [1])
        self.assertEqual(m["watch_dir"], "V")
        self.assertIn("webhook_url", old)  # input untouched
        # unset clips values keep what the file had
        m2 = app.merge_config({"clip_seconds": 25}, [DEST_A], True)
        self.assertEqual(m2["clip_seconds"], 25)
        self.assertNotIn("height", m2)

    def test_save_destinations_only(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.json"
            p.write_text(json.dumps({"webhook_url": DEST_A.url, "clip_seconds": 25, "x": 1}))
            app.save_destinations(p, [clip_watcher.Destination("My server", DEST_A.url, False)])
            data = json.loads(p.read_text())
            self.assertNotIn("webhook_url", data)
            self.assertEqual((data["clip_seconds"], data["x"]), (25, 1))
            self.assertEqual(data["destinations"][0]["enabled"], False)
            self.assertTrue(app.has_destinations(p))


class MockWebhook(http.server.BaseHTTPRequestHandler):
    """Per-server script: `codes` is the list of status codes for successive requests (last repeats)."""
    server_version = "Mock"

    def log_message(self, *a):
        pass

    def do_POST(self):
        srv = self.server
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        with srv.lock:
            code = srv.codes[min(srv.requests, len(srv.codes) - 1)]
            srv.requests += 1
            if code == 200:
                if "multipart" in self.headers.get("Content-Type", ""):
                    body = re.search(rb'name="payload_json".*?\r\n\r\n(.*?)\r\n--', body, re.S).group(1)
                srv.posts.append(json.loads(body))
        payload = (b'{"id":"1","attachments":[{"id":"2"}]}' if code == 200
                   else b'{"message":"Unknown Webhook"}' if code == 404 else b"")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def start_mock(*codes):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MockWebhook)
    srv.codes, srv.requests, srv.posts, srv.lock = list(codes), 0, [], threading.Lock()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.url = f"http://127.0.0.1:{srv.server_port}/api/webhooks/1/SECRETTOKEN"
    return srv


class MultiDestinationPostTests(unittest.TestCase):
    def setUp(self):
        self.tools = clip_watcher.find_ffmpeg(None)
        if self.tools is None or not SYNTH_CLIP.is_file():
            self.skipTest("ffmpeg or the synthetic clip is not available")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.src = self.dir / SYNTH_CLIP.name
        shutil.copyfile(SYNTH_CLIP, self.src)
        self.servers = []
        for patcher in (mock.patch.object(clip_watcher, "TMP_DIR", self.dir / "tmp"),
                        mock.patch.object(clip_watcher, "webhook_is_valid", return_value=True),
                        mock.patch.object(clip_watcher, "RETRY_WAITS", (0.01, 0.01, 0.01))):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.notify = mock.patch.object(clip_watcher, "notify").start()
        self.addCleanup(mock.patch.stopall)

    def _server(self, *codes):
        srv = start_mock(*codes)
        self.servers.append(srv)
        self.addCleanup(srv.shutdown)
        return srv

    def _run(self, dests, **kw):
        cfg = clip_watcher.Config(watch_dir=str(self.dir), clip_seconds=5, height=360, fps=30,
                                  player_name="Oscar", delete_original_after_upload=True,
                                  destinations=[{"name": n, "url": u, "enabled": e} for n, u, e in dests], **kw)
        return clip_watcher.process_clip(cfg, self.tools, self.src, dry_run=False)

    def test_all_succeed_and_original_deleted(self):
        a, b = self._server(200), self._server(200)
        self.assertTrue(self._run([("A", a.url, True), ("B", b.url, True)]))
        for srv in (a, b):
            self.assertEqual(srv.requests, 1)
            self.assertEqual(srv.posts[0]["username"], "Oscar")
            self.assertEqual(srv.posts[0]["allowed_mentions"], {"parse": []})
        self.assertFalse(self.src.exists())
        self.notify.assert_not_called()

    def test_one_fails_other_still_posted_original_kept(self):
        bad, good = self._server(404), self._server(200)
        self.assertFalse(self._run([("Friends #clips", bad.url, True), ("Work", good.url, True)]))
        self.assertEqual(good.requests, 1)
        self.assertEqual(len(good.posts), 1)
        self.assertEqual(bad.requests, 1)
        self.assertTrue(self.src.exists())
        self.notify.assert_called_once()
        title, msg = self.notify.call_args.args
        self.assertEqual(title, "Clip not posted everywhere")
        self.assertIn("Friends #clips", msg)
        self.assertIn("404", msg)
        self.assertNotIn("Work", msg)
        for secret in ("SECRETTOKEN", "127.0.0.1", "http"):
            self.assertNotIn(secret, msg)

    def test_retry_does_not_repost_elsewhere(self):
        flaky, steady = self._server(503, 200), self._server(200)
        self.assertTrue(self._run([("A", flaky.url, True), ("B", steady.url, True)]))
        self.assertEqual(flaky.requests, 2)
        self.assertEqual(len(flaky.posts), 1)
        self.assertEqual((steady.requests, len(steady.posts)), (1, 1))
        self.assertFalse(self.src.exists())

    def test_disabled_destination_gets_nothing(self):
        on, off = self._server(200), self._server(200)
        self.assertTrue(self._run([("On", on.url, True), ("Off", off.url, False)]))
        self.assertEqual(on.requests, 1)
        self.assertEqual(off.requests, 0)

    def test_too_large_reencodes_once_for_everyone(self):
        big, other = self._server(413, 200), self._server(200)
        with mock.patch.object(clip_watcher, "encode", wraps=clip_watcher.encode) as enc:
            self.assertTrue(self._run([("A", big.url, True), ("B", other.url, True)]))
        self.assertEqual(enc.call_count, 2)  # first encode + one re-encode
        self.assertEqual((len(big.posts), len(other.posts)), (1, 1))

    def test_script_mode_old_config_dry_run(self):
        # an old config with only webhook_url still loads and counts as one enabled destination
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            p.write_text(json.dumps({"webhook_url": DEST_A.url}))
            cfg = clip_watcher.load_config(p, False)
            self.assertEqual(len(clip_watcher.enabled_destinations(cfg)), 1)


class TrayAndStatusTests(unittest.TestCase):
    def _app(self, d):
        return app.App(app.Paths(Path(d)))

    def test_status_priority(self):
        with tempfile.TemporaryDirectory() as d:
            a = self._app(d)
            a.state = "downloading"
            a.paused.set()
            self.assertEqual(a.status_text, "Downloading video tools…")
            a.state = "setup"
            self.assertEqual(a.status_text, "Needs setup")
            a.state = "idle"
            self.assertEqual(a.status_text, "Paused")
            a.paused.clear()
            self.assertEqual(a.status_text, "Not posting: no destinations on")
            a.state = "running"
            self.assertEqual(a.status_text, "Running")

    def test_post_to_items(self):
        with tempfile.TemporaryDirectory() as d:
            a = self._app(d)
            items = list(a._post_to_items())
            self.assertEqual([i.text for i in items], ["No destinations"])
            self.assertFalse(items[0].enabled)
            a.destinations = (DEST_A, clip_watcher.Destination("Off & on", DEST_B.url, False))
            items = list(a._post_to_items())
            self.assertEqual([i.checked for i in items], [True, False])
            self.assertEqual(items[1].text, "Off && on")

    def test_toggle_saves_and_restarts(self):
        with tempfile.TemporaryDirectory() as d:
            a = self._app(d)
            a.paths.config.write_text(json.dumps({"webhook_url": DEST_A.url, "keep": 1}))
            with mock.patch.object(app.App, "prepare_and_start") as prep:
                a.toggle_destination(DEST_A.url)
            prep.assert_called_once()
            data = json.loads(a.paths.config.read_text())
            self.assertEqual(data["destinations"], [{"name": "My server", "url": DEST_A.url, "enabled": False}])
            self.assertNotIn("webhook_url", data)
            self.assertEqual(data["keep"], 1)
            self.assertFalse(a.destinations[0].enabled)

    def test_nothing_enabled_means_no_watcher(self):
        with tempfile.TemporaryDirectory() as d:
            a = self._app(d)
            a.paths.config.write_text(json.dumps({"destinations": [
                {"name": "A", "url": DEST_A.url, "enabled": False}], "watch_dir": d}))
            with mock.patch.object(app, "find_tools", return_value=(Path("a"), Path("b"))), \
                    mock.patch.object(clip_watcher, "watch") as watch:
                a.start_watcher()
            watch.assert_not_called()
            self.assertEqual(a.state, "idle")
            self.assertIsNone(a.watcher_thread)


class SettingsTabsTests(unittest.TestCase):
    def _open(self, config=None):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        env = mock.patch.dict(os.environ, {app.HOME_ENV: self._tmp.name})
        env.start()
        self.addCleanup(env.stop)
        for name, rv in (("nvidia_detected", True), ("medal_detected", False)):
            p = mock.patch.object(app, name, return_value=rv)
            p.start()
            self.addCleanup(p.stop)
        if config is not None:
            app.default_paths().config.write_text(json.dumps(config))
        a = app.App(app.default_paths())
        try:
            a.create_root()
        except Exception as e:  # noqa: BLE001
            self.skipTest(f"Tk unavailable: {e}")
        a.window = app.SettingsWindow(a)
        for _ in range(3):
            a.root.update()

        def close():
            a.root.destroy()
            a.window = None
            a.root = None
            gc.collect()
        self.addCleanup(close)
        return a

    def test_tabs_and_empty_list(self):
        a = self._open()
        w = a.window
        self.assertEqual([w.nb.tab(t, "text") for t in w.nb.tabs()], ["Setup", "Clips"])
        self.assertEqual(w.empty_label.cget("text"), "No destinations yet. Add one to start posting.")
        self.assertEqual(w.empty_label.winfo_manager(), "pack")
        self.assertEqual(w.dest_holder.winfo_manager(), "")
        self.assertIn("Leave empty", w.name_help.cget("text"))

    def test_save_needs_a_destination(self):
        a = self._open()
        w = a.window
        w.on_save()
        self.assertEqual(w.msg_var.get(), "Add at least one destination.")
        self.assertFalse(app.default_paths().config.exists())

    def test_invalid_name_blocks_save(self):
        a = self._open({"destinations": [{"name": "A", "url": DEST_A.url, "enabled": True}]})
        w = a.window
        self.assertEqual(len(w.destinations), 1)
        w.name_var.set("Discord Dad")
        a.root.update()
        self.assertTrue(w.name_error_var.get())
        self.assertEqual(w.name_error_label.winfo_manager(), "pack")
        self.assertEqual(str(w.save_btn.cget("state")), "disabled")
        before = app.default_paths().config.read_text()
        with mock.patch.object(app.App, "prepare_and_start") as prep:
            w.on_save()
        prep.assert_not_called()
        self.assertEqual(app.default_paths().config.read_text(), before)
        w.name_var.set("Oscar")
        a.root.update()
        self.assertEqual(w.name_error_var.get(), "")
        self.assertEqual(str(w.save_btn.cget("state")), "normal")

    def test_duplicate_url_rejected_by_dialog_validation(self):
        a = self._open({"webhook_url": DEST_A.url})
        w = a.window
        self.assertEqual([d.name for d in w.destinations], ["My server"])
        urls = [d.url for d in w.destinations]
        self.assertEqual(app.validate_destination("Other", DEST_A.url, urls),
                         "That webhook is already in your list.")
        self.assertIsNone(app.validate_destination("Other", DEST_B.url, urls))

    def test_custom_value_shown_and_kept(self):
        a = self._open({"webhook_url": DEST_A.url, "clip_seconds": 25, "max_upload_mb": 50,
                        "height": 1080, "fps": 30})
        w = a.window
        self.assertEqual(w.clip_vars["clip_seconds"].get(), "25 seconds (custom)")
        self.assertEqual(w.clip_vars["max_upload_mb"].get(), "Level 2 (50 MB)")
        self.assertEqual(w.clip_vars["height"].get(), "1080p")
        self.assertEqual(w.clip_vars["fps"].get(), "30 fps")
        self.assertIn("(custom)", w.clip_boxes["clip_seconds"].cget("values")[-1])
        with mock.patch.object(app.App, "prepare_and_start"), mock.patch.object(app, "set_autostart"):
            w.on_save()
        data = json.loads(app.default_paths().config.read_text())
        self.assertEqual((data["clip_seconds"], data["max_upload_mb"], data["height"], data["fps"]),
                         (25, 50, 1080, 30))
        self.assertNotIn("webhook_url", data)
        self.assertEqual(data["destinations"][0]["name"], "My server")

    def test_quality_warning_follows_choices(self):
        a = self._open({"webhook_url": DEST_A.url})
        w = a.window
        self.assertEqual(w.quality_label.winfo_manager(), "")
        w.clip_vars["clip_seconds"].set("90 seconds")
        w._update_quality_warning()
        self.assertEqual(w.quality_label.winfo_manager(), "pack")
        self.assertEqual(w.quality_label.cget("text"),
                         "Low quality at this length and size. A higher boost level helps.")
        w.clip_vars["max_upload_mb"].set("Level 3 (100 MB)")
        w._update_quality_warning()
        self.assertEqual(w.quality_label.winfo_manager(), "")

    def test_many_destinations_scroll(self):
        dests = [{"name": f"D{i}", "url": f"https://discord.com/api/webhooks/{i}/t", "enabled": True}
                 for i in range(1, 8)]
        a = self._open({"destinations": dests})
        w = a.window
        self.assertEqual(w.dest_scroll.winfo_manager(), "pack")
        self.assertEqual(len(w.dest_inner.winfo_children()), 7)
        tall = w.top.winfo_reqheight()
        w.destinations[:] = w.destinations[:2]
        w._rebuild_destinations()
        self.assertEqual(w.dest_scroll.winfo_manager(), "")
        self.assertLess(w.top.winfo_reqheight(), tall)

    def test_test_button_sends_name_and_reports_inline(self):
        srv = start_mock(200)
        self.addCleanup(srv.shutdown)
        a = self._open({"destinations": [{"name": "Chan", "url": DEST_A.url, "enabled": True}]})
        w = a.window
        w.destinations[0] = clip_watcher.Destination("Chan", srv.url, True)
        w.name_var.set("Oscar")
        w.on_test_destination(0)
        deadline = time.time() + 5
        while time.time() < deadline and not srv.posts:
            time.sleep(0.05)
        for _ in range(20):
            a.root.update()
            time.sleep(0.02)
        self.assertEqual(srv.posts[0]["username"], "Oscar")
        self.assertTrue(w.msg_var.get().startswith("Chan: "))
        self.assertIn("Test message sent", w.msg_var.get())


if __name__ == "__main__":
    unittest.main()

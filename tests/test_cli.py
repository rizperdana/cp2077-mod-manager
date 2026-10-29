import json, os, subprocess, sys, tempfile, unittest, zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import cpmm

CPMM = [sys.executable, str(REPO / "cpmm.py")]


def _scratch_game(tmp):
    g = tmp / "game"
    (g / "bin/x64").mkdir(parents=True, exist_ok=True)
    (g / "bin/x64/Cyberpunk2077.exe").write_bytes(b"")
    (g / "mods").mkdir(exist_ok=True)
    (g / "red4ext/logs").mkdir(parents=True, exist_ok=True)
    return g


def _run(args, env_extra=None, stdin=None):
    env = {**os.environ}
    env.pop("CPMM_STATE_DIR", None)
    env.pop("CP2077_GAME", None)
    if env_extra:
        env.update(env_extra)
    r = subprocess.run(CPMM + args, capture_output=True, text=True, env=env, input=stdin)
    return r


class TestNoColor(unittest.TestCase):
    def _no_ansi(self, out):
        self.assertNotIn("\x1b", out)

    def test_no_color_flag(self):
        r = _run(["--no-color", "detect"])
        self._no_ansi(r.stdout)
        self._no_ansi(r.stderr)

    def test_no_color_env(self):
        r = _run(["detect"], env_extra={"NO_COLOR": "1"})
        self._no_ansi(r.stdout)
        self._no_ansi(r.stderr)

    def test_isatty_false(self):
        r = subprocess.run(CPMM + ["detect"], capture_output=True, text=True,
                           env={**os.environ, "NO_COLOR": "", "CPMM_STATE_DIR": "/nonexistent"})
        self._no_ansi(r.stdout)


class TestMigration(unittest.TestCase):
    def test_categories_added(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            state = td / "mods.json"
            settings = td / "settings.json"
            cpmm.DEFAULT_STATE_DIR = td
            cpmm.state_dir.cache_clear() if hasattr(cpmm.state_dir, 'cache_clear') else None
            # create record without category
            rec = {
                "CET 1.38.1 - Game version 2.5 107 1.38.1 2026-10-26T01-28Z ozVyzbb0n": {
                    "name": "CET 1.38.1 - Game version 2.5 107 1.38.1 2026-10-26T01-28Z ozVyzbb0n",
                    "mod_id": "REBUILT-CET", "mod_type": "CET",
                    "installed_at": 1793259000.0, "order": 1,
                    "files": ["bin/x64/version.dll"],
                    "archive_path": "/tmp/x.zip"
                }
            }
            state.write_text(json.dumps(rec), encoding="utf-8")
            settings.write_text(json.dumps({"game_path": "/nonexistent"}), encoding="utf-8")
            # patch path so load reads from tmp
            orig_sd = cpmm.state_dir
            cpmm.state_dir = lambda: td
            try:
                loaded = cpmm.load_state()
                self.assertIn("category", loaded[list(loaded.keys())[0]])
                self.assertEqual(loaded[list(loaded.keys())[0]]["category"], "framework")
            finally:
                cpmm.state_dir = orig_sd


class TestResolve(unittest.TestCase):
    def test_fuzzy(self):
        recs = {
            "CET 1.38.1": {"name": "CET 1.38.1", "mod_id": "CET", "mod_type": "CET", "files": []},
            "RED4ext 1.31.0": {"name": "RED4ext 1.31.0", "mod_id": "RED4", "mod_type": "RED4ext", "files": []},
            "NPCs Gone Wild 4.3.2": {"name": "NPCs Gone Wild 4.3.2", "mod_id": "NGW", "mod_type": "Archive", "files": []},
        }
        self.assertEqual(cpmm.resolve_record("cet 1.38.1", recs), "CET 1.38.1")
        self.assertEqual(cpmm.resolve_record("RED4ext 1.31.0", recs), "RED4ext 1.31.0")
        self.assertEqual(cpmm.resolve_record("gone wild 4.3.2", recs), "NPCs Gone Wild 4.3.2")
        with self.assertRaises(ValueError):
            cpmm.resolve_record("xxxnosuch", recs)


class TestClassify(unittest.TestCase):
    def test_types(self):
        cet = ["bin/x64/plugins/cyber_engine_tweaks/scripts/foo.lua"]
        red4 = ["red4ext/plugins/ArchiveXL/ArchiveXL.dll"]
        reds = ["r6/scripts/foo.reds"]
        tweak = ["r6/tweaks/bar.yaml"]
        axl = ["archive/pc/mod/foo.archive.xl"]
        arch = ["archive/pc/mod/foo.archive"]
        self.assertIn("CET", cpmm._classify(cet))
        self.assertIn("RED4ext", cpmm._classify(red4))
        self.assertIn("redscript", cpmm._classify(reds))
        self.assertIn("TweakXL", cpmm._classify(tweak))
        self.assertIn("ArchiveXL", cpmm._classify(axl))
        self.assertIn("Archive", cpmm._classify(arch))
        combined = cet + red4 + reds
        res = cpmm._classify(combined)
        for t in ("CET", "RED4ext", "redscript"):
            self.assertIn(t, res)


class TestStateAtomic(unittest.TestCase):
    def test_atomic_backup(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            cpmm.DEFAULT_STATE_DIR = td
            orig_sd = cpmm.state_dir
            cpmm.state_dir = lambda: td
            try:
                state_f = td / "mods.json"
                state_f.write_text('{"a": {"name":"a","mod_id":"1","mod_type":"X","installed_at":1.0,"order":1,"files":["f.txt"]}}')
                cpmm.save_state({"b": {"name":"b","mod_id":"2","mod_type":"X","installed_at":2.0,"order":2,"files":[]}})
                self.assertTrue((td / "mods.json.bak").exists())
                self.assertIn("a", state_f.read_text())
                self.assertIn("b", state_f.read_text())
                self.assertFalse((td / "mods.json.tmp").exists())
            finally:
                cpmm.state_dir = orig_sd


class TestDummyCycle(unittest.TestCase):
    def test_install_enable_disable_uninstall(self):
        with tempfile.TemporaryDirectory() as base:
            base = Path(base)
            game = _scratch_game(base)
            state = base / "state"
            state.mkdir()
            # create zip with redscript file
            z = base / "dummy.zip"
            with zipfile.ZipFile(z, "w") as zf:
                zf.writestr("r6/scripts/dummy.reds", "// test")
            env = {"CPMM_STATE_DIR": str(state), "CP2077_GAME": str(game)}
            # install
            r = _run(["install", str(z), "--category", "misc"], env_extra=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("Installed", r.stdout)
            # list shows it
            r = _run(["list"], env_extra=env)
            self.assertIn("dummy", r.stdout)
            # status ok
            r = _run(["check"], env_extra=env)
            self.assertIn("ok", r.stdout)  # no broken
            # disable
            r = _run(["disable", "dummy"], env_extra=env, stdin="y\n")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("Disabled", r.stdout)
            self.assertIn("y/N", r.stdout)  # confirm prompt shown
            # enable
            r = _run(["enable", "dummy"], env_extra=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("Enabled", r.stdout)
            # uninstall with -y
            r = _run(["uninstall", "dummy", "-y"], env_extra=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("Uninstalled", r.stdout)
            # list empty
            r = _run(["list"], env_extra=env)
            self.assertNotIn("dummy", r.stdout.lower())


if __name__ == "__main__":
    unittest.main()

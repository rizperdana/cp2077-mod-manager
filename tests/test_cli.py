import json, os, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cp2077_mod_manager as m

def _reset(d):
    p = Path(d)
    m.STATE_DIR = p
    m.STATE_FILE = p / "mods.json"
    m.SETTINGS_FILE = p / "settings.json"
    return p

def test_load_state_corrupt_falls_back_to_bak():
    d = tempfile.mkdtemp()
    p = _reset(d)
    m.STATE_FILE.write_text("not json{{{", encoding="utf-8")
    bak = p / "mods.json.bak"
    bak.write_text(json.dumps({"a": {"name":"a","mod_id":"1","mod_type":"X","installed_at":1.0,"order":1,"files":["f.txt"]}}), encoding="utf-8")
    st = m.load_state()
    assert "a" in st, st
    assert st["a"].files == ["f.txt"]

def test_load_state_skips_bad_records():
    d = tempfile.mkdtemp()
    p = _reset(d)
    m.STATE_FILE.write_text(json.dumps({
        "good": {"name":"good","mod_id":"1","mod_type":"X","installed_at":1.0,"order":1,"files":["f.txt"]},
        "bad": {"name":"bad","mod_id":"2","mod_type":"X","installed_at":1.0,"order":2,"files":"not-a-list"},
        "bad2": {"name":"","mod_id":"3","mod_type":"X","installed_at":1.0,"order":3,"files":[]},
    }), encoding="utf-8")
    st = m.load_state()
    assert "good" in st
    assert "bad" not in st
    assert "bad2" not in st

def test_save_state_atomic_and_backup():
    d = tempfile.mkdtemp()
    p = _reset(d)
    m.STATE_FILE.write_text('{"old": {}}', encoding="utf-8")
    rec = m.ModRecord(name="n", mod_id="id", mod_type="T", installed_at=1.0, order=1, files=[])
    m.save_state({"n": rec})
    assert (p / "mods.json.bak").exists()
    assert '"old"' in (p / "mods.json.bak").read_text()
    data = json.loads(m.STATE_FILE.read_text())
    assert "n" in data
    assert not (p / "mods.json.tmp").exists()

def test_sort_stable_with_dup_orders():
    items = [
        m.ModRecord(name="b", mod_id="2", mod_type="T", installed_at=1.0, order=9, files=[]),
        m.ModRecord(name="a", mod_id="1", mod_type="T", installed_at=1.0, order=9, files=[]),
    ]
    state = {r.name: r for r in items}
    out = sorted(state.items(), key=lambda x: (x[1].order if x[1].order is not None else 0, x[0]))
    assert [k for k,_ in out] == ["a","b"]

if __name__ == "__main__":
    for fn in [test_load_state_corrupt_falls_back_to_bak, test_load_state_skips_bad_records,
               test_save_state_atomic_and_backup, test_sort_stable_with_dup_orders]:
        fn()
    print("all tests passed")
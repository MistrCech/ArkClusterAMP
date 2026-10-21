"""Rolling restart s odpoctem, start po skupinach, verze modu - python3 -m unittest discover -s tests"""
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "supervisor"))
import arkclustersupervisor as sup  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 0

    def __call__(self):
        return self.t

    def tick(self):
        self.t += 1


class Done:
    def is_alive(self):
        return False

    def join(self, timeout=None):
        pass


class Worker:
    """Restart mapy, ktery trva D sekund falesneho casu."""

    def __init__(self, clock, until):
        self.clock, self.until = clock, until

    def is_alive(self):
        return self.clock.t < self.until

    def join(self, timeout=None):
        self.clock.t = max(self.clock.t, self.until)


class Rcon:
    def __init__(self, server):
        self.server = server

    def command(self, text, **_):
        if not self.server.running:
            raise sup.RconError("mapa nebezi")
        self.server.sup.events.append((self.server.sup.clock.t, self.server.name, text))
        return ""


class Server:
    def __init__(self, sv, name, running=True):
        self.sup, self.name, self.running, self.desired_up = sv, name, running, running
        self.rcon = Rcon(self)

    def start(self, _rates):
        self.sup.events.append((self.sup.clock.t, self.name, "start"))

    def wait_ready(self, timeout, _abort=None):
        self.sup.events.append((self.sup.clock.t, self.name, f"wait {timeout}"))
        return True

    def stop(self, _fixes, _save_timeout, deadline=None):
        self.sup.events.append((self.sup.clock.t, self.name, "stop"))


def make(names, duration=300, batch=1, stopped=()):
    sv = object.__new__(sup.Supervisor)
    sv.cfg = SimpleNamespace(restart_warning="Restart za {minutes} min.", start_batch=batch,
                             start_delay=0, ready_timeout=600, save_timeout=180, mods=[],
                             gus_extra="", hive_cleanup=False, event="off")
    sv.presets, sv.fixes = {"normal": {}}, {}
    sv.state = SimpleNamespace(apply_pending=lambda: "normal", preset="normal", data={})
    sv.stopping, sv._rolling_cancel = threading.Event(), threading.Event()
    sv._rolling_lock, sv.lifecycle = threading.Lock(), threading.Lock()
    sv._base_mods, sv.startup = None, None
    sv.clock = Clock()
    sv._clock, sv._tick = sv.clock, sv.clock.tick
    sv.events, sv.restarts = [], []
    sv.maps = {n: Server(sv, n, n not in stopped) for n in names}
    sv._mod_updates = lambda: []

    def restart_map(server):
        sv.restarts.append((sv.clock.t, server.name))
        sv.events.append((sv.clock.t, server.name, "restart"))
        return Worker(sv.clock, sv.clock.t + duration)

    def spawn(target, *args):
        if getattr(target, "__name__", "") == "restart_map":
            return target(*args)
        target(*args)
        return Done()

    sv.restart_map = restart_map
    sv._spawn = spawn
    return sv


def warnings(sv, name):
    return [(t, int(text.split("za ")[1].split()[0])) for t, n, text in sv.events
            if n == name and text.startswith("Broadcast ")]


class RollingTest(unittest.TestCase):
    def run_rolling(self, sv, minutes):
        self.assertTrue(sv._rolling_lock.acquire(blocking=False))
        with mock.patch.object(sup, "emit"), mock.patch.object(sup, "log"):
            sv._rolling_restart(minutes)
        self.assertFalse(sv._rolling_lock.locked(), "zamek zustal drzeny")

    def check_invariants(self, sv, minutes, duration):
        names = [n for n, s in sv.maps.items() if s.running]
        self.assertEqual([n for _, n in sv.restarts], names)
        for (t1, _), (t2, _) in zip(sv.restarts, sv.restarts[1:]):
            self.assertGreaterEqual(t2, t1 + duration, "dve mapy dole naraz")
        for t_restart, name in sv.restarts:
            told = warnings(sv, name)
            for t, m in told:
                self.assertGreaterEqual(t_restart, t + m * 60, f"{name}: restart driv, nez hlaseno")
            self.assertEqual([m for _, m in told], sorted({m for _, m in told}, reverse=True))
            if minutes >= 1:
                self.assertEqual(told[-1], (t_restart - 60, 1), name)
            else:
                self.assertEqual(told, [])

    def test_invariants_over_durations_and_countdowns(self):
        for minutes in (0, 1, 3, 5, 15, 20):
            for duration in (100, 300, 360, 420, 600, 1000):
                with self.subTest(minutes=minutes, duration=duration):
                    sv = make(["A", "B", "C", "D"], duration)
                    self.run_rolling(sv, minutes)
                    self.check_invariants(sv, minutes, duration)

    def test_schedule_15_minutes(self):
        sv = make(["A", "B", "C"], duration=300)
        self.run_rolling(sv, 15)
        self.assertEqual(warnings(sv, "A"), [(0, 15), (600, 5), (840, 1)])
        # B: 15 min pred planem 900 + 420, restart A (5 min) skonci driv.
        self.assertEqual(warnings(sv, "B"), [(420, 15), (1020, 5), (1260, 1)])
        self.assertEqual(sv.restarts, [(900, "A"), (1320, "B"), (1740, "C")])

    def test_slow_restart_shifts_next_maps(self):
        sv = make(["A", "B", "C"], duration=600)
        self.run_rolling(sv, 15)
        # A dole 10 min: B hlasi posledni minutu az po nabehnuti A.
        self.assertEqual(sv.restarts, [(900, "A"), (1560, "B"), (2220, "C")])
        self.assertEqual(warnings(sv, "B")[-1], (1500, 1))
        # C ma plan 1740, ale A i B se zdrzi - 5 min hlasi az podle posunuteho planu.
        self.assertEqual(warnings(sv, "C"), [(840, 15), (1920, 5), (2160, 1)])

    def test_late_restart_keeps_short_warnings_accurate(self):
        sv = make(["A", "B", "C", "D"], duration=1000)
        self.run_rolling(sv, 20)
        # B dostala 5 min jeste pred koncem pomaleho restartu A, dalsi uz
        # podle namerene delky restartu.
        for t_restart, name in sv.restarts[2:]:
            five = [t for t, m in warnings(sv, name) if m == 5]
            self.assertEqual(t_restart - five[0], 5 * 60, name)

    def test_stopped_maps_are_skipped(self):
        sv = make(["A", "B", "C"], stopped=("B",))
        self.run_rolling(sv, 5)
        self.assertEqual([n for _, n in sv.restarts], ["A", "C"])
        self.assertEqual(warnings(sv, "B"), [])

    def test_map_stopped_by_operator_during_countdown(self):
        sv = make(["A", "B", "C"])
        inner = sv.restart_map

        def restart_map(server):
            sv.maps["B"].desired_up = False
            return inner(server)

        sv.restart_map = restart_map
        self.run_rolling(sv, 15)
        self.assertEqual([n for _, n in sv.restarts], ["A", "C"])
        self.assertEqual(warnings(sv, "B"), [(420, 15)])

    def test_cancel_lets_running_restart_finish(self):
        sv = make(["A", "B", "C"], duration=300)
        inner = sv.restart_map

        def restart_map(server):
            sv._rolling_cancel.set()
            return inner(server)

        sv.restart_map = restart_map
        self.run_rolling(sv, 1)
        self.assertEqual([n for _, n in sv.restarts], ["A"])
        self.assertGreaterEqual(sv.clock.t, 60 + 300, "zamek pusten pred dobehnutim restartu")

    def test_cluster_stop_ends_countdown(self):
        sv = make(["A", "B"])
        sv._tick = lambda: (sv.clock.tick(), sv.stopping.set() if sv.clock.t == 100 else None)
        self.run_rolling(sv, 15)
        self.assertEqual(sv.restarts, [])

    def test_mod_update_restarts_all_together(self):
        sv = make(["A", "B", "C"], stopped=("C",))
        sv._mod_updates = lambda: ["731604991"]
        calls = []
        sv.start_all = lambda servers=None: calls.append([s.name for s in servers])
        self.run_rolling(sv, 15)
        for name in ("A", "B"):
            self.assertEqual(warnings(sv, name), [(0, 15), (600, 5), (840, 1)])
            self.assertIn((900, name, "stop"), sv.events)
        self.assertEqual(calls, [["A", "B"]])
        self.assertEqual(sv.restarts, [])

    def test_second_rolling_is_refused(self):
        sv = make(["A"])
        with mock.patch.object(sup, "log") as log:
            self.assertTrue(sv._rolling_lock.acquire(blocking=False))
            self.assertFalse(sv._start_rolling(5))
            self.assertIn("uz bezi", log.call_args[0][0])

    def test_command_parsing(self):
        sv = make(["A"])
        seen = []
        sv._start_rolling = lambda minutes: seen.append(minutes)
        with mock.patch.object(sup, "log"):
            for raw in ("rolling", "rolling 5", "rolling 0", "rolling -3", "rolling x"):
                sv.handle(raw)
        self.assertEqual(seen, [15, 5, 0, 0])

    def test_warning_text_is_ascii_and_keeps_braces(self):
        sv = make(["A"])
        sv.cfg.restart_warning = "Restart mapy za {minutes} min {hned}."
        with mock.patch.object(sup, "emit"):
            sv._warn([sv.maps["A"]], 5)
        self.assertEqual(sv.events[-1][2], "Broadcast Restart mapy za 5 min {hned}.")


class StartAllTest(unittest.TestCase):
    def setUp(self):
        for name in ("write_shared_config", "patch_gus", "log", "emit"):
            patcher = mock.patch.object(sup, name)
            patcher.start()
            self.addCleanup(patcher.stop)

    def groups(self, sv):
        out, current, last = [], [], None
        for _, name, what in sv.events:
            if what == "start":
                if last == "wait":
                    out.append(current)
                    current = []
                current.append(name)
            last = "start" if what == "start" else "wait"
        return out + [current]

    def test_batch_1_starts_one_by_one_without_api(self):
        sv = make(list("ABCD"))
        sv._start_alone = lambda: self.fail("u Start Batch 1 se nic nezjistuje")
        sv._mods_pending = lambda: []
        sv.start_all()
        self.assertEqual(self.groups(sv), [["A"], ["B"], ["C"], ["D"]])

    def test_batch_3(self):
        sv = make(list("ABCDEFG"), batch=3)
        sv._start_alone = lambda: None
        sv._mods_pending = lambda: []
        sv.start_all()
        self.assertEqual(self.groups(sv), [["A", "B", "C"], ["D", "E", "F"], ["G"]])

    def test_first_alone_then_batches(self):
        sv = make(list("ABCDEFG"), batch=3)
        sv._start_alone = lambda: "mody maji na Workshopu novou verzi"
        sv._mods_pending = lambda: []
        sv.start_all()
        self.assertEqual(self.groups(sv), [["A"], ["B", "C", "D"], ["E", "F", "G"]])

    def test_pending_mods_start_alone_with_long_timeout(self):
        sv = make(list("ABCD"), batch=3)
        sv._start_alone = lambda: None
        pending = [["111"], []]
        sv._mods_pending = lambda: pending.pop(0) if pending else []
        sv.start_all()
        self.assertEqual(self.groups(sv), [["A"], ["B", "C", "D"]])
        self.assertIn((0, "A", f"wait {sv.MOD_INSTALL_TIMEOUT}"), sv.events)

    def test_subset(self):
        sv = make(list("ABC"), batch=3)
        sv._start_alone = lambda: None
        sv._mods_pending = lambda: []
        sv.start_all([sv.maps["A"], sv.maps["C"]])
        self.assertEqual(self.groups(sv), [["A", "C"]])

    def test_stopping_stops_before_next_group(self):
        sv = make(list("ABCD"), batch=2)
        sv._start_alone = lambda: None
        sv._mods_pending = lambda: []
        first_wait = sv.maps["A"].wait_ready

        def wait_ready(timeout, abort=None):
            sv.stopping.set()
            return first_wait(timeout, abort)

        sv.maps["A"].wait_ready = wait_ready
        sv.start_all()
        self.assertEqual(self.groups(sv), [["A", "B"]])


ACF = '''"AppWorkshop"
{
	"appid"		"346110"
	"WorkshopItemsInstalled"
	{
		"719928795"
		{
			"size"		"24214045"
			"timeupdated"		"1561754829"
			"manifest"		"1"
		}
		"1814953878"
		{
			"size"		"688444153"
			"timeupdated"		"1623041781"
			"manifest"		"2"
		}
	}
	"WorkshopItemDetails"
	{
		"719928795"
		{
			"manifest"		"1"
			"timeupdated"		"1561754829"
		}
		"999"
		{
			"manifest"		"3"
			"timeupdated"		"1700000000"
		}
	}
}
'''


class ModUpdatesTest(unittest.TestCase):
    def test_parse_installed_only(self):
        self.assertEqual(sup.workshop_installed(ACF),
                         {"719928795": 1561754829, "1814953878": 1623041781})
        self.assertEqual(sup.workshop_installed(""), {})

    def make(self, mods):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp))
        acf = tmp / "Engine/Binaries/ThirdParty/SteamCMD/Linux/steamapps/workshop/appworkshop_346110.acf"
        acf.parent.mkdir(parents=True)
        acf.write_text(ACF)
        sv = make(["A"])
        del sv._mod_updates
        sv.cfg.game, sv.cfg.mods, sv.cfg.config_dir = tmp, mods, tmp
        return sv

    @unittest.skipIf(sup.IS_WINDOWS or sup.GAME != "ase", "cesta acf pro linuxove ASE")
    def test_newer_or_missing_is_update(self):
        sv = self.make(["719928795", "1814953878", "555"])
        remote = {"719928795": 1561754829, "1814953878": 1700000000, "555": 1}
        with mock.patch.object(sup, "workshop_updated", return_value=remote):
            self.assertEqual(sv._mod_updates(), ["1814953878", "555"])

    @unittest.skipIf(sup.IS_WINDOWS or sup.GAME != "ase", "cesta acf pro linuxove ASE")
    def test_api_failure_is_unknown(self):
        sv = self.make(["719928795"])
        with mock.patch.object(sup, "workshop_updated", side_effect=OSError("timeout")), \
                mock.patch.object(sup, "log"):
            self.assertIsNone(sv._mod_updates())
            self.assertEqual(sv._start_alone(), "aktualizace modu nejde overit")

    def test_start_alone_reasons(self):
        sv = self.make([])
        sv._mod_updates = lambda: []
        self.assertIsNone(sv._start_alone())
        sv.cfg.gus_extra = "AllowCaveBuildingPvE=True"
        self.assertIn("GameUserSettings", sv._start_alone())
        (sv.cfg.config_dir / "GameUserSettings.ini").write_text("[ServerSettings]\n")
        self.assertIsNone(sv._start_alone())
        sv._mod_updates = lambda: ["1"]
        self.assertIn("novou verzi", sv._start_alone())


if __name__ == "__main__":
    unittest.main()

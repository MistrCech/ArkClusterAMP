"""Vyber eventu (resolve_event, event_mods) - python3 -m unittest discover -s tests"""
import contextlib
import datetime
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "supervisor"))
import arkclustersupervisor as sup  # noqa: E402

EVENTS = {
    "events": {"asa": {"fear": {"name": "Fear Ascended", "mods": ["877752"], "args": ["-HalloweenColors"]},
                       "winter": {"name": "Winter Wonderland", "mods": ["927090"]},
                       "anniversary": {"name": "Anniversary", "mods": [], "args": ["-ActiveEvent=Birthday"]},
                       "nomod": {"name": "Bez modu", "mods": []}},
               "ase": {"winter": {"name": "Winter Wonderland", "mods": [], "args": ["-ActiveEvent=WinterWonderland"]},
                       "fear": {"name": "Fear Evolved", "mods": [], "args": ["-ActiveEvent=FearEvolved"]}}},
    "calendar": [{"event": "fear", "start": "2026-10-20", "end": "2026-11-03"},
                 {"game": "ase", "event": "winter", "start": "2026-12-18", "end": "2027-01-05"}],
}
D = datetime.date


class ResolveTest(unittest.TestCase):
    def test_off(self):
        for choice in ("off", "", None, "OFF"):
            self.assertEqual(sup.resolve_event(choice, EVENTS, D(2026, 10, 25), "asa")[0], None)

    def test_manual(self):
        key, entry = sup.resolve_event("fear", EVENTS, D(2026, 1, 1), "asa")
        self.assertEqual((key, entry["mods"]), ("fear", ["877752"]))

    def test_official_inside_and_on_both_edges(self):
        for day in (D(2026, 10, 20), D(2026, 10, 25), D(2026, 11, 3)):
            self.assertEqual(sup.resolve_event("official", EVENTS, day, "asa")[0], "fear")

    def test_official_outside(self):
        for day in (D(2026, 10, 19), D(2026, 11, 4)):
            self.assertEqual(sup.resolve_event("official", EVENTS, day, "asa")[0], None)

    def test_unknown_and_missing_mod(self):
        self.assertIsNone(sup.resolve_event("easter", EVENTS, D(2026, 1, 1), "asa")[0])
        self.assertIsNone(sup.resolve_event("nomod", EVENTS, D(2026, 1, 1), "asa")[0])

    def test_other_game_has_no_events(self):
        self.assertIsNone(sup.resolve_event("fear", EVENTS, D(2026, 10, 25), "unknown")[0])

    def test_ase_calendar_ignores_asa_entries(self):
        # zaznam bez "game" je ASA: v terminu ASA fear ASE nic nezapne
        self.assertIsNone(sup.resolve_event("official", EVENTS, D(2026, 10, 25), "ase")[0])
        self.assertIsNone(sup.resolve_event("official", EVENTS, D(2026, 12, 20), "asa")[0])
        for day in (D(2026, 12, 18), D(2027, 1, 1), D(2027, 1, 5)):
            self.assertEqual(sup.resolve_event("official", EVENTS, day, "ase")[0], "winter")
        self.assertIsNone(sup.resolve_event("official", EVENTS, D(2027, 1, 6), "ase")[0])

    def test_ase_manual(self):
        key, entry = sup.resolve_event("fear", EVENTS, D(2026, 1, 1), "ase")
        self.assertEqual((key, entry["args"]), ("fear", ["-ActiveEvent=FearEvolved"]))


class PlanTest(unittest.TestCase):
    BASE = ["947033", "942339"]

    def plan(self, choice, day=D(2026, 10, 25), seen=()):
        return sup.plan_event(choice, EVENTS, day, list(seen), self.BASE, "asa")

    def test_off_and_nothing_seen_changes_nothing(self):
        p = self.plan("off")
        self.assertEqual((p["mods"], p["passive"], p["args"], p["active"]), (self.BASE, [], [], None))

    def test_active_event_adds_mod_and_args(self):
        p = self.plan("fear")
        self.assertEqual(p["mods"], self.BASE + ["877752"])
        self.assertEqual((p["passive"], p["args"], p["seen"]), ([], ["-HalloweenColors"], ["fear"]))

    def test_after_event_mod_stays_passive_and_first(self):
        # Wildcard 33.15: -mods=927090,ModID -passivemods=927090 (pasivni prvni).
        p = self.plan("off", seen=["fear"])
        self.assertEqual(p["mods"], ["877752"] + self.BASE)
        self.assertEqual((p["passive"], p["args"]), (["877752"], []))

    def test_next_event_while_previous_is_passive(self):
        p = self.plan("winter", seen=["fear"])
        self.assertEqual(p["mods"], ["877752"] + self.BASE + ["927090"])
        self.assertEqual((p["passive"], p["seen"]), (["877752"], ["fear", "winter"]))

    def test_event_again_is_not_passive(self):
        p = self.plan("fear", seen=["fear", "winter"])
        self.assertEqual(p["passive"], ["927090"])
        self.assertNotIn("877752", p["passive"])
        self.assertEqual(p["mods"].count("877752"), 1)

    def test_args_only_event(self):
        p = self.plan("anniversary")
        self.assertEqual((p["mods"], p["args"], p["active"]), (self.BASE, ["-ActiveEvent=Birthday"], "anniversary"))

    def test_ase_event_is_args_only(self):
        p = sup.plan_event("official", EVENTS, D(2026, 12, 24), [], self.BASE, "ase")
        self.assertEqual((p["mods"], p["passive"], p["args"], p["active"]),
                         (self.BASE, [], ["-ActiveEvent=WinterWonderland"], "winter"))
        p = sup.plan_event("official", EVENTS, D(2027, 1, 10), ["winter"], self.BASE, "ase")
        self.assertEqual((p["mods"], p["passive"], p["args"], p["active"]), (self.BASE, [], [], None))

    def test_mod_already_in_base_list(self):
        p = sup.plan_event("off", EVENTS, D(2026, 1, 1), ["fear"], ["877752", "947033"], "asa")
        self.assertEqual(p["mods"], ["877752", "947033"])
        self.assertEqual(p["passive"], ["877752"])


BOOST_SPEC = {"factor": 2.0,
              "cmdline": {"XPMultiplier": "up", "TamingSpeedMultiplier": "up", "HarvestAmountMultiplier": "up"},
              "gameini": {"EggHatchSpeedMultiplier": "up", "BabyMatureSpeedMultiplier": "up",
                          "MatingIntervalMultiplier": "down", "LayEggIntervalMultiplier": "down",
                          "BabyCuddleIntervalMultiplier": "down"}}
BOOSTED = dict(EVENTS, rate_boost={"ase": BOOST_SPEC},
               events={"asa": EVENTS["events"]["asa"],
                       "ase": dict(EVENTS["events"]["ase"],
                                   quiet={"name": "Bez ratu", "mods": [], "args": ["-ActiveEvent=Summer"],
                                          "rate_boost": False},
                                   triple={"name": "3x", "mods": [], "args": ["-ActiveEvent=Easter"],
                                           "rate_boost": 3},
                                   typo={"name": "Preklep", "mods": [], "args": ["-ActiveEvent=vday"],
                                         "rate_boost": 20})})
# Ostra 23. 11. 2026: preset 5x, XP a Harvest z Extra Launch Options na 1.0, chov v Game.ini z presetu,
# BabyFood z pole instance a za nim sekce SS.
LIVE_CUSTOM = "XPMultiplier=1.0?HarvestAmountMultiplier=1.0?DinoCharacterFoodDrainMultiplier=0.5?ItemStackSizeMultiplier=5.0"
LIVE_GAMEINI_EXTRA = "BabyFoodConsumptionSpeedMultiplier=1.0\n[StructuresPlus]\nSomeSSKey=True"


def fake_map(boost, custom=LIVE_CUSTOM):
    """Jen to, co MapServer.command_line cte."""
    cfg = types.SimpleNamespace(
        rcon_password="x", max_players=70, multihome=False, server_password="", server_settings=[],
        custom_options=custom, mods=[], session_name="Test", binary=Path("ShooterGameServer"),
        cluster_dir=Path("c"), cluster_id="t", passive_mods=[], event_args=[], custom_args="",
        bind_ip="0.0.0.0", rate_boost=boost)
    return types.SimpleNamespace(name="TheIsland", ports={"game": 7777, "query": 27015, "rcon": 27020},
                                 cfg=cfg, MANAGED_OPTIONS=sup.MapServer.MANAGED_OPTIONS)


def url_options(args):
    opts = {}
    for part in args[1].split("?")[1:]:
        key, _, value = part.partition("=")
        opts.setdefault(key, []).append(value)
    return opts


class RateBoostTest(unittest.TestCase):
    PRESETS = json.loads((ROOT / "supervisor/presets.json").read_text(encoding="utf-8"))

    def boost(self, choice="official", day=D(2026, 12, 24)):
        return sup.plan_event(choice, BOOSTED, day, [], [], "ase")["boost"]

    def test_plan(self):
        self.assertEqual(self.boost(), BOOST_SPEC)
        self.assertIsNone(self.boost(day=D(2027, 1, 10)))          # po eventu
        self.assertIsNone(self.boost("off"))
        self.assertIsNone(self.boost("quiet"))                     # event s "rate_boost": false
        self.assertEqual(self.boost("triple")["factor"], 3.0)
        self.assertIsNone(self.boost("typo"))                      # 20x = preklep
        self.assertIsNone(sup.plan_event("fear", BOOSTED, D(2026, 10, 25), [], [], "asa")["boost"])
        self.assertIsNone(sup.plan_event("official", EVENTS, D(2026, 12, 24), [], [], "ase")["boost"])

    def test_format_rate(self):
        self.assertEqual([sup.format_rate(v) for v in (10.0, 0.2 / 2, 2, 0.05, 1 / 3)],
                         ["10.0", "0.1", "2.0", "0.05", "0.333333"])

    def test_command_line_live(self):
        rates = self.PRESETS["5x"]["cmdline"]
        with contextlib.redirect_stdout(io.StringIO()) as out:
            opts = url_options(sup.MapServer.command_line(fake_map(BOOST_SPEC), rates))
        self.assertEqual(opts["XPMultiplier"], ["2.0"])            # 1.0 z Extra Launch Options, ne 5 z presetu
        self.assertEqual(opts["HarvestAmountMultiplier"], ["2.0"])
        self.assertEqual(opts["TamingSpeedMultiplier"], ["10.0"])
        self.assertEqual(opts["DinoCharacterFoodDrainMultiplier"], ["0.5"])
        self.assertEqual(opts["ItemStackSizeMultiplier"], ["5.0"])
        self.assertIn("Event x2: XPMultiplier 1.0->2.0", out.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            plain = url_options(sup.MapServer.command_line(fake_map(None), rates))
        self.assertEqual((plain["XPMultiplier"], plain["TamingSpeedMultiplier"]), (["1.0"], ["5.0"]))

    def test_command_line_missing_key_is_one(self):
        with contextlib.redirect_stdout(io.StringIO()):
            opts = url_options(sup.MapServer.command_line(fake_map(BOOST_SPEC, custom=""), {}))
        self.assertEqual(opts["XPMultiplier"], ["2.0"])

    def gameini(self, boost, extra=LIVE_GAMEINI_EXTRA, preset="5x"):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = types.SimpleNamespace(config_dir=Path(tmp), gameini_extra=extra, gus_extra="",
                                        rate_boost=boost)
            with contextlib.redirect_stdout(io.StringIO()):
                sup.write_shared_config(cfg, self.PRESETS, preset)
            text = (Path(tmp) / "Game.ini").read_text()
        head, _, tail = text.partition("[StructuresPlus]")
        values = {}
        for line in head.splitlines():
            key, sep, value = line.partition("=")
            if sep and "Multiplier" in key:
                values.setdefault(key.strip(), []).append(value.strip())
        return values, tail

    def test_gameini_live(self):
        values, tail = self.gameini(BOOST_SPEC)
        self.assertEqual(values["EggHatchSpeedMultiplier"], ["10.0"])
        self.assertEqual(values["BabyMatureSpeedMultiplier"], ["10.0"])
        self.assertEqual(values["MatingIntervalMultiplier"], ["0.1"])     # intervaly dolu
        self.assertEqual(values["LayEggIntervalMultiplier"], ["0.1"])
        self.assertEqual(values["BabyCuddleIntervalMultiplier"], ["0.1"])
        self.assertEqual(values["BabyFoodConsumptionSpeedMultiplier"], ["1.0"])
        self.assertNotIn("Multiplier", tail)                                # nic nespadlo do sekce SS
        self.assertIn("SomeSSKey=True", tail)

    def test_gameini_without_event_unchanged(self):
        values, _ = self.gameini(None)
        self.assertEqual(values["MatingIntervalMultiplier"], ["0.2"])
        self.assertEqual(values["EggHatchSpeedMultiplier"], ["5.0"])

    def test_gameini_instance_line_is_the_base(self):
        extra = "MatingIntervalMultiplier=0.5\n" + LIVE_GAMEINI_EXTRA
        values, tail = self.gameini(BOOST_SPEC, extra)
        self.assertEqual(values["MatingIntervalMultiplier"], ["0.25"])    # z radku instance, preset ne
        self.assertNotIn("Multiplier", tail)

    def test_gameini_normal_preset_gets_defaults_times_factor(self):
        values, _ = self.gameini(BOOST_SPEC, "", "normal")
        self.assertEqual(values["MatingIntervalMultiplier"], ["0.5"])
        self.assertEqual(values["BabyMatureSpeedMultiplier"], ["2.0"])


class RepoDataTest(unittest.TestCase):
    """events.json v repu: platny JSON, ASCII (Windows ho cte v kodove strance),
    kazdy event v kalendari existuje a ma mod nebo prepinac, data jsou platna a neprekryvaji se."""

    def setUp(self):
        raw = (ROOT / "supervisor/events.json").read_bytes()
        raw.decode("ascii")
        self.data = json.loads(raw)

    def test_calendar_consistent(self):
        cal = sorted(self.data["calendar"], key=lambda c: (c.get("game", "asa"), c["start"]))
        for c in cal:
            game = c.get("game", "asa")
            entry = self.data["events"][game][c["event"]]
            self.assertTrue(sup._event_mod_ids(entry) or entry.get("args"), c)
            start, end = D.fromisoformat(c["start"]), D.fromisoformat(c["end"])
            self.assertLessEqual(start, end, c)
        for a, b in zip(cal, cal[1:]):
            if a.get("game", "asa") == b.get("game", "asa"):
                self.assertLess(a["end"], b["start"], (a, b))

    def test_template_enum_matches_events(self):
        for game, name in (("asa", "arkasaclusterconfig.json"), ("ase", "arkclusterconfig.json")):
            cfg = json.loads((ROOT / name).read_text(encoding="utf-8"))
            field = next(f for f in cfg if f["FieldName"] == "Event")
            keys = set(field["EnumValues"]) - {"off", "official"}
            self.assertEqual(keys, set(self.data["events"][game]), game)
            self.assertEqual(field["DefaultValue"], "off", game)
            kvp = (ROOT / name.replace("config.json", ".kvp")).read_text(encoding="utf-8")
            self.assertIn('"ARK_EVENT":"{{Event}}"', kvp, game)

    def test_rate_boost_directions(self):
        # Intervaly se pri eventu deli (mensi cislo = casteji), vse ostatni nasobi.
        spec = self.data["rate_boost"]["ase"]
        self.assertEqual(spec["factor"], 2.0)
        for section in ("cmdline", "gameini"):
            for key, direction in spec[section].items():
                self.assertEqual(direction, "down" if "Interval" in key else "up", key)
        self.assertNotIn("BabyFoodConsumptionSpeedMultiplier", spec["gameini"])
        self.assertNotIn("asa", self.data["rate_boost"])

    def test_ase_events_are_active_event_switches(self):
        known = {"WinterWonderland", "FearEvolved", "TurkeyTrial", "Easter", "vday", "Summer", "Birthday"}
        for key, entry in self.data["events"]["ase"].items():
            self.assertEqual(sup._event_mod_ids(entry), [], key)
            (arg,) = entry["args"]
            self.assertTrue(arg.startswith("-ActiveEvent="), key)
            self.assertIn(arg.split("=", 1)[1], known, key)


if __name__ == "__main__":
    unittest.main()

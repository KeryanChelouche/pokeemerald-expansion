#!/usr/bin/env python3
"""Compose campaign/ROUTE.txt: the full run, in the order it is actually played.

Two sources, kept deliberately separate.

  * The ORDER, below, is written by hand. Progression gating lives in story flags
    and script logic that cannot be read off the data, so it is stated explicitly
    and marked where it is uncertain.

  * Everything else -- trainers, encounters, items -- is filled in from
    campaign/extracted.json, which comes from the ROM's own data. Guides
    disagree with each other and with the game; the decomp does not.

The order is the normal playthrough order, NOT the speedrun order. The speedrun
sheet reaches Mauville, Wattson, Mt. Chimney and Flannery before Brawly, because
it skips back and forth; a nuzlocke has to take the gyms in badge order.

    tools/campaign_route.py > campaign/ROUTE.txt
"""

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Each stop: (label, [maps], gate, badge, notes)
#
# `gate` is what must be beaten before the stop opens. UNSURE marks a gate that
# is a judgement call rather than something verified against a flag -- see the
# uncertainty list at the end of the generated file.
ORDER = [
    ("Littleroot Town", ["LITTLEROOT_TOWN"], None, None,
     "Starter chosen here. The tutorial Zigzagoon battle is skipped by the "
     "harness (set_party injects the team; see spec-deltas)."),
    ("Route 101", ["ROUTE101"], None, None, ""),
    ("Oldale Town", ["OLDALE_TOWN"], None, None, "No encounters. Shop opens."),
    ("Route 103", ["ROUTE103"], None, None,
     "Rival waits here. The encounter does not require beating them."),
    ("Route 102", ["ROUTE102"], "rival_route_103", None, ""),
    ("Petalburg City", ["PETALBURG_CITY"], "rival_route_103", None,
     "Norman refuses the gym until four badges (Emerald). Wally tutorial is a "
     "scripted catch, not a battle."),
    ("Route 104 (south)", ["ROUTE104"], "rival_route_103", None,
     "UNSURE: Route 104 is one map for north and south halves. The southern "
     "half is reachable before Petalburg Woods, the northern half after. Split "
     "by hand if the encounter should differ."),
    ("Petalburg Woods", ["PETALBURG_WOODS"], "route_104_reached", None,
     "Aqua Grunt here is mandatory (plot: Devon researcher)."),
    ("Rustboro City", ["RUSTBORO_CITY"], "aqua_petalburg_woods", None, ""),
    ("Rustboro Gym", ["RUSTBORO_CITY_GYM"], "aqua_petalburg_woods", "STONE",
     "GYM 1 -- Roxanne. Gives TM39 Rock Tomb."),
    ("Route 116", ["ROUTE116"], "gym_roxanne", None, ""),
    ("Rusturf Tunnel", ["RUSTURF_TUNNEL"], "gym_roxanne", None,
     "Aqua Grunt with the stolen Devon Goods -- mandatory plot fight."),
    ("Rustboro City (rival 2)", ["RUSTBORO_CITY"], "rusturf_grunt", None,
     "UNSURE: the second rival fight triggers outside Rustboro after returning "
     "the Devon Goods. Listed as its own stop because it gates the boat."),
    ("Route 105", ["ROUTE105"], "gym_brawly", None,
     "Requires Surf -- listed here only for completeness; reachable later."),
    ("Route 106", ["ROUTE106"], "rival_rustboro", None, "Reached by Mr Briney's boat."),
    ("Granite Cave", ["GRANITE_CAVE_1F", "GRANITE_CAVE_B1F", "GRANITE_CAVE_B2F",
                      "GRANITE_CAVE_STEVENS_ROOM"], "rival_rustboro", None,
     "Steven gives TM47 Steel Wing. B1F/B2F need Flash for full access."),
    ("Dewford Town", ["DEWFORD_TOWN"], "rival_rustboro", None, ""),
    ("Dewford Gym", ["DEWFORD_TOWN_GYM"], "rival_rustboro", "KNUCKLE",
     "GYM 2 -- Brawly. Gives TM08 Bulk Up."),
    ("Route 107", ["ROUTE107"], "gym_brawly", None, "Water route; Surf/boat."),
    ("Route 108", ["ROUTE108"], "gym_brawly", None, ""),
    ("Abandoned Ship", ["ABANDONED_SHIP_ROOMS_1F", "ABANDONED_SHIP_ROOMS_B1F",
                        "ABANDONED_SHIP_ROOMS2_B1F", "ABANDONED_SHIP_ROOM_B1F",
                        "ABANDONED_SHIP_ROOMS2_1F", "ABANDONED_SHIP_CORRIDORS_1F",
                        "ABANDONED_SHIP_CORRIDORS_B1F", "ABANDONED_SHIP_DECK"],
     "gym_brawly", None, "Optional. Needs Surf and Dive for the full interior."),
    ("Route 109", ["ROUTE109"], "gym_brawly", None, ""),
    ("Slateport City", ["SLATEPORT_CITY"], "gym_brawly", None,
     "Museum Aqua Grunts are mandatory plot fights."),
    ("Route 110", ["ROUTE110"], "slateport_museum", None,
     "Rival 3 fights here. Trick House sits on this route."),
    ("Mauville City", ["MAUVILLE_CITY"], "rival_route_110", None,
     "Wally battles here (PKMN Trainer Wally)."),
    ("Mauville Gym", ["MAUVILLE_CITY_GYM"], "rival_route_110", "DYNAMO",
     "GYM 3 -- Wattson. Gives TM34 Shock Wave."),
    ("Route 117", ["ROUTE117"], "gym_wattson", None, "Day care route."),
    ("Verdanturf Town", ["VERDANTURF_TOWN"], "gym_wattson", None, "No encounters."),
    ("Route 111 (south)", ["ROUTE111"], "gym_wattson", None,
     "UNSURE: Route 111 spans south desert and north. The desert needs Go "
     "Goggles (obtained after Mt. Chimney), so its encounters unlock later "
     "than the southern trainers."),
    ("Route 112", ["ROUTE112"], "gym_wattson", None, ""),
    ("Fiery Path", ["FIERY_PATH"], "gym_wattson", None,
     "Full access needs Strength; encounters reachable on first pass."),
    ("Route 113", ["ROUTE113"], "gym_wattson", None, ""),
    ("Fallarbor Town", ["FALLARBOR_TOWN"], "gym_wattson", None, "No encounters."),
    ("Route 114", ["ROUTE114"], "gym_wattson", None, ""),
    ("Meteor Falls", ["METEOR_FALLS_1F_1R", "METEOR_FALLS_1F_2R",
                      "METEOR_FALLS_B1F_1R", "METEOR_FALLS_B1F_2R"],
     "gym_wattson", None, "Deeper rooms need Surf and Waterfall."),
    ("Route 115", ["ROUTE115"], "gym_wattson", None, ""),
    ("Jagged Pass", ["JAGGED_PASS"], "meteor_falls_magma", None,
     "UNSURE: reached from Route 112 via the Cable Car area."),
    ("Mt. Chimney", ["MT_CHIMNEY"], "jagged_pass", None,
     "Magma Admin Tabitha then Magma Leader Maxie -- mandatory plot fights."),
    ("Route 111 (desert)", ["ROUTE111"], "mt_chimney", None,
     "Go Goggles obtained after Mt. Chimney open the desert."),
    ("Lavaridge Town", ["LAVARIDGE_TOWN"], "mt_chimney", None, ""),
    ("Lavaridge Gym", ["LAVARIDGE_TOWN_GYM_1F", "LAVARIDGE_TOWN_GYM_B1F"],
     "mt_chimney", "HEAT", "GYM 4 -- Flannery. Gives TM50 Overheat."),
    ("Petalburg Gym", ["PETALBURG_CITY_GYM"], "gym_flannery", "BALANCE",
     "GYM 5 -- Norman. Opens only at four badges. Gives TM42 Facade."),
    ("Route 118", ["ROUTE118"], "gym_norman", None, "Needs Surf to cross fully."),
    ("Route 119", ["ROUTE119"], "gym_norman", None,
     "Weather Institute (Aqua) and rival 4 are here."),
    ("Weather Institute", ["ROUTE119_WEATHER_INSTITUTE_1F", "ROUTE119_WEATHER_INSTITUTE_2F"],
     "gym_norman", None, "Aqua Admin Shelly -- mandatory. Gives Castform."),
    ("Fortree City", ["FORTREE_CITY"], "weather_institute", None, ""),
    ("Fortree Gym", ["FORTREE_CITY_GYM"], "weather_institute", "FEATHER",
     "GYM 6 -- Winona. Gives TM40 Aerial Ace."),
    ("Route 120", ["ROUTE120"], "gym_winona", None, ""),
    ("Route 121", ["ROUTE121"], "gym_winona", None, ""),
    ("Safari Zone", ["SAFARI_ZONE_SOUTH", "SAFARI_ZONE_NORTH",
                     "SAFARI_ZONE_SOUTHEAST", "SAFARI_ZONE_NORTHEAST"],
     "gym_winona", None,
     "UNSURE: Safari Zone catching bypasses normal battle mechanics entirely. "
     "Probably needs its own rule or exclusion -- flagged, not decided."),
    ("Mt. Pyre", ["MT_PYRE_1F", "MT_PYRE_2F", "MT_PYRE_3F", "MT_PYRE_4F",
                  "MT_PYRE_5F", "MT_PYRE_6F", "MT_PYRE_EXTERIOR",
                  "MT_PYRE_SUMMIT"], "gym_winona", None, ""),
    ("Route 122", ["ROUTE122"], "gym_winona", None, ""),
    ("Route 123", ["ROUTE123"], "gym_winona", None, ""),
    ("Lilycove City", ["LILYCOVE_CITY"], "mt_pyre", None, "Rival 5 fights here."),
    ("Magma Hideout", ["MAGMA_HIDEOUT_1F", "MAGMA_HIDEOUT_2F_1R",
                       "MAGMA_HIDEOUT_2F_2R", "MAGMA_HIDEOUT_2F_3R",
                       "MAGMA_HIDEOUT_3F_1R", "MAGMA_HIDEOUT_3F_2R",
                       "MAGMA_HIDEOUT_3F_3R", "MAGMA_HIDEOUT_4F"],
     "rival_lilycove", None,
     "Emerald: Magma Hideout is on Jagged Pass. Maxie fight is mandatory."),
    ("Aqua Hideout", ["AQUA_HIDEOUT_1F", "AQUA_HIDEOUT_B1F", "AQUA_HIDEOUT_B2F"],
     "magma_hideout", None, "Aqua Admin Matt -- mandatory."),
    ("Route 124", ["ROUTE124"], "aqua_hideout", None, "Surf."),
    ("Mossdeep City", ["MOSSDEEP_CITY"], "aqua_hideout", None, ""),
    ("Mossdeep Gym", ["MOSSDEEP_CITY_GYM"], "aqua_hideout", "MIND",
     "GYM 7 -- Tate & Liza. DOUBLE battle. Gives TM04 Calm Mind."),
    ("Space Center", ["MOSSDEEP_CITY_SPACE_CENTER_1F",
                      "MOSSDEEP_CITY_SPACE_CENTER_2F"], "gym_tate_liza", None,
     "Magma Admin Tabitha and Maxie -- mandatory."),
    ("Route 125", ["ROUTE125"], "gym_tate_liza", None, ""),
    ("Route 127", ["ROUTE127"], "space_center", None, ""),
    ("Route 128", ["ROUTE128"], "space_center", None, ""),
    ("Seafloor Cavern", ["SEAFLOOR_CAVERN_ENTRANCE", "SEAFLOOR_CAVERN_ROOM1",
                         "SEAFLOOR_CAVERN_ROOM2", "SEAFLOOR_CAVERN_ROOM3",
                         "SEAFLOOR_CAVERN_ROOM4", "SEAFLOOR_CAVERN_ROOM5",
                         "SEAFLOOR_CAVERN_ROOM6", "SEAFLOOR_CAVERN_ROOM7",
                         "SEAFLOOR_CAVERN_ROOM8", "SEAFLOOR_CAVERN_ROOM9"],
     "space_center", None, "Aqua Leader Archie -- mandatory."),
    ("Route 126", ["ROUTE126"], "space_center", None, ""),
    ("Sootopolis City", ["SOOTOPOLIS_CITY"], "seafloor_cavern", None,
     "Rayquaza / Kyogre-Groudon plot. No trainers until the gym opens."),
    ("Sootopolis Gym", ["SOOTOPOLIS_CITY_GYM_1F", "SOOTOPOLIS_CITY_GYM_B1F"],
     "sootopolis_plot", "RAIN", "GYM 8 -- Juan. Gives TM03 Water Pulse."),
    ("Route 129", ["ROUTE129"], "gym_juan", None, ""),
    ("Route 130", ["ROUTE130"], "gym_juan", None, "Mirage Island."),
    ("Route 131", ["ROUTE131"], "gym_juan", None, ""),
    ("Route 132", ["ROUTE132"], "gym_juan", None, ""),
    ("Route 133", ["ROUTE133"], "gym_juan", None, ""),
    ("Route 134", ["ROUTE134"], "gym_juan", None, ""),
    ("Victory Road", ["VICTORY_ROAD_1F", "VICTORY_ROAD_B1F", "VICTORY_ROAD_B2F"],
     "gym_juan", None, "Wally fights at the entrance."),
    ("Pokemon League", ["EVER_GRANDE_CITY_SIDNEYS_ROOM",
                        "EVER_GRANDE_CITY_PHOEBES_ROOM",
                        "EVER_GRANDE_CITY_GLACIAS_ROOM",
                        "EVER_GRANDE_CITY_DRAKES_ROOM",
                        "EVER_GRANDE_CITY_CHAMPIONS_ROOM"],
     "victory_road", None,
     "Elite Four then Champion Wallace. No healing between -- the run ends here."),
]

UNCERTAINTIES = """
==============================================================================
WHAT IS SOLID, WHAT IS NOT
==============================================================================

SOLID -- taken from the ROM's own data, not from a guide
  * Which trainers exist on which map, and whether each fight is single or
    double. Read from the trainerbattle commands in each map's scripts.
  * Every wild encounter table, per map, per method, with level ranges.
  * Every item ball and hidden item, with the exact item.
  Spot-checked against the reference sheet: Route 102 (Calvin, Rick, Tiana,
  Allen), Petalburg Woods (Lyle, Aqua Grunt, James) and Rustboro Gym (Josh,
  Tommy, Marc, Roxanne) all match exactly.

NOT SOLID -- written by hand, needs your judgement
  1. THE GATES. Every "gate:" line is my reconstruction of progression from
     playing the game, not something verified against the story flags. The
     badge order is certainly right; which specific fight opens which route is
     the weak part. Lines marked UNSURE are the ones I would check first.

  2. ONE MAP, TWO AREAS. Route 104 (south before the Woods, north after) and
     Route 111 (south now, desert only once the Go Goggles exist) are each a
     single map with a single encounter table. R2 gives one encounter per
     location, so this needs a ruling: is Route 104 one encounter or two?

  3. REVISITS. Rustboro appears twice (gym, then the rival fight after the
     Devon Goods). Same question as above -- does re-entering a location offer
     a second encounter? I assumed no.

  4. ITEMS ARE INCOMPLETE. Only item balls and hidden items are listed, because
     only those are structured data. NOT included, and all significant:
       - TMs given by gym leaders (TM39 from Roxanne, TM08 from Brawly, ...)
       - every HM, which is what actually gates the map
       - gift items (Wailmer Pail, Devon Goods, Go Goggles, bicycle, Castform)
       - anything bought in a shop
     These come from dialogue scripts. Extractable with more work, but I did
     not want to guess at them.

  5. CAPABILITY UNLOCKS ARE NOT MAPPED. The route says "needs Surf" in places
     but nothing records WHERE Surf is obtained. Since §7.2 filters draws by
     capability, this matters more than the item list does. My recollection,
     to be checked: Cut (Rustboro cutter's house), Flash (Granite Cave man),
     Rock Smash (Mauville), Strength (Rusturf Tunnel), Surf (Wally's father,
     Petalburg, after Norman), Fly (Route 119 after the Weather Institute),
     Dive (Mossdeep after badge 7), Waterfall (Sootopolis).

  6. SAFARI ZONE. Catching there bypasses normal battle mechanics completely --
     no damage, no status, Safari Balls only. It needs either its own rule or
     an exclusion. Flagged, not decided.

  7. STATIC AND SPECIAL ENCOUNTERS are absent, because they are not in the wild
     tables: the legendaries, Kecleon, Sudowoodo, Voltorb, the roaming Latias
     or Latios, Mirage Island, Marine/Terra Cave. Each needs a decision about
     whether it counts as the location's encounter.

  8. TRICK HOUSE has eight puzzles that open progressively across the game. The
     trainers are listed but the unlock order is not modelled.

  9. THE ELITE FOUR is one continuous run with no healing between rooms. The
     table lists the five rooms as one stop but does not encode that rule.

 10. LEVEL CAPS are not here at all. If the run uses a per-badge cap, it has to
     be added -- it is a rule, not map data.

==============================================================================
"""

METHOD_LABEL = {
    "land_mons": "grass",
    "water_mons": "surf",
    "rock_smash_mons": "rock_smash",
    "fishing_mons": "fishing",
}

# Which fights are plot-mandatory. Everything else on a route is optional, which
# matters for a nuzlocke: optional trainers are free EXP but also free risk.
MANDATORY_HINT = ("BRENDAN", "MAY", "ROXANNE", "BRAWLY", "WATTSON", "FLANNERY",
                  "NORMAN", "WINONA", "TATE", "LIZA", "JUAN", "WALLY",
                  "ARCHIE", "MAXIE", "TABITHA", "SHELLY", "MATT",
                  "SIDNEY", "PHOEBE", "GLACIA", "DRAKE", "WALLACE", "STEVEN")


def is_mandatory(tid: str) -> bool:
    return any(h in tid for h in MANDATORY_HINT) or "GRUNT" in tid


ALL_MAP_IDS: set[str] = set()


def load_all_map_ids() -> set[str]:
    import json as _json
    out = set()
    for p in (ROOT / "data/maps").glob("*/map.json"):
        try:
            out.add(_json.loads(p.read_text()).get("id", "").replace("MAP_", ""))
        except Exception:
            pass
    return out


def main() -> int:
    global ALL_MAP_IDS
    ALL_MAP_IDS = load_all_map_ids()
    data = json.loads((ROOT / "campaign/extracted.json").read_text())
    w = sys.stdout.write

    w("=" * 78 + "\n")
    w("EMERALD NUZLOCKE -- FULL CAMPAIGN ROUTE (FIRST DRAFT)\n")
    w("=" * 78 + "\n\n")
    w("Trainers, encounters and items are extracted from the decomp itself\n")
    w("(tools/campaign_extract.py), so they are the game's own data, not a\n")
    w("guide's transcription. The ORDER and the GATES are written by hand and\n")
    w("are where the uncertainty lives -- see the end of this file.\n\n")
    w("This is normal playthrough order, not speedrun order: the reference\n")
    w("sheet reaches Wattson, Mt. Chimney and Flannery before Brawly, which a\n")
    w("badge-ordered nuzlocke cannot do.\n\n")
    w("Legend:  [M] mandatory plot fight   [o] optional trainer\n")
    w("         Encounters list one line per method, deduped, with level range.\n\n")

    seen_trainers: set[str] = set()
    n_stops = n_tr = n_items = 0

    for label, maps, gate, badge, note in ORDER:
        n_stops += 1
        w("-" * 78 + "\n")
        head = label
        if badge:
            head += f"   >>> {badge} BADGE <<<"
        w(head + "\n")
        w(f"  gate: {gate or '(open from the start)'}\n")
        if note:
            for i in range(0, len(note), 72):
                w(f"  note: {note[i:i + 72]}\n" if i == 0 else f"        {note[i:i + 72]}\n")

        for mp in maps:
            rec = data.get(mp)
            if rec is None:
                # Distinguish a wrong name from a genuinely empty map: towns
                # have no wild table, no item ball and no trainer, and the
                # extractor drops them. Only the former needs fixing.
                exists = (ROOT / "data/maps").glob("*/map.json")
                known = mp in ALL_MAP_IDS
                w(f"  (no trainers, encounters or items)\n" if known
                  else f"  ! map {mp} not found -- check the name\n")
                continue

            trs = [t for t in rec["trainers"] if t["id"] not in seen_trainers]
            seen_trainers.update(t["id"] for t in trs)
            if trs:
                w(f"  trainers ({mp}):\n")
                for t in trs:
                    n_tr += 1
                    mark = "[M]" if is_mandatory(t["id"]) else "[o]"
                    dbl = "  (DOUBLE)" if t["kind"] == "double" else ""
                    w(f"    {mark} {t['id']}{dbl}\n")

            if rec["encounters"]:
                w(f"  encounters ({mp}):\n")
                for field, mons in rec["encounters"].items():
                    names = ", ".join(
                        f"{m['species'].replace('SPECIES_', '')} "
                        f"L{m['min']}-{m['max']}" for m in mons)
                    w(f"    {METHOD_LABEL.get(field, field):<11} {names}\n")

            balls, hidden = rec["items"]["ball"], rec["items"]["hidden"]
            if balls or hidden:
                n_items += len(balls) + len(hidden)
                w(f"  items ({mp}):\n")
                if balls:
                    w("    ball       " + ", ".join(
                        i.replace("ITEM_", "") for i in balls) + "\n")
                if hidden:
                    w("    hidden     " + ", ".join(
                        i.replace("ITEM_", "") for i in hidden) + "\n")
        w("\n")

    w("=" * 78 + "\n")
    w(f"TOTALS: {n_stops} stops, {n_tr} trainers listed, {n_items} items\n")
    w("=" * 78 + "\n\n")
    w(UNCERTAINTIES)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

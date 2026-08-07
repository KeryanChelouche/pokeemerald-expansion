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
import textwrap
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Each stop: (label, [maps], gate, badge, [mandatory trainer ids], notes)
#
# MANDATORY comes from the reference sheet's mandatory-only export, not from a
# rule of my own. That export includes fights I had wrongly called optional --
# gym puzzle trainers, and route trainers whose sight lines cannot be walked
# around -- which is the gap the earlier draft flagged and could not close.
#
# THE ORDER IS REROUTED. The sheet is speedrun-routed and takes Wattson,
# Mt. Chimney and Flannery before Brawly. Moving Brawly is not a swap: Slateport
# is only reachable by Mr Briney's boat from Dewford, so every fight from the
# Slateport museum through Flannery sits behind Brawly and moves with him. That
# is 16 fights, marked [REROUTED] below.
#
# The connection data alone will not tell you this. Route 116 connects to
# Verdanturf, and Verdanturf to Route 117 and Mauville, which looks like a way
# to reach Wattson without ever sailing -- but that path runs through Rusturf
# Tunnel, which is blocked by rubble until the story opens it. Geography says
# the shortcut exists; the plot says it does not.
ORDER = [
    ("Littleroot Town", ["LITTLEROOT_TOWN"], None, None, [],
     "Starter chosen here."),
    ("Route 101", ["ROUTE101"], None, None, [], ""),
    ("Oldale Town", ["OLDALE_TOWN"], None, None, [], ""),
    ("Route 103", ["ROUTE103"], None, None,
     ["TRAINER_{rival}_ROUTE_103_{starter}"], "Rival 1."),
    ("Route 102", ["ROUTE102"], "rival_route_103", None, ["TRAINER_CALVIN_1"], ""),
    ("Petalburg City", ["PETALBURG_CITY"], "rival_route_103", None, [],
     "Norman refuses the gym until four badges."),
    ("Route 104 (south)", ["ROUTE104"], "rival_route_103", None, [],
     "Same MAPSEC as the northern half, so the two share one encounter."),
    ("Petalburg Woods", ["PETALBURG_WOODS"], "route_104_south", None,
     ["TRAINER_GRUNT_PETALBURG_WOODS"],
     "NOT IN THE SHEET but kept: this grunt blocks the Devon researcher scene "
     "and the path north. Confirm whether it is genuinely avoidable."),
    ("Route 104 (north)", ["ROUTE104"], "aqua_petalburg_woods", None, [], ""),
    ("Rustboro City", ["RUSTBORO_CITY"], "aqua_petalburg_woods", None, [], ""),
    ("Rustboro Gym", ["RUSTBORO_CITY_GYM"], "aqua_petalburg_woods", "STONE",
     ["TRAINER_JOSH", "TRAINER_TOMMY", "TRAINER_MARC", "TRAINER_ROXANNE_1"],
     "GYM 1. The three gym trainers are unavoidable, per the sheet."),
    ("Route 116", ["ROUTE116"], "gym_roxanne", None, ["TRAINER_DEVAN"], ""),
    ("Rusturf Tunnel", ["RUSTURF_TUNNEL"], "gym_roxanne", None,
     ["TRAINER_GRUNT_RUSTURF_TUNNEL"], "Devon Goods."),
    ("Rustboro City (rival 2)", ["RUSTBORO_CITY"], "rusturf_grunt", None,
     ["TRAINER_{rival}_RUSTBORO_{starter}"],
     "NOT IN THE SHEET but kept -- confirm whether rival 2 is skippable."),
    ("Route 105", ["ROUTE105"], "gym_brawly", None, [], "Needs Surf."),
    ("Route 106", ["ROUTE106"], "rival_rustboro", None, [], "Briney's boat."),
    ("Granite Cave", ["GRANITE_CAVE_1F", "GRANITE_CAVE_B1F", "GRANITE_CAVE_B2F",
                      "GRANITE_CAVE_STEVENS_ROOM"], "rival_rustboro", None, [],
     "Deliver the letter to Steven; he gives TM47."),
    ("Dewford Town", ["DEWFORD_TOWN"], "rival_rustboro", None, [], ""),
    ("Dewford Gym", ["DEWFORD_TOWN_GYM"], "rival_rustboro", "KNUCKLE",
     ["TRAINER_BRAWLY_1"],
     "GYM 2 [REROUTED] -- the sheet fights Brawly after Flannery. Everything "
     "from Slateport to Flannery moves behind this."),
    ("Route 107", ["ROUTE107"], "gym_brawly", None, [], ""),
    ("Route 108", ["ROUTE108"], "gym_brawly", None, [], ""),
    ("Route 109", ["ROUTE109"], "gym_brawly", None, [], ""),
    ("Slateport City", ["SLATEPORT_CITY", "SLATEPORT_CITY_OCEANIC_MUSEUM_2F"],
     "gym_brawly", None, ["TRAINER_GRUNT_MUSEUM_1", "TRAINER_GRUNT_MUSEUM_2"],
     "[REROUTED] Reachable only by boat from Dewford."),
    ("Route 110", ["ROUTE110"], "slateport_museum", None,
     ["TRAINER_{rival}_ROUTE_110_{starter}", "TRAINER_EDWARD", "TRAINER_ALYSSA"],
     "[REROUTED] Rival 3."),
    ("Mauville City", ["MAUVILLE_CITY"], "rival_route_110", None,
     ["TRAINER_WALLY_MAUVILLE"], "[REROUTED]"),
    ("Mauville Gym", ["MAUVILLE_CITY_GYM"], "rival_route_110", "DYNAMO",
     ["TRAINER_BEN", "TRAINER_WATTSON_1"], "GYM 3 [REROUTED]."),
    ("Route 117", ["ROUTE117"], "gym_wattson", None, [], ""),
    ("Verdanturf Town", ["VERDANTURF_TOWN"], "gym_wattson", None, [], ""),
    ("Route 111 (south)", ["ROUTE111"], "gym_wattson", None, [],
     "UNSURE: the desert half needs Go Goggles, obtained after Mt. Chimney."),
    ("Route 112", ["ROUTE112"], "gym_wattson", None, [], ""),
    ("Fiery Path", ["FIERY_PATH"], "gym_wattson", None, [], ""),
    ("Route 113", ["ROUTE113"], "gym_wattson", None, [], ""),
    ("Fallarbor Town", ["FALLARBOR_TOWN"], "gym_wattson", None, [], ""),
    ("Route 114", ["ROUTE114"], "gym_wattson", None, ["TRAINER_LUCAS_1"],
     "[REROUTED]"),
    ("Meteor Falls", ["METEOR_FALLS_1F_1R", "METEOR_FALLS_1F_2R",
                      "METEOR_FALLS_B1F_1R", "METEOR_FALLS_B1F_2R"],
     "gym_wattson", None, [], ""),
    ("Route 115", ["ROUTE115"], "gym_wattson", None, [], ""),
    ("Jagged Pass", ["JAGGED_PASS"], "route_114", None, [], ""),
    ("Mt. Chimney", ["MT_CHIMNEY"], "jagged_pass", None,
     ["TRAINER_GRUNT_MT_CHIMNEY_1", "TRAINER_GRUNT_MT_CHIMNEY_2",
      "TRAINER_TABITHA_MT_CHIMNEY", "TRAINER_MAXIE_MT_CHIMNEY"],
     "[REROUTED]"),
    ("Route 111 (desert)", ["ROUTE111"], "mt_chimney", None, [],
     "Go Goggles open the desert."),
    ("Lavaridge Town", ["LAVARIDGE_TOWN"], "mt_chimney", None, [], ""),
    ("Lavaridge Gym", ["LAVARIDGE_TOWN_GYM_1F", "LAVARIDGE_TOWN_GYM_B1F"],
     "mt_chimney", "HEAT", ["TRAINER_FLANNERY_1"], "GYM 4 [REROUTED]."),
    ("Petalburg Gym", ["PETALBURG_CITY_GYM"], "gym_flannery", "BALANCE",
     ["TRAINER_RANDALL", "TRAINER_PARKER", "TRAINER_JODY", "TRAINER_NORMAN_1"],
     "GYM 5. Opens at four badges."),
    ("Route 118", ["ROUTE118"], "gym_norman", None,
     ["TRAINER_ROSE_1", "TRAINER_DEANDRE"],
     "MOVED: the sheet fights these before Wattson, since Route 118 west is "
     "walkable from Mauville. Placed here because this is when the route is "
     "actually crossed, heading for Route 119. Move them earlier if you would "
     "rather take the EXP sooner."),
    ("Route 119", ["ROUTE119"], "gym_norman", None,
     ["TRAINER_{rival}_ROUTE_119_{starter}"], "Rival 4."),
    ("Weather Institute", ["ROUTE119_WEATHER_INSTITUTE_1F",
                           "ROUTE119_WEATHER_INSTITUTE_2F"], "gym_norman", None,
     ["TRAINER_GRUNT_WEATHER_INST_4", "TRAINER_GRUNT_WEATHER_INST_2",
      "TRAINER_GRUNT_WEATHER_INST_5", "TRAINER_SHELLY_WEATHER_INSTITUTE"],
     "Grunts identified by matching the sheet's teams against the ROM parties."),
    ("Fortree City", ["FORTREE_CITY"], "weather_institute", None, [], ""),
    ("Fortree Gym", ["FORTREE_CITY_GYM"], "weather_institute", "FEATHER",
     ["TRAINER_FLINT", "TRAINER_EDWARDO", "TRAINER_DARIUS", "TRAINER_WINONA_1"],
     "GYM 6."),
    ("Route 120", ["ROUTE120"], "gym_winona", None, [], ""),
    ("Route 121", ["ROUTE121"], "gym_winona", None, [], ""),
    ("Route 122", ["ROUTE122"], "gym_winona", None, [], ""),
    ("Mt. Pyre", ["MT_PYRE_1F", "MT_PYRE_2F", "MT_PYRE_3F", "MT_PYRE_4F",
                  "MT_PYRE_5F", "MT_PYRE_6F", "MT_PYRE_EXTERIOR",
                  "MT_PYRE_SUMMIT"], "gym_winona", None,
     ["TRAINER_GRUNT_MT_PYRE_2", "TRAINER_GRUNT_MT_PYRE_1",
      "TRAINER_GRUNT_MT_PYRE_3", "TRAINER_GRUNT_MT_PYRE_4"],
     "All four matched exactly by team."),
    ("Magma Hideout", ["MAGMA_HIDEOUT_1F", "MAGMA_HIDEOUT_2F_1R",
                       "MAGMA_HIDEOUT_2F_2R", "MAGMA_HIDEOUT_2F_3R",
                       "MAGMA_HIDEOUT_3F_1R", "MAGMA_HIDEOUT_3F_2R",
                       "MAGMA_HIDEOUT_3F_3R", "MAGMA_HIDEOUT_4F"],
     "mt_pyre", None,
     ["TRAINER_GRUNT_MAGMA_HIDEOUT_2", "TRAINER_GRUNT_MAGMA_HIDEOUT_3",
      "TRAINER_GRUNT_MAGMA_HIDEOUT_9", "TRAINER_GRUNT_MAGMA_HIDEOUT_16",
      "TRAINER_GRUNT_MAGMA_HIDEOUT_11", "TRAINER_GRUNT_MAGMA_HIDEOUT_12",
      "TRAINER_GRUNT_MAGMA_HIDEOUT_13",
      "TRAINER_TABITHA_MAGMA_HIDEOUT", "TRAINER_MAXIE_MAGMA_HIDEOUT"],
     "7 of 16 grunts, identified by team and floor. The 2F one is either "
     "_3 or _15 -- identical teams on the same floor, so the sheet cannot "
     "separate them; they are the same fight either way."),
    ("Lilycove City", ["LILYCOVE_CITY"], "magma_hideout", None,
     ["TRAINER_{rival}_LILYCOVE_{starter}"], "Rival 5."),
    ("Aqua Hideout", ["AQUA_HIDEOUT_1F", "AQUA_HIDEOUT_B1F", "AQUA_HIDEOUT_B2F"],
     "rival_lilycove", None,
     ["TRAINER_GRUNT_AQUA_HIDEOUT_2", "TRAINER_GRUNT_AQUA_HIDEOUT_7",
      "TRAINER_GRUNT_AQUA_HIDEOUT_6", "TRAINER_GRUNT_AQUA_HIDEOUT_4",
      "TRAINER_MATT"],
     "4 of 8 grunts. The second B2F one is _4 or _8 -- identical teams, same "
     "floor, same fight either way."),
    ("Route 124", ["ROUTE124"], "aqua_hideout", None, ["TRAINER_DECLAN"], ""),
    ("Mossdeep City", ["MOSSDEEP_CITY"], "aqua_hideout", None, [], ""),
    ("Mossdeep Gym", ["MOSSDEEP_CITY_GYM"], "aqua_hideout", "MIND",
     ["TRAINER_PRESTON", "TRAINER_MAURA", "TRAINER_MACEY", "TRAINER_SYLVIA",
      "TRAINER_HANNAH", "TRAINER_TATE_AND_LIZA_1"], "GYM 7. Tate & Liza is a DOUBLE."),
    ("Space Center", ["MOSSDEEP_CITY_SPACE_CENTER_1F",
                      "MOSSDEEP_CITY_SPACE_CENTER_2F"], "gym_tate_liza", None,
     ["TRAINER_GRUNT_SPACE_CENTER_4", "TRAINER_GRUNT_SPACE_CENTER_2",
      "TRAINER_GRUNT_SPACE_CENTER_5", "TRAINER_GRUNT_SPACE_CENTER_6",
      "TRAINER_GRUNT_SPACE_CENTER_7",
      "TRAINER_MAXIE_MOSSDEEP", "TRAINER_TABITHA_MOSSDEEP"],
     "5 of 7 grunts, all matched exactly. Maxie and Tabitha are a MULTI battle, "
     "2-vs-2 with Steven as partner -- the harness cannot run this yet."),
    ("Route 125", ["ROUTE125"], "gym_tate_liza", None, [], ""),
    ("Route 127", ["ROUTE127"], "space_center", None, [], ""),
    ("Route 128", ["ROUTE128"], "space_center", None, [], ""),
    ("Seafloor Cavern", ["SEAFLOOR_CAVERN_ENTRANCE", "SEAFLOOR_CAVERN_ROOM1",
                         "SEAFLOOR_CAVERN_ROOM2", "SEAFLOOR_CAVERN_ROOM3",
                         "SEAFLOOR_CAVERN_ROOM4", "SEAFLOOR_CAVERN_ROOM5",
                         "SEAFLOOR_CAVERN_ROOM6", "SEAFLOOR_CAVERN_ROOM7",
                         "SEAFLOOR_CAVERN_ROOM8", "SEAFLOOR_CAVERN_ROOM9"],
     "space_center", None,
     ["TRAINER_SHELLY_SEAFLOOR_CAVERN", "TRAINER_ARCHIE"], ""),
    ("Route 126", ["ROUTE126"], "space_center", None, [], ""),
    ("Sootopolis City", ["SOOTOPOLIS_CITY"], "seafloor_cavern", None, [], ""),
    ("Sootopolis Gym", ["SOOTOPOLIS_CITY_GYM_1F", "SOOTOPOLIS_CITY_GYM_B1F"],
     "sootopolis_plot", "RAIN", ["TRAINER_JUAN_1"], "GYM 8."),
    ("Route 129", ["ROUTE129"], "gym_juan", None, [], ""),
    ("Route 130", ["ROUTE130"], "gym_juan", None, [], ""),
    ("Route 131", ["ROUTE131"], "gym_juan", None, [], ""),
    ("Route 132", ["ROUTE132"], "gym_juan", None, [], ""),
    ("Route 133", ["ROUTE133"], "gym_juan", None, [], ""),
    ("Route 134", ["ROUTE134"], "gym_juan", None, [], ""),
    ("Victory Road", ["VICTORY_ROAD_1F", "VICTORY_ROAD_B1F", "VICTORY_ROAD_B2F"],
     "gym_juan", None,
     ["TRAINER_WALLY_VR_1", "TRAINER_HOPE", "TRAINER_SHANNON", "TRAINER_JULIE",
      "TRAINER_EDGAR"], ""),
    ("Pokemon League", ["EVER_GRANDE_CITY_SIDNEYS_ROOM",
                        "EVER_GRANDE_CITY_PHOEBES_ROOM",
                        "EVER_GRANDE_CITY_GLACIAS_ROOM",
                        "EVER_GRANDE_CITY_DRAKES_ROOM",
                        "EVER_GRANDE_CITY_CHAMPIONS_ROOM"],
     "victory_road", None,
     ["TRAINER_SIDNEY", "TRAINER_PHOEBE", "TRAINER_GLACIA", "TRAINER_DRAKE",
      "TRAINER_WALLACE"], "No healing between rooms."),
    ("Meteor Falls (Steven)", ["METEOR_FALLS_1F_2R"], "champion", None, [],
     "Steven is post-Champion in Emerald and is listed in the sheet. Left out "
     "of the mandatory chain: the run ends at Wallace."),
]

# The mandatory fights, stated explicitly rather than matched by name.
#
# A substring rule looked fine and was wrong: "MATT" matches TRAINER_MATTHEW, a
# Route 108 swimmer, and "GRUNT" sweeps in every avoidable hideout grunt. These
# ids were each read out of the map that starts the fight.
#
# {rival} and {starter} are substituted the same way the campaign table does it,
# so the route reflects the trainer's own choices.
UNCERTAINTIES = """
==============================================================================
WHAT IS SOLID, WHAT IS NOT
==============================================================================

SOLID
  * Encounters, items, and which trainers exist on which map, with single vs
    double vs multi: all read from the decomp, not from a guide.
  * WHICH fights are mandatory: taken from the reference sheet's mandatory-only
    export. That corrected a real error -- gym puzzle trainers and route
    trainers with unavoidable sight lines had been treated as optional.
  * Every mandatory id is checked against the game data when this file is
    generated, so a name that stops matching fails loudly.

CAPABILITIES ARE NOW MODELLED
  Where each HM, rod and the Go Goggles are given comes from the giveitem calls
  in the map scripts. Which badge each HM needs comes from field_move.c. A
  capability starts at the later of the two, which matters: Strength is picked up
  in Rusturf Tunnel but is not usable until Flannery, four badges later.

  Draws are therefore listed in the segment where they first become TAKEABLE,
  not where the map opens. Route 102 is the clearest case -- its grass draw is in
  segment 2, its old-rod draw appears when Dewford is reached, its surf draw
  after Norman, and its good-rod draw at Route 118. One location, four segments.

  Fishing tables are split by rod (slots 0-1 old, 2-4 good, 5-9 super, per
  ChooseWildMonIndex_Fishing), so an Old Rod is useful immediately instead of the
  whole table waiting on a Super Rod.

  Gift items now come from the giveitem calls too, which is where every HM, gym
  TM and rod lives. Item balls and hidden items were never the interesting half.

THE REROUTE
  Brawly is gym 2 here; the sheet fights him after Flannery because it is routed
  for a speedrun. Moving him is not a swap -- Slateport is reachable only by
  Mr Briney's boat from Dewford, so all of these move behind Brawly with him:

    Slateport museum grunts, Slateport rival, Edward, Alyssa, Wally,
    Ben, Wattson, Lucas, the four Mt. Chimney fights, Flannery.

  Marked [REROUTED] in the listing above.

  Worth knowing: the map connections alone would tell you the opposite. Route
  116 connects to Verdanturf, and Verdanturf to Route 117 and Mauville, which
  looks like a walking route to Wattson with no boat at all. That path runs
  through Rusturf Tunnel, which is blocked by rubble until the story clears it.
  Geography says the shortcut exists; the plot says it does not. Ordering cannot
  be derived from the data for this reason.

NOT SOLID -- needs your judgement
  1. TWO FIGHTS ARE IN MY LIST BUT NOT IN THE SHEET: the Petalburg Woods Aqua
     grunt, and rival 2 outside Rustboro. Both look unavoidable to me -- the
     grunt blocks the Devon researcher scene, and the rival triggers on the way
     out of town. Kept, and flagged. If the sheet is right they should go.

  2. WHICH GRUNTS. The sheet gives counts, not identities: 1 grunt on Weather
     Institute 1F (data has 2), 2 on 2F (data has 3), 7 across the Magma
     Hideout (data has 16), 4 in the Aqua Hideout (data has 8), 5 at the Space
     Center (data has 7). Only the named bosses are listed above. Resolving
     these needs someone to check which grunts actually block a corridor.

  3. ROUTE 118 PLACEMENT. The sheet fights Rose and Deandre before Wattson,
     since Route 118 west is walkable from Mauville. I placed them after Norman,
     which is when the route is actually crossed on the way to Route 119. Either
     works; it is an EXP-timing choice, not a correctness one.

  4. ROUTE 104 AND ROUTE 111 span two differently-gated halves each. Now
     RESOLVED by keying the one-encounter rule on the MAPSEC rather than on the
     stop: both halves of Route 104 are MAPSEC_ROUTE_104 and share a single
     encounter, so the northern half shows as "only if unspent". Flagged only
     because the halves still appear as separate stops for ordering.

  5. THE SPACE CENTER FIGHT IS A MULTI BATTLE -- Maxie and Tabitha 2-vs-2 with
     Steven as partner, the only one in the game. The harness cannot run it
     (single and double only), and R1 has to say what happens when one of
     Steven's Pokemon faints, since they are not the player's.

  6. GATES are still my reconstruction rather than flag-verified.

  7. SAFARI ZONE, static and roaming encounters, and the Trick House are not
     modelled.

  8. LEVEL CAPS are a rule, not map data, and are not here.

  9. BERRIES AND SHOPS are not listed. Berry trees are their own object type and
     shop stock is per-mart; neither is in here.

==============================================================================
"""


# Capabilities, all read from the game rather than recalled.
#
# WHERE each item is given comes from the giveitem calls in the map scripts.
# WHICH BADGE each HM needs comes from field_move.c -- an HM in the bag is not a
# capability until the badge exists, and for Surf the two are far apart.
#
# This matters more than the item lists do: §7.2 filters draws by capability, so
# without it a segment offers surf and fishing draws that cannot be taken.
CAPABILITIES = {
    # name          item                 given at (stop label)     badge needed
    "CUT":        ("ITEM_HM_CUT",        "Rustboro City",          1),
    "FLASH":      ("ITEM_HM_FLASH",      "Granite Cave",           2),
    "ROCK_SMASH": ("ITEM_HM_ROCK_SMASH", "Mauville City",          3),
    "STRENGTH":   ("ITEM_HM_STRENGTH",   "Rusturf Tunnel",         4),
    "SURF":       ("ITEM_HM_SURF",       "Petalburg Gym",          5),
    "FLY":        ("ITEM_HM_FLY",        "Route 119",              6),
    "DIVE":       ("ITEM_HM_DIVE",       "Mossdeep City",          7),
    "WATERFALL":  ("ITEM_HM_WATERFALL",  "Sootopolis City",        8),
    "ROD_OLD":    ("ITEM_OLD_ROD",       "Dewford Town",           0),
    "ROD_GOOD":   ("ITEM_GOOD_ROD",      "Route 118",              0),
    "ROD_SUPER":  ("ITEM_SUPER_ROD",     "Mossdeep City",          0),
    "GO_GOGGLES": ("ITEM_GO_GOGGLES",    "Lavaridge Town",         0),
}

BADGE_ORDER = ["STONE", "KNUCKLE", "DYNAMO", "HEAT", "BALANCE", "FEATHER",
               "MIND", "RAIN"]

# Fishing tables hold ten slots split by rod: 0-1 old, 2-4 good, 5-9 super
# (ChooseWildMonIndex_Fishing, src/wild_encounter.c). Splitting them is what
# makes an Old Rod useful early instead of the whole table waiting on a Super Rod.
ROD_SLICES = [("ROD_OLD", 0, 2), ("ROD_GOOD", 2, 5), ("ROD_SUPER", 5, 10)]

# Which capability each draw method needs. Land needs nothing.
METHOD_CAP = {"water_mons": "SURF", "rock_smash_mons": "ROCK_SMASH"}

METHOD_LABEL = {
    "land_mons": "grass",
    "water_mons": "surf",
    "rock_smash_mons": "rock_smash",
    "fishing_mons": "fishing",
    "ROD_OLD": "old rod",
    "ROD_GOOD": "good rod",
    "ROD_SUPER": "super rod",
}


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
    w("ONE ENCOUNTER PER LOCATION, FOR THE WHOLE RUN (R2). A location listed in\n")
    w("several segments is not several chances at it. The later rows are the draws\n")
    w("that become reachable IF the location was deliberately left unspent -- they\n")
    w("are marked \"only if unspent\". Taking the grass encounter in an early\n")
    w("segment closes that location permanently, surf and rods included.\n\n")
    w("Banking a location is therefore a real strategic option: Route 102 spent on\n")
    w("grass gives a Lv3 Poochyena, and held until Surf gives a Lv20-30 Marill.\n")
    w("The cost is running those segments a Pokemon short.\n\n")
    w("Structured by GATE, not by route. A fight is the only thing that gates\n")
    w("progress, so everything reachable before a fight is listed under it and the\n")
    w("fight closes the segment. Encounters appear as early as they are available\n")
    w("and fights as late as they can be taken -- catch first, then spend the team\n")
    w("on the fight that opens the next segment.\n\n")
    w("Only MANDATORY fights are listed. Optional route trainers are omitted; they\n")
    w("are free EXP and free risk, and seeking them out is the agent's call. The\n")
    w("full list is in campaign/extracted.json.\n\n")
    w("{rival} resolves to MAY or BRENDAN from the trainer's gender, {starter}\n")
    w("from the starter chosen -- the same substitution the campaign table uses.\n\n")

    # Resolved so a template that stops matching the data fails loudly instead of
    # quietly dropping a mandatory fight.
    all_ids = {t["id"] for r in data.values() for t in r["trainers"]}
    unresolved = []
    for label, _maps, _gate, _badge, mand, _note in ORDER:
        for tid in mand:
            concrete = tid.replace("{rival}", "MAY").replace("{starter}", "TREECKO")
            if concrete not in all_ids:
                unresolved.append((label, tid, concrete))

    # ---- pass 1: number the segments, and work out when things unlock -------
    #
    # A draw is listed in the segment where it first becomes TAKEABLE, which is
    # not the segment where the map opens: Route 102's surf draw waits on Surf,
    # and Surf waits on Norman. Computing that needs the segment numbers first,
    # hence two passes.
    stop_seg, badge_seg, seg_gate = {}, {}, []
    seg = 1
    for label, maps, gate, badge, mand, note in ORDER:
        stop_seg.setdefault(label, seg)
        if badge:
            badge_seg[BADGE_ORDER.index(badge) + 1] = seg
        if mand:
            seg_gate.append((seg, mand, badge, note))
            seg += 1
    n_segments = seg

    cap_seg = {}
    for cap, (item, at_stop, badge_no) in CAPABILITIES.items():
        got = stop_seg.get(at_stop, 1)
        # Held AND usable: the later of picking the item up and earning the badge.
        need = badge_seg.get(badge_no, 1) if badge_no else 1
        cap_seg[cap] = max(got, need)

    # ---- pass 2: bucket every draw and item into the segment it opens in ----
    opens: dict[int, list] = {}

    def add(s, label, kind, text, sec=None):
        opens.setdefault(s, []).append((label, kind, text, sec))

    first_seen: dict[str, int] = {}

    for label, maps, gate, badge, mand, note in ORDER:
        base = stop_seg[label]
        for mp in maps:
            rec = data.get(mp)
            if rec is None:
                continue
            sec = rec.get("region_section") or mp
            for field, mons in rec["encounters"].items():
                def fmt(ms):
                    return ", ".join(f"{m['species'].replace('SPECIES_', '')} "
                                     f"L{m['min']}-{m['max']}" for m in ms)
                if field == "fishing_mons":
                    for rod, lo, hi in ROD_SLICES:
                        part = mons[lo:hi]
                        if part:
                            s = max(base, cap_seg[rod])
                            first_seen[sec] = min(first_seen.get(sec, s), s)
                            add(s, label, METHOD_LABEL[rod], fmt(part), sec)
                else:
                    cap = METHOD_CAP.get(field)
                    s = max(base, cap_seg[cap]) if cap else base
                    first_seen[sec] = min(first_seen.get(sec, s), s)
                    add(s, label, METHOD_LABEL.get(field, field), fmt(mons), sec)
            for key, tag in (("ball", "items"), ("hidden", "hidden"),
                             ("gift", "gift")):
                vals = rec["items"].get(key) or []
                if vals:
                    add(base, label, tag,
                        ", ".join(i.replace("ITEM_", "") for i in vals))
        if note and not mand:
            add(base, label, "note", note)

    for cap, s in cap_seg.items():
        item, at_stop, badge_no = CAPABILITIES[cap]
        why = f"{item.replace('ITEM_', '')} at {at_stop}"
        if badge_no and badge_seg.get(badge_no, 1) > stop_seg.get(at_stop, 1):
            why += f" (held earlier; needs badge {badge_no})"
        add(s, "** CAPABILITY **", cap, why)

    # ---- emit ---------------------------------------------------------------
    n_tr = n_items = 0
    for s, mand, badge, gate_note in seg_gate:
        w("#" * 78 + "\n")
        head = f"SEGMENT {s}"
        if badge:
            head += f"        >>> {badge} BADGE <<<"
        w(head + "\n")
        w("#" * 78 + "\n\n")

        rows = opens.get(s, [])
        if rows:
            w("  AVAILABLE FROM HERE\n")
            last = None
            for label, kind, text, sec in rows:
                if kind == "note":
                    for i, line in enumerate(textwrap.wrap(text, 62)):
                        w(f"        {'note: ' if i == 0 else '      '}{line}\n")
                    continue
                if kind in ("items", "hidden", "gift"):
                    n_items += len(text.split(","))
                shown = label if label != last else ""
                last = label
                # R2 gives a location ONE encounter for the whole run. A method
                # that opens in a later segment is not a second chance -- it is
                # only reachable if the location was deliberately left unspent.
                held = (sec is not None and first_seen.get(sec, s) < s)
                mark = "  <- only if unspent" if held else ""
                w(f"    {shown[:23]:<24}{kind:<10} {text}{mark}\n")
            w("\n")
        else:
            w("  AVAILABLE FROM HERE\n    (nothing new)\n\n")

        w("  GATE -- beat this to open the next segment\n")
        for tid in mand:
            n_tr += 1
            w(f"    {tid}\n")
        if gate_note:
            for line in textwrap.wrap(gate_note, 68):
                w(f"      {line}\n")
        w("\n")

    n_seg = len(seg_gate)

    w("=" * 78 + "\n")
    w(f"TOTALS: {n_seg} segments, {n_tr} mandatory fights, {n_items} items\n")
    if unresolved:
        w("\nUNRESOLVED mandatory ids (fix before trusting this table):\n")
        for stop, tid, concrete in unresolved:
            w(f"  {stop}: {tid} -> {concrete} not found in the game data\n")
    w("=" * 78 + "\n\n")
    w(UNCERTAINTIES)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

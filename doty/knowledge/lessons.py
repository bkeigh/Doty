"""What the Learn page teaches: how strong players think, last hits, leaving lane, items, drafting, fights, dying
less, ranked habits and quick wins, each tip tagged with the players it's for.

Plain words, patch-proof where it can be (timings live in timings.py). Levels (CoachConfig.level):
"new" never played, "learning" Herald-Guardian, "some" Crusader-Legend, "experienced" Ancient-Divine,
"pro" Immortal.
"""

from __future__ import annotations

from dataclasses import dataclass

LEVELS: tuple[tuple[str, str, str], ...] = (
    # (key, short name, who it's for)
    ("new", "New", "New to Dota"),
    ("learning", "Learning", "Herald to Guardian"),
    ("some", "Climbing", "Crusader to Legend"),
    ("experienced", "Experienced", "Ancient to Divine"),
    ("pro", "Top", "Immortal"),
)
LEVEL_KEYS = tuple(k for k, _n, _w in LEVELS)
ALL = frozenset(LEVEL_KEYS)
BASICS = frozenset({"new", "learning"})
MID = frozenset({"learning", "some", "experienced"})
UPPER = frozenset({"some", "experienced", "pro"})


def level_for_rank(rank_tier: int | None) -> str:
    """The coaching level that fits a rank (OpenDota's rank_tier: 11 = Herald 1 ... 80 = Immortal)."""
    medal = int(rank_tier or 0) // 10
    if medal <= 0:
        return "some"
    return "learning" if medal <= 2 else "some" if medal <= 5 else "experienced" if medal <= 7 else "pro"


@dataclass(frozen=True)
class Tip:
    text: str
    levels: frozenset[str] = ALL
    title: str = ""  # a few words in bold before it

    @property
    def items(self) -> tuple[str, ...]:
        """Items to show beside it (dotaconstants keys: their pictures come from Valve's CDN)."""
        return PICTURES.get(self.title, ((), ()))[0]

    @property
    def heroes(self) -> tuple[str, ...]:
        """Heroes it names, to show their portraits."""
        return PICTURES.get(self.title, ((), ()))[1]


# Pictures for tips, by tip title: (items, heroes). A picture is worth a thousand words: the item to buy, the
# heroes it answers.
PICTURES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "Shop and courier": (("courier",), ()),
    "TP scroll": (("tpscroll",), ()),
    "Healing": (("flask", "tango"), ()),
    "Healers": (("spirit_vessel", "skadi"), ("Huskar", "Alchemist", "Necrophos")),
    "Roshan": (("aegis", "cheese"), ()),
    "Power spikes": (("black_king_bar", "blink"), ()),
    "Vision with a purpose": (("ward_observer", "ward_sentry"), ()),
    "Quelling Blade": (("quelling_blade",), ()),
    "TP to waves": (("tpscroll",), ()),
    "Stuns": (("black_king_bar", "glimmer_cape", "force_staff"), ("Lion", "Shadow Shaman", "Lina")),
    "Evasion": (("monkey_king_bar", "bloodthorn"), ("Phantom Assassin", "Windranger")),
    "Invisibility": (("dust", "ward_sentry"), ("Riki", "Clinkz", "Bounty Hunter")),
    "Break": (("silver_edge",), ("Bristleback", "Spectre", "Phantom Assassin")),
    "Lockdown": (("orchid", "sheepstick", "abyssal_blade"), ("Anti-Mage", "Storm Spirit", "Puck")),
    "Illusions": (("mjollnir", "bfury", "shivas_guard"), ("Phantom Lancer", "Naga Siren", "Broodmother")),
    "Timing beats size": (("black_king_bar",), ()),
    "Smoke": (("smoke_of_deceit",), ()),
    "High ground": (("aegis",), ()),
    "Press your save": (("black_king_bar", "glimmer_cape", "force_staff", "magic_wand"), ()),
    "Escape early": (("tpscroll",), ()),
    "Self-cast": (("glimmer_cape", "force_staff"), ()),
    "TP first": (("tpscroll",), ()),
    "Rotate": (("bottle",), ()),
}


@dataclass(frozen=True)
class Topic:
    key: str
    title: str
    blurb: str
    tips: tuple[Tip, ...]

    def for_level(self, level: str) -> list[Tip]:
        return [t for t in self.tips if level in t.levels]


# Where each level loses most games, and the three things that win the most back.
FOCUS = {
    "new": ("Learn what each part of the screen is: your hero, the minimap, the shop, the courier.",
            "Last-hit: hit an enemy creep only when it's about to die. That's where your gold comes from.",
            "Stay near your team and your towers. Walking alone is how new players die."),
    "learning": ("Die less: most games at this rank are lost to deaths, not to bad builds. Back off when enemies "
                 "go missing from the minimap.",
                 "Farm more: aim for 35 last hits by 10:00, then 45. Practice in Demo Hero (below).",
                 "Always carry a TP scroll, and use your save items before you're stunned."),
    "some": ("Turn won fights into towers and Roshan right away, instead of farming or chasing.",
             "Itemize against their heroes: Black King Bar against stuns, Monkey King Bar against evasion, "
             "Spirit Vessel against healing.",
             "Keep a farm route: wave, camp, next wave. Never stand still without a reason."),
    "experienced": ("Play around timings: your power spike and theirs, Roshan, runes, day and night.",
                    "Vision wins games here: ward where they'll walk, and deward before objectives.",
                    "Draft for the game plan: what your lineup wants to do at 15, 25 and 40 minutes."),
    "pro": ("Lane equilibrium and creep aggro decide lanes: control where the wave meets.",
            "Tempo: every minute should end with farm, an objective, or space made for your cores.",
            "Track their cooldowns and buybacks after every fight, and force fights when they're down."),
}

# Last hits by 10:00 for a core: (where to start, a solid target), by level.
LAST_HIT_TARGETS = {
    # (About 80 lane creeps reach a lane by 10:00, so the top of the scale needs a near-perfect lane or jungle farm.)
    "new": (20, 30), "learning": (35, 45), "some": (45, 55), "experienced": (55, 65), "pro": (65, 75),
}

DRILL = (
    "Open Dota, go to Heroes, pick a hero you play and press Demo Hero. Turn on creep spawning in the demo panel.",
    "Stand in a lane for 10 minutes without using spells. Only last-hit and deny.",
    "Count your last hits at 5 and 10 minutes against your target (keep a timer if the demo shows no clock). "
    "Doty's laning gauge tracks it for you in real games.",
    "Do it three times with the same hero before moving on: timing an attack is muscle memory.",
)

TOPICS: tuple[Topic, ...] = (
    Topic("basics", "The basics", "What everything is, for your first games.", (
        Tip("Two teams of five each defend an Ancient, the big building in their base. Destroy theirs to win.",
            BASICS, "The goal"),
        Tip("Creeps walk down three lanes every 30 seconds. Your gold comes from landing the last hit on enemy "
            "creeps and heroes.", BASICS, "Gold"),
        Tip("Hitting your own creep when it's under half health denies it: the enemy gets no gold and half the "
            "experience. Press A, then click the creep.", BASICS, "Denies"),
        Tip("Buy items at the shop in your base. Your courier carries them to you: buy, then press its "
            "deliver-items button (or turn on Auto Deliver).", BASICS, "Shop and courier"),
        Tip("A Town Portal Scroll teleports you to a friendly building, like a tower. Carry one at all times.",
            BASICS, "TP scroll"),
        Tip("Your fountain, in your base, heals you fast. Walking back to heal is fine.", BASICS, "Healing"),
        Tip("Cores (carry, mid, offlane) take the farm and grow strong. Supports buy wards, help in lanes and "
            "save their cores.", BASICS, "Roles"),
        Tip("Roshan is a big monster in one of two pits in the river; he switches pits when day turns to night. "
            "Killing him gives the Aegis, an extra life. It takes a team.", BASICS, "Roshan"),
        Tip("Dead heroes come back after a timer. Buyback costs gold (more the richer you are) and brings you back "
            "at once: save it for big late-game fights.", BASICS, "Dying"),
    )),
    Topic("vision", "How strong players see the game", "Map awareness, timings and planning ahead.", (
        Tip("Glance at the minimap every few seconds. Count the enemy heroes you can see. If three are missing "
            "and you're alone far from your towers, leave.", ALL, "The minimap habit"),
        Tip("Think a minute ahead: where is the next wave, the next camp, the next objective? Walk there before "
            "it's time.", MID | {"pro"}, "Plan the next minute"),
        Tip("Know the power spikes: level 6 for most ultimates, and big items like Black King Bar and Blink "
            "Dagger. Fight when yours are up, avoid fights just after theirs land.", MID | {"pro"},
            "Power spikes"),
        Tip("When you can't win a fight, trade: take a tower or Roshan on the other side of the map.",
            UPPER, "Trade, don't chase"),
        Tip("After every fight, note who used their ultimate and who has no buyback. That's your window.",
            frozenset({"experienced", "pro"}), "Track cooldowns"),
        Tip("Ward where the enemy will walk, their jungle entrances and the paths to your farm, not only where "
            "you stand.", UPPER, "Vision with a purpose"),
    )),
    Topic("laning", "Laning and last hits", "How to farm the lane and win it.", (
        Tip("Watch the creep's health bar, not your hero. Start your attack when the creep has about one hit "
            "of health left. Ranged heroes start a bit earlier for the projectile.", ALL, "Timing"),
        Tip("Don't right-click the wave over and over: it pushes the lane toward their tower, where you're easy "
            "to gank. Press Hold (H) between last hits.", ALL, "Don't push by accident"),
        Tip("Deny your own creeps when they drop under half health. Denying is as important as last-hitting "
            "when the lane is close to your tower.", ALL, "Deny"),
        Tip("Melee cores: a Quelling Blade makes last hits much easier.", BASICS, "Quelling Blade"),
        Tip("Harass the enemy laner when they walk up to last-hit: they can't hit you back without losing the "
            "creep.", MID | {"pro"}, "When to trade"),
        Tip("Keep the wave close to your tower when you're the weaker laner. To pull it back, hit an enemy hero "
            "near their creeps and walk away: the creeps follow you.", UPPER, "Creep aggro"),
        Tip("Supports: pull the small camp by your safe lane at about x:15 or x:45 so your creeps meet there, "
            "and stack camps at x:53-x:55 for your cores.", UPPER, "Pull and stack"),
    )),
    Topic("leave_lane", "When to leave lane", "The laning stage, and what comes after it.", (
        Tip("Most laning stages end around 10 minutes (sooner or later depending on the game): when a tier-1 "
            "tower falls, when you reach a key item or level, or when the lane is lost.", ALL, "How long"),
        Tip("If you're dying over and over or can't get last hits, the lane is lost: farm the jungle or another "
            "lane instead.", ALL, "A lost lane"),
        Tip("Mid and offlane: after level 6 or a rune, look for a gank on a side lane, then go back.",
            MID | {"pro"}, "Rotate"),
        Tip("Carries: after laning, farm a lane and the jungle next to it, safely. Join fights when your core "
            "item is done.", MID | {"pro"}, "Carry after laning"),
    )),
    Topic("farming", "Farming", "Gold per minute is the stat that tracks winning best at most ranks.", (
        Tip("Always be farming: wave, then a camp, then the next wave. Standing still costs you gold.",
            ALL, "Never idle"),
        Tip("Farm the lane that's safe: the one on your side of the map, with your towers, while the enemy "
            "is seen elsewhere.", MID | {"pro"}, "Safe farm"),
        Tip("Teleport to a wave about to hit your tower: you defend and farm in one.", UPPER, "TP to waves"),
        Tip("Stacked camps are double the gold. Ask your supports to stack, or stack your own jungle.",
            UPPER, "Stacks"),
    )),
    Topic("items", "Items against heroes", "Buy for the game you're in, not only your usual build.", (
        Tip("Lots of stuns and silences (Lion, Shadow Shaman, Lina...): Black King Bar on cores; Glimmer Cape or "
            "Force Staff on supports.", ALL, "Stuns"),
        Tip("Evasion (Phantom Assassin, Windranger's Windrun): Monkey King Bar or Bloodthorn.", ALL, "Evasion"),
        Tip("Heavy healing (Huskar, Alchemist, Necrophos): Spirit Vessel or Eye of Skadi.", ALL, "Healers"),
        Tip("Invisible heroes (Riki, Clinkz, Bounty Hunter): Dust of Appearance and Sentry Wards, early.",
            ALL, "Invisibility"),
        Tip("Passives that carry them (Bristleback, Spectre, Phantom Assassin): Silver Edge breaks them.",
            UPPER, "Break"),
        Tip("Slippery heroes (Anti-Mage, Storm Spirit, Puck): Orchid, Scythe of Vyse or Abyssal Blade.",
            UPPER, "Lockdown"),
        Tip("Illusions and summons (Phantom Lancer, Naga Siren, Broodmother): area damage like Mjollnir, "
            "Battle Fury (melee heroes) or Shiva's Guard.", UPPER, "Illusions"),
        Tip("Buy the answer early: a Black King Bar at 18 minutes wins more fights than a bigger item at 25.",
            ALL, "Timing beats size"),
        Tip("Doty's Heroes tab shows each hero's usual items, and what beats it.", ALL, "In Doty"),
    )),
    Topic("draft", "Drafting and counter-picking", "Picking heroes that win this game.", (
        Tip("At low ranks, three heroes you know well beat any counter-pick. Pick from your pool first.",
            BASICS | {"some"}, "Your hero pool"),
        Tip("Check what your team is missing before you pick: a stun, a teamfight ultimate, a tough front-liner, "
            "a support.", ALL, "Fill the gaps"),
        Tip("Counter by what a hero does, not by name: area damage against illusions, silences against heroes "
            "that need their spells, a save against burst.", MID | {"pro"}, "Counter the mechanic"),
        Tip("Picking last is an advantage: counter their mid or carry, the heroes that decide the game.",
            UPPER, "Last pick"),
        Tip("Ban the hero that counters your best hero, or the one you hate playing against.", UPPER, "Bans"),
        Tip("Doty's Draft page lists counter-picks from millions of public games at your rank.", ALL, "In Doty"),
    )),
    Topic("fights", "Fights and objectives", "When to fight, and what to do after.", (
        Tip("Fight together. Five heroes in one place beat five spread out.", ALL, "Group up"),
        Tip("After you win a fight or kill two of them, take a tower or Roshan right away. Kills alone don't "
            "win games.", ALL, "Cash in"),
        Tip("Don't fight 4 against 5. If someone's dead or missing, wait or farm.", ALL, "Count heroes"),
        Tip("Use Smoke of Deceit to move as a group unseen, before Roshan or a gank.", UPPER, "Smoke"),
        Tip("High ground is hard to take without a big advantage or the Aegis. Siege slowly; don't dive.",
            UPPER, "High ground"),
        Tip("Late in the game, keep gold for buyback. Losing a fight without it can lose the game.",
            MID | {"pro"}, "Buyback gold"),
    )),
    Topic("die_less", "Dying less", "The fastest way up at most ranks.", (
        Tip("Every death gives the enemy gold and costs you farm. Two fewer deaths a game is a big win rate "
            "jump at low ranks.", BASICS | {"some"}, "Why it matters"),
        Tip("Use your save item the moment you're jumped, before the stun lands: Black King Bar, Glimmer, Force "
            "Staff, Magic Wand. Doty calls them for you.", ALL, "Press your save"),
        Tip("Don't farm a lane on their side of the map when enemies are missing.", ALL, "Missing enemies"),
        Tip("Carry a TP scroll, and walk away at half health, not at a quarter.", ALL, "Escape early"),
        Tip("Doty's post-game review lists how you died and what would have saved you.", ALL, "In Doty"),
    )),
    Topic("ranked", "Climbing ranked", "Habits that move your rank.", (
        Tip("Play one or two roles and a few heroes. You climb by playing the same things well.", ALL,
            "Specialize"),
        Tip("After two losses in a row, take a break. Tilted games go badly.", ALL, "Stop the slide"),
        Tip("Mute anyone who tilts you, in chat and voice. Focus on your own mistakes: they're the ones you "
            "control.", ALL, "Mute"),
        Tip("After each game, read Doty's review and pick one thing to fix next game.", ALL, "One fix a game"),
        Tip("Role queue gives you the role you practice. Use it.", BASICS | {"some"}, "Role queue"),
    )),
    Topic("quick", "Quick wins", "Small habits and settings that help right away.", (
        Tip("Press Select Hero twice (F1 unless you changed it) to put the camera on your hero.", ALL,
            "Find your hero"),
        Tip("Hold Alt and click to ping the map for your team.", ALL, "Ping"),
        Tip("Hold Shift to queue orders: use an item, teleport, then walk, all in one go.", ALL, "Queue orders"),
        Tip("Turn on Quick Cast in Dota's hotkey settings: spells cast on key press, one click faster.",
            MID | {"pro"}, "Quick Cast"),
        Tip("Turn on double-tap self-cast in Dota's settings, then tap Glimmer or Force Staff twice to use it "
            "on yourself in an instant.", ALL, "Self-cast"),
        Tip("Buy a TP scroll before you leave the fountain, every time.", ALL, "TP first"),
        Tip("Press Hold (H) between last hits so your hero doesn't hit the wave on its own (with Auto Attack set to "
            "Standard in Dota's settings).", ALL, "Hold"),
    )),
)


# The topics that matter most at each level come first (the first one opens by itself); the rest keep their order.
FIRST = {
    "new": ("basics", "laning", "die_less"),
    "learning": ("die_less", "laning", "items", "quick"),
    "some": ("fights", "items", "farming", "vision"),
    "experienced": ("vision", "draft", "fights"),
    "pro": ("vision", "laning", "draft"),
}
DRILL_LEVELS = frozenset({"new", "learning", "some"})  # the Demo Hero drill: for the levels still building it


def tips_for(level: str) -> list[tuple[Topic, list[Tip]]]:
    """Each topic with the tips for this level (topics with none left out), the level's key topics first."""
    level = level if level in LEVEL_KEYS else "some"
    first = FIRST.get(level, ())
    order = sorted(TOPICS, key=lambda t: (first.index(t.key) if t.key in first else len(first)))
    if level == "learning":  # (they know the screen already: the basics go last, for a friend they're helping)
        order = [t for t in order if t.key != "basics"] + [t for t in order if t.key == "basics"]
    return [(t, tips) for t in order if (tips := t.for_level(level))]

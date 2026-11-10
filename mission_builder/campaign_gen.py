"""Campaign generator: a fixed run of strike missions (6, 10 or 15) for one aircraft on one map.

Each mission is a strike story (strike.py), planned up front: the stories rotate without
repeating back to back, difficulty climbs toward the end and the last mission is a finale.
A mission counts as won or lost from the RESULT line its triggers write to dcs.log
("MBSTORY|<story id>|RESULT|success"); a lost mission is flown again with a fresh layout
before the campaign moves on. The player can also mark a result by hand, for a flight
that ended before its triggers decided.

Separate from campaign.py, which is the live war on a map.
"""
from __future__ import annotations

import datetime as dt
import json
import random
import re
from pathlib import Path

from . import strike

DIR = Path(__file__).resolve().parent.parent / "campaigns_gen"
LENGTHS = {"Short": 6, "Medium": 10, "Long": 15}
# Difficulty -> (complexity of the first mission, enemy skill).
DIFFICULTY = {"Easy": (1, "Average"), "Normal": (2, "Good"), "Hard": (3, "High"), "Very hard": (4, "Excellent")}
FINALE = {"plane": "airfield", "helicopter": "hvt"}
RESULT = re.compile(r"MBSTORY\|([0-9a-f]+)\|RESULT\|(success|failed)")


def plan(length: str, difficulty: str, helicopter: bool, rng: random.Random) -> list[dict]:
    n = LENGTHS[length]
    base, _ = DIFFICULTY[difficulty]
    finale = FINALE["helicopter" if helicopter else "plane"]
    # The finale's story is kept for the last mission, so it never comes earlier or twice in a row.
    pool = [s for s in (strike.HELICOPTER_STORIES if helicopter else strike.STORIES) if s != finale]
    stories: list[str] = []
    while len(stories) < n - 1:
        batch = rng.sample(pool, len(pool))
        if stories and batch[0] == stories[-1]:
            batch.append(batch.pop(0))
        stories += batch
    stories = stories[:n - 1] + [finale]
    return [{"n": i + 1, "story": s, "complexity": min(5, base + round(2 * i / max(1, n - 1))),
             "seed": rng.randrange(1, 2**31), "status": "planned", "attempts": 0, "flights": []}
            for i, s in enumerate(stories)]


def new(values: dict, length: str, difficulty: str, helicopter: bool, codename: str,
        rng: random.Random | None = None) -> dict:
    rng = rng or random.Random()
    c = {"id": dt.datetime.now().strftime("%Y%m%d-%H%M%S"), "name": f"OPERATION {codename}",
         "created": dt.datetime.now().isoformat(timespec="seconds"), "length": length, "difficulty": difficulty,
         "values": {k: values[k] for k in ("terrain", "aircraft", "airport", "scenario", "side", "loadout", "start",
                                          "flight_size", "awacs", "tanker", "weather") if k in values},
         "missions": plan(length, difficulty, helicopter, rng)}
    save(c)
    return c


def path(cid: str) -> Path:
    if not re.fullmatch(r"[0-9-]+", cid):
        raise ValueError("bad campaign id")
    return DIR / f"{cid}.json"


def save(c: dict) -> None:
    DIR.mkdir(exist_ok=True)
    path(c["id"]).write_text(json.dumps(c, indent=1), encoding="utf-8")


def load(cid: str) -> dict:
    return json.loads(path(cid).read_text(encoding="utf-8"))


def all_() -> list[dict]:
    if not DIR.is_dir():
        return []
    return sorted((json.loads(p.read_text(encoding="utf-8")) for p in DIR.glob("*.json")), key=lambda c: c["id"], reverse=True)


def current(c: dict) -> dict | None:
    """The next mission to fly: the first one not yet won (a lost one is flown again)."""
    return next((m for m in c["missions"] if m["status"] != "success"), None)


def next_values(c: dict) -> dict:
    """MissionSpec values for the next mission. Each attempt gets a fresh layout."""
    m = current(c)
    if m is None:
        raise RuntimeError(f"{c['name']} is complete.")
    _, skill = DIFFICULTY[c["difficulty"]]
    return {**c["values"], "mission_type": "Strike", "story": m["story"], "complexity": m["complexity"],
            "enemy_skill": skill, "seed": m["seed"] + 7919 * m["attempts"]}


def record_save(c: dict, story_id: str, file: str) -> None:
    m = current(c)
    m["attempts"] += 1
    m["status"] = "saved"
    m["flights"].append({"story_id": story_id, "file": file, "result": None})
    save(c)


def mark(c: dict, n: int, result: str) -> None:
    m = c["missions"][n - 1]
    m["status"] = result
    if m["flights"]:
        m["flights"][-1]["result"] = result
    save(c)


def check(c: dict, log_texts: list[str]) -> list[str]:
    """Apply RESULT lines from dcs.log to flights still waiting. Returns what changed."""
    found = {sid: r for text in log_texts for sid, r in RESULT.findall(text)}
    changes = []
    for m in c["missions"]:
        for f in m["flights"]:
            r = found.get(f["story_id"])
            if f["result"] is None and r:
                f["result"] = m["status"] = r
                changes.append(f"Mission {m['n']} ({m['story']}): {'won' if r == 'success' else 'lost'}.")
    if changes:
        save(c)
    return changes


def summary(c: dict) -> dict:
    nxt = current(c)
    won = sum(m["status"] == "success" for m in c["missions"])
    return {**c, "won": won, "total": len(c["missions"]), "next": nxt["n"] if nxt else None, "complete": nxt is None}

"""The air commander: an order in plain language becomes courses of action (frag orders).

Claude reads the campaign as it stands (airfields, enemy sites and the air defence around
them, friendly squadrons with their loadouts, enemy fighters up) plus the player's order,
and proposes several courses of action as structured JSON. Nothing it says is trusted as is:
every frag is rebuilt with war.schedule (squadron, range, loadout, timing) and the odds come
from the war's own model (war.estimate), not from the model's guess.

The AI behind it is a setting (config.json "commander"): "openrouter" (default, DeepSeek V4.1
Flash, key in OPENROUTER_API_KEY) or "anthropic" (Claude Opus 5.5, key in ANTHROPIC_API_KEY).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import ssl
import urllib.error
import urllib.request
import uuid

from . import war
from .campaign import Campaign, _dist, _role_name
from .systems import site_ranges, system_for

PROVIDERS = {"openrouter": "deepseek/deepseek-v4.1-flash", "anthropic": "claude-opus-5-5"}
DEFAULT_PROVIDER = "openrouter"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

SYSTEM = """You are the air component commander for the friendly (blue) side of a DCS World air campaign. \
The player gives you an order in plain language, such as "eliminate the SAM site near Kish" or "capture \
Abu Musa Island". You answer with distinct courses of action they can choose between, each a set of \
frag orders (flights to add to the air tasking order).

How the campaign works:
- Every enemy site belongs to the zone of one enemy airfield. An airfield is captured when 60% of the \
sites in its zone are destroyed, including 60% of its SAM and early-warning radar (ewr) sites; the \
situation gives what each airfield still needs. Destroyed sites are repaired 12-30 hours later, so a \
capture plan should land its strikes within about a day.
- Missions: Strike, SEAD (attack air defences), CAS (armour and troops), CAP (hold a station and \
fight enemy aircraft for an hour), SOF (special forces raid by helicopter: a team is landed 4 km from \
the target, works it for about 40 minutes and is extracted).
- Profiles: "standard"; "low" (low-level: under long-range SAM coverage, but far more exposed to \
short-range air defence and guns); "standoff" (weapons released about 110 km short of the target; \
the flight must carry stand-off weapons such as AGM-86, AGM-154, AGM-84 or AGM-65 - pick the loadout \
that has them); "sof" (only with mission SOF and the special forces squadron).
- Stealth aircraft (F-117A) mostly avoid SAMs and fighters. Heavy bombers (B-1B, B-52H) can destroy \
sites near the aimpoint as well, but are vulnerable over defended targets. Night (time on target \
21:00-05:00 local) weakens enemy fighters.
- A SEAD flight or strike that lands earlier makes the ones after it safer: sequence packages by time \
on target when that matters (e.g. SEAD two minutes before the strike).
- Squadrons marked ai_only can be tasked but the player can't fly them; the others the player can fly \
in DCS, so prefer giving the player at least one flyable flight in an option when it fits.

Rules for your answer:
- Range: a squadron with "can_reach_site_ids" may only be given targets from that list (and CAP \
stations from "cap_stations_in_range"); one with "reaches_all_enemy_sites" can take any target. If no \
squadron of the kind you want can reach the target, use a different kind or leave that option out.
- Use only squadron ids, site ids and airfield names that appear in the situation. Never ask for more \
aircraft than a squadron has available, or targets outside its range.
- Offer 3 to 5 genuinely different options (for example: a stealth night strike, a SEAD-escorted \
package, a low-level attack, a stand-off missile strike, a special forces raid), only where the forces \
for them exist. Give each a short name, the concept in two or three sentences, and the main risk.
- For a capture order, plan enough strikes on that airfield's zone to meet what it still needs, air \
defence first, spread over the next hours.
- Every CAP frag must set cap_station_field to an airfield name from the situation (the one nearest \
the area it protects); leave target_site_id empty for CAP.
- time_on_target: tot_day 0 and tot_time "" for as soon as possible; otherwise the campaign day number \
and "HH:MM" local, which must be later than the current time plus the flight's transit.
- loadout: "" lets the app pick the best stored loadout; otherwise a loadout name exactly as listed.
- If the order is unclear or impossible with what's available, say so in "clarification" and still \
offer the closest useful options. Be brief and concrete; this is a planning tool, not a story."""

_FRAG = {
    "type": "object",
    "properties": {
        "squadron_id": {"type": "string"},
        "mission": {"type": "string", "enum": ["Strike", "SEAD", "CAS", "CAP", "SOF"]},
        "count": {"type": "integer"},
        "target_site_id": {"type": "string", "description": "enemy site id; empty for CAP"},
        "cap_station_field": {"type": "string",
                              "description": "for CAP: the airfield to hold the station over (required); empty otherwise"},
        "loadout": {"type": "string", "description": "stored loadout name, or empty for automatic"},
        "profile": {"type": "string", "enum": list(war.PROFILES)},
        "tot_day": {"type": "integer", "description": "campaign day of the time on target; 0 = asap"},
        "tot_time": {"type": "string", "description": "HH:MM local; empty = asap"},
        "purpose": {"type": "string", "description": "what this flight does in the plan, a few words"},
    },
    "required": ["squadron_id", "mission", "count", "target_site_id", "cap_station_field", "loadout", "profile",
                 "tot_day", "tot_time", "purpose"],
    "additionalProperties": False,
}
SCHEMA = {
    "type": "object",
    "properties": {
        "understanding": {"type": "string", "description": "the order as you understood it, one sentence"},
        "objective_kind": {"type": "string", "enum": ["destroy", "capture", "other"]},
        "objective_site_ids": {"type": "array", "items": {"type": "string"}},
        "objective_field": {"type": "string", "description": "airfield to capture; empty otherwise"},
        "clarification": {"type": "string", "description": "empty unless something is unclear or impossible"},
        "options": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "concept": {"type": "string"},
                    "risk": {"type": "string"},
                    "frags": {"type": "array", "items": _FRAG},
                },
                "required": ["name", "concept", "risk", "frags"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["understanding", "objective_kind", "objective_site_ids", "objective_field", "clarification",
                 "options"],
    "additionalProperties": False,
}


class CommanderError(RuntimeError):
    pass


def schema_for(fields: list[str]) -> dict:
    """SCHEMA with cap_station_field limited to this map's airfield names (or empty)."""
    out = json.loads(json.dumps(SCHEMA))
    out["properties"]["options"]["items"]["properties"]["frags"]["items"]["properties"]["cap_station_field"]["enum"] = \
        [""] + sorted(fields)
    return out


# --- what Claude is told -------------------------------------------------------------------

def _defences(c: Campaign, site: dict) -> dict:
    """The other side's air defence that covers a site, by each system's DCS range."""
    near = {"sams_in_range": [], "short_range_in_range": 0, "radars_that_see_it": 0}
    for s in c.alive_sites(site["side"]):
        if s is site:
            continue
        d = _dist((s["x"], s["y"]), (site["x"], site["y"]))
        reach, see = site_ranges(s)
        if s["role"] == "sam" and reach and d <= reach:
            near["sams_in_range"].append(f"{s['id']} {s.get('system') or system_for(s['side'], 'sam')} ({round(d / 1000)} km)")
        elif s["role"] == "shorad" and reach and d <= reach:
            near["short_range_in_range"] += 1
        elif s["role"] == "ewr" and d <= see:
            near["radars_that_see_it"] += 1
    return near


def _system_info(s: dict) -> dict:
    """What a site is in DCS and how far it reaches (km), for the air defence sites."""
    if s["role"] not in ("sam", "shorad", "ewr"):
        return {}
    reach, see = site_ranges(s)
    out = {"system": s.get("system") or system_for(s["side"], s["role"]), "radar_km": round(see / 1000)}
    if s["role"] != "ewr":
        out["engages_aircraft_within_km"] = round(reach / 1000)
    return out


def situation(c: Campaign, loadouts) -> dict:
    """The campaign as the commander needs it. loadouts(type) -> [(name, [stores])]."""
    w = c.data["war"]
    now = dt.datetime.fromisoformat(w["start"]) + dt.timedelta(seconds=w["clock"])
    blue = [c.data["field_pos"][n] for n in c.fields_of("blue")]
    front = set(c.front_zones("blue"))
    sites = []
    for s in c.alive_sites("red"):
        sites.append({"id": s["id"], "type": re.sub(r"^(an?|the) ", "", _role_name(s)),
                      "role": s["role"], "zone": s["zone"], "front_line": s["zone"] in front,
                      "km_from_nearest_friendly_field": round(min(_dist((s["x"], s["y"]), b) for b in blue) / 1000),
                      "covered_by": _defences(c, s), **_system_info(s)})
    squadrons = []
    for sq in w["squadrons"]:
        if sq["side"] != "blue" or sq["total"] <= 0:
            continue
        reach = (war.SOF_RANGE if "SOF" in sq["missions"] else war.BOMBER_RANGE if sq.get("manual")
                 else war.RANGE["heli" if sq["heli"] else "jet"])
        base = c.data["field_pos"][sq["base"]]
        # What it can reach, worked out here: models don't do the distance arithmetic reliably.
        reachable = [s["id"] for s in c.alive_sites("red") if _dist(base, (s["x"], s["y"])) <= reach]
        entry = {"id": sq["id"], "type": sq["type"], "base": sq["base"], "missions": sq["missions"],
                 "available": war.available(c, sq), "range_km": round(reach / 1000),
                 "ai_only": sq["type"] in war.AI_ONLY, "helicopter": sq["heli"],
                 "loadouts": [{"name": n, "stores": st} for n, st in loadouts(sq["type"])][:14]}
        if len(reachable) == len(c.alive_sites("red")):
            entry["reaches_all_enemy_sites"] = True
        else:
            entry["can_reach_site_ids"] = reachable  # the ONLY targets this squadron may be given
            entry["cap_stations_in_range"] = [n for n, xy in c.data["field_pos"].items() if _dist(base, xy) <= reach]
        squadrons.append(entry)
    caps = []
    for f in w["flights"]:
        if f["side"] == "red" and f["mission"] == "CAP" and f["status"] == "airborne":
            x, y, _ = war.pos_at(f, w["clock"])
            nearest = min(c.data["field_pos"], key=lambda n: _dist(c.data["field_pos"][n], (x, y)))
            caps.append({"type": f["type"], "count": f["count"], "near": nearest})
    return {
        "theatre": c.theatre, "now": war.clock_label(c), "now_local": now.strftime("%H:%M"),
        "day": int(war.clock_label(c).split()[1]), "night_now": war.is_night(c, w["clock"]),
        "airfields": [{"name": n, "side": side, **({"capture_needs": _needs(c, n)} if side == "red" else {})}
                      for n, side in sorted(c.data["fields"].items())],
        "enemy_sites": sites, "friendly_squadrons": squadrons, "enemy_fighters_airborne": caps,
    }


def _needs(c: Campaign, field: str) -> dict:
    z = c.zone_status(field)
    return {"sites_left": z["alive"], "more_to_destroy": max(0, z["needed"] - z["destroyed"]),
            "more_sam_or_ewr_to_destroy": max(0, z["ad_needed"] - z["ad_destroyed"])}


# --- asking Claude ---------------------------------------------------------------------------

def ask(order: str, sit: dict, picked_site: dict | None = None, provider: str = DEFAULT_PROVIDER,
        model: str = "") -> dict:
    """Send the order and situation to the commander's AI; returns the parsed plan (unvalidated)."""
    text = f"Situation:\n{json.dumps(sit, separators=(',', ':'))}\n\n"
    if picked_site:
        text += f"The player has selected enemy site {picked_site['id']} ({picked_site['role']} near " \
                f"{picked_site['zone']}) on the map.\n\n"
    text += f"Order: {order}"
    if provider not in PROVIDERS:
        raise CommanderError(f"Unknown commander provider {provider!r} in config.json (use "
                             f"{' or '.join(PROVIDERS)}).")
    model = model or PROVIDERS[provider]
    schema = schema_for([a["name"] for a in sit.get("airfields", [])])
    if provider == "openrouter":
        return _ask_openrouter(text, model, schema)
    return _ask_anthropic(text, model, schema)


def _ask_openrouter(text: str, model: str, schema: dict) -> dict:
    """OpenRouter's chat completions API with a strict JSON schema, routed only to endpoints
    that support it (provider.require_parameters)."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise CommanderError("No OpenRouter key. Set OPENROUTER_API_KEY as a Windows user environment "
                             "variable, then restart Mission Builder.")
    body = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": text}],
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "courses_of_action", "strict": True, "schema": schema}},
        "provider": {"require_parameters": True},
        # The model's reasoning counts against this, and a capture plan on a big map has many
        # frags: 16,000 cut answers off. At DeepSeek V4.1 Flash's price 40,000 is under 2 cents.
        "max_tokens": 40000,
    }
    req = urllib.request.Request(OPENROUTER_URL, data=json.dumps(body).encode("utf-8"), method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json",
        "HTTP-Referer": "http://127.0.0.1:8765", "X-Title": "DCS Mission Builder"})
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=240, context=ctx) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8")).get("error", {}).get("message", "")
        except (ValueError, AttributeError):
            detail = ""
        reason = {401: "the OpenRouter key was rejected (check OPENROUTER_API_KEY)",
                  402: "the OpenRouter account is out of credits",
                  429: "OpenRouter rate limit reached; try again in a minute"}.get(e.code, f"OpenRouter error {e.code}")
        raise CommanderError(f"The commander couldn't answer: {reason}. {detail}".strip()) from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise CommanderError("Couldn't reach OpenRouter. Check the internet connection.") from e
    if data.get("error"):
        raise CommanderError(f"OpenRouter: {data['error'].get('message', data['error'])}")
    choice = (data.get("choices") or [{}])[0]
    if choice.get("error"):
        raise CommanderError(f"The model's provider failed: {choice['error'].get('message', '')}")
    if choice.get("finish_reason") == "length":
        raise CommanderError("The commander's answer was cut off. Try a narrower order.")
    if choice.get("finish_reason") == "content_filter":
        raise CommanderError("The commander declined that order.")
    plan = _parse((choice.get("message") or {}).get("content") or "")
    u = data.get("usage") or {}
    plan["usage"] = {"input_tokens": u.get("prompt_tokens"), "output_tokens": u.get("completion_tokens"),
                     "reasoning_tokens": (u.get("completion_tokens_details") or {}).get("reasoning_tokens"),
                     "model": data.get("model", model), "cost": u.get("cost")}
    return plan


def _parse(body: str) -> dict:
    body = body.strip()
    if body.startswith("```"):  # some providers wrap JSON in a code fence despite the schema
        body = body.strip("`").removeprefix("json").strip()
    try:
        plan = json.loads(body)
    except json.JSONDecodeError as e:
        raise CommanderError("The commander's answer wasn't readable. Try again.") from e
    missing = [k for k in SCHEMA["required"] if k not in plan]
    if missing:
        raise CommanderError(f"The commander's answer was missing {', '.join(missing)}. Try again.")
    return plan


def _ask_anthropic(text: str, model: str, schema: dict) -> dict:
    try:
        import anthropic
    except ImportError as e:
        raise CommanderError("The anthropic package isn't installed in Mission Builder's .venv.") from e
    try:
        client = anthropic.Anthropic()
        response = client.beta.messages.create(
            model=model,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            thinking={"type": "adaptive"},
            output_config={"effort": "high", "format": {"type": "json_schema", "schema": schema}},
            system=SYSTEM,
            messages=[{"role": "user", "content": text}],
        )
    except anthropic.AuthenticationError as e:
        raise CommanderError("Claude API credentials were rejected. Check ANTHROPIC_API_KEY.") from e
    except anthropic.PermissionDeniedError as e:
        raise CommanderError(f"The Claude API key isn't allowed to do this: {e.message}") from e
    except anthropic.RateLimitError as e:
        raise CommanderError("Claude API rate limit reached. Try again in a minute.") from e
    except anthropic.BadRequestError as e:
        raise CommanderError(f"The Claude API rejected the request: {e.message}") from e
    except anthropic.APIStatusError as e:
        raise CommanderError(f"Claude API error {e.status_code}. Try again shortly.") from e
    except anthropic.APIConnectionError as e:
        raise CommanderError("Couldn't reach the Claude API. Check the internet connection.") from e
    except Exception as e:  # missing credentials surface here as a plain TypeError from the client
        if "api_key" in str(e) or "auth" in str(e).lower():
            raise CommanderError("No Claude API credentials. Set ANTHROPIC_API_KEY (see the README), "
                                 "then restart Mission Builder.") from e
        raise
    if response.stop_reason == "refusal":
        raise CommanderError("The commander declined that order.")
    if response.stop_reason == "max_tokens":
        raise CommanderError("The commander's answer was cut off. Try a narrower order.")
    plan = _parse(next((b.text for b in response.content if b.type == "text"), ""))
    plan["usage"] = {"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens,
                     "model": response.model}
    return plan


# --- checking the answer against the campaign -------------------------------------------------

def _tot_seconds(c: Campaign, day: int, hhmm: str) -> float | None:
    if not day or not hhmm:
        return None
    start = dt.datetime.fromisoformat(c.data["war"]["start"])
    hh, mm = (int(v) for v in hhmm.split(":")[:2])
    when = dt.datetime.combine(start.date() + dt.timedelta(days=day - 1), dt.time(hh % 24, mm % 60))
    return (when - start).total_seconds()


def _field_named(c: Campaign, name: str) -> str | None:
    """An airfield by name, forgiving case and partial names ("kish" -> "Kish Intl")."""
    name = (name or "").strip().lower()
    if not name:
        return None
    exact = next((n for n in c.data["field_pos"] if n.lower() == name), None)
    return exact or next((n for n in c.data["field_pos"] if name in n.lower() or n.lower() in name), None)


def _default_station(c: Campaign, frag: dict, objective: dict | None):
    """Where a CAP the model didn't place should go: over its target, else over the plan's
    objective, else the usual station over the front. Returns ([x, y], why)."""
    by_id = {s["id"]: s for s in c.data["sites"]}
    if frag.get("target_site_id") in by_id:
        s = by_id[frag["target_site_id"]]
        return [s["x"], s["y"]], f"over its target near {s['zone']}"
    if objective and objective.get("field"):
        return c.data["field_pos"][objective["field"]], f"over {objective['field']}"
    if objective and objective.get("sites") and objective["sites"][0] in by_id:
        s = by_id[objective["sites"][0]]
        return [s["x"], s["y"]], f"over the objective near {s['zone']}"
    front = c.front_zones("blue")
    own = c.fields_of("blue")
    if front and own:
        tgt = c.data["field_pos"][front[0]]
        mine = min(own, key=lambda n: _dist(c.data["field_pos"][n], tgt))
        return war._toward(c.data["field_pos"][mine], tgt, 0.4 * _dist(c.data["field_pos"][mine], tgt)), "over the front"
    return None, ""


def build_frag(c: Campaign, frag: dict, commit: bool) -> dict:
    """One frag order as a flight (scheduled when commit), or raises RuntimeError with the reason."""
    station = None
    if frag["mission"] == "CAP":
        if frag.get("cap_station_xy"):
            station = frag["cap_station_xy"]
        else:
            name = _field_named(c, frag["cap_station_field"])
            if not name:
                raise RuntimeError(f"no CAP station given ({frag['cap_station_field']!r} isn't an airfield)")
            station = c.data["field_pos"][name]
    tot = _tot_seconds(c, frag["tot_day"], frag["tot_time"])
    return war.schedule(c, frag["squadron_id"], frag["mission"], frag["count"], tot,
                        frag["target_site_id"] or None, station, frag["loadout"] or "",
                        frag["profile"] if frag["mission"] != "SOF" else "sof", commit=commit)


def evaluate(c: Campaign, plan: dict) -> dict:
    """Validate every frag and estimate each option's odds with the war model."""
    sq_type = {sq["id"]: sq["type"] for sq in c.data["war"]["squadrons"]}
    objective = None
    if plan["objective_kind"] == "destroy" and plan["objective_site_ids"]:
        ids = [i for i in plan["objective_site_ids"] if any(s["id"] == i and s["alive"] for s in c.data["sites"])]
        objective = {"sites": ids} if ids else None
    elif plan["objective_kind"] == "capture" and plan["objective_field"] in c.data["fields"]:
        objective = {"field": plan["objective_field"]}
    options = []
    for opt in plan["options"]:
        flights, frags = [], []
        for frag in opt["frags"]:
            frag = dict(frag)
            note = ""
            if frag["mission"] == "CAP" and not _field_named(c, frag["cap_station_field"]):
                xy, why = _default_station(c, frag, objective)
                if xy:  # the model left the station out: place it sensibly rather than drop the CAP
                    frag["cap_station_xy"], frag["target_site_id"] = [round(xy[0]), round(xy[1])], ""
                    note = f" (station chosen by the app: {why})"
            row = {"spec": frag, "purpose": frag["purpose"] + note, "type": sq_type.get(frag["squadron_id"], "?"),
                   "count": frag["count"], "mission": frag["mission"], "profile": frag["profile"]}
            try:
                f = build_frag(c, frag, commit=False)
                f["id"] = f"plan-{len(flights)}"
                flights.append(f)
                row.update(id=f["id"], target=f.get("target_name", ""), takeoff=war.clock_label(c, f["takeoff"]),
                           on_target=war.clock_label(c, f.get("tot") or f.get("station")), night=f["night"],
                           loadout=f.get("loadout", ""), stores=f.get("stores", []),
                           flyable=f["type"] not in war.AI_ONLY)
            except RuntimeError as e:
                row["error"] = str(e)
            frags.append(row)
        est = war.estimate(c, flights, objective) if flights else None
        for row in frags:
            if est and row.get("id") in est["flights"]:
                row["hit"] = round(est["flights"][row["id"]]["hit"], 2)
                row["losses"] = round(est["flights"][row["id"]]["losses"], 2)
        options.append({"name": opt["name"], "concept": opt["concept"], "risk": opt["risk"], "frags": frags,
                        "valid": sum(1 for r in frags if "error" not in r),
                        "objective": round(est["objective"], 2) if est and est["objective"] is not None else None,
                        "expected_losses": round(est["expected_losses"], 1) if est else None})
    return {"id": uuid.uuid4().hex[:8], "understanding": plan["understanding"], "clarification": plan["clarification"],
            "objective_kind": plan["objective_kind"], "objective": objective, "options": options,
            "usage": plan.get("usage")}


def execute(c: Campaign, option: dict) -> tuple[list[dict], list[str]]:
    """Frag every valid order of an option onto the ATO. Returns (flights, problems)."""
    done, problems = [], []
    for row in option["frags"]:
        if "error" in row:
            continue
        try:
            done.append(build_frag(c, row["spec"], commit=True))
        except RuntimeError as e:
            problems.append(f"{row['type']} {row['mission']}: {e}")
    return done, problems

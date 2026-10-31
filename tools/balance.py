"""War balance: run the Gulf (or another map) for N days without a player, several seeds.
    python tools/balance.py [runs] [days] [theatre] [NAME=value ...]
NAME=value overrides a constant in war.py for the run (blue_attacks / red_attacks: ATTACKS_PER_PLAN).
Uses temporary campaign files; never touches campaign/."""
import logging
import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
logging.disable(logging.ERROR)
from mission_builder import campaign, server, war  # noqa: E402

runs = int(sys.argv[1]) if len(sys.argv) > 1 else 5
days = int(sys.argv[2]) if len(sys.argv) > 2 else 10
theatre = sys.argv[3] if len(sys.argv) > 3 else "PersianGulf"
for arg in sys.argv[4:]:
    name, value = arg.split("=")
    if name in ("blue_attacks", "red_attacks"):
        war.ATTACKS_PER_PLAN[name.split("_")[0]] = float(value)
    else:
        setattr(war, name, type(getattr(war, name))(eval(value)))
server.save_config = lambda c: None
for seed in range(runs):
    random.seed(seed)
    tmp = Path(tempfile.mkdtemp())
    campaign.STATE_DIR = tmp / "campaign"
    config = server.load_config()
    config.update(output_dir=str(tmp / "out"), dcs_logs=str(tmp / "logs"))
    st = server.State(config)
    c = st.campaigns[theatre]
    w = c.data["war"]
    t = w["clock"]
    start = len(c.fields_of("blue"))
    held = []
    while t < days * 86400:
        t += 1800
        war.advance(c, t, random.Random(seed * 100000 + int(t)))
        if t % 86400 == 0:
            held.append(len(c.fields_of("blue")))
    ev = [e["text"] for e in c.data["events"]]
    caps = [x for x in ev if "forces captured" in x]
    m = w.get("missiles", {})
    print(f"RUN seed {seed}: blue {len(c.fields_of('blue'))} red {len(c.fields_of('red'))} (blue started {start}) | "
          f"blue fields by day {held} | lost B{w['stats']['blue_lost']} R{w['stats']['red_lost']} | "
          f"captures {len(caps)}, first {caps[0].split(' · ')[0] if caps else '-'} | missiles {m.get('total')} "
          f"down {m.get('down')} hits {m.get('hits')}", flush=True)

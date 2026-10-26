"""Tkinter front end: pick an aircraft you own, a map and a mission type, get a .miz."""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
SAVED_GAMES = Path(os.path.expandvars(r"%USERPROFILE%\Saved Games"))
DEFAULT_CONFIG = {
    "dcs_dir": r"F:\DCS World",
    "output_dir": str(SAVED_GAMES / "DCS" / "Missions" / "Generated"),
    "liberation_payloads": str(SAVED_GAMES / "dcs_liberation" / "resources" / "customized_payloads"),
    "last": {},
}


def load_config() -> dict:
    config = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        config.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    return config


def save_config(config: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")


class App(tk.Tk):
    def __init__(self, config: dict):
        super().__init__()
        self.config_data = config
        self.title("DCS Mission Builder")
        self.resizable(False, False)

        # pydcs must be configured before anything touches unit payloads.
        from .inventory import configure_payloads, installed_terrains, owned_aircraft
        dcs_dir = Path(config["dcs_dir"])
        configure_payloads(dcs_dir, Path(config["liberation_payloads"]))
        from . import generator
        self.gen = generator

        self.aircraft = [a.type_id for a in owned_aircraft(dcs_dir)]
        self.terrains = installed_terrains(dcs_dir)
        if not self.aircraft or not self.terrains:
            messagebox.showerror("DCS Mission Builder", f"No owned aircraft or terrains found under {dcs_dir}.")
            self.destroy()
            return

        last = config.get("last", {})
        self.v = {
            "aircraft": tk.StringVar(value=last.get("aircraft", self.aircraft[0])),
            "terrain": tk.StringVar(value=last.get("terrain", self.terrains[0])),
            "airport": tk.StringVar(value=last.get("airport", "")),
            "mission_type": tk.StringVar(value=last.get("mission_type", "CAP")),
            "start": tk.StringVar(value=last.get("start", "Runway (engines running)")),
            "flight_size": tk.IntVar(value=last.get("flight_size", 2)),
            "date": tk.StringVar(value=last.get("date", "2012-06-01")),
            "hour": tk.IntVar(value=last.get("hour", 9)),
            "weather": tk.StringVar(value=last.get("weather", "Clear")),
            "enemy_skill": tk.StringVar(value=last.get("enemy_skill", "Good")),
            "awacs": tk.BooleanVar(value=last.get("awacs", True)),
            "tanker": tk.BooleanVar(value=last.get("tanker", False)),
            "output_dir": tk.StringVar(value=config["output_dir"]),
        }

        form = ttk.Frame(self, padding=12)
        form.grid(sticky="nsew")
        row = 0

        def add(label, widget):
            nonlocal row
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=3, padx=(0, 10))
            widget.grid(row=row, column=1, sticky="ew", pady=3)
            row += 1
            return widget

        def combo(var, values, width=34):
            return ttk.Combobox(form, textvariable=var, values=values, state="readonly", width=width)

        add("Aircraft", combo(self.v["aircraft"], self.aircraft))
        terrain_box = add("Map", combo(self.v["terrain"], self.terrains))
        self.airport_box = add("Home airfield", combo(self.v["airport"], []))
        add("Mission", combo(self.v["mission_type"], generator.MISSION_TYPES))
        add("Start", combo(self.v["start"], list(generator.START_TYPES)))
        add("Flight size", ttk.Spinbox(form, from_=1, to=4, textvariable=self.v["flight_size"], width=5, state="readonly"))
        add("Date (YYYY-MM-DD)", ttk.Entry(form, textvariable=self.v["date"], width=12))
        add("Start hour (0-23)", ttk.Spinbox(form, from_=0, to=23, textvariable=self.v["hour"], width=5, state="readonly"))
        add("Weather", combo(self.v["weather"], list(generator.WEATHER)))
        add("Enemy skill", combo(self.v["enemy_skill"], list(generator.SKILLS)))
        add("Support", ttk.Checkbutton(form, text="Friendly AWACS", variable=self.v["awacs"]))
        add("", ttk.Checkbutton(form, text="Tanker (matched to your refuelling system)", variable=self.v["tanker"]))

        out = ttk.Frame(form)
        ttk.Entry(out, textvariable=self.v["output_dir"], width=30).pack(side="left", fill="x", expand=True)
        ttk.Button(out, text="…", width=3, command=self.pick_output).pack(side="left", padx=(4, 0))
        add("Save to", out)

        ttk.Button(form, text="Generate mission", command=self.generate).grid(row=row, column=0, columnspan=2,
                                                                             sticky="ew", pady=(10, 4))
        row += 1
        self.status = tk.Text(form, width=60, height=9, wrap="word", state="disabled")
        self.status.grid(row=row, column=0, columnspan=2, sticky="ew")

        terrain_box.bind("<<ComboboxSelected>>", lambda _e: self.refresh_airports())
        self.refresh_airports(keep=self.v["airport"].get())

    def refresh_airports(self, keep: str = "") -> None:
        names = self.gen.airports(self.v["terrain"].get())
        self.airport_box["values"] = names
        self.v["airport"].set(keep if keep in names else names[0])

    def pick_output(self) -> None:
        folder = filedialog.askdirectory(initialdir=self.v["output_dir"].get())
        if folder:
            self.v["output_dir"].set(folder)

    def show(self, text: str) -> None:
        self.status.configure(state="normal")
        self.status.delete("1.0", "end")
        self.status.insert("1.0", text)
        self.status.configure(state="disabled")

    def generate(self) -> None:
        try:
            date = dt.date.fromisoformat(self.v["date"].get().strip())
        except ValueError:
            messagebox.showerror("DCS Mission Builder", "Date must look like 2012-06-01.")
            return
        values = {k: var.get() for k, var in self.v.items()}
        spec = self.gen.MissionSpec(
            terrain=values["terrain"], aircraft=values["aircraft"], home_airport=values["airport"],
            mission_type=values["mission_type"], start=values["start"], flight_size=int(values["flight_size"]),
            date=date, hour=int(values["hour"]), weather=values["weather"], enemy_skill=values["enemy_skill"],
            awacs=values["awacs"], tanker=values["tanker"])
        self.show("Generating…")
        self.update_idletasks()
        try:
            mission = self.gen.build(spec)
            out_dir = Path(values["output_dir"])
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / self.gen.default_filename(spec)
            mission.save(str(path))
        except Exception as e:  # show any pydcs failure instead of dying silently
            logging.exception("generation failed")
            self.show(f"Failed: {type(e).__name__}: {e}")
            return
        self.config_data["output_dir"] = values["output_dir"]
        self.config_data["last"] = {k: (v.isoformat() if isinstance(v, dt.date) else v)
                                    for k, v in values.items() if k != "output_dir"}
        save_config(self.config_data)
        self.show(f"Saved {path}\n\n{mission.description_bluetask_text()}")


def main() -> None:
    logging.basicConfig(filename=ROOT / "mission_builder.log", level=logging.INFO)
    # pydcs logs every stock livery it can't parse at ERROR; those are harmless.
    logging.getLogger().addFilter(lambda r: "Could not parse livery" not in r.getMessage())
    app = App(load_config())
    if app.winfo_exists():
        app.mainloop()


if __name__ == "__main__":
    main()

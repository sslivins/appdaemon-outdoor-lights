"""Outdoor Lights Off - AppDaemon app.

Turns off a configurable set of outdoor lights/switches at a scheduled time
each night, but defers the shut-off while someone is still outside. "Outside"
is detected by two configurable signals:

  1. A phone belonging to a household member is connected to the backyard
     access point (matched by the device_tracker's ``ap_mac`` attribute).
  2. One or more door sensors (e.g. the kitchen sliding doors) are open.

While either signal is active at/after the scheduled off-time, the lights stay
on. As soon as everyone has gone inside (and all watched doors are closed) the
lights are turned off. A short debounce avoids flapping when a phone briefly
drops off the AP.

No entity IDs are hardcoded - everything comes from the app's YAML config.
"""

import appdaemon.plugins.hass.hassapi as hass
from datetime import timedelta


class OutdoorLightsOff(hass.Hass):

    def initialize(self):
        # --- Schedule ---------------------------------------------------
        self.off_time = self.parse_time(self.args.get("off_time", "22:00:00"))
        self.reset_time = self.parse_time(self.args.get("reset_time", "04:00:00"))
        self.clear_delay = int(self.args.get("clear_delay_seconds", 120))
        self.poll_interval = int(self.args.get("poll_interval_seconds", 60))

        # --- Entities to turn off (lights and/or switches) --------------
        self.lights = self._as_list(self.args.get("lights", []))

        # --- Presence configuration -------------------------------------
        presence = self.args.get("presence", {}) or {}

        self.door_sensors = []
        for d in self._as_list(presence.get("door_sensors", [])):
            if isinstance(d, dict):
                self.door_sensors.append({
                    "entity": d["entity"],
                    "open_state": str(d.get("open_state", "on")),
                })
            else:  # bare entity id string
                self.door_sensors.append({"entity": d, "open_state": "on"})

        ap = presence.get("ap_presence", {}) or {}
        self.ap_mac = str(ap.get("ap_mac", "")).lower()
        self.person_entities = self._as_list(ap.get("person_entities", []))

        # --- Optional AP-presence debug scan ----------------------------
        self.ap_debug = bool(self.args.get("ap_debug", False))

        # --- Runtime state ----------------------------------------------
        self.armed = False          # True between off_time and reset_time
        self._pending_handle = None  # debounce timer handle

        # --- Wiring ------------------------------------------------------
        self.run_daily(self._on_off_time, self.off_time)
        self.run_daily(self._on_reset_time, self.reset_time)

        # React immediately when a watched door changes.
        for d in self.door_sensors:
            self.listen_state(self._on_presence_change, d["entity"])

        # Safety-net poll while armed (covers AP presence, which we sample
        # rather than subscribe to since it is derived from many trackers).
        self.run_every(self._poll, self.datetime() + timedelta(seconds=self.poll_interval),
                       self.poll_interval)

        # Optional diagnostic: continuously log who is seen on the backyard AP
        # (runs regardless of arm state so you can test by walking outside).
        if self.ap_debug:
            interval = int(self.args.get("ap_debug_interval_seconds", 20))
            self.run_every(self._ap_scan_debug,
                           self.datetime() + timedelta(seconds=2), interval)
            self.log("AP debug scan enabled (every %ds)." % interval)

        # If AppDaemon (re)starts after the off-time but before the reset
        # time, arm immediately so a restart doesn't skip the night.
        if self._within_off_window():
            self.armed = True
            self.log("Started within the off-window - arming immediately.")
            self._evaluate("startup")

        self.log(
            "Initialized. off_time=%s reset_time=%s lights=%d door_sensors=%d "
            "persons=%d ap_mac=%s"
            % (self.off_time, self.reset_time, len(self.lights),
               len(self.door_sensors), len(self.person_entities),
               self.ap_mac or "<none>")
        )

    # ------------------------------------------------------------------ #
    # Scheduling callbacks
    # ------------------------------------------------------------------ #
    def _on_off_time(self, kwargs):
        self.armed = True
        self.log("Off-time reached - arming.")
        self._evaluate("off_time")

    def _on_reset_time(self, kwargs):
        # Hard failsafe: unconditionally force everything off at reset time,
        # regardless of presence or reported state. This replaces the old
        # standalone "4am" HA automation.
        self.log("Reset-time reached - forcing all outdoor lights off (failsafe).")
        self._force_off_all()
        self.armed = False
        self._cancel_pending()

    def _poll(self, kwargs):
        if self.armed:
            self._evaluate("poll")

    def _on_presence_change(self, entity, attribute, old, new, kwargs):
        if self.armed:
            self.log("%s changed %s -> %s; re-evaluating." % (entity, old, new))
            self._evaluate("door")

    # ------------------------------------------------------------------ #
    # Core logic
    # ------------------------------------------------------------------ #
    def _evaluate(self, reason):
        """Decide whether to turn the lights off now."""
        if not self.armed:
            return

        if self.someone_outside():
            # Still occupied - cancel any pending shut-off and wait.
            if self._pending_handle is not None:
                self.log("Someone came back outside - cancelling pending shut-off.")
                self._cancel_pending()
            return

        # Nobody outside. Debounce briefly before turning off, in case a
        # phone momentarily dropped off the AP.
        if self.clear_delay > 0:
            if self._pending_handle is None:
                self.log("No one outside (%s). Will shut off in %ds if it stays clear."
                         % (reason, self.clear_delay))
                self._pending_handle = self.run_in(self._confirm_and_shutoff,
                                                   self.clear_delay)
        else:
            self._shutoff()

    def _confirm_and_shutoff(self, kwargs):
        self._pending_handle = None
        if not self.armed:
            return
        if self.someone_outside():
            self.log("Someone outside again at confirm time - staying on.")
            return
        self._shutoff()

    def _shutoff(self):
        self._cancel_pending()
        turned = []
        for entity in self.lights:
            try:
                if self.get_state(entity) == "on":
                    self.turn_off(entity)
                    turned.append(entity)
            except Exception as e:  # noqa: BLE001 - log and continue
                self.log("Failed to turn off %s: %s" % (entity, e), level="WARNING")
        if turned:
            self.log("Turned off: %s" % ", ".join(turned))
        else:
            self.log("Nothing to turn off (all already off).")
        # Done for the night until the next off-time re-arms us.
        self.armed = False

    def _force_off_all(self):
        """Unconditionally turn off every configured entity (failsafe)."""
        for entity in self.lights:
            try:
                self.turn_off(entity)
            except Exception as e:  # noqa: BLE001 - log and continue
                self.log("Failsafe failed to turn off %s: %s" % (entity, e),
                         level="WARNING")
        self.log("Failsafe off issued for: %s" % ", ".join(self.lights))

    # ------------------------------------------------------------------ #
    # Presence detection
    # ------------------------------------------------------------------ #
    def someone_outside(self):
        return self._any_door_open() or self._person_on_ap()

    def _any_door_open(self):
        for d in self.door_sensors:
            if self.get_state(d["entity"]) == d["open_state"]:
                return True
        return False

    def _person_on_ap(self):
        if not self.ap_mac or not self.person_entities:
            return False
        for person in self.person_entities:
            trackers = self.get_state(person, attribute="device_trackers") or []
            for tracker in trackers:
                mac = self.get_state(tracker, attribute="ap_mac")
                if mac and str(mac).lower() == self.ap_mac:
                    return True
        return False

    def _ap_scan_debug(self, kwargs):
        """Log which household devices are currently on the target AP."""
        matched = []
        for person in self.person_entities:
            trackers = self.get_state(person, attribute="device_trackers") or []
            for tracker in trackers:
                mac = self.get_state(tracker, attribute="ap_mac")
                if mac and str(mac).lower() == self.ap_mac:
                    matched.append("%s (%s)" % (person, tracker))
        open_doors = [d["entity"] for d in self.door_sensors
                      if self.get_state(d["entity"]) == d["open_state"]]
        self.log("[ap-scan] on backyard AP (%s): %s | open doors: %s | someone_outside=%s"
                 % (self.ap_mac,
                    ", ".join(matched) if matched else "none",
                    ", ".join(open_doors) if open_doors else "none",
                    self.someone_outside()))

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _within_off_window(self):
        now = self.time()
        if self.off_time <= self.reset_time:
            return self.off_time <= now < self.reset_time
        # Window wraps past midnight (e.g. 22:00 -> 04:00).
        return now >= self.off_time or now < self.reset_time

    def _cancel_pending(self):
        if self._pending_handle is not None:
            try:
                self.cancel_timer(self._pending_handle)
            except Exception:  # noqa: BLE001
                pass
            self._pending_handle = None

    @staticmethod
    def _as_list(value):
        if value is None:
            return []
        return value if isinstance(value, list) else [value]

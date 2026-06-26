"""Outdoor Lights Off - AppDaemon app (controller-backed).

Nightly sweep that turns off a configurable set of outdoor lights/switches once
everyone has gone inside, deferring while someone is still outside. "Outside" is
detected via the shared :class:`home_lib.PresenceMixin` (a household phone on the
backyard AP, or any watched door open).

This version routes its decisions through the ``device_controller`` app instead
of calling ``turn_off`` itself, so it can't fight the door-courtesy app over a
shared light:

* While someone is outside, it publishes an **indefinite presence hold** on every
  managed light. That hold blocks *any* controller off (including a courtesy-off)
  until everyone is back inside - implementing "courtesy timing wins, and nobody
  is left in the dark while outside."
* When everyone is inside (after a short debounce), it releases the presence hold
  and issues a controller **off-request** (with a TTL). The controller then turns
  the light off only if no other hold (e.g. an active courtesy hold) remains.
* At ``reset_time`` it issues an unconditional ``force_off`` failsafe.

No entity IDs are hardcoded - everything comes from the app's YAML config.
"""

import appdaemon.plugins.hass.hassapi as hass
from datetime import timedelta

from home_lib import PresenceMixin, ControllerClient


class OutdoorLightsOff(PresenceMixin, hass.Hass):

    def initialize(self):
        # --- Schedule ---------------------------------------------------
        self.off_time = self.parse_time(self.args.get("off_time", "22:00:00"))
        self.reset_time = self.parse_time(self.args.get("reset_time", "04:00:00"))
        self.clear_delay = int(self.args.get("clear_delay_seconds", 120))
        self.poll_interval = int(self.args.get("poll_interval_seconds", 60))
        # How long a sweep off-request stays valid while waiting out a courtesy
        # hold before it is considered stale (default 6h, comfortably past the
        # 04:00 failsafe).
        self.off_valid_seconds = int(self.args.get("off_valid_seconds", 21600))

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
        # A single AP reference: friendly name (ap_name), entity id (ap_entity),
        # or a literal MAC (ap_mac). The mixin auto-detects and resolves it.
        self.ap = (ap.get("ap_mac") or ap.get("ap_entity")
                   or ap.get("ap_name") or "")
        self.person_entities = self._as_list(ap.get("person_entities", []))

        # --- Optional AP-presence debug scan ----------------------------
        self.ap_debug = bool(self.args.get("ap_debug", False))

        # --- Controller client ------------------------------------------
        self.client = ControllerClient(
            self, self.args.get("controller", "device_controller")
        )

        # --- Runtime state ----------------------------------------------
        self.armed = False           # True between off_time and reset_time
        self._pending_handle = None  # debounce timer handle
        self._presence_held = False  # whether sweep:presence holds are placed

        # --- Wiring ------------------------------------------------------
        self.run_daily(self._on_off_time, self.off_time)
        self.run_daily(self._on_reset_time, self.reset_time)

        for d in self.door_sensors:
            self.listen_state(self._on_presence_change, d["entity"])

        self.run_every(self._poll,
                       self.datetime() + timedelta(seconds=self.poll_interval),
                       self.poll_interval)

        if self.ap_debug:
            interval = int(self.args.get("ap_debug_interval_seconds", 20))
            self.run_every(self._ap_scan_debug,
                           self.datetime() + timedelta(seconds=2), interval)
            self.log("AP debug scan enabled (every %ds)." % interval)

        # Clear any presence holds left by a previous instance of this app (the
        # controller keeps running across our reloads, so it may still hold our
        # "sweep:presence" id). We re-place them below only if we start armed.
        for entity in self.lights:
            self.client.release(entity, "sweep:presence")
        self._presence_held = False

        # The sweep only acts inside the off-window. If AppDaemon (re)starts
        # within it, arm immediately and (re-)assert presence holds via
        # _evaluate (the controller drops indefinite holds across a restart).
        # Outside the window the sweep holds nothing.
        if self._within_off_window():
            self.armed = True
            self.log("Started within the off-window - arming immediately.")
            self._evaluate("startup")

        self.log(
            "Initialized. off_time=%s reset_time=%s lights=%d door_sensors=%d "
            "persons=%d ap=%s"
            % (self.off_time, self.reset_time, len(self.lights),
               len(self.door_sensors), len(self.person_entities),
               self.ap or "<none>")
        )

    # ------------------------------------------------------------------ #
    # Scheduling callbacks
    # ------------------------------------------------------------------ #
    def _on_off_time(self, kwargs):
        self.armed = True
        self.log("Off-time reached - arming.")
        self._evaluate("off_time")

    def _on_reset_time(self, kwargs):
        # Hard failsafe: unconditionally force everything off, clearing any
        # holds/offs in the controller. Replaces the old standalone 4am HA
        # automation.
        self.log("Reset-time reached - forcing all outdoor lights off (failsafe).")
        for entity in self.lights:
            self.client.force_off(entity)
        self.armed = False
        self._presence_held = False
        self._cancel_pending()

    def _poll(self, kwargs):
        # The sweep is an "off only" app: it manages presence holds and
        # off-requests ONLY while armed (inside the off-window). Outside the
        # window it holds nothing - courtesy/manual control own the lights then.
        if self.armed:
            self._evaluate("poll")
        else:
            self._release_presence_holds()

    def _on_presence_change(self, entity, attribute, old, new, kwargs):
        # Presence only matters while armed; outside the off-window the sweep
        # ignores doors/AP entirely.
        if not self.armed:
            return
        self.log("%s changed %s -> %s; re-evaluating." % (entity, old, new))
        self._evaluate("door")

    # ------------------------------------------------------------------ #
    # Core logic
    # ------------------------------------------------------------------ #
    def _refresh_presence_holds(self):
        """Add/remove an indefinite presence hold on every managed light to
        match whether someone is currently outside. Only meaningful while armed;
        callers must not invoke this outside the off-window."""
        outside = self.someone_outside()
        if outside and not self._presence_held:
            for entity in self.lights:
                self.client.hold(entity, "sweep:presence", until=None,
                                 source="sweep")
            self._presence_held = True
            self.log("Someone outside - presence hold placed on managed lights.")
        elif not outside and self._presence_held:
            for entity in self.lights:
                self.client.release(entity, "sweep:presence")
            self._presence_held = False
            self.log("All clear - presence hold released.")

    def _release_presence_holds(self):
        """Drop any sweep presence holds. Used when leaving (or being loaded
        outside) the off-window, where the sweep must hold nothing."""
        if self._presence_held:
            for entity in self.lights:
                self.client.release(entity, "sweep:presence")
            self._presence_held = False
            self.log("Outside off-window - presence holds released.")

    def _evaluate(self, reason):
        """Decide whether to request the lights off now. No-op when not armed."""
        if not self.armed:
            return
        self._refresh_presence_holds()

        if self.someone_outside():
            if self._pending_handle is not None:
                self.log("Someone still outside - cancelling pending shut-off.")
                self._cancel_pending()
            return

        # Nobody outside. Debounce briefly before requesting off, in case a
        # phone momentarily dropped off the AP.
        if self.clear_delay > 0:
            if self._pending_handle is None:
                self.log("No one outside (%s). Will request off in %ds if it "
                         "stays clear." % (reason, self.clear_delay))
                self._pending_handle = self.run_in(self._confirm_and_request_off,
                                                   self.clear_delay)
        else:
            self._request_off_all()

    def _confirm_and_request_off(self, kwargs):
        self._pending_handle = None
        if not self.armed:
            return
        self._refresh_presence_holds()
        if self.someone_outside():
            self.log("Someone outside again at confirm time - staying on.")
            return
        self._request_off_all()

    def _request_off_all(self):
        """Hand the off decision to the controller for every managed light, then
        disarm for the night. The controller turns each light off only once no
        hold (e.g. an active courtesy hold) remains."""
        self._cancel_pending()
        for entity in self.lights:
            self.client.request_off(entity, "sweep",
                                    valid_for=self.off_valid_seconds,
                                    source="sweep")
        self.log("Requested off via controller: %s" % ", ".join(self.lights))
        # Done for the night until the next off-time re-arms us.
        self.armed = False

    # ------------------------------------------------------------------ #
    # Diagnostics
    # ------------------------------------------------------------------ #
    def _ap_scan_debug(self, kwargs):
        """Log which household devices are currently on the target AP."""
        ap_mac = self.resolve_ap_mac(self.ap)
        matched = []
        for person in self.person_entities:
            trackers = self.get_state(person, attribute="device_trackers") or []
            for tracker in trackers:
                mac = self.get_state(tracker, attribute="ap_mac")
                if mac and str(mac).lower() == ap_mac:
                    matched.append("%s (%s)" % (person, tracker))
        open_doors = [d["entity"] for d in self.door_sensors
                      if self.get_state(d["entity"]) == d["open_state"]]
        self.log("[ap-scan] on AP (%s): %s | open doors: %s | someone_outside=%s"
                 % (ap_mac or "<none>",
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

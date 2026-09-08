# Native closure evidence and remaining release relation

The normal `RosMoveItTransport.mechanical_contact_context` now consumes the
existing native completion proof and calibrated closure range. It calls the
existing freshness, incarnation, generation and causal hardware validator;
completion reason 2 denotes the driver's settled-away plateau after movement
(`patches/frcobot_ros2.patch`, gripper completion branch). It does not infer
contact from policy termination, a closed command, or an isolated range sample.
This is contact evidence, not observed object pose or physical Pick success.

The executable counterexample is
`FinitePlanTest.test_calibrated_plateau_does_not_determine_axial_release_clearance`.
It reads the actual qualified 24 mm grasp/object profiles, place-A qualification,
and its digest-bound original FR5 URDF. The fixed wrist-to-gripper translation
is 0.109 m, tool-to-TCP is 0.249852939145247 m, and the nominal datum offset is
0.0085 m. Hence the nominal cube center is 0.149352939145247 m along gripper Z.
Both fingertip collision boxes span Z=[0.138, 0.156] m. A cube at the nominal
center and one shifted 0.0035 m along that axis both overlap both fingertips;
the same opposed-face width permits the same closure plateau. The shift uses
the existing grasp depth, not a new threshold. The second possible relation
turns the existing nominal 0.002 m lower/release clearance into -0.0015 m.
These are geometric alternatives, not claims that either grasp occurred.

The smallest baseline, attaching the nominal expert transform on plateau,
therefore lacks evidence after arbitrary learned motion. The one bounded
alternative, a conservative envelope covering both possible depths, cannot
certify the existing nominal lower pose: it contains the interfering witness.
This does not prove that every alternative release route is impossible. It
identifies the missing input for this existing route: current contact depth or
evidence that the known initial object pose was preserved until contact, bound
to the settled command and current tool pose. Initial Scene state alone does
not establish that invariant after arbitrary learned motion. No new physical
certificate is requested for the already calibrated closure criterion.

Separately, the digest-bound original URDF moves the fingers inward as the raw
opening coordinate increases. The native diagnostic retains this model conflict;
plateau evidence does not repair it. The unqualified candidate model is neither
selected nor changed. No qualification bytes, raw episodes, checkpoints,
controller coordinates, opening stages, thresholds or physical state are changed.

`test_native_contact_producer_consumes_settled_closure_without_inventing_pose`
runs the real producer through OneJob's native serialized CPU caller. It accepts
the actual plateau evidence and rejects motion-done-only, pending, out-of-range
and wrong-reference feedback; all cases retain diagnostics and send no terminal
motion without a relation. Existing success fixtures remain simulated contact
fixtures and are not evidence that the live relation producer is complete.
Physical grasp/release qualification and an accepted normal release path remain
unproven. This patch is a diagnostic/producer increment plus falsifying evidence,
not a claim that R1's complete bound-context path has been implemented.

Restricted software route assessment: `finite_plan.validate_trace` retains
per-segment start/terminal observations and validates generation transitions.
A same-command close observation therefore supplies arm state for FK. With a
preserved initial object pose, this could establish a restricted
MODEL_BASED_EXPECTATION and carry it through a subsequent held segment. The
missing transition is initial Scene -> learned ARM prefix -> first closing
contact: `_planning_scene_objects`, `_apply_and_readback_scene`, and
`_check_plan_collision` currently establish floor/wall checks, not no earlier
object displacement. A small same-owner one-object contact boundary could
prospectively supply that invariant; this is not a universal requirement for a
new sensor. It would need a usable, source-bound aperture/contact model, which
the current digest-bound model contradicts. Selecting or qualifying the separate
candidate is outside this dispatch. Current feedback alone cannot retroactively
prove an unguarded prefix preserved the initial object pose.

# Optional up-view curator v1.2

Routine use has three commands: `prepare --source`, `status --run`, and
`decide --run`. `prepare` resolves the sole compatible JSON profile in
`view_profiles/` and the sole strict policy in `review_policies/`, creates a
full hidden LeRobot candidate, verifies it, then renders one bounded H.264
`raw | overlay | actual candidate` review. No final dataset exists at
`REVIEW_READY`.

`decide` reads only foreground `/dev/tty` and accepts exactly `APPROVE` or
`REJECT`. Approval revalidates the source, candidate, profile, review, and
decision digest chain before atomic no-replace publication. Rejection removes
only the identity-matching hidden candidate; review and decision evidence stay
in the run directory. Neither path grants training authority.

An interrupted prepare run is diagnostic evidence, never resumable work.
Every retry invokes `prepare --source` and receives a fresh run ID; only a
completed `REVIEW_READY` run may cross a process boundary into `decide`.

View profiles retain the strict `curator.up_view_profile_request.v2` geometry
and asset contract while becoming canonical files named `<profile-id>.json`.
There is intentionally no registry index, defaults database, daemon, GUI, or
person model.

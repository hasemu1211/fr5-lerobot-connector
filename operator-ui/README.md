# FR5 collection desk fixture

A backend-free view of setup → readiness → exact digest approval → progress → semantic review/recovery. It never sends robot, camera, recorder, dataset, scene/cell, candidate-admission, or training-approval effects.

Preview in one visible terminal:

```sh
make -C operator-ui preview
```

Open `http://127.0.0.1:4173`. Run the repository-owned frontend check with:

```sh
make -C operator-ui test
```

Both targets load the repository environment through `direnv`. In a new worktree whose `.envrc` the user has not approved, pass an already-approved checkout explicitly: `make -C operator-ui DIRENV_ROOT=/path/to/approved/checkout test`. Never run `direnv allow` on the user's behalf.

The fixture begins at active Setup and the selector exposes every acceptance state. In “Exact digest approval,” typing the displayed phrase previews the running fixture locally; it does not create an approval receipt. Semantic review exposes a required reason only for FAIL or UNCERTAIN and announces an intent preview without changing any artifact.

English is the deterministic default. The native language control switches presentation to bundled Korean copy (or start with `?lang=ko`), updates the document language, and leaves commands, digests, codes, identifiers, fixtures, and backend authority unchanged.

While the preview is running, `http://127.0.0.1:4173/tests/browser-regression.html` runs the dependency-free DOM regressions for both languages and all states, protected bytes, hostile progress, corrected exact approval, setup default, and conditional review reasons.

See [architecture.md](architecture.md) for the operator journey and stack decision, and [backend-contract-proposal.md](backend-contract-proposal.md) for the integration boundary.

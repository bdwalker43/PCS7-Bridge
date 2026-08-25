# PCS 7 Bridge Home Assistant app

This is a Home Assistant app (formerly add-on) for the PCS 7 bridge UX. It is
installed from this repository and opened from its Home Assistant sidebar
panel. Its persistent map lives in the app's `/data/pcs7-bridge.json`.

## Safety model

- New inputs are *pending deployments*, not live PLC mappings.
- The UI's S7 probe only connects/disconnects; it does not read or write a DB.
- `write_enabled` defaults to `false`. When it is explicitly armed *and* a
  point is explicitly activated after PCS 7 engineering, the app polls the HA
  Core API and writes only that typed allow-list to the assigned DB address.
- PLC-to-HA commands are explicit DB58 allow-list mappings. Each mapping must
  be engineered in PCS 7, then individually activated in the UI. The runtime
  baselines values after start/reconfiguration and executes only later valid
  value changes (or a clean pulse edge).
- Existing PCS 7 DBs are append-only. The UI never creates, replaces, compiles
  or downloads a DB/CFC project.

## First validation

1. Add this directory's parent as an HA app repository.
2. Install **PCS 7 Bridge**, keep `write_enabled: false`, and start it.
3. Open the sidebar panel and run one read-only connection probe.
4. Only after a clean connection test, add an input and compare its proposed
   DB member/address with the existing PCS 7 engineering package.

The original external bridge remains the rollback path until a separate,
reviewed migration enables the runtime data loop.

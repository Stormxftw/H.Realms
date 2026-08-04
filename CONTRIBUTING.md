# Contributing game profiles

The Store is fed by the official repository. A merged package becomes visible through `catalog/index.json`; the app refreshes that index from GitHub, verifies the package digest and schemas, and keeps a verified offline cache.

## Add a game

1. Copy a similar file from `catalog/packages/` to `catalog/packages/<game-id>.json`.
2. Use a lowercase kebab-case ID and keep the package ID and embedded profile ID identical.
3. Set a semantic `version` such as `1.0.0` and add useful search tags.
4. Define the declarative profile and one adapter entry inside the package.
5. Rebuild the deterministic index:

   ```bash
   uv run --no-project --with jsonschema python3 scripts/build-profile-catalog.py
   ```

6. Verify before opening the PR:

   ```bash
   uv run --no-project --with jsonschema python3 scripts/build-profile-catalog.py --check
   uv run --no-project --with jsonschema python3 -m unittest tests.test_profile_store tests.test_store_api -v
   ```

7. Open a focused PR and include official dedicated-server documentation plus real testing evidence.

## Trust boundary

Profiles are data, not installers. New community IDs use a code-owned execution layout:

- `adapter.projectDir` must be exactly `community/<game-id>`;
- `service.start` may only call `start.sh`;
- `service.stop` may only call `stop.sh`;
- `service.restart` may only call `stop.sh`, then `start.sh`;
- `backup.create` may only call `backup.sh`;
- community adapters cannot declare mutable configuration properties.

The package may describe status collection, ports, process matching, labels, and controls, but it cannot select an existing server directory or arbitrary executable. No script is downloaded. Controls remain blocked until the operator deliberately provisions the fixed local script slots.

Packages may not contain shell code, absolute paths, `..` traversal, credentials, tokens, non-finite numbers, duplicate JSON fields, or arbitrary URLs. The host executes only fixed local slots after its normal plan/confirm checks.

Automated verification checks:

- strict parsing (duplicate fields and non-finite numbers rejected) plus JSON Schema validation;
- package/profile ID agreement and semantic version shape;
- unique control IDs and tags;
- every binding maps to a declared adapter action;
- fixed community project/script slots and no remote mutable-property authority;
- deterministic index contents, byte size, and SHA-256 digest;
- profile-store and public Store API regression tests.

A green check means the package is structurally safe to review. It does not prove the dedicated server actually works. Maintainers still review documentation, defaults, process matching, ports, licensing, and testing evidence before merge.

## Update a game

Edit its package, increment `version`, rebuild the index, and explain compatibility or migration concerns in the PR. Do not edit `catalog/index.json` by hand.

## Artwork

Artwork requires explicit redistribution permission and provenance. Prefer original contributor-created assets or official assets whose license clearly allows repository packaging. A link to an image is not a license.

# Game artwork

Artwork in this directory is packaged by the official Game Host Console repository so the Store and Desktop UI work offline without third-party requests or tracking.

Every packaged image must have an entry in `manifest.json` with:

- an exact repository-relative path,
- dimensions, MIME type, byte size, and SHA-256 digest,
- a rights/provenance classification,
- an explicit `allow` packaging decision,
- composition metadata for accessible UI overlays.

Public availability, a press kit, or an official CDN URL is not by itself permission to redistribute publisher artwork. Assets without an allowed manifest entry must fall back to the normal Hermes-themed header.

The current Palworld-assigned hero is original generic survival-server artwork. It contains no Palworld logo, characters, creatures, screenshots, or copied trade dress; association is provided only by factual interface text.

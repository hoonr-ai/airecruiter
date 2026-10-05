# Multi-location sourcing release notes

- Search supports up to 10 configured locations. Provider query fan-out is
  capped at that limit; six locations, including Jacksonville, are supported.
- Confirmed out-of-radius candidates are excluded from Step 5 for JobDiva
  Agent, JobDiva Talent, LinkedIn, and Exa. Remote jobs do not apply a
  geographic radius. A temporary geocoder failure is treated as unverifiable,
  not as proof that a candidate is outside the radius.
- JobDiva's server-side `withinMiles` now uses the configured radius without
  the former 2x headroom. Candidate-to-location checks use straight-line
  ZIP/city centroid distances, followed by best-effort geocoding when the
  offline index cannot resolve a place; these are not driving distances.
- Exa DeepSearch accepts one radius for its OR-joined query, so it searches
  with the broadest configured radius. The final Step 5 gate applies each
  location's configured radius independently.

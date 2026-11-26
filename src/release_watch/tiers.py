"""DocExtractor's vendor tiers by ID. Its read-only API key may not read /tiers, so the names live here
(confirmed by the owner, chat 2026-11-26). A tier ID missing here is sent as no tier and named on the health card."""

TIERS: dict[str, int] = {
    "c0edf5b5-0266-45f5-8181-a49d1a832fbd": 1,
    "3076b5aa-6fd6-4fd1-9279-a66c4ef4098f": 2,
    "da51fb10-5b1e-46d0-a684-7870e73cb07f": 3,
}


def tier_of(tier_id) -> int | None:
    return TIERS.get(str(tier_id)) if tier_id else None

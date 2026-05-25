"""Named PostgreSQL cluster configurations exercised by the test suite.

A "variant" is a (PG major version, postgresql.conf overrides) pair.
We keep one long-lived cluster per variant for the whole pytest session
(`scope="session"` on the fixture) so we pay the initdb + start cost
ONCE, not once per test. Per-test isolation is achieved by dropping
every non-system database between tests instead — those DROPs take
milliseconds where initdb takes seconds.

The same variant names are used by `scripts/run-harness.py` to spin up
persistent clusters between test runs (so even the per-session initdb
disappears for local dev loops).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ClusterVariant:
    """One named cluster configuration: a PG major + conf overrides.

    Attributes:
        name: Stable identifier used as the pytest parametrize id and as
            the suffix in `FLD_TEST_PG_*` harness env vars (mapped via
            `env_suffix`).
        pg_major: PostgreSQL major version (16, 17, 18, ...).
        extra_conf: postgresql.conf overrides appended after the test
            defaults. Values are written verbatim, so SQL-quoted strings
            need their own surrounding apostrophes (e.g. `"'clone'"`).
    """

    name: str
    pg_major: int
    extra_conf: dict[str, str] = field(default_factory=dict)

    @property
    def env_suffix(self) -> str:
        """Uppercased, underscore-only form of `name` for env var keys.

        `pg18-clone` -> `PG18_CLONE`, so the harness can publish ports
        under `FLD_TEST_PG_PORT_PG18_CLONE` etc.
        """
        return self.name.upper().replace("-", "_")


VARIANTS: tuple[ClusterVariant, ...] = (
    ClusterVariant("pg16", 16),
    ClusterVariant("pg17", 17),
    ClusterVariant("pg18-default", 18, {"file_copy_method": "'copy'"}),
    ClusterVariant("pg18-clone", 18, {"file_copy_method": "'clone'"}),
)


def variant_by_name(name: str) -> ClusterVariant:
    """Look up a variant by its `name` field. Raises KeyError if missing."""
    for variant in VARIANTS:
        if variant.name == name:
            return variant
    raise KeyError(f"unknown cluster variant: {name!r}")

"""Create the database, seed the simulated world and default schedules (idempotent).
serve.sh runs this once before starting the web and MCP processes."""
from . import db, world
from .company import engine


def main():
    db.init()
    fresh = not world.seeded()
    world.seed()
    engine.seed_schedules()
    engine.seed_v3_schedules()
    engine.seed_v4_schedules()
    engine.seed_v5_schedules()
    print("vix world seeded" if fresh else "vix world already present")


if __name__ == "__main__":
    main()

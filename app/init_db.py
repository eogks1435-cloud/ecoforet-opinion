"""Create the table on a new database:  python -m app.init_db   (safe to repeat; never drops anything).

The app also runs this at startup, so a fresh Render database is ready after the first deploy.
"""

from .database import engine, init_db


def main() -> None:
    init_db()
    print(f"opinion_submissions ready on {engine.url.render_as_string(hide_password=True)}")


if __name__ == "__main__":
    main()

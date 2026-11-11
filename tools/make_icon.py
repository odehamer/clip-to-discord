"""Write assets/icon.ico from app.draw_icon. Run from the repo root: python tools/make_icon.py"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app  # noqa: E402

SIZES = (16, 32, 48, 64, 128, 256)


def main() -> None:
    out = ROOT / "assets" / "icon.ico"
    out.parent.mkdir(exist_ok=True)
    app.draw_icon(256).save(out, format="ICO", sizes=[(s, s) for s in SIZES])
    print(f"wrote {out}")


if __name__ == "__main__":
    main()

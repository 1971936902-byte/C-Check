"""Build a standalone zipapp without pip, Docker or backend dependencies."""
import argparse
from pathlib import Path
import zipfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("wb") as output:
        output.write(b"#!/usr/bin/env python3\n")
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in (
                ("__main__.py", b"from ccheck_review import main\nraise SystemExit(main())\n"),
                ("ccheck_review.py", Path(__file__).with_name("ccheck_review.py").read_bytes()),
            ):
                info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, data)
    args.output.chmod(0o755)
    print(args.output.absolute())


if __name__ == "__main__":
    main()

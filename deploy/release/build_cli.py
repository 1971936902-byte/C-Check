import argparse
from pathlib import Path
import shutil
import tempfile
import zipfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--yaml-package', type=Path, required=True)
    parser.add_argument('--with-assistant', action='store_true')
    parser.add_argument('--with-terminal', action='store_true')
    args = parser.parse_args()
    source = Path(__file__).resolve().parent / 'cli'
    if not (args.yaml_package / '__init__.py').is_file():
        raise RuntimeError('supply the validated PyYAML package directory for vendoring')
    with tempfile.TemporaryDirectory() as temporary:
        tree = Path(temporary)
        shutil.copytree(source / 'ccheck', tree / 'ccheck', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        if args.with_assistant:
            import sys
            sys.path.insert(0, str(source))
            from ccheck.assistant import MIGRATIONS, RESOURCES
            runtime = tree / 'ccheck/assistant_runtime'
            runtime.mkdir()
            for name in RESOURCES:
                shutil.copyfile(source.parent.parent / 'assistant' / name, runtime / name)
            migrations = tree / 'ccheck/assistant_migrations'
            migrations.mkdir()
            for name in MIGRATIONS:
                shutil.copyfile(source.parent.parent.parent / 'backend/alembic/versions' / name, migrations / name)
        if args.with_terminal:
            import sys
            sys.path.insert(0, str(source))
            from ccheck.remote_terminal import RESOURCES
            runtime = tree / 'ccheck/terminal_runtime'
            runtime.mkdir()
            for name in RESOURCES:
                shutil.copyfile(source.parent.parent / 'terminal' / name, runtime / name)
        shutil.copytree(args.yaml_package, tree / 'yaml',
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.so', '*.pyd'))
        (tree / '__main__.py').write_text('from ccheck.__main__ import main\nraise SystemExit(main())\n')
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('wb') as output:
            output.write(b'#!/usr/bin/env python3\n')
            with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(tree.rglob('*')):
                    if not path.is_file():
                        continue
                    entry = zipfile.ZipInfo(path.relative_to(tree).as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
                    entry.create_system = 3
                    entry.external_attr = 0o100644 << 16
                    entry.compress_type = zipfile.ZIP_DEFLATED
                    archive.writestr(entry, path.read_bytes())
    args.output.chmod(0o755)


if __name__ == '__main__':
    main()

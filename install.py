#!/usr/bin/env python3
"""Install the haplocaller shortcut for this account; keep this folder in place."""
import argparse
import os
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bin-dir', type=Path, default=Path.home() / '.local/bin')
    args = parser.parse_args()
    if sys.version_info < (3, 9):
        parser.error('Python 3.9 or newer is required.')
    source = Path(__file__).resolve().parent / 'bin/haplocaller'
    destination = args.bin_dir.expanduser().absolute() / 'haplocaller'
    if not source.is_file() or not os.access(source, os.X_OK):
        parser.error(f'The launcher must exist and be executable: {source}')
    if destination.exists() or destination.is_symlink():
        if not destination.is_symlink() or destination.resolve() != source:
            parser.error(f'{destination} already exists and points elsewhere; it was not changed.')
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.symlink_to(source)
    print(f'Installed: {destination}')
    print('Keep the Haplocaller program folder in place.')
    if str(destination.parent) not in os.environ.get('PATH', '').split(os.pathsep):
        import shlex
        print('Add this to your shell configuration, then open a new terminal:')
        print('export PATH=' + shlex.quote(str(destination.parent)) + ':"$PATH"')
    print('Start with: haplocaller')


if __name__ == '__main__':
    main()

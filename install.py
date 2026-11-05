#!/usr/bin/env python3
"""Second half of ./install.sh: link the command, pick the cluster, account and Dorado."""
import argparse
import getpass
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from haplocaller import cli, software, ui  # noqa: E402
from haplocaller.common import UserError, state_dir, write_json  # noqa: E402


def link_command(bin_dir):
    source = Path(__file__).resolve().parent / 'bin/haplocaller'
    destination = bin_dir.expanduser().absolute() / 'haplocaller'
    if not source.is_file() or not os.access(source, os.X_OK):
        raise UserError(f'The launcher must exist and be executable: {source}')
    if destination.is_symlink() and destination.resolve() != source:
        destination.unlink()  # an older clone of this program; point the command at this one
    elif destination.exists() and not destination.is_symlink():
        raise UserError(f'{destination} exists and is not a link; it was not changed.')
    if not destination.is_symlink():
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.symlink_to(source)
    return destination


def slurm_accounts():
    try:
        result = subprocess.run(['sacctmgr', '-nP', 'show', 'assoc', 'user=' + getpass.getuser(), 'format=account'],
                                capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return sorted({line.strip() for line in result.stdout.splitlines() if line.strip()})


def save(overrides):
    cli.USER_PROFILE.parent.mkdir(parents=True, exist_ok=True)
    write_json(cli.USER_PROFILE, overrides)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bin-dir', type=Path, default=Path.home() / '.local/bin')
    parser.add_argument('--cluster', help='Cluster profile name (default: detected from this machine)')
    parser.add_argument('--dorado', choices=['ask', 'download', 'cluster', 'none'], default='ask',
                        help="Dorado source: download a private copy, use the cluster's, or ask (default)")
    parser.add_argument('--no-dorado', dest='dorado', action='store_const', const='none', help=argparse.SUPPRESS)
    args = parser.parse_args()
    ui.heading('HAPLOCALLER INSTALL')
    destination = link_command(args.bin_dir)

    saved = json.loads(cli.USER_PROFILE.read_text()) if cli.USER_PROFILE.is_file() else {}
    names = cli.profile_names()
    name = args.cluster or cli.detect_cluster()
    if name:
        ui.notice(f'Cluster: {name}', 'ok')
    else:
        name = names[cli.choose('Which cluster is this?', names)]
    if name not in names:
        raise UserError(f'Unknown cluster "{name}". Available: ' + ', '.join(names))
    base = json.loads((cli.PROJECT / 'profiles' / (name + '.json')).read_text())
    overrides = saved if saved.get('cluster') == name else {}
    overrides['cluster'] = name
    overrides['python'] = sys.executable

    accounts = slurm_accounts()
    if accounts and overrides.get('account', base['account']) not in accounts:
        overrides['account'] = accounts[0] if len(accounts) == 1 else accounts[cli.choose('Slurm account', accounts)]
    if accounts:
        ui.notice(f'Slurm account: {overrides.get("account", base["account"])}', 'ok')
    save(overrides)

    profile = cli.load_profile()
    work = Path(profile['work_root'])
    state = state_dir(profile)
    try:
        work.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise UserError(f'Cannot create the working directory {work}: {error}')
    ui.notice(f'Working directory: {work}', 'ok')

    if args.dorado != 'none':
        try:
            current = software.installed(profile)
            ui.notice(f'Dorado {current["version"]} found on this cluster.', 'ok')
        except UserError:
            current = None
        download = args.dorado == 'download' or (current is None and args.dorado != 'cluster')
        if current and args.dorado == 'ask' and sys.stdin.isatty():
            download = cli.yes('Also download the latest Dorado as a private copy (otherwise the cluster\'s is used)?')
        if download:
            ui.notice('Downloading Dorado (a few hundred MB)...', 'accent')
            version = software.latest_version()
            found = software.install(version, state / 'tools')
            write_json(state / 'software.json', found)
            overrides['dorado_executable'] = found['executable']
            overrides['dorado_module'] = None
            save(overrides)
            ui.notice(f'Dorado {version} installed in {state}/tools', 'ok')
        elif current is None:
            ui.notice('No Dorado available; rerun ./install.sh --dorado download', 'warning')
    ui.notice('Installed.', 'ok')
    if str(destination.parent) not in os.environ.get('PATH', '').split(os.pathsep):
        print('Add this to ~/.bashrc, then open a new terminal:')
        print('  export PATH=' + shlex.quote(str(destination.parent)) + ':"$PATH"')
    print('Start with:  haplocaller')


if __name__ == '__main__':
    try:
        main()
    except (UserError, OSError, KeyboardInterrupt, EOFError) as error:
        print(file=sys.stderr)
        print(str(error) or 'Stopped.', file=sys.stderr)
        sys.exit(1)

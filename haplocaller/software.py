import json
import platform
import re
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from .common import UserError, command


def installed(profile):
    executable = profile.get('dorado_executable')
    if not executable:
        raise UserError('Dorado has not been configured for this cluster. Run haplocaller setup once with your cluster maintainer.')
    module = profile.get('dorado_module')
    try:
        result = command([executable, '--version'], module, capture_output=True)
    except OSError as error:
        raise UserError(f'Dorado is not at {executable} ({error.strerror or error}).')
    versions = re.findall(r'(?m)^(\d+\.\d+\.\d+(?:\+[^\s]+)?)\s*$', result.stdout + '\n' + result.stderr)
    if result.returncode or not versions:
        raise UserError('Dorado could not start. Check the cluster profile with haplocaller setup.\n' + result.stderr[-1000:])
    result = command([profile['python'], '-c',
                      'import shutil,sys;print(shutil.which(sys.argv[1]) or "")', executable],
                     module, capture_output=True, check=True)
    resolved = result.stdout.strip()
    if not resolved or not Path(resolved).is_file():
        raise UserError('Could not resolve the Dorado executable.')
    return {'executable': resolved, 'module': module, 'version': versions[-1]}


def latest_version():
    """Newest Dorado release. The API is rate-limited per IP (shared on login nodes), so fall back
    to the release page redirect, which is not."""
    headers = {'User-Agent': 'haplocaller-lab-wizard'}
    try:
        request = urllib.request.Request('https://api.github.com/repos/nanoporetech/dorado/releases/latest', headers=headers)
        with urllib.request.urlopen(request, timeout=5) as response:
            tag = json.load(response)['tag_name']
    except Exception:
        request = urllib.request.Request('https://github.com/nanoporetech/dorado/releases/latest', headers=headers)
        with urllib.request.urlopen(request, timeout=8) as response:
            tag = response.geturl().rstrip('/').rsplit('/', 1)[-1]
    version = tag.removeprefix('v')
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('Unrecognised Dorado release version')
    return version


def newer(candidate, current):
    return tuple(map(int, candidate.split('.'))) > tuple(map(int, current.split('+')[0].split('.')))


def install(version, cache):
    """Called only after an explicit update choice and final preparation action."""
    if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'amd64'):
        raise UserError('Automatic installation currently supports Linux x86_64 only. Configure an existing Dorado executable instead.')
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise UserError('Invalid Dorado version')
    cache = Path(cache)
    destination = cache / ('dorado-' + version)
    executable = destination / 'bin/dorado'
    if executable.is_file():
        return {'executable': str(executable), 'module': None, 'version': version}
    cache.mkdir(parents=True, exist_ok=True)
    url = f'https://cdn.oxfordnanoportal.com/software/analysis/dorado-{version}-linux-x64.tar.gz'
    with tempfile.TemporaryDirectory(prefix='install-', dir=cache) as temp:
        temp = Path(temp)
        archive_path = temp / 'dorado.tar.gz'
        with urllib.request.urlopen(url, timeout=60) as response, archive_path.open('wb') as out:
            shutil.copyfileobj(response, out, length=1024 * 1024)
        with tarfile.open(archive_path) as archive:
            if not hasattr(tarfile, 'data_filter'):
                raise UserError('This Python lacks safe archive extraction. Configure an existing Dorado executable instead.')
            def extraction_filter(member, destination):
                # Dorado ships one symlink to an absolute path; everything else stays strictly confined.
                try:
                    return tarfile.data_filter(member, destination)
                except tarfile.AbsoluteLinkError:
                    return tarfile.tar_filter(member, destination)

            archive.extractall(temp / 'unpacked', filter=extraction_filter)
        binaries = list((temp / 'unpacked').glob('*/bin/dorado'))
        if len(binaries) != 1:
            raise UserError('The downloaded Dorado package has an unexpected layout.')
        result = command([str(binaries[0]), '--version'], capture_output=True)
        if result.returncode or version not in result.stdout + result.stderr:
            raise UserError('The downloaded Dorado executable did not pass its version check.')
        binaries[0].parent.parent.rename(destination)
    return {'executable': str(executable), 'module': None, 'version': version}

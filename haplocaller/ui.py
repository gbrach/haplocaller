"""Small terminal helpers; no colour escapes in redirected output by default."""
import os
import re
import sys

MODE = 'auto'
ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
STYLES = {'title': '1;36', 'label': '1', 'accent': '36', 'ok': '32',
          'warning': '33', 'error': '1;31', 'muted': '2', 'selected': '1;32'}


def enabled(stream=None):
    stream = stream or sys.stdout
    if MODE == 'never' or 'NO_COLOR' in os.environ:
        return False
    return MODE == 'always' or (stream.isatty() and os.environ.get('TERM') != 'dumb')


def clean(value):
    text = ANSI.sub('', str(value))
    return ''.join(c for c in text if c >= ' ' and c != '\x7f')


def paint(value, style='accent', stream=None):
    value = clean(value)
    return f'\033[{STYLES[style]}m{value}\033[0m' if enabled(stream) else value


def prompt(value):
    text = paint(value, 'label')
    # Tell readline that SGR sequences occupy no columns.
    return ANSI.sub(lambda m: '\001' + m[0] + '\002', text) if sys.stdin.isatty() else text


def heading(value):
    print('\n' + paint(value, 'title'))


def field(label, value):
    print('  ' + paint(f'{label:<14}', 'label') + clean(value))


def notice(value, style='warning', stream=None):
    print(paint(value, style, stream), file=stream or sys.stdout)


def bar(done, total, width=30, style='accent'):
    fraction = min(1, max(0, done / total)) if total else 0
    filled = int(width * fraction)
    return '[' + paint('#' * filled, style) + paint('-' * (width - filled), 'muted') + ']'

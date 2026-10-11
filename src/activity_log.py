"""Normalize service output for the shared Activity panel."""
from dataclasses import dataclass
from datetime import datetime
import json
import re
import zlib

from rich.table import Table
from rich.text import Text

from dashboard_view import plain_output

STAMP = re.compile(r'^\[?(\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)?)\]?\s*(?:\|\s*)?')
CLOCK = re.compile(r'^\[?(\d\d:\d\d:\d\d)(?:\.\d+)?\]?\s+')
LEVEL = re.compile(r'^\s*\[?(TRACE|DEBUG|INFO|WARNING|WARN|ERROR|ERR|CRITICAL|FATAL)\]?\s*(?::|\|)?\s+', re.I)
ALIASES = {'chat': 'Chat', 'agent': 'Agent', 'caddy': 'Caddy', 'image-web': 'Image UI',
           'gateway': 'Gateway', 'logs': 'Log reader', 'metrics': 'Metrics',
           'dashboard': 'Dashboard', 'inference/qwen-omlx': 'Qwen / oMLX',
           'inference/deepseek': 'DeepSeek / DS4'}
COLORS = ('#79c9e8', '#c5a3ef', '#81c9ab', '#edc480', '#eaa4c5', '#9eb8f0')
LEVEL_COLORS = {'ERR': '#f08080', 'WARN': '#edc480', 'INFO': '#8396aa',
                'DEBUG': '#697b90', 'TRACE': '#697b90'}


@dataclass(frozen=True)
class ActivityLine:
    time: datetime
    source: str
    level: str
    message: str

    def render(self, width):
        """Keep wrapped messages under their own column at every panel width."""
        width = max(20, width)
        table = Table.grid(padding=(0, 1), expand=True)
        table.width = width
        cells = []
        if width >= 42:
            table.add_column(width=8, no_wrap=True)
            cells.append(Text(self.time.strftime('%H:%M:%S'), style='#697b90'))
        source_width = 14 if width >= 64 else 11
        table.add_column(width=source_width, no_wrap=True)
        label = ALIASES.get(self.source, self.source.removeprefix('inference/'))
        if len(label) > source_width:
            label = label[:source_width - 5] + '…' + label[-4:]
        color = COLORS[zlib.crc32(self.source.casefold().encode()) % len(COLORS)]
        cells.append(Text(label, style='bold ' + color))
        table.add_column(width=4, no_wrap=True)
        cells.append(Text(self.level[:4], style=LEVEL_COLORS.get(self.level, '#8396aa')))
        table.add_column(ratio=1, overflow='fold')
        cells.append(Text(self.message, style='#f0a0a0' if self.level == 'ERR' else '#d3dee9', overflow='fold'))
        table.add_row(*cells)
        return table


def normalize(source, line, received=None):
    received = received or datetime.now().astimezone()
    timestamp, message, level = received, plain_output(line).expandtabs(4), 'INFO'
    stamped = False
    # Docker adds an outer timestamp even when the application has its own.
    for _ in range(2):
        match = STAMP.match(message)
        if not match:
            break
        try:
            parsed = datetime.fromisoformat(match[1].replace('Z', '+00:00')).astimezone()
        except ValueError:
            break
        if not stamped:
            timestamp = parsed
        stamped = True
        message = message[match.end():]
    if match := CLOCK.match(message):
        if not stamped:
            hour, minute, second = map(int, match[1].split(':'))
            try:
                timestamp = received.replace(hour=hour, minute=minute, second=second)
            except ValueError:
                pass
        message = message[match.end():]
    try:
        record = json.loads(message)
    except (ValueError, TypeError):
        record = None
    if isinstance(record, dict) and isinstance(record.get('msg', record.get('message')), str):
        message = record.get('msg', record.get('message'))
        level = str(record.get('level', level)).upper()
        extras = {k: v for k, v in record.items() if k not in ('msg', 'message', 'level', 'ts', 'time', 'source')}
        if extras:
            message += ' · ' + json.dumps(extras, ensure_ascii=False, separators=(', ', ': '))
    elif match := LEVEL.match(message):
        level, message = match[1].upper(), message[match.end():]
    level = {'ERROR': 'ERR', 'CRITICAL': 'ERR', 'FATAL': 'ERR', 'WARNING': 'WARN'}.get(level, level)
    return ActivityLine(timestamp, plain_output(source), level, plain_output(message))

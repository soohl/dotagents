"""Terminal utilization and inference charts with real time on X."""
from collections import deque
from math import ceil

from rich.text import Text
from textual.containers import Horizontal
from textual.widget import Widget
from inference_speeds import WINDOW

SCALE_WIDTH = 8


def label_space(width):
    """Leave the same scale gutter and at least one plot cell in every chart."""
    return min(22, max(20, width // 3), width - SCALE_WIDTH - 2)


class UsageChart(Widget):
    DEFAULT_CSS = 'UsageChart { width: 1fr; height: 1fr; min-height: 3; min-width: 0; }'
    WINDOW = WINDOW
    ceiling = 1

    def value_label(self, value):
        return f'{value:.0%}'

    def axis_label(self, value):
        return str(round(value * 100))

    def current_value(self):
        return self.samples[-1][1] if self.samples else None

    def __init__(self, label, color, max_gap=6, **kwargs):
        super().__init__(**kwargs)
        self.label, self.color = label, color
        self.samples = deque(maxlen=120)
        self.detail = 'Awaiting sample'
        self.extra = []
        self.max_gap = max_gap

    def sample(self, timestamp, value, detail, extra=()):
        if self.samples and timestamp <= self.samples[-1][0]:
            return
        self.samples.append((timestamp, value, dict(extra)))
        self.detail = detail
        self.extra = list(dict(extra))
        self.refresh()
        if isinstance(self.parent, MemoryCharts):
            self.parent.query_one(MemoryLegend).refresh()

    def render(self):
        width, height = self.size
        if isinstance(self.parent, MemoryCharts):
            return plot(self.samples, width, height - 1, '', '', self.color,
                        self.ceiling, self.axis_label, 0, self.max_gap, self.extra,
                        axis=True)
        if width < 20 or height < 3:
            return Text(self.label, style=self.color)
        value = self.current_value()
        heading = f'{self.label} {self.value_label(value)}' if value is not None else f'{self.label} —'
        label_width = label_space(width)
        return plot(self.samples, width, height - 1, heading, self.detail, self.color,
                    self.ceiling, self.axis_label, label_width, self.max_gap, self.extra,
                    axis=True)


class MemoryLegend(Widget):
    def render(self):
        output = Text()
        for index, chart in enumerate(self.parent.query(UsageChart)):
            if index:
                output.append('\n')
            detail = chart.detail if chart.current_value() is not None else '—'
            line = Text(f'{chart.label} {detail}', style=chart.color)
            line.truncate(max(0, self.size.width - 1), overflow='ellipsis')
            output.append(line)
        return output


class MemoryCharts(Horizontal):
    """Use one left legend and the same two plot columns as inference speeds."""
    DEFAULT_CSS = """
    MemoryCharts { height: 1fr; min-height: 3; }
    MemoryLegend { height: 1fr; }
    """

    def compose(self):
        yield MemoryLegend()

    def on_mount(self):
        self.move_child(self.query_one(MemoryLegend), before=0)

    def on_resize(self, event):
        width = event.size.width
        legend_width = label_space(width)
        graph_width = width - legend_width - 3
        show_plots = graph_width >= 2 * (SCALE_WIDTH + 2)
        self.query_one(MemoryLegend).styles.width = legend_width if show_plots else width
        for index, chart in enumerate(self.query(UsageChart)):
            chart.display = show_plots
            chart.styles.width = (graph_width // 2 if index == 0
                                  else graph_width - graph_width // 2 + 2)


def plot(samples, width, plot_height, heading, detail, color, ceiling, axis_label,
         label_width, max_gap=6, extra=(), axis=False):
    """Render measured points. Missing readings and long pauses break lines."""
    output = Text()
    maximum_label = axis_label(ceiling)
    if plot_height == 1:
        maximum_label = '0–' + maximum_label
    # Rates, percentages, and compact zero–maximum labels share a fixed gutter.
    # An unusually large label must not move the axis or change the plot width.
    scale_width = SCALE_WIDTH
    if len(maximum_label) > scale_width:
        maximum_label = maximum_label[:scale_width - 1] + '…'
    plot_width = width - label_width - scale_width - 1
    if plot_width < 1 or plot_height < 1:
        return Text(heading, style=color)
    # Braille gives each terminal cell a 2 by 4 pixel plotting surface.
    grid = [[(0, '') for _ in range(plot_width)] for _ in range(plot_height)]
    bits = ((1, 8), (2, 16), (4, 32), (64, 128))

    def point(x, y, color):
        row, column = y // 4, x // 2
        mask, _ = grid[row][column]
        grid[row][column] = (mask | bits[y % 4][x % 2], color)
    now = samples[-1][0] if samples else 0
    series = [(None, color), *((key, '#c084fc') for key in extra)]
    for key, series_color in series:
        previous = None
        for timestamp, primary, extras in samples:
            reading = primary if key is None else extras.get(key)
            if not now - WINDOW <= timestamp <= now or reading is None:
                previous = None
                continue
            x = round((timestamp - now + WINDOW) / WINDOW * (plot_width * 2 - 1))
            y = round((1 - min(1, max(0, reading / ceiling))) * (plot_height * 4 - 1))
            if previous is not None and timestamp - previous[2] <= max_gap:
                px, py, _ = previous
                steps = max(abs(x - px), abs(y - py), 1)
                for step in range(steps + 1):
                    dx = round(px + (x - px) * step / steps)
                    dy = round(py + (y - py) * step / steps)
                    point(dx, dy, series_color)
            point(x, y, series_color)
            previous = x, y, timestamp
    for row, cells in enumerate(grid):
        if row:
            output.append('\n')
        reading = heading if row == 0 else detail if row == 1 else ''
        output.append(reading[:label_width - 1].ljust(label_width),
                      style=color if row == 0 else '#94a3b8')
        label = maximum_label.rjust(scale_width) if row == 0 else '0'.rjust(scale_width) if row == plot_height - 1 else ' ' * scale_width
        output.append(label + '│', style='#64748b')
        for mask, color in cells:
            output.append(chr(0x2800 + mask) if mask else ' ', style=color)
    if axis:
        axis_width = width - label_width
        output.append('\n' + ' ' * label_width + f'−{WINDOW}s' + '─' * max(0, axis_width - 7) + 'now',
                      style='#64748b')
    return output


class InferenceChart(Widget):
    DEFAULT_CSS = 'InferenceChart { width: 1fr; height: 1fr; min-height: 3; min-width: 0; }'

    def __init__(self, device_key, endpoint_host=None, **kwargs):
        super().__init__(**kwargs)
        self.device_key = device_key
        self.endpoint_host = endpoint_host
        self.history = None
        self.now = 0
        self.image = False
        self.running = False

    def show_history(self, history, now, description, image=False, running=False):
        self.history, self.now, self.image = history, now, image
        self.running = running
        self.tooltip = description
        self.refresh()

    def render(self):
        width, height = self.size
        metrics = [('steps', 'Steps/s', '#fbbf24'), ('duration', 'Time s', '#c084fc')] if self.image else [
            ('prefill', 'Prefill', '#60a5fa'), ('decode', 'Decode', '#4ade80')]
        if width < 20 or height < 3:
            output = Text()
            for index, (_, label, color) in enumerate(metrics):
                if index:
                    output.append(' / ')
                output.append(label, style=color if self.running else '#94a3b8')
            return output
        label_width = label_space(width)
        graph_width = width - label_width - 3
        show_plots = graph_width >= 2 * (SCALE_WIDTH + 2)
        if not show_plots:
            label_width = width
        widths = (graph_width // 2, graph_width - graph_width // 2)
        labels = []
        panes = []
        for pane_width, (metric, label, color) in zip(widths, metrics):
            if not self.running:
                color = '#94a3b8'
            samples = self.history.chart_samples(metric, self.now) if self.history else []
            peak = max((value for stamp, value, _ in samples
                        if value is not None and self.now - WINDOW <= stamp <= self.now), default=1)
            value = self.history.value(metric, self.now) if self.history else None
            if self.image:
                heading = f'{label} {value:.2f}' if value is not None else f'{label} —'
            else:
                heading = f'{label} {value:.0f} tok/s' if value is not None else f'{label} — tok/s'
            legend = Text(heading, style=color)
            legend.truncate(label_width - 1, overflow='ellipsis')
            labels.append(legend)
            if not show_plots:
                continue
            ceiling = (peak or 1) if self.image else max(1, ceil(peak))
            axis_label = (lambda value: f'{value:.3g}') if self.image else (lambda value: str(round(value)))
            graph = plot(samples, pane_width, height - 1, '', '', color,
                         ceiling, axis_label, 0,
                         max_gap=6 if self.image else float('inf'), axis=True)
            graph_lines = list(graph.split('\n'))
            graph_lines.extend(Text() for _ in range(height - len(graph_lines)))
            for line in graph_lines:
                line.truncate(pane_width, overflow='ellipsis', pad=True)
            panes.append(graph_lines)
        output = Text()
        for index in range(height):
            if index:
                output.append('\n')
            legend = labels[index] if index < len(labels) else Text()
            legend.pad_right(label_width - legend.cell_len)
            output.append(legend)
            if show_plots:
                output.append(panes[0][index])
                output.append(' │ ', style='#344b61')
                output.append(panes[1][index])
        return output

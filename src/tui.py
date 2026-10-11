"""One-screen terminal control plane for DotAgents."""
import asyncio
from collections import deque
import os
import socket
import sys
import time
from urllib.parse import urlsplit

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical
from textual.widgets import DataTable, Footer, Header, Input, RichLog, Static

import control_plane
import harness_service
from inference_view import bindings
from charts import UsageChart, InferenceChart, MemoryCharts
from inference_speeds import SpeedHistory
from jobs import OwnedJob
import telemetry
import remote_telemetry
import inference_observations
from service_logs import ServiceLogs
from activity_log import normalize

from dashboard_view import (
    INFERENCE_COLUMNS, InferenceColumn, ObservationTable, RemoteHardware, Status,
    display, engine_label, inference_widths, model_label, plain_output,
    speed_detail, model_api_counts,
    docker_state, stack_services, stack_addresses, grouped_addresses, hardware_label,
)


class DotAgents(App):
    TITLE = 'DotAgents'
    BINDINGS = [Binding('q', 'quit', 'Quit', priority=True)]
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen, Header, Footer, FooterKey, DataTable, RichLog, Input,
    Input:focus, .datatable--header, .datatable--header-cursor,
    .footer-key--key, .footer-key--description {
        background: ansi_default;
    }
    HeaderIcon, HeaderClockSpace { display: none; }
    Footer { align: center middle; }
    #dashboard { height: 1fr; grid-size: 1 4; grid-columns: 1fr;
                 grid-rows: 10 5 5 5; overflow-y: auto; }
    #system-panel { min-height: 10; }
    #stack-panel, #inference-panel { min-height: 5; }
    .panel { height: 1fr; min-height: 4; border: round #344b61; padding: 0 1;
             border-title-color: #a4dbe8; }
    .panel:focus-within { border: round #65c7bf; }
    DataTable { height: 1fr; scrollbar-size: 1 1; }
    #stack-columns { height: 1fr; }
    #stack { width: 3fr; }
    #docker-stack { width: 2fr; border-left: solid #344b61; }
    #operation { height: 1; }
    #output { height: 1fr; scrollbar-size: 1 1; }
    #agent-task { height: 1; border: none; padding: 0; }
    #activity-panel { min-height: 5; }
    #hardware-info { height: 1; color: #a4b5c9; }
    #system-charts { height: 1fr; }
    .gpu-memory { border-left: solid #344b61; margin-left: 1; padding-left: 1; }
    #hardware-cards, .hardware-card { width: 1fr; height: 1fr; min-width: 0; }
    .hardware-title, .hardware-description { height: 1; color: #a4b5c9; text-overflow: ellipsis; }
    .remote-hardware { border-left: solid #344b61; padding-left: 1; }
    #inference { min-height: 3; }
    """

    def __init__(self, root=control_plane.ROOT):
        super().__init__(ansi_color=True)
        self.root = root
        self.closing = False
        self.refreshing = False
        self.row_details = {}
        self.models = []
        self.hardware_refreshing = False
        self.previous_cpu_ticks = None
        self.local_reading = None
        self.local_cpu = None
        self.spinner_frame = 0
        self.animated_cells = {}
        self.log_queue = deque(maxlen=2000)
        self.dropped_logs = 0
        self.service_logs = ServiceLogs(root, self.receive_log)
        self.last_snapshot = None
        self.metrics_error = None
        try:
            self.metrics_peers = [remote_telemetry.Peer(e) for e in remote_telemetry.load_endpoints(root)]
        except (OSError, ValueError) as error:
            self.metrics_peers = []
            self.metrics_error = str(error)
        self.remote_cards = [RemoteHardware(p.endpoint) for p in self.metrics_peers]
        self.metrics_refreshing = False
        self.polling = set()
        self.remote_observations = {}
        self.remote_sources = None
        self.harness_observations = {}
        self.speed_histories = {}
        self.gateway_job = OwnedJob()
        self.gateway_requested = False
        self.verified = False
        self.agent_submitting = False


    @work(group='local-inference')
    async def start_inference(self):
        if self.closing:
            return
        directory = self.root / '.local/inference-gateway'
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        descriptor = os.open(directory / 'gateway.log', os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, 'a', buffering=1) as output:
            self.service_logs.sync(self.last_snapshot.get('containers', []))
            try:
                def write(chunk):
                    output.write(chunk)
                    output.flush()
                args = [sys.executable, '-u', str(self.root / 'src/dashboard_inference.py')]
                code = await self.gateway_job.run(args, write, self.root)
                if code and not self.closing:
                    self.receive_log('gateway', f'Gateway: startup or inference exited ({code}). Read {directory / "gateway.log"}.')
            except (OSError, ValueError) as error:
                if not self.closing:
                    self.receive_log('gateway', f'Gateway: {error}')

    def compose(self) -> ComposeResult:
        yield Header()
        with Grid(id='dashboard'):
            with Vertical(id='system-panel', classes='panel with-remote' if self.metrics_peers else 'panel'):
                with Horizontal(id='hardware-cards'):
                    with Vertical(classes='hardware-card'):
                        yield Static(display(Status('Host · ' + socket.gethostname().removesuffix('.local'), 'connected')), id='local-title', classes='hardware-title')
                        yield Static('Detecting hardware…', id='hardware-info', markup=False)
                        with Vertical(id='system-charts'):
                            yield InferenceChart(device_key='local', id='inference-chart')
                            with MemoryCharts():
                                yield UsageChart('RAM', '#4ade80', id='ram-chart')
                                yield UsageChart('GPU', '#c084fc', id='gpu-chart', classes='gpu-memory')
                    yield from self.remote_cards
            with Vertical(id='stack-panel', classes='panel'):
                with Horizontal(id='stack-columns'):
                    yield ObservationTable(id='stack', cell_padding=0, cursor_type='row', cursor_foreground_priority='renderable')
                    yield ObservationTable(id='docker-stack', cell_padding=0, cursor_type='row', cursor_foreground_priority='renderable')
            with Vertical(id='inference-panel', classes='panel'):
                yield ObservationTable(id='inference', cursor_type='row', cursor_foreground_priority='renderable')
            with Vertical(id='activity-panel', classes='panel'):
                yield Static('Connecting service logs…', id='operation', markup=False)
                yield RichLog(id='output', min_width=1, wrap=True, markup=False, highlight=False, max_lines=2000)
                yield Input(placeholder='Agent task · Enter to run · /stop to cancel · Esc to leave', id='agent-task')
        yield Footer(compact=True, show_command_palette=False)

    def on_mount(self):
        for name, title in [('system', 'System'), ('stack', 'Stack'),
                            ('inference', 'Inference'), ('activity', 'Activity')]:
            self.query_one(f'#{name}-panel', Vertical).border_title = title
        for table_id, label in [('stack', 'Services'), ('docker-stack', 'Docker')]:
            table = self.query_one(f'#{table_id}', DataTable)
            table.add_column(label, width=12)
            if table_id == 'stack':
                table.add_column('IP:port', width=21)
            table.add_column('State', width=12)
        for label, width in INFERENCE_COLUMNS:
            self.query_one('#inference', DataTable).add_column(label, width=width)
        self.refresh_state()
        self.set_interval(5, self.refresh_state)
        self.set_interval(1, self.refresh_inference)
        self.set_interval(1, self.refresh_hardware)
        self.set_interval(0.1, self.animate_status)
        self.set_interval(0.2, self.flush_logs)
        self.set_interval(1, self.refresh_speed_rows)
        if self.metrics_peers:
            self.refresh_remote_hardware()
            self.set_interval(1, self.refresh_remote_hardware)
        if self.metrics_error:
            self.receive_log('metrics', 'ERROR: Configuration: ' + self.metrics_error)

    def on_resize(self, event):
        if self.is_mounted:
            available = max(25, event.size.height - 2)
            system = max(10, int(available * .4))
            stack = max(5, (available - system) // 3)
            inference = stack
            activity = available - system - stack - inference
            self.query_one('#dashboard', Grid).styles.grid_rows = (
                f'{system} {stack} {inference} {activity}'
            )

    @work(group='remote-hardware')
    async def refresh_remote_hardware(self):
        if self.metrics_refreshing or self.closing:
            return
        self.metrics_refreshing = True
        try:
            results = await asyncio.gather(*(asyncio.to_thread(peer.poll) for peer in self.metrics_peers))
            if not self.closing:
                for card, result in zip(self.remote_cards, results):
                    if result is not None:
                        card.show_reading(result)
                if self.last_snapshot:
                    self.show_inference(self.last_snapshot)
        finally:
            self.metrics_refreshing = False

    def fill_table(self, table_id, rows):
        """Keep the user's selected row and scroll position across observations."""
        table = self.query_one(f'#{table_id}', DataTable)
        cursor = table.cursor_row
        selected = table.ordered_rows[cursor].key.value if table.row_count else None
        scroll = table.scroll_offset
        self.row_details[table_id] = {key: detail for key, cells, detail in rows}
        self.animated_cells[table_id] = []
        width = max(12, table.content_size.width - 1)
        compact = width < (24 if table_id in ('stack', 'docker-stack') else 48)
        if table_id in ('stack', 'docker-stack'):
            widths = [max(table.ordered_columns[index].label.cell_len + 1,
                          max((max((line.cell_len for line in display(cells[index]).split('\n')), default=0) + 1
                               for _, cells, _ in rows), default=1))
                      for index in range(3 if table_id == 'stack' else 2)]
            if table_id == 'stack':
                widths[2] = max(len('State') + 1, widths[2])
                if sum(widths) > width:
                    widths[2] = 1
                    widths[0] = max(6, min(widths[0], width - 2))
                    widths[1] = max(1, width - widths[0] - 1)
                table.ordered_columns[2].label = Text('State' if widths[2] > 1 else '')
        elif table_id == 'inference':
            table.cell_padding = 0
            widths, hidden = inference_widths(width, [cells for _, cells, _ in rows])
            for index, column in enumerate(table.ordered_columns):
                column.label = Text('' if index in hidden else INFERENCE_COLUMNS[index][0])
            for index, column in enumerate(table.ordered_columns):
                column.label.pad_right(1)
        else:
            path_width = 9 if compact else 18
            widths = [max(6, width - path_width - 4), path_width]
        for column, column_width in zip(table.ordered_columns, widths):
            column.width = column_width
        table.clear()
        for key, cells, detail in rows:
            cells = tuple(Status('', cell.state or cell.label) if isinstance(cell, Status)
                          and widths[index] == 1 else cell for index, cell in enumerate(cells))
            rendered = [display(cell).copy() for cell in cells]
            if table_id == 'inference':
                for index, text in enumerate(rendered[:-1]):
                    if index not in hidden:
                        if index != InferenceColumn.CONNECTION:
                            text.pad_right(1)
                    else:
                        rendered[index] = Text(' ')
            table.add_row(*rendered, key=key,
                          height=max(str(cell).count('\n') + 1 for cell in cells))
            for index, cell in enumerate(cells):
                if isinstance(cell, Status) and cell.animated:
                    self.animated_cells[table_id].append((key, table.ordered_columns[index].key, cell))
        if rows:
            keys = [key for key, _, _ in rows]
            table.move_cursor(row=keys.index(selected) if selected in keys else min(cursor, len(rows) - 1),
                              animate=False, scroll=False)
            table.scroll_to(x=scroll.x, y=scroll.y, animate=False, immediate=True)

    def resize_snapshot(self):
        if self.last_snapshot is not None and not self.closing:
            self.show_snapshot(self.last_snapshot)

    def animate_status(self):
        if self.closing or not self.is_running:
            return
        self.spinner_frame += 1
        for table_id, cells in self.animated_cells.items():
            if cells:
                table = self.query_one(f'#{table_id}', DataTable)
                for row, column, status in cells:
                    table.update_cell(row, column, status.render(self.spinner_frame))

    def receive_log(self, source, line):
        if self.closing:
            return
        if source.startswith('inference/'):
            key = source.removeprefix('inference/')
            self.speed_histories.setdefault(key, SpeedHistory()).feed_log(plain_output(line))
        for part in line.splitlines():
            if not part.strip():
                continue
            if len(self.log_queue) == self.log_queue.maxlen:
                self.dropped_logs += 1
            self.log_queue.append(normalize(source, part))

    def flush_logs(self):
        if self.closing or not self.is_running:
            return
        log = self.query_one('#output', RichLog)
        width = max(20, log.scrollable_content_region.width)
        if self.dropped_logs:
            log.write(normalize('logs', f'WARN: Skipped {self.dropped_logs} older lines during a burst').render(width))
            self.dropped_logs = 0
        for _ in range(min(100, len(self.log_queue))):
            log.write(self.log_queue.popleft().render(width))
        count = len(self.service_logs.readers)
        self.query_one('#operation', Static).update(
            'Agent task running · /stop to cancel' if any(
                (self.last_snapshot or {}).get('harness', {}).get('activity', {}).get(key)
                for key in ('coordinator', 'worker', 'queued')) else
            f'Live logs · {count} sources' if count else 'Waiting for service logs')

    def check_action(self, action, parameters):
        # Text entry must accept the letter q. Esc returns to the log panel,
        # where the existing priority q binding still closes the dashboard.
        if action == 'quit' and isinstance(self.focused, Input):
            return False
        return True

    def on_key(self, event):
        if event.key == 'escape' and isinstance(self.focused, Input):
            self.query_one('#output', RichLog).focus()
            event.stop()

    def on_input_submitted(self, event: Input.Submitted):
        if event.input.id != 'agent-task':
            return
        task = event.value.strip()
        if self.closing or not task:
            return
        if task == '/stop':
            self.stop_agent()
        elif self.agent_submitting:
            self.receive_log('Agent', 'An Agent task is running. Enter /stop to cancel it.')
            return
        else:
            self.agent_submitting = True
            self.run_agent(task)
        event.input.value = ''

    @work(group='agent')
    async def run_agent(self, task):
        try:
            if not self.closing:
                await asyncio.to_thread(harness_service.web_command, self.root, task)
                self.receive_log('Agent', 'Task submitted to the browser session.')
        except (OSError, ValueError) as error:
            self.receive_log('Agent', str(error))
        finally:
            self.agent_submitting = False

    @work(group='agent-stop')
    async def stop_agent(self):
        try:
            await asyncio.to_thread(harness_service.web_command, self.root)
            self.receive_log('Agent', 'Task cancelled. Agent UI remains available.')
        except (OSError, ValueError) as error:
            self.receive_log('Agent', str(error))

    @work(group='observations')
    async def refresh_state(self):
        if self.refreshing or self.closing:
            return
        self.refreshing = True
        try:
            result = await asyncio.to_thread(control_plane.snapshot, self.root,
                include_hardware=self.local_reading is None,
                include_remote=False,
                inference_observation=({key: self.last_snapshot[key] for key in (
                    'inference', 'models', 'inference_services', 'harness', 'inference_sampled_at')}
                    if self.last_snapshot and all(key in self.last_snapshot for key in (
                        'inference', 'harness', 'inference_sampled_at')) else None))
            if not self.closing:
                self.show_snapshot(result)
        except (OSError, ValueError, KeyError) as error:
            if not self.closing:
                self.receive_log('dashboard', f'ERROR: Observation failed; displayed data may be stale: {error}')
        finally:
            self.refreshing = False

    def show_snapshot(self, result):
        if (self.last_snapshot is not None and self.last_snapshot.get('inference_sampled_at', 0)
                > result.get('inference_sampled_at', 0)):
            result = dict(result, **{key: self.last_snapshot[key] for key in (
                'inference', 'models', 'inference_services', 'inference_sampled_at')},
                harness=self.last_snapshot.get('harness', {}))
        if self.remote_observations:
            services = list(result['inference_services'])
            for source, rows in self.remote_observations.items():
                services = [service for service in services
                            if service.get('observation_source') != source]
                services.extend(rows)
            result = dict(result, inference_services=services)
        if self.harness_observations:
            services = dict(result.get('harness', {}).get('services', {}), **self.harness_observations)
            result = dict(result, harness=harness_service.observation(self.root, services))
        self.last_snapshot = result
        self.models = result['models']
        if not self.gateway_requested and result.get('platform') == 'mac-metal' and self.verified:
            self.gateway_requested = True
            self.start_inference()
        checks = {c['name']: c for c in result['checks']}
        if 'hardware' in result:
            self.show_hardware(result['hardware'])
        observed = result.get('containers', [])
        self.service_logs.sync(observed)
        gateway = next((container for container in observed if container['name'] == 'caddy'), None)
        checks['Gateway'] = {'state': control_plane.container_state(gateway) if gateway else 'unknown',
                             'detail': 'Local Caddy gateway for the dashboard, Harness, and Image workspace.'}
        addresses = stack_addresses(result)
        stack = []
        for name, state, detail in stack_services(dict(result, checks=[
                dict(check, name=name) for name, check in checks.items()])):
            endpoints = addresses[name]
            address = grouped_addresses(endpoints)
            detail += ' Endpoints: ' + (', '.join(endpoints) or 'No published TCP endpoint observed.')
            stack.append((name, (name, address, Status(state)), f'{name}: {detail}'))
        self.fill_table('stack', stack)
        applications = result.get('applications')
        if applications is None:
            applications = control_plane.application_rows(result.get('containers', []), self.root)
        containers = []
        for container in observed:
            service = next((service for service in applications
                            if service.get('compose_service') == container['name']
                            and service['status'] == 'available'), None)
            owner = service['state_owner'] if service else 'unverified'
            retained = service['retains'] if service else []
            storage = (f"Data: {owner} owns {', '.join(retained)}." if retained else
                       'Data: no application store.' if owner == 'none' else 'Data ownership unverified.')
            label = container.get('container_name') or container['name']
            state = docker_state(container)
            detail = (f"{label} · Docker · {state} · Process: {container['state']}; health check: {container.get('health', 'none')}. "
                      + 'Without a health check, healthy means the container process is running. '
                      + storage + ' ' + (service['detail'] if service else ''))
            key = service['id'] if service else 'compose:' + container['name']
            containers.append((key, (label, Status(state)), detail))
        self.fill_table('docker-stack', containers)
        self.show_inference(result)

    def device_hardware(self, row):
        if row['device_key'] == 'local':
            reading, cpu = self.local_reading, self.local_cpu
            available = reading is not None and time.monotonic() - reading['sampled_at'] <= 9
        else:
            endpoint_host = row['address'].rsplit(':', 1)[0].strip('[]')
            card = next((card for card in self.remote_cards
                         if row['device_key'] == 'remote:' + card.endpoint['device']
                         or endpoint_host == urlsplit(card.endpoint['url']).hostname), None)
            reading, cpu = (card.reading, card.cpu) if card else (None, None)
            available = bool(card and card.connected and reading
                             and time.monotonic() - reading['sampled_at'] <= 12)
        return telemetry.inference_summary(reading, cpu, available)

    def show_inference(self, result):
        services = result['inference_services']
        rows = []
        joined = bindings(result)
        now = time.monotonic()
        timestamp = result.get('inference_sampled_at', now)
        previous_connection = None
        previous_service = None
        for row in joined:
            device = plain_output(row['device_display']).replace('\n', ' ')
            model = model_label(row['model_name'])
            engine = engine_label(row['engine'])
            connection = row['connection']
            connection_key = (row['device_key'], row['connection'], row['connection_state'])
            connection_cell = ('' if connection_key == previous_connection else
                               Status('', row['connection_state']))
            previous_connection = connection_key
            hardware = self.device_hardware(row)
            detail = (row['detail'] + f' Hardware: {hardware[0]} · CPU {hardware[1]} · RAM {hardware[2]} · VRAM {hardware[3]}.'
                      + ' Usage covers the whole device, not this model. On Apple Silicon, VRAM is GPU allocation in shared RAM; do not add it to RAM usage.')
            info = hardware[0] if row['device_display'] else ''
            service_label = row['service']
            service_label = {'Chat': 'Chat / Agent', 'Image': 'Image workspace'}.get(service_label, service_label)
            service_key = (row['device_key'], service_label)
            service = '' if service_key == previous_service else service_label
            previous_service = service_key
            if row['model'] != '—' and row['model_state'] in ('loaded', 'running'):
                history = self.speed_histories.setdefault(row['key'], SpeedHistory())
                if row['device_key'] == 'local':
                    history.observe_local(row.get('speed_metrics', {}))
                else:
                    history.observe(row.get('speed_metrics', {}), row.get('sampled_at') or timestamp,
                                    row['connection_state'] == 'connected')
                detail += speed_detail(row, history, now)
            rows.append((row['key'], (connection_cell,
                                     device, info, service, model, engine,
                                     Status(row['state'])), detail))
        self.fill_table('inference', rows)
        self.show_system_speeds(joined, now, timestamp)
        represented = {row['key'] for row in joined}
        for key in list(self.speed_histories):
            if key not in represented:
                del self.speed_histories[key]
        connected, total = model_api_counts(services)
        summary = f"Model APIs: {connected}/{total} connected"
        self.query_one('#inference-panel', Vertical).border_subtitle = summary

    def show_system_speeds(self, rows, now, sampled_at):
        loaded = [row for row in rows if row['model'] != '—' and row['model_state'] in ('loaded', 'running')]
        for chart in self.query(InferenceChart):
            candidates = [row for row in loaded if row['device_key'] == chart.device_key
                          or (chart.endpoint_host and row['address'].rsplit(':', 1)[0].strip('[]') == chart.endpoint_host)]
            candidates.sort(key=lambda row: (row['state'] != 'running',
                                             not bool(self.speed_histories[row['key']].latest)))
            row = candidates[0] if candidates else None
            history = self.speed_histories[row['key']] if row else None
            image = bool(row and row['service'] == 'Image')
            detail = (row['device'] + ' · ' + row['service'] + ' · ' + row['model_name'] +
                      speed_detail(row, history, now)) if row else 'No loaded model confirmed on this device.'
            running = bool(row and row['state'] == 'running' and row['connection_state'] == 'connected'
                           and 0 <= now - (row.get('sampled_at') or sampled_at) <= 3)
            chart.show_history(history, now, plain_output(detail), image=image, running=running)

    def refresh_speed_rows(self):
        if self.last_snapshot is not None and not self.closing and self.is_running:
            self.show_inference(self.last_snapshot)

    @work(group='inference-observations')
    async def refresh_inference(self):
        if self.closing or self.last_snapshot is None:
            return
        self.poll_source('local', control_plane.observe_inference, self.root, self.models,
                         include_remote=False)
        for role in ('chat', 'agent'):
            self.poll_source('harness:' + role, harness_service.observe_role, role)
        try:
            endpoints = inference_observations.load_endpoints(self.root)
            self.remote_sources = {'remote:' + endpoint['id'] for endpoint in endpoints}
            removed = set(self.remote_observations) - self.remote_sources
            for source in removed:
                del self.remote_observations[source]
            if removed:
                self.show_snapshot(dict(self.last_snapshot, inference_services=[
                    service for service in self.last_snapshot['inference_services']
                    if service.get('observation_source') not in removed]))
            for endpoint in endpoints:
                self.poll_source('remote:' + endpoint['id'], inference_observations.observe, endpoint)
        except (OSError, ValueError, KeyError) as error:
            self.receive_log('dashboard', f'ERROR: Endpoint configuration: {error}')

    @work(group='source-observations')
    async def poll_source(self, source, observe, *args, **kwargs):
        if source in self.polling or self.closing:
            return
        self.polling.add(source)
        sampled_at = time.monotonic()
        try:
            result = await asyncio.to_thread(observe, *args, **kwargs)
            if self.closing or self.last_snapshot is None:
                return
            if source == 'local':
                remote = [service for service in self.last_snapshot['inference_services']
                          if service.get('node', {}).get('location') != 'local']
                result['inference_services'] += remote
                self.show_snapshot(dict(self.last_snapshot, **result))
            elif source.startswith('remote:'):
                if self.remote_sources is not None and source not in self.remote_sources:
                    return
                self.remote_observations[source] = [dict(row, sampled_at=sampled_at,
                    observation_source=source) for row in result]
                self.show_snapshot(self.last_snapshot)
            else:
                self.harness_observations[source.removeprefix('harness:')] = result
                self.show_snapshot(self.last_snapshot)
        except (OSError, ValueError, KeyError) as error:
            if not self.closing:
                self.receive_log('dashboard', f'ERROR: {source} observation failed: {error}')
        finally:
            self.polling.discard(source)

    @work(group='hardware')
    async def refresh_hardware(self):
        if self.hardware_refreshing or self.closing:
            return
        self.hardware_refreshing = True
        try:
            reading = await asyncio.to_thread(telemetry.read, self.root, control_plane.capture)
            if not self.closing:
                self.show_hardware(reading)
        except (OSError, ValueError):
            self.query_one('#system-panel', Vertical).border_subtitle = 'Probe failed · readings stale'
        finally:
            self.hardware_refreshing = False

    def show_hardware(self, reading):
        timestamp = reading['sampled_at']
        if timestamp <= getattr(self, 'hardware_timestamp', -1):
            return
        self.hardware_timestamp = timestamp
        chip = hardware_label(reading)
        info = self.query_one('#hardware-info', Static)
        info.update(display(chip))
        info.tooltip = display(f"{chip} · {reading['os']}")
        cpu = telemetry.cpu_usage(self.previous_cpu_ticks, reading.get('cpu_ticks'))
        self.previous_cpu_ticks = reading.get('cpu_ticks')
        self.local_reading, self.local_cpu = reading, cpu
        shared = reading['memory'].get('shared', False)
        panel = self.query_one('#system-panel', Vertical)
        if panel.has_class('shared-memory') != shared:
            panel.set_class(shared, 'shared-memory')
        gpus = reading['gpus']
        gpu_capacity = ({'used': sum(g['used'] for g in gpus),
                         'total': max(g['total'] for g in gpus) if shared else sum(g['total'] for g in gpus)}
                        if gpus and all(g['used'] is not None and g['total'] for g in gpus)
                        else {'used': None, 'total': None})
        for metric, chart_id in [(reading['memory'], 'ram-chart'), (gpu_capacity, 'gpu-chart')]:
            used, total = metric.get('used'), metric.get('total')
            ratio = used / total if used is not None and total else None
            detail = f'{used / 2**30:.1f}/{total / 2**30:.0f} GiB' if ratio is not None else 'Unavailable'
            tooltip = ('RAM: ' if chart_id == 'ram-chart' else 'GPU allocation: ') + detail
            if shared:
                tooltip += ' · GPU allocation uses shared RAM; do not add the readings.'
            chart = self.query_one('#' + chart_id, UsageChart)
            chart.sample(timestamp, ratio, detail)
            chart.tooltip = display(tooltip)
        self.query_one('#system-panel', Vertical).border_subtitle = (
            '1s samples · speed & usage / time')
        if self.last_snapshot:
            self.show_inference(self.last_snapshot)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted):
        if not event.data_table.has_focus:
            return
        detail = self.row_details.get(event.data_table.id, {}).get(event.row_key.value)
        if detail:
            event.data_table.tooltip = display(detail)

    def action_refresh(self):
        self.refresh_state()
        self.refresh_remote_hardware()

    def action_quit(self):
        if self.closing:
            return
        self.closing = True
        self.query_one('#operation', Static).update('Stopping inference…')
        self.finish_quit()

    @work(group='shutdown')
    async def finish_quit(self):
        try:
            await asyncio.gather(self.gateway_job.stop(), self.service_logs.close())
        finally:
            self.exit()

    async def on_unmount(self):
        self.closing = True
        try:
            await self.gateway_job.stop()
        finally:
            await self.service_logs.close()

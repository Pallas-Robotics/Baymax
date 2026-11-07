"""Local activity telemetry. Includes requested speech transcripts; no keys or tool arguments."""
import atexit
import _thread
from collections import deque
import json
import os
from pathlib import Path
import threading
import time
from features.conversational_ability.timing import ConversationTiming

timing = ConversationTiming()

PATH = Path(__file__).with_name('.runtime-status.json')
STOP_PATH = Path(__file__).with_name('.runtime-stop.json')
lock = threading.RLock()
events = deque(maxlen=80)
transcripts = deque(maxlen=60)
active_transcripts = {}
transcript_sequence = 0
state = 'starting'
detail = 'Starting Ember'
activities = {}
active_tool_events = {}
activity_sequence = 0
started = False
stop_event = threading.Event()
event_observer = None  # Optional non-blocking state consumer (MVP eyes).
eye_expression_handler = None
transcript_observer = None  # Optional (speaker, {'id', 'text'}) transcript consumer.
transcript_listeners = []  # More (speaker, {'id', 'text'}) consumers (e.g. 'Ember disengage').
waiting_audio = None
echo_text = {'suspected': False}
sensors = {}  # Sensor name -> time.monotonic() of its last sign of life (MVP eyes watchdog).


def sensor_alive(name):
    sensors[name] = time.monotonic()


def notify(event):
    global event_observer, waiting_audio
    if waiting_audio is not None and event not in ('reply_playback_done', 'mic_live', 'mic_stalled'):
        try:
            waiting_audio.event(event)
        except Exception:
            waiting_audio = None
            print("[MONITOR] Waiting audio disabled after an error.", flush=True)
    observer = event_observer
    if observer is not None:
        try:
            observer(event)
        except Exception:
            event_observer = None
            print("[MONITOR] State observer disabled after an error.", flush=True)


def emit(next_state, message):
    global state, detail, echo_text
    with lock:
        if (state, detail) != (next_state, message):
            events.append({'time': time.time(), 'state': next_state, 'message': message})
        state, detail = next_state, message
        if next_state in ('connecting', 'reconnecting', 'stopping', 'stopped'):
            echo_text = {'suspected': False}
    notify('reply_complete' if next_state == 'listening' and message.startswith('Reply complete') else next_state)


def record(event_state, message):
    """Record a passive feature event without replacing conversation state."""
    with lock:
        events.append({'time': time.time(), 'state': event_state, 'message': message})


def activity(name, running=True, background=False):
    global activity_sequence
    with lock:
        now = time.time()
        if running:
            activity_sequence += 1
            activities[name] = {'background': background, 'since': now}
            event = {'id': 'tool-' + str(activity_sequence), 'time': now, 'state': 'toolkit',
                     'name': name, 'started_at': now, 'finished_at': None, 'background': background,
                     'message': 'Started ' + name + (' (background)' if background else '')}
            active_tool_events[name] = event
            events.append(event)
        else:
            activities.pop(name, None)
            event = active_tool_events.pop(name, None)
            if event is not None:
                event.update(finished_at=now, message='Finished ' + name + (' (background)' if background else ''))


def transcribe(speaker, fragment):
    """Append exact Gemini transcription deltas, preserving token whitespace."""
    global transcript_sequence, echo_text
    if speaker not in ('user', 'ember') or not isinstance(fragment, str) or not fragment:
        return
    if speaker == 'user' and fragment.strip():
        notify('user_speech')
    with lock:
        if not fragment.strip() and speaker not in active_transcripts:
            return
        for other in list(active_transcripts):
            if other != speaker:
                active_transcripts[other]['live'] = False
                active_transcripts.pop(other)
        message = active_transcripts.get(speaker)
        if message is None:
            transcript_sequence += 1
            message = {'id': transcript_sequence, 'speaker': speaker, 'text': '', 'time': time.time(), 'live': True}
            transcripts.append(message)
            active_transcripts[speaker] = message
        message['text'] = (message['text'] + fragment)[-8000:]
        message['updated_at'] = time.time()
        if speaker == 'user' and fragment.strip():
            timing.transcript(speaker, message['id'])
        if speaker == 'user':
            from features.conversational_ability.echo import transcript_overlap
            now = time.time()
            recent = [entry['text'] for entry in transcripts if entry['speaker'] == 'ember'
                      and 0 <= now - entry.get('updated_at', entry['time']) <= 8]
            match = transcript_overlap(message['text'], recent)
            if match['suspected'] and (not echo_text.get('suspected') or echo_text.get('message_id') != message['id']):
                events.append({'time': now, 'state': 'audio',
                               'message': 'Possible speaker echo: transcript overlaps recent Ember speech; user speech retained.'})
            echo_text = {**match, 'time': now, 'message_id': message['id']}
        seen = {'id': message['id'], 'text': message['text']}
    observer = transcript_observer
    if observer is not None:
        try:
            observer(speaker, seen)
        except Exception:
            print("[MONITOR] Transcript observer failed.", flush=True)
    for listener in list(transcript_listeners):
        try:
            listener(speaker, seen)
        except Exception:
            print("[MONITOR] Transcript listener failed.", flush=True)


def finish_transcripts(speaker=None):
    with lock:
        for role in list(active_transcripts):
            if speaker is None or role == speaker:
                active_transcripts.pop(role)['live'] = False


def snapshot(metrics=None):
    if metrics is not None and metrics.get('playback_bytes') == 0:
        notify('reply_playback_done')
    from features.toolkits.fall_detection import worker as fall_worker
    fall_status = fall_worker.status()
    with lock:
        current = state
        if metrics and metrics.get('playback_bytes', 0) > 0 and current in ('listening', 'thinking', 'speaking'):
            current = 'speaking'
        return {'pid': os.getpid(), 'heartbeat': time.time(), 'state': current, 'detail': detail,
                'echo_diagnostics': {'transcript_match': dict(echo_text) if 0 <= time.time() - echo_text.get('time', 0) <= 8 else {'suspected': False}},
                'activities': dict(activities), 'events': [dict(event) for event in events], 'metrics': metrics or {},
                'sensor_age_s': {name: round(time.monotonic() - seen, 1) for name, seen in dict(sensors).items()},
                'transcripts': [dict(message) for message in transcripts],
                'conversation_timing': timing.snapshot(),
                'speech_detection': timing.speech_status(),
                'capabilities': {'memory': 'Disabled in version-8 demo', 'google_search': _search_status(),
                                 'toolkit': 'Location, weather and optional experimental fall detection',
                                 'fall_detection': fall_status}}


def _search_status():
    if os.getenv('BAYMAX_TOOLKIT_ENABLED', '1') == '0' or os.getenv('BAYMAX_GOOGLE_SEARCH', '1') == '0':
        return 'Disabled'
    return 'Gemini built-in grounding'


def write_status(metrics=None):
    temporary = PATH.with_suffix('.tmp')
    try:
        temporary.write_text(json.dumps(snapshot(metrics)), encoding='utf-8')
        os.replace(temporary, PATH)
    except OSError:
        # Diagnostics must never interrupt conversation or shutdown.
        pass


def start(sample=lambda: {}):
    global started
    if started:
        return
    started = True
    def worker():
        while not stop_event.is_set():
            if consume_stop_request():
                emit('stopping', 'Stop requested from clickable launcher')
                write_status()
                _thread.interrupt_main()
            try:
                metrics = sample()
            except Exception:
                metrics = {}
            write_status(metrics)
            stop_event.wait(.2)
    threading.Thread(target=worker, name='ember-monitor', daemon=True).start()
    atexit.register(stop)


def consume_stop_request():
    try:
        request = json.loads(STOP_PATH.read_text(encoding='utf-8'))
        matches = request.get('pid') == os.getpid() and 0 <= time.time() - request.get('requested_at', 0) < 15
        STOP_PATH.unlink(missing_ok=True)
        return matches
    except (OSError, ValueError, TypeError):
        return False


def stop():
    stop_event.set()
    finish_transcripts()
    emit('stopped', 'Ember stopped')
    write_status()


def read_status(path=PATH, now=None):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            raise ValueError('Invalid monitor state')
        if (now if now is not None else time.time()) - value['heartbeat'] > 5:
            value.update(state='offline', detail='Ember is offline; showing the last session', activities={})
            for message in value.get('transcripts', []):
                message['live'] = False
        return value
    except (OSError, ValueError, KeyError, TypeError):
        return {'state': 'offline', 'detail': 'Ember is not running', 'events': [], 'activities': {}}
